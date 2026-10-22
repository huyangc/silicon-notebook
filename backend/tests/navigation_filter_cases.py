"""Shared cases for the read side of the navigation-page marker: every
chunk-row builder retrieval reads carries ``chunks.navigation``.

Used by ``tests/test_navigation_filter.py`` (SQLite) and
``tests/postgres/test_navigation_filter_pg.py`` (PostgreSQL). Seeding goes
through the stores, so both backends run the same code; a navigation chunk and
a body chunk share one element (``el-shared``), the way a long table split
into parts can have one part judged a table of contents.

``CHUNK_ROW_BUILDERS`` is the enumerated list of adapter builders; the static
scan in ``tests/test_navigation_filter.py`` keeps it complete against the
adapters' SQL.
"""
from __future__ import annotations

from uuid import uuid4

from app.models.notebooks import NotebookCreate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from app.services.navigation_filter import row_navigation

TOC_TEXT = (
    "Contents 5.7.2.1 Loss and Crosstalk Mask ........ 210 "
    "5.7.3 Standard Package ........ 214 Table 5-18 Standard Package Loss ........ 215"
)
SHARED_TEXT = "Table 5-18 Standard Package Loss values per lane"
BODY_TEXT = (
    "The Standard Package insertion loss mask allows at most 1.5 dB at the "
    "Nyquist frequency for a 25 mm channel."
)
NEEDLE = "Standard Package"


class Seeded:
    def __init__(self, notebook_id, source_id, toc_id, body_id):
        self.notebook_id = notebook_id
        self.source_id = source_id
        self.toc_id = toc_id
        self.body_id = body_id

    def element(self, name: str) -> str:
        return f"el-{name}-{self.source_id}"


def seed(repo) -> Seeded:
    runtime = repo._runtime
    notebook_id = repo.create_notebook(NotebookCreate(name="nav-filter")).id
    source_id = f"src-navf-{uuid4().hex[:10]}"
    runtime.source_store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title="spec",
        source_type="document", status="parsed", parse_status="extracted",
        file_name="spec.md", file_path="", file_size=0, file_hash=source_id,
        summary="", doc_type="",
    )
    seeded = Seeded(
        notebook_id, source_id, f"ck-toc-{source_id}", f"ck-body-{source_id}"
    )
    now = runtime.seams.now()
    elements = [
        SourceElementWrite(seeded.element("toc"), "paragraph", "p1", TOC_TEXT, {}),
        SourceElementWrite(seeded.element("shared"), "table", "p2", SHARED_TEXT, {}),
        SourceElementWrite(seeded.element("body"), "paragraph", "p3", BODY_TEXT, {}),
    ]
    with runtime.database.write() as db:
        runtime.source_store.replace_elements(db, source_id, elements, created_at=now)
    chunks = [
        ChunkWrite(
            seeded.toc_id, f"[Contents] {TOC_TEXT} {SHARED_TEXT}", "Contents",
            (seeded.element("toc"), seeded.element("shared")), navigation=True,
        ),
        ChunkWrite(
            seeded.body_id, f"[5.7.3 Standard Package] {BODY_TEXT} {SHARED_TEXT}",
            "5.7.3 Standard Package",
            (seeded.element("body"), seeded.element("shared")), navigation=False,
        ),
    ]
    assert runtime.chunk_store.replace_source_chunks(
        source_id, notebook_id, chunks, created_at=now, mark_chunked_at=now,
        navigation_scanned_at=now,
    )
    return seeded


def _placeholder(repo) -> str:
    return "%s" if "postgres" in type(repo._runtime.database).__module__ else "?"


def _flags(rows, key="id") -> dict:
    return {str(row[key]): row_navigation(row) for row in rows}


