"""PR-B 任务 B5:KG-overlay 的参考库对象拿得到自己的原文。

`_chunk_kg_overlay` 今天就会返回挂载参考库的知识对象,但 `_kg_source_chunks`
把这些对象的 evidence element 反查回 chunk 时用的 SQL 钉死 `ce.notebook_id =
active`,所以「引了参考库的知识对象,却拿不出它的原文」。本文件钉住放开后的
五条不变量:

* 参考库对象的 evidence → 参考库自己的 chunk,且带 `notebook_id = base`;当前库
  的 chunk 仍不打标(空 = active,与 `chunk_federation` 同一判据);
* 不传 `object_owners` 时输出与今天**逐值相等**(含顺序);
* 混合 active/base 时 first-seen 序仍严格按 `object_ids` → 对象内 evidence 序 →
  该 element 的 chunk 列表序;
* 参考库的隐藏投影(memory/knowhow 来源)恒不出现,哪怕某个对象的 evidence 指到它;
* 库维度被取消勾选的参考库整库跳过,且对该库零读。

外加接线四条:`CHUNK_FEDERATION_ENABLED=0` 与「本轮没有外库对象」都传 `None`;
归属表**与 chunk 腿的参与集求交**(`CHUNK_FEDERATION_MAX_PARTICIPANTS` 对这条腿
同样是上界,超出的库零读);逐库天花板与联邦向量腿**共用同一个 run 级 memo
key**(run 中途新增的来源不进本轮);以及 `_mix_retrieve` 真跑一遍时参考库的原文
进入候选且归属正确。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.models.source_scope import BaseNotebookScope
from app.services.embedding import FakeEmbedder
from app.services.retrieval_run import memoized_retrieval_value, retrieval_run
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


_NOW = "2026-09-19T00:00:00+00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    instance.settings.query_rewrite_enabled = False
    return instance


def _seed_source(repo, notebook_id: str, source_id: str, *,
                 source_type: str = "markdown") -> str:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, source_type, "ready",
             "parsed", _NOW, _NOW),
        )
    return source_id


def _seed_chunks(repo, notebook_id: str, source_id: str, spec) -> None:
    with repo._write() as db:
        for chunk_id, element_ids in spec:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, notebook_id, source_id, f"{chunk_id} 的正文", "",
                 json.dumps(list(element_ids)), _NOW),
            )


def _seed_object(repo, notebook_id: str, object_id: str, source_id: str,
                 element_ids) -> str:
    """直插一个 KG 对象,evidence 逐条指向给定的 element,顺序即入参顺序。"""
    evidence = json.dumps([
        {"source_id": source_id, "source_title": "", "element_id": element_id,
         "element_type": "paragraph", "location_label": "p",
         "quoted_span": "q", "confidence": 1.0}
        for element_id in element_ids
    ])
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
            "owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (object_id, notebook_id, "concept", "approved", "",
             json.dumps({"name": object_id}), evidence, source_id, _NOW, _NOW),
        )
    return object_id


def _backfill(repo, notebook_id: str) -> None:
    """跑一次离线回填,让该库走 ``chunk_elements`` 有界点查。

    ``force=True``:用例直插 chunks(不走 ``replace_source_chunks`` 的前向维护),
    所以补种之后必须重建,否则第二次调用会以「已完成」跳过,新 chunk 的反查行
    根本不存在 —— 那会让一条断言因为索引缺行而不是因为被测规则而成立。
    """
    repo.maintenance.begin_chunk_element_backfill(notebook_id, force=True)
    progress = repo.maintenance.resume_chunk_element_backfill_batch(
        notebook_id, batch_size=2000
    )
    while progress["status"] == "running":
        progress = repo.maintenance.resume_chunk_element_backfill_batch(
            notebook_id, batch_size=2000
        )


@pytest.fixture
def mounted(repo):
    """(active, base):两边各一篇来源、各一个 KG 对象,base 已挂载并已回填。

    active: src-act / ck-act-1(el-act-1) / 对象 o-act
    base:   src-base / ck-base-1(el-base-1)、ck-base-2(el-base-1) / 对象 o-base
    """
    base = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="我的笔记本"))
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    repo.collection_catalog.invalidate()

    _seed_source(repo, active.id, "src-act")
    _seed_chunks(repo, active.id, "src-act", [("ck-act-1", ["el-act-1"])])
    _seed_object(repo, active.id, "o-act", "src-act", ["el-act-1"])

    _seed_source(repo, base.id, "src-base")
    _seed_chunks(repo, base.id, "src-base",
                 [("ck-base-1", ["el-base-1"]), ("ck-base-2", ["el-base-1"])])
    _seed_object(repo, base.id, "o-base", "src-base", ["el-base-1"])

    for notebook_id in (active.id, base.id):
        _backfill(repo, notebook_id)
    return active.id, base.id


# ---------------------------------------------------------------------------
# 1. 参考库对象的 evidence 反查到参考库自己的 chunk
# ---------------------------------------------------------------------------


def test_base_object_evidence_resolves_to_base_chunk(repo, mounted):
    active, base = mounted
    graph = repo.retrieval.graph

    chunks = graph._kg_source_chunks(
        active, ["o-base", "o-act"],
        object_owners={"o-base": base, "o-act": active},
    )

    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    assert set(by_id) == {"ck-base-1", "ck-base-2", "ck-act-1"}
    # 参考库的 chunk 打自己的 id;当前库的不打标(空 = active)。
    assert by_id["ck-base-1"].notebook_id == base
    assert by_id["ck-base-2"].notebook_id == base
    assert by_id["ck-act-1"].notebook_id == ""
    # 原文确实是参考库那一篇,不是同名的 active 行。
    assert by_id["ck-base-1"].source_id == "src-base"


def test_without_owner_map_base_object_still_resolves_to_nothing(repo, mounted):
    """今天的形态:不传归属 → 参考库对象的 evidence 反查不到任何 chunk。

    这条不是回归护栏,而是把「本任务修的到底是什么」钉成可执行的对照。
    """
    active, _base = mounted
    assert repo.retrieval.graph._kg_source_chunks(active, ["o-base"]) == []


# ---------------------------------------------------------------------------
# 2. 不传 object_owners → 逐值相等
# ---------------------------------------------------------------------------


def test_owner_map_absent_is_byte_identical(repo, mounted):
    active, base = mounted
    graph = repo.retrieval.graph
    object_ids = ["o-act", "o-base"]

    legacy = graph._kg_source_chunks(active, object_ids)
    # 全部对象都归 active 的归属表 = 语义上的同一件事,必须逐值相等。
    same = graph._kg_source_chunks(
        active, object_ids, object_owners={"o-act": active, "o-base": active},
    )

    assert legacy == same
    assert [chunk.chunk_id for chunk in legacy] == ["ck-act-1"]
    assert all(chunk.notebook_id == "" for chunk in legacy)


# ---------------------------------------------------------------------------
# 3. first-seen 序
# ---------------------------------------------------------------------------


def test_output_order_follows_object_ids_then_evidence_then_chunk(repo, mounted):
    """object_ids 输入序 → 对象内 evidence 序 → 该 element 的 chunk 列表序。

    三层都要能被单独证伪,所以夹具给每一层都留了一个可换的维度:
    两个对象(跨库)、对象内两个 element、单个 element 下两个 chunk。
    """
    active, base = mounted
    _seed_chunks(repo, active, "src-act", [("ck-act-2", ["el-act-2"])])
    # 对象内 evidence 序:el-act-2 在前、el-act-1 在后。
    _seed_object(repo, active, "o-act2", "src-act", ["el-act-2", "el-act-1"])
    _backfill(repo, active)
    graph = repo.retrieval.graph
    owners = {"o-act2": active, "o-base": base}

    forward = graph._kg_source_chunks(
        active, ["o-act2", "o-base"], object_owners=owners,
    )
    reverse = graph._kg_source_chunks(
        active, ["o-base", "o-act2"], object_owners=owners,
    )

    assert [chunk.chunk_id for chunk in forward] == [
        "ck-act-2",            # o-act2 的第一条 evidence
        "ck-act-1",            # o-act2 的第二条 evidence
        "ck-base-1", "ck-base-2",   # o-base 的 element,chunk 列表序
    ]
    # object_ids 换序 → 输出整体换序,证明外层循环才是第一层排序键。
    assert [chunk.chunk_id for chunk in reverse] == [
        "ck-base-1", "ck-base-2", "ck-act-2", "ck-act-1",
    ]


# ---------------------------------------------------------------------------
# 4. 参考库的隐藏投影恒不出现
# ---------------------------------------------------------------------------


def test_base_hidden_projection_never_resolves(repo, mounted):
    """base 里一条 memory 类型来源的 chunk,即使被 KG 对象的 evidence 指到。

    `all_visible_source_ids` 按构造排除 memory/knowhow;请求用户通常不是参考库
    的成员,那个库的 Memory 投影今天到不了 chunk 通道,这条反查也不许放进来。
    """
    active, base = mounted
    _seed_source(repo, base, "src-base-mem", source_type="memory")
    _seed_chunks(repo, base, "src-base-mem", [("ck-base-mem", ["el-base-mem"])])
    _seed_object(repo, base, "o-base-mem", "src-base-mem",
                 ["el-base-mem", "el-base-1"])
    _backfill(repo, base)

    chunks = repo.retrieval.graph._kg_source_chunks(
        active, ["o-base-mem"], object_owners={"o-base-mem": base},
    )

    ids = [chunk.chunk_id for chunk in chunks]
    assert "ck-base-mem" not in ids
    # 同一个对象的可见 evidence 仍然照常反查得到 —— 排除的是来源,不是对象。
    assert ids == ["ck-base-1", "ck-base-2"]


# ---------------------------------------------------------------------------
# 5. 取消勾选的参考库整库跳过,且零读
# ---------------------------------------------------------------------------


def test_unchecked_library_is_skipped_without_reads(repo, mounted, monkeypatch):
    active, base = mounted
    graph = repo.retrieval.graph
    visible_calls: list[str] = []
    element_calls: list[str] = []
    original_visible = graph.sources.all_visible_source_ids
    original_elements = graph.chunks.chunks_for_element_ids
    monkeypatch.setattr(
        graph.sources, "all_visible_source_ids",
        lambda nid: (visible_calls.append(nid), original_visible(nid))[1],
    )
    monkeypatch.setattr(
        graph.chunks, "chunks_for_element_ids",
        lambda db, nid, elems: (
            element_calls.append(nid), original_elements(db, nid, elems)
        )[1],
    )

    with source_scope_context(
        active, None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        chunks = graph._kg_source_chunks(
            active, ["o-base", "o-act"],
            object_owners={"o-base": base, "o-act": active},
        )

    assert [chunk.chunk_id for chunk in chunks] == ["ck-act-1"]
    # 库维度先判,所以被排除的库连一次来源枚举都不该发生。
    assert base not in visible_calls
    assert base not in element_calls


# ---------------------------------------------------------------------------
# 6. 未回填的参考库不触发整库扫描
# ---------------------------------------------------------------------------


def test_unbackfilled_peer_never_full_scans(repo, mounted, monkeypatch):
    """回填标记缺失的参考库这一轮空手,而不是去扫它整库的 chunks。

    `_elem_chunks_scoped` 的另一支是 `_elem_chunk_map` —— 对整库 chunks 的全量
    扫描 + 逐行 json.loads。那是联邦不该强加给一个用户并不在其中的外库的成本。
    """
    active, base = mounted
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET chunk_elements_indexed=0 "
            "WHERE notebook_id=?", (base,),
        )

    def _fail(notebook_id):  # pragma: no cover - 断言就是它不被调用
        raise AssertionError(f"peer full scan: {notebook_id}")

    monkeypatch.setattr(repo.retrieval.graph, "_elem_chunk_map", _fail)
    chunks = repo.retrieval.graph._kg_source_chunks(
        active, ["o-base"], object_owners={"o-base": base},
    )

    assert chunks == []


# ---------------------------------------------------------------------------
# 7. 逐库天花板与联邦向量腿共用同一个 run 级 memo key
# ---------------------------------------------------------------------------


def test_peer_ceiling_reuses_the_federated_chunk_memo_key(repo, mounted):
    """run 中途给参考库新增的可见来源,不进本轮的 KG 反查。

    这条守的是 `_kg_peer_source_ceilings` 的 docstring 写成不变量的那件事:
    它与联邦 chunk 腿共用 `("federated_chunk_visible", nid)` 这一个 memo key,
    所以一次 ask 里两条腿看到的是**同一份**冻结快照。直读 `all_visible_source_ids`
    或换一个 key 都会让这条断言红——run 中途的上传会当场放宽一次在飞的检索,
    而向量腿那边仍是冻结的,「向量腿引不到、KG 腿引得到」的不对称就出现了。
    """
    active, base = mounted
    with retrieval_run(run_kind="ask_chunk"):
        # 向量腿在 `_federated_tasks` 里就是这样冻住每个 peer 的来源清单的。
        frozen = memoized_retrieval_value(
            ("federated_chunk_visible", base),
            lambda: tuple(repo.retrieval.graph.sources.all_visible_source_ids(base)),
        )
        assert "src-base-late" not in frozen

        # run 中途上传:新来源 + 它的 chunk + 指到它的 KG evidence。
        _seed_source(repo, base, "src-base-late")
        _seed_chunks(repo, base, "src-base-late",
                     [("ck-base-late", ["el-base-late"])])
        _seed_object(repo, base, "o-base-late", "src-base-late",
                     ["el-base-late", "el-base-1"])
        _backfill(repo, base)

        chunks = repo.retrieval.graph._kg_source_chunks(
            active, ["o-base-late"], object_owners={"o-base-late": base},
        )

    ids = [chunk.chunk_id for chunk in chunks]
    assert "ck-base-late" not in ids
    # 冻结清单里那条来源的原文照常反查得到 —— 冻的是清单,不是整条通道。
    assert ids == ["ck-base-1", "ck-base-2"]


# ---------------------------------------------------------------------------
# 8. 归属表 ⊆ chunk 腿的参与集
# ---------------------------------------------------------------------------


@pytest.fixture
def three_bases(repo):
    """active 挂 3 个都有 KG 与原文的参考库,挂载顺序即参与集顺序。"""
    active = repo.create_notebook(NotebookCreate(name="我的笔记本"))
    bases = []
    for index in (1, 2, 3):
        base = repo.create_notebook(NotebookCreate(name=f"参考库{index}"))
        repo.mark_notebook_base(base.id)
        source_id = f"src-b{index}"
        _seed_source(repo, base.id, source_id)
        _seed_chunks(repo, base.id, source_id, [(f"ck-b{index}", [f"el-b{index}"])])
        _seed_object(repo, base.id, f"o-b{index}", source_id, [f"el-b{index}"])
        _backfill(repo, base.id)
        bases.append(base.id)
    repo.replace_notebook_bases(active.id, bases, "user-local")
    repo.collection_catalog.invalidate()
    return active.id, bases


def test_owner_map_is_intersected_with_the_participant_set(
    repo, three_bases, monkeypatch
):
    """上界设 2(active + 1 个参考库)→ 第 3 个库的对象零读、不出原文。

    `CHUNK_FEDERATION_MAX_PARTICIPANTS` 文档写的是**上界**。不求交的话向量腿
    只搜被允许的那些库,KG 腿却照样为超出上界的库反查原文并送进答案——那个
    旋钮对这条腿就是失效的。`notebook_in_scope` 顶不上:没提交 `base_scope`
    时它对任意 id 恒真。
    """
    active, bases = three_bases
    candidates = repo.retrieval.candidates
    repo.settings.chunk_federation_max_participants = 2
    id_map = {
        f"k{index}": {"object_id": f"o-b{index}", "notebook_id": base}
        for index, base in enumerate(bases, start=1)
    }

    owners = candidates._kg_object_owners(active, id_map)

    # active + 首个挂载库 = 2 个席位;其余两个落回 active。
    assert owners == {
        "o-b1": bases[0], "o-b2": active, "o-b3": active,
    }

    visible_calls: list[str] = []
    original = repo.retrieval.graph.sources.all_visible_source_ids
    monkeypatch.setattr(
        repo.retrieval.graph.sources, "all_visible_source_ids",
        lambda nid: (visible_calls.append(nid), original(nid))[1],
    )
    chunks = repo.retrieval.graph._kg_source_chunks(
        active, ["o-b1", "o-b2", "o-b3"], object_owners=owners,
    )

    assert [chunk.chunk_id for chunk in chunks] == ["ck-b1"]
    # 超出上界的库连一次来源枚举都不该发生。
    assert visible_calls == [bases[0]]


def _collect_events(repo, monkeypatch) -> list:
    """收事件,并且**用 monkeypatch 还原** —— `event_log` 是 runtime 级对象,
    裸赋值会把替身留给同一个 worker 后面的用例。"""
    events: list = []
    monkeypatch.setattr(
        repo.retrieval.candidates.event_log, "emit", events.append,
    )
    return events


def test_participant_ids_never_emit_the_truncation_event(
    repo, three_bases, monkeypatch
):
    """只问成员资格的那一口不发 `chunk_federation_truncated`。

    同一次 ask 里真正扇出的向量腿已经发过一次;KG 腿再发一次,会把一次截断
    在事件流里读成两次。
    """
    from app.services.chunk_federation import (
        federation_participant_ids, federation_participants,
    )

    active, bases = three_bases
    candidates = repo.retrieval.candidates
    repo.settings.chunk_federation_max_participants = 2
    events = _collect_events(repo, monkeypatch)

    assert federation_participant_ids(candidates, active) == {active, bases[0]}
    assert events == []

    # 对照:announcing 的那一口照发不误,事件本身没被删掉。
    federation_participants(candidates, active)
    assert [event["kind"] for event in events] == ["chunk_federation_truncated"]
    assert events[0]["participants"] == 4 and events[0]["kept"] == 2


def test_one_ask_announces_one_truncation(repo, three_bases, monkeypatch):
    """一次 `_mix_retrieve` 里 `chunk_federation_truncated` 恰好一条。

    向量腿与 KG 腿读的是同一个参与集,但只有真正扇出的那条腿该 announce。
    KG 腿若改用 announcing 的那一口,同一次截断会在事件流里出现两次,运维
    读到的「参与库被截断」次数就是实际的两倍。
    """
    active, _bases = three_bases
    candidates = repo.retrieval.candidates
    repo.settings.chunk_federation_max_participants = 2
    events = _collect_events(repo, monkeypatch)

    candidates._mix_retrieve(active, "o-b1", "", ["o-b1"])

    truncations = [
        event for event in events
        if event.get("kind") == "chunk_federation_truncated"
    ]
    assert len(truncations) == 1, events


# ---------------------------------------------------------------------------
# 9. _mix_retrieve 的接线
# ---------------------------------------------------------------------------


def _capture_owners(repo, monkeypatch) -> list:
    captured: list = []
    original = repo.retrieval.graph._kg_source_chunks

    def _spy(notebook_id, object_ids, *, support_by_object=None,
             object_owners=None):
        captured.append(object_owners)
        return original(
            notebook_id, object_ids, support_by_object=support_by_object,
            object_owners=object_owners,
        )

    monkeypatch.setattr(repo.retrieval.graph, "_kg_source_chunks", _spy)
    return captured


def test_feature_flag_off_passes_no_owner_map(repo, mounted, monkeypatch):
    active, base = mounted
    repo.settings.chunk_federation_enabled = False
    captured = _capture_owners(repo, monkeypatch)

    repo.retrieval.candidates._mix_retrieve(active, "concept", "", ["concept"])

    assert captured and all(owners is None for owners in captured)
    # 直接问一次归属表本身,避免用例只依赖 overlay 这一轮恰好命中了什么。
    assert repo.retrieval.candidates._kg_object_owners(
        active, {"k1": {"object_id": "o-base", "notebook_id": base}},
    ) is None


def test_owner_map_is_none_without_any_foreign_object(repo, mounted):
    active, _base = mounted
    owners = repo.retrieval.candidates._kg_object_owners(
        active, {"k1": {"object_id": "o-act", "notebook_id": ""}},
    )
    assert owners is None


def test_owner_map_normalises_the_active_empty_id(repo, mounted):
    active, base = mounted
    owners = repo.retrieval.candidates._kg_object_owners(
        active,
        {"k1": {"object_id": "o-act", "notebook_id": ""},
         "k2": {"object_id": "o-base", "notebook_id": base}},
    )
    assert owners == {"o-act": active, "o-base": base}


def test_mix_retrieve_carries_base_passages_with_their_origin(
    repo, mounted, monkeypatch
):
    """端到端:mix 分支真跑一遍,参考库的原文进入候选且引用归属正确。

    向量腿不打桩 —— 联邦 chunk 通道本来也会带回 `ck-base-*`,两条腿撞上同一条
    原文时 `prefer_stronger_chunk_candidate` 取并集,所以 `kg_source` 这条来路
    是 KG 腿真的跑通了的证据,而不是向量腿的副产品。
    """
    active, base = mounted
    captured = _capture_owners(repo, monkeypatch)

    merged, _block, id_map, _hits, _ppr = repo.retrieval.candidates._mix_retrieve(
        active, "o-base", "", ["o-base"],
    )

    assert any(
        owners and owners.get("o-base") == base for owners in captured
    ), captured
    assert any(entry.get("object_id") == "o-base" for entry in id_map.values())
    base_chunks = [chunk for chunk in merged if chunk.chunk_id.startswith("ck-base")]
    assert base_chunks, [chunk.chunk_id for chunk in merged]
    assert all(chunk.notebook_id == base for chunk in base_chunks)
    assert any(
        support.origin == "kg_source"
        for chunk in base_chunks for support in chunk.retrieval_supports
    )


# ---------------------------------------------------------------------------
# 12. 逐 owner 的准备失败被隔离
# ---------------------------------------------------------------------------


def test_one_owners_unreadable_ceiling_never_takes_down_the_lane(
    repo, mounted, monkeypatch
):
    """某个参考库的来源枚举抛错(语句超时之类)→ 只有它这一轮空手。

    active 与其余 owner 照常反查,发一条内容无关事件;绝不退化成「读不到清单
    就不带天花板去查它」——那会把一次读失败变成一次比正常路径更宽的检索。
    """
    active, base = mounted
    graph = repo.retrieval.graph
    events = _collect_events(repo, monkeypatch)
    element_calls: list[str] = []
    original_elements = graph.chunks.chunks_for_element_ids
    monkeypatch.setattr(
        graph.chunks, "chunks_for_element_ids",
        lambda db, nid, elems: (
            element_calls.append(nid), original_elements(db, nid, elems)
        )[1],
    )
    monkeypatch.setattr(
        graph.sources, "all_visible_source_ids",
        lambda nid: (_ for _ in ()).throw(
            RuntimeError("statement timeout on src-base")
        ),
    )

    chunks = graph._kg_source_chunks(
        active, ["o-base", "o-act"],
        object_owners={"o-base": base, "o-act": active},
    )

    assert [chunk.chunk_id for chunk in chunks] == ["ck-act-1"]
    assert base not in element_calls, "读不到天花板的库不得被无天花板地反查"
    skipped = [
        event for event in events
        if event.get("kind") == "kg_peer_ceiling_skipped"
    ]
    assert len(skipped) == 1
    event = dict(skipped[0])
    assert isinstance(event.pop("latency_ms"), int)
    assert event == {
        "kind": "kg_peer_ceiling_skipped", "notebook_id": base,
        "error_type": "RuntimeError",
    }
    assert "timeout" not in repr(events), "事件不得携带异常消息"


def test_cancellation_while_preparing_an_owner_is_not_swallowed(
    repo, mounted, monkeypatch
):
    from app.services.cancellation import AskCancelled

    active, base = mounted
    graph = repo.retrieval.graph
    monkeypatch.setattr(
        graph.sources, "all_visible_source_ids",
        lambda nid: (_ for _ in ()).throw(AskCancelled()),
    )

    with pytest.raises(AskCancelled):
        graph._kg_source_chunks(
            active, ["o-base", "o-act"],
            object_owners={"o-base": base, "o-act": active},
        )
