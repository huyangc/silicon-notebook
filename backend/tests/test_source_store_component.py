# backend/tests/test_source_store_component.py
"""Task 10: SourceStore component — sources/source_elements row persistence and
the C5 batched SourceSummary hydration extracted off the facade.

Row-level semantics under test (must stay identical to the former facade
bodies):
- insert_source can join a caller transaction (batch import atomicity);
- set_status updates status+parse_status(+optional summary)+error_message;
- replace_elements swaps a source's elements INSIDE the caller transaction;
- source_elements keeps the created_at ASC, id ASC order and KeyError guard;
- list_sources / list_sources_page hydrate extraction warnings & KG flags.
"""
import pytest

from app.core.config import Settings
from app.repositories.sqlite.source_store import SourceElementWrite, SourceStore
from app.services import sqlite_repository


NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return sqlite_repository.SQLiteRepository(Settings())


@pytest.fixture
def store(repo):
    return repo._runtime.source_store


@pytest.fixture
def notebook_id(repo):
    from app.models.schemas import NotebookCreate

    return repo.create_notebook(NotebookCreate(name="nb")).id


def _insert(store, notebook_id, source_id, **overrides):
    kwargs = dict(
        source_id=source_id,
        notebook_id=notebook_id,
        title=f"Doc {source_id}",
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name=f"{source_id}.md",
        file_path=f"/tmp/{source_id}.md",
        file_size=0,
        file_hash="",
        summary="",
        doc_type="",
    )
    kwargs.update(overrides)
    store.insert_source(**kwargs)


def test_source_store_owns_row_level_sql():
    expected = {
        "list_sources",
        "list_sources_page",
        "get_source",
        "source_elements",
        "insert_source",
        "set_status",
        "clear_chunked_at",
        "replace_elements",
        "delete_source_row",
        "source_from_row",
        "sources_from_rows",
        "extraction_warning",
    }
    assert expected <= SourceStore.__dict__.keys()
    assert "__getattr__" not in SourceStore.__dict__


def test_source_store_shares_runtime_database(repo):
    assert repo._runtime.source_store.database is repo._runtime.database


def test_insert_source_and_get_source_roundtrip(store, notebook_id):
    _insert(store, notebook_id, "src-round", doc_type="textbook")
    detail = store.get_source("src-round")
    assert detail.id == "src-round"
    assert detail.notebook_id == notebook_id
    assert detail.type == "markdown"
    assert detail.status == "queued"
    assert detail.parse_status == "queued"
    assert detail.doc_type == "textbook"
    assert detail.source_url == ""          # kwarg default == DDL default
    assert detail.error_message == ""
    assert detail.file_path == "/tmp/src-round.md"
    with pytest.raises(KeyError):
        store.get_source("src-missing")


def test_insert_source_joins_caller_transaction(repo, store, notebook_id):
    """Batch import atomicity: rows inserted through the caller's connection
    roll back with it (import_sources keeps its all-or-nothing semantics)."""
    with pytest.raises(RuntimeError, match="abort"):
        with repo._write() as db:
            _insert(store, notebook_id, "src-tx-a", connection=db)
            _insert(store, notebook_id, "src-tx-b", connection=db)
            raise RuntimeError("abort")
    with pytest.raises(KeyError):
        store.get_source("src-tx-a")
    with pytest.raises(KeyError):
        store.get_source("src-tx-b")


def test_set_status_updates_fields_and_optional_summary(store, notebook_id):
    _insert(store, notebook_id, "src-status")
    store.set_status("src-status", "parsing")
    detail = store.get_source("src-status")
    assert detail.status == "parsing" and detail.parse_status == "parsing"
    assert detail.summary == ""             # untouched without summary kwarg

    store.set_status("src-status", "failed", summary="s", error_message="boom")
    detail = store.get_source("src-status")
    assert detail.status == "failed"
    assert detail.summary == "s"
    assert detail.error_message == "boom"


def test_replace_elements_swaps_rows_in_caller_transaction(repo, store, notebook_id):
    _insert(store, notebook_id, "src-el")
    old = [
        SourceElementWrite("el-src-el-0001", "paragraph", "p1", "old one", {}),
        SourceElementWrite("el-src-el-0002", "paragraph", "p2", "old two", {}),
    ]
    with repo._write() as db:
        store.replace_elements(db, "src-el", old, created_at=NOW)
    assert [e.text for e in store.source_elements("src-el")] == ["old one", "old two"]

    # Failure inside the caller transaction keeps the previous elements.
    new = [SourceElementWrite("el-src-el-0001", "paragraph", "p1", "new", {"k": 1})]
    with pytest.raises(RuntimeError, match="abort"):
        with repo._write() as db:
            store.replace_elements(db, "src-el", new, created_at=NOW)
            raise RuntimeError("abort")
    assert [e.text for e in store.source_elements("src-el")] == ["old one", "old two"]

    with repo._write() as db:
        store.replace_elements(db, "src-el", new, created_at=NOW)
    elements = store.source_elements("src-el")
    assert [e.text for e in elements] == ["new"]
    assert elements[0].metadata == {"k": 1}


