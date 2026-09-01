"""服务层的 KNN 路由判据（POSTGRES_LEXICAL_KNN_ENABLED）。

判据集中在 `_lexical_knn_allowed` 一个可测的 helper 里：开关开、run 未按来源
收窄、库的 nodes+chunks ≥ `POSTGRES_LEXICAL_KNN_MIN_ROWS`。三者缺一即 False，
SQL 逐字回到 legacy。**必须直接测 helper 本身**——只经 `_lexical_object_hits`
捕获 kwarg 的写法抓不住「删掉 scoped 排除」这类变异（受限分支本来就不转发该
kwarg，缺省 False 让断言空转；评审实测过）。KNN SQL 的正确性由 PostgreSQL
conformance 测试负责，这份测试只回答「hint 什么时候被给出、有没有真的传下去」。

另一半同样不可省：helper 必须在调用方**打开连接之前**跑（它读
`notebook_copy_stats`，那条链会自己开一条池化连接；持连接时嵌套获取会在报告
多节并发下坐死连接池——`_lexical_object_hits` docstring 里整段警告的形态）。
这里用调用顺序断言钉住。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _verdict(repo, *, flag, rows, allowed_source_ids=None, stats_error=False,
             capable=True):
    """对判据组合直接求 `_lexical_knn_allowed` 的值。

    fixture 是 SQLite 仓库,其适配器如实声明 `lexical_knn_capable = False`;
    这里默认在实例上覆盖成 True,好让其余判据可测——`capable=False` 那一档
    测的才是 SQLite 部署的真实短路。
    """
    candidates = repo.retrieval.candidates
    real_stats = candidates.notebook_copy_stats

    def fake_stats(nb):
        if stats_error:
            raise RuntimeError("stats unavailable")
        return {"copyable": rows <= 5000, "size": {"nodes": rows, "chunks": 0}}

    candidates.notebook_copy_stats = fake_stats
    candidates.settings.postgres_lexical_knn_enabled = flag
    candidates.knowledge.lexical_knn_capable = capable
    try:
        return candidates._lexical_knn_allowed(
            "nb-any", allowed_source_ids=allowed_source_ids
        )
    finally:
        candidates.notebook_copy_stats = real_stats
        del candidates.knowledge.lexical_knn_capable


def test_verdict_requires_all_three_conditions(repo):
    threshold = repo.retrieval.candidates.settings.postgres_lexical_knn_min_rows
    # 全部满足 → True;逐个抽掉一个条件 → False。
    assert _verdict(repo, flag=True, rows=threshold) is True
    assert _verdict(repo, flag=False, rows=threshold) is False
    assert _verdict(
        repo, flag=True, rows=threshold, allowed_source_ids=["src-1"]
    ) is False
    # GiST 索引没有 notebook 键,KNN 是全库距离序:未达规模下限的库要在别人的
    # 行里翻找自己的候选,反而丢掉复合 GIN 快路径——必须留在 legacy。
    assert _verdict(repo, flag=True, rows=threshold - 1) is False
    # 判定链上的异常按 False 收:legacy 永远是安全出口。
    assert _verdict(repo, flag=True, rows=threshold, stats_error=True) is False
    # 适配器声明不具备(SQLite 的真实声明)→ False,其余判据满足也不例外。
    assert _verdict(repo, flag=True, rows=threshold, capable=False) is False


def test_incapable_adapter_short_circuits_before_copy_stats(repo):
    """能力位在规模判定之前——默认开之后 SQLite 部署的零成本就靠这一条。

    发行默认后端是 SQLite,flag 默认 True:没有这道短路,每次未收窄检索都要
    白付一条版本查询、冷缓存时再加五条规模聚合,而 hint 在 FTS5 上永远无事
    可做(codex #464 R1 P2)。exploding stats 证明 stats 根本没被触碰。
    """
    candidates = repo.retrieval.candidates
    real_stats = candidates.notebook_copy_stats
    calls: list[str] = []

    def counting_stats(nb):
        # 计数而不是抛错:判定函数的 fail-closed `except` 会把异常吞成
        # False,抛错版在「能力检查被删」的变异下照样全绿(实测)。
        calls.append(nb)
        return {"copyable": False, "size": {"nodes": 10**7, "chunks": 0}}

    candidates.notebook_copy_stats = counting_stats
    candidates.settings.postgres_lexical_knn_enabled = True
    try:
        # 不覆盖能力位:SQLite 适配器自己的 False 声明就是被测对象。
        assert candidates.knowledge.lexical_knn_capable is False
        assert candidates._lexical_knn_allowed("nb-any") is False
        assert calls == [], "适配器不具备时不得读 notebook_copy_stats"
    finally:
        candidates.notebook_copy_stats = real_stats


def test_flag_off_short_circuits_before_copy_stats(repo):
    """开关关着时零开销——copy_stats(会开连接、发 SELECT)绝不能被触碰。"""
    candidates = repo.retrieval.candidates
    real_stats = candidates.notebook_copy_stats

    def exploding_stats(nb):
        raise AssertionError("flag off 时不得读 notebook_copy_stats")

    candidates.notebook_copy_stats = exploding_stats
    candidates.settings.postgres_lexical_knn_enabled = False
    try:
        assert candidates._lexical_knn_allowed("nb-any") is False
    finally:
        candidates.notebook_copy_stats = real_stats


def test_hits_helper_forwards_the_precomputed_verdict(repo):
    """`_lexical_object_hits` 只转发、不自算——判定必须发生在连接之外。

    两个断言合起来钉住这条:①helper 执行期间 copy_stats 不被调用(自算的
    回归形态);②传入的 allow_knn 原样到达 fts_search。
    """
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    candidates = repo.retrieval.candidates
    captured: dict = {}
    real_stats = candidates.notebook_copy_stats

    def exploding_stats(_nb):
        raise AssertionError("_lexical_object_hits 内不得读 notebook_copy_stats")

    def fake_fts_search(db, nb_id, query, k, **kwargs):
        captured.update(kwargs)
        kwargs["routing_stats"].update({
            "term_count": 4,
            "knn_term_count": 2,
            "knn_seconds": 0.125,
        })
        return []

    candidates.notebook_copy_stats = exploding_stats
    candidates.knowledge.fts_search = fake_fts_search
    routing_stats: dict[str, int | float] = {}
    try:
        with repo._connect() as db:
            candidates._lexical_object_hits(
                db, nb.id, "thermal design", 12,
                site="test", corpus_langs=None, allow_knn=True,
                routing_stats=routing_stats,
            )
    finally:
        candidates.notebook_copy_stats = real_stats
    assert captured.get("allow_knn") is True
    assert captured.get("knn_max_term_chars") == 32
    assert isinstance(captured.get("routing_stats"), dict)
    assert routing_stats == {
        "term_count": 4,
        "knn_term_count": 2,
        "knn_seconds": 0.125,
    }


def test_sqlite_adapter_accepts_and_ignores_the_hint(repo):
    """签名对等:service 不判 dialect,同一调用必须在 SQLite 上也合法。

    经真实的服务级 knowledge 句柄调用(与 `_lexical_object_hits` 同一入口),
    不 mock——这条测试的全部意义就是真实签名接得住这个 kwarg。
    """
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    knowledge = repo.retrieval.candidates.knowledge
    with repo._connect() as db:
        rows = knowledge.fts_search(db, nb.id, "thermal", 5, allow_knn=True)
    assert rows == []
