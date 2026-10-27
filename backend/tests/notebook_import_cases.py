"""Notebook import (``.snbk``) behaviour -- **both backends run the same checks on the
same worlds**: ``tests/test_notebook_import.py`` (SQLite -> SQLite) and
``tests/postgres/test_notebook_import_pg.py`` (SQLite -> PostgreSQL, PostgreSQL ->
SQLite, PostgreSQL -> PostgreSQL) each supply repositories and ``insert`` adapters.

Worlds are the export suites' (``copy_memory_cases.world`` and
``notebook_copy_characterization_cases.full_world``), their vectors resized to
``4 * EMBED_DIM`` bytes so a matching model keeps them. The round trip exports a
notebook, imports it, exports the import again and compares the two archives table by
table after translating every old id through the import's own remap maps
(``capture_run``) -- and after applying, spelled out here, every documented import
difference (``_expected_archive``). Failure checks compare whole-database row counts
and both storage trees before and after (``residue``): a refused or failed import
leaves nothing.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    FILES_INDEX_NAME,
    MANIFEST_NAME,
    rows_entry,
)
from app.domain.portable_rows import DigestCounter, decode_value, json_document, json_line
from app.services.notebook_transfer import archive_reader
from app.services.notebook_transfer import export as export_module
from app.services.notebook_transfer.import_ import NotebookImportError
from tests import copy_memory_cases as memory_cases
from tests import notebook_archive_cases as archive_cases
from tests import notebook_copy_characterization_cases as char_cases
from tests.copy_memory_cases import Row

DIM = 4
ENV = {"EMBED_DIM": str(DIM)}
"""Settings every repository of these checks needs (set before it is built)."""
IMPORTER = "u-importer"
NB = char_cases.NB
Backend = archive_cases.Backend


# --------------------------------------------------------------- worlds


def resized(rows: list[Row]) -> list[Row]:
    """The world with every vector ``4 * DIM`` bytes long (deterministic)."""
    out = []
    for row in rows:
        values = dict(row.values)
        vector = values.get("vector")
        if isinstance(vector, (bytes, bytearray)):
            values["vector"] = (bytes(vector) * (4 * DIM))[: 4 * DIM]
        out.append(Row(row.table, values))
    return out


def full_world() -> list[Row]:
    return resized(char_cases.full_world())


def memory_world() -> list[Row]:
    return resized(memory_cases.world())


def placeholder(repo) -> str:
    return "?" if repo.settings.database_url.startswith("sqlite") else "%s"


def ensure_importer(repo) -> None:
    """The importing user, in whichever deployment imports."""
    ph = placeholder(repo)
    columns = repo._runtime.sharing_store.import_table_columns(["users"])["users"]
    values = {
        "id": IMPORTER, "username": IMPORTER, "email": f"{IMPORTER}@example.test",
        "display_name": IMPORTER, "role": "user", "password_hash": "", "password_salt": "",
        "password_iterations": 0, "created_at": memory_cases.NOW, "updated_at": memory_cases.NOW,
    }
    values = {k: v for k, v in values.items() if k in columns}
    with repo._runtime.database.write() as db:
        if db.execute(f"SELECT 1 FROM users WHERE id = {ph}", (IMPORTER,)).fetchone():
            return
        db.execute(
            f"INSERT INTO users ({','.join(values)}) VALUES ({','.join(ph for _ in values)})",
            tuple(values.values()),
        )


WORLDS = {
    "full": (full_world, NB, char_cases.FULL_FILES),
    "memory": (memory_world, memory_cases.NOTEBOOK, {}),
}


def seed_world(repo, backend: Backend, world: str) -> str:
    rows, notebook_id, files = WORLDS[world]
    archive_cases.seed(repo, backend, rows(), Path(repo.storage_dir), files)
    return notebook_id


def export(repo, notebook_id: str, out: Path, **kwargs):
    return archive_cases.export(repo, notebook_id, out, **kwargs)


# --------------------------------------------------------------- the import


def service(repo):
    return repo._runtime.notebook_import


def target_models(repo, monkeypatch, services: Callable[[str], Any] = archive_cases.fake_services):
    """Make this deployment bind ``services`` (default: the models the export
    fixture records, so those workloads keep their vectors)."""
    settings = repo.settings
    monkeypatch.setattr(
        service(repo), "_target_embeddings",
        lambda: export_module.embedding_identities(settings, services),
    )


def capture_run(repo, monkeypatch) -> dict:
    """Record the run object (its remap maps) of the next imports."""
    captured: dict = {}
    real = service(repo)._new_run

    def recording(*args, **kwargs):
        run = real(*args, **kwargs)
        captured.setdefault("runs", []).append(run)
        captured["run"] = run
        return run

    monkeypatch.setattr(service(repo), "_new_run", recording)
    return captured


def run_import(repo, path: Path, **kwargs):
    kwargs.setdefault("owner_id", IMPORTER)
    ensure_importer(repo)
    return service(repo).import_notebook(path, **kwargs)


# --------------------------------------------------------------- residue


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        if repo.settings.database_url.startswith("sqlite"):
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [row["name"] for row in rows]
        rows = db.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
        return [row["name"] for row in rows]


def residue(repo) -> dict:
    """Every table's row count and both storage trees: what a failed import must
    leave exactly as it found it."""
    counts = {}
    with repo._runtime.database.connect() as db:
        for table in _tables(repo):
            if table.endswith(("_fts_data", "_fts_idx", "_fts_docsize", "_fts_config", "_fts_content")):
                continue
            row = db.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
            counts[table] = int(row["n"] if hasattr(row, "keys") else row[0])
    storage = Path(repo.storage_dir)
    trees = {
        top: sorted(p.name for p in (storage / top).iterdir()) if (storage / top).is_dir() else []
        for top in ("notebooks", "assets")
    }
    return {"rows": counts, "trees": trees}


def assert_no_residue(repo, before: dict) -> None:
    after = residue(repo)
    changed = {
        table: (before["rows"].get(table), count)
        for table, count in after["rows"].items()
        if before["rows"].get(table) != count
    }
    assert not changed, changed
    assert after["trees"] == before["trees"]


def assert_refused(repo, path: Path, match: str, **kwargs) -> None:
    ensure_importer(repo)
    before = residue(repo)
    with pytest.raises(NotebookImportError, match=match):
        run_import(repo, path, **kwargs)
    assert_no_residue(repo, before)


# --------------------------------------------------------------- comparing


def _instant(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    elif isinstance(value, datetime):
        parsed = value
    else:
        return value
    if parsed.tzinfo is None:  # the archive convention: a naive instant is UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


_JSON = archive_cases._JSON_SHAPED


def _canonical(column: str, value: Any) -> Any:
    value = decode_value(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": bytes(value).hex()}
    if column in _JSON and isinstance(value, str):
        try:
            return json.loads(value) if value else []
        except ValueError:
            return value
    if column.endswith("_at") or column in ("last_reviewed", "delete_after"):
        # SQLite's "never" sentinel ``""`` is PostgreSQL's NULL (POSTGRES_EMPTY_TIME_SENTINELS).
        return None if value == "" else _instant(value)
    return value


def canonical(rows: list[dict], drop: frozenset = frozenset()) -> list[str]:
    return sorted(
        json.dumps(
            {c: _canonical(c, v) for c, v in row.items() if c not in drop},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        for row in rows
    )


def translation(run, new_notebook_id: str) -> dict:
    ids: dict = {run.source_notebook_id: new_notebook_id}
    for mapping in (
        run.source_map, run.element_map, run.chunk_map, run.object_map, run.fact_map,
        run.relation_map, run.khtbl_map, run.khcol_map, run.khrow_map, run.khcel_map,
        run.khcode_map, run.asset_map,
    ):
        ids.update(mapping)
    for (_source, generation), new in run.generation_map.items():
        ids[generation] = new
    return ids


class Translator:
    """An archive value with every old id replaced by its new one; an id stored
    inside JSON with no new id is cleared (``element_ids`` loses it) -- the import's
    documented rule, written out independently of the writer."""

    def __init__(self, ids: dict) -> None:
        self.ids = ids

    def text(self, value: str) -> str:
        if value in self.ids:
            return self.ids[value]
        value = re.sub(
            r"asset://([\w-]+)", lambda m: "asset://" + self.ids.get(m.group(1), m.group(1)),
            value,
        )
        return re.sub(r"~(ko-[\w-]+)", lambda m: "~" + self.ids.get(m.group(1), m.group(1)), value)

    def json(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key in ("element_id", "source_id", "object_id", "asset_id") and isinstance(
                    item, str
                ) and item:
                    out[key] = self.ids.get(item)
                elif key == "element_ids" and isinstance(item, list):
                    out[key] = [self.ids[c] for c in item if isinstance(c, str) and c in self.ids]
                else:
                    out[key] = self.json(item)
            return out
        if isinstance(value, list):
            return [self.json(item) for item in value]
        return value

    def row(self, row: dict) -> dict:
        out = {}
        for column, value in row.items():
            if column == "element_ids" and isinstance(value, str):
                out[column] = json.dumps(
                    [self.ids[c] for c in json.loads(value or "[]") if c in self.ids]
                )
            elif column in _JSON and isinstance(value, str) and value:
                out[column] = json.dumps(self.json(json.loads(value)), ensure_ascii=False)
            elif isinstance(value, str):
                out[column] = self.text(value)
            else:
                out[column] = value
        return out


#: Columns an import rewrites for every row (compared separately) or mints anew.
_REWRITTEN = {
    "sources": frozenset({"memory_id", "agent_profile_id", "uploaded_by"}),
    "source_authors": frozenset({"id"}),
    "chunk_questions": frozenset({"id"}),
    "knowhow_tables": frozenset({"created_by", "mutation_seq"}),
    "knowhow_rows": frozenset({"projection_status"}),
    "knowhow_cell_code": frozenset({"updated_by"}),
    "notebook_assets": frozenset({"created_by"}),
    "notebook_object_schemas": frozenset({"created_by"}),
    "chunks": frozenset({"question_indexed_at"}),
}


def _knowhow_source_ids(rows: dict) -> set[str]:
    return {str(t["hidden_source_id"]) for t in rows["knowhow_tables"] if t.get("hidden_source_id")}


def check_round_trip(
    src, src_backend: Backend, dst, tmp: Path, monkeypatch, *, world: str
) -> dict:
    """Export ``world`` from ``src``, import it into ``dst`` (three workloads bound to
    the archive's models), export the import again: every table equal through the
    id maps apart from the documented import rules; vectors of kept workloads byte
    for byte, of the other two absent; files identical; the notebook live, owned
    by the importer, readable and editable. Returns the report and both archives."""
    from app.models.schemas import NotebookUpdate

    notebook_id = seed_world(src, src_backend, world)
    first = tmp / f"{world}-first.snbk"
    export(src, notebook_id, first)
    target_models(dst, monkeypatch)
    captured = capture_run(dst, monkeypatch)
    report = run_import(dst, first)
    run = captured["run"]
    second = tmp / f"{world}-second.snbk"
    export(dst, report.notebook_id, second)

    original, again = archive_cases.archive_rows(first), archive_cases.archive_rows(second)
    translate = Translator(translation(run, report.notebook_id))
    knowhow_old, knowhow_new = _knowhow_source_ids(original), _knowhow_source_ids(again)
    knowhow_chunks_old = {r["id"] for r in original["chunks"] if r["source_id"] in knowhow_old}
    kept = {w for w, verdict in report.vectors.items() if verdict.kept}
    assert kept == {"chunk_embedding", "source_element_embedding", "knowledge_object_embedding"}
    for table in ARCHIVE_TABLES:
        if table in ("notebooks", "concept_clusters"):
            continue
        rows = [r for r in original[table] if not _is_knowhow_projection(table, r, knowhow_old,
                                                                         knowhow_chunks_old)]
        if table == "relation_embeddings":
            rows = []  # relation_embedding is unbound in the archive: dropped
        if table == "chunk_embeddings":
            rows = [r for r in rows if r["chunk_id"] not in knowhow_chunks_old]
        expected = canonical([translate.row(r) for r in rows], _REWRITTEN.get(table, frozenset()))
        actual_rows = [
            r for r in again[table]
            if not _is_knowhow_projection(table, r, knowhow_new, set())
            and not (table == "chunk_embeddings" and r["chunk_id"].startswith("chunk-kh-"))
        ]
        actual = canonical(actual_rows, _REWRITTEN.get(table, frozenset()))
        assert actual == expected, (
            table, sorted(set(expected) - set(actual))[:3], sorted(set(actual) - set(expected))[:3]
        )
    _check_rewritten_columns(again, report)
    _check_clusters(dst, original, translate, report)
    _check_files(first, second, translate)

    summary = dst.get_notebook(report.notebook_id)
    assert summary.status == original["notebooks"][0]["status"]
    (row,) = again["notebooks"]
    assert row["created_by"] == IMPORTER and row["sync_origin"] == ""
    assert row["name"] == original["notebooks"][0]["name"]
    assert dst.user_can_access_notebook(report.notebook_id, IMPORTER)
    renamed = dst.update_notebook(report.notebook_id, NotebookUpdate(name="导入后改名"))
    assert renamed.name == "导入后改名"
    return {"report": report, "first": first, "second": second, "run": run}


def _is_knowhow_projection(table: str, row: dict, knowhow_sources: set, knowhow_chunks: set) -> bool:
    """Rows the post-publish Knowhow projection rewrites (elements, chunks): their
    content is the projector's, compared by ``check_post_publish_order``."""
    if table in ("source_elements", "chunks"):
        return row.get("source_id") in knowhow_sources
    if table == "chunk_embeddings":
        return row.get("chunk_id") in knowhow_chunks
    return False


def _check_rewritten_columns(again: dict, report) -> None:
    for row in again["sources"]:
        assert row["memory_id"] == "" and row["agent_profile_id"] is None
        assert row["uploaded_by"] is None
    for table, column in (
        ("knowhow_tables", "created_by"), ("knowhow_cell_code", "updated_by"),
        ("notebook_assets", "created_by"), ("notebook_object_schemas", "created_by"),
    ):
        assert {r[column] for r in again[table]} <= {IMPORTER}, table
    assert {r["projection_status"] for r in again["knowhow_rows"]} <= {"synced"}
    assert report.owner_id == IMPORTER


def _check_clusters(dst, original: dict, translate: Translator, report) -> None:
    with dst._runtime.database.connect() as db:
        placeholder = "?" if dst.settings.database_url.startswith("sqlite") else "%s"
        rows = [dict(r) for r in db.execute(
            f"SELECT * FROM concept_clusters WHERE notebook_id = {placeholder}",
            (report.notebook_id,),
        ).fetchall()]
    expected = [translate.row(r) for r in original["concept_clusters"]]
    drop = frozenset({"id", "ordinal"})
    assert canonical(rows, drop) == canonical(expected, drop)


def _check_files(first: Path, second: Path, translate: Translator) -> None:
    def renamed(path: str) -> str:
        if path.startswith("files/assets/"):
            stem, _, ext = path[len("files/assets/"):].rpartition(".")
            return f"files/assets/{translate.ids.get(stem, stem)}.{ext}"
        return path

    original = {renamed(p): b for p, b in archive_cases.archive_files(first).items()}
    assert archive_cases.archive_files(second) == original


# --------------------------------------------------------------- repeat


def check_importing_twice_makes_two_notebooks(src, src_backend, dst, tmp, monkeypatch) -> None:
    notebook_id = seed_world(src, src_backend, "full")
    path = tmp / "twice.snbk"
    export(src, notebook_id, path)
    first, second = run_import(dst, path), run_import(dst, path)
    assert first.notebook_id != second.notebook_id
    assert first.rows == second.rows
    with dst._runtime.database.connect() as db:
        placeholder = "?" if dst.settings.database_url.startswith("sqlite") else "%s"
        ids = [
            {r["id"] for r in db.execute(
                f"SELECT id FROM {table} WHERE notebook_id = {placeholder}", (nb,)
            ).fetchall()}
            for table in ("sources", "chunks", "knowledge_objects")
            for nb in (first.notebook_id, second.notebook_id)
        ]
    for index in range(0, len(ids), 2):
        assert ids[index] and not ids[index] & ids[index + 1]
    # Deleting one leaves the other whole.
    dst.delete_notebook(first.notebook_id)
    assert dst.get_notebook(second.notebook_id).id == second.notebook_id
    export(dst, second.notebook_id, tmp / "second-after-delete.snbk")


# --------------------------------------------------------------- vectors


def _vector_rows(repo, notebook_id: str) -> dict[str, int]:
    placeholder = "?" if repo.settings.database_url.startswith("sqlite") else "%s"
    with repo._runtime.database.connect() as db:
        return {
            table: int(db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE notebook_id = {placeholder}",
                (notebook_id,),
            ).fetchone()["n"])
            for table in ("chunk_embeddings", "element_embeddings", "knowledge_embeddings",
                          "relation_embeddings", "chunk_questions")
        }


