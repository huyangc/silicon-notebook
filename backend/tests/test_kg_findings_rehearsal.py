"""scripts/kg_findings_rehearsal.py — the production read-only rehearsal of the
KG-analysis findings list (design 2026-10-19 §14③). Hermetic: no database.

The script's database part is exercised against a fake connection that routes each
SQL text to canned rows, so the whole orchestration (``run_rehearsal``), the
read-only wire contract (every statement a SELECT, the connection opened read-only)
and the privacy contract (no title / name unless ``--show-samples``) are all checked
without PostgreSQL. The SQL fragments the product shares with the script are pinned
against what the product store really sends.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest

from app.core.config import Settings
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from app.services import kg_findings

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "kg_findings_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("kg_findings_rehearsal", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` resolves string annotations through ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rehearsal = _load()
NOTEBOOK = "nb-rehearsal"


# ------------------------------------------------------------ the fake database
class _Lib:
    """One notebook's rows, in the shapes the script's SELECTs return."""

    def __init__(self) -> None:
        self.relations: List[Tuple[str, str, str, bool]] = []        # s, t, source_id, is_relink
        self.objects: List[Tuple[str, str, bool, str]] = []          # source, canonical, is_concept, type
        self.names: Dict[str, str] = {}                              # canonical -> canonical name
        self.titles: Dict[str, str] = {}
        self.created: Dict[str, Optional[datetime]] = {}
        self.decision_rows: List[tuple] = []                         # a, b, seed_a, seed_b, status
        self.member_names: Dict[str, List[str]] = {}
        self.visible: Dict[str, List[str]] = {}
        self.chunks: Dict[str, int] = {}
        self.concept_names: List[Optional[str]] = []
        self.state: List[tuple] = [(26, 8, 26, 0, 0, 100, 200, 30)]

    def source(self, source_id: str, concepts: Sequence[str], *, chain: bool = True,
               claims: int = 0, relink: bool = False) -> None:
        self.titles[source_id] = f"Title of {source_id} ZEBRA"
        self.created.setdefault(source_id, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.chunks[source_id] = max(1, (len(concepts) + claims) // 6)
        for cid in concepts:
            self.objects.append((source_id, cid, True, "concept"))
            self.names.setdefault(cid, f"Concept {cid[2:]}")
        for k in range(claims):
            claim = f"KL-{source_id}-{k}"
            self.objects.append((source_id, claim, False, "claim"))
            self.relations.append((claim, concepts[0], source_id, False))
        if chain:
            for a, b in zip(concepts, concepts[1:]):
                self.relations.append((a, b, source_id, relink))


def _library(*, extras: bool = True) -> _Lib:
    """Twelve mainstream sources (sliding windows over a ring pool, one giant
    component) plus: an off-topic island, a duplicated pair, a J~1/3 pair the
    product does not flag, a source with broken names, and an unlinked source."""
    lib = _Lib()
    pool = 14 * 12
    for i in range(12):
        lib.source(f"m{i:02d}", [f"K-core{(14 * i + k) % pool:03d}" for k in range(20)], claims=2)
    if not extras:
        return lib
    lib.source("x-island", [f"K-isl{k:02d}" for k in range(20)], claims=2)
    dup = [f"K-core{k:03d}" for k in range(40, 52)] + [f"K-dup{k}" for k in range(8)]
    lib.source("d-a", dup, claims=2)
    lib.source("d-b", dup[:-1], claims=2)
    shared = [f"K-third{k:02d}" for k in range(10)]
    lib.source("p-a", shared + [f"K-pa{k:02d}" for k in range(10)], claims=2)
    lib.source("p-b", shared + [f"K-pb{k:02d}" for k in range(10)], claims=2)
    broken = [f"K-brk{k}" for k in range(6)]
    lib.source("f3-broken", broken + [f"K-core{k:03d}" for k in range(60, 74)], claims=2)
    for k, cid in enumerate(broken):
        lib.names[cid] = f"L l a m a {k}"
    lib.source("f3-unlinked", [f"K-lone{k:02d}" for k in range(25)], chain=False)
    # F4 / S1: the narrow half (Q-Former / Qformer) and the wide half (Llama 3.1 / 31).
    for cid, name in (("K-qf-a", "Q-Former"), ("K-qf-b", "Qformer"),
                      ("K-ll-a", "Llama 3.1"), ("K-ll-b", "Llama 31")):
        lib.names[cid] = name
    lib.objects.extend([("m00", "K-qf-a", True, "concept"), ("m01", "K-qf-b", True, "concept"),
                        ("m02", "K-ll-a", True, "concept"), ("m03", "K-ll-b", True, "concept")])
    lib.concept_names = [lib.names[c] for c in ("K-qf-a", "K-qf-b", "K-ll-a", "K-ll-b")]
    lib.chunks["m00"] = 5
    return lib


class _Cursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def stream(self, sql, params=()):
        yield from self._conn.answer(sql, params)


class _Result:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _DictCursor:
    """The dict-row cursor `StoreReads` opens for the product store's reads."""

    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._rows: List[dict] = []

    def execute(self, sql, params=(), **kwargs):
        self._conn.store_kwargs.append(kwargs)
        self._rows = self._conn.answer_store(sql, params)
        return self

    def fetchall(self):
        return list(self._rows)


def _bound_lists(params) -> List[List[str]]:
    """The id lists a store statement bound (``id_binding.bind_ids``: one text
    parameter joined by ``ID_SEPARATOR``, or an array), in parameter order."""
    from app.domain.knowledge_contracts import USABLE_STATUSES
    from app.repositories.postgres.id_binding import ID_SEPARATOR

    lists = []
    for param in params:
        if isinstance(param, list):
            lists.append([str(value) for value in param])
        elif isinstance(param, str) and param != NOTEBOOK and param not in USABLE_STATUSES:
            lists.append(param.split(ID_SEPARATOR))
    return lists


