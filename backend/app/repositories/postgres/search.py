"""PostgreSQL lexical-candidate SQL, centralized to match expression indexes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from psycopg import sql

from app.repositories.like_pattern import LIKE_ESCAPE_CHAR, escape_like_pattern
from app.repositories.postgres.access_sql import (
    read_access_exists_clause,
    read_access_params,
)


PAYLOAD_NAME_EXPRESSION = '(payload ->> \'name\') COLLATE "C"'
TAGS_JSON_EXPRESSION = '(tags_json::text) COLLATE "C"'

_SEARCH_EXPRESSIONS = {
    "knowledge_payload_name": PAYLOAD_NAME_EXPRESSION,
    "memory_tags": TAGS_JSON_EXPRESSION,
    "chunk_text": "text",
    "source_element_text": "text",
    "source_title": "title",
    "source_summary": "summary",
}
@dataclass(frozen=True)
class _SearchTarget:
    table: str
    id_column: str
    text_expression: str
    notebook_column: str
    live_predicate: str = ""


@dataclass(frozen=True)
class MemoryCandidateScope:
    """Reviewed predicates that must be applied before Memory's LIMIT."""

    owner_id: str
    viewer_id: str
    notebook_id: str | None = None
    statuses: tuple[str, ...] = ()
    origin: str | None = None


_SEARCH_TARGETS = {
    (target.table, target.id_column, target.text_expression): target
    for target in (
        _SearchTarget(
            "knowledge_objects",
            "id",
            PAYLOAD_NAME_EXPRESSION,
            "notebook_id",
            "status!='deprecated'",
        ),
        _SearchTarget("memory_items", "id", TAGS_JSON_EXPRESSION, "notebook_id"),
        _SearchTarget("chunks", "id", "text", "notebook_id"),
        _SearchTarget("source_elements", "id", "text", "notebook_id"),
        _SearchTarget("sources", "id", "title", "notebook_id"),
        _SearchTarget("sources", "id", "summary", "notebook_id"),
    )
}


def lexical_candidate_sql(
    *,
    table: str,
    id_column: str,
    text_expression: str,
    scoped: bool = False,
    live_only: bool = False,
) -> str:
    """Return bounded trigram SQL for one reviewed table/expression pairing.

    The similarity value is ordering-only adapter state.  Callers expose their
    backend-neutral relevance scores after deterministic application ranking.
    Scope and lifecycle predicates are assembled structurally before the match
    expression; no caller rewrites SQL text or supplies a free-form predicate.
    """
    try:
        target = _SEARCH_TARGETS[(table, id_column, text_expression)]
    except KeyError:
        raise ValueError(
            "unsupported PostgreSQL lexical search table/expression pairing"
        ) from None
    if live_only and not target.live_predicate:
        raise ValueError("PostgreSQL lexical target has no lifecycle predicate")
    table_sql = sql.Identifier(target.table).as_string()
    id_sql = sql.Identifier(target.id_column).as_string()
    predicates = []
    if scoped:
        predicates.append(f"{sql.Identifier(target.notebook_column).as_string()}=%s")
    if live_only:
        predicates.append(target.live_predicate)
    predicates.append(
        f"({target.text_expression} OPERATOR(public.%%) %s OR "
        f"{target.text_expression} ILIKE %s)"
    )
    return (
        f"SELECT {id_sql} AS candidate_id, "
        f"public.similarity({target.text_expression}, %s) AS candidate_similarity "
        f"FROM {table_sql} "
        f"WHERE {' AND '.join(predicates)} "
        f"ORDER BY candidate_similarity DESC, {id_sql} COLLATE \"C\" "
        "LIMIT %s"
    )


LIKE_ESCAPE_CHARACTER = LIKE_ESCAPE_CHAR


def like_contains_pattern(term: str) -> str:
    """Return a literal "contains" ILIKE pattern for one lexical term.

    Recall terms are raw user text -- the whole question is itself a term -- so
    ``%``, ``_`` and ``\\`` would otherwise reach LIKE as metacharacters and
    silently widen the probe: ``set_db`` would also admit ``setXdb`` and
    ``set db``.  Escaping belongs to the LIKE arm alone; the trigram operator
    and ``public.similarity`` keep the unescaped term so ordering is unchanged.
    """
    return f"%{escape_like_pattern(term)}%"


def expression(name: str) -> str:
    try:
        return _SEARCH_EXPRESSIONS[name]
    except KeyError:
        raise ValueError("unsupported PostgreSQL lexical search expression") from None


