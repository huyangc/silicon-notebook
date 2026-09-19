"""warm-peek lane(PR-B 任务 B3):非 active 的大库参与腿绝不冷加载 scale 索引。

``_CHUNK_PEEK_ONLY`` 的**设置方**是 ``chunk_federation`` 的联邦任务体(任务
B2),本文件只钉**读方**,因此一律直接用 ``contextvars.copy_context().run``
把 lane 打开——与任务体 ``ctx.run(...)`` 的生命周期同形,且不依赖 B2 落地。

钉住四件事:
  1. peek lane 上取索引走 ``catalog.peek_warm_chunk_index``,``_scale_index``
     一次都不许被调;暖 ANN 缺席时当场落 ``_retrieve_chunks_fts_degraded``,
     绝不进有界暴力向量路径(它会 ``_gather_chunks`` 整表读正文,并写
     ``_vector_matrix`` 的共享进程缓存);
  2. 默认腿逐字不变,仍 ``_scale_index(notebook_id, allow_stale=True)``;
  3. ContextVar 的作用域严格是那一份 context 副本;
  4. 每库来源天花板在**生产者**(FTS/ANN)的 ``LIMIT`` 之前到位,不是合并后
     的结果侧过滤。

``federated_chunk_candidates`` 相关的接线断言(``producer_explicit=False``、
逐库 visible 清单、被排除的库零 SQL)属于 B2/B4,不在本文件。
"""
import contextvars
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import retrieval_candidates as rc
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed(repo, n=3):
    """n 个带向量与 FTS 的 chunk(id ``c0..``,同属来源 ``s1``)。直插 chunks 会
    绕过写路径的 FTS 维护,故显式 backfill;未建 scale 索引 → 真实路径上 ANN 自然
    不可用,正好让「取索引」这一步成为本文件唯一的变量。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-09-19T00:00:00"
    embedder = repo._runtime.models.embedding("retrieval_query_embedding")
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("s1", nb.id, "Doc", "md", "ready", now, now),
        )
        for i in range(n):
            text = f"bandgap reference topic {i} body detail"
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"c{i}", nb.id, "s1", text, "", "[]", now),
            )
            db.execute(
                "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,"
                "created_at) VALUES (?,?,?,?)",
                (f"c{i}", nb.id, json.dumps(embedder.embed_texts([text])[0]), now),
            )
    repo.backfill_chunk_fts(nb.id)
    return nb


def _in_peek_context(fn, *args, **kwargs):
    """在一份 ``copy_context`` 里打开 peek lane 后调用——与联邦任务体每任务一份
    快照的生命周期同形,绝不把值留在调用方的上下文里。"""
    context = contextvars.copy_context()

    def _call():
        rc._CHUNK_PEEK_ONLY.set(True)
        return fn(*args, **kwargs)

    return context.run(_call)


def _forbid(monkeypatch, target, name, reason):
    def _boom(*_args, **_kwargs):
        pytest.fail(reason)

    monkeypatch.setattr(target, name, _boom)


def _capture_events(repo, monkeypatch):
    events = []
    original = repo.event_log.emit

    def _spy(event, **kwargs):
        events.append(event)
        return original(event, **kwargs)

    monkeypatch.setattr(repo.event_log, "emit", _spy)
    return events


class _Ann:
    """已经打开的 hnswlib handle 替身。挂在索引的 ``chunk_ann_handle`` 上,
    ``catalog.open_ann`` 因此在第一行就命中 memoize 直接归还——peek 腿上不发生
    任何 artifact 打开,这是本文件不桩 ``_open_scale_ann`` 的理由。"""

    def __init__(self):
        self.k_values = []

    def set_ef(self, _value):
        return None

    def knn_query(self, _query, *, k, **_kwargs):
        self.k_values.append(k)
        return (
            np.asarray([[0]], dtype=np.int64),
            np.asarray([[0.0]], dtype=np.float32),
        )


def _warm_index(handle):
    return SimpleNamespace(
        chunk_ann_labels=["c0"],
        chunk_ann_source_names=["s1"],
        chunk_ann_source_codes=np.asarray([0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1], dtype=np.int64),
        chunk_ann_handle=handle,
        chunk_ann_path="/nonexistent/chunk_ann.bin",
        manifest={"dim": 16, "has_chunk_ann_sources": True},
    )


class _SettingsProxy:
    """只覆盖 ``chunk_fanout_max_workers`` 的薄壳。该字段由并行任务 B2 加进
    ``Settings``;本用例用代理提供它,所以无论 B2 是否已落地都成立。"""

    def __init__(self, base, fanout):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "chunk_fanout_max_workers", fanout)

    def __getattr__(self, name):
        return getattr(self._base, name)


def _peek_returns(monkeypatch, candidates, index):
    """桩在 ``_RetrievalState`` 的一跳委托上,与 ``_scale_index`` 对称——两条腿
    因此是同一层的两个可替换座位,而不是「一个方法 vs 两跳属性链」。"""
    monkeypatch.setattr(
        candidates, "_peek_warm_chunk_index", lambda _notebook_id: index,
    )


def test_peek_only_never_loads_scale_index(repo, monkeypatch):
    """暖 ANN 在场:peek 腿借用它走 ANN,``_scale_index`` 一次都不被调。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    handle = _Ann()
    _forbid(
        monkeypatch, candidates, "_scale_index",
        "peek lane 不得冷加载 scale 索引",
    )
    _peek_returns(monkeypatch, candidates, _warm_index(handle))

    scored, _ids, _mat = _in_peek_context(
        candidates._retrieve_chunks_baseline, notebook.id, "bandgap",
        drifted=False,
    )

    assert handle.k_values, "暖 ANN 在场时 peek 腿必须走 ANN"
    assert {chunk.chunk_id for chunk in scored} <= {"c0", "c1", "c2"}


