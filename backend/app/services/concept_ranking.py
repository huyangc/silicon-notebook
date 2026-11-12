"""Bounded, read-only concept mention rankings (design 2026-11-12 §4 / §13.1).

Counts deliberately use distinct owner sources, rather than evidence support.
The executor never writes the graph or asks a model to compute a count.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Optional, Sequence, Tuple

from app.domain.concept_ranking_contracts import (
    CONCEPT_RANK_CITATIONS_PER_ROW,
    CONCEPT_RANK_PAGE_SIZE,
    CONCEPT_RANK_PRECOMPUTED_ROWS,
    CONCEPT_RANK_QUERY_MAX_CHARS,
    CONCEPT_RANK_RECOUNT_BATCH_ROWS,
    RANK_PATH_PRECOMPUTED,
    RANK_PATH_QUERY,
    RANK_PATH_SCOPE_FULL,
    RANK_REASON_ANALYSIS_UNAVAILABLE,
    RANK_REASON_NOT_COMPUTED,
    RANK_REASON_RANK_FAILED,
    RANK_REASON_RECOUNT_BUDGET_EXHAUSTED,
    RANK_REASON_REFRESH_PRODUCED_NOTHING,
    RANK_REASON_SCOPED_LARGE_NOTEBOOK,
    RANK_SCOPE_ALL,
    RANK_SCOPE_CURRENT_NOTEBOOK,
    RANK_SKIP_READ_FAILED,
    RANK_SKIP_TIMEOUT,
    RANK_STATUS_OK,
    RANK_STATUS_UNAVAILABLE,
    RANK_STATUS_UNSUPPORTED,
    top_concept_ranks,
)
from app.domain.kg_analysis_contracts import ARTIFACT_FINDINGS
from app.domain.kg_findings_contracts import RULE_DUPLICATE_IMPORT
from app.domain.kg_merge_seed import concept_squash_key
from app.domain.retrieval_control import RetrievalControlError
from app.repositories.memory_suggest_rows import json_list_value
from app.repositories.read_budget import classify_read_failure, read_budget
from app.services.cancellation import AskCancelled, raise_if_cancelled
from app.services.concept_ranking_paging import (
    rank_fingerprint,
    rank_next_cursor,
    rank_page_rows,
    rank_page_valid,
)
from app.services.federated_run import current_federated_run_plan
from app.services.retrieval_participants import federated_ask_active
from app.services.source_scope import (
    collection_ceiling_drifted,
    current_skipped_mounted_libraries,
    current_source_scope,
    subjectless_run_active,
)


class RankRequestError(ValueError):
    """Invalid ranking parameters."""


class RankCursorError(ValueError):
    """Invalid cursor or changed query identity."""


class RankCursorStaleError(RankCursorError):
    """The cursor's graph or source ceiling changed."""


@dataclass(frozen=True)
class ConceptRankRequest:
    query: str = ""
    scope: str = RANK_SCOPE_ALL
    cursor: str = ""
    page_size: int = CONCEPT_RANK_PAGE_SIZE


@dataclass(frozen=True)
class ConceptRankEvidence:
    element_id: str
    source_id: str
    notebook_id: str


@dataclass(frozen=True)
class ConceptRankRow:
    notebook_id: str
    canonical_id: str
    name: str
    source_count: int
    object_count: int
    same_name_group: str
    evidence: Tuple[ConceptRankEvidence, ...]
    tier: str


@dataclass(frozen=True)
class LibraryRankCoverage:
    notebook_id: str
    notebook_name: str
    status: str = RANK_STATUS_OK
    reason: str = ""
    path: str = ""
    scoped: bool = False
    drifted: bool = False
    scope_sources: int = 0
    candidates_lexical_only: bool = False
    stale_counts: bool = False
    seq_behind: int = 0
    recount_capped: bool = False
    recount_skipped: int = 0
    unnamed_dropped: int = 0
    duplicate_import_pairs: int = 0
    exact: bool = False
    skipped: str = ""


@dataclass(frozen=True)
class LibraryRanking:
    coverage: LibraryRankCoverage
    rows: Tuple[ConceptRankRow, ...]
    query_candidates_digest: str = ""