def _candidate_rows(
    connection,
    *,
    table: str,
    id_column: str,
    text_expression: str,
    notebook_id: str,
    query: str,
    limit: int,
    live_only: bool = False,
):
    if limit <= 0:
        return []
    statement = lexical_candidate_sql(
        table=table,
        id_column=id_column,
        text_expression=text_expression,
        scoped=True,
        live_only=live_only,
    )
    return connection.execute(
        statement,
        (query, notebook_id, query, f"%{query}%", max(int(limit), 12)),
    ).fetchall()


def knowledge_candidate_rows(connection, notebook_id: str, query: str, limit: int):
    return _candidate_rows(
        connection,
        table="knowledge_objects",
        id_column="id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
        notebook_id=notebook_id,
        query=query,
        limit=limit,
        live_only=True,
    )


def chunk_candidate_rows(connection, notebook_id: str, query: str, limit: int):
    return _candidate_rows(
        connection,
        table="chunks",
        id_column="id",
        text_expression=expression("chunk_text"),
        notebook_id=notebook_id,
        query=query,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# GiST KNN early-stop for the knowledge-object name probe (default-on flag;
# POSTGRES_LEXICAL_KNN_ENABLED=false is the rollback).
#
# The legacy LATERAL orders by `similarity(...) DESC` — form B: the LIMIT cannot
# terminate early, so a common short term recomputes similarity for every
# trigram candidate in the notebook before keeping 67 rows (9.1M-row base,
# measured: 7.4s for one term, hard timeout across a multi-term question, the
# whole lexical arm dying fail-open).  A GiST `<->` scan emits rows in
# similarity order incrementally and stops at the LIMIT (measured 123ms, 60×).
#
# Two-phase, and the split is a proof rather than a heuristic: a row admitted
# ONLY by the ILIKE arm has similarity below the `%` threshold, so whenever the
# KNN page (which contains `%`-passing rows exclusively) comes back FULL, the
# legacy top-k provably contains no ILIKE-only row and the pages agree up to
# equal-similarity ties.  Only a SHORT page can be missing ILIKE-only rows, and
# for exactly those terms the legacy statement is cheap (few trigram matches),
# so phase 2 re-runs it for the short terms and replaces their rows wholesale.
#
# Within an equal-similarity tie class the two orders may pick different
# members (measured: 285 rows named exactly "DAC" at similarity 1.0) — a
# registered, accepted trade (owner decision 2026-08-06: the 60× win takes
# precedence; POSTGRES_LEXICAL_KNN_ENABLED=false is the per-deployment
# rollback for anyone needing bit-stable candidate sets).  The KNN inner scan
# carries NO tiebreak (adding one would defeat the early stop), so within such
# a class the KNN path is not even run-to-run stable: tie membership follows
# GiST traversal order, which shifts as the index is written.  The outer
# re-sort keeps the OUTPUT ordering deterministic given the set; the set
# itself is what may drift.  A recall A/B against the legacy path must
# therefore sample repeatedly — a single paired run cannot separate
# access-path differences from same-path tie jitter.
# ---------------------------------------------------------------------------

# Availability is a physical-table property.  The pg_index inspection runs once
# per (dbname, table oid) for the process lifetime; what every call still pays
# is one `to_regclass` round trip to resolve that key — the oid pins the exact
# physical table the current search_path names, so conformance tests running
# many disposable schemas inside one database cannot poison each other's
# verdicts.  The flag may be on while the operator has not (yet) built the
# index, and that must degrade silently to the legacy statement rather than
# fail or, worse, run an unindexed `ORDER BY <->` sort.  The cache never
# invalidates: dropping the index while the flag is on requires the documented
# rollback order (flag off + restart first).
_KNN_INDEX_CACHE: dict[tuple[str, int], bool] = {}


def reset_knn_index_cache() -> None:
    """Test seam: availability is otherwise cached for the process lifetime."""
    _KNN_INDEX_CACHE.clear()


# There is deliberately NO connection-free "absence hint" layered on this
# cache for the service's sizing gate.  It was built and reverted: three
# successive review rounds each found a real defect (a process-wide aggregate
# vetoes unprobed tables permanently; an instance-recorded verdict persists
# transient probe failures as absence; recording from the hinted call doubles
# the catalog round trip on the hot path).  The cost it tried to remove is one
# indexed single-row version query per unscoped probe — noise next to the
# LATERALs that follow — and is registered as an accepted trade in
# `_lexical_knn_allowed`.  Do not reintroduce a hint without solving all three.


def knn_name_index_available(connection) -> bool:
    """True iff a usable GiST trgm index covers the knowledge-object name.

    Shape-based rather than name-based on purpose: the production index may
    predate the feature under an operator-chosen name (the measured 2.5GB bench
    index), and rebuilding it to satisfy a naming convention would be pure
    waste.  Accepts both partial (`status != 'deprecated'` — the query always
    carries that predicate) and unconditional variants.

    The shape check is EXACT, not a substring sniff, because a false positive
    is a performance regression rather than an error: the KNN statement would
    run with no usable index — an unindexed distance sort per term, roughly
    legacy cost — while reporting nothing.  Two shapes a loose match admits and
    the planner then refuses: a wrapped expression (`lower(payload->>'name')`
    still CONTAINS the bare key text), and — the textbook spelling — the same
    expression WITHOUT `COLLATE "C"`, which fails the planner's collation match
    against the query's expression.  Hence key-definition equality plus an
    explicit `indcollation` check against pg_catalog."C" (the collation never
    appears in `pg_get_indexdef`'s key text, mirroring how
    `retrieval_indexes._matches_shape` checks it out-of-band).
    """
    # A catalog-read failure must answer "not available", not propagate — AND
    # it must leave the transaction usable.  The pool runs autocommit=False,
    # so a failed probe statement would otherwise poison the caller's implicit
    # transaction: returning False alone breaks the promised legacy fallback,
    # whose very next statement raises InFailedSqlTransaction and collapses
    # the lexical arm to [] (codex #463 round-1 P2).  Hence the SAVEPOINT
    # bracket: a probe failure rolls back to it and the legacy statement runs
    # on a clean state.  The failure verdict is deliberately NOT cached — a
    # transient error must not disable KNN for the process lifetime.
    try:
        connection.execute("SAVEPOINT knn_index_probe")
    except Exception:  # noqa: BLE001 — no savepoint, no probe: legacy exits
        return False
    try:
        oid_row = connection.execute(
            "SELECT to_regclass('knowledge_objects')::oid AS oid"
        ).fetchone()
        if oid_row is None or oid_row["oid"] is None:
            return False
        key = (str(connection.info.dbname), int(oid_row["oid"]))
        cached = _KNN_INDEX_CACHE.get(key)
        if cached is not None:
            return cached
        row = _knn_index_row(connection, int(oid_row["oid"]))
    except Exception:  # noqa: BLE001 — legacy is always the safe exit
        try:
            connection.execute("ROLLBACK TO SAVEPOINT knn_index_probe")
        except Exception:  # noqa: BLE001 — dead connection; nothing to salvage
            pass
        return False
    finally:
        try:
            connection.execute("RELEASE SAVEPOINT knn_index_probe")
        except Exception:  # noqa: BLE001 — released by rollback path already
            pass
    available = row is not None
    _KNN_INDEX_CACHE[key] = available
    return available


def _knn_index_row(connection, table_oid: int):
    return connection.execute(
        "SELECT 1 FROM pg_index i "
        "JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_am am ON am.oid=idx.relam "
        "WHERE i.indrelid=%s::oid "
        "AND am.amname='gist' AND i.indisvalid AND i.indisready "
        "AND NOT i.indisunique AND i.indnkeyatts=1 AND i.indnatts=1 "
        "AND (SELECT opc.opcname FROM unnest(i.indclass::oid[]) "
        "     WITH ORDINALITY op(oid,ord) "
        "     JOIN pg_opclass opc ON opc.oid=op.oid WHERE op.ord=1)"
        "    ='gist_trgm_ops' "
        "AND (SELECT ons.nspname FROM unnest(i.indclass::oid[]) "
        "     WITH ORDINALITY op(oid,ord) "
        "     JOIN pg_opclass opc ON opc.oid=op.oid "
        "     JOIN pg_namespace ons ON ons.oid=opc.opcnamespace WHERE op.ord=1)"
        "    ='public' "
        "AND i.indcollation[0]=(SELECT c.oid FROM pg_collation c "
        "     JOIN pg_namespace n ON n.oid=c.collnamespace "
        "     WHERE n.nspname='pg_catalog' AND c.collname='C') "
        "AND pg_get_indexdef(i.indexrelid,1,true) = %s "
        # pg_get_expr(pretty=true) renders the predicate WITHOUT outer parens
        # (verified against PG16: `status <> 'deprecated'::text`); accept the
        # parenthesised spelling too so a server-version drift fails open to
        # "not available" only when the predicate genuinely differs.
        "AND COALESCE(pg_get_expr(i.indpred,i.indrelid,true),'') "
        "    IN ('','status <> ''deprecated''::text',"
        "        '(status <> ''deprecated''::text)') "
        "LIMIT 1",
        # The key comparison is the deparser's EXACT rendering — no lower(),
        # no whitespace stripping.  Case-folding or space-stripping would remap
        # `payload ->> 'Name'` and `payload ->> 'na me'` onto this string even
        # though the planner cannot use either index for the query expression
        # (codex #463 round-1 P2): normalization must never reach inside a
        # quoted literal, and the cheapest way to guarantee that is to do none.
        # If a future PG major changes the deparser's spacing, detection fails
        # CLOSED to legacy speed, and the conformance suite — which builds the
        # canonical index on the real server — turns red at upgrade time.
        (table_oid, "(payload ->> 'name'::text)"),
    ).fetchone()


def _knn_candidate_rows_for_terms(
    connection, notebook_id: str, terms: list[str], per_term_limit: int
):
    """Phase 1: one KNN LATERAL per term, `%` arm only, early-stopping."""
    term_values = ",".join("(%s,%s)" for _ in terms)
    statement = (
        f"WITH lexical_terms(term_rank,term) AS (VALUES {term_values}) "
        "SELECT candidate.candidate_id,lexical_terms.term_rank,"
        "candidate.candidate_similarity FROM lexical_terms "
        "CROSS JOIN LATERAL ("
        "SELECT id AS candidate_id,"
        f"public.similarity({PAYLOAD_NAME_EXPRESSION},lexical_terms.term) "
        "AS candidate_similarity FROM knowledge_objects "
        "WHERE notebook_id=%s AND status!='deprecated' "
        f"AND {PAYLOAD_NAME_EXPRESSION} OPERATOR(public.%%) lexical_terms.term "
        f"ORDER BY {PAYLOAD_NAME_EXPRESSION} OPERATOR(public.<->) "
        "lexical_terms.term LIMIT %s"
        ") AS candidate ORDER BY lexical_terms.term_rank,"
        "candidate.candidate_similarity DESC,candidate.candidate_id COLLATE \"C\""
    )
    params = [
        value for term_rank, term in enumerate(terms)
        for value in (term_rank, term)
    ]
    params.append(notebook_id)
    params.append(int(per_term_limit))
    return connection.execute(statement, params).fetchall()


def _knowledge_rows_via_knn(
    connection, notebook_id: str, terms: list[str], per_term_limit: int
):
    """Two-phase KNN probe returning the legacy row shape and ordering."""
    knn_rows = _knn_candidate_rows_for_terms(
        connection, notebook_id, terms, per_term_limit
    )
    counts: dict[int, int] = {}
    for row in knn_rows:
        rank = int(row["term_rank"])
        counts[rank] = counts.get(rank, 0) + 1
    short_ranks = [
        rank for rank in range(len(terms))
        if counts.get(rank, 0) < int(per_term_limit)
    ]
    if not short_ranks:
        return knn_rows
    # Phase 2: the short terms rerun the LEGACY statement and their rows are
    # replaced wholesale — legacy output is exactly what the flag-off path
    # would have produced for them, so no merge policy has to be invented.
    #
    # No COST guarantee rides on this: a short `%` page says nothing about the
    # ILIKE arm's cardinality (a term like "test" can have a near-empty `%` arm
    # yet six-figure ILIKE matches), so phase 2 simply costs whatever the
    # flag-off path always cost for that term.  The overhead of the wasted KNN
    # statement is bounded by an index scan of the `%` condition, and each
    # phase runs under its own statement timeout — worst case the probe's wall
    # clock doubles; it never exceeds legacy by more than the KNN scan itself.
    legacy_rows = _candidate_rows_for_terms(
        connection,
        table="knowledge_objects",
        id_column="id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
        notebook_id=notebook_id,
        terms=[terms[rank] for rank in short_ranks],
        per_term_limit=per_term_limit,
        live_only=True,
    )
    rank_map = dict(enumerate(short_ranks))
    short_rank_set = set(short_ranks)
    merged = [
        row for row in knn_rows if int(row["term_rank"]) not in short_rank_set
    ]
    for row in legacy_rows:
        item = dict(row)
        item["term_rank"] = rank_map[int(row["term_rank"])]
        merged.append(item)
    # Restore the single-statement output ordering across both phases.
    merged.sort(key=lambda row: (
        int(row["term_rank"]),
        -float(row["candidate_similarity"] or 0.0),
        str(row["candidate_id"]),
    ))
    return merged


def _candidate_rows_for_terms(
    connection,
    *,
    table: str,
    id_column: str,
    text_expression: str,
    notebook_id: str,
    terms: list[str],
    per_term_limit: int,
    live_only: bool = False,
    allowed_source_ids: list[str] | None = None,
    authoritative_source_filter: bool = False,
):
    """Run one indexed LATERAL probe per term in a single PostgreSQL query."""
    if not terms or per_term_limit <= 0:
        return []
    try:
        target = _SEARCH_TARGETS[(table, id_column, text_expression)]
    except KeyError:
        raise ValueError(
            "unsupported PostgreSQL lexical search table/expression pairing"
        ) from None
    if live_only and not target.live_predicate:
        raise ValueError("PostgreSQL lexical target has no lifecycle predicate")

    table_sql = sql.Identifier(target.table).as_string()
    id_sql = sql.Identifier(target.id_column).as_string()
    predicates = [
        f"{sql.Identifier(target.notebook_column).as_string()}=%s",
    ]
    if live_only:
        predicates.append(target.live_predicate)
    source_params: list[object] = []
    if allowed_source_ids is not None:
        if not allowed_source_ids:
            return []
        if target.table == "knowledge_objects":
            if authoritative_source_filter:
                predicates.append(
                    f"EXISTS (SELECT 1 FROM jsonb_array_elements("
                    f"CASE WHEN jsonb_typeof({table_sql}.evidence)='array' "
                    f"THEN {table_sql}.evidence ELSE '[]'::jsonb END) ev "
                    f"WHERE ev->>'source_id'=ANY(%s))"
                )
            else:
                predicates.append(
                    f"EXISTS (SELECT 1 FROM knowledge_object_sources kos "
                    f"WHERE kos.notebook_id={table_sql}.notebook_id "
                    f"AND kos.object_id={table_sql}.{id_sql} AND kos.source_id=ANY(%s))"
                )
        elif target.table == "chunks":
            predicates.append(f"{table_sql}.source_id=ANY(%s)")
        else:
            raise ValueError("source-scoped lexical search target is unsupported")
        source_params.append(allowed_source_ids)
    # The LIKE pattern is escaped and wrapped in Python so LIKE metacharacters
    # in the term stay literal; the trigram arm keeps the raw term.
    predicates.append(
        f"({target.text_expression} OPERATOR(public.%%) lexical_terms.term OR "
        f"{target.text_expression} ILIKE lexical_terms.like_pattern "
        f"ESCAPE '{LIKE_ESCAPE_CHARACTER}')"
    )
    term_values = ",".join("(%s,%s,%s)" for _ in terms)
    statement = (
        f"WITH lexical_terms(term_rank,term,like_pattern) AS (VALUES {term_values}) "
        "SELECT candidate.candidate_id,lexical_terms.term_rank,"
        "candidate.candidate_similarity FROM lexical_terms "
        "CROSS JOIN LATERAL ("
        f"SELECT {id_sql} AS candidate_id,"
        f"public.similarity({target.text_expression},lexical_terms.term) "
        f"AS candidate_similarity FROM {table_sql} "
        f"WHERE {' AND '.join(predicates)} "
        f"ORDER BY candidate_similarity DESC,{id_sql} COLLATE \"C\" LIMIT %s"
        ") AS candidate ORDER BY lexical_terms.term_rank,"
        "candidate.candidate_similarity DESC,candidate.candidate_id COLLATE \"C\""
    )
    params = [
        value
        for term_rank, term in enumerate(terms)
        for value in (term_rank, term, like_contains_pattern(term))
    ]
    params.append(notebook_id)
    params.extend(source_params)
    params.append(int(per_term_limit))
    return connection.execute(statement, params).fetchall()


def knowledge_candidate_rows_for_terms(
    connection, notebook_id: str, terms: list[str], per_term_limit: int,
    allowed_source_ids: list[str] | None = None, *, allow_knn: bool = False,
    authoritative_source_filter: bool = False,
):
    # `allow_knn` is a hint, not a command: it engages only when the run is
    # unscoped (the source-scoped statement carries an EXISTS predicate the KNN
    # shape has no bench for) and a conforming GiST index actually exists.
    # Every other combination is the byte-identical legacy statement.
    if (
        allow_knn
        and allowed_source_ids is None
        and terms
        and per_term_limit > 0
        and knn_name_index_available(connection)
    ):
        return _knowledge_rows_via_knn(
            connection, notebook_id, terms, per_term_limit
        )
    return _candidate_rows_for_terms(
        connection,
        table="knowledge_objects",
        id_column="id",
        text_expression=PAYLOAD_NAME_EXPRESSION,
        notebook_id=notebook_id,
        terms=terms,
        per_term_limit=per_term_limit,
        live_only=True,
        allowed_source_ids=allowed_source_ids,
        authoritative_source_filter=authoritative_source_filter,
    )


def chunk_candidate_rows_for_terms(
    connection, notebook_id: str, terms: list[str], per_term_limit: int,
    allowed_source_ids: list[str] | None = None, *, allow_knn: bool = False,
    authoritative_source_filter: bool = False,
):
    # Accepted and deliberately ignored: whole-chunk text vs a short term is a
    # different length regime (trigram signatures degrade, similarity ordering
    # is near-noise) with no bench behind it yet.  Taking the kwarg keeps the
    # two candidate producers signature-compatible for the shared union seam.
    del allow_knn, authoritative_source_filter
    return _candidate_rows_for_terms(
        connection,
        table="chunks",
        id_column="id",
        text_expression=expression("chunk_text"),
        notebook_id=notebook_id,
        terms=terms,
        per_term_limit=per_term_limit,
        allowed_source_ids=allowed_source_ids,
    )


def knowledge_candidate_documents(connection, ids):
    values = list(ids)
    if not values:
        return []
    return connection.execute(
        f"SELECT id,{PAYLOAD_NAME_EXPRESSION} AS name "
        "FROM knowledge_objects WHERE id=ANY(%s) AND status!='deprecated'",
        (values,),
    ).fetchall()


def chunk_exact_candidate_rows(connection, notebook_id: str, needle: str, limit: int):
    """EXACT substring chunk candidates for the identifier fast path.

    Only `ILIKE '%needle%'` — no `OPERATOR(public.%)` similarity branch, since
    "exact" here means exact; the trigram GIN index (`idx_chunks_text_trgm`)
    accelerates ILIKE patterns just as it does the union path's.

    `%`/`_`/`\\` in the needle are escaped, and that is load-bearing rather
    than defensive: `set_db` contains LIKE's single-character wildcard, so the
    unescaped pattern would also accept `setXdb` — a silent precision loss in
    the one code path whose entire purpose is precision.

    `similarity()` supplies the ordering key (and the reported score) exactly
    as `lexical_candidate_sql` does, with the same `C`-collated id tie-break so
    a truncated window is deterministic.
    """
    if limit <= 0 or not (needle or "").strip():
        return []
    text_expression = expression("chunk_text")
    return connection.execute(
        "SELECT id AS candidate_id,source_id,section_path,"
        f"public.similarity({text_expression},%s) AS candidate_similarity "
        f"FROM chunks WHERE notebook_id=%s AND {text_expression} ILIKE %s "
        "ORDER BY candidate_similarity DESC,id COLLATE \"C\" LIMIT %s",
        (needle, notebook_id, f"%{escape_like_pattern(needle)}%", int(limit)),
    ).fetchall()


def chunk_section_rows(
    connection, notebook_id: str, source_id: str, section_path: str, limit: int
):
    """One section's chunks (that node plus its descendants) in document order.

    `ordinal` is PostgreSQL's document order, mirroring the SQLite adapter's
    `rowid` (chunk ids are random surrogates and sort meaninglessly).  The
    subtree predicate and its escaping match the SQLite adapter verbatim;
    PostgreSQL needs no ESCAPE clause because backslash is already its default
    LIKE escape character and the pattern is a bound parameter.
    """
    path = section_path or ""
    if not path or limit <= 0:
        return []
    return connection.execute(
        "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
        "s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
        "WHERE c.notebook_id=%s AND c.source_id=%s "
        "AND (c.section_path=%s OR c.section_path LIKE %s) "
        "ORDER BY c.ordinal LIMIT %s",
        (
            notebook_id,
            source_id,
            path,
            escape_like_pattern(path) + " > %",
            int(limit),
        ),
    ).fetchall()


def chunk_candidate_documents(connection, ids):
    values = list(ids)
    if not values:
        return []
    return connection.execute(
        "SELECT id,text FROM chunks WHERE id=ANY(%s)",
        (values,),
    ).fetchall()


def memory_candidate_ids(
    connection,
    query: str,
    limit: int,
    *,
    scope: MemoryCandidateScope,
    phrase_queries: Sequence[str] = (),
) -> list[str]:
    """Return bounded mixed-language Memory candidates using all v6 indexes."""
    if limit <= 0 or not query.strip():
        return []
    predicates, params = _memory_match_predicates(query, scope, phrase_queries)
    probes = memory_match_probes(query, phrase_queries)
    for probe in probes:
        params.extend([probe, probe, probe])
    params.append(max(12, int(limit)))
    rows = connection.execute(
        "SELECT id FROM memory_items WHERE "
        f"{' AND '.join(predicates)} "
        f"ORDER BY {_memory_rank_expression(probes)} DESC,"
        "id COLLATE \"C\" LIMIT %s",
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def memory_match_count(
    connection,
    query: str,
    *,
    scope: MemoryCandidateScope,
) -> int:
    """Count every scoped lexical match without materializing candidate rows."""
    if not query.strip():
        return 0
    predicates, params = _memory_match_predicates(query, scope)
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM memory_items WHERE "
        f"{' AND '.join(predicates)}",
        params,
    ).fetchone()
    return int(row["n"])


def memory_page_candidate_ids(
    connection,
    query: str,
    limit: int,
    offset: int,
    *,
    scope: MemoryCandidateScope,
) -> list[str]:
    """Return one bounded public-list page using the exact scoped matcher."""
    if limit <= 0 or not query.strip():
        return []
    predicates, params = _memory_match_predicates(query, scope)
    params.extend(
        [
            query,
            query,
            query,
            max(1, min(200, int(limit))),
            max(0, int(offset)),
        ]
    )
    rows = connection.execute(
        "SELECT id FROM memory_items WHERE "
        f"{' AND '.join(predicates)} "
        "ORDER BY GREATEST(public.similarity(title,%s),"
        "public.similarity(content_md,%s),"
        f"public.similarity({TAGS_JSON_EXPRESSION},%s)) DESC,"
        "id COLLATE \"C\" LIMIT %s OFFSET %s",
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def memory_match_probes(
    query: str, phrase_queries: Sequence[str] = ()
) -> list[str]:
    """The whole query first, then each distinct extra phrase probe.

    One ordering, used by both the match predicate and the ranking expression:
    a probe that can admit a row must also be able to rank it, or the row is
    admitted and then cut by the LIMIT before anything phrase-aware sees it.
    """
    probes: list[str] = [query]
    for phrase in phrase_queries:
        value = str(phrase).strip()
        if value and value not in probes:
            probes.append(value)
    return probes


def _memory_rank_expression(probes: Sequence[str]) -> str:
    """`GREATEST` over every probe × every searched column.

    Ranking on the full query alone discards exactly the rows the independent
    phrase probe exists to admit: a memory carrying the quoted phrase but none
    of the surrounding sentence scores near zero against that sentence, so with
    more than `limit` loose matches it never survives the cut (codex #410
    round-7 P2). Bounded by MAX_QUOTED_PHRASES + 1 probes × 3 columns.
    """
    terms = ",".join(
        "public.similarity(title,%s),public.similarity(content_md,%s),"
        f"public.similarity({TAGS_JSON_EXPRESSION},%s)"
        for _ in probes
    )
    return f"GREATEST({terms})"


def _memory_match_predicates(
    query: str,
    scope: MemoryCandidateScope,
    phrase_queries: Sequence[str] = (),
) -> tuple[list[str], list[object]]:
    """Build the one reviewed scope+match predicate shared by page and count.

    `phrase_queries` are additional INDEPENDENT match probes OR-ed into the same
    statement — one more OR group on an already-indexed predicate, not another
    round trip. They exist because this path probes the whole query as one
    value: a user-quoted phrase would otherwise only match a memory that also
    carries the surrounding sentence (codex #410 round-6 P2).
    """
    predicates = ["created_by=%s"]
    params: list[object] = [scope.owner_id]
    if scope.notebook_id is not None:
        predicates.append("notebook_id=%s")
        params.append(scope.notebook_id)
    if scope.statuses:
        predicates.append("status=ANY(%s)")
        params.append(list(scope.statuses))
    if scope.origin is not None:
        predicates.append("origin=%s")
        params.append(scope.origin)
    predicates.append(read_access_exists_clause("memory_items"))
    params.extend(read_access_params(scope.viewer_id))
    probes = memory_match_probes(query, phrase_queries)
    groups: list[str] = []
    for index, probe in enumerate(probes):
        # The phrase probes are escaped, the whole-query probe deliberately is
        # not. A phrase promises exactness, and `set_db` / `100% coverage` carry
        # LIKE's own wildcards — unescaped they would admit `setXdb` and crowd
        # real matches out of the bounded pool (codex #410 round-7 P2). The
        # whole-query arm keeps its historical unescaped pattern: widening a
        # candidate probe there is imprecise but harmless, and narrowing it now
        # would silently change Memory recall for every existing query.
        pattern = (
            f"%{query}%" if index == 0
            else f"%{escape_like_pattern(probe)}%"
        )
        params.extend([probe, pattern, probe, pattern, probe, pattern])
        groups.append(
            "("
            "title OPERATOR(public.%%) %s OR title ILIKE %s OR "
            "content_md OPERATOR(public.%%) %s OR content_md ILIKE %s OR "
            f"{TAGS_JSON_EXPRESSION} OPERATOR(public.%%) %s OR "
            f"{TAGS_JSON_EXPRESSION} ILIKE %s)"
        )
    predicates.append("(" + " OR ".join(groups) + ")")
    return predicates, params


def notebook_source_rows(connection, notebook_id: str, needle: str, limit: int):
    pattern = f"%{needle}%"
    return connection.execute(
        "SELECT * FROM sources WHERE notebook_id=%s "
        "AND source_type NOT IN ('memory','knowhow') AND "
        "(title ILIKE %s OR summary ILIKE %s OR file_name ILIKE %s) "
        "ORDER BY created_at,id COLLATE \"C\" LIMIT %s",
        (notebook_id, pattern, pattern, pattern, limit),
    ).fetchall()


def notebook_element_rows(connection, notebook_id: str, needle: str, limit: int):
    pattern = f"%{needle}%"
    return connection.execute(
        "SELECT se.*,s.title AS source_title FROM source_elements se "
        "JOIN sources s ON s.id=se.source_id "
        "WHERE s.notebook_id=%s AND s.source_type NOT IN ('memory','knowhow') "
        "AND (se.text ILIKE %s OR se.location_label ILIKE %s OR s.title ILIKE %s) "
        "ORDER BY se.ordinal LIMIT %s",
        (notebook_id, pattern, pattern, pattern, limit),
    ).fetchall()


def notebook_knowledge_rows(connection, notebook_id: str, needle: str, limit: int):
    pattern = f"%{needle}%"
    return connection.execute(
        "SELECT id,object_type,payload FROM knowledge_objects "
        "WHERE notebook_id=%s AND status!='deprecated' AND "
        f"({PAYLOAD_NAME_EXPRESSION} ILIKE %s OR "
        "(payload::text) COLLATE \"C\" ILIKE %s) "
        "ORDER BY ordinal LIMIT %s",
        (notebook_id, pattern, pattern, limit),
    ).fetchall()


def mention_claim_rows(connection, notebook_id: str):
    return connection.execute(
        f"SELECT id,{PAYLOAD_NAME_EXPRESSION} AS nm FROM knowledge_objects "
        "WHERE notebook_id=%s AND object_type='claim' AND status!='deprecated' "
        "ORDER BY ordinal",
        (notebook_id,),
    ).fetchall()


def prepare_mention_scan(connection, rows) -> None:
    connection.execute("DROP TABLE IF EXISTS pg_temp.mention_scan_claims")
    connection.execute(
        "CREATE TEMP TABLE mention_scan_claims("
        "claim_index bigint PRIMARY KEY,text text COLLATE \"C\" NOT NULL)"
    )
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO mention_scan_claims(claim_index,text) VALUES (%s,%s)",
            list(rows),
        )
    connection.execute(
        "CREATE INDEX mention_scan_claims_text_trgm ON mention_scan_claims "
        "USING gin (text public.gin_trgm_ops)"
    )


def mention_scan_matches(connection, alias: str):
    return connection.execute(
        "SELECT claim_index AS rowid FROM mention_scan_claims "
        "WHERE text OPERATOR(public.%%) %s OR text ILIKE %s ORDER BY claim_index",
        (alias, f"%{alias}%"),
    )


def drop_mention_scan(connection) -> None:
    connection.execute("DROP TABLE IF EXISTS pg_temp.mention_scan_claims")


def deterministic_lexical_score(query: str, text: str) -> float:
    """Backend-neutral score used after PostgreSQL candidate generation."""
    needle = query.casefold().strip()
    haystack = text.casefold()
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 2.0 + min(1.0, len(needle) / max(1, len(haystack)))
    grams = {needle[index : index + 2] for index in range(max(1, len(needle) - 1))}
    text_grams = {
        haystack[index : index + 2] for index in range(max(1, len(haystack) - 1))
    }
    return len(grams & text_grams) / max(1, len(grams | text_grams))


def deterministic_lexical_score_terms(terms: list[str], text: str) -> float:
    """Score multiple recall terms while building the document grams once."""
    haystack = text.casefold()
    if not terms or not haystack:
        return 0.0
    text_grams = {
        haystack[index : index + 2] for index in range(max(1, len(haystack) - 1))
    }
    best = 0.0
    for query in terms:
        needle = query.casefold().strip()
        if not needle:
            continue
        if needle in haystack:
            score = 2.0 + min(1.0, len(needle) / max(1, len(haystack)))
        else:
            grams = {
                needle[index : index + 2]
                for index in range(max(1, len(needle) - 1))
            }
            score = len(grams & text_grams) / max(1, len(grams | text_grams))
        best = max(best, score)
    return best
