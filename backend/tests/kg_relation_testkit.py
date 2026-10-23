"""Test-only raw relation insert.

Moved off ``RepositoryFacade`` in S2 batch F: no production path inserts
relations this way, only tests and fixtures seeding a graph.
"""
from __future__ import annotations


def add_relations(repo, notebook_id: str, source_id, relations: list[dict]) -> int:
    """Insert ``relations`` as-is through the knowledge store and drop the
    notebook's review-queue ranking memo.

    The raw insert bypasses ``store_kg``'s ``kg_mutation_seq`` bump, which is
    what the seq-gated review-queue ranking memo (items + total) relies on to
    self-invalidate. Mirrors ``knowledge_query.insert_test_object``: drop it
    explicitly, so a fixture that warms the ranking, seeds relations here and
    reads the queue again never sees a stale ranking or total. None of the
    ``knowledge_counts_cache`` memos read ``knowledge_relations``, so they need
    no invalidation (codex #638 R1).
    """
    runtime = repo._runtime
    with runtime.database.write() as db:
        result = runtime.knowledge.add_relations(
            db, notebook_id, source_id, relations, runtime.seams.now()
        )
    repo._runtime.review_queue_memo.invalidate(notebook_id)
    return result
