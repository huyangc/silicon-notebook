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
    #    generation 在 legacy 臂上被刻意忽略——master 的探针跨代整切片读。
    def legacy_existing_members(connection, notebook_id, object_type, rows,
                                generation):
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

    # ⑤ 向量读取(P1 轮批 D):打回 master 的 `embedding_rows` —— 整个 notebook 的
    #    **全类型**向量。无 ANN 暴力分支只按 concept 的 object_id 取值,所以这条
    #    seam 打回后产出必须逐位不变。
    #
    #    ⚠ 判别力取决于**夹具**:只有当库里存在「非 concept 或 deprecated 的
    #    带向量对象」时,legacy 臂与收窄臂读回的行集才真的不同,这条差分才在说
    #    「多读的那些行确实没被用上」。凡是要给这个接缝提供 oracle 的用例都必须
    #    带上那种对象 —— `test_differential_bruteforce_bridge_branch` 与
    #    `test_differential_skipped_branch_over_max_entities` 已经带了,由
    #    `test_legacy_vector_seam_actually_reads_more_rows` 正面钉住。
    def legacy_embedding_rows(db, notebook_id):
        return db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
            (notebook_id,)).fetchall()

    monkeypatch.setattr(service.knowledge, "concept_embedding_rows",
                        legacy_embedding_rows)


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


def _deprecate(object_id):
    """`_run_differential` 的 before 钩子:把一个对象打成 deprecated。"""
    def _before(repository, notebook_id):
        with repository._write() as db:
            db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
                       (object_id,))
    return _before


def test_differential_bruteforce_bridge_branch(make_repo, monkeypatch):
    """无 ANN 且实体数 ≤ max_entities → 暴力余弦分支,含既有 pending/已决排除集。

    夹具**刻意**带上一条有向量的 claim 与一个有向量的 deprecated concept:接缝 ⑤
    (向量读打回全类型全表)只有在库里存在这类对象时,两臂读回的行集才真的不同,
    这条差分才是「多读的那些行确实没被用上」的 oracle 而不是空转。"""
    from app.services.kg_merge import _norm

    near = [0.99] + [0.0] * (DIM - 1)
    other = [1.0] + [0.0] * (DIM - 1)
    legacy, bounded = _run_differential(
        make_repo, monkeypatch,
        objects=[("ko-old", "concept", "Expert Routing", "src-A", None, other),
                 ("ko-old2", "concept", "Gating Network", "src-A", None, near),
                 # 被收窄挡在门外的两类,都给上与 ko-new 高度相似的向量:一旦
                 # 某个消费方真的用了它们,产出会立刻分叉。
                 ("kl-old", "claim", "routing halves the KV cache", "src-A", None, near),
                 ("ko-dead", "concept", "Retired Router", "src-A", None, near),
                 ("ko-new", "concept", "MoE Gating", "src-B", None, near)],
        clusters=[("K-" + _norm("Expert Routing"), "ko-old", "Expert Routing", "concept"),
                  ("K-" + _norm("Gating Network"), "ko-old2", "Gating Network", "concept"),
                  ("K-" + _norm("Retired Router"), "ko-dead", "Retired Router", "concept")],
        candidates=[(*sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Gating Network"))),
                     "rejected")],
        before=_deprecate("ko-dead"))
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

    def spy(connection, notebook_id, object_type, rows, generation):
        found = original(connection, notebook_id, object_type, rows, generation)
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
    # 批 3·W2 §1.4:cluster_fold_rows 加 published 代次谓词(自带一个 ? 的
    # COALESCE 子查询)→ 每条语句恰好 +1 参数。
    assert [sql.count("?") for sql in emitted] == [_FUSE_FOLD_BATCH_SIZE + 2, 7]
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
    assert [sql.count("?") for sql in probes] == [8, 8, 5]   # 5+5+2 ids, +3 fixed(批 3·W2 探针加 generation)


# ── P1 轮批 D:orphan 簇的**生产者清零** + 纯遗留残渣兜底 ────────────────────
#
# master 在 `incremental_fuse_source` 开头无条件跑一次**全库** anti-join
# (`member_object_id NOT IN (SELECT id FROM knowledge_objects …)`),而本方法每成
# 功抽取一个来源就被调一次、fold 还按 delta 来源循环调 —— 千万级对象的库上等于
# O(delta × 全库)。
#
# 最终形状是把**生产者**清零,而不是给消费端加聪明的闸(闸走过两个被否的版本,
# 都在下面各有一条守卫):
#   ① 来源删除/重解析/replace_source → `_delete_object_id_batch` 同事务按
#      (notebook_id, member_object_id) 清簇行;
#   ② knowhow 重投影/删表 → `prune_cluster_rows_for_source` 同事务只清**差集**;
#   ③ 整库重建 → `delete_notebook_graph_rows` 整表清空;
#   ④ T-5a 预排水 → `drain_notebook_graph_rows_page` 的 knowledge_objects 页
#      按 ① 同形在同一事务里连带清本页对象的簇行(排水中断也不产孤儿)。
# 于是这里剩下的唯一职责是清历史残渣:每进程每 notebook 一次。