#: ``(name, call(store, db, seeded) -> rows, id column)``: every ChunkStore /
#: KnowledgeStore method whose rows a retrieval producer turns into candidates.
CHUNK_ROW_BUILDERS = (
    ("ChunkStore.retrieval_rows",
     lambda rt, db, s: rt.chunk_store.retrieval_rows(db, s.notebook_id), "id"),
    ("ChunkStore.hydrate_rows",
     lambda rt, db, s: rt.chunk_store.hydrate_rows(db, [s.toc_id, s.body_id]), "id"),
    ("ChunkStore.graph_hydrate_rows",
     lambda rt, db, s: rt.chunk_store.graph_hydrate_rows(db, [s.toc_id, s.body_id]), "id"),
    ("ChunkStore.graph_hydrate_rows[ceiling]",
     lambda rt, db, s: rt.chunk_store.graph_hydrate_rows(
         db, [s.toc_id, s.body_id],
         allowed_source_ids={s.notebook_id: [s.source_id]}), "id"),
    ("ChunkStore.retrieval_contribution_rows",
     lambda rt, db, s: rt.chunk_store.retrieval_contribution_rows(
         db, s.notebook_id, [s.toc_id, s.body_id], actor_id="u",
         source_mode=None, source_ids=()), "id"),
    ("ChunkStore.retrieval_contribution_rows[include]",
     lambda rt, db, s: rt.chunk_store.retrieval_contribution_rows(
         db, s.notebook_id, [s.toc_id, s.body_id], actor_id="u",
         source_mode="include", source_ids=(s.source_id,)), "id"),
    ("ChunkStore.rows_by_ids",
     lambda rt, db, s: rt.chunk_store.rows_by_ids(db, [s.toc_id, s.body_id]), "id"),
    ("KnowledgeStore.chunk_exact_search",
     lambda rt, db, s: rt.knowledge.chunk_exact_search(db, s.notebook_id, NEEDLE, 10),
     "chunk_id"),
    ("KnowledgeStore.chunk_exact_search[ceiling]",
     lambda rt, db, s: rt.knowledge.chunk_exact_search(
         db, s.notebook_id, NEEDLE, 10, allowed_source_ids=[s.source_id]), "chunk_id"),
)


def case_chunk_row_builders_carry_navigation(repo) -> None:
    seeded = seed(repo)
    runtime = repo._runtime
    expected = {seeded.toc_id: True, seeded.body_id: False}
    with runtime.database.connect() as db:
        for name, call, key in CHUNK_ROW_BUILDERS:
            assert _flags(call(runtime, db, seeded), key) == expected, name
        sections = {
            path: _flags(runtime.chunk_store.chunks_by_section(
                db, seeded.notebook_id, seeded.source_id, path, 10))
            for path in ("Contents", "5.7.3 Standard Package")
        }
        assert sections == {
            "Contents": {seeded.toc_id: True},
            "5.7.3 Standard Package": {seeded.body_id: False},
        }
        # The reverse lookup itself is unchanged; its navigation mode reads the
        # source's flagged chunks forward.
        reverse = runtime.chunk_store.chunks_for_element_ids(
            db, seeded.notebook_id, [seeded.element("shared")])
        assert {row["chunk_id"] for row in reverse} == set(expected)
        flagged = runtime.chunk_store.chunks_for_element_ids(
            db, seeded.notebook_id,
            [seeded.element("shared"), seeded.element("body"), "el-missing"],
            navigation_source_id=seeded.source_id)
        assert [(row["element_id"], row["chunk_id"]) for row in flagged] == [
            (seeded.element("shared"), seeded.toc_id)]
        assert runtime.chunk_store.chunks_for_element_ids(
            db, seeded.notebook_id, [], navigation_source_id=seeded.source_id) == []


def case_element_rows_carry_navigation(repo) -> None:
    """An element belongs to a navigation page when a navigation chunk lists
    it in its own ``element_ids``, the shared element included; without the
    switch the historical statement carries no flag."""
    seeded = seed(repo)
    runtime = repo._runtime
    with runtime.database.connect() as db:
        rows = runtime.source_store.retrieval_element_rows(
            db, seeded.notebook_id, with_navigation=True)
        plain = runtime.source_store.retrieval_element_rows(
            db, seeded.notebook_id, with_navigation=False)
    # The flag reads the flagged chunks' own element_ids, so it holds with
    # the reverse index emptied (a notebook not backfilled yet).
    with runtime.database.write() as db:
        db.execute(
            "DELETE FROM chunk_elements WHERE notebook_id=" + _placeholder(repo),
            (seeded.notebook_id,))
    with runtime.database.connect() as db:
        unindexed = runtime.source_store.retrieval_element_rows(
            db, seeded.notebook_id, with_navigation=True)
    assert _flags(plain) == dict.fromkeys(_flags(rows), False)
    assert _flags(unindexed) == _flags(rows)
    assert _flags(rows) == {
        seeded.element("toc"): True,
        seeded.element("shared"): True,
        seeded.element("body"): False,
    }


def _navigation_versions(repo, seeded) -> tuple:
    _signature, navigation = repo._runtime.index_projections.source_subgraph_signature(
        seeded.notebook_id, [seeded.source_id], with_navigation=True)
    return navigation


