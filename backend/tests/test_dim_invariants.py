"""T10:运行时截断的结构性守卫——防未来新代码绕过 truncate_vec 直读向量做
相似度(风险登记 R2:静默零召回/混空间是本项目默认失败模式,漏一处只是
「检索变差」不报错)。这些是元测试(读源码 AST),不跑业务逻辑。

计划:docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md T10。
"""
import ast
import pathlib

import numpy as np
import pytest
from tests.model_testkit import bind_all_embedding_clients

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
REPO_PY = APP / "services" / "sqlite_repository.py"
# Task 15:四旁路正文迁到 lifecycle/governance 服务 —— 扫描面跟着代码走。
_SCAN_FILES = (REPO_PY, APP / "services" / "retrieval_candidates.py", APP / "services" / "knowledge_lifecycle.py",
               APP / "services" / "knowledge_governance.py")
# 允许调用 decode_vector 的函数(_SCAN_FILES 内)。新增消费点必须:①在此白名单登记;
# ②若做相似度,同函数须调 truncate_vec(见下条测试)。白名单外 decode_vector = 红。
_ALLOWED_DECODE_FUNCS = {
    # 四旁路(相似度,必接 truncate_vec):
    "incremental_fuse_source", "_tier2_bridge_candidates_ann",
    "resolve_notebook_conflicts", "_stream_seed_reps", "_gather_elements",
    "complete_relations_for_source",
    # 死代码(无调用者;复活须接 truncate_vec —— 见 docstring 标注):
    "_knowledge_vectors",
}
# 四旁路中真正做相似度的函数:除登记外,同函数体必须出现 truncate_vec
_MUST_TRUNCATE = {
    "incremental_fuse_source", "_tier2_bridge_candidates_ann",
    "resolve_notebook_conflicts", "_stream_seed_reps", "_gather_elements",
    "complete_relations_for_source",
}


def _funcs_calling(tree, callee_names):
    """返回 {enclosing_func_name: {called names in that func}} 仅含调用了
    callee_names 里任一名字的函数。嵌套 def 归到最近的外层 def。"""
    out = {}

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fn in callee_names and self.stack:
                out.setdefault(self.stack[0], set()).add(fn)
            self.generic_visit(node)

    V().visit(tree)
    return out


def test_no_unwhitelisted_decode_vector_in_repo():
    """_SCAN_FILES 里所有 decode_vector 调用必须在白名单函数内(合并三模块)。"""
    callers = {k: v for f in _SCAN_FILES for k, v in _funcs_calling(
        ast.parse(f.read_text(encoding="utf-8")), {"decode_vector"}).items()}
    offenders = set(callers) - _ALLOWED_DECODE_FUNCS
    assert not offenders, (
        f"新的 decode_vector 直读位点未登记: {sorted(offenders)} —— "
        f"若做相似度必须接 truncate_vec 并加入白名单(tests/test_dim_invariants.py),"
        f"若是写/迁移路径请在白名单注明。见风险登记 R2。")


def test_similarity_bypasses_also_truncate():
    """四旁路(相似度)每个函数体必须同时调用 truncate_vec —— 只 decode 不截断
    = 查询/语料混空间。"""
    callers = {k: v for f in _SCAN_FILES for k, v in _funcs_calling(
        ast.parse(f.read_text(encoding="utf-8")), {"decode_vector", "truncate_vec"}).items()}
    for fn in _MUST_TRUNCATE:
        called = callers.get(fn, set())
        assert "decode_vector" in called, f"{fn} 应调 decode_vector(测试前提失效?)"
        assert "truncate_vec" in called, (
            f"{fn} 调了 decode_vector 但没调 truncate_vec —— 相似度位点漏截断,"
            f"混空间静默零召回(R1)")


def test_no_raw_frombuffer_similarity_outside_vector_index():
    """np.frombuffer 只应出现在 vector_index.decode_vector(唯一解码入口);
    别处直接 frombuffer 绕过 decode_vector = 也绕过 truncate。

    例外:``dtype=np.uint8`` 的字节反序列化不是**相似度向量**解码——向量以 float
    落盘/读回、必须经 decode_vector 截断混维;而 uint8 只是把 bytes 读回一维字节
    数组(如 ``scale_index.encode_viz_edges`` 绕开 npz 字符串标量 2^31 上限,与
    truncate 语义无关)。守卫真正要挡的是绕过 decode_vector 的向量解码,故放行 uint8。"""
    hits = []
    for p in APP.rglob("*.py"):
        # Only the domain module actually decodes bytes with frombuffer; the
        # app.services.vector_index shim (same basename, B3 re-export) has
        # zero frombuffer calls of its own, so exempting by basename alone
        # would silently cover a services-layer violation too.
        if p.relative_to(APP) == pathlib.Path("domain/vector_index.py"):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "np.frombuffer(" in line or (".frombuffer(" in line and "np" in line):
                # 允许注释里提及
                if line.lstrip().startswith("#"):
                    continue
                # uint8 字节反序列化 ≠ 相似度向量解码(向量走 float),放行。
                if "dtype=np.uint8" in line or "dtype=numpy.uint8" in line:
                    continue
                hits.append(f"{p.relative_to(APP)}:{i}")
    assert not hits, f"vector_index 外的裸 frombuffer: {hits}(应走 decode_vector)"

# ── 运行时不变量:写路径保持原生维 ──────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_RUNTIME_DIM", "16")     # 运行时开
    for k, v in {"EMBED_DIM": "32"}.items():
        monkeypatch.setenv(k, v)
    from app.core.config import Settings, get_settings
    get_settings.cache_clear()
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=32))
    yield r
    get_settings.cache_clear()


def test_write_path_stores_native_dim_under_runtime_truncation(repo):
    """运行时维=16,但落库的 chunk/KG 向量长度仍==存储维 32(真相源不被截断)。"""
    from app.models.schemas import NotebookCreate
    from app.services.vector_index import decode_vector
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
                  [{"local_id": "o1", "object_type": "concept",
                    "payload": {"name": "Bandgap"}, "evidence": []}], [])
    # KG 节点向量写路径:_backfill 走 embedder→encode_vector(原生维),不经截断
    with repo._connect() as db:
        objs = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchall()
    with repo._write() as db:
        repo._backfill_knowledge_embeddings(
            db, nb.id, [{"id": o["id"], "payload": o["payload"]} for o in objs])
    with repo._connect() as db:
        rows = db.execute(
            "SELECT vector FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchall()
    assert rows, "应有 KG 节点向量落库"
    for r in rows:
        arr = decode_vector(r["vector"])
        assert arr.shape[0] == 32, "落库向量必须是存储维 32(真相源),不能被运行时截断"