class FakeConn:
    """Routes each statement to canned rows; records every statement it saw."""

    def __init__(self, lib: _Lib) -> None:
        self.lib = lib
        self.statements: List[str] = []
        self.store_kwargs: List[dict] = []
        self.commits = 0
        self.closed = False
        self.read_only = True

    def cursor(self, row_factory=None):
        return _Cursor(self) if row_factory is None else _DictCursor(self)

    def answer_store(self, sql: str, params) -> List[dict]:
        """The product store's own findings reads (`StoreReads`)."""
        self.statements.append(sql)
        flat = " ".join(sql.split())
        lib = self.lib
        if "AS visible" in flat:                        # kg_finding_member_name_rows
            canonicals = _bound_lists(params)[0]
            rows = []
            for cid in canonicals:
                visible = lib.visible.get(cid, [lib.names.get(cid)])
                for name in lib.member_names.get(cid, visible):
                    rows.append({"canonical_id": cid, "name": name,
                                 "visible": name in visible})
            return rows
        if "AS source_id, c.canonical_id AS canonical_id" in flat:   # display names
            canonicals, sources = _bound_lists(params)[:2]
            return [{"source_id": sid, "canonical_id": cid, "name": f"{cid}@{sid}"}
                    for cid in canonicals for sid in sources]
        raise AssertionError(f"unrouted store statement: {flat[:160]}")

    def execute(self, sql, params=()):
        return _Result(self.answer(sql, params))

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    def answer(self, sql: str, params) -> List[tuple]:
        self.statements.append(sql)
        flat = " ".join(sql.split())
        lib = self.lib
        if "current_setting('server_version')" in flat:
            return [("16.0",)]
        if flat.startswith("SELECT kg_mutation_seq"):       # (the generation subquery of
            return lib.state                                # other statements also names it)
        if flat.startswith("SELECT DISTINCT canonical_id, canonical_name"):
            return sorted(lib.names.items())
        if "FROM knowledge_relations kr" in flat:
            return lib.relations
        if "AS is_concept" in flat:
            return lib.objects
        if "FROM concept_merge_candidates" in flat:
            return lib.decision_rows
        if flat.startswith("SELECT id, title, created_at FROM sources"):
            return [(sid, lib.titles[sid], lib.created.get(sid)) for sid in params[0]]
        if flat.startswith("SELECT id, created_at FROM sources"):
            return [(sid, lib.created.get(sid)) for sid in params[0]]
        if flat.startswith("SELECT id, title FROM sources"):
            return [(sid, lib.titles[sid]) for sid in params[0]]
        if "FROM chunks" in flat:
            return list(lib.chunks.items())
        if "o.object_type='concept' AND o.status <> 'deprecated'" in flat:
            return [(name,) for name in lib.concept_names]
        raise AssertionError(f"unrouted statement: {flat[:160]}")


def _rehearse(lib: _Lib, **options: Any) -> Tuple[dict, FakeConn]:
    conn = FakeConn(lib)
    timer = rehearsal.PhaseTimer(log=lambda _line: None)
    report = rehearsal.run_rehearsal(conn, NOTEBOOK, rehearsal.Options(**options), timer)
    return report, conn


@pytest.fixture(scope="module")
def full() -> Tuple[dict, FakeConn, _Lib]:
    lib = _library()
    report, conn = _rehearse(lib, with_chunks=True, s1_impact=True)
    return report, conn, lib


# ================================================================== pure helpers
def test_quantiles_summarise_a_sample_and_an_empty_one():
    assert rehearsal.quantiles([]) == {}
    summary = rehearsal.quantiles(range(1, 101))
    assert summary["n"] == 100 and summary["min"] == 1.0 and summary["max"] == 100.0
    assert summary["p50"] == 50.5 and summary["p99"] == pytest.approx(99.01)
    assert list(summary)[:3] == ["p1", "p5", "p10"]


def test_pairs_at_least_is_exact_on_the_threshold():
    # 9/20 sits exactly on 0.45 and must count; 8/20 must not (no float shortcut).
    pairs = [(0, 1, 9, 20), (0, 2, 8, 20), (1, 2, 3, 10)]
    assert rehearsal.pairs_at_least(pairs, 0.45) == 1
    assert rehearsal.pairs_at_least(pairs, 0.41) == 1
    assert rehearsal.pairs_at_least(pairs, 0.40) == 2         # 8/20 exactly on the line
    assert rehearsal.pairs_at_least(pairs, 0.35) == 2
    assert rehearsal.pairs_at_least(pairs, 0.30) == 3         # 3/10 exactly on the line


def test_the_floor_run_restores_the_engine_threshold_even_when_it_fails():
    original = kg_findings._DUP_J
    with rehearsal.duplicate_threshold(0.3):
        assert kg_findings._DUP_J == Fraction(3, 10)
    assert kg_findings._DUP_J == original
    with pytest.raises(RuntimeError):
        with rehearsal.duplicate_threshold(0.3):
            raise RuntimeError("boom")
    assert kg_findings._DUP_J == original


def test_default_candidate_cap_is_the_product_default():
    assert rehearsal.DEFAULT_MAX_CANDIDATE_PAIRS == (
        Settings.model_fields["kg_findings_max_candidate_pairs"].default
    )


