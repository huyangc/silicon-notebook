"""Real-PostgreSQL plan pins for the KG analysis findings store reads
(PostgreSQL 0077), on a production-shaped corpus and with the planner left
alone (no ``enable_seqscan`` games):

* the statements that bind an id list (``id_binding``): the per-source
  state read (``kg_finding_source_states``), the sample display-name read
  (``kg_finding_display_name_rows``), the member-name read
  (``kg_finding_member_name_rows``, PR-2) and the candidate-state read
  (``merge_candidate_states_for_seeds``) and the finding-key probe
  (``kg_analysis_finding_keys``) -- each must reach its rows
  through the keys it exists to read and never scan the table;
* the publish transaction's old-detail read (``kg_analysis_finding_rows``):
  this notebook's primary-key prefix among many notebooks' findings, never a
  table scan (a sort of that capped slice is allowed).

Every statement pinned is the one the store executes, captured on its way
through ``psycopg.Connection.execute`` and explained with its real
parameters (a custom plan, as ``execute_ids`` gets).
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.domain.kg_analysis_contracts import KG_FINDINGS_ROWS_MAX
from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_findings_plan_pins"),
]

NB = "nb-fpin"
USER = "u-fpin"
NOW = "2026-10-19T00:00:00+00:00"
SOURCES = 12_000
OBJECTS = 120_000          # ten concept objects per source
OTHER_NOTEBOOKS = 40
# One page of findings' sources, and the canonicals of a page of samples.
PAGE_SOURCES = [f"s-{index:05d}" for index in range(0, SOURCES, 97)][:100]
# One page of samples: (source, canonical) for clustered objects, plus a few
# unclustered objects under their own id (every fifth object, offset 3).
PAGE_PAIRS = [
    (f"s-{index % SOURCES:05d}", f"can-{index // 5:06d}")
    for index in range(0, OBJECTS, 487)
][:200] + [
    (f"s-{index % SOURCES:05d}", f"ko-{index:06d}") for index in range(3, OBJECTS, 2405)
][:50]
CANDIDATE_CANONICALS = [f"can-{index:06d}" for index in range(0, 2400, 17)][:120]


@pytest.fixture
def pin_database(postgres_settings):
    database = PostgresDatabase(
        postgres_settings.model_copy(update={
            "postgres_pool_min_size": 1,
            "postgres_pool_max_size": 2,
            "postgres_pool_acquire_timeout_seconds": 10,
            "postgres_statement_timeout_seconds": 60,
            "postgres_lock_timeout_seconds": 10,
        }),
        Path(__file__).resolve().parents[3],
    )
    try:
        assert PostgresMigrator(database).migrate() == 84
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase) -> None:
    """One notebook with 12 000 sources (two extraction runs each), 120 000
    concept objects (ten per source) -- four in five clustered in fives, the
    fifth left unclustered -- and 24 000 merge candidates across the four
    statuses; 40 other notebooks hold 125 findings each beside this one's 125,
    and one of them as many candidates of its own."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (%s,'fpin@example.test','Pin','admin',%s,%s)",
            (USER, NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "SELECT id, id, %s, %s, %s FROM (SELECT %s AS id UNION ALL "
            "SELECT 'nb-other-'||lpad(g::text,2,'0') FROM generate_series(1, %s) g) n",
            (USER, NOW, NOW, NB, OTHER_NOTEBOOKS),
        )
        db.execute(
            "INSERT INTO unified_kg_state(notebook_id,updated_at) VALUES (%s,%s)",
            (NB, NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) "
            "SELECT 's-'||lpad(g::text,5,'0'), %s, 'Doc '||g, 'markdown', 'extracted', "
            "'parsed', %s, %s FROM generate_series(0, %s) g",
            (NB, NOW, NOW, SOURCES - 1),
        )
        db.execute(
            "INSERT INTO extraction_runs(id,notebook_id,source_id,run_type,status,"
            "error_message,created_at,updated_at) "
            "SELECT 'er-'||g||'-'||k, %s, 's-'||lpad(g::text,5,'0'), 'kg', 'completed', "
            "'', %s::timestamptz + (k || ' hours')::interval, %s "
            "FROM generate_series(0, %s) g, generate_series(0, 1) k",
            (NB, NOW, NOW, SOURCES - 1),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "SELECT 'ko-'||lpad(g::text,6,'0'), %s, 'concept', 'approved', "
            "jsonb_build_object('name', 'concept '||g), '[]'::jsonb, "
            "'s-'||lpad((g %% %s)::text,5,'0'), %s, %s FROM generate_series(0, %s) g",
            (NB, SOURCES, NOW, NOW, OBJECTS - 1),
        )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,created_at) "
            "SELECT 'cc-'||g, %s, 'can-'||lpad((g / 5)::text,6,'0'), "
            "'ko-'||lpad(g::text,6,'0'), 'canon '||(g / 5), %s "
            "FROM generate_series(0, %s) g WHERE g %% 5 <> 3",
            (NB, NOW, OBJECTS - 1),
        )
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,created_at,updated_at) "
            "SELECT 'mc-'||n.id||'-'||g, n.id, 'can-'||lpad((g %% 2400)::text,6,'0'), "
            "'can-'||lpad(((g * 7 + 1) %% 2400)::text,6,'0'), 0.9, "
            "(ARRAY['pending','confirmed','rejected','deferred'])[1 + g %% 4], %s, %s "
            "FROM generate_series(0, 23999) g, "
            "(VALUES (%s), ('nb-other-01')) n(id)",
            (NOW, NOW, NB),
        )
        db.execute(
            "INSERT INTO kg_analysis_findings(notebook_id,rule_id,subject_key,subject_kind,"
            "subject_label,subject_refs,severity,impact_objects,evidence,"
            "first_seen_kg_seq,first_seen_cluster_seq) "
            "SELECT n.id, 'F'||(1 + g %% 5), 's-'||lpad(g::text,5,'0'), 'source', "
            "'Doc '||g, jsonb_build_array('s-'||lpad(g::text,5,'0')), 'high', g, "
            "jsonb_build_object('concepts', g), 1, 1 "
            "FROM generate_series(0, 124) g, "
            "(SELECT %s AS id UNION ALL SELECT 'nb-other-'||lpad(o::text,2,'0') "
            " FROM generate_series(1, %s) o) n",
            (NB, OTHER_NOTEBOOKS),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute(
            "VACUUM (ANALYZE) sources, extraction_runs, knowledge_objects, "
            "concept_clusters, concept_merge_candidates, kg_analysis_findings, "
            "unified_kg_state"
        )


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-fpin", now=lambda: NOW,
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _map: value,
        in_chunk_size=lambda: 900,
    )