def test_peek_only_without_warm_ann_degrades_instead_of_bruteforce(
    repo, monkeypatch
):
    """暖 ANN 缺席:当场落 FTS 降级。小库(3 chunk、copyable)在默认腿上本该走
    有界暴力向量路径,所以 ``_vector_matrix``/``_gather_chunks`` 一被调用就是
    回归。``n_chunks`` 必须是 ``notebook_chunk_count`` 的真实计数,不是占位值。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    _forbid(
        monkeypatch, candidates, "_scale_index",
        "peek lane 不得冷加载 scale 索引",
    )
    _forbid(
        monkeypatch, candidates, "_vector_matrix",
        "peek lane 不得写共享向量矩阵缓存",
    )
    _forbid(
        monkeypatch, candidates, "_gather_chunks",
        "peek lane 不得整表读正文",
    )
    _peek_returns(monkeypatch, candidates, None)
    degraded = []
    original = candidates._retrieve_chunks_fts_degraded

    def _spy(*args, **kwargs):
        degraded.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(candidates, "_retrieve_chunks_fts_degraded", _spy)
    events = _capture_events(repo, monkeypatch)

    scored, _ids, _mat = _in_peek_context(
        candidates._retrieve_chunks_baseline, notebook.id, "bandgap",
        drifted=False,
    )

    assert len(degraded) == 1
    skipped = [
        event for event in events
        if event.get("kind") == "chunk_bruteforce_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["n_chunks"] == 3, "必须是整库真实计数,不得传占位值"
    assert skipped[0]["reason"] == "peek_no_warm_ann", (
        "索引可能建好且健康,只是此刻不常驻;发 large_library_no_ann 会让运维"
        "去建一个已经存在的索引"
    )
    assert len(scored) >= 1, "降级仍要给出 FTS 候选"


@pytest.mark.parametrize("arm", ["ann_off", "embed_failed"])
def test_peek_only_degrades_when_the_ann_branch_is_never_entered(
    repo, monkeypatch, arm
):
    """``chunk_ann_enabled=0`` **与** embed 失败(``query_vector is None``)时
    ANN 分支根本不进,「绝不暴力」这条约束照样成立——判据因此必须读在那个 if
    之外。两条臂各跑一次,而不是只跑一条再在 docstring 里声称覆盖两条。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    if arm == "ann_off":
        monkeypatch.setattr(candidates.settings, "chunk_ann_enabled", False)
    else:
        monkeypatch.setattr(candidates, "_embed_query", lambda _query: None)
    _forbid(
        monkeypatch, candidates, "_scale_index",
        "peek lane 不得冷加载 scale 索引",
    )
    _forbid(
        monkeypatch, candidates, "_gather_chunks",
        "peek lane 不得整表读正文",
    )
    events = _capture_events(repo, monkeypatch)

    _in_peek_context(
        candidates._retrieve_chunks_baseline, notebook.id, "bandgap",
        drifted=False,
    )

    skipped = [
        event for event in events
        if event.get("kind") == "chunk_bruteforce_skipped"
    ]
    assert skipped and skipped[0]["reason"] == "peek_no_warm_ann"
    assert skipped[0]["embed_ok"] is (arm == "ann_off")