@dataclass(frozen=True)
class ConceptRankComputation:
    fingerprint: str
    query: str
    scope: str
    libraries: Tuple[LibraryRanking, ...]
    seqs: Tuple[Tuple[str, int, int], ...]
    ceiling_digest: str


@dataclass(frozen=True)
class ConceptRankPage:
    computation: ConceptRankComputation
    page_number: int
    page_size: int
    next_cursor: str

    def page_rows(self) -> Tuple[ConceptRankRow, ...]:
        return rank_page_rows(self.computation, self.page_number, self.page_size)

    def failed_libraries(self) -> Tuple[LibraryRankCoverage, ...]:
        return tuple(lib.coverage for lib in self.computation.libraries
                     if lib.coverage.status != RANK_STATUS_OK or lib.coverage.skipped)


def _decode_cursor(raw: str, fingerprint: str, page_size: int) -> dict:
    try:
        data = json.loads(base64.b64decode(
            raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        if (not isinstance(data, dict) or data.get("v") != 2
                or data.get("f") != fingerprint or type(data.get("p")) is not int
                or not 0 <= data["p"] < CONCEPT_RANK_PRECOMPUTED_ROWS
                or type(data.get("l")) is not int or data["l"] != page_size
                or not isinstance(data.get("s"), list) or not isinstance(data.get("d"), str)):
            raise ValueError("cursor shape")
        return data
    except (ValueError, TypeError, UnicodeError) as exc:
        raise RankCursorError("invalid ranking cursor") from exc


def _recount_batches(canonicals, counts):
    batch, rows = [], 0
    for canonical in canonicals:
        count = int(counts.get(canonical, 1))
        if batch and rows + count > CONCEPT_RANK_RECOUNT_BATCH_ROWS:
            yield batch, rows
            batch, rows = [], 0
        batch.append(canonical)
        rows += count
    if batch:
        yield batch, rows


def _query_cursor_scope_digest(digest: str) -> str:
    """Unpack the scope identity before doing any fresh candidate retrieval."""
    try:
        parts = json.loads(digest)
        if (not isinstance(parts, list) or len(parts) != 2
                or not all(isinstance(part, str) for part in parts)):
            raise ValueError("query cursor digest shape")
        return parts[0]
    except (ValueError, TypeError) as exc:
        raise RankCursorError("invalid query ranking cursor digest") from exc


def _query_cursor_digest(scope_digest: str, libraries: Sequence[LibraryRanking]) -> str:
    candidates = [(library.coverage.notebook_id, library.query_candidates_digest)
                  for library in libraries]
    summary = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
    return json.dumps([scope_digest, summary], separators=(",", ":"))


class ConceptRankingService:
    def __init__(self, *, database, unified_kg, sources, catalog, enumeration, settings,
                 producible: Callable[[str], bool], refresh_settled: Callable[[str], bool],
                 notebook_names: Callable[[Sequence[str]], Mapping[str, str]]) -> None:
        self._database = database
        self._unified_kg = unified_kg
        self._sources = sources
        self._catalog = catalog
        self._enumeration = enumeration
        self._settings = settings
        self._producible = producible
        self._refresh_settled = refresh_settled
        self._notebook_names = notebook_names

    def rank(self, active_notebook_id: str, request: ConceptRankRequest, *, retrieval,
             ceiling_binds: bool, cancel_event=None, memo: Optional[dict] = None,
             participants: Optional[Sequence[str]] = None,
             bind_query_candidates: bool = False) -> ConceptRankPage:
        raise_if_cancelled(cancel_event)
        query = request.query.strip()
        if len(query) > CONCEPT_RANK_QUERY_MAX_CHARS:
            raise RankRequestError("ranking query too long")
        if request.scope not in (RANK_SCOPE_ALL, RANK_SCOPE_CURRENT_NOTEBOOK):
            raise RankRequestError("invalid ranking scope")
        if type(request.page_size) is not int or not 1 <= request.page_size <= CONCEPT_RANK_PAGE_SIZE:
            raise RankRequestError("invalid ranking page size")
        if participants is None:
            notebook_ids, tiers = self._enumeration.rank_participants(active_notebook_id)
            notebook_ids = tuple(notebook_ids)
        else:
            notebook_ids, tiers = tuple(participants), {}
        subjectless = subjectless_run_active()
        if request.scope == RANK_SCOPE_CURRENT_NOTEBOOK and not subjectless:
            notebook_ids = tuple(nb for nb in notebook_ids if nb == active_notebook_id)
        fingerprint = rank_fingerprint(query, request.scope, notebook_ids)
        cursor = _decode_cursor(request.cursor, fingerprint, request.page_size) if request.cursor else None
        bind_candidates = bind_query_candidates and bool(query)
        if bind_candidates:
            # MCP has no run memo: each page must observe fresh candidate recall.
            memo = None
        cursor_scope_digest = (_query_cursor_scope_digest(cursor["d"])
                               if cursor and bind_candidates else cursor["d"] if cursor else "")
        computation = memo.get(fingerprint) if memo is not None else None
        call_deadline = time.monotonic() + self._settings.kg_rank_call_budget_ms / 1000
        try:
            with read_budget(call_deadline, cancel_event), self._database.connect_read() as db:
                scope = current_source_scope()
                if not subjectless and not (scope is not None and scope.narrowed is True):
                    self._catalog.confirm_fast_path(
                        db, tuple(nb for nb in notebook_ids if nb == active_notebook_id),
                        # Binding can protect another member's Memory without
                        # proving the visible universe stayed frozen. Ranking
                        # still needs the real, Memory-excluding change signal.
                        ceiling_binds=False,
                    )
                seqs = tuple((nb, *self._unified_kg.graph_seq_row(db, nb)[:2]) for nb in notebook_ids)
                digest = self._catalog.scope_ceiling_digest(db, notebook_ids, ceiling_binds=ceiling_binds)
        except Exception:
            raise_if_cancelled(cancel_event)
            raise
        raise_if_cancelled(cancel_event)
        if cursor and (cursor["s"] != [list(seq) for seq in seqs] or cursor_scope_digest != digest):
            raise RankCursorStaleError("ranking graph or source ceiling changed")
        active_drifted = bool(not subjectless and scope is not None
                              and scope.narrowed is not True
                              and collection_ceiling_drifted(scope, active_notebook_id))
        if computation is not None and (
            computation.seqs != seqs or computation.ceiling_digest != digest
            or any(library.coverage.notebook_id == active_notebook_id
                   and library.coverage.drifted != active_drifted
                   for library in computation.libraries)
        ):
            computation = None
        if computation is None:
            cache_keys = {
                nb: (fingerprint, "library", nb, kg_seq, cluster_seq, digest,
                     active_drifted if nb == active_notebook_id else False)
                for nb, kg_seq, cluster_seq in seqs
            }
            pending = tuple(nb for nb, kg_seq, cluster_seq in seqs
                            if memo is None or memo.get(cache_keys[nb]) is None)
            names = {}
            if pending:
                # Names are needed only for newly computed libraries. Their lightweight
                # batch read remains outside the computation budget, including on retries.
                names_start = time.monotonic()
                try:
                    names = self._notebook_names(pending)
                except (AskCancelled, RetrievalControlError):
                    raise
                except Exception:
                    raise_if_cancelled(cancel_event)
                call_deadline += time.monotonic() - names_start
            libraries = []
            all_cacheable = True
            plan = current_federated_run_plan() if federated_ask_active() else None
            for nb, kg_seq, cluster_seq in seqs:
                raise_if_cancelled(cancel_event)
                cache_key = cache_keys[nb]
                cached_library = memo.get(cache_key) if memo is not None else None
                if cached_library is not None:
                    libraries.append(cached_library)
                    continue
                start = time.monotonic()
                deadline = min(start + self._settings.kg_rank_recount_budget_ms / 1000, call_deadline)
                if plan is not None:
                    deadline = min(deadline, start + plan.notebook_timeout_seconds)
                coverage = LibraryRankCoverage(nb, str(names.get(nb, "")))
                if start >= call_deadline:
                    all_cacheable = False
                    libraries.append(LibraryRanking(replace(
                        coverage, status=RANK_STATUS_UNAVAILABLE, skipped=RANK_SKIP_TIMEOUT), ()))
                    continue
                try:
                    library, cacheable = self._rank_library(
                        nb, query, coverage, active_single=nb == active_notebook_id and not subjectless,
                        retrieval=retrieval, ceiling_binds=ceiling_binds,
                        deadline=deadline, call_deadline=call_deadline, cancel_event=cancel_event,
                        tier=str(tiers.get(nb, "personal")),
                    )
                    all_cacheable = all_cacheable and cacheable
                    if library is not None:
                        libraries.append(library)
                        if memo is not None and cacheable:
                            memo[cache_key] = library
                except (AskCancelled, RetrievalControlError):
                    raise
                except Exception as exc:
                    all_cacheable = False
                    raise_if_cancelled(cancel_event)
                    skipped = (RANK_SKIP_TIMEOUT if classify_read_failure(exc) == "timeout"
                               or time.monotonic() >= deadline else RANK_SKIP_READ_FAILED)
                    libraries.append(LibraryRanking(replace(
                        coverage, status=RANK_STATUS_UNAVAILABLE, skipped=skipped), ()))
            computation = ConceptRankComputation(
                fingerprint, query, request.scope, tuple(libraries), seqs, digest)
            if memo is not None and all_cacheable:
                memo[fingerprint] = computation
        if bind_candidates and all(
            library.coverage.status == RANK_STATUS_OK and not library.coverage.skipped
            and library.query_candidates_digest for library in computation.libraries
        ):
            bound_digest = _query_cursor_digest(digest, computation.libraries)
            if cursor and cursor["d"] != bound_digest:
                raise RankCursorStaleError("ranking query candidates changed")
            computation = replace(computation, ceiling_digest=bound_digest)
        page_number = cursor["p"] if cursor else 0
        if not rank_page_valid(computation, page_number, request.page_size):
            raise RankCursorError("invalid ranking cursor page")
        return ConceptRankPage(
            computation, page_number, request.page_size,
            rank_next_cursor(computation, page_number, request.page_size))

    def _rank_library(self, nb, query, coverage, *, active_single, retrieval,
                      ceiling_binds, deadline, call_deadline, cancel_event, tier):
        scope = current_source_scope()
        with read_budget(deadline, cancel_event), self._database.connect_read() as db:
            ceiling = self._catalog.source_ceiling(db, nb, ceiling_binds=ceiling_binds)
        if ceiling is not None and not ceiling.members:
            skipped = current_skipped_mounted_libraries().get(nb)
            if skipped:
                reason = RANK_SKIP_TIMEOUT if skipped in ("timeout", "queue_deadline") else RANK_SKIP_READ_FAILED
                return LibraryRanking(replace(coverage, status=RANK_STATUS_UNAVAILABLE, skipped=reason), ()), False
            return None, True
        narrowed = active_single and scope is not None and scope.narrowed is True
        drifted = bool(active_single and not narrowed and scope is not None
                       and collection_ceiling_drifted(scope, nb))
        count_ceiling = (ceiling.members if ceiling is not None
                         and (narrowed or active_single and not drifted) else None)
        show_ceiling = (ceiling.members if ceiling is not None else
                        self._catalog.fast_path_members(nb, ceiling_binds=ceiling_binds))
        with read_budget(deadline, cancel_event):
            if narrowed:
                scope_sources = len(set(self._sources.all_visible_source_ids(nb)) & count_ceiling)
            elif count_ceiling is not None:
                scope_sources = self._sources.visible_source_count(nb, ceiling=count_ceiling)
            else:
                scope_sources = self._sources.visible_source_count(nb)
            large = int(self._catalog.unscoped_usable_counts(nb).get("concept", 0)) > self._settings.kg_rank_online_max_objects
        coverage = replace(coverage, scoped=bool(narrowed), drifted=bool(drifted),
                           scope_sources=scope_sources)
        if large and narrowed:
            return LibraryRanking(replace(coverage, status=RANK_STATUS_UNSUPPORTED,
                                          reason=RANK_REASON_SCOPED_LARGE_NOTEBOOK), ()), False
        path = RANK_PATH_QUERY if query else RANK_PATH_PRECOMPUTED if large else RANK_PATH_SCOPE_FULL
        coverage = replace(coverage, path=path)
        try:
            return self._rank_contents(nb, query, coverage, retrieval=retrieval,
                                       count_ceiling=count_ceiling, show_ceiling=show_ceiling,
                                       deadline=deadline, call_deadline=call_deadline,
                                       cancel_event=cancel_event, tier=tier)
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:
            raise_if_cancelled(cancel_event)
            reason = (RANK_REASON_RECOUNT_BUDGET_EXHAUSTED
                      if classify_read_failure(exc) == "timeout" else RANK_REASON_RANK_FAILED)
            return LibraryRanking(replace(coverage, status=RANK_STATUS_UNAVAILABLE, reason=reason), ()), False

    def _rank_contents(self, nb, query, coverage, *, retrieval, count_ceiling,
                       show_ceiling, deadline, call_deadline, cancel_event, tier):
        cacheable = True
        candidate_digest = ""
        if query:
            coverage = replace(coverage, candidates_lexical_only=coverage.scoped)
            entries, coverage, cacheable, candidate_digest = self._query_rows(
                nb, query, coverage, retrieval, count_ceiling, deadline, cancel_event)
        elif coverage.path == RANK_PATH_SCOPE_FULL:
            with read_budget(deadline, cancel_event), self._database.connect_read() as db:
                entries = top_concept_ranks(self._unified_kg.concept_rank_full_rows(
                    db, nb, ceiling=count_ceiling), CONCEPT_RANK_PRECOMPUTED_ROWS)
        else:
            entries, coverage = self._precomputed_rows(nb, coverage, deadline, cancel_event)
        if coverage.status != RANK_STATUS_OK:
            return LibraryRanking(coverage, ()), False
        finish_deadline = min(call_deadline, max(
            deadline, time.monotonic() + self._settings.kg_rank_finish_budget_ms / 1000))
        rows, dropped, finished = self._named_rows(nb, entries, show_ceiling, finish_deadline, cancel_event, tier)
        cacheable = cacheable and finished
        counts = Counter(concept_squash_key(row.name) for row in rows)
        groups = {}
        for row in rows:
            key = concept_squash_key(row.name)
            if counts[key] > 1 and key not in groups:
                groups[key] = f"g{len(groups) + 1}"
        rows = tuple(replace(row, same_name_group=groups.get(concept_squash_key(row.name), "")) for row in rows)
        artifact = {}
        try:
            with read_budget(finish_deadline, cancel_event), self._database.connect_read() as db:
                artifact = self._unified_kg.kg_analysis_artifact_rows(db, nb).get(ARTIFACT_FINDINGS, {})
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:
            raise_if_cancelled(cancel_event)
            if classify_read_failure(exc) != "timeout":
                raise
            cacheable = False
        rule = artifact.get("payload", {}).get("rules", {}).get(RULE_DUPLICATE_IMPORT, {})
        pairs = int(rule.get("pairs_hit", 0)) if rule.get("status") == "ran" else 0
        exact = not coverage.drifted and dropped == 0 and (
            (coverage.path == RANK_PATH_SCOPE_FULL and not coverage.recount_capped)
            or (coverage.path == RANK_PATH_PRECOMPUTED and not coverage.stale_counts))
        return LibraryRanking(replace(coverage, unnamed_dropped=dropped,
                                      duplicate_import_pairs=pairs, exact=exact), rows,
                              query_candidates_digest=candidate_digest), cacheable

    def _query_rows(self, nb, query, coverage, retrieval, ceiling, deadline, cancel_event):
        hits = retrieval.retrieve_scored(nb, query, types=["concept"],
                                        allowed_source_ids=sorted(ceiling) if ceiling is not None else None)
        object_ids = []
        for hit in hits:
            if hit.object_type == "concept":
                object_ids.append(hit.object_id)
                if len(object_ids) >= self._settings.kg_rank_query_candidates:
                    break
        with read_budget(deadline, cancel_event), self._database.connect_read() as db:
            mapping = self._unified_kg.concept_rank_candidate_canonicals(db, nb, object_ids)
        canonicals = list(dict.fromkeys(mapping[oid] for oid in object_ids if oid in mapping))
        # Bind the entire mapped candidate set, including candidates outside the
        # retained top 80, with no dependence on retrieval order or aliases.
        candidate_digest = hashlib.sha256(json.dumps(sorted(canonicals)).encode()).hexdigest()
        with read_budget(deadline, cancel_event), self._database.connect_read() as db:
            counts = self._unified_kg.concept_rank_member_counts(db, nb, canonicals)
        entries, scanned, completed = [], 0, 0
        cacheable = True
        for batch, batch_rows in _recount_batches(canonicals, counts):
            if scanned + batch_rows > self._settings.kg_rank_recount_max_rows:
                coverage = replace(coverage, recount_capped=True)
                if completed:
                    coverage = replace(coverage, recount_skipped=len(canonicals) - completed)
                    break
            try:
                with read_budget(deadline, cancel_event), self._database.connect_read() as db:
                    entries.extend(self._unified_kg.concept_recount_rows(db, nb, batch, ceiling=ceiling))
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception as exc:
                raise_if_cancelled(cancel_event)
                if classify_read_failure(exc) != "timeout":
                    raise
                cacheable = False
                coverage = replace(coverage, recount_capped=True, recount_skipped=len(canonicals) - completed)
                break
            scanned += batch_rows
            completed += len(batch)
        if canonicals and not completed:
            coverage = replace(coverage, status=RANK_STATUS_UNAVAILABLE,
                               reason=RANK_REASON_RECOUNT_BUDGET_EXHAUSTED)
        return top_concept_ranks(entries, CONCEPT_RANK_PRECOMPUTED_ROWS), coverage, cacheable, candidate_digest

    def _precomputed_rows(self, nb, coverage, deadline, cancel_event):
        with read_budget(deadline, cancel_event), self._database.connect_read() as db:
            header = self._unified_kg.concept_rank_header(db, nb)
        if header is None:
            reason = (RANK_REASON_ANALYSIS_UNAVAILABLE if not self._producible(nb) else
                      RANK_REASON_REFRESH_PRODUCED_NOTHING if self._refresh_settled(nb) else
                      RANK_REASON_NOT_COMPUTED)
            return [], replace(coverage, status=RANK_STATUS_UNAVAILABLE, reason=reason)
        with read_budget(deadline, cancel_event), self._database.connect_read() as db:
            stored = self._unified_kg.concept_rank_precomputed_rows(db, nb)
            current = self._unified_kg.graph_seq_row(db, nb)[:2]
        stale = tuple(header) != tuple(current)
        entries, seen = [], set()
        for row in stored:
            canonical = row["current_canonical_id"] or row["canonical_id"]
            if canonical in seen:
                stale = True
                continue
            seen.add(canonical)
            entries.append((canonical, row["source_count"], row["object_count"]))
        return entries, replace(coverage, stale_counts=stale,
                                seq_behind=sum(max(0, cur - old) for cur, old in zip(current, header)))

    def _named_rows(self, nb, entries, ceiling, deadline, cancel_event, tier):
        names, evidence = {}, {}
        finished = True
        for offset in range(0, len(entries), CONCEPT_RANK_PAGE_SIZE):
            ids = [row[0] for row in entries[offset:offset + CONCEPT_RANK_PAGE_SIZE]]
            try:
                with read_budget(deadline, cancel_event), self._database.connect_read() as db:
                    names.update(self._unified_kg.concept_rank_names(
                        db, nb, ids, ceiling=ceiling, scan=self._settings.kg_rank_citation_scan))
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception as exc:
                raise_if_cancelled(cancel_event)
                if classify_read_failure(exc) != "timeout":
                    raise
                finished = False
            citations = []
            try:
                with read_budget(deadline, cancel_event), self._database.connect_read() as db:
                    citations = self._unified_kg.concept_rank_citation_rows(
                        db, nb, ids, ceiling=ceiling, scan=self._settings.kg_rank_citation_scan)
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception as exc:
                raise_if_cancelled(cancel_event)
                if classify_read_failure(exc) != "timeout":
                    raise
                finished = False
            owners = {}
            for canonical, _oid, source_id, raw in citations:
                selected = owners.setdefault(canonical, set())
                if source_id in selected or len(selected) >= CONCEPT_RANK_CITATIONS_PER_ROW:
                    continue
                selected.add(source_id)
                for entry in json_list_value(raw):
                    if (isinstance(entry, dict) and entry.get("source_id") == source_id
                            and entry.get("element_id")):
                        evidence.setdefault(canonical, []).append(ConceptRankEvidence(
                            str(entry["element_id"]), source_id, nb))
                        break
        rows = tuple(ConceptRankRow(nb, canonical, names[canonical], sources, objects, "",
                                    tuple(evidence.get(canonical, ())), tier)
                     for canonical, sources, objects in entries if names.get(canonical))
        return rows, len(entries) - len(rows), finished