# 清扫的第一条语句(每批必发的 keyset 页读)。数「跑了几批清扫」要数它而不是数
# DELETE:P1 修复后 DELETE 只在页非空**且**页内真有孤儿要删时才发,而清扫是否
# 开跑与库里有没有孤儿无关(零孤儿的库照样要扫一遍才知道它是零)。
_SWEEP_PAGE_SQL_FRAGMENT = (
    # 批 3·W2:游标带 generation 分量(四列唯一后 (type,member) 不再唯一)。
    "SELECT object_type, member_object_id, generation, id FROM concept_clusters WHERE notebook_id = ?"
)
_SWEEP_DELETE_SQL_FRAGMENT = (
    "WHERE k.id = c.member_object_id AND k.notebook_id = c.notebook_id"
)


def _sweeps(log):
    """清扫扫过的**页数**(= 发出的 keyset 页读条数)。"""
    return len([sql for sql in log if _SWEEP_PAGE_SQL_FRAGMENT in sql])


def _sweep_deletes(log):
    """清扫发出的 DELETE 条数 —— **每个非空页一条**(页内有没有孤儿要等这条语句
    自己去判)。终止用的那次空页读不带 DELETE,所以它恒等于「非空页数」。"""
    return len([sql for sql in log if _SWEEP_DELETE_SQL_FRAGMENT in sql])


def _cluster_seq_bumps(log):
    """`cluster_mutation_seq` 被推进的次数 —— 只有真删到行的那些批才该推。"""
    return len([sql for sql in log if "cluster_mutation_seq=" in sql])


def _cluster_members(repo, notebook_id):
    with repo._connect() as db:
        return {r["member_object_id"] for r in db.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id=?",
            (notebook_id,))}


def _insert_orphan_cluster_row(repo, notebook_id, member="ko-vanished"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"cc-orphan-{member}", notebook_id, "K-vanished", member,
             "Vanished", "concept", "", NOW))
    return member


@pytest.fixture
def fresh_process():
    """每条用例都从「全新进程」出发(兜底闸是进程内状态)。"""
    from app.services import knowledge_lifecycle as lifecycle_module

    lifecycle_module._ORPHAN_SWEEP_DONE.clear()
    yield lifecycle_module._ORPHAN_SWEEP_DONE
    lifecycle_module._ORPHAN_SWEEP_DONE.clear()


def test_upload_hot_path_never_reopens_the_orphan_sweep(
        repo, monkeypatch, fresh_process):
    """回归守卫(评审实测打回的第一版):`store_kg` → 融合重复三次,全库 anti-join
    只在冷进程第一次跑。

    第一版的闸判据是 `kg_mutation_seq`,而 `store_kg` 收尾无条件 bump 它 ——
    于是「代次变了」在上传主热路径上恒成立,三次上传三次全表反连接。"""
    notebook = repo.create_notebook(NotebookCreate(name="upload-hot"))
    _seed_busy_notebook(repo, notebook.id)
    log = _sql_log(repo, monkeypatch)

    for round_index in range(3):
        repo.store_kg(notebook.id, f"src-up{round_index}", [
            {"local_id": f"c{round_index}", "object_type": "concept",
             "payload": {"name": f"Uploaded Concept {round_index}"},
             "evidence": []}], [])
        repo.incremental_fuse_source(notebook.id, f"src-up{round_index}")

    assert _sweeps(log) == 1


def test_orphan_sweep_is_once_per_process_per_notebook(
        repo, monkeypatch, fresh_process):
    """兜底闸的完整语义:冷进程每本库扫一次,之后不再扫;进程重启后再扫一次。"""
    first = repo.create_notebook(NotebookCreate(name="sweep-a"))
    # 第二本库刻意留空:`_seed_busy_notebook` 的对象 id 是写死的,而
    # knowledge_objects.id 是**全局**主键,两本库塞同一批 id 会撞 UNIQUE。闸的
    # 按库语义与库里有没有对象无关。
    second = repo.create_notebook(NotebookCreate(name="sweep-b"))
    _seed_busy_notebook(repo, first.id)
    log = _sql_log(repo, monkeypatch)

    repo.incremental_fuse_source(first.id, "src-B")
    assert _sweeps(log) == 1
    repo.incremental_fuse_source(first.id, "src-B")
    assert _sweeps(log) == 1                 # 同库第二次:跳过
    repo.incremental_fuse_source(second.id, "src-B")
    assert _sweeps(log) == 2                 # 闸是**按库**的,不是全局一次

    fresh_process.clear()                    # 进程重启
    repo.incremental_fuse_source(first.id, "src-B")
    assert _sweeps(log) == 3                 # 兜底不失效