class QuestionEmbedder:
    configured = True

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("model down")
        return [[float(i + 1)] * DIM for i, _ in enumerate(texts)]


def check_vectors_of_another_model(src, src_backend, dst, tmp, monkeypatch) -> None:
    """Every workload bound to another model: no vector lands, the legs cover
    every kind of row, and ``chunk_questions`` are embedded again with this
    deployment's model before publish (a model that fails, or none: dropped,
    reported, their chunks' question marks cleared)."""
    notebook_id = seed_world(src, src_backend, "full")
    exported = tmp / "other-model-exported.snbk"
    export(src, notebook_id, exported)
    path = tmp / "other-model.snbk"
    asked = {q["chunk_id"] for q in archive_cases.archive_rows(exported)["chunk_questions"]}
    # The chunks whose questions were generated carry the question-index mark.
    rebuild(exported, path, rows=lambda table, rows: [
        {**r, "question_indexed_at": memory_cases.NOW} if r["id"] in asked else r for r in rows
    ] if table == "chunks" else rows)

    def other(workload):
        return archive_cases._Service("openai", f"another-{workload}")

    target_models(dst, monkeypatch, other)
    embedder = QuestionEmbedder()
    monkeypatch.setattr(service(dst), "_question_embedder", lambda: embedder)
    report = run_import(dst, path)
    counts = _vector_rows(dst, report.notebook_id)
    assert {k: v for k, v in counts.items() if k != "chunk_questions"} == {
        "chunk_embeddings": 0, "element_embeddings": 0, "knowledge_embeddings": 0,
        "relation_embeddings": 0,
    }
    assert counts["chunk_questions"] == 2 and len(embedder.calls) == 1
    assert report.chunk_questions == {
        "kept": 0, "reembedded": 2, "dropped": 0, "reason": "", "marks_cleared": 0,
    }
    assert report.reembed_legs == ("chunks", "elements", "kg_objects", "kg_relations")
    assert {w: v.reason for w, v in report.vectors.items()} == {
        "chunk_embedding": "model_differs", "source_element_embedding": "model_differs",
        "knowledge_object_embedding": "model_differs",
        "relation_embedding": "unbound_in_archive", "knowhow_embedding": "unbound_in_archive",
    }
    placeholder = "?" if dst.settings.database_url.startswith("sqlite") else "%s"
    with dst._runtime.database.connect() as db:
        vectors = [bytes(r["vector"]) for r in db.execute(
            f"SELECT vector FROM chunk_questions WHERE notebook_id = {placeholder}",
            (report.notebook_id,),
        ).fetchall()]
    assert all(len(v) == 4 * DIM for v in vectors)

    with dst._runtime.database.connect() as db:
        kept_marks = db.execute(
            f"SELECT COUNT(*) AS n FROM chunks WHERE notebook_id = {placeholder} "
            "AND question_indexed_at IS NOT NULL", (report.notebook_id,),
        ).fetchone()["n"]
    assert kept_marks == len(asked) == 2  # re-embedded questions keep their chunks' marks
    for failing, reason in ((QuestionEmbedder(fail=True), "embedding_failed:RuntimeError"),
                            (None, "not_configured")):
        monkeypatch.setattr(service(dst), "_question_embedder", lambda e=failing: e)
        dropped = run_import(dst, path)
        assert _vector_rows(dst, dropped.notebook_id)["chunk_questions"] == 0
        assert dropped.chunk_questions == {
            "kept": 0, "reembedded": 0, "dropped": 2, "reason": reason, "marks_cleared": 2,
        }
        with dst._runtime.database.connect() as db:
            marked = db.execute(
                f"SELECT COUNT(*) AS n FROM chunks WHERE notebook_id = {placeholder} "
                "AND question_indexed_at IS NOT NULL", (dropped.notebook_id,),
            ).fetchone()["n"]
        assert marked == 0


