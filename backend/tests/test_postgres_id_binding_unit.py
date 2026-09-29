"""Unit pins for ``app.repositories.postgres.id_binding`` and its call sites.

No server needed: a recording connection shows which execute options every
converted PostgreSQL statement sends.  A statement that binds a source ceiling
must run with ``prepare=False`` (always a custom plan); a statement that binds
no list must keep today's call byte-identical — no options at all — so it keeps
the plan cache.  The live-server pins (prepared-statement census, EXPLAIN) are
in ``tests/postgres/test_id_list_binding_pins.py``.
"""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.repositories.postgres.chunk_store import ChunkStore
from app.repositories.postgres.database import _BudgetedReadConnection
from app.repositories.postgres.id_binding import (
    ID_SEPARATOR,
    BoundIds,
    bind_ids,
    execute_ids,
)
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.search import (
    _knn_candidate_rows_for_terms,
    chunk_candidate_rows_for_terms,
    knowledge_candidate_rows_for_terms,
)
from app.repositories.postgres.source_store import SourceStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore


TEXT_ARRAY = "string_to_array(%s,E'\\x1f')"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Recorder:
    """Records ``(statement, params, options)``; answers the few reads the
    converted methods make with just enough rows to reach every statement."""

    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None, **options):
        self.calls.append((statement, params, options))
        if "source_index_backfilled FROM unified_kg_state" in statement:
            return _Result([{"source_index_backfilled": 1}])
        if "FROM concept_comentions" in statement:
            return _Result([{
                "canonical_a": "can-0", "canonical_b": "can-1", "bridge_claims": 3,
            }])
        return _Result([])

    def options_by_statement(self):
        return [(statement, options) for statement, _params, options in self.calls]


def _database(connection):
    @contextmanager
    def connect():
        yield connection

    return SimpleNamespace(connect=connect)


def test_bind_ids_uses_one_text_parameter_when_it_is_exact():
    bound = bind_ids(["s-2", "s-1", "s-2"])
    assert bound == BoundIds(TEXT_ARRAY, "s-2\x1fs-1\x1fs-2")
    assert ID_SEPARATOR == "\x1f"
    # Order and duplicates are the caller's: nothing is sorted or deduplicated.
    assert bind_ids(("b", "a")).param == "b\x1fa"
    assert bind_ids([]) == BoundIds(TEXT_ARRAY, "")


@pytest.mark.parametrize(
    "ids",
    [
        ["s-1", "s\x1f2"],   # the separator would split one id in two
        ["s-1", ""],         # an empty id would vanish from string_to_array
        [""],                # ... and a lone one would yield an empty array
        ["s-1", None],       # array NULL semantics the text form cannot carry
        [1, 2],              # not text at all
    ],
)
def test_bind_ids_falls_back_to_the_array_parameter_it_replaced(ids):
    bound = bind_ids(ids)
    assert bound.array_sql == "%s"
    assert bound.param == list(ids)


def test_execute_ids_always_disables_statement_preparation():
    connection = _Recorder()
    execute_ids(connection, "SELECT 1 WHERE x=ANY(%s)", ["a"])
    assert connection.calls == [("SELECT 1 WHERE x=ANY(%s)", ["a"], {"prepare": False})]


def test_the_read_budget_wrapper_forwards_prepare_to_the_bound_statement():
    inner = _Recorder()
    budget = SimpleNamespace(remaining_seconds=lambda: 1.5)
    wrapped = _BudgetedReadConnection(inner, budget, statement_ceiling_ms=3000)

    execute_ids(wrapped, "SELECT 1 WHERE x=ANY(%s)", ["a"])

    # The per-statement deadline is set first (its own plain statement), then
    # the bound statement runs with the option untouched.
    assert inner.calls == [
        ("SELECT set_config('statement_timeout', %s, true)", ("1500ms",), {}),
        ("SELECT 1 WHERE x=ANY(%s)", ["a"], {"prepare": False}),
    ]


def _single(connection):
    assert len(connection.calls) == 1, connection.calls
    return connection.calls[0]


@pytest.mark.parametrize("authoritative", [False, True])
def test_kg_lexical_candidates_bind_the_ceiling_unprepared(authoritative):
    connection = _Recorder()
    knowledge_candidate_rows_for_terms(
        connection, "nb", ["etching"], 4, ["s-1", "s-2"],
        authoritative_source_filter=authoritative,
    )
    statement, params, options = _single(connection)
    assert options == {"prepare": False}
    assert statement.count(f"=ANY({TEXT_ARRAY})") == 2
    assert params.count("s-1\x1fs-2") == 2


def test_chunk_lexical_candidates_bind_the_ceiling_unprepared():
    connection = _Recorder()
    chunk_candidate_rows_for_terms(connection, "nb", ["wafer"], 4, ["s-1"])
    statement, params, options = _single(connection)
    assert options == {"prepare": False}
    assert f'"chunks".source_id=ANY({TEXT_ARRAY})' in statement
    assert "s-1" in params


def test_unscoped_lexical_statements_keep_their_plain_execute():
    for probe in (
        lambda c: knowledge_candidate_rows_for_terms(c, "nb", ["etching"], 4),
        lambda c: chunk_candidate_rows_for_terms(c, "nb", ["wafer"], 4),
        lambda c: _knn_candidate_rows_for_terms(c, "nb", ["etching"], 4),
    ):
        connection = _Recorder()
        probe(connection)
        _statement, _params, options = _single(connection)
        assert options == {}


