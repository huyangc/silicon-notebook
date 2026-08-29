"""Tests for ``scripts/diag_pg_hotpaths.py`` (fake-connection only, no live PG).

Contract under test:
  1. Read-only guard — every exported statement starts with EXPLAIN (the hot
     statement family, wrapped for ``EXPLAIN (ANALYZE, BUFFERS, FORMAT
     JSON)``) or with ``SELECT COUNT`` (the per-table row-count overview),
     and none of them contain a mutating keyword.
  2. ``--deep`` gating — the default statement selection (``deep=False``)
     never includes either of the two heavy ILIKE probes; only
     ``select_statements(deep=True)`` does.
  3. Output redaction — rendering a fake EXPLAIN plan whose Filter/Output
     fields carry object content never leaks that content; only structural
     plan fields (Node Type-derived scan/index labels) and numeric counters
     reach the rendered line.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "diag_pg_hotpaths.py"

_MUTATING_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER", "GRANT", "REVOKE")


def _load_module():
    """Import scripts/diag_pg_hotpaths.py as a module (scripts/ is not a package).

    Registering the module in ``sys.modules`` *before* ``exec_module`` is
    required here (unlike the app-free diag scripts) because this module
    uses ``@dataclass`` — CPython's dataclasses implementation looks its own
    module up in ``sys.modules`` while processing type annotations, and
    raises ``AttributeError`` on a module object that was never registered.
    """
    if str(_REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("diag_pg_hotpaths", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def diag_pg_hotpaths():
    return _load_module()


# ---------------------------------------------------------------------------
# 1. Read-only guard
# ---------------------------------------------------------------------------


def test_all_statements_start_with_explain_or_select_count(diag_pg_hotpaths):
    for spec in diag_pg_hotpaths.ALL_STATEMENTS:
        stripped = spec.sql.strip().upper()
        assert stripped.startswith("EXPLAIN") or stripped.startswith("SELECT COUNT"), (
            f"{spec.name}: statement must start with EXPLAIN or SELECT COUNT, got: {spec.sql[:60]!r}"
        )


def test_no_statement_contains_a_mutating_keyword(diag_pg_hotpaths):
    for spec in diag_pg_hotpaths.ALL_STATEMENTS:
        upper = spec.sql.upper()
        for keyword in _MUTATING_KEYWORDS:
            assert not re.search(rf"\b{keyword}\b", upper), (
                f"{spec.name}: statement must not contain {keyword}: {spec.sql!r}"
            )


def test_index_and_fk_audit_sql_are_read_only(diag_pg_hotpaths):
    """The catalog-lookup helpers aren't part of ALL_STATEMENTS but must be
    just as read-only — they only ever query pg_catalog/pg_indexes."""
    assert diag_pg_hotpaths._FK_SQL.strip().upper().startswith("SELECT")
    for keyword in _MUTATING_KEYWORDS:
        assert keyword not in diag_pg_hotpaths._FK_SQL.upper()


# ---------------------------------------------------------------------------
# 2. --deep gating
# ---------------------------------------------------------------------------


def test_default_selection_excludes_deep_statements(diag_pg_hotpaths):
    default_names = {spec.name for spec in diag_pg_hotpaths.select_statements(deep=False)}
    deep_names = {spec.name for spec in diag_pg_hotpaths.DEEP_STATEMENTS}
    assert deep_names, "there must be at least the two heavy probes to gate"
    assert not (default_names & deep_names), "deep-only statements leaked into the default run"


def test_deep_selection_includes_exactly_the_four_heavy_probes(diag_pg_hotpaths):
    """The two ILIKE full-text probes, plus the two missing-vector anti-join
    COUNTs (moved here from HOT_STATEMENTS — Z7 pulled them out of the
    backfill-vectors admission path for being cold-database, tens-of-seconds
    queries; this diagnostic keeps them, just gated behind --deep now)."""
    deep_names = {spec.name for spec in diag_pg_hotpaths.DEEP_STATEMENTS}
    assert deep_names == {
        "chunks_exact_ilike_probe",
        "knowledge_payload_ilike_probe",
        "missing_chunk_vectors_count",
        "missing_element_vectors_count",
    }
    with_deep_names = {spec.name for spec in diag_pg_hotpaths.select_statements(deep=True)}
    assert deep_names <= with_deep_names


def test_hot_statements_are_never_flagged_deep(diag_pg_hotpaths):
    assert all(not spec.deep for spec in diag_pg_hotpaths.HOT_STATEMENTS)
    assert all(not spec.deep for spec in diag_pg_hotpaths.TABLE_ROW_COUNTS)
    assert all(spec.deep for spec in diag_pg_hotpaths.DEEP_STATEMENTS)


# ---------------------------------------------------------------------------
# 3. Output redaction
# ---------------------------------------------------------------------------


_SECRET = "TOP-SECRET-OBJECT-PAYLOAD-CONTENT-nb-real-42"


def _fake_seq_scan_plan(secret: str) -> dict:
    return {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "chunks",
            "Filter": f"(text ~~ '%{secret}%'::text)",
            "Output": [f"'{secret}'"],
            "Actual Rows": 12345,
            "Shared Hit Blocks": 7,
            "Shared Read Blocks": 3,
        },
        "Execution Time": 456.7,
    }


def _fake_index_scan_plan(secret: str) -> dict:
    return {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "knowledge_objects",
            "Index Name": "idx_knowledge_objects_nb_status",
            "Index Cond": f"((notebook_id)::text = '{secret}'::text)",
            "Actual Rows": 7,
            "Shared Hit Blocks": 4,
            "Shared Read Blocks": 0,
            "Plans": [
                {
                    "Node Type": "Bitmap Index Scan",
                    "Index Name": "idx_knowledge_objects_nb_status",
                    "Shared Hit Blocks": 1,
                    "Shared Read Blocks": 0,
                }
            ],
        },
        "Execution Time": 1.2,
    }


@pytest.mark.parametrize("plan_factory", [_fake_seq_scan_plan, _fake_index_scan_plan])
def test_rendered_line_never_leaks_object_content(diag_pg_hotpaths, plan_factory):
    plan = plan_factory(_SECRET)
    summary = diag_pg_hotpaths.parse_explain_json(plan)
    rendered = diag_pg_hotpaths.format_summary_line("probe_statement", summary)
    assert _SECRET not in rendered
    assert "Filter" not in rendered
    assert "Index Cond" not in rendered
    assert "Output" not in rendered


def test_rendered_line_never_leaks_object_content_from_json_text(diag_pg_hotpaths):
    """Same guarantee when the driver hands back the JSON as text rather than
    an already-decoded object (psycopg normally auto-decodes json/jsonb, but
    the parser must not assume that)."""
    import json

    raw = json.dumps([_fake_seq_scan_plan(_SECRET)])
    summary = diag_pg_hotpaths.parse_explain_json(raw)
    rendered = diag_pg_hotpaths.format_summary_line("probe_statement", summary)
    assert _SECRET not in rendered


def test_seq_scan_and_index_scan_are_distinguished(diag_pg_hotpaths):
    seq_summary = diag_pg_hotpaths.parse_explain_json(_fake_seq_scan_plan(_SECRET))
    assert seq_summary.seq_scan_relations == ("chunks",)
    assert seq_summary.actual_rows == 12345
    assert seq_summary.execution_ms == 456.7
    assert seq_summary.shared_hit_blocks == 7
    assert seq_summary.shared_read_blocks == 3

    idx_summary = diag_pg_hotpaths.parse_explain_json(_fake_index_scan_plan(_SECRET))
    assert idx_summary.seq_scan_relations == ()
    assert idx_summary.index_names == ("idx_knowledge_objects_nb_status",)
    # buffers come from the root plan node only — PostgreSQL's BUFFERS output
    # already reports each node's Shared Hit/Read Blocks as cumulative over
    # its own subtree, so the root's 4 already includes the child's 1; summing
    # across nodes would double-count (this used to wrongly assert 4+1==5).
    assert idx_summary.shared_hit_blocks == 4
    assert idx_summary.shared_read_blocks == 0


def test_malformed_plan_fails_closed_without_raising(diag_pg_hotpaths):
    for bad in (None, {}, [], "not json{{{", 42, {"no_plan_key": True}):
        summary = diag_pg_hotpaths.parse_explain_json(bad)
        assert summary.error is not None
        rendered = diag_pg_hotpaths.format_summary_line("probe_statement", summary)
        assert "error:" in rendered


# ---------------------------------------------------------------------------
# Fake-connection exercise of run_statement / run_row_count / audits
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._row or []


class _FakeConnection:
    """Minimal stand-in for psycopg's ``Connection.execute`` convenience API,
    scripted per-SQL-prefix so one fake can serve the whole diagnostic run."""

    def __init__(self, responses):
        self._responses = responses
        self.executed: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or {}))
        for prefix, row in self._responses:
            if sql.strip().startswith(prefix):
                return _FakeCursor(row)
        raise AssertionError(f"unscripted statement: {sql[:80]!r}")


def test_run_statement_uses_connection_execute_and_parses_result(diag_pg_hotpaths):
    plan = [_fake_index_scan_plan(_SECRET)]
    fake = _FakeConnection([("EXPLAIN", {"QUERY PLAN": plan})])
    spec = diag_pg_hotpaths.HOT_STATEMENTS[0]
    summary = diag_pg_hotpaths.run_statement(fake, spec, {"notebook_id": "nb-1"})
    assert summary.error is None
    assert summary.index_names == ("idx_knowledge_objects_nb_status",)
    assert fake.executed[0][1] == {"notebook_id": "nb-1"}


def test_run_statement_fails_closed_on_exception(diag_pg_hotpaths):
    class _RaisingConnection:
        def execute(self, sql, params=None):
            raise RuntimeError("boom")

    spec = diag_pg_hotpaths.HOT_STATEMENTS[0]
    summary = diag_pg_hotpaths.run_statement(_RaisingConnection(), spec, {"notebook_id": "nb-1"})
    assert summary.error == "RuntimeError"


def test_run_row_count_fails_closed_on_exception(diag_pg_hotpaths):
    class _RaisingConnection:
        def execute(self, sql, params=None):
            raise RuntimeError("boom")

    spec = diag_pg_hotpaths.TABLE_ROW_COUNTS[0]
    result = diag_pg_hotpaths.run_row_count(_RaisingConnection(), spec)
    assert result == "error:RuntimeError"


# ---------------------------------------------------------------------------
# run_diagnostics()-level guards: read-only session ordering, the --deep gate
# itself (not just the static select_statements() tuple), and the exit code.
# ---------------------------------------------------------------------------


class _OrderTrackingConnection:
    """Records every executed statement in call order (unlike ``_FakeConnection``,
    a single instance is meant to be reused across ``_connect``/``_pick_notebook_id``/
    ``_sample_canonical``/``audit_indexes`` calls so ordering across those can be
    asserted), scripted per-SQL-prefix."""

    def __init__(self, responses):
        self._responses = responses
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        stripped = sql.strip()
        self.executed.append(stripped)
        for prefix, row in self._responses:
            if stripped.startswith(prefix):
                return _FakeCursor(row)
        raise AssertionError(f"unscripted statement: {sql[:80]!r}")


def test_read_only_pragma_precedes_notebook_pick_sampling_and_index_audit(
    diag_pg_hotpaths, monkeypatch
):
    """``SET default_transaction_read_only = on`` must be the very first
    statement issued on the connection — before notebook auto-selection
    (``_pick_notebook_id``), concept_clusters sampling (``_sample_canonical``),
    and the pg_indexes audit queries. Regression: this used to be set inside
    ``run_diagnostics()``, which ``main()`` calls only *after*
    ``_pick_notebook_id`` already ran the notebook auto-selection query
    against a still-writable session — moved into ``_connect()`` so it lands
    before any of those three helper queries, not just before the hot
    statement family."""
    fake = _OrderTrackingConnection([
        ("SET default_transaction_read_only", None),
        (
            "SELECT notebook_id, COUNT(*) AS c FROM knowledge_objects",
            {"notebook_id": "nb-order", "c": 3},
        ),
        ("SELECT canonical_id, canonical_name FROM concept_clusters", None),
        ("SELECT indexdef FROM pg_indexes", []),
    ])
    monkeypatch.setattr(diag_pg_hotpaths.psycopg, "connect", lambda *a, **k: fake)

    connection = diag_pg_hotpaths._connect("postgresql://fake/db")
    assert connection is fake
    assert len(fake.executed) == 1
    assert fake.executed[0].upper().startswith("SET DEFAULT_TRANSACTION_READ_ONLY")

    notebook_id = diag_pg_hotpaths._pick_notebook_id(fake)
    assert notebook_id == "nb-order"
    diag_pg_hotpaths._sample_canonical(fake, notebook_id)
    diag_pg_hotpaths.audit_indexes(fake)

    # the pragma is still executed[0] — everything run afterwards (notebook
    # pick, sampling, index audit) necessarily happened after it.
    assert fake.executed[0].upper().startswith("SET DEFAULT_TRANSACTION_READ_ONLY")
    assert len(fake.executed) > 1  # the three helper queries did run, after it


def _fake_hotpath_row_for(sql_upper: str) -> "dict | list":
    """Canned response shape for any statement ``run_diagnostics`` may issue,
    keyed by a cheap prefix/substring sniff of the (uppercased) SQL text."""
    if sql_upper.startswith("EXPLAIN"):
        return {"QUERY PLAN": [_fake_index_scan_plan(_SECRET)]}
    if "GROUP BY NOTEBOOK_ID" in sql_upper:
        return {"notebook_id": "nb-deep-gate", "c": 1}
    if "CONCEPT_CLUSTERS" in sql_upper and "CANONICAL_NAME" in sql_upper:
        return None  # no concept_clusters rows for this notebook — probes skip
    if sql_upper.startswith("SELECT INDEXDEF FROM PG_INDEXES"):
        return []
    if sql_upper.startswith("SELECT C.CONNAME AS NAME"):
        return []
    if sql_upper.startswith("SELECT COUNT(*) AS C FROM"):
        return {"c": 7}
    if sql_upper.startswith("SET") or sql_upper.startswith("SELECT SET_CONFIG"):
        return None
    raise AssertionError(f"unscripted statement in run_diagnostics: {sql_upper[:80]!r}")


class _FullFakeConnection:
    """Serves every statement a full ``run_diagnostics()`` call can issue
    (hot statements, deep statements, row counts, index/FK audits, --deep's
    scoped statement_timeout), scripted generically by SQL shape rather than
    per-exact-string so it works for both ``deep=False`` and ``deep=True``.
    Records executed SQL in order so tests can assert which statements ran."""

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        stripped = sql.strip()
        self.executed.append(stripped)
        return _FakeCursor(_fake_hotpath_row_for(stripped.upper()))


def test_run_diagnostics_default_never_executes_deep_statements(diag_pg_hotpaths):
    """Guard for the ``if deep:`` branch in ``run_diagnostics()`` itself, not
    just the static ``select_statements()`` tuple selection (that one is
    covered by ``test_default_selection_excludes_deep_statements`` above, but
    that test never calls ``run_diagnostics`` — a regression that dropped the
    ``if deep:`` gate and always ran ``DEEP_STATEMENTS`` unconditionally would
    sail through it undetected)."""
    fake = _FullFakeConnection()
    exit_code = diag_pg_hotpaths.run_diagnostics(
        fake, notebook_id="nb-deep-gate", deep=False, deep_timeout_ms=0
    )
    assert exit_code == 0
    deep_sql_texts = {spec.sql.strip() for spec in diag_pg_hotpaths.DEEP_STATEMENTS}
    assert not (set(fake.executed) & deep_sql_texts), (
        "run_diagnostics(deep=False) executed a DEEP_STATEMENTS statement"
    )


def test_run_diagnostics_deep_flag_does_execute_deep_statements(diag_pg_hotpaths):
    """Companion to the guard above: proves the fake/assertion actually
    distinguishes the two paths (a fake that always returns 0 or an
    assertion that could never fail either way would make the guard test
    vacuous) by confirming deep=True *does* run all four DEEP_STATEMENTS."""
    fake = _FullFakeConnection()
    exit_code = diag_pg_hotpaths.run_diagnostics(
        fake, notebook_id="nb-deep-gate", deep=True, deep_timeout_ms=0
    )
    assert exit_code == 0
    deep_sql_texts = {spec.sql.strip() for spec in diag_pg_hotpaths.DEEP_STATEMENTS}
    assert deep_sql_texts <= set(fake.executed)


def test_run_diagnostics_returns_1_when_any_statement_fails(diag_pg_hotpaths):
    """Exit code contract: any statement failure (EXPLAIN, row count, or
    otherwise) must flip the exit code to 1, even though the run continues
    through every remaining statement and always prints the summary."""

    class _OneFailingStatementConnection:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, sql, params=None):
            stripped = sql.strip()
            self.executed.append(stripped)
            if stripped.upper().startswith("SELECT COUNT(*) AS C FROM CHUNKS"):
                raise RuntimeError("simulated failure")
            return _FakeCursor(_fake_hotpath_row_for(stripped.upper()))

    fake = _OneFailingStatementConnection()
    exit_code = diag_pg_hotpaths.run_diagnostics(
        fake, notebook_id="nb-deep-gate", deep=False, deep_timeout_ms=0
    )
    assert exit_code == 1
    # the run must still have reached the summary section, i.e. it kept going
    # after the failure rather than aborting the whole diagnostic.
    assert any(
        s.upper().startswith("SELECT INDEXDEF FROM PG_INDEXES") for s in fake.executed
    )


def test_audit_indexes_reports_existing_and_missing(diag_pg_hotpaths):
    fake = _FakeConnection([
        (
            "SELECT indexdef FROM pg_indexes",
            [
                {"indexdef": "CREATE INDEX idx_chunks_nb ON public.chunks USING btree (notebook_id)"},
            ],
        ),
    ])
    rows = diag_pg_hotpaths.audit_indexes(fake)
    by_label = {row["label"]: row["state"] for row in rows}
    # chunks(notebook_id) is covered by the one indexdef every table lookup returns
    assert by_label["chunks(notebook_id)"] == "存在"
    # concept_clusters(notebook_id, canonical_id) is not covered by that same indexdef
    assert by_label["concept_clusters(notebook_id, canonical_id)"] == "缺失"


def test_index_covers_respects_fragment_order(diag_pg_hotpaths):
    covers = diag_pg_hotpaths._index_covers
    assert covers(["CREATE INDEX x ON t (notebook_id, canonical_id)"], ("notebook_id", "canonical_id"))
    # reversed order in the indexdef should not satisfy an ordered key check
    assert not covers(["CREATE INDEX x ON t (canonical_id, notebook_id)"], ("notebook_id", "canonical_id"))
    assert not covers(["CREATE INDEX x ON t (notebook_id)"], ("notebook_id", "canonical_id"))


def test_index_covers_ignores_partial_index_where_clause(diag_pg_hotpaths):
    """Regression for a false positive a live smoke test actually hit:
    ``idx_sources_visible_identity ON sources(notebook_id, created_at, id)
    WHERE source_type <> ALL(ARRAY['memory','knowhow'])`` mentions
    ``source_type`` in its partial predicate, not its key — that must not
    count as covering ``sources(notebook_id, source_type)``."""
    covers = diag_pg_hotpaths._index_covers
    partial_indexdef = (
        "CREATE INDEX idx_sources_visible_identity ON public.sources "
        "USING btree (notebook_id, created_at, id) "
        "WHERE (source_type <> ALL (ARRAY['memory'::text, 'knowhow'::text]))"
    )
    assert not covers([partial_indexdef], ("notebook_id", "source_type"))
    # the predicate-free prefix this same index legitimately covers must
    # still be detected
    assert covers([partial_indexdef], ("notebook_id", "created_at"))


def test_index_covers_rejects_a_non_adjacent_intervening_column(diag_pg_hotpaths):
    """Regression for the loophole the prefix-based rewrite closed: an
    ordered-substring scan (the first cut at this heuristic) would wrongly
    call ``(notebook_id, parse_status, source_type)`` a match for
    ``(notebook_id, source_type)`` — PostgreSQL cannot use ``source_type`` as
    an index condition through this index without also constraining
    ``parse_status``."""
    covers = diag_pg_hotpaths._index_covers
    indexdef = (
        "CREATE INDEX idx_sources_nb_parse_status_type ON public.sources "
        "USING btree (notebook_id, parse_status, source_type)"
    )
    assert not covers([indexdef], ("notebook_id", "source_type"))
    assert covers([indexdef], ("notebook_id", "parse_status"))
    assert covers([indexdef], ("notebook_id", "parse_status", "source_type"))


def test_extract_index_columns_handles_functional_expression_indexes(diag_pg_hotpaths):
    """``idx_knowledge_objects_name_trgm``'s real indexdef has doubled parens
    and a COLLATE clause around its expression column — must parse as one
    column, not be shredded by the nested parens."""
    indexdef = (
        "CREATE INDEX idx_knowledge_objects_name_trgm ON public.knowledge_objects "
        "USING gin (((payload ->> 'name'::text)) COLLATE \"C\" gin_trgm_ops)"
    )
    columns = diag_pg_hotpaths._extract_index_columns(indexdef)
    assert len(columns) == 1
    assert "payload ->> 'name'" in columns[0]