def test_default_statement_timeout_does_not_hold_a_snapshot_for_an_hour():
    """PR-2 review: a streamed full read keeps its snapshot open (vacuum waits);
    twenty minutes covers the read and stops short of an hour-long hold."""
    assert rehearsal.DEFAULT_STATEMENT_TIMEOUT_SECONDS == 1200
    assert "statement_timeout=1200000" in rehearsal.connection_options(
        rehearsal.DEFAULT_STATEMENT_TIMEOUT_SECONDS)


def test_phase_timer_records_a_phase_even_when_its_body_fails():
    ticks = iter(float(i) for i in range(100))
    lines: List[str] = []
    timer = rehearsal.PhaseTimer(clock=lambda: next(ticks), log=lines.append)
    with pytest.raises(ValueError):
        with timer.phase("bad", product=True):
            raise ValueError("x")
    with timer.phase("fine", product=False):
        pass
    assert timer.phases["bad"]["product_cost"] is True
    assert timer.phases["fine"]["product_cost"] is False
    assert timer.phases["bad"]["seconds"] > 0
    assert any("bad done" in line for line in lines)


def test_to_json_handles_numpy_scalars_and_sets():
    text = rehearsal.to_json({"a": np.int64(3), "b": np.float64(0.5), "c": np.bool_(True),
                              "d": {"y", "x"}})
    assert json.loads(text) == {"a": 3, "b": 0.5, "c": True, "d": ["x", "y"]}


def test_write_private_creates_an_owner_only_file(tmp_path):
    target = tmp_path / "report.json"
    rehearsal.write_private(str(target), "{}")
    assert target.read_text(encoding="utf-8") == "{}"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_write_private_narrows_an_existing_readable_file_before_writing(tmp_path, monkeypatch):
    """codex #941 P2: ``os.open``'s mode only applies on create — overwriting a
    group/world-readable target must still leave it owner-only, and the report
    must not land in it while it is still readable by others."""
    target = tmp_path / "report.json"
    target.write_text("old contents", encoding="utf-8")
    os.chmod(target, 0o644)
    seen: List[Any] = []
    real_fchmod = os.fchmod

    def spy(fd, mode):
        seen.append((stat.S_IMODE(os.fstat(fd).st_mode), target.read_text(encoding="utf-8")))
        real_fchmod(fd, mode)

    monkeypatch.setattr(rehearsal.os, "fchmod", spy)
    rehearsal.write_private(str(target), '{"samples": ["secret title"]}')
    assert seen == [(0o644, "")]            # narrowed while still empty
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == '{"samples": ["secret title"]}'


# ================================================================== S1 impact
def test_s1_impact_known_examples():
    names = ["Q-Former", "Qformer", "Q Former", "Llama 3.1", "Llama-3-1", "Llama 31", None, ""]
    decisions = [
        ("K-q former", "K-qformer", "q former", "qformer", "rejected"),     # collide under S1
        ("K-llama 3 1", "K-llama 31", "llama 3 1", "llama 31", "deferred"),  # collide via llama31
        ("K-q former", "K-qformer", "q former", "qformer", "confirmed"),    # confirmed: not a conflict
        ("K-nowhere", "K-qformer", "nowhere", "qformer", "rejected"),       # no current object
        ("K-q former", "K-qformer", "", "", "rejected"),                    # legacy empty seeds -> strip K-
    ]
    impact = rehearsal.s1_impact(names, decisions)
    assert impact["concept_objects"] == 6
    # v1: "q former" (x2) and "qformer" -> one v2 seed; "llama 3 1" (x2: 3.1 and 3-1)
    # splits into "llama3 1" and "llama31"; "llama 31" -> "llama31".
    assert impact["old_seeds"] == 4 and impact["s1_seeds"] == 3
    assert impact["old_seed_groups_split_by_s1"] == 1          # "llama 3 1" is torn apart
    assert impact["s1_seeds_merging_several_old"] == 2         # qformer, llama31
    assert impact["merge_only_variant_risky_merges"] == 1      # "llama31" squashed from two seeds S1 keeps apart
    assert impact["decisions"] == {"rejected": 3, "deferred": 1, "confirmed": 1}
    assert impact["decisions_with_empty_seed"] == 1
    assert impact["decisions_unmappable_to_current_objects"] == 1
    assert impact["rejected_or_deferred_that_collide_under_s1"] == 3
    assert impact["rejected_or_deferred_that_collide_by_status"] == {"rejected": 2, "deferred": 1}
    assert "split_examples" not in impact


def test_s1_impact_examples_only_with_samples():
    impact = rehearsal.s1_impact(["Llama 3.1", "Llama-3-1"], [], show_samples=True)
    assert impact["split_examples"] == [["llama 3 1", ["llama3 1", "llama31"]]]


# ================================================================ engine hooks
def test_rehearsal_calls_the_engine_functions_by_their_real_names():
    """The script measures the engine's own pure functions. A rename or a signature
    change must fail HERE (loudly), not silently measure something else."""
    expected = {
        "_drain_tally": ["tally", "structure"],
        "_duplicate_pairs": ["corpus", "cap"],
        "_shared_shares": ["corpus", "pairs"],
        "_parse_damage": ["corpus", "relations_by_source", "fragmented"],
        "_names_pass": ["names", "can2idx", "corpus"],
        "_squash_collisions": ["hashes", "indices"],
        "_resolve_canonical_ids": ["can2idx", "corpus", "needed"],
        "_merge_specs": ["corpus", "groups", "ids", "names", "adjacency"],
        "_exact_median": ["numerators", "denominators"],
    }
    for name, parameters in expected.items():
        assert list(inspect.signature(getattr(kg_findings, name)).parameters) == parameters, name
    assert isinstance(kg_findings._DUP_J, Fraction)
    assert list(inspect.signature(kg_findings._DuplicatePairs).parameters) == [
        "pairs", "candidate_pairs", "candidate_generations", "candidates_truncated",
        "universe_can", "universe_indptr",
    ]


