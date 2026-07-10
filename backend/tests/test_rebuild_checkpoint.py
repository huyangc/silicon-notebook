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


class _CountingDescLLM:
    """merge 审查一律 keep_separate(不并簇,保持描述阶段候选稳定);描述返回定值,计数。"""
    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, schema):
        content = messages[0]["content"]
        if "candidate concept merges" in content:      # merge 审查 prompt
            import re, json as _j
            ids = re.findall(r"id=(ac\d+)", content)
            return _j.dumps({"decisions": [
                {"candidate_id": i, "decision": "keep_separate",
                 "canonical_name": "", "confidence": 0.9, "rationale": "r"} for i in ids]})
        self.calls += 1                                # 概念描述 prompt
        import json as _j
        return _j.dumps({"description": "一句定值描述。"})


def test_concept_desc_checkpoint_skips_relled_llm_on_second_run(repo, monkeypatch):
    """概念描述 checkpoint 隔离验证:写簇前被杀,二跑仍应跳过已完成描述的 LLM 调用。

    不能只写"连跑两次、断言二跑零调用"——那样的测试即使不加 checkpoint 也会通过
    (VACUOUS):既有 old_desc 缓存(读 concept_clusters.canonical_desc_sig,写簇时
    才落库)本身就会在二跑时命中同一份数据并跳过 LLM。要真正驱动本任务新加的
    checkpoint,必须让 old_desc 在二跑时读不到任何东西——即首跑要在"描述已算完
    (checkpoint 已 flush)、簇还没写(concept_clusters 仍空)"的中间点崩溃:
      - 首跑:monkeypatch `_write_cluster_map_streamed` 对 object_type=='concept'
        的第一次调用抛 RuntimeError。描述阶段(PHASE1/2)在这次调用之前已经跑完
        并把结果 flush 进 `kg_rebuild_checkpoint`,随后写簇函数抛错 → rebuild
        整体失败、concept_clusters 表这次 rebuild 什么都没写进去。
      - 二跑:恢复写簇函数,重新 rebuild。这时 old_desc 必为空(上面已断言
        concept_clusters 为空),二跑若仍是零新增 LLM 调用,只能是命中了
        concept_desc checkpoint —— 这就是非掩盖的证明。
    """
    fake = _CountingDescLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", True, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 造跨"源"同名 concept → 多成员 canonical(total>=2)→ 触发描述生成
    repo.store_kg(nb.id, "s1", [{"local_id": "a", "object_type": "concept",
        "payload": {"name": "bandgap reference", "section_path": ""},
        "evidence": [{"quoted_span": "bandgap ref circuit"}]}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "b", "object_type": "concept",
        "payload": {"name": "bandgap reference", "section_path": ""},
        "evidence": [{"quoted_span": "bandgap ref circuit"}]}], [])

    ver_before = repo._cluster_input_version(nb.id)

    # 首跑:写簇前(object_type=='concept' 的第一次调用)人为炸掉。
    orig_write = repo._write_cluster_map_streamed

    def _boom(notebook_id, object_type, *a, **kw):
        if object_type == "concept":
            raise RuntimeError("simulated crash before concept cluster write")
        return orig_write(notebook_id, object_type, *a, **kw)

    monkeypatch.setattr(repo, "_write_cluster_map_streamed", _boom)
    with pytest.raises(RuntimeError):
        repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    assert first > 0                        # 非空跑:描述 LLM 确实执行过

    # 崩溃点在簇写之前:concept_clusters 必然仍是空的——二跑时 old_desc 读不到
    # 任何东西,不可能是它让二跑跳过。
    with repo._connect() as db:
        cc = db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=? AND object_type='concept'",
            (nb.id,)).fetchone()["c"]
    assert cc == 0

    ver_after_crash = repo._cluster_input_version(nb.id)
    assert ver_after_crash == ver_before    # 崩溃发生在 mutation_seq bump 之前,
                                             # 二跑能在同一 input_version 下找到 checkpoint

    # 二跑:恢复写簇函数,重新 rebuild —— 应命中 concept_desc checkpoint,零新增调用。
    monkeypatch.setattr(repo, "_write_cluster_map_streamed", orig_write)
    repo.rebuild_unified_kg(nb.id, force=True)
    assert fake.calls == first              # 二跑零新增(old_desc 此刻仍为空,
                                             # 唯一可能的命中源是 concept_desc checkpoint)


