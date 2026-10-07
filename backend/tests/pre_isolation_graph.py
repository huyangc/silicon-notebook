"""Build a notebook's knowledge graph the way it was built BEFORE the M1
isolation (E4-2) -- test data construction for the read-side suites.

Since E4-2 the graph-build readers of ``unified_kg_store`` leave
Memory-derived objects and relations out, so a rebuild never puts a Memory
object into a shared cluster. Notebooks built before the isolation still hold
such mixed clusters until migration 0067 / v87 and the isolated rebuild clear
them, and the READ side (the KG viewer rule of PR-A, E4-4 / E4-7) must stay
correct on them. A test that needs that legacy shape builds it with
``build_pre_isolation(repo, notebook_id)`` INSTEAD of
``repo.rebuild_unified_kg(notebook_id)``:

* only the rebuild step runs with both stores' Memory exclusions rendered as
  a tautology (the pre-isolation SQL; PostgreSQL's mention-claim reader in
  ``search.py`` included); everything the test does afterwards --
  including every assertion -- runs the production code;
* a sentinel then asserts the seam took effect: the notebook holds at least one
  live Memory-derived object of a clustered type, and every such object is a
  member of a published cluster (after an isolated rebuild none would be). A
  seam that silently stopped working fails here, loudly, instead of turning the
  read-side tests into tests of an empty case.

Nothing in production can reach this state through a rebuild any more. The
name and signature are stable: other tasks (E4-7) call it from the same files.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

_TAUTOLOGY = "(1=1)"
_NEVER = "(1=0)"
_CLUSTERED_TYPES = ("concept", "claim", "formula", "procedure")


@contextmanager
def _pre_isolation_sql():
    from app.repositories.postgres import search as pg_search
    from app.repositories.postgres import unified_kg_store as pg_store
    from app.repositories.sqlite import unified_kg_store as sqlite_store

    with mock.patch.multiple(
        sqlite_store,
        _not_memory=lambda *_a: _TAUTOLOGY,
        _not_memory_object_ref=lambda *_a: _TAUTOLOGY,
    ), mock.patch.multiple(
        pg_store,
        _not_memory=lambda *_a: _TAUTOLOGY,
        _not_memory_object_ref=lambda *_a: _TAUTOLOGY,
    ), mock.patch.object(
        # PostgreSQL's claim reader for the mention bridge lives in search.py
        # and renders the classifier directly ("NOT <fragment>"): "never
        # Memory" there too, so both backends build the same legacy graph
        pg_search, "memory_derived_in_notebook", lambda *_a: _NEVER,
    ):
        yield


def _is_postgres(repo) -> bool:
    return type(repo).__name__ == "PostgresRepository"


def build_pre_isolation(repo, notebook_id: str, **rebuild_kwargs):
    """``repo.rebuild_unified_kg(notebook_id, **rebuild_kwargs)`` with the
    pre-isolation graph build, then the sentinel above. Returns what the
    rebuild returned."""
    with _pre_isolation_sql():
        result = repo.rebuild_unified_kg(notebook_id, **rebuild_kwargs)
    ph = "%s" if _is_postgres(repo) else "?"
    types = ",".join(f"'{t}'" for t in _CLUSTERED_TYPES)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT ko.id AS id, EXISTS (SELECT 1 FROM concept_clusters c "
            "WHERE c.notebook_id = ko.notebook_id AND c.member_object_id = ko.id "
            "AND c.generation = COALESCE((SELECT cluster_generation FROM unified_kg_state "
            f"WHERE notebook_id = {ph}), 0)) AS clustered "
            "FROM knowledge_objects ko JOIN sources s ON s.id = ko.source_id "
            f"WHERE ko.notebook_id = {ph} AND s.source_type = 'memory' "
            f"AND ko.status != 'deprecated' AND ko.object_type IN ({types})",
            (notebook_id, notebook_id),
        ).fetchall()
    memory = {row["id"]: bool(row["clustered"]) for row in rows}
    assert memory, (
        "build_pre_isolation on a notebook without a live Memory-derived object: "
        "use repo.rebuild_unified_kg"
    )
    unclustered = sorted(oid for oid, clustered in memory.items() if not clustered)
    assert not unclustered, (
        "the pre-isolation seam did not take effect: these Memory-derived "
        f"objects are in no published cluster: {unclustered}"
    )
    return result