def test_snapshot_tally_is_independent_and_shares_can2idx():
    lib = _library(extras=False)
    graph = rehearsal.scan_graph(((s, t, sid, rel) for s, t, sid, rel in lib.relations))
    structure = kg_findings.GraphStructure.from_edges(
        graph.table.src, graph.table.dst, graph.table.n_nodes)
    scan = rehearsal.scan_objects(iter(lib.objects), graph.can2idx, structure)
    snapshot = rehearsal.snapshot_tally(scan.tally, graph.can2idx)
    assert snapshot is not scan.tally and snapshot._can2idx is graph.can2idx
    mine = kg_findings._drain_tally(snapshot, structure)
    theirs = kg_findings._drain_tally(scan.tally, structure)      # the tally the engine would drain
    for column in ("objects", "isolated", "in_graph", "giant", "inc_src", "inc_can",
                   "inc_objects", "df", "concepts"):
        assert np.array_equal(getattr(mine, column), getattr(theirs, column)), column
    assert mine.source_ids == theirs.source_ids


def test_custom_object_types_are_bucketed_never_reported_by_name():
    """knowhow projections mint object types from USER column names; the isolated
    breakdown folds every unknown type into one group so the name cannot leak."""
    graph = rehearsal.scan_graph([("a", "b", "s", False)])
    structure = kg_findings.GraphStructure.from_edges(
        graph.table.src, graph.table.dst, graph.table.n_nodes)
    rows = [("s", "a", True, "concept"), ("s", "lone-1", False, "claim"),
            ("s", "lone-2", False, "周几"), ("s", "lone-3", False, "根因分析动作"),
            ("s", "lone-4", False, None)]
    scan = rehearsal.scan_objects(iter(rows), graph.can2idx, structure)
    assert scan.isolated_by_type == {"claim": 1, "other": 3}
    assert "周几" not in rehearsal.to_json({"objects": scan.isolated_by_type})


def test_scan_graph_counts_every_row_per_source_and_skips_self_loops():
    rows = [("a", "b", "s1", False), ("b", "b", "s1", True), ("a", "c", None, True),
            ("", "c", "s2", False)]
    scan = rehearsal.scan_graph(rows, keep_raw=True)
    assert scan.relation_rows == 4 and scan.relink_rows == 2
    assert scan.relations_by_source == {"s1": 2, "": 1, "s2": 1}
    assert scan.can2idx == {"a": 0, "b": 1, "c": 2}
    assert len(scan.table) == 2 and list(scan.raw.src) == [0, 0]
    assert scan.raw.ranks == {"s1": 0, "": 1}


# =============================================================== the e2e report
def test_report_agrees_with_the_engine_run(full):
    report, _conn, _lib = full
    rules = report["engine"]["rules"]
    f1, f2, f3, f4 = report["f1"], report["f2"], report["f3"], report["f4"]
    # F1: the distribution pass recomputes what the engine decided, from a snapshot.
    assert f1["hits"] == rules["F1"]["sources_hit"] >= 1
    assert f1["deferred_to_f3"] == rules["F1"]["sources_deferred_to_f3"] == 1
    assert f1["hits_before_f3_deferral"] == f1["hits"] + f1["deferred_to_f3"]
    assert f1["eligible_sources"] == rules["F1"]["sources_checked"]
    # F2.
    assert f2["duplicate_pairs"] == rules["F2"]["pairs_hit"] >= 1
    assert f2["candidate_pairs"] == rules["F2"]["candidate_pairs"]
    assert f2["candidate_pair_generations"] == rules["F2"]["candidate_pair_generations"]
    assert f2["candidates_truncated"] is False
    # F3, per sub-signal.
    assert f3["fragmented_names"]["hits"] == rules["F3"]["sources_hit_fragmented_names"] == 1
    assert f3["low_relations"]["hits"] == rules["F3"]["sources_hit_low_relations"]
    assert f3["high_isolated"]["hits"] == rules["F3"]["sources_hit_isolated"] >= 1
    assert f3["low_relations"]["median_relations_per_object"] == pytest.approx(
        rules["F3"]["median_relations_per_object"])
    assert f3["high_isolated"]["median_isolated_share"] == pytest.approx(
        rules["F3"]["median_isolated_share"])
    # F4 (wide) / S1 (narrow): no stored decisions here, so before == after.
    assert f4["wide_groups_before_decisions"] == rules["F4"]["total"] == 1
    assert f4["narrow_groups_before_decisions"] == rules["S1"]["narrow_groups"] == 1
    assert f4["narrow_group_objects"] == rules["S1"]["narrow_group_objects"]
    # F5.
    assert report["f5"]["isolated_objects"] == report["objects"]["isolated_objects"] \
        == report["engine"]["findings"]["F5"][0]["isolated_objects"]
    assert report["objects"]["usable_visible_objects"] == rules["F5"]["objects_checked"]
    # The engine's own structure numbers are the graph section's.
    assert report["graph"]["components"] == report["engine"]["structure"]["components"]
    assert report["graph"]["canonical_nodes"] == report["engine"]["structure"]["graph_nodes"]