def _without_vectors(repo, notebook_id: str) -> dict[str, list[dict]]:
    """Knowledge objects (with their source's type, status and payload) and
    non-blank chunks of ``notebook_id`` that have no vector."""
    ph = placeholder(repo)
    with repo._runtime.database.connect() as db:
        objects = [dict(r) for r in db.execute(
            "SELECT o.id, o.status, o.payload, s.source_type FROM knowledge_objects o "
            "LEFT JOIN sources s ON s.id = o.source_id "
            f"WHERE o.notebook_id = {ph} AND NOT EXISTS "
            "(SELECT 1 FROM knowledge_embeddings e WHERE e.object_id = o.id)", (notebook_id,),
        ).fetchall()]
        chunks = [dict(r) for r in db.execute(
            f"SELECT c.id FROM chunks c WHERE c.notebook_id = {ph} AND c.text <> '' "
            "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id = c.id)",
            (notebook_id,),
        ).fetchall()]
    return {"knowledge_objects": objects, "chunks": chunks}


def _embeddable(row: dict) -> bool:
    """What the object leg embeds: not deprecated, with a name to embed."""
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    return row["status"] != "deprecated" and bool(str(payload.get("name") or "").strip())


def check_unprojected_knowhow_cells_end_with_chunk_vectors(src, src_backend, dst, tmp,
                                                           monkeypatch) -> None:
    """Knowhow cells that were never projected travel without a single chunk;
    with the Knowhow model absent here (not kept) the post-publish projection
    creates their chunks without vectors (``embed=False``). The report names the
    ``chunks`` leg all the same -- what is missing is read from storage, not from
    the archive's chunk count -- and once the legs ran no chunk lacks a vector."""
    from app.services.embedding import FakeEmbedder
    from app.services.notebook_reembed import LEG_CHUNKS, NotebookReembedService
    from tests.model_testkit import bind_embedding_client

    notebook_id = seed_world(src, src_backend, "full")
    good = tmp / "unprojected-good.snbk"
    export(src, notebook_id, good)
    tables = archive_cases.archive_rows(good)
    hidden = {r["id"] for r in tables["sources"] if r.get("source_type") == "knowhow"}
    knowhow_chunks = {r["id"] for r in tables["chunks"] if r["source_id"] in hidden}
    knowhow_elements = {r["id"] for r in tables["source_elements"] if r["source_id"] in hidden}
    assert hidden and knowhow_chunks and tables["knowhow_cells"]
    unprojected = {
        "chunks": lambda r: r["id"] not in knowhow_chunks,
        "chunk_embeddings": lambda r: r["chunk_id"] not in knowhow_chunks,
        "chunk_questions": lambda r: r["chunk_id"] not in knowhow_chunks,
        "source_elements": lambda r: r["id"] not in knowhow_elements,
        "element_embeddings": lambda r: r["element_id"] not in knowhow_elements,
    }
    path = tmp / "unprojected.snbk"
    rebuild(good, path, rows=lambda table, rows: [
        r for r in rows if unprojected[table](r)
    ] if table in unprojected else rows)
    target_models(dst, monkeypatch)
    report = run_import(dst, path)
    assert not report.vectors["knowhow_embedding"].kept
    assert report.vectors["chunk_embedding"].kept
    assert _without_vectors(dst, report.notebook_id)["chunks"], "projection embedded nothing"
    assert LEG_CHUNKS in report.reembed_legs, report.reembed_legs
    bind_embedding_client(dst, FakeEmbedder(dim=DIM), workload_ids=[
        "chunk_embedding", "source_element_embedding", "knowledge_object_embedding",
        "relation_embedding", "knowhow_embedding",
    ])
    status = NotebookReembedService(dst).run(report.notebook_id, legs=report.reembed_legs)
    left = _without_vectors(dst, report.notebook_id)
    assert left["chunks"] == [], (left, status)


def check_knowhow_objects_end_with_vectors(src, src_backend, dst, tmp, monkeypatch) -> None:
    """A Knowhow table's knowledge objects are rebuilt by the post-publish
    projection, and the rebuilt objects carry no vector -- so the report names the
    ``kg_objects`` leg even though the archive's object vectors were kept (same
    model here). Once the legs the report names have run, no knowledge object
    lacks a vector (but the two the world seeds as never embeddable: a blank
    name, a deprecated object), and no chunk."""
    from app.services.embedding import FakeEmbedder
    from app.services.notebook_reembed import LEG_KG_OBJECTS, NotebookReembedService
    from tests.model_testkit import bind_embedding_client

    notebook_id = seed_world(src, src_backend, "full")
    path = tmp / "knowhow-objects.snbk"
    export(src, notebook_id, path)
    target_models(dst, monkeypatch)
    report = run_import(dst, path)
    assert report.vectors["knowledge_object_embedding"].kept
    assert report.rows.get("knowhow_tables", 0) > 0
    after_projection = _without_vectors(dst, report.notebook_id)["knowledge_objects"]
    assert any(row["source_type"] == "knowhow" for row in after_projection), after_projection
    assert LEG_KG_OBJECTS in report.reembed_legs, report.reembed_legs
    bind_embedding_client(dst, FakeEmbedder(dim=DIM), workload_ids=[
        "chunk_embedding", "source_element_embedding", "knowledge_object_embedding",
        "relation_embedding", "knowhow_embedding",
    ])
    status = NotebookReembedService(dst).run(report.notebook_id, legs=report.reembed_legs)
    left = _without_vectors(dst, report.notebook_id)
    assert left["chunks"] == [], (left, status)
    assert [row for row in left["knowledge_objects"] if _embeddable(row)] == [], (left, status)
    assert len(left["knowledge_objects"]) == 2, left


def check_vectors_kept_byte_for_byte(src, src_backend, dst, tmp, monkeypatch) -> None:
    """Same models: every vector of the three bound workloads lands unchanged, the
    two unbound workloads (relations, Knowhow chunks) are dropped, and questions
    keep their stored vectors without a model call; a vector of the wrong length
    under a kept workload is dropped and its leg requested."""
    notebook_id = seed_world(src, src_backend, "full")
    path = tmp / "same-model.snbk"
    export(src, notebook_id, path)
    target_models(dst, monkeypatch)
    embedder = QuestionEmbedder()
    monkeypatch.setattr(service(dst), "_question_embedder", lambda: embedder)
    captured = capture_run(dst, monkeypatch)
    report = run_import(dst, path)
    assert embedder.calls == []
    assert report.chunk_questions["kept"] == 2
    translate = Translator(translation(captured["run"], report.notebook_id))
    original = archive_cases.archive_rows(path)
    placeholder = "?" if dst.settings.database_url.startswith("sqlite") else "%s"
    with dst._runtime.database.connect() as db:
        for table, key in (("element_embeddings", "element_id"),
                           ("knowledge_embeddings", "object_id")):
            stored = {
                r[key]: bytes(r["vector"]) for r in db.execute(
                    f"SELECT {key}, vector FROM {table} WHERE notebook_id = {placeholder}",
                    (report.notebook_id,),
                ).fetchall()
            }
            expected = {translate.ids[r[key]]: decode_value(r["vector"]) for r in original[table]}
            assert stored == expected and stored, table
    # Kept object vectors, but the Knowhow tables' objects are rebuilt without
    # one: the object leg is named all the same.
    assert report.reembed_legs == ("chunks", "kg_objects", "kg_relations")
    assert report.dropped == {
        "chunk_embeddings.knowhow_embedding": 4, "relation_embeddings.relation_embedding": 2,
    }

    # A wrong-length vector under a kept workload: dropped, its leg requested.
    broken = tmp / "short-vector.snbk"
    rebuild(path, broken, rows=lambda table, rows: [
        {**r, "vector": {"$bytes": "AAAA"}} if table == "element_embeddings" else r
        for r in rows
    ])
    short = run_import(dst, broken)
    assert short.dropped["element_embeddings.source_element_embedding"] == 3
    assert "elements" in short.reembed_legs


# --------------------------------------------------------------- tampering