def case_navigation_version_moves_with_the_flagged_set(repo) -> None:
    """``sources.navigation_version`` (the snapshot cache's key beside the
    generation signature) moves exactly when a write changes the source's set
    of flagged chunks: the build, the background pass and ``--clear``; the
    generation signature itself never carries it (on-disk partition
    manifests compare it byte for byte)."""
    seeded = seed(repo)  # the build wrote one flagged chunk: 0 -> 1
    projections = repo._runtime.index_projections
    store = repo._runtime.chunk_store
    assert _navigation_versions(repo, seeded) == ((seeded.source_id, 1),)
    plain = projections.source_subgraph_signature(seeded.notebook_id, [seeded.source_id])
    assert projections.source_subgraph_signature(
        seeded.notebook_id, [seeded.source_id], with_navigation=True
    ) == (plain, ((seeded.source_id, 1),))
    assert projections.source_subgraph_signature(
        seeded.notebook_id, [], with_navigation=True) == ((0, 0, 0, ()), ())

    # A pass that changes no flag leaves the counter alone.
    token = store.chunk_generation(seeded.source_id)
    assert store.apply_navigation_flags(
        seeded.source_id, expected_chunked_at=token, changes=[])
    assert _navigation_versions(repo, seeded) == ((seeded.source_id, 1),)
    # A pass that moves the flag bumps it, and the plain signature is unmoved.
    assert store.apply_navigation_flags(
        seeded.source_id, expected_chunked_at=token,
        changes=[(seeded.toc_id, False), (seeded.body_id, True)])
    assert _navigation_versions(repo, seeded) == ((seeded.source_id, 2),)
    assert projections.source_subgraph_signature(
        seeded.notebook_id, [seeded.source_id]) == plain

    # A rebuild with the same flagged set does not bump; a different one does.
    now = repo._runtime.seams.now()
    same = [
        ChunkWrite(seeded.toc_id, "toc", "Contents", (), navigation=False),
        ChunkWrite(seeded.body_id, "body", "Body", (), navigation=True),
    ]
    assert store.replace_source_chunks(
        seeded.source_id, seeded.notebook_id, same, created_at=now,
        mark_chunked_at=now, navigation_scanned_at=now)
    assert _navigation_versions(repo, seeded) == ((seeded.source_id, 2),)
    assert store.replace_source_chunks(
        seeded.source_id, seeded.notebook_id,
        [ChunkWrite(seeded.toc_id, "toc", "Contents", (), navigation=True)],
        created_at=now, mark_chunked_at=now, navigation_scanned_at=now)
    assert _navigation_versions(repo, seeded) == ((seeded.source_id, 3),)

    # --clear bumps every source whose flagged set it empties, only those.
    other = seed(repo)
    assert store.apply_navigation_flags(
        other.source_id, expected_chunked_at=store.chunk_generation(other.source_id),
        changes=[(other.toc_id, False)])
    assert _navigation_versions(repo, other) == ((other.source_id, 2),)
    cleared, _reset, _notebooks = store.clear_navigation_flags()
    assert cleared >= 1
    assert _navigation_versions(repo, seeded) == ((seeded.source_id, 4),)
    assert _navigation_versions(repo, other) == ((other.source_id, 2),)


def case_service_builders_carry_navigation(repo, monkeypatch) -> None:
    """The service-side row builders: the dicts ``score_chunks`` reads and the
    by-id hydrations that build ``RetrievedChunk`` directly."""
    seeded = seed(repo)
    candidates = repo.retrieval.candidates
    expected = {seeded.toc_id: True, seeded.body_id: False}
    rows, _ids, _matrix = candidates.hydrate_chunk_candidates(
        [seeded.toc_id, seeded.body_id])
    assert {row["chunk_id"]: row["navigation"] for row in rows} == expected
    assert {
        row["chunk_id"]: row["navigation"]
        for row in candidates._hydrate_chunk_texts([seeded.toc_id, seeded.body_id])
    } == expected
    with candidates._connect() as db:
        gathered = candidates._gather_chunks(db, seeded.notebook_id)
        scoped = candidates._gather_chunks(
            db, seeded.notebook_id, allowed_source_ids=[seeded.source_id])
    assert {row["chunk_id"]: row["navigation"] for row in gathered} == expected
    assert {row["chunk_id"]: row["navigation"] for row in scoped} == expected

    # By-id admission hydrate: drops the page under the switch, carries the
    # flag with the switch off.
    hydrate = candidates._hydrate_generated_question_chunks
    ids = [seeded.toc_id, seeded.body_id]
    assert [c.chunk_id for c in hydrate(
        seeded.notebook_id, "u", ids, allowed_source_ids=None)] == [seeded.body_id]
    assert [c.chunk_id for c in candidates.hydrate_retrieval_contribution_chunks(
        seeded.notebook_id, "u", ids)] == [seeded.body_id]
    monkeypatch.setattr(candidates.settings, "retrieval_navigation_filter_enabled", False)
    assert {
        c.chunk_id: c.navigation
        for c in hydrate(seeded.notebook_id, "u", ids, allowed_source_ids=None)
    } == expected
