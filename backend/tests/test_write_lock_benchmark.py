"""写锁瘦身的合成基准。

默认档(20k 对象)每次都跑,当回归守卫用;大规模档(--benchmark-scale 或 slow 标记)
按需跑,用来量部署量级(10^5-10^6)的分布。

种子数据用 repo.store_kg 的公开 API 造(与 test_rebuild_streaming.py 同款),
不写裸 SQL —— 服务层/测试层都不该出现裸 SQL(callers_static 约束)。唯一例外是
_mk_source:它只插一行满足 knowledge_relations.source_id 外键的最小 sources
行,做法照抄 test_canonical_relations.py/test_mention_bridge.py 已有的 _mk_src
helper(仅测试 fixture 引导,不是给生产代码抄的写法)。
"""
import os

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate
from tests.model_testkit import RecordingModelProvider, bind_all_embedding_clients

_BATCH = 2000
# 同一来源、同一类型里,每 _GROUP 个对象共享一个规范名——让聚类真的产生多成员
# 簇(全唯一名会让 concept_clusters 退化成 1:1,量不出真实的整表替换代价)。
_GROUP = 8
# rebuild_unified_kg 对这四种类型各跑一遍(concept 走向量聚类那一支;其余三种走
# knowledge_lifecycle.py 的 _TYPE_MERGE 分支,knowledge_lifecycle.py ~1937-1944)。
# 四种都得有对象,否则对应的 pass 是空的,测不出它的锁占用(Fix 3 第三点)。
_TYPES = ("concept", "claim", "formula", "procedure")
# >=2 是 replace_mention_bridge 的硬性前提:概念簇必须横跨 >=2 个不同来源才会
# 产出跨源别名(knowledge_lifecycle.rebuild_mention_bridge 的 cross 过滤,
# ~2172-2181)。单来源无论怎么分组、怎么加 relations,这条件都测不出来
# (Fix 3 第二点——seed 关系不是充分条件)。
_N_SOURCES = 4


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    provider = RecordingModelProvider()
    r = SQLiteRepository(
        Settings(model_services_config=""),
        model_provider=provider,
    )
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _mk_source(repo, notebook_id: str, source_id: str) -> None:
    """插一行最小 sources,满足 knowledge_relations.source_id 的外键——
    PRAGMA foreign_keys=ON 常开,relations 非空又传一个不存在的 source_id 会
    直接 FK failed(与 test_canonical_relations.py/test_mention_bridge.py 的
    _mk_src 同款)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'markdown', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (source_id, notebook_id, source_id),
        )


def seed_notebook(repo, n_objects: int) -> str:
    """造 n_objects 个对象,分布在 _N_SOURCES 个来源 × 四种类型上;同一来源同一
    类型里每 _GROUP 个对象共享一个规范名,使聚类真的产生多成员簇。

    与早期版本(仅 concept/claim、source_id=None、relations=[])相比,这版让
    四选一重构候选点里此前测不出的两个也被真实触发:

    - 多来源:kind 本地计数器在每个来源内部都从 0 重新数,所以任一规范名在
      _N_SOURCES 个来源里各出现一次同样的分组序列——同一个 concept 簇天然横跨
      全部 _N_SOURCES 个来源,满足 replace_mention_bridge 的 cross 过滤。
    - relations 非空:批内相邻两个对象连一条边,replace_canonical_relations
      才有 knowledge_relations 行可折叠进 canonical_relations。
    - claim 的名字里嵌入同一 cycle 的 concept 组名(如"...concept-5...");
      concept 计数器与 claim 计数器每个 j 的 cycle 里同步递增(四种类型按
      _TYPES 顺序轮转,每 4 个 j 各类型各出现一次),所以两者的组号 g 恒相等
      ——确定性触发 mention_scan 的边界匹配,而不只是结构上「可能」触发
      (簇/来源条件即使都满足,匹配命中数不够仍会是 0)。
    """
    nb = repo.create_notebook(NotebookCreate(name="bench"))
    per_source = max(1, n_objects // _N_SOURCES)
    for src_idx in range(_N_SOURCES):
        source_id = f"bench-src-{src_idx}"
        _mk_source(repo, nb.id, source_id)
        kind_local = {k: 0 for k in _TYPES}
        batch: list = []
        rels: list = []
        for j in range(per_source):
            kind = _TYPES[j % len(_TYPES)]
            g = kind_local[kind] // _GROUP
            kind_local[kind] += 1
            local_id = f"s{src_idx}o{j}"
            if kind == "claim":
                # g 与同一 cycle 里 concept 的组号恒相等(见上面 docstring)。
                # 奇数组额外提及上一组,制造两个 canonical 同时被提——
                # concept_comentions(replace_mention_bridge 写的另一张表)
                # 需要同一条 claim 命中 >=2 个不同 canonical 才会有行,只提
                # 一个概念的 claim 永远测不出这部分。
                if g % 2 == 1 and g > 0:
                    name = (f"this claim compares concept-{g} and "
                            f"concept-{g - 1} directly in practice")
                else:
                    name = (f"experiment confirms that concept-{g} behaves "
                            f"as documented in practice")
            else:
                name = f"{kind}-{g}"
            batch.append({
                "local_id": local_id,
                "object_type": kind,
                "payload": {"name": name, "section_path": ""},
                "evidence": [],
            })
            if len(batch) >= 2:
                rels.append({
                    "source_local_id": batch[-2]["local_id"],
                    "target_local_id": batch[-1]["local_id"],
                    "edge_type": "relates_to",
                    "evidence": [],
                })
            if len(batch) >= _BATCH:
                repo.store_kg(nb.id, source_id, batch, rels)
                batch, rels = [], []
        if batch:
            repo.store_kg(nb.id, source_id, batch, rels)
    return nb.id


def measure_rebuild(repo, notebook_id: str) -> dict:
    db = repo._runtime.database
    assert db.stats is not None, "基准需要仪器打开(DB_WRITE_LOCK_STATS)"
    db.stats.reset()
    repo.rebuild_unified_kg(notebook_id, force=True)
    return db.stats.snapshot()["sites"]


def report(sites: dict) -> str:
    rows = sorted(sites.items(), key=lambda kv: kv[1]["hold_max_ms"],
                  reverse=True)
    out = [f"{'site':<44}{'n':>7}{'hold_max':>11}{'hold_p99':>11}"
           f"{'wait_max':>11}"]
    for site, s in rows:
        out.append(f"{site:<44}{s['count']:>7}{s['hold_max_ms']:>11.1f}"
                   f"{s['hold_p99_ms']:>11.1f}{s['wait_max_ms']:>11.1f}")
    return "\n".join(out)


def test_benchmark_default_scale_reports_hold_distribution(repo, capsys):
    """默认档:20k 对象。这一档不设门槛断言(小规模下几乎必然达标),
    只保证基准本身可跑、报表可读 —— 门槛断言在 Task 6/7 之后加。

    但形状断言现在是有的(Fix 2):`assert sites` 单独一条测不出「坍缩成一个
    桶」——skip=3 的 facade 坍缩 bug 就是在这条断言下存活了四个任务,因为坍缩
    后 sites 仍然非空,只是只有 1~2 行、且都记在 wrapper/seam 文件自己身上。
    下面三条断言专门堵这个盲区。"""
    nb = seed_notebook(repo, 20_000)
    sites = measure_rebuild(repo, nb)
    assert sites, "rebuild 期间没有采到任何 write() 样本"
    assert "?" not in sites, (
        "记录到 unresolved-stack fallback('?')——帧走查退化,归因不可信: "
        f"{sites}"
    )
    wrapper_sites = [s for s in sites
                     if s.startswith("database.py:")
                     or s.startswith("sqlite_repository.py:")]
    assert not wrapper_sites, (
        "记录到了 wrapper/seam 文件自己的行——database.py/sqlite_repository.py "
        "都只是写锁的包装层,真实调用点应该在别处;帧走查提前停在包装帧上正是"
        f"本任务修复的坍缩缺陷的症状: {wrapper_sites}"
    )
    assert len(sites) >= 3, (
        f"只记录到 {len(sites)} 个不同调用点——坍缩成 1~2 个正是 skip=3 那个 "
        f"facade 坍缩 bug 的症状: {sites}"
    )
    with capsys.disabled():
        print("\n=== write-lock benchmark (20k objects) ===")
        print(report(sites))


@pytest.mark.slow
def test_benchmark_large_scale_reports_hold_distribution(repo, capsys):
    """大规模档:仅当显式设置了 BENCH_OBJECTS 才跑(opt-in),否则 skip。

    ⚠ 不能只靠 ``@pytest.mark.slow`` 门:``backend/pytest.ini`` 的 ``addopts``
    没有 ``-m``,``scripts/check_backend.sh`` 也是对 ``tests/`` 裸调用
    ``pytest``(不加 ``-m "not slow"``——加了会连累这仓库其它 slow 标记的测试
    一起被过滤掉,超出本次改动范围)。两边都不过滤标记,这条测试就会在每次
    ``scripts/check.sh`` 里都真的跑一遍,默认 300k 对象、一次全量重聚类。
    显式检查 ``BENCH_OBJECTS`` 是否在环境变量里,给这一档一个标记之外、真正
    生效的 opt-in 开关。

    跑法:
      ``backend/pytest.ini`` 的 ``addopts`` 固定 ``-n 12``,xdist 开着时 ``-s``
      拿不到 worker 的 stdout —— 不加 ``-n0`` 会通过但一张表都不打印:
      cd backend && SILICON_NOTEBOOK_ENV_FILE="" BENCH_OBJECTS=500000 \\
        python -m pytest tests/test_write_lock_benchmark.py -m slow -s -q -n0
    """
    if "BENCH_OBJECTS" not in os.environ:
        pytest.skip(
            "opt-in only: set BENCH_OBJECTS (e.g. BENCH_OBJECTS=300000) to "
            "run the large-scale write-lock benchmark"
        )
    n = int(os.environ["BENCH_OBJECTS"])
    nb = seed_notebook(repo, n)
    sites = measure_rebuild(repo, nb)
    with capsys.disabled():
        print(f"\n=== write-lock benchmark ({n} objects) ===")
        print(report(sites))
