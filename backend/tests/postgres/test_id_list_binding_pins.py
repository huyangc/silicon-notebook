"""Real-PostgreSQL pins for ``app.repositories.postgres.id_binding``.

A run's frozen source ceiling can list every source of a notebook.  Bound as a
prepared statement, PostgreSQL switches the statement to a generic plan from
about its 11th execution on a connection, where the array is an opaque
parameter (estimated at 10 elements, not hashed, used as an index condition):
measured 7 ms -> 7.7 s for a rare chunk term at 49 000 ids.  These pins fix
the remedy on a live server:

* every converted statement, executed 15 times with a 5 000-id ceiling on ONE
  connection, never enters ``pg_prepared_statements`` — and the same captured
  statement executed the default way does (the control that proves the check
  can see a prepared statement of that shape);
* the plans psycopg actually gets for the chunk rare term (P1), the KG
  authoritative gate (P3) and contribution hydration (P5) carry the ceiling
  as a folded constant array, never a parameter, and keep the access path the
  ceiling must not steal; a forced generic plan of the same statement is the
  control;
* the text form is exact where it is used and the array fallback keeps the
  cases it cannot express.

The rule is stated in ``app/repositories/postgres/id_binding.py`` and in
``docs/development.md`` ("Binding id lists in SQL").  Test names use these
labels: P1 chunk FTS candidates (``chunk_fts_search`` /
``chunk_candidate_rows_for_terms``); P2 / P3 KG FTS reverse-index /
authoritative gates (``fts_search``); P4 ``question_index_rows``; P5
``retrieval_contribution_rows``; P6 / P7 ``ids_for_sources`` (membership /
presence with ordinality); P8 ``community_member_peers``; P9
``comention_peers``.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg import sql as pg_sql

from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.postgres.chunk_store import ChunkStore
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.id_binding import (
    bind_ids,
    execute_ids,
    member_of,
    not_member_of,
)
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.search import chunk_candidate_rows_for_terms
from app.repositories.postgres.unified_kg_store import UnifiedKgStore

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_id_list_binding_pins"),
]

NB = "nb-pin"
USER = "u-pin"
NOW = "2026-09-01T00:00:00+00:00"
SOURCES = 12_000
EXECUTIONS = 15
# 5 000 ids: every other real source (6 000 exist) up to 2 500 of them, plus
# 2 500 ids no row carries — the shape of a wide frozen ceiling.
CEILING = (
    [f"s-{index:05d}" for index in range(0, SOURCES, 2)][:2500]
    + [f"s-filler-{index:05d}" for index in range(2500)]
)
CANDIDATE_CHUNKS = [f"c-{index:05d}-0" for index in range(0, SOURCES, 188)][:64]


@pytest.fixture
def pin_database(postgres_settings):
    """A pool of exactly one connection: every store call below, including
    those that acquire their own connection, runs on the same backend, whose
    ``pg_prepared_statements`` is what the pins read."""
    settings = postgres_settings.model_copy(update={
        "postgres_pool_min_size": 1,
        "postgres_pool_max_size": 1,
        "postgres_pool_acquire_timeout_seconds": 10,
        "postgres_statement_timeout_seconds": 20,
        "postgres_chunk_fts_timeout_seconds": 10.0,
    })
    database = PostgresDatabase(settings, Path(__file__).resolve().parents[3])
    try:
        assert PostgresMigrator(database).migrate() == 68
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase) -> None:
    """A realistic single-notebook corpus: 12 000 sources, two chunks and one
    generated question per chunk, one KG object per source (evidence plus the
    reverse index), clusters of five, a 500-member community, a co-mention hub,
    two elements per source and a relation per third source."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (%s,'pin@example.test','Pin','admin',%s,%s)",
            (USER, NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (NB, NB, USER, NOW, NOW),
        )
        db.execute(
            "INSERT INTO unified_kg_state(notebook_id,source_index_backfilled,updated_at) "
            "VALUES (%s,1,%s)",
            (NB, NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) "
            "SELECT 's-'||lpad(g::text,5,'0'), %s, 'Doc '||g, 'file', 'ready', 'ready', %s, %s "
            "FROM generate_series(0, %s) g",
            (NB, NOW, NOW, SOURCES - 1),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,ordinal,element_ids,created_at) "
            "SELECT 'c-'||lpad(g::text,5,'0')||'-'||k, %s, 's-'||lpad(g::text,5,'0'), "
            "'w'||(g %% 997)||' w'||((g * 7 + k) %% 991)||' w'||((g * 13) %% 983)"
            "||CASE WHEN (g + k) %% 3 = 0 THEN ' wafer' ELSE '' END"
            "||CASE WHEN g %% 500 = 0 AND k = 0 THEN ' quantumdot' ELSE '' END, "
            "g * 2 + k + 1, '[]', %s "
            "FROM generate_series(0, %s) g, generate_series(0, 1) k",
            (NB, NOW, SOURCES - 1),
        )
        # 30 chunks with identical text (tied similarity) inserted in
        # DESCENDING id order, so heap order never happens to equal the
        # documented id tie-break that decides which of them fill a LIMIT.
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,ordinal,element_ids,created_at) "
            "SELECT 'c-tie-'||lpad(g::text,2,'0'), %s, 's-'||lpad((g * 2)::text,5,'0'), "
            "'tiebreakmarker', 100000 + g, '[]', %s "
            "FROM generate_series(0, 29) g ORDER BY g DESC",
            (NB, NOW),
        )
        db.execute(
            "INSERT INTO chunk_questions(id,chunk_id,notebook_id,source_id,question,vector,"
            "created_at) SELECT 'q-'||id, id, notebook_id, source_id, 'q', "
            "'\\x00000000'::bytea, created_at FROM chunks WHERE notebook_id=%s",
            (NB,),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,ordinal,created_at,updated_at) "
            "SELECT 'ko-'||lpad(g::text,5,'0'), %s, 'concept', 'approved', "
            "jsonb_build_object('name', 'w'||(g %% 997)||CASE WHEN g %% 10 = 0 "
            "THEN ' etching' ELSE ' w'||((g * 13) %% 991) END), "
            "jsonb_build_array(jsonb_build_object('source_id','s-'||lpad(g::text,5,'0'),"
            "'element_id','el-'||g,'quoted_span','q')), "
            "'s-'||lpad(g::text,5,'0'), g + 1, %s, %s "
            "FROM generate_series(0, %s) g",
            (NB, NOW, NOW, SOURCES - 1),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "SELECT id, source_id, notebook_id FROM knowledge_objects WHERE notebook_id=%s",
            (NB,),
        )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,created_at) "
            "SELECT 'cc-'||g, %s, 'can-'||lpad((g / 5)::text,5,'0'), "
            "'ko-'||lpad(g::text,5,'0'), 'canon '||(g / 5), %s "
            "FROM generate_series(0, %s) g",
            (NB, NOW, SOURCES - 1),
        )
        db.execute(
            "INSERT INTO community_members(canonical_id,notebook_id,level,community_id,"
            "canonical_name,centrality) "
            "SELECT 'can-'||lpad(c::text,5,'0'), %s, 0, 'com-'||(c / 500), "
            "'canon '||c, (%s - c)::float FROM generate_series(0, %s) c",
            (NB, SOURCES // 5, SOURCES // 5 - 1),
        )
        db.execute(
            "INSERT INTO concept_comentions(notebook_id,canonical_a,canonical_b,"
            "bridge_claims) SELECT %s, 'can-00000', 'can-'||lpad(c::text,5,'0'), "
            "1 + c %% 7 FROM generate_series(1, 2000) c",
            (NB,),
        )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
            "created_at) SELECT 'el-'||g||'-'||k, 's-'||lpad(g::text,5,'0'), "
            "(ARRAY['paragraph','table','formula','image'])[1 + (g + k) %% 4], "
            "CASE WHEN k = 1 AND g %% 3 = 0 THEN 'Table 1 part 2' ELSE 'p'||k END, "
            "'element '||g, %s FROM generate_series(0, %s) g, generate_series(0, 1) k",
            (NOW, SOURCES - 1),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) "
            "SELECT 'kr-'||g, %s, 's-'||lpad(g::text,5,'0'), 'ko-'||lpad(g::text,5,'0'), "
            "'ko-'||lpad(((g + 1) %% %s)::text,5,'0'), 'related_to', %s "
            "FROM generate_series(0, %s, 3) g",
            (NB, SOURCES, NOW, SOURCES - 1),
        )
    with psycopg.connect(
        database.settings.database_url, autocommit=True
    ) as raw:
        # The seeded tables only (the scoped URL's search_path names them);
        # VACUUM sets the visibility map the planner's costs lean on.
        raw.execute(
            "VACUUM (ANALYZE) sources, chunks, chunk_questions, knowledge_objects, "
            "knowledge_object_sources, concept_clusters, community_members, "
            "concept_comentions, source_elements, knowledge_relations"
        )


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-pin", now=lambda: NOW,
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _map: value,
        in_chunk_size=lambda: 900,
    )