def test_sensitivity_cells_at_the_product_thresholds_equal_the_engine_hits(full):
    report, _conn, _lib = full
    rules = report["engine"]["rules"]
    f1 = report["f1"]
    assert f1["sensitivity_before_f3_deferral"]["shared<0.05&concepts>=10"] \
        == f1["hits_before_f3_deferral"]
    f3 = report["f3"]
    assert f3["fragmented_names"]["sensitivity_hits"]["share>=0.1&names>=5"] \
        == rules["F3"]["sources_hit_fragmented_names"]
    assert f3["low_relations"]["sensitivity_hits"]["0.5"] \
        == rules["F3"]["sources_hit_low_relations"]
    assert f3["high_isolated"]["sensitivity_hits"]["factor=3&floor=0.1"] \
        == rules["F3"]["sources_hit_isolated"]
    # the grids are monotone in the obvious direction
    cells = f1["sensitivity_before_f3_deferral"]
    assert cells["shared<0.02&concepts>=10"] <= cells["shared<0.05&concepts>=10"] \
        <= cells["shared<0.1&concepts>=10"]
    assert cells["shared<0.05&concepts>=20"] <= cells["shared<0.05&concepts>=10"] \
        <= cells["shared<0.05&concepts>=5"]


def test_floor_run_counts_pairs_the_product_threshold_does_not_flag(full):
    report, _conn, _lib = full
    f2 = report["f2"]
    product = f2["duplicate_pairs"]
    # Three pairs sit just under the product threshold and are invisible to it:
    # p-a / p-b (10 shared of 30 = 1/3) and each d-copy against the ring window m03
    # (10 shared, J = 0.333 and 0.345). Only the floor run can count them.
    assert f2["pairs_j_ge_floor"]["floor"] == 0.3
    assert f2["pairs_j_ge"]["0.3"] == product + 3
    assert f2["pairs_j_ge"]["0.35"] == product
    assert f2["pairs_j_ge"]["0.45"] == product
    assert f2["pairs_j_ge_floor"]["pairs"] == f2["pairs_j_ge"]["0.3"]
    assert f2["pairs_j_ge_floor"]["candidate_pairs"] >= f2["candidate_pairs"]
    assert list(f2["pairs_j_ge"]) == sorted(f2["pairs_j_ge"], key=float)
    # and the product's candidates count does not change because of it
    assert f2["candidate_pairs"] == report["engine"]["rules"]["F2"]["candidate_pairs"]


def test_a_floor_at_or_above_the_product_threshold_skips_the_extra_run():
    report, _conn = _rehearse(_library(), j_floor=0.45)
    f2 = report["f2"]
    assert f2["pairs_j_ge_floor"]["floor"] == 0.45
    assert f2["pairs_j_ge"]["0.45"] == f2["duplicate_pairs"]
    assert "0.3" not in f2["pairs_j_ge"]
    assert kg_findings._DUP_J == Fraction("0.45")


def test_document_frequency_section(full):
    report, _conn, _lib = full
    concepts = report["concepts"]
    # K-core044 appears in m03, m04 (ring windows) and d-a / d-b: df counts sources.
    assert concepts["concepts_df_gt"]["1"] >= 1
    assert concepts["concepts_df_gt"]["1000"] == 0
    assert list(concepts["concepts_df_gt"]) == ["1", "10", "30", "100", "300", "1000"]
    assert concepts["doc_freq_dist"]["max"] <= report["objects"]["sources_with_objects"]
    assert concepts["concept_canonicals"] == concepts["doc_freq_dist"]["n"]


def test_chunk_section_uses_the_judged_sources_only(full):
    report, _conn, _lib = full
    chunks = report["f3"]["objects_per_chunk"]
    assert chunks["sources_without_chunks"] == 0
    assert chunks["objects_per_chunk_dist"]["n"] == report["objects"]["sources_with_objects"]
    assert set(chunks["sensitivity_hits"]) == {
        "yield<1/4*median", "yield<1/3*median", "yield<1/2*median"}
    without, _ = _rehearse(_library())
    assert "objects_per_chunk" not in without["f3"]


def test_empty_and_single_source_libraries_do_not_crash():
    empty, _ = _rehearse(_Lib())
    assert empty["engine"]["rules"]["F1"]["reason"] == "no_objects"
    assert empty["f1"] if "f1" in empty else True
    assert empty["concepts"]["concept_canonicals"] == 0
    lib = _Lib()
    lib.source("only", [f"K-c{k}" for k in range(20)])
    single, _ = _rehearse(lib, s1_impact=True)
    assert single["f2"]["comparable"] is False and single["f1"]["comparable"] is False
    assert single["engine"]["rules"]["F2"]["reason"] == "fewer_than_two_sources"
    assert single["f1"]["hits"] == 0 and single["f2"]["duplicate_pairs"] == 0


def test_stored_decisions_are_applied_by_the_engine_and_not_by_the_raw_counts():
    lib = _library()
    lib.decision_rows = [
        ("K-qf-a", "K-qf-b", "q former", "qformer", "rejected"),
        ("K-ll-a", "K-ll-b", "llama 3 1", "llama 31", "deferred"),
        ("K-x", "K-y", "", "", "confirmed"),
    ]
    report, _ = _rehearse(lib, s1_impact=True)
    rules = report["engine"]["rules"]
    assert report["engine"]["decisions"] == 3
    assert rules["S1"]["narrow_groups"] == 0 and rules["S1"]["groups_decided_apart"] == 1
    assert rules["F4"]["total"] == 0 and rules["F4"]["groups_decided_apart"] == 1
    # the raw numbers deliberately ignore decisions, so both halves are still visible
    assert report["f4"]["narrow_groups_before_decisions"] == 1
    assert report["f4"]["wide_groups_before_decisions"] == 1
    assert report["engine"]["member_seed_calls"]            # the seeds were really asked for