def test_default_lane_still_loads_scale_index(repo, monkeypatch):
    """对照臂:不设 ContextVar → 逐字维持 ``_scale_index(nb, allow_stale=True)``,
    且一次都不碰 peek。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    seen = []

    def _scale_index(notebook_id, allow_stale=False):
        seen.append((notebook_id, allow_stale))
        return None

    monkeypatch.setattr(candidates, "_scale_index", _scale_index)
    _forbid(
        monkeypatch, candidates, "_peek_warm_chunk_index",
        "默认腿不得走 warm-peek",
    )
    events = _capture_events(repo, monkeypatch)

    candidates._retrieve_chunks_baseline(notebook.id, "bandgap", drifted=False)

    assert seen == [(notebook.id, True)]
    stages = [
        event for event in events
        if event.get("stage") == "chunk_scale_index"
    ]
    assert stages and all("lane" not in event for event in stages), (
        "默认腿的 ask_stage 事件不得带 lane 字段"
    )


def test_peek_lane_marks_its_scale_index_stage(repo, monkeypatch):
    """``stage`` 保持 ``chunk_scale_index``(diag_slow 有白名单),腿的区别走新增
    的 ``lane`` 字段:peek 量的是一次字典查找,混进常驻加载的分位会拉低 p50。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    _forbid(
        monkeypatch, candidates, "_scale_index",
        "peek lane 不得冷加载 scale 索引",
    )
    _peek_returns(monkeypatch, candidates, None)
    events = _capture_events(repo, monkeypatch)

    _in_peek_context(
        candidates._retrieve_chunks_baseline, notebook.id, "bandgap",
        drifted=False,
    )

    stages = [
        event for event in events
        if event.get("stage") == "chunk_scale_index"
    ]
    assert stages and all(event["lane"] == "peek" for event in stages)


def test_peek_lane_skips_the_supplemental_contributor_seam(repo, monkeypatch):
    """补召回接缝也受 lane 约束:peek 落空 → FTS 命中常低于触发阈值 → 否则就会
    对一个**外库**拉最多 ``generated_question_max_scan_rows + 1`` 行问题索引向量
    并全部解码,每个(库,子查询)一次、无 memo。越权面没变,但成本面正是这条 lane
    承诺不付的那一种。"""
    from app.services.retrieval_run import retrieval_run

    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    # 对照臂也要跑得通,所以这里不能禁 ``_scale_index``,只把它桩成"索引缺席":
    # 两条腿于是走到同一个降级返回,唯一的变量就是这条 lane。
    monkeypatch.setattr(
        candidates, "_scale_index", lambda _notebook_id, allow_stale=False: None,
    )
    _peek_returns(monkeypatch, candidates, None)
    monkeypatch.setattr(
        candidates.settings, "generated_question_index_mode", "shadow",
    )
    runs = []
    monkeypatch.setattr(
        candidates, "retrieval_contributors",
        SimpleNamespace(run=lambda *a, **kw: runs.append(kw) or []),
    )

    with retrieval_run(run_kind="ask", actor_id="user-local"):
        _in_peek_context(candidates._retrieve_chunks, notebook.id, "bandgap")
        assert runs == [], "peek 腿不得触达补召回 host"

        # 对照臂:同一个 host、同一次运行,默认腿照常调用。
        candidates._retrieve_chunks(notebook.id, "bandgap")

    assert len(runs) == 1