def test_orphan_sweep_is_not_suppressed_across_workers(
        repo, monkeypatch, fresh_process):
    """codex P1 的守卫:多 worker 下不得出现「A 产生、B 收不到信号因而永久压制」。

    被否的 tick 版本里,knowhow 的删除只推进**本进程**的信号,另一个 worker 的
    记账停在旧世代 → 它永远跳过清扫。这里用两份独立的进程内状态模拟两个 worker:
    worker B 已经扫过一次(记账已落),此时 worker A 做一次 knowhow 差集删除 ——
    断言簇行已经在**那次删除自己的事务**里没了,B 扫不扫都无所谓。"""
    from app.services import knowledge_lifecycle as lifecycle_module

    notebook = repo.create_notebook(NotebookCreate(name="two-workers"))
    _seed_busy_notebook(repo, notebook.id)
    store = repo._runtime.knowledge_lifecycle.knowledge

    # worker B:先融合一次,记账落下 → 它此后不会再扫这本库。
    repo.incremental_fuse_source(notebook.id, "src-B")
    worker_b_state = set(lifecycle_module._ORPHAN_SWEEP_DONE)
    assert notebook.id in worker_b_state

    # worker A(独立进程,自己的状态)做一次 knowhow 重投影:留下 ko-old0,丢掉
    # ko-old1。差集清理在同一事务内完成,不依赖任何跨进程信号。
    with repo._write() as db:
        store.prune_cluster_rows_for_source(
            db, notebook.id, "src-A", keep_object_ids=["ko-old0"])

    members = _cluster_members(repo, notebook.id)
    assert "ko-old0" in members                    # 活对象保留
    assert "ko-old1" not in members                # 差集当场清掉

    # worker B 再融合:它照样跳过 sweep —— 而库里已经没有悬空行,压制无害。
    log = _sql_log(repo, monkeypatch)
    repo.incremental_fuse_source(notebook.id, "src-B")
    assert _sweeps(log) == 0
    assert lifecycle_module._ORPHAN_SWEEP_DONE == worker_b_state


def test_orphan_sweep_mark_is_recorded_even_when_the_sweep_raises(
        repo, monkeypatch, fresh_process):
    """Z6(P0,行为恢复):记账现在挪进了 try/finally —— 语义从「成功过一次」变成
    「尝试过一次」。清扫本身已经是有界的 keyset 分批 DELETE,不该再稳定撞
    statement_timeout;但如果一次异常(连接抖动、某一批恰好超时)不记账,下一次
    上传会在同一个大概率还会失败的清扫上重开一遍、每次都白烧时间,还连带打断
    整个 `incremental_fuse_source`(Tier2 桥接与 claim/formula/procedure 聚类因此
    对这本库永久不生效)——这个代价远大于「漏扫一次历史残渣」,而残渣本身可以被
    下一次进程重启或 rebuild 兜住。

    变异复现:把记账挪回 `try/finally` 之外(旧形态,只在成功时记账)会让第一个
    assert 变红 —— 失败之后 notebook.id 不会进 `_ORPHAN_SWEEP_DONE`。"""
    from app.services import knowledge_lifecycle as lifecycle_module

    notebook = repo.create_notebook(NotebookCreate(name="sweep-raises"))
    _seed_busy_notebook(repo, notebook.id)
    service = repo._runtime.knowledge_lifecycle
    store = repo._runtime.governance
    original = store.sweep_orphan_clusters_page

    def exploding_sweep(db, notebook_id, after_object_type,
                        after_member_object_id, after_generation, limit):
        raise RuntimeError("sweep transaction failed")

    monkeypatch.setattr(store, "sweep_orphan_clusters_page", exploding_sweep)
    with pytest.raises(RuntimeError, match="sweep transaction failed"):
        service.incremental_fuse_source(notebook.id, "src-B")
    assert notebook.id in lifecycle_module._ORPHAN_SWEEP_DONE  # 失败也记账了

    monkeypatch.setattr(store, "sweep_orphan_clusters_page", original)
    log = _sql_log(repo, monkeypatch)
    repo.incremental_fuse_source(notebook.id, "src-B")
    assert _sweeps(log) == 0                 # 已记过账 → 这次不再重扫


def test_orphan_sweep_gate_takes_no_write_lock_when_it_skips(
        repo, monkeypatch, fresh_process):
    """P2:闸本身是纯内存判断 —— 跳过时既不读库也不开写事务。

    fold 循环按 delta 来源调 D 次,每次白拿一遍进程写锁正是 PR#320 写锁饿死的
    同一形状。"""
    notebook = repo.create_notebook(NotebookCreate(name="sweep-nolock"))
    _seed_busy_notebook(repo, notebook.id)
    repo.incremental_fuse_source(notebook.id, "src-B")   # 先让冷进程那次扫完

    writes = []
    database = repo._runtime.database
    original_write = database.write
    from contextlib import contextmanager

    @contextmanager
    def counting_write(**kwargs):
        writes.append(1)
        with original_write(**kwargs) as connection:
            yield connection

    monkeypatch.setattr(database, "write", counting_write)
    repo.incremental_fuse_source(notebook.id, "src-C-empty")
    # 本源没有任何新对象 → 除被跳过的 sweep 外,融合路径本身不写任何东西。
    assert writes == []


def test_orphan_sweep_done_set_clears_when_it_overflows(fresh_process):
    """P2:进程内集合的有界淘汰分支必须真的被走到,且淘汰方向安全(清空 → 读不到
    记录 → 多扫一次,绝不漏扫)。"""
    from app.services import knowledge_lifecycle as lifecycle_module

    done = lifecycle_module._ORPHAN_SWEEP_DONE
    limit = lifecycle_module._ORPHAN_SWEEP_DONE_MAX
    done.update(f"nb-{index}" for index in range(limit))
    assert len(done) == limit
    # 生产里由 incremental_fuse_source 写入;这里复现那三行的淘汰语义。
    with lifecycle_module._ORPHAN_SWEEP_DONE_LOCK:
        if len(done) >= limit:
            done.clear()
        done.add("nb-new")
    assert done == {"nb-new"}