def _converted_calls(database: PostgresDatabase):
    """Every converted statement (ledger id -> one call binding CEILING)."""
    knowledge = KnowledgeStore(database, _seams())
    chunks = ChunkStore(database)
    unified = UnifiedKgStore(database)

    def on_connection(fn):
        def call():
            with database.connect() as db:
                return fn(db)
        return call

    def backfilled(flag: int, fn):
        def call():
            with database.write() as db:
                db.execute(
                    "UPDATE unified_kg_state SET source_index_backfilled=%s "
                    "WHERE notebook_id=%s", (flag, NB),
                )
            return fn()
        return call

    return {
        "P1 chunk FTS": on_connection(lambda db: knowledge.chunk_fts_search(
            db, NB, "quantumdot wafer", k=50, allowed_source_ids=CEILING)),
        "P2 KG FTS reverse-index gate": on_connection(lambda db: knowledge.fts_search(
            db, NB, "etching", k=50, allowed_source_ids=CEILING)),
        "P3 KG FTS authoritative gate": on_connection(lambda db: knowledge.fts_search(
            db, NB, "etching", k=50, allowed_source_ids=CEILING,
            authoritative_source_filter=True)),
        "P4 generated-question rows": lambda: chunks.question_index_rows(
            NB, actor_id=USER, allowed_source_ids=CEILING, limit=10001),
        "P5 contribution hydration (include)": on_connection(
            lambda db: chunks.retrieval_contribution_rows(
                db, NB, CANDIDATE_CHUNKS, actor_id=USER,
                source_mode="include", source_ids=CEILING)),
        "P5 contribution hydration (exclude)": on_connection(
            lambda db: chunks.retrieval_contribution_rows(
                db, NB, CANDIDATE_CHUNKS, actor_id=USER,
                source_mode="exclude", source_ids=CEILING)),
        "P6 ids_for_sources": on_connection(
            lambda db: chunks.ids_for_sources(db, NB, CEILING)),
        "P7 ids_for_sources (presence, ordinality)": on_connection(
            lambda db: chunks.ids_for_sources(
                db, NB, CEILING, presence_only=True)),
        "P8 community peers (reverse index)": backfilled(1, lambda: (
            unified.community_member_peers(
                NB, "com-0", "can-00000", 8, allowed_source_ids=CEILING))),
        "P8 community peers (authoritative)": backfilled(0, lambda: (
            unified.community_member_peers(
                NB, "com-0", "can-00000", 8, allowed_source_ids=CEILING))),
        "P9 comention peers (reverse index)": backfilled(1, lambda: (
            unified.comention_peers(
                NB, "can-00000", 1, 8, allowed_source_ids=CEILING))),
        "P9 comention peers (authoritative)": backfilled(0, lambda: (
            unified.comention_peers(
                NB, "can-00000", 1, 8, allowed_source_ids=CEILING))),
    }