# ===================================================================== privacy
def _quoted_strings(lib: _Lib) -> List[str]:
    values = set(lib.names.values()) | set(lib.titles.values()) | set(lib.names)
    values |= set(lib.titles)                                  # source ids
    return sorted(values)


def test_default_output_contains_no_titles_names_or_ids(full):
    report, _conn, lib = full
    text = rehearsal.to_json(report)
    for value in _quoted_strings(lib):
        assert json.dumps(value, ensure_ascii=False) not in text, value
    for fragment in ("ZEBRA", "Q-Former", "Llama 3.1", "Qformer", "examples",
                     "subject_label", "subject_key", "canonical_id"):
        assert fragment not in text, fragment
    assert "samples" not in report
    # the engine's S1 status carries example NAMES; the report must have dropped them
    assert "examples" not in report["engine"]["rules"]["S1"]
    # the finding rows are numbers (and signal codes) only
    for rule_rows in report["engine"]["findings"].values():
        for row in rule_rows:
            assert all(isinstance(v, (bool, int, float, list)) for v in row.values())


def test_show_samples_adds_titles_and_names_and_nothing_else_changes():
    lib = _library()
    plain, plain_conn = _rehearse(lib, s1_impact=True)
    shown, shown_conn = _rehearse(lib, s1_impact=True, show_samples=True)
    text = rehearsal.to_json(shown)
    assert shown["script"]["show_samples"] is True
    assert "Title of x-island ZEBRA" in text                       # an F1 finding's title
    assert "Q-Former" in text and "Llama 3.1" in text              # S1 / F4 group names
    samples = shown["samples"]
    assert samples["lowest_shared_sources"][0]["title"].startswith("Title of ")
    assert any(pair["title_a"] for pair in samples["top_duplicate_pairs"])
    assert {row["half"] for row in samples["squash_group_examples"]} == {"narrow", "wide"}
    assert len(samples["findings"]["F1"]) >= 1 and "evidence" in samples["findings"]["F1"][0]
    # the numbers do not depend on the flag
    for key in ("graph", "objects", "concepts", "f1", "f2", "f3", "f4", "f5"):
        assert shown[key] == plain[key], key
    assert shown["engine"]["rules"]["F1"] == plain["engine"]["rules"]["F1"]
    # without the flag the titles are not even fetched
    assert not any("title" in s for s in plain_conn.statements if "FROM sources" in s)
    assert any("title" in s for s in shown_conn.statements if "FROM sources" in s)


# =============================================================== read-only wire
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|ANALYZE|"
    r"SET|LOCK|CALL|DO)\b", re.IGNORECASE)


def test_every_statement_the_script_sends_is_a_select(full):
    _report, conn, _lib = full
    assert len(conn.statements) > 8
    for sql in conn.statements:
        assert sql.lstrip().upper().startswith("SELECT"), sql[:80]
        assert not FORBIDDEN.search(sql), sql[:120]
    # every phase hands its snapshot back
    assert conn.commits >= 8


def test_the_query_builders_only_build_selects():
    builders = [
        rehearsal.state_query(NOTEBOOK), rehearsal.names_query(NOTEBOOK),
        rehearsal.graph_query(NOTEBOOK), rehearsal.object_query(NOTEBOOK),
        rehearsal.decisions_query(NOTEBOOK),
        rehearsal.source_facts_query(["s"], with_titles=True),
        rehearsal.source_facts_query(["s"], with_titles=False),
        rehearsal.source_titles_query(["s"]), rehearsal.chunk_counts_query(NOTEBOOK),
        rehearsal.concept_names_query(NOTEBOOK),
    ]
    for sql, _params in builders:
        assert sql.lstrip().upper().startswith("SELECT")
        assert not FORBIDDEN.search(sql), sql[:120]


def test_the_connection_is_opened_read_only(monkeypatch):
    calls: List[Tuple[tuple, dict]] = []

    class _Connection:
        read_only = False

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return _Connection()

    monkeypatch.setattr(rehearsal.psycopg, "connect", fake_connect)
    connection = rehearsal.open_readonly_connection("postgresql://u@h/db",
                                                    statement_timeout_seconds=120)
    assert connection.read_only is True
    (args, kwargs), = calls
    assert args == ("postgresql://u@h/db",)
    assert "-c default_transaction_read_only=on" in kwargs["options"]
    assert kwargs["options"].startswith("-c default_transaction_read_only=on")
    assert "-c statement_timeout=120000" in kwargs["options"]
    assert kwargs["application_name"] == "kg_findings_rehearsal"


def test_the_script_has_exactly_one_connect_call_and_it_is_read_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    connects = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"]
    assert len(connects) == 1, "every connection must go through open_readonly_connection"
    opener = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "open_readonly_connection")
    assert connects[0] in list(ast.walk(opener))
    assigned = [t.attr for node in ast.walk(opener) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Attribute)]
    assert "read_only" in assigned
    assert "default_transaction_read_only=on" in ast.get_source_segment(
        SCRIPT.read_text(encoding="utf-8"),
        next(node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == "connection_options"))


def test_resolve_dsn_is_postgres_only_and_never_echoes_the_value(monkeypatch):
    assert rehearsal.resolve_dsn("postgresql://u:pw@h/db", "X") == "postgresql://u:pw@h/db"
    monkeypatch.setenv("REHEARSAL_TEST_DSN", "postgresql://u@h/other")
    assert rehearsal.resolve_dsn(None, "REHEARSAL_TEST_DSN") == "postgresql://u@h/other"
    for bad in ("sqlite:///x.db", ".local/silicon_notebook.db", "host=h dbname=d password=secretpw"):
        with pytest.raises(rehearsal.RehearsalError) as caught:
            rehearsal.resolve_dsn(bad, "X")
        assert "secretpw" not in str(caught.value) and bad not in str(caught.value)
    monkeypatch.delenv("REHEARSAL_TEST_DSN")
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.resolve_dsn(None, "REHEARSAL_TEST_DSN")