def rebuild(
    src: Path,
    dst: Path,
    *,
    rows: "Callable[[str, list[dict]], list[dict]] | None" = None,
    files: "Callable[[dict[str, bytes]], dict[str, bytes]] | None" = None,
    manifest: "Callable[[dict], dict] | None" = None,
) -> None:
    """Write ``dst`` from ``src`` with its rows, files or manifest edited and every
    checksum, size, row count and column list recomputed, so the edit is all that
    differs: an integrity-correct archive that only the import's deeper checks can
    refuse."""
    tables = archive_cases.archive_rows(src)
    carried = archive_cases.archive_files(src)
    doc = archive_cases.archive_manifest(src)
    if files is not None:
        carried = files(dict(carried))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as out:
        for table in ARCHIVE_TABLES:
            table_rows = tables[table] if rows is None else rows(table, tables[table])
            digest = DigestCounter()
            columns: set[str] = set()
            body = b"".join((json_line(r) + "\n").encode("utf-8") for r in table_rows)
            digest.update(body)
            for r in table_rows:
                columns.update(r)
            out.writestr(rows_entry(table), body)
            doc["tables"][table] = {
                "columns": sorted(columns), "rows": len(table_rows),
                "bytes": digest.size, "sha256": digest.hexdigest(),
            }
        index = b""
        for path, data in carried.items():
            out.writestr(path, data)
            index += (json_line({
                "path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            }) + "\n").encode("utf-8")
        out.writestr(FILES_INDEX_NAME, index)
        doc["files"] = {
            "count": len(carried), "bytes": sum(len(b) for b in carried.values()),
            "index_bytes": len(index), "index_sha256": hashlib.sha256(index).hexdigest(),
        }
        if manifest is not None:
            doc = manifest(doc)
        out.writestr(MANIFEST_NAME, json_document(doc).encode("utf-8"))
    dst.write_bytes(buffer.getvalue())


def raw_zip(dst: Path, entries: list[tuple[Any, bytes]]) -> None:
    """A zip written entry by entry, exactly as given (a ``ZipInfo`` or a name)."""
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for info, data in entries:
            out.writestr(info, data)


def _good_entries(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        return [(name, archive.read(name)) for name in archive.namelist()]


def check_tampered_archives_are_refused(repo, backend: Backend, tmp: Path, monkeypatch) -> None:
    """Every refusal leaves the target exactly as it was (rows and both trees)."""
    notebook_id = seed_world(repo, backend, "full")
    good = tmp / "good.snbk"
    export(repo, notebook_id, good)
    cases: list[tuple[str, Path, str]] = []

    def case(name: str, match: str) -> Path:
        path = tmp / f"{name}.snbk"
        cases.append((name, path, match))
        return path

    entries = _good_entries(good)
    raw_zip(case("traversal", "不允许的条目名"),
            entries + [("files/notebooks/../../evil.txt", b"x")])
    link = zipfile.ZipInfo("files/notebooks/link.txt")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    raw_zip(case("symlink", "符号链接"), entries + [(link, b"/etc/passwd")])
    raw_zip(case("duplicate", "重复的条目名"), entries + [entries[0]])
    raw_zip(case("bomb", "压缩比"), entries + [("files/notebooks/zeros.bin", bytes(8 * 1024 * 1024))])
    raw_zip(case("unknown-table", "不允许的条目名"), entries + [("rows/users.jsonl", b"{}\n")])
    rebuild(good, case("extra-column", "导出端版本较新"),
            rows=lambda table, rows: [{**r, "evil": 1} for r in rows] if table == "sources" else rows)
    rebuild(good, case("unsafe-column", "列名不合法"),
            rows=lambda table, rows: [{**r, "id) VALUES (1); --": 1} for r in rows]
            if table == "sources" else rows)
    rebuild(good, case("ghost-endpoint", "文件内容不完整"),
            rows=lambda table, rows: [{**r, "target_object_id": "ko-ghost"} for r in rows]
            if table == "knowledge_relations" else rows)
    rebuild(good, case("ghost-source", r"knowledge_objects\.source_id"),
            rows=lambda table, rows: [{**r, "source_id": "src-ghost"} for r in rows]
            if table == "knowledge_objects" else rows)
    rebuild(good, case("ghost-hidden-source", r"knowhow_tables\.hidden_source_id"),
            rows=lambda table, rows: [{**r, "hidden_source_id": "src-ghost"} for r in rows]
        if table == "knowhow_tables" else rows)
    rebuild(good, case("memory-source", "私有记忆来源"),
            rows=lambda table, rows: [{**r, "source_type": "memory"} for r in rows]
            if table == "sources" else rows)
    rebuild(good, case("traversing-file-path", "不是安全的相对路径"),
            rows=lambda table, rows: [{**r, "file_path": "../../../etc/passwd"} if r.get("file_path")
                                      else r for r in rows] if table == "sources" else rows)
    rebuild(good, case("newer-format", "更新"),
            manifest=lambda doc: {**doc, "format_version": 99})
    rebuild(good, case("case-collision", "重名"),
            files=lambda f: {**f, "files/notebooks/DOC.md": b"other", "files/notebooks/doc.md": b"x"})
    rebuild(good, case("unicode-collision", "重名"),
            files=lambda f: {**f, "files/notebooks/café.txt": b"a",
                             "files/notebooks/café.txt": b"b"})
    rebuild(good, case("two-notebooks", "恰好包含一本笔记本"),
            rows=lambda table, rows: rows + rows if table == "notebooks" else rows)
    # Checksum no longer matching (an entry edited after the export).
    tampered = [(n, d.replace(b"web page", b"WEB PAGE") if n == "rows/sources.jsonl" else d)
                for n, d in entries]
    raw_zip(case("checksum", "校验和与清单声明不符|大小与清单声明不符"), tampered)
    for name, path, match in cases:
        assert_refused(repo, path, match), name

    # One line over the reader's line limit.
    monkeypatch.setattr(archive_reader, "MAX_JSONL_LINE_BYTES", 300)
    assert_refused(repo, good, "单行上限")


#: A string that stands for content, a database value or a private path inside
#: an exception message: it must never reach a log.
LOG_SENTINEL = "SENTINEL-cell-text /private/srv/notebooks/secret.md"


def _assert_logs_carry_no_message(records) -> None:
    for record in records:
        if record.name in ("silicon_notebook.notebook_import", "silicon_notebook.startup"):
            assert record.exc_info is None and not record.exc_text, record
        assert LOG_SENTINEL not in record.getMessage(), record.getMessage()


def check_failure_logs_carry_ids_and_class_only(repo, backend, tmp, monkeypatch, caplog) -> None:
    """A Knowhow projection that fails after publish, a compensation that fails,
    and a startup sweep that fails each log the notebook / table ids and the
    exception's class -- never its message nor a traceback (either can carry
    cell content, database values or private paths). The failure report is
    unchanged: the projection failure is still listed, the import's own error
    still raised."""
    import logging

    from app.services import startup_warmup

    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "logs.snbk"
    export(repo, notebook_id, path)
    svc = service(repo)

    def boom(*args, **kwargs):
        raise RuntimeError(LOG_SENTINEL)

    caplog.set_level(logging.DEBUG)
    with monkeypatch.context() as patched:
        patched.setattr(svc, "_project_table", boom)
        report = run_import(repo, path)
    assert report.projection_failures and set(report.projection_failures) == set(
        report.knowhow_tables
    )
    projection = [r for r in caplog.records if "投影失败" in r.getMessage()]
    assert projection and all("RuntimeError" in r.getMessage() for r in projection)

    class Refused(Exception):
        pass

    def refuse(*args, **kwargs):
        raise Refused("validate_import: sources 行数不一致 1!=2")

    store_type = type(repo._runtime.sharing_store)
    with monkeypatch.context() as patched:
        patched.setattr(store_type, "validate_import", refuse)
        patched.setattr(store_type, "compensate_import", boom)
        with pytest.raises(Refused):
            run_import(repo, path)
    compensation = [r for r in caplog.records if "补偿" in r.getMessage()]
    assert compensation and all("RuntimeError" in r.getMessage() for r in compensation)

    class Runtime:
        class notebook_import:
            sweep_stale_imports = staticmethod(boom)

    class Repo:
        _runtime = Runtime()

    startup_warmup._sweep_stale_notebook_imports(Repo())
    sweep = [r for r in caplog.records if "stale notebook import sweep failed" in r.getMessage()]
    assert sweep and all("RuntimeError" in r.getMessage() for r in sweep)
    _assert_logs_carry_no_message(caplog.records)
    assert LOG_SENTINEL not in caplog.text


def check_unservable_values_are_refused(repo, backend, tmp) -> None:
    """Values a serving path trusts because this deployment only ever persists
    them in one shape: an attachment whose MIME type is not one of the image
    types the asset routes serve inline (HTML, SVG -- with its ``.bin`` file
    carried), and a source link that is not an http(s) URL, each refuse the
    whole file in the preflight; nothing remains."""
    notebook_id = seed_world(repo, backend, "full")
    good = tmp / "served.snbk"
    export(repo, notebook_id, good)
    assets = archive_cases.archive_rows(good)["notebook_assets"]
    assert assets
    first = assets[0]["id"]
    for mime in ("text/html", "image/svg+xml", "IMAGE/PNG"):
        path = tmp / f"asset-{mime.replace('/', '-').replace('+', '-')}.snbk"
        rebuild(
            good, path,
            rows=lambda table, rows, mime=mime: [
                {**r, "mime": mime} if r["id"] == first else r for r in rows
            ] if table == "notebook_assets" else rows,
            files=lambda carried: {
                **carried, f"files/assets/{first}.bin": b"<script>alert(document.domain)</script>",
            },
        )
        assert_refused(repo, path, "MIME 类型不在允许的图片类型之内")
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://"):
        path = tmp / f"url-{len(url)}.snbk"
        rebuild(good, path, rows=lambda table, rows, url=url: [
            {**rows[0], "source_url": url}, *rows[1:]
        ] if table == "sources" else rows)
        assert_refused(repo, path, "来源链接不是 http")
    # The shapes this deployment does persist still import.
    fine = tmp / "served-fine.snbk"
    rebuild(good, fine, rows=lambda table, rows: [
        {**rows[0], "source_url": "HTTPS://example.org/paper.pdf"}, *rows[1:]
    ] if table == "sources" else rows)
    run_import(repo, fine)


def check_dangling_ids_inside_json_are_cleared(repo, backend, tmp) -> None:
    """An id inside stored JSON naming no row of the archive is cleared and counted;
    the import still succeeds and no cleared id names a row of the target."""
    notebook_id = seed_world(repo, backend, "full")
    good = tmp / "json.snbk"
    export(repo, notebook_id, good)
    path = tmp / "json-ghost.snbk"

    def ghost(table, rows):
        if table != "knowledge_objects":
            return rows
        edited = []
        for r in rows:
            payload = json.loads(r["payload"])
            payload["object_id"] = notebook_id  # an id that exists here, outside the archive
            payload["element_ids"] = ["el-ghost-1", "el-ghost-2"]
            edited.append({**r, "payload": json.dumps(payload)})
        return edited

    rebuild(good, path, rows=ghost)
    report = run_import(repo, path)
    objects = len(archive_cases.archive_rows(good)["knowledge_objects"])
    assert report.cleared["knowledge_objects.payload.object_id"] == objects
    assert report.cleared["knowledge_objects.payload.element_ids"] >= 2 * objects
    placeholder = "?" if repo.settings.database_url.startswith("sqlite") else "%s"
    with repo._runtime.database.connect() as db:
        payloads = [r["payload"] for r in db.execute(
            f"SELECT payload FROM knowledge_objects WHERE notebook_id = {placeholder}",
            (report.notebook_id,),
        ).fetchall()]
    for payload in payloads:
        value = payload if isinstance(payload, dict) else json.loads(payload)
        if "object_id" in value:
            assert value["object_id"] is None
        assert not any(str(e).startswith("el-ghost") for e in value.get("element_ids", []))


#: The reference columns of ``_IMPORT_DANGLING_SQL`` that name an object (the
#: rest name a source), and the key of each table whose key is not ``id``.
_OBJECT_REFERENCES = frozenset({
    "source_object_id", "target_object_id", "object_id", "global_object_id",
    "after_object_id", "member_object_id",
})
_ROW_KEYS = {"knowledge_embeddings": "object_id", "knowledge_source_fact_backfills": "source_id"}


def _point_outside(repo, notebook_id: str, table: str, column: str, value: str) -> Callable[[], None]:
    """Point one ``table`` row of ``notebook_id`` at ``value`` (a row of another
    notebook) through ``column``; returns what puts it back."""
    ph = placeholder(repo)
    key = _ROW_KEYS.get(table, "id")
    with repo._runtime.database.write() as db:
        if table == "knowledge_embeddings":  # its key is the object id: free it
            db.execute(f"DELETE FROM knowledge_embeddings WHERE object_id = {ph}", (value,))
        row = db.execute(
            f"SELECT {key} AS k, {column} AS v FROM {table} WHERE notebook_id = {ph} "
            f"ORDER BY {key} LIMIT 1", (notebook_id,),
        ).fetchone()
        assert row is not None, f"the world has no {table} row to point outside"
        db.execute(f"UPDATE {table} SET {column} = {ph} WHERE {key} = {ph}", (value, row["k"]))
    where = value if column == key else row["k"]

    def restore() -> None:
        with repo._runtime.database.write() as db:
            db.execute(f"UPDATE {table} SET {column} = {ph} WHERE {key} = {ph}", (row["v"], where))

    return restore


def check_dangling_references_are_caught(repo, backend, tmp, monkeypatch) -> None:
    """Each reference ``validate_import`` checks, pointed at a row of ANOTHER
    notebook (so no foreign key stops it), is reported under its own label; and
    ``validate_copy`` catches a relation whose source or target object is not
    the notebook's own. Behaviour, not shape: dropping a check or correlating it
    on the wrong column turns this red."""
    import sys

    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "closure.snbk"
    export(repo, notebook_id, path)
    target_models(repo, monkeypatch)
    imported = run_import(repo, path).notebook_id
    store = repo._runtime.sharing_store
    ph = placeholder(repo)
    with repo._runtime.database.connect() as db:
        outside = {
            kind: db.execute(
                f"SELECT id FROM {table} WHERE notebook_id = {ph} ORDER BY id LIMIT 1",
                (notebook_id,),
            ).fetchone()["id"]
            for kind, table in (("object", "knowledge_objects"), ("source", "sources"))
        }
    labels = [label for label, _query in sys.modules[type(store).__module__]._IMPORT_DANGLING_SQL]
    assert len(labels) == 11
    store.validate_import(imported, {})
    for label in labels:
        table, column = label.split(".")
        restore = _point_outside(
            repo, imported, table, column,
            outside["object" if column in _OBJECT_REFERENCES else "source"],
        )
        with pytest.raises(RuntimeError, match=re.escape(f"{label} 存在悬空引用")):
            store.validate_import(imported, {})
        restore()
        store.validate_import(imported, {})
    # validate_copy, inside a real copy: the copy's relation endpoint is pointed
    # outside just before the copy validates itself, and the copy fails.
    sharing = repo._runtime.sharing
    real_validate = type(store).validate_copy
    for column in ("source_object_id", "target_object_id"):
        def corrupt_then_validate(self, source_id, new_id, column=column):
            _point_outside(repo, new_id, "knowledge_relations", column, outside["object"])
            return real_validate(self, source_id, new_id)

        before = residue(repo)
        with monkeypatch.context() as patched:
            patched.setattr(type(store), "validate_copy", corrupt_then_validate)
            with pytest.raises(RuntimeError, match="关系存在悬空引用"):
                sharing.copy_notebook(imported, new_owner_id=IMPORTER)
        assert_no_residue(repo, before)
    sharing.copy_notebook(imported, new_owner_id=IMPORTER)  # untouched, the copy validates


# --------------------------------------------------------------- crash and sweep


def _run_delete_jobs_inline(repo, monkeypatch) -> None:
    runner = repo._runtime.notebook_delete
    monkeypatch.setattr(runner, "submit_job", lambda job_id, nb: runner.run(job_id) or True)


def check_crash_leaves_a_sentinel_the_sweep_reaps(repo, backend, tmp, monkeypatch) -> None:
    """A process that dies mid-import leaves its hidden sentinel (and files); once its
    heartbeat is older than ``NOTEBOOK_IMPORT_STALE_SECONDS`` the startup sweep hands
    it to the delete job, which leaves nothing. A sentinel with a fresh heartbeat and
    a stale sync mirror (``sync_origin`` set) are never reaped."""
    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "crash.snbk"
    export(repo, notebook_id, path)
    ensure_importer(repo)
    before = residue(repo)
    svc = service(repo)
    store = repo._runtime.sharing_store

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise Crash()

    with monkeypatch.context() as patched:
        patched.setattr(store, "validate_import", crash)
        patched.setattr(svc, "_compensate", lambda *a, **k: None)
        with pytest.raises(Crash):
            run_import(repo, path)
    placeholder = "?" if repo.settings.database_url.startswith("sqlite") else "%s"
    with repo._runtime.database.connect() as db:
        (sentinel,) = [dict(r) for r in db.execute(
            "SELECT id, status, sync_origin FROM notebooks WHERE status = 'importing'"
        ).fetchall()]
    assert sentinel["sync_origin"] == ""
    assert sentinel["id"] not in {nb.id for nb in repo.list_notebooks()}
    assert residue(repo) != before
    # A fresh heartbeat is not stale.
    assert svc.sweep_stale_imports() == 0
    stale = "2000-01-01T00:00:00+00:00"
    with repo._runtime.database.write() as db:
        db.execute(
            f"UPDATE notebooks SET updated_at = {placeholder} WHERE id = {placeholder}",
            (stale, sentinel["id"]),
        )
        backend.insert(db, "notebooks", {
            "id": "nb-sync-mirror", "name": "mirror", "purpose": "", "primary_domain": "",
            "status": "importing", "created_by": IMPORTER, "created_at": stale,
            "updated_at": stale, "sync_origin": "env-a",
        })
    _run_delete_jobs_inline(repo, monkeypatch)
    assert svc.sweep_stale_imports() == 1
    with repo._runtime.database.connect() as db:
        left = [r["id"] for r in db.execute(
            "SELECT id FROM notebooks WHERE status = 'importing'"
        ).fetchall()]
        db_rows = db.execute(
            f"SELECT COUNT(*) AS n FROM notebook_delete_jobs WHERE notebook_id = {placeholder}",
            (sentinel["id"],),
        ).fetchone()["n"]
    assert left == ["nb-sync-mirror"]
    assert db_rows == 0
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id = 'nb-sync-mirror'")
    assert_no_residue(repo, before)


def check_large_import_compensates_through_the_delete_job(repo, backend, tmp, monkeypatch) -> None:
    """Past the copy limits a failed import is not deleted in one transaction: its
    sentinel moves to ``deleting`` with a delete-job row in the same transaction and
    the batched delete job removes it; nothing remains either way."""
    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "large.snbk"
    export(repo, notebook_id, path)
    ensure_importer(repo)
    before = residue(repo)
    store = repo._runtime.sharing_store
    requested: list[str] = []
    real_request = repo._runtime.notebook_delete_jobs.request_delete_from_import

    def recording(nb, **kwargs):
        job = real_request(nb, **kwargs)
        with repo._runtime.database.connect() as db:
            placeholder = "?" if repo.settings.database_url.startswith("sqlite") else "%s"
            status = db.execute(
                f"SELECT status FROM notebooks WHERE id = {placeholder}", (nb,)
            ).fetchone()["status"]
        requested.append(status)
        return job

    def fail(*args, **kwargs):
        raise RuntimeError("validation failed")

    with monkeypatch.context() as patched:
        patched.setattr(repo.settings, "notebook_copy_max_rows", 1)
        patched.setattr(repo._runtime.notebook_delete_jobs, "request_delete_from_import", recording)
        patched.setattr(store, "validate_import", fail)
        compensate_small = []
        patched.setattr(store, "compensate_import", lambda nb: compensate_small.append(nb))
        with pytest.raises(RuntimeError, match="validation failed"):
            run_import(repo, path)
    assert requested == ["deleting"] and compensate_small == []
    assert_no_residue(repo, before)


# --------------------------------------------------------------- after publish


def check_post_publish_order(repo, backend, tmp, monkeypatch) -> None:
    """With Knowhow tables: every table is projected again before the import
    returns, the legs include ``kg_relations``, and after the re-embedding run every
    relation of the notebook -- the projection's new edges too -- has its vector."""
    from app.services.notebook_reembed import NotebookReembedService
    from tests.notebook_reembed_cases import bind_embedders

    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "order.snbk"
    export(repo, notebook_id, path)
    events: list[str] = []
    svc = service(repo)
    real_project = svc._project_table

    def project(table_id, embed):
        events.append(f"project:{table_id}")
        return real_project(table_id, embed)

    monkeypatch.setattr(svc, "_project_table", project)
    report = run_import(repo, path, progress=lambda event: events.append(event.stage))
    assert events.index("done") > max(i for i, e in enumerate(events) if e.startswith("project:"))
    assert {e for e in events if e.startswith("project:")} == {
        f"project:{t}" for t in report.knowhow_tables
    }
    assert {"kg_objects", "kg_relations"} <= set(report.reembed_legs)
    bind_embedders(repo)
    status = NotebookReembedService(repo).run(report.notebook_id, legs=report.reembed_legs)
    assert status.progress["kg_relations"].state == "done", status
    placeholder = "?" if repo.settings.database_url.startswith("sqlite") else "%s"
    with repo._runtime.database.connect() as db:
        knowhow_sources = [r["hidden_source_id"] for r in db.execute(
            f"SELECT hidden_source_id FROM knowhow_tables WHERE notebook_id = {placeholder}",
            (report.notebook_id,),
        ).fetchall()]
        projected = {r["id"] for r in db.execute(
            "SELECT id FROM knowledge_relations WHERE source_id IN ("
            + ",".join(placeholder for _ in knowhow_sources) + ")",
            tuple(knowhow_sources),
        ).fetchall()}
        vectors = {r["relation_id"] for r in db.execute(
            f"SELECT relation_id FROM relation_embeddings WHERE notebook_id = {placeholder}",
            (report.notebook_id,),
        ).fetchall()}
        genesis = [r["note"] for r in db.execute(
            "SELECT note FROM knowhow_changes WHERE table_id IN ("
            + ",".join(placeholder for _ in report.knowhow_tables) + ")",
            tuple(report.knowhow_tables),
        ).fetchall()]
    # The projection's own edges (rebuilt after publish, their vectors deleted with
    # the old ones) carry vectors again: the relation leg ran after the projection.
    assert projected and projected <= vectors, (projected, vectors)
    assert genesis and set(genesis) == {"随笔记本《Shared》导入而来"}


# --------------------------------------------------------------- dry run


def check_dry_run_writes_nothing(repo, backend, tmp) -> None:
    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "dry.snbk"
    export(repo, notebook_id, path)
    ensure_importer(repo)
    before = residue(repo)
    rehearsal = run_import(repo, path, dry_run=True)
    assert_no_residue(repo, before)
    assert rehearsal.dry_run and rehearsal.notebook_id == ""
    real = run_import(repo, path)
    assert rehearsal.rows == real.rows, (rehearsal.rows, real.rows)
    assert rehearsal.reembed_legs == real.reembed_legs
    assert rehearsal.cleared == real.cleared and rehearsal.dropped == real.dropped



# --------------------------------------------------------------- timestamps


def naive(rows: list[Row]) -> list[Row]:
    """The world as an old SQLite deployment stored it: timestamps without a zone,
    and ``""`` for "never reviewed" (``knowledge_objects.last_reviewed``)."""
    out = []
    for row in rows:
        values = {
            key: value[: -len("+00:00")]
            if isinstance(value, str) and key.endswith("_at") and value.endswith("+00:00")
            else value
            for key, value in row.values.items()
        }
        if row.table == "knowledge_objects":
            values["last_reviewed"] = ""
        out.append(Row(row.table, values))
    return out


def check_naive_sqlite_timestamps_land_as_utc(src, src_backend, dst, tmp) -> None:
    """A SQLite archive's naive timestamps travel as written and land in
    PostgreSQL as that wall time in UTC (the sync convention); its ``""`` "never"
    sentinel lands as NULL."""
    archive_cases.seed(src, src_backend, naive(full_world()), Path(src.storage_dir),
                       char_cases.FULL_FILES)
    path = tmp / "naive.snbk"
    export(src, NB, path)
    rows = archive_cases.archive_rows(path)
    assert {r["created_at"] for r in rows["sources"]} == {"2026-09-29T00:00:00"}
    assert {r["last_reviewed"] for r in rows["knowledge_objects"]} == {""}
    report = run_import(dst, path)
    with dst._runtime.database.connect() as db:
        stamps = {r["created_at"] for r in db.execute(
            "SELECT created_at FROM sources WHERE notebook_id = %s", (report.notebook_id,)
        ).fetchall()}
        reviewed = {r["last_reviewed"] for r in db.execute(
            "SELECT last_reviewed FROM knowledge_objects WHERE notebook_id = %s",
            (report.notebook_id,),
        ).fetchall()}
    assert stamps == {datetime(2026, 9, 29, tzinfo=timezone.utc)}
    assert reviewed == {None}


# --------------------------------------------------------------- the CLI


def _cli(argv: list[str], capsys) -> tuple[int, dict]:
    from app.core.config import get_settings
    from tests.cli_schema_guest_support import load_script

    cli = load_script("notebook_archive")
    get_settings.cache_clear()
    try:
        status = cli.main(argv)
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    return status, json.loads(out) if "--json" in argv else {}


def check_cli_import(repo, backend: Backend, tmp: Path, capsys, *, confirm: tuple[str, ...]) -> None:
    """``scripts/cli.sh notebook import``: a dry run writes nothing; an import prints
    a complete JSON report (the import, its re-embedding run, the scale build);
    every failure -- an unknown owner, a refused file, ``--name`` with two files --
    still prints the structured report, never a connection detail, and leaves
    nothing."""
    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "cli.snbk"
    export(repo, notebook_id, path)
    ensure_importer(repo)
    before = residue(repo)

    status, payload = _cli(["import", str(path), "--owner", IMPORTER, "--dry-run", "--json"], capsys)
    assert status == 0 and payload["failed"] == []
    (entry,) = payload["imported"]
    assert entry["import"]["dry_run"] is True and entry["reembed"]["state"] == "dry_run"
    assert_no_residue(repo, before)

    status, payload = _cli(["import", str(path), "--owner", "nobody-here", *confirm, "--json"],
                           capsys)
    assert status == 2 and payload["imported"] == []
    assert "nobody-here" in payload["failed"][0]["error"]
    other = tmp / "cli-other.snbk"
    other.write_bytes(path.read_bytes())
    status, payload = _cli(["import", str(path), str(other), "--owner", IMPORTER, "--name", "x",
                            *confirm, "--json"], capsys)
    assert status == 2 and "--name" in payload["failed"][0]["error"]
    broken = tmp / "cli-broken.snbk"
    broken.write_bytes(b"not a zip")
    status, payload = _cli(["import", str(broken), "--owner", IMPORTER, *confirm, "--json"], capsys)
    assert status == 1 and payload["failed"][0]["file"] == str(broken)
    assert repo.settings.database_url.split("@")[-1] not in json.dumps(payload)
    assert_no_residue(repo, before)

    status, payload = _cli(["import", str(path), "--owner", IMPORTER, "--name", "命令行导入",
                            *confirm, "--json"], capsys)
    assert status == 0 and payload["failed"] == []
    (entry,) = payload["imported"]
    report = entry["import"]
    assert report["name"] == "命令行导入" and report["owner_id"] == IMPORTER
    assert repo.get_notebook(report["notebook_id"]).name == "命令行导入"
    assert set(report) >= {
        "rows", "files", "mount_edges_dropped", "cleared", "dropped", "vectors", "reembed_legs",
        "chunk_questions", "global_object_types", "knowhow_tables",
    }
    # No embedding model is bound here: the legs run and report what they could not
    # do, and the scale index is not built over the missing vectors.
    assert entry["reembed"]["legs"] == report["reembed_legs"]
    assert entry["reembed"]["state"] == "failed"
    assert entry["scale"]["outcome"] == "skipped:reembed_not_done"
    status, payload = _cli(["import", str(path), "--owner", IMPORTER, "--reembed", "skip",
                            *confirm, "--json"], capsys)
    (entry,) = payload["imported"]
    assert status == 0 and entry["reembed"]["state"] == "skipped"
    assert entry["scale"]["outcome"] == "skipped:reembed_not_done"


def check_cli_reports_every_failure(repo, backend: Backend, tmp: Path, capsys, monkeypatch, *,
                                    confirm: tuple[str, ...]) -> None:
    """A refused file (JSON nested too deep) and an unexpected failure (a
    validation error) each end the run with the structured report -- the files
    imported before stay listed -- and leave nothing of the failed file."""
    notebook_id = seed_world(repo, backend, "full")
    good = tmp / "cli-good.snbk"
    export(repo, notebook_id, good)
    deep = tmp / "cli-deep.snbk"
    rebuild(good, deep, rows=lambda table, rows: [
        {**r, "payload": _nested(5000)} for r in rows
    ] if table == "knowledge_objects" else rows)
    ensure_importer(repo)
    status, payload = _cli(["import", str(good), str(deep), "--owner", IMPORTER,
                            "--reembed", "skip", *confirm, "--json"], capsys)
    assert status == 1
    assert [entry["file"] for entry in payload["imported"]] == [str(good)]
    assert payload["failed"][0]["file"] == str(deep)
    assert "嵌套过深" in payload["failed"][0]["error"]
    before = residue(repo)

    def explode(*args, **kwargs):
        raise RuntimeError("validate_import: sources 行数不一致 1!=2")

    with monkeypatch.context() as patched:
        patched.setattr(type(repo._runtime.sharing_store), "validate_import", explode)
        status, payload = _cli(["import", str(good), "--owner", IMPORTER, *confirm, "--json"],
                               capsys)
    assert status == 1 and payload["imported"] == []
    assert payload["failed"] == [{"file": str(good), "error": "导入失败: RuntimeError"}]
    assert_no_residue(repo, before)
    _check_cli_page_beat_failures(repo, good, capsys, monkeypatch, confirm=confirm)
    _check_cli_reembed_failure_names_the_class_only(repo, good, capsys, monkeypatch,
                                                    confirm=confirm)


def _beaten_while_paging() -> bool:
    """Whether the heartbeat being beaten was called from a page of
    ``ArchiveRowSource.batches`` (not from a file written or a validation)."""
    import sys

    frame = sys._getframe(2)
    while frame is not None:
        code = frame.f_code
        if code.co_name == "batches" and code.co_filename == archive_reader.__file__:
            return True
        frame = frame.f_back
    return False


def _check_cli_page_beat_failures(repo, good: Path, capsys, monkeypatch, *,
                                  confirm: tuple[str, ...]) -> None:
    """A heartbeat that fails while a table is being paged -- the sentinel was
    reaped, the database failed -- is reported as what it is, never as a damaged
    file; nothing of the import remains."""
    from app.services.notebook_transfer import import_ as import_module

    if repo.settings.database_url.startswith("sqlite"):
        import sqlite3

        database_error: type[Exception] = sqlite3.OperationalError
    else:
        import psycopg

        database_error = psycopg.OperationalError
    real_new_run = import_module.NotebookImportService._new_run

    def paged_run(self, *args, **kwargs):
        # One row per page (every page is a mid-table one) and every beat due.
        run = real_new_run(self, *args, **kwargs)
        run.chunk_size = 1
        self._clock = SteppingClock()
        return run

    store_type = type(repo._runtime.sharing_store)
    real_touch = store_type.touch_import

    def reaped(self, notebook_id):
        return False if _beaten_while_paging() else real_touch(self, notebook_id)

    def broken(self, notebook_id):
        if _beaten_while_paging():
            raise database_error("could not reach the database")
        return real_touch(self, notebook_id)

    before = residue(repo)
    for touch, expected in ((reaped, "被收割"), (broken, "数据库错误: OperationalError")):
        with monkeypatch.context() as patched:
            patched.setattr(import_module.NotebookImportService, "_new_run", paged_run)
            patched.setattr(store_type, "touch_import", touch)
            status, payload = _cli(["import", str(good), "--owner", IMPORTER, "--reembed", "skip",
                                    *confirm, "--json"], capsys)
        assert status == 1 and payload["imported"] == [], payload
        (failure,) = payload["failed"]
        assert expected in failure["error"] and "导出文件" not in failure["error"], failure
        assert_no_residue(repo, before)


def _check_cli_reembed_failure_names_the_class_only(repo, good: Path, capsys, monkeypatch, *,
                                                    confirm: tuple[str, ...]) -> None:
    """A re-embedding run that raises reports the exception's class only: a
    model service's message can carry its URL."""
    from app.services.notebook_reembed import NotebookReembedService

    def unreachable(*args, **kwargs):
        raise RuntimeError("POST http://model-gateway.internal:9000/v1/embeddings refused")

    with monkeypatch.context() as patched:
        patched.setattr(NotebookReembedService, "run", unreachable)
        status, payload = _cli(["import", str(good), "--owner", IMPORTER, *confirm, "--json"],
                               capsys)
    assert status == 0, payload
    (entry,) = payload["imported"]
    assert entry["reembed"]["state"] == "failed"
    assert entry["reembed"]["error"] == "重新嵌入失败: RuntimeError"
    assert "model-gateway" not in json.dumps(payload)


# --------------------------------------------------------------- cluster seeds


def check_cluster_seeds_are_reminted(repo, backend, tmp) -> None:
    """A canonical id minted from an object (``<prefix>~ko-<id>``) names the new
    object after the import (the next copy of the imported notebook still finds its
    seed); a seed the archive does not carry refuses the file."""
    notebook_id = seed_world(repo, backend, "full")
    good = tmp / "clusters.snbk"
    export(repo, notebook_id, good)
    seeded = tmp / "clusters-seeded.snbk"
    rebuild(good, seeded, rows=lambda table, rows: [
        {**r, "canonical_id": f"K-~{r['member_object_id']}"} for r in rows
    ] if table == "concept_clusters" else rows)
    members = {r["member_object_id"] for r in archive_cases.archive_rows(seeded)["concept_clusters"]}
    assert members and all(m.startswith("ko-") for m in members)
    captured: dict = {}
    real = service(repo)._new_run

    def recording(*args, **kwargs):
        captured["run"] = real(*args, **kwargs)
        return captured["run"]

    service(repo)._new_run = recording
    try:
        report = run_import(repo, seeded)
    finally:
        del service(repo)._new_run
    with repo._runtime.database.connect() as db:
        rows = [dict(r) for r in db.execute(
            f"SELECT canonical_id, member_object_id FROM concept_clusters "
            f"WHERE notebook_id = {placeholder(repo)}", (report.notebook_id,),
        ).fetchall()]
    object_map = captured["run"].object_map
    assert rows and {(r["canonical_id"], r["member_object_id"]) for r in rows} == {
        (f"K-~{object_map[m]}", object_map[m]) for m in members
    }
    ghost = tmp / "clusters-ghost.snbk"
    rebuild(good, ghost, rows=lambda table, rows: [
        {**r, "canonical_id": "KL-~ko-ghost"} for r in rows
    ] if table == "concept_clusters" else rows)
    assert_refused(repo, ghost, r"concept_clusters\.canonical_id")


# --------------------------------------------------------------- seed space


def _kg_state(repo, notebook_id: str) -> "tuple[int, int] | None":
    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT concept_seed_version, dirty FROM unified_kg_state "
            f"WHERE notebook_id = {placeholder(repo)}", (notebook_id,),
        ).fetchone()
    return None if row is None else (int(row["concept_seed_version"]), int(row["dirty"]))


def v1_space_world() -> list[Row]:
    """A clean notebook without Memory whose published merge (and so its clusters)
    is still in concept seed space 1."""
    return resized(memory_cases.world(memory_cases.NOTEBOOK_PLAIN, with_memory=False)) + [
        Row("unified_kg_state", {
            "notebook_id": memory_cases.NOTEBOOK_PLAIN, "dirty": 0, "kg_mutation_seq": 3,
            "concept_seed_version": 1, "updated_at": memory_cases.NOW,
        }),
    ]


def check_seed_space_travels(src, src_backend, dst, tmp) -> None:
    """The clusters' seed space travels: a source still publishing space-1 clusters
    gives an archive saying so, and the import records space 1 on the new
    notebook's state row (not dirty: a clean source stays clean), as a deep copy
    does. An archive without the key (written before it existed) starts dirty and
    says why; an unknown space is refused."""
    archive_cases.seed(src, src_backend, v1_space_world(), Path(src.storage_dir))
    path = tmp / "v1.snbk"
    export(src, memory_cases.NOTEBOOK_PLAIN, path)
    manifest = archive_cases.archive_manifest(path)
    assert manifest["kg"] == {
        "starts_dirty": False, "clusters_dropped": False, "concept_seed_version": 1,
    }
    assert archive_cases.archive_rows(path)["concept_clusters"]
    report = run_import(dst, path)
    assert _kg_state(dst, report.notebook_id) == (1, 0)
    assert report.kg_concept_seed_version == 1 and report.kg_notes == ()
    assert report.kg_starts_dirty is False

    old = tmp / "v1-without-key.snbk"
    rebuild(path, old, manifest=lambda doc: {
        **doc, "kg": {"starts_dirty": False, "clusters_dropped": False},
    })
    unknown = run_import(dst, old)
    assert unknown.kg_notes == ("seed_space_unknown",) and unknown.kg_starts_dirty
    state = _kg_state(dst, unknown.notebook_id)
    assert state is not None and state[1] == 1
    forged = tmp / "v1-forged.snbk"
    rebuild(path, forged, manifest=lambda doc: {
        **doc, "kg": {**doc["kg"], "concept_seed_version": 7},
    })
    assert_refused(dst, forged, "概念种子空间")


# --------------------------------------------------------------- sentinel first


class SteppingClock:
    """Monotonic seconds that jump a full hour per reading: every heartbeat the
    import considers is due."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 3600.0
        return self.now


def check_heartbeat_during_file_extraction(repo, backend, tmp, monkeypatch) -> None:
    """The sentinel is committed before the first file is written, so a heartbeat
    due while files are still being extracted finds its row; the import succeeds."""
    from app.services.notebook_transfer import import_ as import_module

    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "slow-files.snbk"
    export(repo, notebook_id, path)
    events: list[str] = []
    store = repo._runtime.sharing_store
    real_touch, real_write = store.touch_import, import_module._write_entry

    def touch(nb):
        events.append("touch")
        return real_touch(nb)

    def write(*args, **kwargs):
        events.append("file")
        return real_write(*args, **kwargs)

    real_insert = store.insert_copy_rows

    def insert(*args, **kwargs):
        events.append("insert")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(service(repo), "_clock", SteppingClock())
    monkeypatch.setattr(store, "touch_import", touch)
    monkeypatch.setattr(store, "insert_copy_rows", insert)
    monkeypatch.setattr(import_module, "_write_entry", write)
    report = run_import(repo, path)
    assert report.files == 5
    first_file = events.index("file")
    assert "touch" in events[first_file:events.index("file", first_file + 1)]
    # Every page read beats too -- also the writer's read-only pre-passes (the
    # object and fact pre-scans), which insert nothing: far more beats than the
    # inserts, files and validation alone would give.
    assert events.count("touch") >= events.count("insert") + events.count("file") + 6


def check_crash_while_extracting_is_reaped(repo, backend, tmp, monkeypatch) -> None:
    """A process that dies while it writes the notebook's files leaves a sentinel
    whose tree the stale-import sweep removes with it: nothing remains."""
    from app.services.notebook_transfer import import_ as import_module

    notebook_id = seed_world(repo, backend, "full")
    path = tmp / "crash-files.snbk"
    export(repo, notebook_id, path)
    ensure_importer(repo)
    before = residue(repo)

    class Crash(BaseException):
        pass

    real_write = import_module._write_entry
    written: list[int] = []

    def crash_on_second(*args, **kwargs):
        written.append(1)
        if len(written) == 2:
            raise Crash()
        return real_write(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(import_module, "_write_entry", crash_on_second)
        patched.setattr(service(repo), "_compensate", lambda *a, **k: None)
        with pytest.raises(Crash):
            run_import(repo, path)
    with repo._runtime.database.connect() as db:
        (sentinel,) = [r["id"] for r in db.execute(
            "SELECT id FROM notebooks WHERE status = 'importing'"
        ).fetchall()]
    assert (Path(repo.storage_dir) / "notebooks" / sentinel).is_dir()
    with repo._runtime.database.write() as db:
        db.execute(
            f"UPDATE notebooks SET updated_at = {placeholder(repo)} WHERE id = {placeholder(repo)}",
            ("2000-01-01T00:00:00+00:00", sentinel),
        )
    _run_delete_jobs_inline(repo, monkeypatch)
    assert service(repo).sweep_stale_imports() == 1
    assert_no_residue(repo, before)


# --------------------------------------------------------------- hostile content


def _nested(depth: int, **extra: str) -> str:
    """A payload ``depth + 1`` levels deep (the object plus ``depth`` arrays)."""
    fields = "".join(f"{json.dumps(k)}: {json.dumps(v)}, " for k, v in extra.items())
    return '{"name": "deep", ' + fields + '"x": ' + "[" * depth + "]" * depth + "}"


def _with_payload(path: Path, out: Path, payload: Any) -> Path:
    rebuild(path, out, rows=lambda table, rows: [
        {**r, "payload": payload} for r in rows
    ] if table == "knowledge_objects" else rows)
    return out


def check_hostile_content_is_refused(repo, backend, tmp, monkeypatch) -> None:
    """JSON nested deeper than the import accepts refuses the file before any
    later parser sees it (exactly ``MAX_JSON_DEPTH`` levels still import, also
    with brackets and escaped quotes inside a string); a JSON column holding an
    object instead of text refuses it too (it would reach the driver unchecked);
    an archive changed on disk after the preflight fails while it is read; every
    refusal leaves nothing."""
    from app.services.notebook_transfer.import_ import MAX_JSON_DEPTH

    notebook_id = seed_world(repo, backend, "full")
    good = tmp / "hostile.snbk"
    export(repo, notebook_id, good)
    assert_refused(repo, _with_payload(good, tmp / "deep-json.snbk", _nested(5000)), "嵌套过深")
    assert_refused(repo, _with_payload(
        good, tmp / "one-too-deep.snbk", _nested(MAX_JSON_DEPTH)), "嵌套过深")
    text = '[\\"{' * 100  # brackets and escaped quotes, all inside one string
    at_limit = _with_payload(good, tmp / "at-limit.snbk", _nested(MAX_JSON_DEPTH - 1, note=text))
    imported = run_import(repo, at_limit).notebook_id
    with repo._runtime.database.connect() as db:
        payloads = [r["payload"] for r in db.execute(
            f"SELECT payload FROM knowledge_objects WHERE notebook_id = {placeholder(repo)}",
            (imported,),
        ).fetchall()]
    notes = [(p if isinstance(p, dict) else json.loads(p)).get("note") for p in payloads]
    assert text in notes, notes
    # An object where the exporter writes text, shallow or deep: refused, and on
    # PostgreSQL nothing of it lands (no ``jsonb`` built from it).
    for depth in (1, 300):
        value: Any = {"name": "deep"}
        for _ in range(depth - 1):
            value = {"x": value}
        assert_refused(repo, _with_payload(good, tmp / f"object-{depth}.snbk", value),
                       "不是 JSON 文本")

    # The bytes read after the preflight differ from the ones it verified (the
    # file was rewritten in place): every later pass re-hashes, so the import
    # fails rather than writing what was never checked. Simulated at the zip
    # layer -- the process's read buffer may still hold verified bytes for a
    # region rewritten on disk.
    real = service(repo)._preflight

    def preflight_then_change(path, archive):
        checked = real(path, archive)
        real_open = archive.open

        def changed_open(name, *args, **kwargs):
            if getattr(name, "filename", name) != "rows/knowledge_objects.jsonl":
                return real_open(name, *args, **kwargs)
            with real_open(name) as handle:
                data = handle.read()
            return io.BytesIO(data.replace(b"shared", b"SHARED", 1))

        archive.open = changed_open
        return checked

    monkeypatch.setattr(service(repo), "_preflight", preflight_then_change)
    assert_refused(repo, good, "自预检后已被改动")


# --------------------------------------------------------------- object types


def check_object_types(src, src_backend, dst, tmp) -> None:
    """The source's effective definition wins where this deployment has none: a
    deployment-wide type it lacks becomes the notebook's own (replacing a local
    ``proposed`` row of that type); a type it has keeps its definition; an active
    local row shadows and is kept; a type that was only proposed is not created."""
    from app.models.knowledge import ObjectSchemaCreate

    notebook_id = seed_world(src, src_backend, "full")
    good = tmp / "types.snbk"
    export(src, notebook_id, good)
    dst.create_object_schema(ObjectSchemaCreate(
        object_type="widget", plural="target widgets", fields=["name"], primary="name",
    ))

    def global_type(object_type: str, status: str = "active") -> dict:
        return {
            "object_type": object_type, "plural": f"{object_type}s", "fields": ["name", "size"],
            "primary_field": "name", "description": f"{object_type} from the source",
            "label": object_type.title(), "list_fields": ["size"], "status": status,
        }

    def local_proposal(rows):
        template = dict(rows[0])
        return rows + [{
            **template, "object_type": "probe", "status": "proposed", "plural": "draft probes",
            "source": "induced", "fields": json.dumps([{"name": "name", "type": "text"}]),
        }]

    path = tmp / "types-edited.snbk"
    rebuild(good, path, rows=lambda table, rows: local_proposal(rows)
            if table == "notebook_object_schemas" else rows,
            manifest=lambda doc: {**doc, "global_object_types": [
                global_type("gadget"), global_type("widget"), global_type("device"),
                global_type("probe"), global_type("draft", "proposed"),
            ]})
    report = run_import(dst, path)
    assert report.global_object_types == {
        "added": ("gadget", "probe"), "existing": ("widget",), "local": ("device",),
        "proposals_superseded": ("probe",), "skipped_proposed": ("draft",),
    }
    with dst._runtime.database.connect() as db:
        rows = {r["object_type"]: dict(r) for r in db.execute(
            "SELECT * FROM notebook_object_schemas "
            f"WHERE notebook_id = {placeholder(dst)}", (report.notebook_id,),
        ).fetchall()}
    assert set(rows) == {"device", "gadget", "probe"}

    def as_list(value):
        return json.loads(value) if isinstance(value, str) else value

    for object_type in ("gadget", "probe"):
        row = rows[object_type]
        assert row["source"] == "custom" and row["status"] == "active", row
        assert as_list(row["fields"]) == ["name", "size"]
        assert as_list(row["list_fields"]) == ["size"]
        assert row["plural"] == f"{object_type}s" and row["created_by"] == IMPORTER
    assert rows["device"]["status"] == "active" and rows["device"]["source"] != "induced"
    widget = next(s for s in dst.list_object_schemas() if s.object_type == "widget")
    assert widget.plural == "target widgets"
    # A type listed twice is a hand-edited manifest: refused in the preflight
    # (it would otherwise write two schema rows of one type).
    twice = tmp / "types-twice.snbk"
    rebuild(good, twice, manifest=lambda doc: {**doc, "global_object_types": [
        global_type("gadget"), global_type("gadget", "disabled"),
    ]})
    assert_refused(dst, twice, "重复列出了类型")


# --------------------------------------------------------------- rows rewritten


def check_unfinished_sources_and_orphan_assets(src, src_backend, dst, tmp) -> None:
    """A source still being parsed when exported arrives failed (counted); an
    attachment whose source the archive lacks is dropped with its file (counted)."""
    notebook_id = seed_world(src, src_backend, "full")
    good = tmp / "rows.snbk"
    export(src, notebook_id, good)
    original = archive_cases.archive_rows(good)
    parsing = next(r["id"] for r in original["sources"] if r["source_type"] != "knowhow")
    template = original["notebook_assets"][0]

    path = tmp / "rows-edited.snbk"
    rebuild(good, path, rows=lambda table, rows: (
        [{**r, "parse_status": "parsing", "status": "parsing"} if r["id"] == parsing else r
         for r in rows] if table == "sources"
        else rows + [{**template, "id": "asset-orphan", "source_id": "src-ghost",
                      "filename": "orphan.png"}] if table == "notebook_assets"
        else rows
    ), files=lambda files: {**files, "files/assets/asset-orphan.png": char_cases.PNG_A})
    captured: dict = {}
    real = service(dst)._new_run

    def recording(*args, **kwargs):
        captured["run"] = real(*args, **kwargs)
        return captured["run"]

    service(dst)._new_run = recording
    try:
        report = run_import(dst, path)
    finally:
        del service(dst)._new_run
    assert report.sources_reset == 1
    assert report.dropped["notebook_assets.source_id"] == 1
    new_source = captured["run"].source_map[parsing]
    with dst._runtime.database.connect() as db:
        source = dict(db.execute(
            f"SELECT status, parse_status, error_message FROM sources WHERE id = {placeholder(dst)}",
            (new_source,),
        ).fetchone())
        assets = db.execute(
            f"SELECT COUNT(*) AS n FROM notebook_assets WHERE notebook_id = {placeholder(dst)}",
            (report.notebook_id,),
        ).fetchone()["n"]
    assert source["status"] == "failed" and source["parse_status"] == "failed"
    assert source["error_message"]
    tree = Path(dst.storage_dir) / "assets" / report.notebook_id
    assert assets == len(original["notebook_assets"])
    assert len(list(tree.iterdir())) == assets
    assert "asset-orphan" not in captured["run"].asset_map