def test_chunk_store_statements_pick_their_execute_by_what_they_bind():
    connection = _Recorder()
    store = ChunkStore(_database(connection))

    store.question_index_rows("nb", actor_id="u", allowed_source_ids=None, limit=5)
    store.question_index_rows("nb", actor_id="u", allowed_source_ids=["s-1"], limit=5)
    ChunkStore.retrieval_contribution_rows(
        connection, "nb", ["c-1"], actor_id="u", source_mode=None, source_ids=[])
    ChunkStore.retrieval_contribution_rows(
        connection, "nb", ["c-1"], actor_id="u", source_mode="exclude", source_ids=[])
    ChunkStore.retrieval_contribution_rows(
        connection, "nb", ["c-1"], actor_id="u", source_mode="include",
        source_ids=["s-1"])
    ChunkStore.retrieval_contribution_rows(
        connection, "nb", ["c-1"], actor_id="u", source_mode="exclude",
        source_ids=["s-1"])
    ChunkStore.ids_for_sources(connection, "nb", ["s-1"])

    options = [options for _statement, options in connection.options_by_statement()]
    assert options == [
        {}, {"prepare": False},               # P4: unscoped / ceiling
        {}, {},                               # P5: no list bound
        {"prepare": False}, {"prepare": False},  # P5: include / exclude
        {"prepare": False},                   # P6
    ]
    statements = [statement for statement, _options in connection.options_by_statement()]
    assert f"q.source_id=ANY({TEXT_ARRAY})" in statements[1]
    assert f"c.source_id=ANY({TEXT_ARRAY})" in statements[4]
    assert f"c.source_id<>ALL({TEXT_ARRAY})" in statements[5]
    assert f"source_id=ANY({TEXT_ARRAY})" in statements[6]


def test_unified_kg_peer_statements_pick_their_execute_by_what_they_bind():
    connection = _Recorder()
    store = UnifiedKgStore(_database(connection))

    store.community_member_peers("nb", "com-0", "can-9", 8)
    store.community_member_peers("nb", "com-0", "can-9", 8, allowed_source_ids=["s-1"])
    store.comention_peers("nb", "can-0", 1, 8)
    store.comention_peers("nb", "can-0", 1, 8, allowed_source_ids=["s-1"])

    recorded = connection.options_by_statement()
    kinds = [
        ("backfilled" if "source_index_backfilled" in statement
         else "comentions" if "FROM concept_comentions" in statement
         else "names" if "MIN(canonical_name)" in statement
         else "community", options)
        for statement, options in recorded
    ]
    assert kinds == [
        ("community", {}),
        ("backfilled", {}), ("community", {"prepare": False}),
        ("comentions", {}), ("names", {}),
        ("backfilled", {}), ("comentions", {"prepare": False}),
        ("names", {"prepare": False}),
    ]
    for statement, options in recorded:
        assert (TEXT_ARRAY in statement) == (options == {"prepare": False}), statement


def test_source_store_list_statements_bind_unprepared():
    connection = _Recorder()
    store = SourceStore(_database(connection), now=lambda: "2026-09-01T00:00:00+00:00")

    SourceStore.retrieval_element_rows(connection, "nb")
    store.element_type_count_rows(connection, ["s-1", "s-2"], ["paragraph", "table"])

    recorded = connection.options_by_statement()
    assert [options for _statement, options in recorded] == [
        {}, {"prepare": False}, {"prepare": False},
    ]
    assert all(f"source_id=ANY({TEXT_ARRAY})" in s for s, _ in recorded[1:])


def test_whole_notebook_reads_have_no_source_list_form():
    """The element fallback and the isolated-object probe read the whole
    notebook; a source list reaches neither (``_retrieve_elements`` routes a
    list to the chunk path; the probe's only caller passes none), so neither
    method takes one on either backend or on the port."""
    from app.repositories.ports import (
        RetrievalKnowledgeStorePort,
        SourceStorePort,
    )
    from app.repositories.sqlite.knowledge_store import (
        KnowledgeStore as SqliteKnowledgeStore,
    )
    from app.repositories.sqlite.source_store import (
        SourceStore as SqliteSourceStore,
    )

    connection = _Recorder()
    KnowledgeStore.relation_endpoint_rows(connection, "nb")
    SourceStore.retrieval_element_rows(connection, "nb")
    assert [options for _s, _p, options in connection.calls] == [{}, {}]
    assert [params for _s, params, _o in connection.calls] == [("nb",), ("nb",)]
    for method in (
        KnowledgeStore.relation_endpoint_rows,
        SqliteKnowledgeStore.relation_endpoint_rows,
        SourceStore.retrieval_element_rows,
        SqliteSourceStore.retrieval_element_rows,
    ):
        with pytest.raises(TypeError):
            method(connection, "nb", ["s-1"])
    for port, name in (
        (RetrievalKnowledgeStorePort, "relation_endpoint_rows"),
        (SourceStorePort, "retrieval_element_rows"),
    ):
        parameters = list(inspect.signature(getattr(port, name)).parameters)
        assert parameters[-2:] == ["db", "notebook_id"], (port, parameters)
