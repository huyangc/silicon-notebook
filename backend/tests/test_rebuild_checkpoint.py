import re

import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_migration_creates_checkpoint_table(repo):
    """迁移后表存在,且 user_version 已达 SCHEMA_VERSION。"""
    from app.services.sqlite_repository import SCHEMA_VERSION
    with repo._connect() as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_rebuild_checkpoint'"
        ).fetchone()
        uv = int(db.execute("PRAGMA user_version").fetchone()[0])
    assert row is not None
    assert uv == SCHEMA_VERSION


def test_deployed_v9_db_gets_checkpoint_table_backfilled(repo):
    """已部署库(user_version=9,缺 kg_rebuild_checkpoint 表)重新打开时,
    _migration_10 必须独立补建该表——镜像 test_mention_bridge.py 的
    test_deployed_v8_db_gets_backfilled 写法(drop 表→回退版本戳→重开)。"""
    from app.services.sqlite_repository import SCHEMA_VERSION

    with repo._connect() as db:
        db.execute("DROP TABLE kg_rebuild_checkpoint")
        db.execute("PRAGMA user_version = 9")

    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_rebuild_checkpoint'"
        ).fetchone()
        cols = {r["name"] for r in db.execute("PRAGMA table_info(kg_rebuild_checkpoint)").fetchall()}
        uv = int(db.execute("PRAGMA user_version").fetchone()[0])
    assert row is not None
    assert cols == {"notebook_id", "input_version", "stage", "item_key", "payload", "created_at"}
    assert uv == SCHEMA_VERSION


def test_ckpt_put_load_roundtrip(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "v1", "merge_review",
                           [("K-a\x1fK-b", {"decision": "merge", "confidence": 0.9})])
    loaded = repo._rebuild_ckpt_load(nb.id, "v1", "merge_review")
    assert loaded == {"K-a\x1fK-b": {"decision": "merge", "confidence": 0.9}}
    # 不同 stage / 版本互不干扰
    assert repo._rebuild_ckpt_load(nb.id, "v1", "concept_desc") == {}
    assert repo._rebuild_ckpt_load(nb.id, "v2", "merge_review") == {}


def test_ckpt_gc_drops_other_versions_only(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "old", "merge_review", [("k1", {"d": 1})])
    repo._rebuild_ckpt_put(nb.id, "cur", "merge_review", [("k2", {"d": 2})])
    repo._rebuild_ckpt_gc(nb.id, "cur")
    assert repo._rebuild_ckpt_load(nb.id, "old", "merge_review") == {}
    assert repo._rebuild_ckpt_load(nb.id, "cur", "merge_review") == {"k2": {"d": 2}}


def test_ckpt_clear_drops_all(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "cur", "merge_review", [("k1", {"d": 1})])
    repo._rebuild_ckpt_put(nb.id, "cur", "concept_desc", [("k2", {"d": 2})])
    repo._rebuild_ckpt_clear(nb.id)
    assert repo._rebuild_ckpt_load(nb.id, "cur", "merge_review") == {}
    assert repo._rebuild_ckpt_load(nb.id, "cur", "concept_desc") == {}


class _CountingReviewLLM:
    """把每个候选都判成 merge;记录 chat_json 调用次数。"""
    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, schema):
        self.calls += 1
        import re
        ids = re.findall(r"id=(ac\d+)", messages[0]["content"])
        decisions = [{"candidate_id": i, "decision": "merge",
                      "canonical_name": "x", "confidence": 0.99, "rationale": "r"}
                     for i in ids]
        return __import__("json").dumps({"decisions": decisions})


class _GroupedFakeEmbedder(FakeEmbedder):
    """默认 FakeEmbedder 是纯 SHA256 哈希——不同字符串的向量近似正交(实测
    "low noise amplifier 0" vs "lna 0" cos_sim≈0.71,达不到 cluster_seeds 的
    hi=0.94 门槛,auto_candidates 永远空,merge 审查压根不会触发)。这里按名字
    尾部数字分组:同组两个变体共享同一底层向量(cos_sim=1.0,可靠过 hi 门槛),
    组间仍走原始哈希(近似正交,不会误并)。只影响这一个测试文件的 fixture,
    不改变生产 embedder 行为。"""
    def _vec(self, text: str):
        m = re.search(r"(\d+)\s*$", text.strip())
        key = f"grp-{m.group(1)}" if m else text
        return super()._vec(key)


def _seed_mergeable(repo, nb_id):
    """造若干近义 concept(名字接近 → 进 auto_candidates → 触发 merge 审查)。"""
    repo.embedder = _GroupedFakeEmbedder(dim=repo.settings.embed_dim)
    objs = []
    for i in range(6):
        objs.append({"local_id": f"c{i}", "object_type": "concept",
                     "payload": {"name": f"low noise amplifier {i}", "section_path": ""},
                     "evidence": [{"quoted_span": f"lna variant {i}"}]})
        objs.append({"local_id": f"d{i}", "object_type": "concept",
                     "payload": {"name": f"lna {i}", "section_path": ""},
                     "evidence": [{"quoted_span": f"lna variant {i}"}]})
    repo.store_kg(nb_id, None, objs, [])


def test_merge_review_checkpoint_skips_relled_llm_on_second_run(repo, monkeypatch):
    """同输入连跑两次 rebuild:第二次 merge 审查 LLM 调用数=0(全部命中 checkpoint)。"""
    fake = _CountingReviewLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(type(repo), "kg_concept_desc_enabled", False, raising=False)  # 隔离描述阶段
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", False, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_mergeable(repo, nb.id)

    repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    assert first > 0                       # 首跑确有 merge 审查 LLM

    repo.rebuild_unified_kg(nb.id, force=True)
    assert fake.calls == first             # 二跑零新增(input_version 未变 → 全命中)


def test_fresh_clears_checkpoint_and_readjudicates(repo, monkeypatch):
    """--fresh(fresh=True)清 checkpoint → 再跑重新裁决(LLM 又被调用)。"""
    fake = _CountingReviewLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", False, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_mergeable(repo, nb.id)

    repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    repo.rebuild_unified_kg(nb.id, force=True, fresh=True)
    assert fake.calls > first               # fresh 清了 checkpoint → 又裁决一轮