def test_fanout_default_is_a_single_shared_constant(repo):
    """``Settings`` 的默认值与 ``_retrieve_chunks_multi`` 的替身兜底必须同源:
    两处各写一个 8,其中一处被改时会静默分叉成两个并发上限。"""
    from app.core.config import DEFAULT_CHUNK_FANOUT_MAX_WORKERS

    assert repo.settings.chunk_fanout_max_workers == (
        DEFAULT_CHUNK_FANOUT_MAX_WORKERS
    )
    assert Settings().chunk_fanout_max_workers == (
        DEFAULT_CHUNK_FANOUT_MAX_WORKERS
    )


def test_peek_only_is_scoped_to_the_copied_context():
    assert rc._CHUNK_PEEK_ONLY.get() is False

    def _inside():
        rc._CHUNK_PEEK_ONLY.set(True)
        return rc._CHUNK_PEEK_ONLY.get()

    assert contextvars.copy_context().run(_inside) is True
    assert rc._CHUNK_PEEK_ONLY.get() is False, "副本里的 set 不得外泄"


def test_allowed_source_ids_reach_the_fts_producer_under_peek(
    repo, monkeypatch
):
    """降级腿上,来源天花板原样到达 ``_chunk_fts_hits``——在它的 ``k``/``LIMIT``
    之前,不是合并后的结果侧过滤。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    _forbid(
        monkeypatch, candidates, "_scale_index",
        "peek lane 不得冷加载 scale 索引",
    )
    monkeypatch.setattr(
        candidates.scale_runtime.catalog, "peek_warm_chunk_index",
        lambda _notebook_id: None,
    )
    seen = []
    original = candidates._chunk_fts_hits

    def _spy(db, notebook_id, query, *, k, allowed_source_ids=None,
             corpus_langs=None):
        seen.append(allowed_source_ids)
        return original(
            db, notebook_id, query, k=k,
            allowed_source_ids=allowed_source_ids, corpus_langs=corpus_langs,
        )

    monkeypatch.setattr(candidates, "_chunk_fts_hits", _spy)

    _in_peek_context(
        candidates._retrieve_chunks_baseline, notebook.id, "bandgap",
        allowed_source_ids=("s1",), producer_explicit=False, drifted=False,
    )

    assert seen == [("s1",)]


def test_allowed_source_ids_reach_the_ann_producer_under_peek(
    repo, monkeypatch
):
    """ANN 腿同款:清单在 ``_retrieve_chunks_ann`` 拿到 handle 之前就到位。"""
    notebook = _seed(repo)
    candidates = repo.retrieval.candidates
    _forbid(
        monkeypatch, candidates, "_scale_index",
        "peek lane 不得冷加载 scale 索引",
    )
    monkeypatch.setattr(
        candidates.scale_runtime.catalog, "peek_warm_chunk_index",
        lambda _notebook_id: _warm_index(_Ann()),
    )
    seen = []

    def _spy(_notebook_id, _query, _vector, _idx, _recall, *,
             allowed_source_ids=None, source_restricted=False):
        seen.append(allowed_source_ids)
        return ([], [], None)

    monkeypatch.setattr(candidates, "_retrieve_chunks_ann", _spy)

    _in_peek_context(
        candidates._retrieve_chunks_baseline, notebook.id, "bandgap",
        allowed_source_ids=("s1",), producer_explicit=False, drifted=False,
    )

    assert seen == [("s1",)]


@pytest.mark.parametrize("fanout", [2, 3])
def test_multi_fanout_executor_width_is_exactly_the_setting(
    repo, monkeypatch, fanout
):
    """上限的**确定性**断言:直接看执行器的构造参数。

    仅靠下面那条握手不足以钉死上界——池里多出来的工作线程未必会在握手窗口内
    被调度进来,于是「改回字面量 8」有相当概率仍然全绿(实测约三成)。"""
    import concurrent.futures

    candidates = repo.retrieval.candidates
    monkeypatch.setattr(
        candidates, "settings", _SettingsProxy(candidates.settings, fanout)
    )
    monkeypatch.setattr(candidates, "_retrieve_chunks",
                        lambda *_args, **_kwargs: ([], [], None))
    seen = []
    original = concurrent.futures.ThreadPoolExecutor

    def _record(*args, **kwargs):
        seen.append(kwargs.get("max_workers"))
        return original(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _record)

    candidates._retrieve_chunks_multi(
        "nb", [f"q{i}" for i in range(fanout * 2)], drifted=False
    )
    assert seen == [fanout], "超过上限的子查询数必须被压到设定上限"

    seen.clear()
    candidates._retrieve_chunks_multi("nb", ["only-one"], drifted=False)
    assert seen == [1], "子查询少于上限时取子查询数"


@pytest.mark.parametrize("fanout", [2, 3])
def test_multi_fanout_reads_the_setting(repo, monkeypatch, fanout):
    """并发**下界**:上限确实被用满了,不是被压到 1 顺序跑。

    握手不用墙钟:``Barrier(fanout)`` 只有在**恰好** fanout 个任务同时在跑时才
    放行,上限更小会停在 barrier(``broken``)。"""
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(
        candidates, "settings", _SettingsProxy(candidates.settings, fanout)
    )
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}
    broken = []
    gate = threading.Barrier(fanout, timeout=10)

    def _fake_retrieve_chunks(_notebook_id, _query, *_args, **_kwargs):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            broken.append(1)
        finally:
            with lock:
                live["now"] -= 1
        return ([], [], None)

    monkeypatch.setattr(candidates, "_retrieve_chunks", _fake_retrieve_chunks)

    candidates._retrieve_chunks_multi(
        "nb", [f"q{i}" for i in range(fanout * 2)], drifted=False
    )

    assert not broken, "并发不足以让 barrier 放行 → 上限没读到 settings"
    assert live["peak"] == fanout, "并发峰值必须正好是设定的上限"


@pytest.mark.parametrize("lane", ["bruteforce", "peek_fts", "peek_ann"])
def test_one_recall_leg_never_repeats_a_chunk_id(repo, monkeypatch, lane):
    """一条召回腿内 chunk_id 唯一 —— 三条 lane 各跑一次。

    ``chunk_federation._single_library_result`` 把 ``scored`` 按 chunk_id 装进
    ``collected``,所以一条腿内出现重复 id 会**静默丢候选**(后一条覆盖前一条),
    而 ANN∪FTS 的 union、暴力向量路径与 FTS 降级路径各自都有机会产出同一个
    chunk。这条断言把那个假设从注释变成用例。
    """
    notebook = _seed(repo, n=6)
    candidates = repo.retrieval.candidates
    if lane == "bruteforce":
        scored, _ids, _mat = candidates._retrieve_chunks(notebook.id, "bandgap")
    else:
        _forbid(
            monkeypatch, candidates, "_scale_index",
            "peek lane 不得冷加载 scale 索引",
        )
        _peek_returns(
            monkeypatch, candidates,
            _warm_index(_Ann()) if lane == "peek_ann" else None,
        )
        scored, _ids, _mat = _in_peek_context(
            candidates._retrieve_chunks, notebook.id, "bandgap",
        )

    ids = [chunk.chunk_id for chunk in scored]
    assert ids, f"{lane} 腿零候选,这条断言证不了任何事"
    assert len(ids) == len(set(ids)), f"{lane} 腿出现重复 chunk_id:{ids}"