def test_legacy_orphan_residue_is_swept_by_a_fresh_process(repo, fresh_process):
    """端到端:改造之前留在库里的历史残渣,由新进程的第一次融合清掉。"""
    notebook = repo.create_notebook(NotebookCreate(name="sweep-residue"))
    _seed_busy_notebook(repo, notebook.id)
    orphan = _insert_orphan_cluster_row(repo, notebook.id)
    assert orphan in _cluster_members(repo, notebook.id)

    repo.incremental_fuse_source(notebook.id, "src-B")
    assert orphan not in _cluster_members(repo, notebook.id)


def _apply_legacy_not_in_sweep(repo, notebook_id):
    """Z6 ②的等价 oracle:冻结拷贝的**旧**单条 ``NOT IN`` 反连接语义,只用来跟
    NOT EXISTS 分批版对账 —— 不是任何生产路径。"""
    with repo._write() as db:
        db.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=? AND member_object_id NOT IN "
            "(SELECT id FROM knowledge_objects WHERE notebook_id=?)",
            (notebook_id, notebook_id),
        )


def _seed_sweep_oracle_fixture(repo, notebook_id, other_notebook_id):
    """两个 notebook 都要跑同一套镜像数据(相同的 object_type/member_object_id
    组合,字面 id 完全相同)——两边各自跑在**独立的 sqlite 文件**里,不共享
    ``knowledge_objects`` 的全局主键空间,所以字面复用不会冲突,也让两边的最终
    存活集合可以直接按字符串比较。

    覆盖:同 object_type 内既有存活又有孤儿、跨 object_type、以及**跨 notebook
    的 id 撞名**(member_object_id 是另一个 notebook 的真实对象 id —— 旧查询按
    notebook 过滤后仍必须判它是孤儿,这正是新 NOT EXISTS 里
    ``k.notebook_id = c.notebook_id`` 那个额外条件要保住的语义)。"""
    with repo._write() as db:
        # 另一个 notebook 下的真实对象:对 notebook_id 而言,它的 id 存在于
        # knowledge_objects(全局 PK 命中),但属于别的库。
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ko-cross-owner", other_notebook_id, "concept",
             "approved", "", "{}", "[]", "src-A", NOW, NOW),
        )
        for index in range(3):
            alive_id = f"ko-alive-{index}"
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                "payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (alive_id, notebook_id, "concept", "approved", "", "{}", "[]",
                 "src-A", NOW, NOW),
            )
        rows = [
            ("concept", "ko-alive-0"),
            ("concept", "ko-alive-1"),
            ("concept", "ko-orphan-a"),
            ("claim", "ko-orphan-b"),
            ("claim", "ko-alive-2"),
            ("procedure", "ko-cross-owner"),  # 全局存在,但不属于本库
        ]
        for index, (object_type, member_object_id) in enumerate(rows):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,canonical_description,"
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"cc-{notebook_id}-{index}", notebook_id, f"K-{index}",
                 member_object_id, "Name", object_type, "", NOW),
            )


def test_new_batched_sweep_matches_legacy_not_in_semantics_bit_for_bit(
        make_repo, fresh_process):
    """Z6 ②:等价 oracle —— NOT EXISTS 分批版在同一套夹具上,存活/被删的
    ``(object_type, member_object_id)`` 集合必须与旧的单条 NOT IN 反连接逐字
    相同,包括「member_object_id 是另一个 notebook 的真实对象 id」这个边界
    (旧查询按 notebook 过滤后仍判它是孤儿,新查询靠 NOT EXISTS 里的
    ``k.notebook_id = c.notebook_id`` 保住同一判定)。

    两套完全隔离的 sqlite 文件各自灌入同构数据,一套跑冻结的旧算法,另一套跑
    `_sweep_orphan_clusters_page_loop`,比较两边最终存活的成员集合。"""
    repo_old = make_repo("legacy")
    repo_new = make_repo("batched")

    notebook_old = repo_old.create_notebook(NotebookCreate(name="oracle"))
    other_old = repo_old.create_notebook(NotebookCreate(name="oracle-other"))
    notebook_new = repo_new.create_notebook(NotebookCreate(name="oracle"))
    other_new = repo_new.create_notebook(NotebookCreate(name="oracle-other"))

    _seed_sweep_oracle_fixture(repo_old, notebook_old.id, other_old.id)
    _seed_sweep_oracle_fixture(repo_new, notebook_new.id, other_new.id)

    before_old = _cluster_members(repo_old, notebook_old.id)
    before_new = _cluster_members(repo_new, notebook_new.id)
    assert before_old == before_new                     # 夹具本身镜像一致

    _apply_legacy_not_in_sweep(repo_old, notebook_old.id)
    deleted = repo_new._runtime.knowledge_lifecycle._sweep_orphan_clusters_page_loop(
        notebook_new.id
    )

    after_old = _cluster_members(repo_old, notebook_old.id)
    after_new = _cluster_members(repo_new, notebook_new.id)
    assert after_old == after_new                        # 存活集合逐字一致
    assert before_old - after_old == before_new - after_new  # 被删集合也逐字一致
    assert deleted == len(before_new - after_new)
    # 反向:两态里都真的既有存活又有被删,不是退化成全删或全不删。
    assert after_old
    assert before_old - after_old


