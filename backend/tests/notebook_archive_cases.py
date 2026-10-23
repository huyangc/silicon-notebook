"""Notebook export (``.snbk``) behaviour -- **both backends run the same checks on the
same worlds**: ``tests/test_notebook_archive_export.py`` (SQLite) and
``tests/postgres/test_notebook_archive_export_pg.py`` (PostgreSQL) each supply only an
``insert`` adapter and a repository.

Worlds come from the copy suites: ``copy_memory_cases.world`` (Memory members, lent
evidence, mixed clusters) and ``notebook_copy_characterization_cases.full_world``
(files, assets, mounts, knowhow, a share token, a sync origin, a gone source), plus the
dangling soft pointers below. The strongest check compares the archive's rows with
``snapshot_copy_rows`` of the same notebook after canonicalising both
(``canonical_rows``: bytes decoded, instants in UTC, JSON parsed) -- independently of the
exporter's own encoder.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    EMBEDDING_WORKLOADS,
    FILES_INDEX_NAME,
    MANIFEST_NAME,
    VECTOR_TABLES,
    rows_entry,
)
from app.domain.portable_rows import decode_value
from app.services.notebook_transfer import export as export_module
from app.services.notebook_transfer.archive_reader import inspect_archive
from app.services.notebook_transfer.export import ExportReport, export_notebook
from tests import copy_memory_cases as memory_cases
from tests import notebook_copy_characterization_cases as char_cases
from tests.copy_memory_cases import Row

JSON_COLUMNS = char_cases.JSON_COLUMNS
NB = char_cases.NB

#: JSON-shaped columns of the closure, parsed before comparing (text on SQLite and in
#: the archive, jsonb on PostgreSQL).
_JSON_SHAPED = frozenset({
    "payload", "evidence", "element_ids", "metadata", "fields", "list_fields",
    "keywords", "raw_json", "expected_questions", "source_types", "taxonomy",
})


@dataclasses.dataclass(frozen=True)
class Backend:
    name: str
    insert: Callable[[Any, str, dict], None]


@dataclasses.dataclass(frozen=True)
class _Service:
    protocol: str
    model: str


def fake_services(workload: str) -> "_Service | None":
    """Bindings for the embedding identity: two workloads unbound."""
    return {
        "chunk_embedding": _Service("openai", "embed-chunks-v1"),
        "source_element_embedding": _Service("openai", "embed-elements-v1"),
        "knowledge_object_embedding": _Service("dashscope", "embed-kg-v2"),
    }.get(workload)


def seed(repo, backend: Backend, rows: list[Row], storage: Path, files: dict | None = None):
    char_cases._seed(repo, backend, rows, storage)
    if files:
        char_cases._write_files(storage, files)


def export(repo, notebook_id: str, out: Path, **kwargs) -> ExportReport:
    store = repo._runtime.sharing_store
    settings = store.settings
    return export_notebook(
        store, notebook_id, out,
        storage_dir=Path(repo.storage_dir), settings=settings,
        embeddings=export_module.embedding_identities(settings, fake_services),
        global_object_schemas=repo.list_object_schemas(),
        **kwargs,
    )


def archive_rows(path: Path) -> dict[str, list[dict]]:
    with zipfile.ZipFile(path) as archive:
        return {
            table: [
                json.loads(line)
                for line in archive.read(rows_entry(table)).decode("utf-8").splitlines()
            ]
            for table in ARCHIVE_TABLES
        }


def archive_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read(MANIFEST_NAME))


def archive_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        index = [json.loads(line) for line in archive.read(FILES_INDEX_NAME).splitlines()]
        return {item["path"]: archive.read(item["path"]) for item in index}


def _instant(value: Any) -> Any:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        return value
    if parsed.tzinfo is None:
        return value if isinstance(value, str) else parsed.isoformat()
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical_value(column: str, value: Any) -> Any:
    value = decode_value(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": bytes(value).hex()}
    if column in _JSON_SHAPED and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if column.endswith("_at") or column in ("last_reviewed", "delete_after"):
        return _instant(value)
    return value


def canonical_rows(rows: list[dict]) -> list[str]:
    return sorted(
        json.dumps(
            {column: _canonical_value(column, value) for column, value in row.items()},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        for row in rows
    )


# --------------------------------------------------------------- the checks


def _expected_from_snapshot(snapshot: dict, storage: Path, notebook_id: str) -> dict:
    """The snapshot's rows with exactly the documented export differences applied,
    each spelled out here rather than taken from the exporter: no share link; no
    mount edges; file paths relative to the notebook's file tree; attachments of a
    source outside the closure dropped."""
    source_ids = {row["id"] for row in snapshot.get("sources", [])}
    notebook_root = storage / "notebooks" / notebook_id
    expected: dict[str, list[dict]] = {}
    for table in ARCHIVE_TABLES:
        rows = [dict(row) for row in snapshot.get(table, [])]
        for row in rows:
            row.pop("_copy_starts_dirty", None)
        if table == "notebooks":
            for row in rows:
                row.update(share_token=None, is_shared=0)
        elif table == "notebook_bases":
            rows = []
        elif table == "sources":
            for row in rows:
                if row.get("file_path"):
                    row["file_path"] = Path(row["file_path"]).relative_to(notebook_root).as_posix()
        elif table == "notebook_assets":
            rows = [r for r in rows if r.get("source_id") is None or r["source_id"] in source_ids]
        expected[table] = rows
    return expected


def _assert_rows_equal_snapshot(out: Path, rows: dict, snapshot: dict, storage: Path,
                                notebook_id: str) -> dict:
    inspection = inspect_archive(out)
    assert set(rows) == set(ARCHIVE_TABLES)
    expected = _expected_from_snapshot(snapshot, storage, notebook_id)
    for table in ARCHIVE_TABLES:
        assert canonical_rows(rows[table]) == canonical_rows(expected[table]), table
        assert inspection.manifest.tables[table].rows == len(expected[table]), table
    return expected


def check_full_world_export_equals_snapshot(repo, backend: Backend, tmp: Path) -> None:
    """The same row-by-row comparison on the full world, where the knowhow tables,
    the object schema, the attachments and the mounts have rows: only the notebook's
    mount edges table is empty in the archive, and the differences from the snapshot
    are exactly the documented ones."""
    storage = Path(repo.storage_dir)
    seed(repo, backend, char_cases.full_world(), storage, char_cases.FULL_FILES)
    snapshot = repo._runtime.sharing_store.snapshot_copy_rows(NB)
    out = tmp / "full-equals.snbk"
    report = export(repo, NB, out)
    rows = archive_rows(out)
    expected = _assert_rows_equal_snapshot(out, rows, snapshot, storage, NB)
    assert {table for table in ARCHIVE_TABLES if not expected[table]} == {"notebook_bases"}
    assert len(snapshot["notebook_bases"]) == 4 and len(snapshot["notebook_assets"]) == 4
    assert report.normalized == {"notebook_assets.source_id": 1}


def check_dirty_notebook_carries_no_clusters(repo, backend: Backend, tmp: Path) -> None:
    """A dirty notebook without Memory: its clustering is out of date, so the archive
    carries no cluster row and says an import starts dirty with clusters dropped."""
    storage = Path(repo.storage_dir)
    rows = memory_cases.world(memory_cases.NOTEBOOK_PLAIN, with_memory=False) + [
        Row("unified_kg_state", {"notebook_id": memory_cases.NOTEBOOK_PLAIN, "dirty": 1,
                                 "kg_mutation_seq": 5, "updated_at": memory_cases.NOW}),
    ]
    seed(repo, backend, rows, storage)
    assert any(row.table == "concept_clusters" for row in rows)
    out = tmp / "dirty.snbk"
    export(repo, memory_cases.NOTEBOOK_PLAIN, out)
    inspect_archive(out)
    assert archive_manifest(out)["kg"] == {"starts_dirty": True, "clusters_dropped": True}
    assert archive_rows(out)["concept_clusters"] == []


def check_stream_reads_one_snapshot(repo, backend: Backend) -> None:
    """Every statement reads the snapshot the root read pinned: a chunk another
    connection commits while the stream is open (after ``sources``, before ``chunks``)
    is not in it, though it is in the database afterwards."""
    import threading

    seed(repo, backend, char_cases.full_world(), Path(repo.storage_dir))
    store = repo._runtime.sharing_store
    seen: dict[str, list] = {}
    late_chunk = {
        "id": "ck-late", "notebook_id": NB, "source_id": "src-web",
        "text": "committed mid-export", "element_ids": [], "created_at": memory_cases.NOW,
    }
    errors: list[BaseException] = []

    def commit_elsewhere() -> None:
        try:
            with repo._runtime.database.write() as db:
                backend.insert(db, "chunks", late_chunk)
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the test thread
            errors.append(exc)

    def on_table(table, pages):
        seen[table] = [row for page in pages for row in page]
        if table == "sources":
            writer = threading.Thread(target=commit_elsewhere)
            writer.start()
            writer.join()

    store.iter_copy_rows(NB, on_table=on_table)
    assert not errors, errors
    assert seen["chunks"] and "ck-late" not in {row["id"] for row in seen["chunks"]}
    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT id FROM chunks WHERE id = 'ck-late'").fetchone()


def check_consumer_must_read_every_page(repo, backend: Backend, tmp: Path) -> None:
    """A consumer that returns with pages unread fails loudly (its cursor would
    otherwise outlive the connection); one that raises mid-table propagates its error.
    The store is usable afterwards either way."""
    from app.repositories.ports import CopyRowsNotConsumedError

    seed(repo, backend, char_cases.full_world(), Path(repo.storage_dir),
         char_cases.FULL_FILES)
    store = repo._runtime.sharing_store

    def lazy(table, pages):
        if table == "chunks":
            next(pages)  # one page of several, then return
        else:
            for _page in pages:
                pass

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(
            importlib.import_module(type(store).__module__), "COPY_STREAM_PAGE_ROWS", 1
        )
        with pytest.raises(CopyRowsNotConsumedError, match="chunks"):
            store.iter_copy_rows(NB, on_table=lazy)
        with pytest.raises(CopyRowsNotConsumedError, match="notebooks"):
            store.iter_copy_rows(NB, on_table=lambda table, pages: None)

    class Boom(Exception):
        pass

    def failing(table, pages):
        for page in pages:
            if table == "knowledge_objects":
                raise Boom(table)

    with pytest.raises(Boom):
        store.iter_copy_rows(NB, on_table=failing)
    out = tmp / "after-failures.snbk"
    export(repo, NB, out)
    inspect_archive(out)


def check_export_equals_snapshot(repo, backend: Backend, tmp: Path) -> None:
    """Memory world: every archive table holds exactly the snapshot's rows, canonicalised
    (bytes decoded, instants in UTC, JSON parsed), less only the documented export rules
    (no share link, no mount edges); no Memory text anywhere; the archive verifies."""
    storage = Path(repo.storage_dir)
    seed(repo, backend, memory_cases.world(), storage)
    store = repo._runtime.sharing_store
    snapshot = store.snapshot_copy_rows(memory_cases.NOTEBOOK)
    out = tmp / "memory.snbk"
    report = export(repo, memory_cases.NOTEBOOK, out)

    rows = archive_rows(out)
    assert report.normalized == {}
    _assert_rows_equal_snapshot(out, rows, snapshot, storage, memory_cases.NOTEBOOK)
    # Memory sources, Memory-derived rows and the evidence a Memory lent are absent.
    with zipfile.ZipFile(out) as archive:
        for name in archive.namelist():
            assert memory_cases.MARK.encode() not in archive.read(name), name
    assert {row["id"] for row in rows["sources"]} == {"src-doc"}
    assert {row["id"] for row in rows["knowledge_objects"]} == memory_cases.SHARED_OBJECTS
    for row in rows["knowledge_objects"] + rows["knowledge_relations"]:
        for item in json.loads(row["evidence"]):
            assert not str(item.get("source_id", "")).startswith("src-mem"), row["id"]
    manifest = archive_manifest(out)
    assert manifest["kg"] == {
        "starts_dirty": True, "clusters_dropped": False, "concept_seed_version": 2,
    }
    assert manifest["excluded"] == {"memory": "not_carried", "mount_edges": 0}


def check_byte_columns_and_timestamps_are_portable(repo, backend: Backend, tmp: Path) -> None:
    """Vectors travel as ``{"$bytes": b64}`` that decode to the stored bytes; every
    timestamp the world wrote reads back as the same UTC instant text."""
    storage = Path(repo.storage_dir)
    seed(repo, backend, memory_cases.world(), storage)
    out = tmp / "portable.snbk"
    export(repo, memory_cases.NOTEBOOK, out)
    rows = archive_rows(out)
    vector = next(row for row in rows["chunk_embeddings"] if row["chunk_id"] == "ck-doc-1")
    assert set(vector["vector"]) == {"$bytes"}
    assert decode_value(vector["vector"]) == b"vec-ck-doc-1"
    for table in ("sources", "chunks", "knowledge_objects"):
        for row in rows[table]:
            assert row["created_at"] == memory_cases.NOW, (table, row["created_at"])
    assert isinstance(rows["knowledge_objects"][0]["payload"], str)


def check_full_world_export(repo, backend: Backend, tmp: Path) -> None:
    """Files, assets, mounts, share link, embedding identity, global object types."""
    storage = Path(repo.storage_dir)
    seed(repo, backend, char_cases.full_world(), storage, char_cases.FULL_FILES)
    out = tmp / "full.snbk"
    report = export(repo, NB, out)
    inspect_archive(out)
    rows = archive_rows(out)
    manifest = archive_manifest(out)
    files = archive_files(out)

    # Exactly this notebook's two trees, byte for byte, and nothing of the others.
    expected_files = {
        "files/notebooks/doc.md": char_cases.FULL_FILES[f"notebooks/{NB}/doc.md"],
        "files/notebooks/extra/notes.txt": char_cases.FULL_FILES[f"notebooks/{NB}/extra/notes.txt"],
        "files/assets/asset-paste-1.png": char_cases.PNG_A,
        "files/assets/asset-img-1.png": char_cases.PNG_B,
        "files/assets/asset-img-2.png": char_cases.PNG_C,
    }
    assert files == expected_files
    with zipfile.ZipFile(out) as archive:
        index = [json.loads(line) for line in archive.read(FILES_INDEX_NAME).splitlines()]
    for item in index:
        assert item["sha256"] == hashlib.sha256(expected_files[item["path"]]).hexdigest()
        assert item["size"] == len(expected_files[item["path"]])
    assert manifest["files"]["count"] == 5
    assert manifest["files"]["bytes"] == sum(len(b) for b in expected_files.values())
    assert report.files == 5 and report.missing_source_files == 0

    # The share link never travels; the file path is relative to the notebook tree.
    (notebook,) = rows["notebooks"]
    assert notebook["share_token"] is None and notebook["is_shared"] == 0
    doc = next(row for row in rows["sources"] if row["id"] == "src-doc")
    assert doc["file_path"] == "doc.md"
    assert str(storage) not in json.dumps(rows, ensure_ascii=False)
    # Mount edges are counted, not carried.
    assert rows["notebook_bases"] == []
    assert manifest["excluded"] == {"memory": "not_carried", "mount_edges": 4}
    # The asset of the gone source is dropped (with its file, which is gone anyway).
    assert {row["id"] for row in rows["notebook_assets"]} == {
        "asset-paste-1", "asset-img-1", "asset-img-2",
    }
    assert report.normalized == {"notebook_assets.source_id": 1}
    # In-closure soft pointers are kept as they are.
    (backfill,) = [r for r in rows["knowledge_source_fact_backfills"] if r["source_id"] == "src-doc"]
    assert backfill["after_object_id"] == "ko-shared-2"
    web = next(row for row in rows["knowledge_objects"] if row["id"] == "ko-web-1")
    assert web["source_candidate_id"] == "src-doc"
    # Knowhow tables and the object schema travel; projected knowhow objects do not.
    assert {row["id"] for row in rows["knowhow_tables"]} == {"kt-1", "kt-2"}
    assert "ko-kh-projected" not in {row["id"] for row in rows["knowledge_objects"]}
    assert [row["object_type"] for row in rows["notebook_object_schemas"]] == ["device"]
    # Embedding identity per workload, unbound workloads recorded as null.
    workloads = manifest["embeddings"]["workloads"]
    assert set(workloads) == set(EMBEDDING_WORKLOADS)
    assert workloads["chunk_embedding"] == {
        "protocol": "openai", "model": "embed-chunks-v1",
        "dim": repo._runtime.sharing_store.settings.embed_dim, "encoding": "float32-le",
    }
    assert workloads["relation_embedding"] is None and workloads["knowhow_embedding"] is None
    assert manifest["embeddings"]["vectors_included"] is True
    assert manifest["format"] == "silicon-notebook.notebook-archive"
    assert manifest["format_version"] == 1
    assert manifest["notebook"]["name"] == "Shared"
    assert memory_cases.MARK not in json.dumps(rows, ensure_ascii=False)


def dangling_rows() -> list[Row]:
    """Soft pointers that leave the closure, one per normalisation point."""
    return [
        Row("sources", {"id": "src-other-nb", "notebook_id": "nb-base-public",
                        "title": "elsewhere", "source_type": "document", "status": "ready",
                        "parse_status": "ready", "created_at": memory_cases.NOW,
                        "updated_at": memory_cases.NOW}),
        Row("knowledge_objects", {
            "id": "ko-dangling", "notebook_id": NB, "object_type": "concept",
            "status": "approved", "source_id": "src-vanished",
            "source_candidate_id": "src-other-nb", "payload": {"name": "dangling"},
            "evidence": [], "created_at": memory_cases.NOW, "updated_at": memory_cases.NOW,
        }),
        Row("knowledge_source_facts", {
            "id": "fact-dangling", "notebook_id": NB, "source_id": "src-web",
            "source_generation": "gen-web-a", "local_object_id": "local-dangling",
            "global_object_id": "ko-vanished", "object_type": "concept",
            "payload": {"name": "dangling fact"}, "evidence": [],
            "created_at": char_cases.GEN_LATE, "updated_at": char_cases.GEN_LATE,
        }),
        Row("knowledge_source_fact_backfills", {
            "source_id": "src-web", "notebook_id": NB, "source_generation": "gen-web-a",
            "status": "complete", "after_object_id": "ko-vanished",
            "created_at": memory_cases.NOW, "updated_at": memory_cases.NOW,
        }),
        Row("knowledge_relations", {
            "id": "kr-dangling", "notebook_id": NB, "source_id": "src-other-nb",
            "source_object_id": "ko-web-1", "target_object_id": "ko-shared-1",
            "edge_type": "related_to", "evidence": [], "created_at": memory_cases.NOW,
        }),
        Row("concept_clusters", {
            "id": "cc-dangling", "notebook_id": NB, "canonical_id": "K-dangling",
            "member_object_id": "ko-vanished", "canonical_name": "dangling",
            "object_type": "concept", "created_at": memory_cases.NOW, "generation": 0,
        }),
        Row("concept_clusters", {
            "id": "cc-minted-gone", "notebook_id": NB, "canonical_id": "KL-~ko-kh-projected",
            "member_object_id": "ko-web-1", "canonical_name": "minted", "object_type": "concept",
            "created_at": memory_cases.NOW, "generation": 0,
        }),
        Row("concept_clusters", {
            "id": "cc-minted-kept", "notebook_id": NB, "canonical_id": "K-~ko-web-blank",
            "member_object_id": "ko-web-blank", "canonical_name": "kept",
            "object_type": "concept", "created_at": memory_cases.NOW, "generation": 0,
        }),
        Row("knowhow_tables", {
            "id": "kt-3", "notebook_id": NB, "title": "空表", "description": "d",
            "mutation_seq": 1, "hidden_source_id": "src-kh-vanished",
            "created_by": memory_cases.OWNER, "created_at": memory_cases.NOW,
            "updated_at": memory_cases.NOW,
        }),
    ]


def check_global_object_types(repo, backend: Backend, tmp: Path) -> None:
    """A deployment-wide custom type the KG uses travels in the manifest; a built-in
    type, a type the notebook defines itself and an unused custom type do not."""
    storage = Path(repo.storage_dir)
    now = memory_cases.NOW

    def schema(object_type: str, source: str) -> Row:
        return Row("object_schemas", {
            "object_type": object_type, "plural": f"{object_type}s",
            "fields": ["name", "vendor"], "primary_field": "name", "description": "d",
            "label": f"label {object_type}", "list_fields": ["name"], "source": source,
            "status": "active", "rationale": "", "notebook_id": "", "created_at": now,
            "updated_at": now,
        })

    def typed_object(oid: str, object_type: str) -> Row:
        return Row("knowledge_objects", {
            "id": oid, "notebook_id": NB, "object_type": object_type, "status": "approved",
            "source_id": "src-web", "payload": {"name": oid}, "evidence": [],
            "created_at": now, "updated_at": now,
        })

    # ``sensor`` also has a local PROPOSED row: review-only, the registry keeps
    # resolving the global definition, so the global one still travels (codex
    # #974 r4). ``device`` has an active local row, which does shadow it.
    proposed_sensor = Row("notebook_object_schemas", {
        "notebook_id": NB, "object_type": "sensor", "plural": "sensors",
        "fields": [{"name": "name", "type": "text"}], "primary_field": "name",
        "list_fields": ["name"], "status": "proposed", "created_by": char_cases.OWNER,
        "created_at": now, "updated_at": now,
    })
    extra = [
        schema("chip", "custom"), schema("device", "custom"), schema("unused", "induced"),
        schema("sensor", "custom"), proposed_sensor,
        typed_object("ko-chip", "chip"), typed_object("ko-device", "device"),
        typed_object("ko-sensor", "sensor"),
    ]
    seed(repo, backend, char_cases.full_world() + extra, storage, char_cases.FULL_FILES)
    out = tmp / "types.snbk"
    export(repo, NB, out)
    inspect_archive(out)
    assert archive_manifest(out)["global_object_types"] == [
        {
            "object_type": object_type, "plural": f"{object_type}s",
            "fields": ["name", "vendor"], "primary_field": "name", "description": "d",
            "label": f"label {object_type}", "list_fields": ["name"], "status": "active",
        }
        for object_type in ("chip", "sensor")
    ]


def check_soft_pointers_are_normalised(repo, backend: Backend, tmp: Path) -> None:
    """Every pass-through point of the deep copy that can leave the closure is cleared
    (or its row dropped) in the archive, and counted."""
    storage = Path(repo.storage_dir)
    files = dict(char_cases.FULL_FILES)
    files[f"assets/{NB}/asset-gone.gif"] = b"GIF89a-gone"
    seed(repo, backend, char_cases.full_world() + dangling_rows(), storage, files)
    out = tmp / "dangling.snbk"
    report = export(repo, NB, out)
    inspect_archive(out)
    rows = archive_rows(out)
    by_id = {table: {row.get("id"): row for row in rows[table]} for table in rows}

    dangling = by_id["knowledge_objects"]["ko-dangling"]
    assert dangling["source_id"] == "" and dangling["source_candidate_id"] is None
    assert by_id["knowledge_source_facts"]["fact-dangling"]["global_object_id"] == ""
    (backfill,) = [r for r in rows["knowledge_source_fact_backfills"] if r["source_id"] == "src-web"]
    assert backfill["after_object_id"] == ""
    assert by_id["knowledge_relations"]["kr-dangling"]["source_id"] is None
    assert "cc-dangling" not in by_id["concept_clusters"]
    assert "cc-minted-gone" not in by_id["concept_clusters"]
    assert by_id["concept_clusters"]["cc-minted-kept"]["canonical_id"] == "K-~ko-web-blank"
    assert by_id["knowhow_tables"]["kt-3"]["hidden_source_id"] is None
    assert "asset-gone" not in by_id["notebook_assets"]
    assert "files/assets/asset-gone.gif" not in archive_files(out)
    # In-closure pointers of the same rows' neighbours are untouched.
    assert by_id["knowhow_tables"]["kt-1"]["hidden_source_id"] == "src-kh-1"
    assert by_id["knowledge_relations"]["kr-web"]["source_id"] == "src-web"
    assert report.normalized == {
        "concept_clusters.canonical_id": 1,
        "concept_clusters.member_object_id": 1,
        "knowhow_tables.hidden_source_id": 1,
        "knowledge_objects.source_candidate_id": 1,
        "knowledge_objects.source_id": 1,
        "knowledge_relations.source_id": 1,
        "knowledge_source_fact_backfills.after_object_id": 1,
        "knowledge_source_facts.global_object_id": 1,
        "notebook_assets.source_id": 1,
    }


def check_without_vectors(repo, backend: Backend, tmp: Path) -> None:
    storage = Path(repo.storage_dir)
    seed(repo, backend, char_cases.full_world(), storage, char_cases.FULL_FILES)
    out = tmp / "novec.snbk"
    report = export(repo, NB, out, include_vectors=False)
    inspect_archive(out)
    rows = archive_rows(out)
    for table in VECTOR_TABLES:
        assert rows[table] == [], table
    assert rows["chunk_questions"], "question vectors are NOT NULL and travel"
    manifest = archive_manifest(out)
    assert manifest["embeddings"]["vectors_included"] is False
    assert not report.vectors_included
    full = tmp / "withvec.snbk"
    export(repo, NB, full)
    assert all(archive_rows(full)[table] for table in VECTOR_TABLES)


def check_streams_in_pages(repo, backend: Backend, tmp: Path, monkeypatch, store_module) -> None:
    """With a one-row page every table arrives as one page per row, and the archive is
    the same as with the default page size."""
    storage = Path(repo.storage_dir)
    seed(repo, backend, char_cases.full_world(), storage, char_cases.FULL_FILES)
    big = tmp / "big-pages.snbk"
    export(repo, NB, big)
    store = repo._runtime.sharing_store
    pages: dict[str, list[int]] = {}
    real = store.iter_copy_rows

    def counting(notebook_id, *, on_table, skip_tables=frozenset()):
        def spy(table, page_iter):
            def counted():
                for page in page_iter:
                    pages.setdefault(table, []).append(len(page))
                    yield page
            on_table(table, counted())
        return real(notebook_id, on_table=spy, skip_tables=skip_tables)

    monkeypatch.setattr(store_module, "COPY_STREAM_PAGE_ROWS", 1)
    monkeypatch.setattr(store, "iter_copy_rows", counting)
    small = tmp / "small-pages.snbk"
    export(repo, NB, small)
    objects = len(archive_rows(small)["knowledge_objects"])
    assert objects >= 3
    assert pages["knowledge_objects"] == [1] * objects
    assert pages["chunks"] == [1] * len(archive_rows(small)["chunks"])
    assert archive_rows(small) == archive_rows(big)


def check_exports_beyond_the_copy_limits(repo, backend: Backend, tmp: Path, monkeypatch) -> None:
    from app.repositories.ports import NotebookTooLargeToCopyError

    storage = Path(repo.storage_dir)
    seed(repo, backend, char_cases.full_world(), storage, char_cases.FULL_FILES)
    store = repo._runtime.sharing_store
    reference = tmp / "reference.snbk"
    export(repo, NB, reference)
    monkeypatch.setattr(store.settings, "notebook_copy_max_rows", 1)
    monkeypatch.setattr(store.settings, "notebook_copy_max_snapshot_rows", 1)
    with pytest.raises(NotebookTooLargeToCopyError):
        store.snapshot_copy_rows(NB)
    out = tmp / "too-large-to-copy.snbk"
    export(repo, NB, out)
    inspect_archive(out)
    assert archive_rows(out) == archive_rows(reference)


def check_unknown_notebook_and_cleanup(repo, backend: Backend, tmp: Path, monkeypatch) -> None:
    from app.services.notebook_transfer.export import NotebookExportError

    storage = Path(repo.storage_dir)
    seed(repo, backend, char_cases.full_world(), storage, char_cases.FULL_FILES)
    with pytest.raises(KeyError):
        export(repo, "nb-does-not-exist", tmp / "missing.snbk")
    assert list(tmp.glob("missing.snbk*")) == []

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(export_module._FileIndex, "add_tree", boom)
        with pytest.raises(OSError):
            export(repo, NB, tmp / "failed.snbk")
    assert list(tmp.glob("failed.snbk*")) == [], "the part file is removed on failure"

    existing = tmp / "existing.snbk"
    existing.write_bytes(b"keep me")
    with pytest.raises(NotebookExportError):
        export(repo, NB, existing)
    assert existing.read_bytes() == b"keep me"
