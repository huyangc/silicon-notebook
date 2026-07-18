import time
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from app.services.knowhow import transfer as kh_transfer

COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
]

class _FakeEmbedder:
    dim = 3
    def __init__(self):
        self.call_count = 0
    def embed_texts(self, texts):
        self.call_count += len(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    r = SQLiteRepository(Settings())
    r.embedder = _FakeEmbedder()
    return r

def _nb(repo, name="KH"):
    return repo.create_notebook(NotebookCreate(name=name, purpose="p", primary_domain="d")).id

def _table_with_row(repo, nb):
    tid = repo.create_knowhow_table(nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    return tid

def _settle(repo, tid, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = repo.get_knowhow_table(tid)
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return repo.get_knowhow_table(tid)

def _project(repo, tid):
    # store 层的 add_knowhow_row 不自动调度投影（那是路由/api 层的事）——测试里显式调度。
    from app.services.knowhow.api import get_scheduler
    get_scheduler(repo).schedule(tid)
    return _settle(repo, tid)

def test_copy_creates_independent_table_in_target(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)  # 不投影：业务表拷贝与投影无关

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert new_tid != src_tid
    dst = repo.get_knowhow_table(new_tid)
    assert dst["notebook_id"] == dst_nb
    assert dst["created_by"] == "user-x"
    assert {c["name"] for c in dst["columns"]} == {"违例类型", "现象识别"}
    assert len(dst["rows"]) == 1
    # 源不受影响
    assert repo.get_knowhow_table(src_tid)["notebook_id"] == src_nb

def test_copy_reprojection_reuses_vectors_zero_reembed(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    _project(repo, src_tid)  # 先把源投影好，产出 chunks + chunk_embeddings
    repo.embedder.call_count = 0  # 归零，之后只观察 copy 引发的 embed

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")
    _settle(repo, new_tid)  # 等重投影落地（copy_table 自己已调度）

    # K-1：chunk_embeddings 已随拷贝以稳定 id 落库 → 重投影零重嵌入
    assert repo.embedder.call_count == 0


def test_copy_is_retrievable_in_target_via_lexical_and_vector(repo):
    """拷贝出来的表必须在目标 notebook 里「搜得到」——两条检索通道都要有。

    词法通道尤其脆弱：chunks_fts 是无触发器的手工维护 FTS5 虚表，只有
    ChunkStore.insert_rows/delete_by_ids 会写它；而对拷贝出来的 chunk，
    重投影必然走 `old_specs == new_specs -> continue`（projection.py），
    两个写 FTS 的路径一个都不会碰 → 事务里不显式补 chunks_fts 的话，副本
    永久只能被向量召回、词法搜索彻底搜不到，且没有任何自愈路径（不像向量
    有 self-heal probe）。这条断言就是那个缺口的回归闸。"""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    _project(repo, src_tid)

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")
    _settle(repo, new_tid)

    with repo._connect() as db:
        # 词法/FTS 通道（production 检索用的同一个原语）
        fts_hits = repo._runtime.knowledge.chunk_fts_search(db, dst_nb, "示波器观察", k=10)
        # 向量通道：拷贝进来的 chunk 必须都带着自己的向量
        vec_rows = db.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE notebook_id=?", (dst_nb,)
        ).fetchone()[0]
        chunk_rows = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (dst_nb,)
        ).fetchone()[0]

    assert fts_hits, "副本的 chunk 未进 chunks_fts —— 目标库词法检索永久搜不到"
    assert chunk_rows > 0
    assert vec_rows == chunk_rows, "副本的 chunk 与向量数量不匹配"