def _captured(call, marker: str) -> list[tuple[str, object]]:
    """Run ``call`` and return every statement it executed whose text carries
    ``marker``, with its parameters."""
    captured: list[tuple[str, object]] = []
    original = psycopg.Connection.execute

    def recording(self, query, params=None, **options):
        if isinstance(query, str) and marker in query:
            captured.append((query, params))
        return original(self, query, params, **options)

    psycopg.Connection.execute = recording
    try:
        call()
    finally:
        psycopg.Connection.execute = original
    assert captured, f"no statement carrying {marker!r} ran"
    return captured


def _plan(database: PostgresDatabase, sql: str, params) -> str:
    with database.connect() as db:
        rows = db.execute(f"EXPLAIN {sql}", params, prepare=False).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _no_scan(plan: str, *tables: str) -> None:
    for table in tables:
        assert f"Seq Scan on {table} " not in plan + " ", plan


def _assert_source_state_read_seeks_each_listed_source(pin_database):
    store = UnifiedKgStore(pin_database)

    def call():
        with store.read_snapshot() as db:
            states = store.kg_finding_source_states(db, NB, PAGE_SOURCES)
        assert all(state["exists"] for state in states.values())

    [(sql, params)] = _captured(call, "LEFT JOIN LATERAL")
    plan = _plan(pin_database, sql, params)
    _no_scan(plan, "sources", "extraction_runs")
    assert "pk_sources" in plan, plan
    assert "idx_extraction_runs_source_created" in plan, plan


def _assert_display_name_read_is_driven_by_the_listed_canonicals(pin_database):
    store = UnifiedKgStore(pin_database)

    def call():
        assert store.kg_finding_display_name_rows(NB, PAGE_PAIRS)

    [(sql, params)] = _captured(call, "payload->>'name'")
    plan = _plan(pin_database, sql, params)
    _no_scan(plan, "concept_clusters", "knowledge_objects", "sources")
    assert "pk_knowledge_objects" in plan, plan
    assert "idx_clusters_member" in plan, plan


