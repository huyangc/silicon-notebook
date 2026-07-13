import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_node_context_reads_payload_steps(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure", {
        "name": "Foundation Flow", "section_path": "1 > Flow",
        "steps": [{"name": "import", "element_id": "E0", "quote": "import"},
                  {"name": "floorplan", "element_id": "E1", "quote": "floorplan"}]})
    ctx = repo.node_context(nb.id, pid)
    assert [s["name"] for s in ctx["steps"]] == ["import", "floorplan"]


def test_node_context_legacy_procedure_fallback(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure",
                                   {"name": "old step", "section_path": "1 > X"})  # no steps[]
    ctx = repo.node_context(nb.id, pid)
    assert isinstance(ctx["steps"], list)
    assert any(s["name"] == "old step" for s in ctx["steps"])


def test_node_context_legacy_fallback_only_returns_same_section_siblings(repo):
    """P2-3: the legacy fallback groups sibling procedure nodes sharing the
    target's exact section_path. A procedure in a DIFFERENT section must never
    appear, whether the bound is applied in SQL (section known) or in Python
    (LIMIT fallback) — output for the common case is unchanged."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure",
                                   {"name": "step A", "section_path": "1 > X"})
    repo._test_insert_object(nb.id, "procedure",
                             {"name": "step B", "section_path": "1 > X"})   # same section
    repo._test_insert_object(nb.id, "procedure",
                             {"name": "step C", "section_path": "2 > Y"})   # different section
    ctx = repo.node_context(nb.id, pid)
    names = {s["name"] for s in ctx["steps"]}
    assert names == {"step A", "step B"}
    assert "step C" not in names


def test_node_context_legacy_fallback_query_is_bound_by_section_path(repo):
    """When the target node has a non-empty section_path, the SQL query itself
    filters on it (not just a Python post-filter over every procedure row in
    the notebook) — verified by spying on the executed SQL text via sqlite3's
    per-connection trace callback. Connections are now reused per-thread
    (SqliteDatabase.connect() caches one connection per thread), so
    node_context()'s internal _connect() call returns the SAME connection
    object already live on this (the test's) thread — attach the trace
    callback directly to that connection instead of trying to intercept a
    fresh sqlite3.connect() call that reuse means will never happen."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure",
                                   {"name": "old step", "section_path": "1 > X"})

    seen_sql = []
    conn = repo._connect()  # same thread-local connection node_context() will use
    conn.set_trace_callback(lambda sql: seen_sql.append(sql))
    try:
        repo.node_context(nb.id, pid)
    finally:
        conn.set_trace_callback(None)

    fallback_queries = [
        s for s in seen_sql
        if "object_type='procedure'" in s or 'object_type=\'procedure\'' in s
    ]
    assert fallback_queries, "expected the legacy procedure fallback query to run"
    assert any("section_path" in q for q in fallback_queries), (
        "legacy fallback query must filter by section_path in SQL when the "
        "target node's own section_path is known"
    )