def _insert_live_cluster_row(repo, notebook_id, member):
    """一条**非孤儿**簇行:对象真实存在于同一个 notebook。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (member, notebook_id, "concept", "approved", "", "{}", "[]",
             "src-A", NOW, NOW))
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"cc-live-{member}", notebook_id, "K-live", member,
             "Live", "concept", "", NOW))
    return member


def _record_sweep_cursors(repo, monkeypatch, *, max_calls):
    """把 store 的清扫批包一层,记下每批**收到的游标**和**扫到的页长**。

    这是变异自检的着力点:P1 修复的判据是「游标按扫描页推进」,而它只能从
    连续两批的游标关系上看出来 —— 光看最终存活集合,退化实现(按被删行推进)在
    有孤儿的夹具上也能碰巧通过。``max_calls`` 是死循环护栏:退化实现在零孤儿的库
    上游标永不推进,没有它这条用例会挂死而不是变红。"""
    store = repo._runtime.governance
    original = store.sweep_orphan_clusters_page
    seen: list[tuple[tuple[str, str], int]] = []

    def recording(db, notebook_id, after_object_type, after_member_object_id,
                  after_generation, limit):
        cursor = (after_object_type, after_member_object_id, after_generation)
        assert len(seen) < max_calls, (
            f"清扫批数超过 {max_calls} —— 游标没有按扫描页推进(退化成按被删行推进"
            f"就会在零孤儿页上原地打转);已见游标={seen}")
        page, deleted = original(
            db, notebook_id, after_object_type, after_member_object_id,
            after_generation, limit)
        # 单批扫描行数 ≤ 页大小 —— 批大小界住的是**扫描**,不是被删。
        assert len(page) <= limit, (len(page), limit)
        seen.append((cursor, len(page)))
        return page, deleted

    monkeypatch.setattr(store, "sweep_orphan_clusters_page", recording)
    return seen


def test_orphan_sweep_batches_progress_by_scanned_pages(
        repo, monkeypatch, fresh_process):
    """Z6 ③(P1 修复后重写):批数由**扫描页数**决定,不由孤儿数决定。

    夹具:10 行、页大小 3 → 页边界 [0,1,2][3,4,5][6,7,8][9];4 个孤儿分散在
    第 1、2、4 页(第 3 页**一个孤儿都没有**)。断言按页推进 4 批、孤儿全删、
    非孤儿一行不少 —— 中间那个「零孤儿页」正是旧实现会跳过、从而把游标推过头
    (或反复重扫)的地方。"""
    from app.services import knowledge_lifecycle as lifecycle_module

    monkeypatch.setattr(lifecycle_module, "_ORPHAN_SWEEP_BATCH_SIZE", 3)
    notebook = repo.create_notebook(NotebookCreate(name="sweep-batches"))
    orphan_indexes = {0, 4, 5, 9}
    orphans, alive = [], []
    for index in range(10):
        member = f"ko-row-{index}"
        if index in orphan_indexes:
            orphans.append(_insert_orphan_cluster_row(repo, notebook.id, member=member))
        else:
            alive.append(_insert_live_cluster_row(repo, notebook.id, member))

    cursors = _record_sweep_cursors(repo, monkeypatch, max_calls=12)
    log = _sql_log(repo, monkeypatch)
    service = repo._runtime.knowledge_lifecycle
    deleted = service._sweep_orphan_clusters_page_loop(notebook.id)

    members = _cluster_members(repo, notebook.id)
    assert deleted == len(orphans)
    assert members.isdisjoint(orphans)       # 孤儿全删
    assert set(alive) <= members             # 非孤儿全存
    # 4 批 = ceil(10/3);短的最后一页(1 行)是终止条件。
    assert _sweeps(log) == 4
    assert [page_length for _cursor, page_length in cursors] == [3, 3, 3, 1]
    # 游标严格按**扫到的页尾**推进,与那一页删没删无关(第 3 页零孤儿)。
    # 批 3·W2:游标带 generation 尾分量(起始 -1;种子行全 0 代)。
    assert [cursor for cursor, _length in cursors] == [
        ("", "", -1),
        ("concept", "ko-row-2", 0),
        ("concept", "ko-row-5", 0),
        ("concept", "ko-row-8", 0),
    ]
    # 每个非空页一条 DELETE(4 页全非空);但只有真删到行的 3 页推进 cseq ——
    # 第 3 页零孤儿:DELETE 发了、删到 0 行、不 bump,游标照样推进(上一条断言)。
    assert _sweep_deletes(log) == 4
    assert _cluster_seq_bumps(log) == 3


def test_orphan_sweep_terminates_on_a_zero_orphan_notebook(
        repo, monkeypatch, fresh_process):
    """P1 的正面判据:一本**一个孤儿都没有**的库(生产常态 —— 生产者已清零)
    照样按页扫完并正常终止,且每批扫描行数 ≤ 页大小、零删除。

    旧形态在这里最痛:`LIMIT` 挂在已被 `NOT EXISTS` 过滤过的子查询上,攒不满
    n 条孤儿就一路扫到切片尽头 —— 单批 = 全 notebook 扫描,900 万行照撞 30s
    statement_timeout,而 docstring 里「没有语句的成本随全量簇行数增长」的说法
    根本不成立。

    变异自检:把 `_sweep_orphan_clusters_page_loop` 的游标改回按**被删行**推进
    (旧形态),这条会因为游标原地不动而撞上 `_record_sweep_cursors` 的护栏、或
    因为一批就 break 而在批数断言上变红。"""
    from app.services import knowledge_lifecycle as lifecycle_module

    monkeypatch.setattr(lifecycle_module, "_ORPHAN_SWEEP_BATCH_SIZE", 3)
    notebook = repo.create_notebook(NotebookCreate(name="sweep-zero-orphan"))
    alive = [
        _insert_live_cluster_row(repo, notebook.id, f"ko-live-{index}")
        for index in range(9)
    ]

    cursors = _record_sweep_cursors(repo, monkeypatch, max_calls=12)
    log = _sql_log(repo, monkeypatch)
    service = repo._runtime.knowledge_lifecycle
    deleted = service._sweep_orphan_clusters_page_loop(notebook.id)

    assert deleted == 0
    assert _cluster_members(repo, notebook.id) == set(alive)   # 一行都没被误删
    # 3 个满页 + 1 次空探测:9 行整除页大小,所以终止靠的是最后那次空页读,
    # 而不是任何「这批没删到东西」的判断。
    assert [page_length for _cursor, page_length in cursors] == [3, 3, 3, 0]
    assert _sweeps(log) == 4
    # 3 个非空页各发一条 DELETE(它们各自删到 0 行),空页读不发 —— 而一次
    # cseq 推进都没有:清扫没动过任何一行,不得制造变更信号。
    assert _sweep_deletes(log) == 3
    assert _cluster_seq_bumps(log) == 0


def test_sweep_delete_never_binds_more_ids_than_the_chunk(
        repo, monkeypatch, fresh_process):
    """SQLite 侧独有的一件事:页可以比一条 DELETE 允许绑定的 id 数宽。

    PG 把整页当成一个 `= ANY(%s)` 数组传;SQLite 没有数组,只能把 id 展开成
    `IN (?,?,…)`,所以一页要按 `_SWEEP_DELETE_ID_CHUNK` 切片,免得一条语句绑上
    几千个参数(SQLite 有 host-parameter 上限,而且仓库其余展开式 IN 一律钉在
    500/900 这个量级)。总成本不变:⌈页/切片⌉ 条语句,每条 ≤ 切片次主键探针。"""
    from app.repositories.sqlite import governance_store as gov_module
    from app.services import knowledge_lifecycle as lifecycle_module

    monkeypatch.setattr(lifecycle_module, "_ORPHAN_SWEEP_BATCH_SIZE", 5)
    monkeypatch.setattr(gov_module, "_SWEEP_DELETE_ID_CHUNK", 2)
    notebook = repo.create_notebook(NotebookCreate(name="sweep-chunk"))
    orphans = [
        _insert_orphan_cluster_row(repo, notebook.id, member=f"ko-vanished-{i}")
        for i in range(5)
    ]

    log = _sql_log(repo, monkeypatch)
    deleted = repo._runtime.knowledge_lifecycle._sweep_orphan_clusters_page_loop(
        notebook.id)

    assert deleted == 5
    assert _cluster_members(repo, notebook.id).isdisjoint(orphans)
    deletes = [sql for sql in log if _SWEEP_DELETE_SQL_FRAGMENT in sql]
    assert len(deletes) == 3                 # 一页 5 个 id → 切成 2+2+1
    # 每条 DELETE 的占位符 = 1 个 notebook_id + ≤ chunk 个 id。
    assert [sql.count("?") for sql in deletes] == [3, 3, 2]


# ── 生产者侧:三条删除路径各自的事务内清理 ──────────────────────────────────

def test_object_deletion_drops_its_cluster_rows_in_the_same_transaction(repo):
    """① 来源删除/重解析/replace_source:删除路径自己清干净,不留账给融合。

    这里**一次融合都不跑** —— master 下这些成员行会一直悬空到某次
    `incremental_fuse_source` 的全库 anti-join 才被清掉。"""
    notebook = repo.create_notebook(NotebookCreate(name="delete-clean"))
    _seed_busy_notebook(repo, notebook.id)
    store = repo._runtime.knowledge_lifecycle.knowledge
    before = _cluster_members(repo, notebook.id)
    assert {"ko-old0", "kl-old0"} <= before

    with repo._write() as db:
        store.clear_source_graph_state(db, "src-A", notebook.id)

    # src-A 的成员行一条不剩,且清理只针对被删对象 —— 本夹具里 src-A 的对象就是
    # 全部有簇行的对象,所以剩余集合恰好为空。
    after = _cluster_members(repo, notebook.id)
    assert after == set()
    assert before  # 反向:确实有东西可删(否则上一条是空断言)


def test_precise_cluster_cleanup_is_scoped_to_the_deleting_notebook(repo):
    """精确清理必须带 notebook 谓词:与它替代的 sweep 逐字同形,不靠「对象 id 是
    全局主键」这类远端事实。别的 notebook 里同名 member 的行不得被牵连。"""
    first = repo.create_notebook(NotebookCreate(name="own"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    _seed_busy_notebook(repo, first.id)
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("cc-other-collide", other.id, "K-collide", "ko-old0",
             "Collide", "concept", "", NOW))

    store = repo._runtime.knowledge_lifecycle.knowledge
    with repo._write() as db:
        store.clear_source_graph_state(db, "src-A", first.id)

    assert _cluster_members(repo, first.id) == set()
    assert _cluster_members(repo, other.id) == {"ko-old0"}   # 邻居不受牵连


def test_reprojection_prunes_only_the_dropped_objects(repo):
    """② knowhow 重投影:只清**差集**,同 id 重插的活对象一行不碰。

    KO id 是内容稳定 hash,所以「删了再插」里绝大多数对象会原样回来 —— 盲删全部
    成员行会把它们从簇里踢出去(那正是 `_delete_object_id_batch` 的清理刻意不被
    复用在这条路上的原因)。"""
    notebook = repo.create_notebook(NotebookCreate(name="reproject"))
    _seed_busy_notebook(repo, notebook.id)
    store = repo._runtime.knowledge_lifecycle.knowledge
    keep = ["ko-old0", "ko-old1", "kl-old0"]

    with repo._write() as db:
        pruned = store.prune_cluster_rows_for_source(
            db, notebook.id, "src-A", keep_object_ids=keep)

    members = _cluster_members(repo, notebook.id)
    assert set(keep) <= members                    # 活对象保留
    assert not [m for m in members if m.startswith(("ko-old", "kl-old"))
                and m not in keep]                 # 差集清光
    assert pruned == 13                            # 12 concept + 4 claim - 3 keep


def test_teardown_prunes_every_object_of_the_source(repo):
    """② 删表路径:没有重插,该源全部对象的成员行都走。"""
    notebook = repo.create_notebook(NotebookCreate(name="teardown"))
    _seed_busy_notebook(repo, notebook.id)
    store = repo._runtime.knowledge_lifecycle.knowledge

    with repo._write() as db:
        pruned = store.prune_cluster_rows_for_source(db, notebook.id, "src-A")

    assert pruned == 16
    assert _cluster_members(repo, notebook.id) == set()


def test_reprojection_prune_is_scoped_to_the_notebook(repo):
    """差集清理同样带 notebook 谓词。"""
    first = repo.create_notebook(NotebookCreate(name="prune-own"))
    other = repo.create_notebook(NotebookCreate(name="prune-other"))
    _seed_busy_notebook(repo, first.id)
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("cc-other-prune", other.id, "K-collide", "ko-old0",
             "Collide", "concept", "", NOW))

    store = repo._runtime.knowledge_lifecycle.knowledge
    with repo._write() as db:
        store.prune_cluster_rows_for_source(db, first.id, "src-A")

    assert _cluster_members(repo, first.id) == set()
    assert _cluster_members(repo, other.id) == {"ko-old0"}


def test_orphan_producing_paths_are_exhaustively_registered():
    """闸的正确性建立在「全仓零 orphan 生产者」上,这条把那个穷举钉死。

    豁免清单**必须为空**:每一条删 `knowledge_objects` 的路径都要在自己的写事务里
    清掉簇行(或整表清空)。新增一条 DELETE、或给 `delete_objects_by_source*` 接上
    一个不做差集清理的服务层调用点,这条就红。"""
    import ast
    import re
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]

    # ① 两个后端各恰好五条 DELETE FROM knowledge_objects,且每条所在的函数都必须
    #    同时删 concept_clusters —— 除了 knowhow 那两条:它们的清理由服务层在同一
    #    写事务里经 prune_cluster_rows_for_source 完成(见 ② ③)。第五条是
    #    batch-3-W1 T-5a 的 drain_notebook_graph_rows_page(delete_notebook_kg
    #    预排水的 knowledge_objects 页):它按 _delete_object_id_batch 同形在同一
    #    事务里删本页对象的簇成员行,由下面的 AST 扫描(函数体须含
    #    concept_clusters)自动覆盖——排水不是新的孤儿生产者。
    KNOWHOW_DELETES = {"delete_objects_by_source", "delete_objects_by_source_and_row"}
    WHOLE_TABLE_WIPES = {"delete_notebook_graph_rows"}
    for adapter in ("sqlite", "postgres"):
        path = backend / "app" / "repositories" / adapter / "knowledge_store.py"
        source = path.read_text(encoding="utf-8")
        assert source.count("DELETE FROM knowledge_objects") == 5, adapter
        tree = ast.parse(source, filename=str(path))
        unguarded = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, node) or ""
            if "DELETE FROM knowledge_objects" not in body:
                continue
            if node.name in KNOWHOW_DELETES:
                continue
            if node.name in WHOLE_TABLE_WIPES:
                # 整库重建:整表清空 concept_clusters。SQLite 在函数体里循环表名
                # 字面量,PostgreSQL 走模块级 `_GRAPH_RESET_TABLES` —— 两种写法
                # 都单独断言过(见下面 ①'),这里只要求它确实是那两种之一。
                assert ("concept_clusters" in body
                        or "_GRAPH_RESET_TABLES" in body), f"{adapter}:{node.name}"
                continue
            if "concept_clusters" not in body:
                unguarded.append(f"{adapter}:{node.name}")
        assert unguarded == [], unguarded

    # ①' 整库重建那条的两种写法各自点名 concept_clusters(PG 的表名在模块级常量
    #     里,函数体文本看不见它)。
    from app.repositories.postgres import knowledge_store as pg_knowledge_store

    assert "concept_clusters" in pg_knowledge_store._GRAPH_RESET_TABLES
    sqlite_source = (backend / "app" / "repositories" / "sqlite"
                     / "knowledge_store.py").read_text(encoding="utf-8")
    assert 'for table in (\n            "concept_clusters"' in sqlite_source

    # ② `delete_objects_by_source*` 在生产代码里只有 knowhow 投影这一个**调用**点。
    call_re = re.compile(r"\.delete_objects_by_source(_and_row)?\(")
    services = backend / "app" / "services"
    callers = {
        path.relative_to(backend).as_posix()
        for path in services.rglob("*.py")
        if call_re.search(path.read_text(encoding="utf-8"))
    }
    assert callers == {"app/services/knowhow/projection.py"}, callers

    # ③ 那个文件里,每个删对象的函数都必须在同一函数体内调
    #    `prune_cluster_rows_for_source`。**豁免清单为空** —— 漏一处就是又出现了
    #    一个 orphan 生产者,而进程本地的兜底闸对跨 worker 的生产者是压制不住的。
    projection = services / "knowhow" / "projection.py"
    tree = ast.parse(projection.read_text(encoding="utf-8"), filename=str(projection))
    deleting, pruning = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {
            inner.func.attr if isinstance(inner.func, ast.Attribute) else
            getattr(inner.func, "id", "")
            for inner in ast.walk(node) if isinstance(inner, ast.Call)
        }
        if names & KNOWHOW_DELETES:
            deleting.add(node.name)
            if "prune_cluster_rows_for_source" in names:
                pruning.add(node.name)
    assert deleting, "projection.py 里没找到删除调用点,守卫可能已经失配"
    assert deleting == pruning, sorted(deleting - pruning)

    # ④ 兜底闸不得再依赖任何跨进程信号(被 codex 判 P1 的 tick 版本已整体移除)。
    assert not (services / "orphan_cluster_signal.py").exists()


def test_bruteforce_bridge_reads_only_concept_vectors(repo, monkeypatch):
    """无 ANN 暴力分支的向量读收窄到 concept:它唯一的消费方
    (`detect_bridge_candidates`)只按 concept 的 object_id 取值,claim 向量
    (生产上占对象数的大头)以前是解码、验维、截断之后原样丢掉。"""
    from app.services.kg_merge import _norm

    def _vec(first):
        return [first] + [0.0] * (DIM - 1)

    notebook = repo.create_notebook(NotebookCreate(name="vec-scope"))
    _seed(repo, notebook.id,
          objects=[("ko-old", "concept", "Expert Routing", "src-A", None, _vec(1.0)),
                   # 带向量的 claim:收窄前它会被读回来,收窄后不该出现。
                   ("kl-old", "claim", "an old claim about routing", "src-A", None,
                    _vec(0.5)),
                   ("ko-dead", "concept", "Retired Concept", "src-A",
                    None, _vec(0.7)),
                   ("ko-new", "concept", "MoE Gating", "src-B", None, _vec(0.99))],
          clusters=[("K-" + _norm("Expert Routing"), "ko-old", "Expert Routing",
                     "concept")])
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
                   ("ko-dead",))

    seen = []
    store = repo._runtime.knowledge_lifecycle.knowledge
    original = store.concept_embedding_rows

    def spy(db, notebook_id):
        rows = original(db, notebook_id)
        seen.extend(row["object_id"] for row in rows)
        return rows

    monkeypatch.setattr(store, "concept_embedding_rows", spy)
    repo.incremental_fuse_source(notebook.id, "src-B")

    assert seen                                  # 真的走到了暴力分支
    # 谓词与 `incremental_object_rows(..., 'concept')` 逐字相同:非 concept 与
    # deprecated 都不进来,消费方读得到的键一个不少。
    assert set(seen) == {"ko-old", "ko-new"}


def test_legacy_vector_seam_actually_reads_more_rows(repo, monkeypatch):
    """正面钉住接缝 ⑤ 的判别力:同一份夹具下,legacy 全类型读回的行**严格多于**
    收窄读。缺了这条,差分用例可能在两臂读回同一份行的夹具上空转,绿得毫无意义。"""
    from app.services.kg_merge import _norm

    def _vec(first):
        return [first] + [0.0] * (DIM - 1)

    notebook = repo.create_notebook(NotebookCreate(name="seam-power"))
    _seed(repo, notebook.id,
          objects=[("ko-old", "concept", "Expert Routing", "src-A", None, _vec(1.0)),
                   ("kl-old", "claim", "an old claim", "src-A", None, _vec(0.5)),
                   ("ko-dead", "concept", "Retired", "src-A", None, _vec(0.7)),
                   ("ko-new", "concept", "MoE Gating", "src-B", None, _vec(0.99))],
          clusters=[("K-" + _norm("Expert Routing"), "ko-old", "Expert Routing",
                     "concept")])
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
                   ("ko-dead",))

    store = repo._runtime.knowledge_lifecycle.knowledge
    with repo._connect() as db:
        narrowed = {row["object_id"] for row in
                    store.concept_embedding_rows(db, notebook.id)}
        legacy = {row["object_id"] for row in db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
            (notebook.id,))}
    assert narrowed < legacy                      # 真子集,不是相等
    assert legacy - narrowed == {"kl-old", "ko-dead"}