def _assert_member_name_read_is_driven_by_the_listed_canonicals(pin_database):
    """PR-2: every member of the listed canonicals (both legs, as the display
    names) plus the hidden-source probe by the sources' primary key."""
    store = UnifiedKgStore(pin_database)
    canonicals = sorted({canonical for _source, canonical in PAGE_PAIRS})

    def call():
        with store.read_snapshot() as db:
            assert store.kg_finding_member_name_rows(db, NB, canonicals)

    [(sql, params)] = _captured(call, "AS visible")
    plan = _plan(pin_database, sql, params)
    _no_scan(plan, "concept_clusters", "knowledge_objects", "sources")
    # both legs reach objects by primary key (the clustered leg once per
    # member, fenced by LATERAL … LIMIT 1), never by a hash of the whole table
    assert plan.count("pk_knowledge_objects") == 2, plan
    assert "Hash Join" not in plan, plan
    assert "idx_clusters_member" in plan, plan


def _assert_candidate_state_read_is_driven_by_the_notebook_status_slice(pin_database):
    store = GovernanceStore(pin_database, _seams())

    def call():
        # The corpus' candidates carry empty seeds and canonicals without "K-",
        # so their effective seed IS the canonical id: the list doubles as seeds.
        with UnifiedKgStore(pin_database).read_snapshot() as db:
            assert store.merge_candidate_states_for_seeds(
                db, NB, ("confirmed", "rejected", "deferred"), CANDIDATE_CANONICALS)

    [(sql, params)] = _captured(call, "FROM concept_merge_candidates WHERE")
    plan = _plan(pin_database, sql, params)
    _no_scan(plan, "concept_merge_candidates")
    assert "idx_candidates_nb_status" in plan, plan
    assert "Sort" not in plan, plan


def _assert_finding_key_read_probes_the_primary_key(pin_database):
    """PR-2 review: the dismissal write's existence check and the overview's
    dismissed count probe ``pk_kg_analysis_findings`` per key — never the
    notebook's whole detail, never a scan of every notebook's findings."""
    store = UnifiedKgStore(pin_database)
    keys = [("F1", f"s-{index:05d}") for index in range(0, 125, 5)]

    def call():
        with store.read_snapshot() as db:
            assert store.kg_analysis_finding_keys(db, NB, keys)

    [(sql, params)] = _captured(call, "SELECT rule_id, subject_key FROM kg_analysis_findings")
    plan = _plan(pin_database, sql, params)
    _no_scan(plan, "kg_analysis_findings")
    assert "pk_kg_analysis_findings" in plan, plan
    assert "subject_key = ANY" in plan, plan


def _assert_old_detail_read_walks_the_primary_key(pin_database):
    """The notebook's slice of ``pk_kg_analysis_findings`` and nothing else.
    PostgreSQL may sort that slice (a bitmap scan of 125 rows here) -- a sort
    bounded by the write-side cap, ``KG_FINDINGS_ROWS_MAX + 1`` rows at most;
    what must never happen is a scan of every notebook's findings."""
    store = UnifiedKgStore(pin_database)

    def call():
        with pin_database.write() as db:
            assert len(store.kg_analysis_finding_rows(db, NB)) == 125

    [(sql, params)] = _captured(call, "FROM kg_analysis_findings WHERE")
    assert params[-1] == KG_FINDINGS_ROWS_MAX + 1
    plan = _plan(pin_database, sql, params)
    _no_scan(plan, "kg_analysis_findings")
    assert "pk_kg_analysis_findings" in plan, plan
    assert "Index Cond: (notebook_id = " in plan, plan


def test_kg_findings_read_plans_match_the_production_shaped_corpus(pin_database):
    """All six read-only plan checks use one unchanged, fully migrated corpus.

    Keep each result and plan assertion, the full corpus, and VACUUM intact;
    only migration and seed preparation are shared within this test's schema.
    """
    checks = (
        ("source state", _assert_source_state_read_seeks_each_listed_source),
        ("display names", _assert_display_name_read_is_driven_by_the_listed_canonicals),
        ("member names", _assert_member_name_read_is_driven_by_the_listed_canonicals),
        (
            "candidate state",
            _assert_candidate_state_read_is_driven_by_the_notebook_status_slice,
        ),
        ("finding keys", _assert_finding_key_read_probes_the_primary_key),
        ("old detail", _assert_old_detail_read_walks_the_primary_key),
    )
    for label, check in checks:
        try:
            check(pin_database)
        except Exception as error:
            error.add_note(f"KG findings plan scenario: {label}")
            raise