def test_concept_desc_checkpoint_periodic_flush_at_16(repo, monkeypatch):
    """概念描述阶段的周期 flush 分支(_DESC_CKPT_FLUSH=16,PHASE2 循环内
    `if len(_ck_buf) >= _DESC_CKPT_FLUSH`)此前从未被测试覆盖过——已有用例的工作项
    数量都远小于 16,只会走循环结束后的 remainder flush。这里造 17 个互不相同的
    跨源同名 concept(各 2 member,total=2 → 都触发描述生成),驱动至少 17 个描述
    工作项:flush-at-16 一次 + remainder 一次,单次 rebuild 内
    _rebuild_ckpt_put(stage='concept_desc') 必须被调用 ≥2 次。"""
    fake = _CountingDescLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", True, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 17 个 distinct concept 名,每个都跨两个 source 各出现一次(exact-name seed
    # 合并 → 每个 canonical total members=2),互不相似 → 不会触发 merge-review。
    for i in range(17):
        name = f"concept topic {i}"
        repo.store_kg(nb.id, f"s{i}a", [{
            "local_id": f"a{i}", "object_type": "concept",
            "payload": {"name": name, "section_path": ""},
            "evidence": [{"quoted_span": f"topic {i} quote alpha"}],
        }], [])
        repo.store_kg(nb.id, f"s{i}b", [{
            "local_id": f"b{i}", "object_type": "concept",
            "payload": {"name": name, "section_path": ""},
            "evidence": [{"quoted_span": f"topic {i} quote beta"}],
        }], [])

    put_calls = {"concept_desc": 0}
    orig_put = repo._rebuild_ckpt_put

    def _spy_put(notebook_id, input_version, stage, rows):
        if stage == "concept_desc":
            put_calls["concept_desc"] += 1
        return orig_put(notebook_id, input_version, stage, rows)

    monkeypatch.setattr(repo, "_rebuild_ckpt_put", _spy_put)

    repo.rebuild_unified_kg(nb.id, force=True)

    assert fake.calls == 17                  # 17 个 canonical 都触发了描述 LLM 调用
    assert put_calls["concept_desc"] >= 2    # flush-at-16 一次 + remainder 一次(非仅 1 次)


def test_concept_desc_checkpoint_put_failure_does_not_abort_rebuild(repo, monkeypatch):
    """Fix 1 的 fail-open 证明:_rebuild_ckpt_put 每次调用都抛错时,
    rebuild_unified_kg 仍必须完整跑完(返回 cluster 数、不抛异常)——对应全局约束
    "checkpoint 落库失败,绝不能抛出打断 rebuild"。用单 canonical(autoc 为空、
    不会触发 merge-review)隔离验证,只专注概念描述阶段的 checkpoint 落库路径。"""
    fake = _CountingDescLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", True, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "a", "object_type": "concept",
        "payload": {"name": "bandgap reference", "section_path": ""},
        "evidence": [{"quoted_span": "bandgap ref circuit"}]}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "b", "object_type": "concept",
        "payload": {"name": "bandgap reference", "section_path": ""},
        "evidence": [{"quoted_span": "bandgap ref circuit"}]}], [])

    def _boom_put(*a, **kw):
        raise RuntimeError("simulated checkpoint put failure")

    monkeypatch.setattr(repo, "_rebuild_ckpt_put", _boom_put)

    n = repo.rebuild_unified_kg(nb.id, force=True)   # 不应抛出——fail-open
    assert isinstance(n, int) and n > 0


def test_node_vector_backfill_preserves_merge_review_checkpoint(repo, monkeypatch):
    """节点向量部分 backfill(emb_c 变、kg_mutation_seq 不变)后重跑
    rebuild_unified_kg(force=True):merge 审查 checkpoint 必须存活(0 新 LLM 调用)。

    Task 5 的节点向量 backfill 增量提交,emb_c(knowledge_embeddings COUNT)会在
    backfill 过程中持续爬升,但 kg_mutation_seq 只在 backfill 结束时才 bump(经
    _mark_unified_kg_dirty)。若 checkpoint 键在含 emb_c 的 _cluster_input_version
    上,backfill 中途崩溃后的续跑会因为 emb_c 变了而被判定"输入变化",entry GC
    把 merge_review checkpoint 清空,被迫对全部候选重新走一遍(可能数小时的)LLM
    裁决——这正是本测试要防住的回归。用一次裸 INSERT 模拟"backfill 提交了一行,
    但还没跑完"的中间状态(不经 store_kg/_mark_unified_kg_dirty,seq 不动)。
    """
    fake = _CountingReviewLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", False, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_mergeable(repo, nb.id)

    repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    assert first > 0                        # 首跑确有 merge 审查 LLM

    ver_before = repo._cluster_input_version(nb.id)
    ck_ver_before = repo._cluster_input_version(nb.id, exclude_emb_count=True)

    # 模拟节点向量部分 backfill 落库一行:直接 INSERT 进 knowledge_embeddings,
    # 不经 store_kg/_mark_unified_kg_dirty —— 只有 emb_c 的 COUNT 会变,
    # kg_mutation_seq/obj_c/dec_c 全部不动(镜像 backfill 中途、结束前的状态)。
    from app.services.vector_index import encode_vector
    with repo._write() as db:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings "
            "(object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
            ("dummy-partial-backfill", nb.id, encode_vector([0.0] * 16), "2026-07-10T00:00:00"),
        )

    ver_after = repo._cluster_input_version(nb.id)
    ck_ver_after = repo._cluster_input_version(nb.id, exclude_emb_count=True)
    # 含 emb_c 的版本确实变了(证明这是个有意义的"backfill 中途"模拟,不是空操作)
    assert ver_after != ver_before
    # exclude_emb_count 版本对 emb_c 变化免疫 —— Fix 1 的核心保证
    assert ck_ver_after == ck_ver_before

    repo.rebuild_unified_kg(nb.id, force=True)
    # 0 新增 LLM 调用 → merge_review checkpoint 存活。
    # 旧代码(checkpoint 键在含 emb_c 的 _ver 上):emb_c 变 → _ver 变 → entry GC
    # 把上一轮 checkpoint 全部清空 → 全部候选重新裁决 → fake.calls 会翻倍。
    assert fake.calls == first
