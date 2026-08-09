"""PR-C(审计批3):增量融合有界化 —— 融合产出逐位不变的差分与有界守卫。

背景:``incremental_fuse_source`` 每次来源上传都要
  1. ``cluster_map(notebook_id)`` 整表物化 {member→canonical}(生产 9.1M 行 ≈2GB),
     而且每次 append 都推进 ``cluster_mutation_seq`` 当场作废版本缓存 → 下一个来源
     再付一遍(fold 路径按 delta 来源循环调它 = O(D×全库));
  2. ``merge_candidate_pairs`` 全表扫 ×2~3;
  3. ``insert_clusters`` 在**写事务内**读整个 (notebook, object_type) 切片做幂等去重。

本 PR 把这三处换成按本次新对象/候选 id 的定点查询。第一验收是**产出逐位一致**,
所以本文件分两半:

* 差分半 —— 把被替换的四个数据供给接缝**打回旧的全表形态**,在同一批夹具上跑同一份
  融合代码,比对 append 的 cluster 行、merge candidates、事件。除这四个接缝外融合路径
  逐字未改,故这组比对就是"新旧实现输出一致"的判据;第五个接缝(``place_new_concepts``
  丢掉的 ``existing_cids`` 守卫)是真的删了一段逻辑,用 master 的冻结拷贝单独钉。
* 守卫半 —— 计数适配器断言融合路径不再发全表读,``insert_clusters`` 读取行数 ≤ 本次
  插入行数,以及 id 列表确实分批。
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients

NOW = "2026-08-08T00:00:00"
DIM = 16


@pytest.fixture
def make_repo(tmp_path, monkeypatch):
    """独立 repository 工厂 —— 差分要两套完全隔离的库。

    ``knowledge_objects.id`` 是全局主键,同一个库里塞不下两份同 id 的夹具;而
    ``seed_or_unique`` 的空 seed 哨兵会把 object id 编进 canonical id,靠前缀改写
    再回退比较很容易把差异一起改没。两个库、同样的 id,产出可以直接逐位比。"""
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(DIM))
    made = []

    def factory(tag):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f'{tag}.db'}")
        repository = SQLiteRepository(Settings(_env_file=None))
        bind_all_embedding_clients(repository, FakeEmbedder(dim=DIM))
        made.append(repository)
        return repository

    return factory


@pytest.fixture
def repo(make_repo):
    return make_repo("single")


# ── master 冻结拷贝:place_new_concepts 的旧签名/旧函数体 ────────────────────

def _legacy_place_new_concepts(new_objects, existing_cluster_map, existing_canon_names,
                               *, seed_fn, id_prefix="K-"):
    """origin/master 的 kg_merge.place_new_concepts,逐字拷贝(仅改名)。"""
    from app.services.kg_merge import (
        build_acronym_alias_map, seed_or_unique, _seed_with_alias,
    )

    existing_cids = set(existing_cluster_map.values())
    alias_map = build_acronym_alias_map(
        [o.get("name", "") for o in new_objects] + list(existing_canon_names.values()))
    rows = []
    for o in new_objects:
        name = o.get("name", "")
        seed = seed_or_unique(_seed_with_alias(o, seed_fn, alias_map), o["object_id"])
        cid = f"{id_prefix}{seed}"
        canon_name = existing_canon_names.get(cid, name) if cid in existing_cids else name
        rows.append({"canonical_id": cid, "member_object_id": o["object_id"],
                     "canonical_name": canon_name})
    return rows


# ── 夹具:两个内容完全相同的 notebook,一个跑旧接缝一个跑新接缝 ──────────────

def _seed(repo, notebook_id, objects, clusters, candidates=()):
    """objects: [(oid, type, name, source_id, payload_extra, vector|None)]
    clusters: [(canonical_id, member_object_id, canonical_name, object_type)]
    candidates: [(canonical_a, canonical_b, status)]"""
    with repo._write() as db:
        for index, (oid, otype, name, src, extra, vec) in enumerate(objects):
            payload = {"name": name}
            payload.update(extra or {})
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                "payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, notebook_id, otype, "approved", "", json.dumps(payload), "[]",
                 src, NOW, NOW))
            if vec is not None:
                db.execute(
                    "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                    "VALUES (?,?,?,?)", (oid, notebook_id, json.dumps(vec), NOW))
        for index, (cid, member, cname, otype) in enumerate(clusters):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,canonical_description,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"cc-{notebook_id}-{index}", notebook_id, cid, member, cname, otype, "", NOW))
        for index, (a, b, status) in enumerate(candidates):
            db.execute(
                "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,"
                "score,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"cm-{notebook_id}-{index}", notebook_id, a, b, 0.9, status, NOW, NOW))


def _fusion_output(repo, notebook_id):
    """融合产出的规范化快照:代理 id/时间戳不参与比较(它们本就每次不同)。"""
    with repo._connect() as db:
        clusters = [
            (r["canonical_id"], r["member_object_id"], r["canonical_name"], r["object_type"])
            for r in db.execute(
                "SELECT canonical_id, member_object_id, canonical_name, object_type "
                "FROM concept_clusters WHERE notebook_id=? "
                "ORDER BY object_type, canonical_id, member_object_id", (notebook_id,))
        ]
        candidates = [
            (r["canonical_a"], r["canonical_b"], round(float(r["score"]), 6), r["status"])
            for r in db.execute(
                "SELECT canonical_a, canonical_b, score, status FROM concept_merge_candidates "
                "WHERE notebook_id=? ORDER BY canonical_a, canonical_b, status", (notebook_id,))
        ]
    return {"clusters": clusters, "candidates": candidates}


def _legacy_seams(repo, monkeypatch):
    """把本 PR 改掉的四个数据供给接缝打回 origin/master 的全表形态。

    融合路径的其余每一行都逐字未改,所以「打回接缝后产出相同」正是「有界化没有
    改变任何可观察结果」的判据。

    ⚠ 接缝的**时刻**和它的形状一样重要(评审 P1-2:第一版这里每次调用都当场重读
    整表,于是 legacy 臂也看得见 append 刚写进去的新簇行,两臂一起错、差分全绿)。
    master 的真实形态是「``incremental_fuse_source`` 入口读**一次**整表 cluster_map,
    Tier1 放置与 Tier2 桥接全程用这**一份**快照」,所以这里在方法入口冻结一次、
    后续从快照取。master 的读点严格说在 orphan sweep 之后,本文件的夹具没有悬空
    成员行(每个 cluster 成员都对应一条 knowledge_objects),两者等价。"""
    service = repo._runtime.knowledge_lifecycle
    store = repo._runtime.governance
    snapshot: dict = {}

    # ⓪ 时刻接缝:入口冻结一份整表快照,①②④ 全部从它取。
    original_fuse = service.incremental_fuse_source

    def fuse_with_entry_snapshot(notebook_id, source_id):
        with repo._connect() as db:
            snapshot.clear()
            snapshot.update(service.unified_kg.cluster_map_rows(db, notebook_id))
        return original_fuse(notebook_id, source_id)

    monkeypatch.setattr(service, "incremental_fuse_source", fuse_with_entry_snapshot)

    # ① 排除集:忽略 canonical id 上界,读整本库的候选对(master 的 merge_candidate_pairs)。
    def legacy_exclude_rows(db, notebook_id, statuses, canonical_ids):
        return store.merge_candidate_pairs(db, notebook_id, statuses)

    monkeypatch.setattr(service, "_bridge_exclude_rows", legacy_exclude_rows)

    # ② canonical 折叠:返回**入口冻结的**整表 cluster_map(master 传进 Tier2 的那份)。
    def legacy_fold(db, notebook_id, object_ids):
        return dict(snapshot)

    monkeypatch.setattr(service, "_fold_canonical_ids", legacy_fold)

    # ②' 冻结接缝本身在 legacy 臂上必须**不存在**:master 没有这一步,Tier2 的 memo
    #     初值是空的,同源新对象的 canonical 全靠 ① 的快照决定(那份快照里它们没有
    #     簇行 → 被跳过)。返回 {} 而不是 pre-append 折叠,才是 master 的形态。
    monkeypatch.setattr(service, "_freeze_new_object_canonicals",
                        lambda notebook_id, new_objs: {})

    # ③ append 幂等去重:读整个 (notebook, object_type) 切片(master 的 insert_clusters)。
    def legacy_existing_members(connection, notebook_id, object_type, rows):
        return {r["member_object_id"] for r in connection.execute(
            "SELECT member_object_id FROM concept_clusters "
            "WHERE notebook_id=? AND object_type=?",
            (notebook_id, object_type)).fetchall()}

    monkeypatch.setattr(store, "_existing_cluster_members", legacy_existing_members)

    # ④ place_new_concepts:master 的旧签名 + existing_cids 守卫,cmap 取入口快照。
    import app.services.kg_merge as kg_merge

    def legacy_place(new_objects, existing_canon_names, *, seed_fn, id_prefix="K-"):
        return _legacy_place_new_concepts(
            new_objects, dict(snapshot), existing_canon_names,
            seed_fn=seed_fn, id_prefix=id_prefix)

    monkeypatch.setattr(kg_merge, "place_new_concepts", legacy_place)


def _run_differential(make_repo, monkeypatch, objects, clusters, candidates=(), *,
                      fuse_source="src-B", before=None, settings=None):
    """同一批数据建两个隔离的库,一个走旧接缝一个走新接缝,返回两份产出快照。"""
    outputs = []
    for tag, legacy in (("bounded", False), ("legacy", True)):
        repository = make_repo(tag)
        for key, value in (settings or {}).items():
            setattr(repository.settings, key, value)
        notebook = repository.create_notebook(NotebookCreate(name=tag))
        _seed(repository, notebook.id, objects, clusters, candidates)
        if before is not None:
            before(repository, notebook.id)
        events = []
        with monkeypatch.context() as patch:
            # 事件也是融合产出的一部分(tier2_skipped 等);notebook id 两库不同,
            # 比较前剥掉。
            patch.setattr(repository._runtime.knowledge_lifecycle.event_log, "emit",
                          lambda payload: events.append(
                              {k: v for k, v in dict(payload).items()
                               if k != "notebook_id"}))
            if legacy:
                _legacy_seams(repository, patch)
            repository.incremental_fuse_source(notebook.id, fuse_source)
        outputs.append({**_fusion_output(repository, notebook.id), "events": events})
    bounded, legacy_out = outputs
    return legacy_out, bounded


# ── 差分夹具 ────────────────────────────────────────────────────────────────

def test_differential_first_fuse_into_empty_library(make_repo, monkeypatch):
    """新库首融:没有任何既有簇行,全部走「建新簇」分支。"""
    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-b1", "concept", "Charge Pump", "src-B", None, None),
                 ("ko-b2", "concept", "Charge Pump", "src-B", None, None),
                 ("ko-b3", "concept", "→", "src-B", None, None)],
        clusters=[])
    assert legacy == bounded
    assert len(bounded["clusters"]) == 3


def test_differential_same_name_across_sources_appends_to_existing_cluster(make_repo, monkeypatch):
    """同名跨来源概念:新对象必须落进既有 K- 簇并复用簇名(canon_name 复用分支)。"""
    from app.services.kg_merge import _norm

    cid = "K-" + _norm("Mixture-of-Experts (MoE)")
    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-a", "concept", "Mixture-of-Experts (MoE)", "src-A", None, None),
                 ("ko-b", "concept", "Mixture-of-Experts (MoE)", "src-B", None, None)],
        clusters=[(cid, "ko-a", "Mixture-of-Experts (MoE)", "concept")])
    assert legacy == bounded
    assert (cid, "ko-b", "Mixture-of-Experts (MoE)", "concept") in bounded["clusters"]


def test_differential_acronym_alias_against_existing_canonical_names(make_repo, monkeypatch):
    """已登记的**未收窄**读的回归钉:裸缩写新概念要靠既有簇名里的
    「Full (ACR)」定义并进 Full 的簇 —— 这正是 acronym 别名表必须吃整份既有簇名的
    原因。收窄掉那一读就会让 ko-acr 自成 "K-csa" 簇,本用例立刻变红。"""
    from app.services.kg_merge import _norm

    expansion_cid = "K-" + _norm("Compressed Sparse Attention (CSA)")
    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-full", "concept", "Compressed Sparse Attention (CSA)", "src-A", None, None),
                 ("ko-acr", "concept", "CSA", "src-B", None, None)],
        clusters=[(expansion_cid, "ko-full", "Compressed Sparse Attention (CSA)", "concept")])
    assert legacy == bounded
    assert (expansion_cid, "ko-acr", "Compressed Sparse Attention (CSA)", "concept") \
        in bounded["clusters"]


def test_differential_non_concept_types(make_repo, monkeypatch):
    """claim/formula/procedure 三个非 concept 类型(各自 KL-/KF-/KP- 号段)。"""
    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[
            ("kl-a", "claim", "gain is 40 dB", "src-A", None, None),
            ("kl-b", "claim", "gain is 40 dB", "src-B", None, None),
            ("kf-b", "formula", "V = I * R", "src-B", None, None),
            ("kp-b", "procedure", "calibrate ADC", "src-B",
             {"steps": [{"name": "sample"}, {"name": "set ref"}]}, None),
        ],
        clusters=[("KL-gain is 40 db", "kl-a", "gain is 40 dB", "claim")])
    assert legacy == bounded
    types = {row[3] for row in bounded["clusters"]}
    assert types == {"claim", "formula", "procedure"}


def test_differential_bruteforce_bridge_branch(make_repo, monkeypatch):
    """无 ANN 且实体数 ≤ max_entities → 暴力余弦分支,含既有 pending/已决排除集。"""
    from app.services.kg_merge import _norm

    near = [0.99] + [0.0] * (DIM - 1)
    other = [1.0] + [0.0] * (DIM - 1)
    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-old", "concept", "Expert Routing", "src-A", None, other),
                 ("ko-old2", "concept", "Gating Network", "src-A", None, near),
                 ("ko-new", "concept", "MoE Gating", "src-B", None, near)],
        clusters=[("K-" + _norm("Expert Routing"), "ko-old", "Expert Routing", "concept"),
                  ("K-" + _norm("Gating Network"), "ko-old2", "Gating Network", "concept")],
        candidates=[(*sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Gating Network"))),
                     "rejected")])
    assert legacy == bounded
    pairs = {(a, b) for a, b, _score, _status in bounded["candidates"]}
    # 已决那一对不得复活,另一对必须入队 —— 两条都由排除集的有界读决定。
    assert (tuple(sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Expert Routing"))))
            in pairs)
    assert len([p for p in bounded["candidates"] if p[3] == "rejected"]) == 1


def test_differential_skipped_branch_over_max_entities(make_repo, monkeypatch):
    """无 ANN 且实体数 > max_entities → 跳过桥接(仍发 tier2_skipped 事件)。"""
    from app.services.kg_merge import _norm

    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-old", "concept", "Expert Routing", "src-A", None,
                  [1.0] + [0.0] * (DIM - 1)),
                 ("ko-new", "concept", "MoE Gating", "src-B", None,
                  [0.99] + [0.0] * (DIM - 1))],
        clusters=[("K-" + _norm("Expert Routing"), "ko-old", "Expert Routing", "concept")],
        settings={"kg_incremental_tier2_max_entities": 0})
    assert legacy == bounded
    assert bounded["candidates"] == []
    # 事件也逐位一致(_run_differential 把 notebook_id 剥掉后整体比过了)。
    assert bounded["events"] == [
        {"kind": "tier2_skipped", "entities": 1, "reason": "no_index_over_threshold"}]


def test_differential_ann_bridge_branch(make_repo, monkeypatch):
    """有 kg ANN → ANN 桥接分支(canonical 折叠 + 排除集都被有界化的那一支)。

    ⚠夹具刻意放**两个**同源新 concept 且让它们彼此比对既有 concept 更近(评审
    P1-2:只有一个新对象时,「append 之后再折叠」这个 bug 根本无从体现,差分照样
    全绿)。master 语义 = 同源新对象在这一轮互相看不见对方的簇行,所以两两之间
    绝不产候选;下面的 pairs 断言把这条钉住。"""
    from app.services.kg_merge import _norm

    def build_index(repository, notebook_id):
        # 索引建在 fuse **之前**,所以 ann_labels 里已经有本源的新对象 —— 这正是
        # 生产上「上传后先建索引再融合」以及 re-fuse 的形态。
        repository.build_scale_index(notebook_id)

    existing = []
    for index in range(3):
        vec = [0.0] * DIM
        vec[0] = 0.9
        vec[1 + index] = (1 - 0.9 ** 2) ** 0.5
        existing.append((f"ko-e{index}", "concept", f"Concept {index}", "src-A", None, vec))
    new_vec = [0.0] * DIM
    new_vec[0] = 1.0
    new_vec2 = [0.0] * DIM
    new_vec2[0] = 0.999
    new_vec2[DIM - 1] = (1 - 0.999 ** 2) ** 0.5
    objects = existing + [("ko-new", "concept", "MoE Gating", "src-B", None, new_vec),
                          ("ko-new2", "concept", "Gate Network", "src-B", None, new_vec2)]
    clusters = [("K-" + _norm(name), oid, name, "concept")
                for oid, _t, name, _s, _e, _v in existing]

    legacy, bounded = _run_differential(
        make_repo, monkeypatch, objects=objects, clusters=clusters, before=build_index)
    assert legacy == bounded
    pairs = {(a, b) for a, b, _score, _status in bounded["candidates"]}
    for a, b, _score, status in bounded["candidates"]:
        assert status == "pending"
        assert a.startswith("K-") and b.startswith("K-")
    # 每个新对象各桥到三个既有 concept,同源两个之间**不**配对(master 语义)。
    mine = {"K-" + _norm("MoE Gating"), "K-" + _norm("Gate Network")}
    assert tuple(sorted(mine)) not in pairs
    assert len(pairs) == 6
    assert all(len(mine & {a, b}) == 1 for a, b in pairs)


def test_differential_reruns_are_idempotent(make_repo, monkeypatch):
    """已有 K- 簇追加 + 二次融合:幂等去重(insert_clusters 的有界探测)不得漏判。"""
    from app.services.kg_merge import _norm

    def fuse_once(repository, notebook_id):
        repository.incremental_fuse_source(notebook_id, "src-B")

    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-a", "concept", "Charge Pump", "src-A", None, None),
                 ("ko-b", "concept", "Charge Pump", "src-B", None, None),
                 ("kl-b", "claim", "gain is 40 dB", "src-B", None, None)],
        clusters=[("K-" + _norm("Charge Pump"), "ko-a", "Charge Pump", "concept")],
        before=fuse_once)
    assert legacy == bounded
    members = [row[1] for row in bounded["clusters"]]
    assert sorted(members) == ["ko-a", "ko-b", "kl-b"] or len(members) == len(set(members))


def test_same_source_new_concepts_never_bridge_against_each_other(repo):
    """评审 P1-1 的最小复现,现在当回归钉用(不依赖差分臂,直接钉绝对结果)。

    夹具:一个远得不能再远的既有 concept(有簇行),两个**同源新** concept 彼此
    sim≈0.995。索引建在融合之前,所以两个新对象都在 ann_labels 里。

    master 在方法开头读一次整表 cluster_map,那份快照里两个新对象**没有簇行**,
    于是它们互为 ANN 命中时都被 `if not other_cid: continue` 跳掉 —— 也就是
    `_tier2_bridge_candidates_ann` docstring 里写的「新↔新推迟到下一次索引重建」。
    有界化如果把折叠挪到 append **之后**,新对象已经有簇行了,这一对就会凭空进
    「待确认合并」人审队列。实测:挪到 append 之后 → 多出一条
    ('K-gate driver', 'K-level shifter', 0.985, 'pending')。

    这条钉的是**绝对**行为(候选表必须为空),所以「两臂一起错」这种差分盲区
    在它面前无处可藏。"""
    from app.services.kg_merge import _norm

    notebook = repo.create_notebook(NotebookCreate(name="m")).id
    far = [0.0] * DIM
    far[3] = 1.0
    v1 = [0.0] * DIM
    v1[0] = 1.0
    v2 = [0.0] * DIM
    v2[0] = 0.995
    v2[1] = (1 - 0.995 ** 2) ** 0.5
    _seed(repo, notebook,
          objects=[("ko-a", "concept", "Unrelated Thing", "src-A", None, far),
                   ("ko-n1", "concept", "Gate Driver", "src-B", None, v1),
                   ("ko-n2", "concept", "Level Shifter", "src-B", None, v2)],
          clusters=[("K-" + _norm("Unrelated Thing"), "ko-a", "Unrelated Thing", "concept")])
    repo.build_scale_index(notebook)
    repo.incremental_fuse_source(notebook, "src-B")

    assert _fusion_output(repo, notebook)["candidates"] == []
    # 反向:两个新对象**确实**都进了 ANN 的可命中集合且彼此够近,否则这条钉是空的。
    idx = repo._scale_index(notebook, allow_stale=True)
    assert {"ko-n1", "ko-n2"} <= set(idx.ann_labels)


def test_refuse_keeps_the_canonical_frozen_at_the_pre_append_moment(repo):
    """冻结取的是 append **之前**的真实值,不是「一律记成缺失」。

    同一来源第二次融合时,这批 id 上一轮已经写进 concept_clusters —— master 在
    方法开头读到的快照里它们**是有簇行的**,于是同源新↔新这一对在第二次融合确实
    会产出。把冻结实现成「新对象一律 miss」会静默丢掉这条,那同样是行为改变。"""
    from app.services.kg_merge import _norm

    notebook = repo.create_notebook(NotebookCreate(name="refuse")).id
    far = [0.0] * DIM
    far[3] = 1.0
    v1 = [0.0] * DIM
    v1[0] = 1.0
    v2 = [0.0] * DIM
    v2[0] = 0.995
    v2[1] = (1 - 0.995 ** 2) ** 0.5
    _seed(repo, notebook,
          objects=[("ko-a", "concept", "Unrelated Thing", "src-A", None, far),
                   ("ko-n1", "concept", "Gate Driver", "src-B", None, v1),
                   ("ko-n2", "concept", "Level Shifter", "src-B", None, v2)],
          clusters=[("K-" + _norm("Unrelated Thing"), "ko-a", "Unrelated Thing", "concept")])
    repo.build_scale_index(notebook)
    repo.incremental_fuse_source(notebook, "src-B")     # 第一次:簇行落库,零候选
    assert _fusion_output(repo, notebook)["candidates"] == []

    repo.incremental_fuse_source(notebook, "src-B")     # 第二次:快照里已有簇行
    pairs = {(a, b) for a, b, _score, _status in _fusion_output(repo, notebook)["candidates"]}
    assert pairs == {tuple(sorted(("K-" + _norm("Gate Driver"),
                                   "K-" + _norm("Level Shifter"))))}


# ── place_new_concepts:被删掉的 existing_cids 守卫的冻结拷贝差分 ────────────

@pytest.mark.parametrize("cmap,canon,new_objects", [
    # 命中既有簇 → 复用簇名
    ({"o-old": "K-moe"}, {"K-moe": "Mixture of Experts"},
     [{"object_id": "o-new", "name": "moe"}]),
    # 既有 cid 存在但该 cid 没有名录条目(分支 B)
    ({"o-old": "K-moe"}, {}, [{"object_id": "o-new", "name": "moe"}]),
    # cid 完全不存在(分支 C)
    ({}, {}, [{"object_id": "o-new", "name": "moe"}]),
    # cmap 含其他类型的 canonical(生产形态:cluster_map 不按类型过滤)
    ({"o-old": "KL-something", "o-c": "K-moe"}, {"K-moe": "Mixture of Experts"},
     [{"object_id": "o-new", "name": "MoE"}, {"object_id": "o-2", "name": "→"}]),
    # acronym 别名:既有簇名提供 "Full (ACR)" 定义
    ({"o-old": "K-compressed sparse attention"},
     {"K-compressed sparse attention": "Compressed Sparse Attention (CSA)"},
     [{"object_id": "o-new", "name": "CSA"}]),
])
def test_place_new_concepts_matches_frozen_master_copy(cmap, canon, new_objects):
    """新签名(无 cmap)与 master 冻结拷贝在同一输入上逐位一致。

    前提是 ``keys(canon) ⊆ values(cmap)``(生产恒成立,理由见函数 docstring),
    参数化里的每一组都满足它。"""
    from app.services.kg_merge import place_new_concepts, _norm

    assert set(canon) <= set(cmap.values())     # 前提本身也钉住
    seed_fn = (lambda o: _norm(o.get("name", "")))
    assert place_new_concepts(new_objects, canon, seed_fn=seed_fn, id_prefix="K-") == \
        _legacy_place_new_concepts(new_objects, cmap, canon,
                                   seed_fn=seed_fn, id_prefix="K-")


# ── _bridge_canonical_ids:与桥接侧内联式子的同构钉 ─────────────────────────

def _inline_bridge_cid(obj):
    """``detect_bridge_candidates`` / ``_tier2_bridge_candidates_ann`` 里内联的
    那条式子,逐字拷贝 —— 排除集的有界键必须与它同构,否则查出来的对根本不含
    真正要排除的那些,已决/已 pending 的对会被重新入队。"""
    from app.services.kg_merge import _norm, seed_or_unique

    return "K-" + seed_or_unique(_norm(obj.get("name", "")), obj["object_id"])


@pytest.mark.parametrize("new_objs", [
    # 普通名称
    [{"object_id": "o-1", "name": "MoE Gating"}],
    # 空 seed 哨兵:符号-only 名 → _norm 得空串 → seed_or_unique 编进 object id
    # (K-~o-sym),绝不塌缩成裸 "K-"。
    [{"object_id": "o-sym", "name": "→"}, {"object_id": "o-sym2", "name": "☆"}],
    # 缺 name 键(payload 里没写 name):同样走哨兵而不是 KeyError
    [{"object_id": "o-noname"}],
    # 重名新对象:两个对象同一个 canonical → 有界键必须去重成一个
    [{"object_id": "o-a", "name": "Charge Pump"},
     {"object_id": "o-b", "name": "Charge Pump"}],
])
def test_bridge_canonical_ids_match_the_inline_expression(new_objs):
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService

    expected = list(dict.fromkeys(_inline_bridge_cid(o) for o in new_objs))
    assert KnowledgeLifecycleService._bridge_canonical_ids(new_objs) == expected
    assert not any(cid == "K-" for cid in expected)     # 哨兵没退化成裸 K-


def test_bridge_canonical_ids_do_not_follow_the_acronym_alias_redirect():
    """桥接侧**不**过 ``_seed_with_alias``(与 place_new_concepts 的既有差异)。

    这条钉的是 drift 的两个方向:若哪天有人「顺手统一」让 _bridge_canonical_ids
    也走别名重定向,排除集的键就会指向 Full 的簇,而桥接产出的对仍用裸缩写的
    canonical —— 排除集从此永远命中不了自己要排的那些对。"""
    from app.services.kg_merge import _norm, _seed_with_alias, build_acronym_alias_map
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService

    new_objs = [{"object_id": "o-acr", "name": "CSA"}]
    alias_map = build_acronym_alias_map(
        ["Compressed Sparse Attention (CSA)", "CSA"])
    assert alias_map == {"csa": "compressed sparse attention"}   # 别名确实存在
    redirected = "K-" + _seed_with_alias(
        new_objs[0], lambda o: _norm(o.get("name", "")), alias_map)
    assert redirected == "K-compressed sparse attention"

    got = KnowledgeLifecycleService._bridge_canonical_ids(new_objs)
    assert got == ["K-csa"] == [_inline_bridge_cid(new_objs[0])]
    assert redirected not in got


# ── 有界守卫 ────────────────────────────────────────────────────────────────

class _SqlCounter:
    """按 SQL 形态计数的连接代理。"""

    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def execute(self, sql, *args, **kwargs):
        self._log.append(" ".join(str(sql).split()))
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _sql_log(repo, monkeypatch):
    """记录融合期间经读连接与写事务发出的每一条 SQL。

    ``connect()`` 直接返回连接对象(既能 ``with`` 也能裸用),``write()`` 是
    ``@contextmanager`` 生成器 —— 两者形态不同,不能用同一个包装。"""
    from contextlib import contextmanager

    log = []
    database = repo._runtime.database
    original_connect = database.connect
    original_write = database.write

    def traced_connect():
        return _SqlCounter(original_connect(), log)

    @contextmanager
    def traced_write(**kwargs):
        with original_write(**kwargs) as connection:
            yield _SqlCounter(connection, log)

    monkeypatch.setattr(database, "connect", traced_connect)
    monkeypatch.setattr(database, "write", traced_write)
    return log


def _seed_busy_notebook(repo, notebook_id, *, vectors=False):
    from app.services.kg_merge import _norm

    def _vec(index):
        if not vectors:
            return None
        vec = [0.0] * DIM
        vec[0] = 0.5
        vec[1 + (index % (DIM - 1))] = (1 - 0.5 ** 2) ** 0.5
        return vec

    objects, clusters, candidates = [], [], []
    for index in range(12):
        name = f"Existing Concept {index}"
        objects.append((f"ko-old{index}", "concept", name, "src-A", None, _vec(index)))
        clusters.append(("K-" + _norm(name), f"ko-old{index}", name, "concept"))
        candidates.append((f"K-a{index}", f"K-b{index}", "pending"))
    for index in range(4):
        name = f"old claim {index}"
        objects.append((f"kl-old{index}", "claim", name, "src-A", None, None))
        clusters.append(("KL-" + name, f"kl-old{index}", name, "claim"))
    new_vec = None
    if vectors:
        new_vec = [0.0] * DIM
        new_vec[0] = 1.0
    objects.append(("ko-new", "concept", "Brand New Concept", "src-B", None, new_vec))
    objects.append(("kl-new", "claim", "brand new claim", "src-B", None, None))
    _seed(repo, notebook_id, objects, clusters, candidates)


_CLUSTER_MAP_SQL = "SELECT member_object_id, canonical_id FROM concept_clusters"


def _unbounded_reads(log):
    """融合期间出现的、**不该**出现的无界读。"""
    return [
        sql for sql in log
        if (
            # 整表 cluster_map(member_object_id, canonical_id 投影)
            _CLUSTER_MAP_SQL in sql
            # insert_clusters 的切片去重必须带 IN
            or (sql.startswith("SELECT member_object_id FROM concept_clusters")
                and " IN (" not in sql)
            # 桥接排除集必须带 canonical id 上界
            or (sql.startswith("SELECT canonical_a, canonical_b FROM concept_merge_candidates")
                and " IN (" not in sql)
        )
    ]


def _assert_bounded_reads_actually_ran(log):
    """反向:确实跑到了那几条有界查询(否则守卫可能只是因为整条路径没走)。"""
    assert any(sql.startswith("SELECT member_object_id FROM concept_clusters")
               and " IN (" in sql for sql in log)
    assert any(sql.startswith("SELECT canonical_a, canonical_b FROM concept_merge_candidates")
               and " IN (" in sql for sql in log)


def test_ann_fusion_issues_no_unbounded_cluster_reads(repo, monkeypatch):
    """守卫:**ANN 分支**(大库上生产实际走的那一支)不得出现任何整表 cluster_map /
    无 IN 的切片读 / 全表候选对读。

    这是本 PR 的核心验收面:有索引的库每来源上传都会走这里,而 master 在这里要
    物化 9.1M 行的 {member→canonical}。"""
    notebook = repo.create_notebook(NotebookCreate(name="guard-ann"))
    _seed_busy_notebook(repo, notebook.id, vectors=True)
    repo.build_scale_index(notebook.id)
    log = _sql_log(repo, monkeypatch)
    repo.incremental_fuse_source(notebook.id, "src-B")

    assert _unbounded_reads(log) == [], _unbounded_reads(log)
    _assert_bounded_reads_actually_ran(log)
    # 反向:ANN 分支真的跑了(否则这条守卫等于在测暴力分支)。
    assert any("FROM concept_clusters WHERE notebook_id=? AND member_object_id IN" in sql
               for sql in log)


def test_bruteforce_fusion_reads_the_cluster_map_range_exactly_once(repo, monkeypatch):
    """无 ANN 的暴力桥接分支:**刻意**保留一次整表范围读,且只此一次。

    登记在 `incremental_fuse_source` 的 ex_cmap 处 —— 那一支要折叠的是被
    `kg_incremental_tier2_max_entities`(默认 5 万)界住的整批既有 concept,
    实测定点分批比范围读慢约 20×。除这一条之外,其余无界读仍然一条都不许有;
    「只此一次」也把「回归成每个消费方各读一遍」挡在门外。"""
    notebook = repo.create_notebook(NotebookCreate(name="guard-brute"))
    _seed_busy_notebook(repo, notebook.id)
    log = _sql_log(repo, monkeypatch)
    repo.incremental_fuse_source(notebook.id, "src-B")

    range_reads = [sql for sql in log if _CLUSTER_MAP_SQL in sql]
    assert len(range_reads) == 1, range_reads
    assert [sql for sql in _unbounded_reads(log) if _CLUSTER_MAP_SQL not in sql] == []
    _assert_bounded_reads_actually_ran(log)


def test_incremental_cluster_rows_skipped_for_types_with_no_new_objects(repo, monkeypatch):
    """本源没有该类型新对象时,不得为它扫一遍类型切片(旧代码 3 个类型无条件各扫一遍)。"""
    notebook = repo.create_notebook(NotebookCreate(name="guard"))
    _seed_busy_notebook(repo, notebook.id)
    seen = []
    store = repo._runtime.governance
    original = store.incremental_cluster_rows

    def spy(db, notebook_id, object_type):
        seen.append(object_type)
        return original(db, notebook_id, object_type)

    monkeypatch.setattr(store, "incremental_cluster_rows", spy)
    repo.incremental_fuse_source(notebook.id, "src-B")
    # 该来源只有 concept + claim 两类新对象 → formula/procedure 一次都不读。
    assert seen == ["concept", "claim"]


def test_insert_clusters_reads_no_more_rows_than_it_inserts(repo, monkeypatch):
    """守卫:去重探测读回的行数 ≤ 本次待插入行数(旧实现读整个切片)。"""
    notebook = repo.create_notebook(NotebookCreate(name="guard"))
    _seed_busy_notebook(repo, notebook.id)
    observed = []
    store = repo._runtime.governance
    original = store._existing_cluster_members

    def spy(connection, notebook_id, object_type, rows):
        found = original(connection, notebook_id, object_type, rows)
        observed.append((len(rows), len(found)))
        return found

    monkeypatch.setattr(store, "_existing_cluster_members", spy)
    repo.incremental_fuse_source(notebook.id, "src-B")

    assert observed                                     # 真的走到了 append
    for requested, found in observed:
        assert found <= requested
    # 该库有 12 个既有 concept 簇成员,若退回整切片读,found 会远大于 requested(=1)。
    assert max(found for _requested, found in observed) <= 1


def test_ann_fold_requests_only_hits_that_can_still_win_a_slot(repo, monkeypatch):
    """守卫:ANN 分支的 canonical 折叠只为**过完三道过滤**的命中发查询。

    折叠多余的 id 不改变产出(``.get()`` 出来的值用不到),所以差分用例照常绿 ——
    这是一条纯成本回归,必须由专门的守卫盯住:``cluster:`` 集线节点、查询对象自己、
    以及已废弃/已消失的对象都不该占一次定点查询。"""
    from app.services.kg_merge import _norm

    notebook = repo.create_notebook(NotebookCreate(name="fold-scope"))
    live_vec = [0.0] * DIM
    live_vec[0] = 0.95
    live_vec[1] = (1 - 0.95 ** 2) ** 0.5
    dead_vec = [0.0] * DIM
    dead_vec[0] = 0.99
    dead_vec[2] = (1 - 0.99 ** 2) ** 0.5
    new_vec = [0.0] * DIM
    new_vec[0] = 1.0
    _seed(repo, notebook.id,
          objects=[("ko-live", "concept", "Expert Routing", "src-A", None, live_vec),
                   ("ko-dead", "concept", "Dropped Concept", "src-A", None, dead_vec),
                   ("ko-new", "concept", "MoE Gating", "src-B", None, new_vec)],
          clusters=[("K-" + _norm("Expert Routing"), "ko-live", "Expert Routing", "concept"),
                    ("K-" + _norm("Dropped Concept"), "ko-dead", "Dropped Concept", "concept")])
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?", ("ko-dead",))
    repo.build_scale_index(notebook.id)

    calls = []
    service = repo._runtime.knowledge_lifecycle
    original = service._fold_canonical_ids

    def spy(db, notebook_id, object_ids):
        ids = list(object_ids)
        calls.append(ids)
        return original(db, notebook_id, ids)

    monkeypatch.setattr(service, "_fold_canonical_ids", spy)
    repo.incremental_fuse_source(notebook.id, "src-B")

    # 第一条是 append **之前**的冻结(本批新对象自己,见 P1-1 的登记),它必须存在
    # 且只含 new_objs;剩下的才是 ANN 命中折叠。
    assert calls[0] == ["ko-new"]
    hits = [i for ids in calls[1:] for i in ids]
    assert hits                                       # ANN 分支确实跑了
    assert "ko-dead" not in hits                      # 已废弃对象不占一次查询
    assert "ko-new" not in hits                       # 查询对象自己不折叠(已冻结)
    assert not any(i.startswith("cluster:") for i in hits)
    assert len(hits) == len(set(hits))                # memo 生效,不重复查
    assert set(hits) == {"ko-live"}


# SQLite 老版本的 `SQLITE_MAX_VARIABLE_NUMBER` 默认 999(仓库多处按这个数写守卫,
# 见 `sqlite/source_store.py` 的注释);`_IN_CHUNK` 一族取 900,是给 notebook/type
# 这类固定参数留出的余量。下面几条守卫必须钉这个**绝对**数字而不只是「等于常量
# 本身」—— 自引用断言在常量被抬高 100× 时照样绿(评审 P2)。
_SQLITE_PARAM_CEILING = 999


def test_bounded_id_batches_respect_the_parameter_limit():
    """id 列表必须分批(漏掉就会在大批量上撞变量上限/PG 规划耗时)。"""
    from app.services.knowledge_lifecycle import (
        _FUSE_FOLD_BATCH_SIZE, _FUSE_ID_BATCH_SIZE, _in_fuse_batches,
    )

    ids = [f"K-{index}" for index in range(_FUSE_ID_BATCH_SIZE * 2 + 7)]
    batches = list(_in_fuse_batches(ids + ids))     # 含重复:必须去重
    assert [len(batch) for batch in batches] == [
        _FUSE_ID_BATCH_SIZE, _FUSE_ID_BATCH_SIZE, 7]
    assert [item for batch in batches for item in batch] == ids
    assert max(len(batch) for batch in batches) <= _FUSE_ID_BATCH_SIZE
    # 折叠用的是另一档(单列 IN,沿用仓库既有的 900)。
    wide = [f"ko-{index}" for index in range(_FUSE_FOLD_BATCH_SIZE + 3)]
    assert [len(batch) for batch in _in_fuse_batches(wide, _FUSE_FOLD_BATCH_SIZE)] == [
        _FUSE_FOLD_BATCH_SIZE, 3]

    # ── 绝对上界(不是自引用):把常量抬高就必须红 ──────────────────────────
    # 排除集语句 = notebook 1 个 + id 列表**用两次**。
    assert 1 + _FUSE_ID_BATCH_SIZE * 2 <= _SQLITE_PARAM_CEILING
    # cluster_fold_rows = notebook 1 个 + id 列表一次。
    assert 1 + _FUSE_FOLD_BATCH_SIZE <= _SQLITE_PARAM_CEILING


def test_bridge_exclude_read_is_batched_at_the_narrow_limit(repo, monkeypatch):
    """排除集查询把 id 列表用两次,批次上限必须是窄的那一档。"""
    from app.services.knowledge_lifecycle import _FUSE_ID_BATCH_SIZE

    notebook = repo.create_notebook(NotebookCreate(name="batch"))
    service = repo._runtime.knowledge_lifecycle
    sizes = []
    store = repo._runtime.governance
    original = store.merge_candidate_pairs_for_canonicals

    def spy(db, notebook_id, statuses, canonical_ids):
        ids = list(canonical_ids)
        sizes.append(len(ids))
        return original(db, notebook_id, statuses, ids)

    monkeypatch.setattr(store, "merge_candidate_pairs_for_canonicals", spy)
    wanted = [f"K-{index}" for index in range(_FUSE_ID_BATCH_SIZE * 2 + 5)]
    log = _sql_log(repo, monkeypatch)
    with repo._connect() as db:
        assert service._bridge_exclude_rows(db, notebook.id, ("pending",), wanted) == []
    assert sizes == [_FUSE_ID_BATCH_SIZE, _FUSE_ID_BATCH_SIZE, 5]
    # 绝对上界:真实发出的那条语句的占位符数量必须留在 SQLite 的变量上限内。
    emitted = [sql for sql in log
               if sql.startswith("SELECT canonical_a, canonical_b FROM concept_merge_candidates")]
    assert emitted
    assert max(sql.count("?") for sql in emitted) <= _SQLITE_PARAM_CEILING


def test_merge_candidate_pairs_for_canonicals_is_bounded_by_either_endpoint(repo):
    """SQLite 侧的语义钉,与 PG conformance 的同名用例双后端同义
    (``tests/postgres/test_knowledge_store_conformance.py``)。"""
    notebook = repo.create_notebook(NotebookCreate(name="either-end"))
    store = repo._runtime.governance
    _seed(repo, notebook.id, objects=[], clusters=[], candidates=[
        ("K-mine", "K-other", "pending"),
        ("K-far-a", "K-mine", "pending"),            # 命中 canonical_b 一侧
        ("K-unrelated-a", "K-unrelated-b", "pending"),
        ("K-mine", "K-decided", "confirmed"),        # 状态不符
    ])
    with repo._connect() as db:
        rows = store.merge_candidate_pairs_for_canonicals(
            db, notebook.id, ("pending",), ["K-mine"])
        empty = store.merge_candidate_pairs_for_canonicals(
            db, notebook.id, ("pending",), [])
        decided = store.merge_candidate_pairs_for_canonicals(
            db, notebook.id, ("confirmed", "rejected", "deferred"), ["K-mine"])
    assert sorted((r["canonical_a"], r["canonical_b"]) for r in rows) == [
        ("K-far-a", "K-mine"), ("K-mine", "K-other")]
    assert empty == []
    assert [(r["canonical_a"], r["canonical_b"]) for r in decided] == [
        ("K-mine", "K-decided")]
    # deny-by-default:非法 statuses 必须**在**空 id 早退之前就炸,否则调用方分批
    # 时一个拼错的 statuses 只在「恰好这一批非空」才报错(评审 P3)。
    for canonical_ids in (["K-mine"], []):
        with pytest.raises(ValueError):
            with repo._connect() as db:
                store.merge_candidate_pairs_for_canonicals(
                    db, notebook.id, ("bogus",), canonical_ids)


def test_fold_read_is_batched_within_the_parameter_ceiling(repo, monkeypatch):
    """折叠查询走宽的那一档,同样钉真实发出的语句的**绝对**参数数。"""
    from app.services.knowledge_lifecycle import _FUSE_FOLD_BATCH_SIZE

    notebook = repo.create_notebook(NotebookCreate(name="fold-batch"))
    service = repo._runtime.knowledge_lifecycle
    log = _sql_log(repo, monkeypatch)
    wanted = [f"ko-{index}" for index in range(_FUSE_FOLD_BATCH_SIZE + 5)]
    with repo._connect() as db:
        assert service._fold_canonical_ids(db, notebook.id, wanted) == {}
    emitted = [sql for sql in log
               if "FROM concept_clusters WHERE notebook_id=? AND member_object_id IN" in sql]
    assert [sql.count("?") for sql in emitted] == [_FUSE_FOLD_BATCH_SIZE + 1, 6]
    assert max(sql.count("?") for sql in emitted) <= _SQLITE_PARAM_CEILING


def test_insert_clusters_batches_member_probe(repo, monkeypatch):
    """insert_clusters 自己也分批(它拿到的是整批 rows,批次上限来自 seams)。"""
    notebook = repo.create_notebook(NotebookCreate(name="batch"))
    store = repo._runtime.governance
    monkeypatch.setattr(repo, "_IN_CHUNK", 5, raising=False)
    log = _sql_log(repo, monkeypatch)
    rows = [{"canonical_id": f"K-{index}", "member_object_id": f"ko-{index}",
             "canonical_name": f"n{index}"} for index in range(12)]
    with repo._write() as db:
        assert store.insert_clusters(db, notebook.id, "concept", rows, NOW) == 12
    probes = [sql for sql in log
              if sql.startswith("SELECT member_object_id FROM concept_clusters")]
    assert [sql.count("?") for sql in probes] == [7, 7, 4]   # 5+5+2 ids, +2 fixed
