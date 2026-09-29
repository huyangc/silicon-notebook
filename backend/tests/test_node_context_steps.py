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


def _set_evidence(repo, object_id, source_ids):
    import json
    evidence = [{"source_id": sid, "source_title": sid, "element_id": "",
                 "element_type": "paragraph", "location_label": "p",
                 "quoted_span": "q", "confidence": 1.0} for sid in source_ids]
    with repo._connect() as db:
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?",
                   (json.dumps(evidence), object_id))
        # 反向索引与 evidence 同步维护(生产写路径的 replace_object_sources)。
        notebook_id = db.execute("SELECT notebook_id FROM knowledge_objects WHERE id=?",
                                 (object_id,)).fetchone()["notebook_id"]
        repo._runtime.knowledge.replace_object_sources(
            db, object_id, notebook_id, json.dumps(evidence))


@pytest.mark.parametrize("section", ["1 > X", ""])
def test_legacy_fallback_under_a_source_ceiling_keeps_only_supported_siblings(repo, section):
    """PR-A·A1:兄弟过程只留至少有一条天花板内证据来源的;无证据的兄弟归因不到
    来源,同样丢掉。section 为空时走 LIMIT 500 那条路,闸在 SQL 里、LIMIT 之前。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure", {"name": "in", "section_path": section})
    out = repo._test_insert_object(nb.id, "procedure", {"name": "out", "section_path": section})
    repo._test_insert_object(nb.id, "procedure", {"name": "bare", "section_path": section})
    _set_evidence(repo, pid, ["s-in"])
    _set_evidence(repo, out, ["s-out"])
    assert {s["name"] for s in repo.node_context(nb.id, pid)["steps"]} == {"in", "out", "bare"}
    assert [s["name"] for s in repo.node_context(
        nb.id, pid, allowed_source_ids=["s-in"])["steps"]] == ["in"]
    assert repo.node_context(nb.id, pid, allowed_source_ids=[])["steps"] == []


def test_legacy_fallback_ceiling_gate_sits_before_the_limit(repo):
    """section 为空的那条路有 ``LIMIT 500``:闸若落在 LIMIT 之后(先取 500 行再在
    Python 里滤),先插入的 501 个天花板外兄弟会占满名额、把天花板内的那个挤掉。"""
    import json
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    out_ev = json.dumps([{"source_id": "s-out", "element_id": "", "quoted_span": "q"}])
    now = "2026-01-01T00:00:00"
    with repo._connect() as db:
        for i in range(501):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,'procedure',?,?,'s-out',?,?)",
                       (f"ko-crowd-{i:03d}", nb.id, json.dumps({"name": f"crowd {i}"}), out_ev, now, now))
            db.execute("INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) VALUES (?,?,?)",
                       (f"ko-crowd-{i:03d}", "s-out", nb.id))
    pid = repo._test_insert_object(nb.id, "procedure", {"name": "in"})
    _set_evidence(repo, pid, ["s-in"])
    assert [s["name"] for s in repo.node_context(
        nb.id, pid, allowed_source_ids=["s-in"])["steps"]] == ["in"]


def test_payload_steps_under_a_source_ceiling_keep_names_but_not_unattributable_text(repo):
    """payload steps 是对象自己的内容:步骤名保留;元素不存在时 quote 归因不到
    来源,天花板下置空(缺省时仍回落到 quote,逐值不变)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    pid = repo._test_insert_object(nb.id, "procedure", {
        "name": "Flow", "section_path": "1 > Flow",
        "steps": [{"name": "import", "element_id": "E0", "quote": "import q"}]})
    assert repo.node_context(nb.id, pid)["steps"][0]["element_text"] == "import q"
    scoped = repo.node_context(nb.id, pid, allowed_source_ids=["s-any"])["steps"]
    assert scoped == [{"name": "import", "element_text": "", "section_path": "1 > Flow"}]