def test_main_runs_end_to_end_and_never_prints_the_dsn(monkeypatch, tmp_path, capsys):
    seen: Dict[str, Any] = {}
    lib = _library()
    holder: List[FakeConn] = []

    def fake_connect(dsn, **kwargs):
        seen["dsn"], seen["kwargs"] = dsn, kwargs
        holder.append(FakeConn(lib))
        holder[0].read_only = False
        return holder[0]

    monkeypatch.setattr(rehearsal.psycopg, "connect", fake_connect)
    out = tmp_path / "out.json"
    code = rehearsal.main(["--dsn", "postgresql://user:hunter2@db.example/prod",
                           "--notebook", NOTEBOOK, "--out", str(out), "--s1-impact"])
    captured = capsys.readouterr()
    assert code == 0
    assert "hunter2" not in captured.out + captured.err
    assert "db.example" not in captured.out + captured.err
    assert seen["dsn"] == "postgresql://user:hunter2@db.example/prod"
    assert "default_transaction_read_only=on" in seen["kwargs"]["options"]
    assert holder[0].read_only is True and holder[0].closed is True
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["notebook"] == NOTEBOOK and report["script"]["connection_read_only"] is True
    assert "s1_impact" in report and "partition_bench" not in report
    # no --out: the JSON goes to stdout
    assert rehearsal.main(["--dsn", "postgresql://u@h/db", "--notebook", NOTEBOOK]) == 0
    assert json.loads(capsys.readouterr().out)["notebook"] == NOTEBOOK