def test_source_elements_orders_and_guards_missing(store, notebook_id, repo):
    _insert(store, notebook_id, "src-ord")
    rows = [
        SourceElementWrite("el-src-ord-0002", "paragraph", "p2", "two", {}),
        SourceElementWrite("el-src-ord-0001", "paragraph", "p1", "one", {}),
    ]
    with repo._write() as db:
        store.replace_elements(db, "src-ord", rows, created_at=NOW)
    assert [e.id for e in store.source_elements("src-ord")] == [
        "el-src-ord-0001",
        "el-src-ord-0002",
    ]  # created_at ASC, id ASC
    with pytest.raises(KeyError):
        store.source_elements("src-nope")


def test_list_sources_hydrates_warning_counts_and_kg_flag(repo, store, notebook_id):
    _insert(store, notebook_id, "src-hyd")
    with repo._write() as db:
        store.replace_elements(
            db,
            "src-hyd",
            [SourceElementWrite("el-src-hyd-0001", "paragraph", "p1", "t", {})],
            created_at=NOW,
        )
        db.execute(
            "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,"
            "error_message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("run-1", notebook_id, "src-hyd", "kg", "completed",
             "windows_failed=1/3", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("ko-1", notebook_id, "concept", '{"name":"x"}', "[]", "src-hyd", NOW, NOW),
        )
    summaries = store.list_sources(notebook_id)
    assert [s.id for s in summaries] == ["src-hyd"]
    src = summaries[0]
    assert src.element_count == 1
    assert src.kg_extracted is True
    assert src.extraction_warning and "1/3" in src.extraction_warning
    with store.database.connect() as db:
        assert store.extraction_warning(db, "src-hyd") == src.extraction_warning


def test_list_sources_page_filters_and_paginates(store, notebook_id):
    for i in range(5):
        _insert(store, notebook_id, f"src-pg-{i}", title=f"Alpha {i}")
    _insert(store, notebook_id, "src-pg-b", title="Beta", file_name="beta.md")
    page = store.list_sources_page(notebook_id, offset=0, limit=3)
    assert page.total_count == 6 and len(page.items) == 3
    assert page.offset == 0 and page.limit == 3
    hit = store.list_sources_page(notebook_id, q="BETA")
    assert hit.total_count == 1 and hit.items[0].id == "src-pg-b"
    by_file = store.list_sources_page(notebook_id, q="beta.md")
    assert by_file.total_count == 1


def _read_chunked_at(store, source_id):
    with store.database.connect() as db:
        return db.execute(
            "SELECT chunked_at FROM sources WHERE id=?", (source_id,)
        ).fetchone()["chunked_at"]


def _stamp_chunked_at(repo, source_id, ts=NOW):
    """置 chunked_at 建 setup。生产侧 chunked_at 只由 replace_source_chunks 的
    mark_chunked_at 参数随 chunk 提交原子置位(见 chunk_store),store 层没有独立的置
    方法,故测试 setup 直接写这一列。"""
    with repo._write() as db:
        db.execute("UPDATE sources SET chunked_at=? WHERE id=?", (ts, source_id))


def test_clear_chunked_at_zeroes_inside_caller_transaction(repo, store, notebook_id):
    _insert(store, notebook_id, "src-clr", status="extracted", parse_status="extracted")
    _stamp_chunked_at(repo, "src-clr")
    with repo._write() as db:
        store.clear_chunked_at(db, "src-clr")
    assert _read_chunked_at(store, "src-clr") is None
    # Rides the caller transaction: a rollback keeps the prior mark (the process
    # pipeline zeroes it in the SAME write() as replace_elements — no crash window).
    _stamp_chunked_at(repo, "src-clr")
    with pytest.raises(RuntimeError, match="abort"):
        with repo._write() as db:
            store.clear_chunked_at(db, "src-clr")
            raise RuntimeError("abort")
    assert _read_chunked_at(store, "src-clr") == NOW


def test_delete_source_row_removes_the_row(repo, store, notebook_id):
    _insert(store, notebook_id, "src-del")
    with repo._write() as db:
        store.delete_source_row(db, "src-del")
    with pytest.raises(KeyError):
        store.get_source("src-del")