def _prepared_statements(database: PostgresDatabase) -> list[str]:
    with database.connect() as db:
        return [
            str(row["statement"])
            for row in db.execute(
                "SELECT statement FROM pg_prepared_statements"
            ).fetchall()
        ]


def _is_ceiling_statement(statement: str) -> bool:
    return "string_to_array" in statement


def test_every_converted_statement_stays_out_of_the_plan_cache(
    pin_database, monkeypatch,
):
    backend_pids = set()
    captured: list[tuple[str, object]] = []
    original = psycopg.Connection.execute

    def recording_execute(self, query, params=None, **options):
        if options.get("prepare") is False and isinstance(query, str):
            captured.append((query, params))
        backend_pids.add(self.info.backend_pid)
        return original(self, query, params, **options)

    monkeypatch.setattr(psycopg.Connection, "execute", recording_execute)
    for name, call in _converted_calls(pin_database).items():
        captured.clear()
        for _ in range(EXECUTIONS):
            call()
        # Each converted call bound the ceiling at least once, unprepared.
        assert captured, f"{name}: no statement ran through execute_ids"
        assert all(_is_ceiling_statement(sql) for sql, _ in captured), name
        leaked = [s for s in _prepared_statements(pin_database)
                  if _is_ceiling_statement(s)]
        assert leaked == [], (
            f"{name}: a ceiling statement was prepared after {EXECUTIONS} "
            f"executions — it will reach a generic plan: {leaked[0][:300]}"
        )
    assert len(backend_pids) == 1, "the pins must all run on ONE connection"

    # Control: the very statement execute_ids ran, executed the default way
    # the same number of times on the same connection, IS prepared — so an
    # empty result above means "not prepared", not "invisible to the check".
    sql, params = captured[0]
    with pin_database.connect() as db:
        for _ in range(EXECUTIONS):
            original(db, sql, params).fetchall()
    prepared = [s for s in _prepared_statements(pin_database)
                if _is_ceiling_statement(s)]
    assert len(prepared) == 1, prepared