def test_main_refuses_sqlite_without_connecting(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(rehearsal.psycopg, "connect", boom)
    assert rehearsal.main(["--dsn", "sqlite:///x.db", "--notebook", NOTEBOOK]) == 2
    assert "PostgreSQL" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [
    ["--j-floor", "0"], ["--j-floor", "1.5"], ["--max-candidate-pairs", "0"],
    ["--statement-timeout-seconds", "-1"],
])
def test_nonsensical_numeric_flags_are_rejected_before_connecting(monkeypatch, flags):
    def boom(*_a, **_k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(rehearsal.psycopg, "connect", boom)
    with pytest.raises(SystemExit) as caught:
        rehearsal.main(["--dsn", "postgresql://u@h/db", "--notebook", NOTEBOOK, *flags])
    assert caught.value.code == 2


def test_a_database_failure_is_reported_by_class_without_its_text(monkeypatch, capsys):
    """codex #941 R4: the driver's text can carry the server address, a socket
    path or SQL — stderr gets the exception's class name and a fixed hint only."""
    def refuse(*_a, **_k):
        raise rehearsal.psycopg.OperationalError(
            'connection to server on socket "/var/run/secret-pg-7/.s.PGSQL.5432" failed: '
            'FATAL: role "svc_private" does not exist (host db-internal.example:5432)')

    monkeypatch.setattr(rehearsal.psycopg, "connect", refuse)
    assert rehearsal.main(["--dsn", "postgresql://u:pw@h/db", "--notebook", NOTEBOOK]) == 3
    err = capsys.readouterr().err
    assert "OperationalError" in err and rehearsal.DATABASE_ERROR_HINT in err
    for detail in ("secret-pg-7", ".s.PGSQL", "svc_private", "db-internal", "FATAL",
                   "pw@", "connection to server"):
        assert detail not in err, detail
    assert "Traceback" not in err


def test_a_query_failure_mid_run_is_reported_the_same_way(monkeypatch, capsys):
    """A driver error raised by a query (not the connect) takes the same path:
    no SQL text, no server detail."""
    class _Failing(FakeConn):
        def answer(self, sql, params):
            raise rehearsal.psycopg.errors.QueryCanceled(
                "canceling statement due to statement timeout; SELECT secret_column FROM x")

    monkeypatch.setattr(rehearsal.psycopg, "connect", lambda *_a, **_k: _Failing(_library()))
    assert rehearsal.main(["--dsn", "postgresql://u@h/db", "--notebook", NOTEBOOK]) == 3
    err = capsys.readouterr().err
    assert "QueryCanceled" in err and "secret_column" not in err and "SELECT" not in err


def test_name_lookups_are_batched_store_reads_and_samples_off_reads_no_names():
    """codex #941 R4: the lookup rounds send the product store's batched,
    index-driven reads — one member-name statement per ≤ 500 canonicals of a
    round, never one statement per key — and without ``--show-samples`` the
    sample-name keys are not read at all."""
    report, conn = _rehearse(_library())
    engine = report["engine"]
    member_reads = [s for s in conn.statements if "AS visible" in " ".join(s.split())]
    display_reads = [s for s in conn.statements
                     if "AS source_id, c.canonical_id AS canonical_id" in " ".join(s.split())]
    assert display_reads == []                                   # samples off: no names
    assert engine["lookup_name_keys"] > engine["lookup_rounds"]  # several keys per round
    assert engine["lookup_name_statements"] <= engine["lookup_rounds"]
    assert engine["member_seed_statements"] == len(engine["member_seed_calls"])
    assert len(member_reads) == (engine["lookup_name_statements"]
                                 + engine["member_seed_statements"])
    assert not any("COALESCE(c.canonical_id, o.id)=%s" in " ".join(s.split())
                   for s in conn.statements)
    assert all(kwargs == {"prepare": False} for kwargs in conn.store_kwargs)
    # the F4 group is still re-judged on visible names: the counts do not need samples
    assert engine["rows_per_rule"]["F4"] == 1


def test_with_samples_the_display_names_are_one_batched_read_per_round():
    report, conn = _rehearse(_library(), show_samples=True)
    display_reads = [s for s in conn.statements
                     if "AS source_id, c.canonical_id AS canonical_id" in " ".join(s.split())]
    assert 1 <= len(display_reads) <= report["engine"]["lookup_rounds"]
    samples = report["samples"]["findings"]["F1"][0]["evidence"]["samples"]
    assert samples and all("@x-island" in name for name in samples)


def test_a_notebook_without_a_kg_state_row_is_reported_not_crashed(capsys):
    lib = _library()
    lib.state = []
    with pytest.raises(rehearsal.RehearsalError):
        _rehearse(lib)


# ===================================================== same caliber as the product
class _ProductDb:
    """Stands in for the connection the product store reads through."""

    def __init__(self) -> None:
        self.executed: List[Tuple[str, Any]] = []
        self.streams: List[Tuple[str, Any]] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return []

    def cursor(self):
        return self

    def stream(self, sql, params=()):
        self.streams.append((sql, params))
        return iter(())


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _from(sql: str, marker: str) -> str:
    flat = _flat(sql)
    return flat[flat.index(marker):]


def test_graph_and_object_sql_share_the_product_tail():
    """The rehearsal reads the product's streams plus extra columns. Everything from
    the FROM clause on — joins, memory exclusions, ORDER BY — must be the product's
    text, so the node indexing (first-seen order) and the object set cannot drift."""
    db = _ProductDb()
    UnifiedKgStore.community_graph_rows(db, NOTEBOOK)
    (product_graph, product_graph_params), = db.streams
    mine_sql, mine_params = rehearsal.graph_query(NOTEBOOK)
    assert _from(mine_sql, "FROM knowledge_relations kr") == _from(
        product_graph, "FROM knowledge_relations kr")
    assert tuple(mine_params) == tuple(product_graph_params)
    assert 'ORDER BY kr.id COLLATE "C"' in mine_sql

    names = [(sql, params) for sql, params in db.executed if "SELECT DISTINCT" in sql]
    (product_names, product_names_params), = names
    mine_names, mine_names_params = rehearsal.names_query(NOTEBOOK)
    assert _flat(mine_names) == _flat(product_names)
    assert tuple(mine_names_params) == tuple(product_names_params)

    db = _ProductDb()
    UnifiedKgStore.source_canonical_rows(db, NOTEBOOK)
    (product_objects, product_object_params), = db.streams
    mine_sql, mine_params = rehearsal.object_query(NOTEBOOK)
    assert _from(mine_sql, "FROM knowledge_objects o") == _from(
        product_objects, "FROM knowledge_objects o")
    assert tuple(mine_params) == tuple(product_object_params)


def test_decisions_are_read_and_keyed_like_the_product_reader():
    rows = [
        {"canonical_a": "K-a", "canonical_b": "K-b", "seed_a": "a seed", "seed_b": "b seed",
         "status": "rejected"},
        {"canonical_a": "K-old one", "canonical_b": "K-old two", "seed_a": "", "seed_b": "",
         "status": "deferred"},
        {"canonical_a": "K-x", "canonical_b": "K-y", "seed_a": "x", "seed_b": "y",
         "status": "confirmed"},
        {"canonical_a": "K-x", "canonical_b": "K-y", "seed_a": "y", "seed_b": "x",
         "status": "rejected"},                       # same pair, other orientation: last row wins
    ]
    seen: List[Tuple[str, Any]] = []

    class _Connection:
        def execute(self, sql, params=()):
            seen.append((sql, params))
            return _Result(rows)

    product = GovernanceStore.decided_seed_pairs_from(
        _Connection(), NOTEBOOK, seed_version=None)
    sql, params = seen[0]
    mine_sql, mine_params = rehearsal.decisions_query(NOTEBOOK)
    assert _flat(mine_sql) == _flat(sql) and tuple(mine_params) == tuple(params)
    tuples = [(r["canonical_a"], r["canonical_b"], r["seed_a"], r["seed_b"], r["status"])
              for r in rows]
    assert rehearsal.decisions_from_rows(tuples) == product
    assert product[frozenset(("old one", "old two"))] == "deferred"
    assert product[frozenset(("x", "y"))] == "rejected"


def test_member_seeds_are_the_decision_seeds_of_every_member_name():
    """v1 and v2 seeds (a decision of either space names the member); an
    empty name names nothing."""
    seeds = rehearsal.member_seeds_from_rows(
        [("K-1", "Q-Former"), ("K-1", "Q Former"), ("K-1", None), ("K-2", "Qformer")])
    assert seeds == {"K-1": {"q former", "qformer"}, "K-2": {"qformer"}}


# ============================================================== partition bench
def test_partition_bench_runs_on_a_small_graph():
    report, conn = _rehearse(_library(), partition_bench=True)
    bench = report["partition_bench"]
    assert set(bench["base"]) == {
        "louvain_A_seconds", "modularity_A", "per_component_B_seconds",
        "leiden_warm_C_seconds", "leiden_warm_C_steady_seconds", "modularity_C"}
    island = bench["delete_island_source"]
    assert island["relations_removed"] > 0 and 0.0 <= island["A_product"]["ARI"] <= 1.0
    assert "delete_giant_component_source" in bench
    assert any(name.startswith("11b_") for name in report["phases"])
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in conn.statements)