def _recorded_statement(database, call) -> tuple[str, object]:
    """Run ``call`` and return the (sql, params) it sent through execute_ids."""
    captured: list[tuple[str, object]] = []
    original = psycopg.Connection.execute

    def recording_execute(self, query, params=None, **options):
        if options.get("prepare") is False and isinstance(query, str):
            captured.append((query, params))
        return original(self, query, params, **options)

    psycopg.Connection.execute = recording_execute
    try:
        call()
    finally:
        psycopg.Connection.execute = original
    assert len(captured) == 1, [sql[:120] for sql, _ in captured]
    return captured[0]


def _custom_plan(database, sql, params) -> str:
    with database.connect() as db:
        rows = execute_ids(db, f"EXPLAIN (COSTS OFF, VERBOSE) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _generic_plan(database, sql, params) -> str:
    """The plan the same statement gets once the plan cache goes generic.

    ``EXPLAIN`` of a statement always plans it with the bound values, so the
    control prepares the statement itself (as psycopg does from its 5th
    execution) and explains ``EXECUTE`` under ``force_generic_plan``.
    """
    with database.connect() as db:
        db.execute(sql, params, prepare=True).fetchall()
        name = db.execute(
            "SELECT name FROM pg_prepared_statements "
            "WHERE strpos(statement, 'string_to_array') > 0"
        ).fetchone()["name"]
        db.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cursor = psycopg.ClientCursor(db)
        placeholders = ",".join("%s" for _ in params)
        cursor.execute(
            pg_sql.SQL("EXPLAIN (COSTS OFF, VERBOSE) EXECUTE {}(").format(
                pg_sql.Identifier(name)
            ).as_string(db) + placeholders + ")",
            params,
        )
        rows = cursor.fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _assert_constant_ceiling(plan: str, generic: str, predicate: str) -> None:
    # Custom plan: string_to_array() over the bound text folded into ONE
    # constant text[] carrying the real ids, so the planner hashes it and
    # knows its length.  No parameter survives for the ceiling.
    assert f"{predicate} = ANY ('{{s-00000," in plan, plan
    assert "string_to_array" not in plan, plan
    # Control: the same statement planned generically keeps the ceiling as an
    # opaque parameter — the shape the pin above must never see.
    assert "string_to_array($" in generic, generic


def test_chunk_rare_term_plan_keeps_the_ceiling_a_constant_filter(pin_database):
    knowledge = KnowledgeStore(pin_database, _seams())

    def call():
        with pin_database.connect() as db:
            knowledge.chunk_fts_search(
                db, NB, "quantumdot", k=50, allowed_source_ids=CEILING,
            )

    sql, params = _recorded_statement(pin_database, call)
    plan = _custom_plan(pin_database, sql, params)
    generic = _generic_plan(pin_database, sql, params)
    _assert_constant_ceiling(plan, generic, "chunks.source_id")
    # The rare term drives the probe through the trigram index; the ceiling
    # only filters its handful of rows.  A generic plan instead drove by the
    # ceiling ("Index Cond: source_id = ANY($n)") and filtered ~98k rows.
    assert "idx_chunks_text_trgm" in plan, plan
    assert "Index Cond: (chunks.source_id" not in plan, plan
    assert 'Index Cond: ("chunks".source_id' not in plan, plan


def test_kg_authoritative_gate_plan_matches_evidence_against_a_constant(
    pin_database,
):
    knowledge = KnowledgeStore(pin_database, _seams())

    def call():
        with pin_database.connect() as db:
            knowledge.fts_search(
                db, NB, "etching", k=50, allowed_source_ids=CEILING,
                authoritative_source_filter=True,
            )

    sql, params = _recorded_statement(pin_database, call)
    plan = _custom_plan(pin_database, sql, params)
    generic = _generic_plan(pin_database, sql, params)
    # The per-object evidence probe compares against the folded constant
    # (hashable) instead of an unhashable parameter scanned per element.
    _assert_constant_ceiling(plan, generic, "(ev.value ->> 'source_id'::text)")


def test_contribution_hydration_plan_drives_by_candidate_primary_keys(
    pin_database,
):
    chunks = ChunkStore(pin_database)

    def call():
        with pin_database.connect() as db:
            chunks.retrieval_contribution_rows(
                db, NB, CANDIDATE_CHUNKS, actor_id=USER,
                source_mode="include", source_ids=CEILING,
            )

    sql, params = _recorded_statement(pin_database, call)
    plan = _custom_plan(pin_database, sql, params)
    generic = _generic_plan(pin_database, sql, params)
    _assert_constant_ceiling(plan, generic, "c.source_id")
    # <= 64 candidate primary keys drive the read; the ceiling is a filter on
    # those rows, never the index condition that walks a whole notebook.
    assert "pk_chunks" in plan, plan
    assert "Index Cond: (c.id = ANY" in plan, plan
    assert "Index Cond: (c.source_id" not in plan, plan


def test_converted_statements_keep_their_documented_row_order(pin_database):
    """The binding changed how the ceiling travels, never which rows come back
    or in what order: each ordered statement is checked against its own
    ORDER BY, on data where a lost key would show."""
    knowledge = KnowledgeStore(pin_database, _seams())
    chunks = ChunkStore(pin_database)
    unified = UnifiedKgStore(pin_database)
    allowed = set(CEILING)

    with pin_database.connect() as db:
        # P1: 30 tied chunks, 5 slots -> the id tie-break picks the 5 lowest.
        tied = chunk_candidate_rows_for_terms(
            db, NB, ["tiebreakmarker"], 5, CEILING,
        )
        assert [row["candidate_id"] for row in tied] == [
            f"c-tie-{index:02d}" for index in range(5)
        ]
        # P5: candidate primary keys in the ceiling, in document order.
        hydrated = chunks.retrieval_contribution_rows(
            db, NB, list(reversed(CANDIDATE_CHUNKS)), actor_id=USER,
            source_mode="include", source_ids=CEILING,
        )
        expected = sorted(
            chunk for chunk in CANDIDATE_CHUNKS
            if "s-" + chunk.split("-")[1] in allowed
        )
        assert [row["id"] for row in hydrated] == expected and expected
        # P1 store path: hits keep the union's deterministic ranking.
        first = knowledge.chunk_fts_search(
            db, NB, "tiebreakmarker", k=5, allowed_source_ids=CEILING,
        )
        assert [hit["chunk_id"] for hit in first] == [
            f"c-tie-{index:02d}" for index in range(5)
        ]
    # P4: the scan window is the lowest question ids (C collation).
    questions = chunks.question_index_rows(
        NB, actor_id=USER, allowed_source_ids=CEILING, limit=50,
    )
    ids = [row["id"] for row in questions]
    assert len(ids) == 50 and ids == sorted(ids) and ids[0] == "q-c-00000-0"
    # P8: centrality DESC, canonical id ascending.
    peers = unified.community_member_peers(
        NB, "com-0", "can-00000", 20, allowed_source_ids=CEILING,
    )
    centralities = [row["centrality"] for row in peers]
    assert len(peers) == 20 and centralities == sorted(centralities, reverse=True)
    # P9: bridge claims DESC.
    comentions = unified.comention_peers(
        NB, "can-00000", 1, 20, allowed_source_ids=CEILING,
    )
    claims = [claim for _name, claim in comentions]
    assert len(comentions) == 20 and claims == sorted(claims, reverse=True)


def test_text_form_and_fallback_select_exactly_the_array_rows(pin_database):
    """``string_to_array`` over the joined ids equals the bound array for every
    list the text form is chosen for; the lists it cannot express (an empty
    id, the separator inside an id) keep the array binding.  Both predicate
    forms select the same rows as the plain array predicate."""
    lists = [
        [],
        ["s-00000"],
        ["s-00000", "s-00000", "s-00002"],
        ["s-00000", "", "s-00002"],
        ["   ", "s-00002"],
        ["s-00000\x1fs-00002", "s-00004"],
        CEILING,
    ]
    forms = (("=ANY", member_of), ("<>ALL", not_member_of))
    with pin_database.connect() as db:
        for ids in lists:
            bound = bind_ids(ids)
            text_form = bound.sql != "%s::text[]"
            assert text_form == all(
                value and "\x1f" not in value for value in ids
            ), ids
            for array_predicate, form in forms:
                expected = db.execute(
                    f"SELECT id FROM sources WHERE notebook_id=%s "
                    f"AND id{array_predicate}(%s::text[]) ORDER BY id",
                    (NB, list(ids)),
                ).fetchall()
                actual = execute_ids(
                    db,
                    f"SELECT id FROM sources WHERE notebook_id=%s "
                    f"AND {form('id', bound)} ORDER BY id",
                    (NB, bound.param),
                ).fetchall()
                assert actual == expected, (form.__name__, ids[:5])
    # The separator cannot smuggle two ids through one element.
    with pin_database.connect() as db:
        smuggled = bind_ids(["s-00000\x1fs-00002"])
        rows = execute_ids(
            db,
            f"SELECT id FROM sources WHERE {member_of('id', smuggled)}",
            (smuggled.param,),
        ).fetchall()
    assert rows == []


def test_unscoped_calls_leave_the_plan_cache_to_do_its_work(pin_database):
    """No ceiling, no list: those statements keep psycopg's default execute
    and are prepared as before — the binding module never taxes them."""
    knowledge = KnowledgeStore(pin_database, _seams())
    for _ in range(EXECUTIONS):
        with pin_database.connect() as db:
            knowledge.chunk_fts_search(db, NB, "quantumdot", k=50)
    prepared = _prepared_statements(pin_database)
    assert any(
        "CROSS JOIN LATERAL" in statement and "FROM \"chunks\"" in statement
        for statement in prepared
    ), [statement[:120] for statement in prepared]
    assert not any(_is_ceiling_statement(s) for s in prepared)


# ------------------------------------------------------------ row identity
# Which rows come back, not only their order: each statement is compared with
# a Python reference computed from the fixture.  An inverted membership test
# or an ignored ceiling returns rows outside CEILING, which every assertion
# below would see.
_ALLOWED = frozenset(CEILING)


def _chunk_sources(database) -> dict[str, str]:
    with database.connect() as db:
        return {
            row["id"]: row["source_id"]
            for row in db.execute(
                "SELECT id, source_id FROM chunks WHERE notebook_id=%s", (NB,),
            ).fetchall()
        }


def test_p4_question_rows_are_exactly_the_ceiling_rows_of_the_window(pin_database):
    chunks = ChunkStore(pin_database)
    unscoped = chunks.question_index_rows(
        NB, actor_id=USER, allowed_source_ids=None, limit=10001,
    )
    # The unscoped window starts with sources outside CEILING (odd indices),
    # so a scoped page that ignored the ceiling would show them.
    outside = [row for row in unscoped[:50] if row["source_id"] not in _ALLOWED]
    assert outside, "fixture: rows outside the ceiling must rank in the page"
    scoped = chunks.question_index_rows(
        NB, actor_id=USER, allowed_source_ids=CEILING, limit=50,
    )
    expected = [row for row in unscoped if row["source_id"] in _ALLOWED][:50]
    assert [row["id"] for row in scoped] == [row["id"] for row in expected]
    assert all(row["source_id"] in _ALLOWED for row in scoped)
    assert not {row["id"] for row in outside} & {row["id"] for row in scoped}


def test_p6_ids_for_sources_equals_the_fixture_reference(pin_database):
    chunk_sources = _chunk_sources(pin_database)
    expected = sorted(
        chunk_id for chunk_id, source in chunk_sources.items()
        if source in _ALLOWED
    )
    assert expected and len(expected) < len(chunk_sources)
    with pin_database.connect() as db:
        got = sorted(
            row["id"] for row in ChunkStore.ids_for_sources(db, NB, CEILING)
        )
    assert got == expected


def test_p7_presence_keeps_first_occurrence_order_of_the_listed_sources(
    pin_database,
):
    present = set(_chunk_sources(pin_database).values())
    requested = (
        ["s-filler-00001", "s-00010", "s-00003", "s-00010"]
        + list(reversed(CEILING[:40]))
    )
    with pin_database.connect() as db:
        got = [
            row["source_id"]
            for row in ChunkStore.ids_for_sources(
                db, NB, requested, presence_only=True,
            )
        ]
    assert got == [
        source for source in dict.fromkeys(requested) if source in present
    ]


def test_p5_exclude_mode_returns_exactly_the_candidates_outside_the_ceiling(
    pin_database,
):
    chunk_sources = _chunk_sources(pin_database)
    candidates = [
        f"c-{index:05d}-{part}" for index in range(0, 400, 7) for part in (0, 1)
    ]
    with pin_database.connect() as db:
        ordinals = {
            row["id"]: row["ordinal"]
            for row in db.execute(
                "SELECT id, ordinal FROM chunks WHERE id=ANY(%s)", (candidates,),
            ).fetchall()
        }
        excluded = ChunkStore.retrieval_contribution_rows(
            db, NB, candidates, actor_id=USER, source_mode="exclude",
            source_ids=CEILING,
        )
        included = ChunkStore.retrieval_contribution_rows(
            db, NB, candidates, actor_id=USER, source_mode="include",
            source_ids=CEILING,
        )
    outside = sorted(
        (chunk for chunk in candidates if chunk_sources[chunk] not in _ALLOWED),
        key=ordinals.__getitem__,
    )
    inside = sorted(
        (chunk for chunk in candidates if chunk_sources[chunk] in _ALLOWED),
        key=ordinals.__getitem__,
    )
    assert outside and inside
    assert [row["id"] for row in excluded] == outside
    assert [row["id"] for row in included] == inside
