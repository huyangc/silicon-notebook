# backend/tests/test_checkup_service.py
"""P2·T2: CheckupService — 只读体检聚合(H2–H8)。

覆盖两层:
- 真 repo 集成:H2/H3/H6 走**新增的** store SQL(sources_without_elements / T1 的
  sources_missing_chunks / pending_kg_source_count)+ 活跃租约的 Python 后置减法 + 聚合。
  每项各造命中/不命中两态,断言 count / sample / fix。
- 直接构造 + 窄 seam 假件:H4/H5(service 把 maintenance 计数映射成 count+fix)、
  H7(state='stale' 映射 + fail-soft)、H8(磁盘 manifest 身份缓存:健康按身份命中不重探/身份变
  重探/损坏从不缓存以致重建即自愈/未建短路/LRU 淘汰 + 磁盘探针三分支 + never-raise)。

红线:service 只读、零模型调用、零写库——测试只 seed 已有表,断言聚合结果。
"""
import uuid

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import sqlite_repository
from app.services.checkup import CheckupService, probe_scale_index_integrity
from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients

_NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = sqlite_repository.SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


# ------------------------------------------------------------------ helpers
def _check(result, code):
    return next(c for c in result.checks if c.code == code)


def _seed_source(
    repo,
    notebook_id,
    *,
    source_type="document",
    parse_status="extracted",
    chunked_at=None,
    n_elements=0,
):
    sid = f"src-{uuid.uuid4().hex[:8]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,parse_status,chunked_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, notebook_id, "S", source_type, parse_status, chunked_at, _NOW, _NOW),
        )
        for i in range(1, n_elements + 1):
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", f"text {i}", "{}", _NOW),
            )
    return sid


def _seed_completed_kg(repo, notebook_id, source_id):
    """给 source 挂一个 KG object(无 extraction_run → COALESCE 判 'completed'),
    使其从 pending_kg_source_count 里排除(H6 不命中态)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,source_id,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"ko-{uuid.uuid4().hex[:8]}",
                notebook_id,
                "concept",
                "approved",
                source_id,
                "{}",
                "[]",
                _NOW,
                _NOW,
            ),
        )


def _stamp_lease(repo, source_id):
    si = repo._runtime.source_ingestion
    with si._active_sources_lock:
        si._active_sources[source_id] = si._active_sources.get(source_id, 0) + 1


def _service(repo, notebook_id="nb-x", **overrides):
    """直接构造 CheckupService,窄 seam 默认全「不命中」,按需覆写。database 复用 repo 的
    (H2/H3/H6 对空/不存在 notebook 返回 0),其余 seam 全假件——单测 service 的映射/缓存
    逻辑而不碰真索引/真 maintenance。"""
    defaults = dict(
        database=repo._runtime.database,
        count_missing_chunk_vectors=lambda nb, exclude: 0,
        count_missing_element_vectors=lambda nb, exclude: 0,
        scale_index_state=lambda nb: "indexed",
        # 默认每次返回全新 sentinel → H7 memo 永不命中,seam-injection 测试仍见每次 scale_index_state
        # 调用(缓存行为由下面注入受控签名的专测覆盖)。
        index_state_signature=lambda nb: object(),
        index_manifest_identity=lambda nb: (True, "ver-const"),
        probe_index_integrity=lambda nb: 0,
        active_source_ids=lambda: set(),
        now=lambda: _NOW,
        event_log=None,
    )
    defaults.update(overrides)
    return CheckupService(**defaults)


# ------------------------------------------------------------- composition
def test_runtime_composes_checkup(repo):
    assert isinstance(repo._runtime.checkup, CheckupService)


def test_healthy_fresh_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    result = repo._runtime.checkup.run(nb.id)
    assert result.notebook_id == nb.id
    assert result.checked_at
    assert {c.code for c in result.checks} == {"H2", "H3", "H4", "H5", "H6", "H7", "H8"}
    assert all(c.count == 0 for c in result.checks)
    assert result.healthy is True
    # fix 枚举逐项钉死(内部契约,前端映射依赖它稳定)。
    fixes = {c.code: c.fix for c in result.checks}
    assert fixes == {
        "H2": "reparse",
        "H3": "reparse",
        "H4": "backfill_vectors",
        "H5": "backfill_vectors",
        "H6": "extract_kg",
        "H7": "fold_index",
        "H8": "rebuild_index",
    }


# --------------------------------------------------------------------- H2
def test_h2_hit_empty_source(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=0)
    result = repo._runtime.checkup.run(nb.id)
    h2 = _check(result, "H2")
    assert h2.count == 1
    assert h2.sample == [sid]
    assert h2.fix == "reparse"
    assert result.healthy is False


def test_h2_miss_has_elements(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=2)
    assert _check(repo._runtime.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_still_parsing(repo):
    """正在解析(parse_status IN queued/parsing)的空源不算损坏——瞬时无 elements 属正常。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="parsing", n_elements=0)
    _seed_source(repo, nb.id, parse_status="queued", n_elements=0)
    assert _check(repo._runtime.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_metadata_only(repo):
    """metadata-only 源(仅导入元数据、内容待用户上传,source_ingestion.py:315)按**设计**就没有
    source_elements——不是损坏,正确动作是「上传文件」而非 reparse。白名单谓词把它排除
    (评审阻塞项:黑名单只排 queued/parsing 会把它误报成损坏+建议 reparse)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="metadata-only", n_elements=0)
    assert _check(repo._runtime.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_failed_parse(repo):
    """解析已明确失败(parse_status='failed')的源无 elements 不算 H2——失败已作为错误态呈现
    给用户,不是「看着成功却静默没落 elements」的那种损坏,不该被体检当空源复报。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="failed", n_elements=0)
    assert _check(repo._runtime.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_hidden_source(repo):
    """隐藏合成源(memory/knowhow)不走文档解析,无 elements 是常态,不该被 H2 捞出。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, source_type="memory", parse_status="extracted", n_elements=0)
    _seed_source(repo, nb.id, source_type="knowhow", parse_status="extracted", n_elements=0)
    assert _check(repo._runtime.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_active_lease(repo):
    """在途处理(活跃租约里)的空源被 service 层减法排除——即便 SQL 候选集命中。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=0)
    _stamp_lease(repo, sid)
    assert _check(repo._runtime.checkup.run(nb.id), "H2").count == 0


# --------------------------------------------------------------------- H3
def test_h3_hit_missing_chunks(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", chunked_at=None, n_elements=2)
    h3 = _check(repo._runtime.checkup.run(nb.id), "H3")
    assert h3.count == 1
    assert h3.sample == [sid]
    assert h3.fix == "reparse"


def test_h3_miss_chunked_marker_set(repo):
    """chunked_at 有值(分块成功,含纯标题 0-chunk)→ 不是缺分块。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=2)
    assert _check(repo._runtime.checkup.run(nb.id), "H3").count == 0


def test_h3_miss_active_lease(repo):
    """在途 reparse 的源瞬时 chunked_at=NULL 属正常,被活跃租约减法排除。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", chunked_at=None, n_elements=2)
    _stamp_lease(repo, sid)
    assert _check(repo._runtime.checkup.run(nb.id), "H3").count == 0


# --------------------------------------------------------------------- H6
def _bump_kg_seq(repo, notebook_id):
    """裸 SQL seed 不 bump kg_mutation_seq,而 H6 走 seq-gated 计数缓存
    (visible_pending_kg_source_count)——真实加源(process_source)会 bump seq、令该缓存失效。
    H6 用例补这一下,否则先前被别处(NotebookSummary 看板)按旧 seq 填过的缓存会返回陈旧 0。"""
    repo._mark_unified_kg_dirty(notebook_id)


def test_h6_hit_pending_kg(repo):
    """有 elements、无任何(completed)KG → pending。chunked_at 置位以隔离掉 H3。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=1)
    _bump_kg_seq(repo, nb.id)
    result = repo._runtime.checkup.run(nb.id)
    h6 = _check(result, "H6")
    assert h6.count == 1
    assert h6.fix == "extract_kg"
    assert h6.sample == []  # 计数型,不返回样本


def test_h6_miss_completed_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=1)
    _seed_completed_kg(repo, nb.id, sid)
    _bump_kg_seq(repo, nb.id)
    assert _check(repo._runtime.checkup.run(nb.id), "H6").count == 0


def test_h6_miss_hidden_synthetic_source(repo):
    """knowhow/memory 合成源有 elements、却不走文档 KG 抽取——H6 用 visible_ 口径
    (排除 memory/knowhow),不该把它算成「待分析」。否则挂 knowhow 表的库会与看板
    「知识图谱」行(同用 visible_ 口径)自相矛盾、healthy 恒 false、点分析新增也修不掉
    (评审阻塞项:H6 曾误用全集 pending_kg_source_count)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 合成源有 elements、无 completed KG:全集口径会命中 H6,visible_ 口径应排除。
    _seed_source(repo, nb.id, source_type="knowhow", parse_status="extracted",
                 chunked_at=_NOW, n_elements=1)
    _seed_source(repo, nb.id, source_type="memory", parse_status="extracted",
                 chunked_at=_NOW, n_elements=1)
    _bump_kg_seq(repo, nb.id)
    assert _check(repo._runtime.checkup.run(nb.id), "H6").count == 0


# ------------------------------------------------------------------ H4/H5
def test_h4_maps_missing_chunk_vectors(repo):
    hit = _service(repo, count_missing_chunk_vectors=lambda nb, exclude: 3)
    h4 = _check(hit.run("nb-x"), "H4")
    assert h4.count == 3 and h4.fix == "backfill_vectors" and h4.sample == []
    assert hit.run("nb-x").healthy is False
    miss = _service(repo, count_missing_chunk_vectors=lambda nb, exclude: 0)
    assert _check(miss.run("nb-x"), "H4").count == 0


def test_h5_maps_missing_element_vectors(repo):
    hit = _service(repo, count_missing_element_vectors=lambda nb, exclude: 5)
    h5 = _check(hit.run("nb-x"), "H5")
    assert h5.count == 5 and h5.fix == "backfill_vectors" and h5.sample == []
    miss = _service(repo, count_missing_element_vectors=lambda nb, exclude: 0)
    assert _check(miss.run("nb-x"), "H5").count == 0


def test_count_missing_element_vectors_excludes_given_sources(repo):
    """maintenance 计数的 exclude_source_ids 真排除指定源(H5 减活跃租约的底座,codex)。
    chunk 侧 count_missing_chunk_vectors 是对称 SQL(AND source_id NOT IN),同款。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, parse_status="extracted", n_elements=2)  # 有 element、无向量
    b = _seed_source(repo, nb.id, parse_status="extracted", n_elements=3)
    mnt = repo._runtime.maintenance_component
    assert mnt.count_missing_element_vectors(nb.id) == 5           # 全缺(2+3)
    assert mnt.count_missing_element_vectors(nb.id, {a}) == 3      # 排除 a → 只剩 b 的 3
    assert mnt.count_missing_element_vectors(nb.id, {a, b}) == 0   # 全排除


def test_missing_element_rows_only_source_id(repo):
    """missing_element_embedding_rows 的 only_source_id 只取某源的缺向量行——体检 backfill 逐源
    持分块锁、锁内按此现读现嵌(codex P1:element id 在 reparse 时复用,锁内读→嵌避免挂陈旧向量)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, parse_status="extracted", n_elements=2)
    b = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    mnt = repo._runtime.maintenance_component
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id)} == {a, b}
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id, only_source_id=a)} == {a}
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id, only_source_id=b)} == {b}


def test_missing_vector_source_ids_are_distinct_and_lightweight(repo):
    """codex 第2轮 P1:missing_*_vector_source_ids 只返 DISTINCT source_id(不物化正文),
    判据与 missing_*_embedding_rows 一致——backfill 用它做廉价源发现,避免大库上把每行全文
    读进内存(GB 级/OOM)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, parse_status="extracted", n_elements=3)  # 一个源 3 个缺 element
    b = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    with repo._write() as db:  # 给 a 造一个缺向量的 chunk(chunk 侧正例)
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) VALUES (?,?,?,?,?)",
            (f"ch-{a}-1", nb.id, a, "chunk text", _NOW),
        )
    mnt = repo._runtime.maintenance_component
    # element 侧:两个源各有缺向量 element,去重后是 {a, b}(不是 4 个行)。
    assert set(mnt.missing_element_vector_source_ids(nb.id)) == {a, b}
    # 与 rows 版的 source_id 集合逐一致(判据同款,只投影不同)。
    assert set(mnt.missing_element_vector_source_ids(nb.id)) == {
        r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id)
    }
    # chunk 侧:只有 a 有 chunk 且缺向量 → {a}。
    assert set(mnt.missing_chunk_vector_source_ids(nb.id)) == {a}


def test_backfill_job_embeds_under_per_source_lock(repo, monkeypatch):
    """_backfill_vectors_job 逐源持 P1.5 分块锁、锁内读→嵌(codex P1:与 reparse 的 element 换血
    互斥——process_source 在同一把锁内先 clear_embeddings 再换 elements,复用 el-<sid>-<idx> id;
    补齐若在锁外读旧代行、嵌后在换血之后落库,会给新文本挂上永久陈旧向量)。验证嵌入发生时本源
    分块锁确处于持有态,且只嵌本源的行。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=2)
    monkeypatch.setattr(repo, "configured", lambda wid: True)
    from app.api import source_routes

    ingestion = repo._runtime.source_ingestion
    mnt = repo._runtime.maintenance_component
    seen: dict = {}

    def _spy_elem(notebook_id, items):
        # 嵌入时,本源的分块锁必须处于持有态(锁内读→嵌);记录被嵌的源集。
        seen["locked"] = ingestion._source_chunk_lock(sid).locked()
        seen["sources"] = {it["source_id"] for it in items}
        return len(items)

    monkeypatch.setattr(mnt, "embed_elements_batch", _spy_elem)
    monkeypatch.setattr(mnt, "embed_chunks_batch", lambda notebook_id, items: None)
    source_routes._backfill_vectors_job(repo, nb.id)
    assert seen.get("locked") is True   # 锁内嵌
    assert seen.get("sources") == {sid}


def test_h4_h5_pass_active_lease_snapshot_to_counts(repo):
    """H4/H5 把活跃租约快照传给计数 seam(codex:正在嵌入的源 chunk/element 已在、向量还没落,
    是正常在途,不该算缺向量——由 count 的 exclude_source_ids 排除)。"""
    seen: dict[str, set] = {}
    svc = _service(
        repo,
        count_missing_chunk_vectors=lambda nb, exclude: seen.__setitem__("chunk", set(exclude)) or 0,
        count_missing_element_vectors=lambda nb, exclude: seen.__setitem__("elem", set(exclude)) or 0,
        active_source_ids=lambda: {"src-embedding"},
    )
    svc.run("nb-x")
    assert seen["chunk"] == {"src-embedding"}
    assert seen["elem"] == {"src-embedding"}


# --------------------------------------------------------------------- H7
def test_h7_hit_stale(repo):
    svc = _service(repo, scale_index_state=lambda nb: "stale")
    h7 = _check(svc.run("nb-x"), "H7")
    assert h7.count == 1 and h7.fix == "fold_index"


def test_h7_miss_indexed(repo):
    svc = _service(repo, scale_index_state=lambda nb: "indexed")
    assert _check(svc.run("nb-x"), "H7").count == 0


def test_h7_fail_soft_on_probe_error(repo):
    def _boom(nb):
        raise RuntimeError("delta probe blew up")

    svc = _service(repo, scale_index_state=_boom)
    # 探针异常不 raise 出体检热路径,保守判「未过期」。
    assert _check(svc.run("nb-x"), "H7").count == 0


def test_h7_caches_by_signature(repo):
    """H7 按廉价签名 memo(codex P2):签名不变 → 复用、不重跑昂贵状态计算;签名变(新数据
    bump seq / rebuild 换 mtime)→ 重跑一次。"""
    sig = {"v": ("sig-A",)}
    calls = []

    def _state(nb):
        calls.append(nb)
        return "stale"

    svc = _service(repo, index_state_signature=lambda nb: sig["v"], scale_index_state=_state)
    assert _check(svc.run("nb-x"), "H7").count == 1
    assert _check(svc.run("nb-x"), "H7").count == 1
    assert len(calls) == 1  # 同签名,第二次命中缓存不重跑昂贵 status()

    sig["v"] = ("sig-B",)
    assert _check(svc.run("nb-x"), "H7").count == 1
    assert len(calls) == 2  # 签名变,重跑一次


def test_h7_error_result_not_cached(repo):
    """H7 状态探针异常:保守判未过期(0)且**不写缓存**——即便签名不变,下次仍现探
    (不把偶发失败粘成长期误判)。"""
    state = {"boom": True}
    calls = []

    def _state(nb):
        calls.append(nb)
        if state["boom"]:
            raise RuntimeError("delta probe blew up")
        return "indexed"

    svc = _service(repo, index_state_signature=lambda nb: ("sig-const",), scale_index_state=_state)
    assert _check(svc.run("nb-x"), "H7").count == 0  # 异常 → 0
    state["boom"] = False
    assert _check(svc.run("nb-x"), "H7").count == 0  # 未缓存 → 同签名仍现探(此刻 indexed → 0)
    assert len(calls) == 2


# --------------------------------------------------------------------- H8
def test_h8_hit_corrupt(repo):
    svc = _service(repo, probe_index_integrity=lambda nb: 1)
    h8 = _check(svc.run("nb-x"), "H8")
    assert h8.count == 1 and h8.fix == "rebuild_index"


def test_h8_caches_healthy_by_manifest_identity(repo):
    """健康结论按磁盘 manifest 身份缓存:身份不变 → 命中缓存不重探;身份变(rebuild/fold 换新
    manifest.version)→ 重探一次。"""
    ident = {"v": (True, "ver-A")}
    calls = []

    def _probe(nb):
        calls.append(nb)
        return 0  # healthy

    svc = _service(
        repo, index_manifest_identity=lambda nb: ident["v"], probe_index_integrity=_probe
    )
    assert _check(svc.run("nb-x"), "H8").count == 0
    assert _check(svc.run("nb-x"), "H8").count == 0
    assert len(calls) == 1  # 同 manifest 身份,第二次命中缓存不重探

    ident["v"] = (True, "ver-B")  # rebuild/fold 换新 manifest.version
    assert _check(svc.run("nb-x"), "H8").count == 0
    assert len(calls) == 2  # 身份变,重探一次


def test_h8_corrupt_never_cached_so_rebuild_clears_it(repo):
    """评审 B1 假阳性防线:损坏结论**从不入缓存** → 每次现探 → 用户点重建修好后立刻现探为
    健康,不被旧损坏缓存粘住。⚠ 关键用**不变的 manifest 身份**模拟最坏情形(重建但数据未变、
    写回同一 version):若把损坏也按身份缓存,这里就会永远报损坏、清不掉。"""
    state = {"result": 1}  # 先损坏
    calls = []

    def _probe(nb):
        calls.append(nb)
        return state["result"]

    svc = _service(
        repo, index_manifest_identity=lambda nb: (True, "ver-same"),
        probe_index_integrity=_probe,
    )
    assert _check(svc.run("nb-x"), "H8").count == 1
    assert _check(svc.run("nb-x"), "H8").count == 1
    assert len(calls) == 2  # 损坏不缓存,每次现探

    state["result"] = 0  # 用户重建修好(manifest 身份仍 ver-same)
    assert _check(svc.run("nb-x"), "H8").count == 0  # 立刻自愈
    assert len(calls) == 3


def test_h8_not_built_short_circuits_without_probe(repo):
    """manifest 不存在 → 未建索引 → 判 0 且**不 load**(廉价短路,连探针都不调)。"""
    calls = []
    svc = _service(
        repo, index_manifest_identity=lambda nb: (False, None),
        probe_index_integrity=lambda nb: calls.append(nb) or 1,
    )
    assert _check(svc.run("nb-x"), "H8").count == 0
    assert calls == []


def test_h8_fail_soft_when_manifest_identity_unavailable(repo):
    def _boom(nb):
        raise RuntimeError("no manifest")

    probed = []
    svc = _service(
        repo,
        index_manifest_identity=_boom,
        probe_index_integrity=lambda nb: probed.append(nb) or 1,
    )
    # manifest 身份取不到 → 保守判未损坏、且根本不 probe(无从判定不如不误报)。
    assert _check(svc.run("nb-x"), "H8").count == 0
    assert probed == []


def test_h8_probe_failure_not_cached(repo):
    """探针抛异常那次保守判 0、不缓存;下次同身份仍现探,拿到真结论。"""
    state = {"boom": True}
    calls = []

    def _probe(nb):
        calls.append(nb)
        if state["boom"]:
            raise IOError("disk hiccup")
        return 1

    svc = _service(
        repo, index_manifest_identity=lambda nb: (True, "ver-A"),
        probe_index_integrity=_probe,
    )
    assert _check(svc.run("nb-x"), "H8").count == 0  # 探针失败,保守判未损坏
    state["boom"] = False
    # 上次没缓存(且损坏也不缓存),故同身份会再 probe 一次,拿到真结论 1。
    assert _check(svc.run("nb-x"), "H8").count == 1
    assert len(calls) == 2


def test_h8_lru_evicts_least_recently_used(repo, monkeypatch):
    """健康缓存 LRU 有界:超过上界淘汰最久未访问的 nb。钉住 popitem(last=False)——
    改成 last=True(淘汰错端)会让这条红。"""
    import app.services.checkup as checkup_mod

    monkeypatch.setattr(checkup_mod, "_H8_CACHE_MAX", 2)
    calls = []
    svc = _service(
        repo,
        index_manifest_identity=lambda nb: (True, f"ver-{nb}"),
        probe_index_integrity=lambda nb: calls.append(nb) or 0,  # 全健康,进缓存
    )
    svc.run("nb-1")
    svc.run("nb-2")
    svc.run("nb-1")  # nb-1 命中缓存(move_to_end 成最近),不重探
    assert calls == ["nb-1", "nb-2"]
    svc.run("nb-3")  # 越界 → 淘汰最久未访问的 nb-2(不是刚访问的 nb-1)
    svc.run("nb-2")  # nb-2 已被淘汰 → 重探
    assert calls == ["nb-1", "nb-2", "nb-3", "nb-2"]


def test_h8_healthy_cache_expires_after_ttl(repo, monkeypatch):
    """健康缓存有界存活:manifest 身份不变时 TTL 内命中不重探,超 TTL 即重探一次
    (codex:探过后外部截断/损坏而不动 manifest,身份不变靠 TTL 复检才能重新发现)。"""
    import app.services.checkup as checkup_mod

    clock = [1000.0]
    monkeypatch.setattr(checkup_mod.time, "monotonic", lambda: clock[0])
    calls = []
    svc = _service(
        repo,
        index_manifest_identity=lambda nb: (True, "ver-fixed"),  # 身份始终不变
        probe_index_integrity=lambda nb: calls.append(nb) or 0,
    )
    svc.run("nb-x")
    svc.run("nb-x")
    assert len(calls) == 1  # TTL 内、身份不变 → 命中缓存
    clock[0] += checkup_mod._H8_CACHE_TTL + 1  # 时钟越过 TTL
    svc.run("nb-x")
    assert len(calls) == 2  # 身份仍不变,但超 TTL → 重探一次


# --------------------------- module-level disk probe (H8 判据落点) ----------
def test_probe_no_manifest_is_not_corrupt(tmp_path):
    # manifest.json 不存在 = 未建索引,不是损坏。
    assert probe_scale_index_integrity(tmp_path) == 0


def test_probe_manifest_present_load_none_is_corrupt(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        "app.services.kg.scale_index.load_scale_index", lambda d: None
    )
    assert probe_scale_index_integrity(tmp_path) == 1


def test_probe_manifest_present_load_ok(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        "app.services.kg.scale_index.load_scale_index", lambda d: object()
    )
    assert probe_scale_index_integrity(tmp_path) == 0


def test_probe_never_raises(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")

    def _boom(d):
        raise IOError("disk gone")

    monkeypatch.setattr("app.services.kg.scale_index.load_scale_index", _boom)
    assert probe_scale_index_integrity(tmp_path) == 0


def test_probe_flags_missing_ann_binary(tmp_path, monkeypatch):
    """load_scale_index 只校验 .npy、不校验 ANN 二进制(懒加载):主 ANN 有标签(n_ann>0)却
    没 ann.bin 文件 → 判损坏(codex),否则坏索引报健康、检索时才开不了。"""
    (tmp_path / "manifest.json").write_text("{}")

    class _Idx:
        ann_labels = ["a", "b"]                 # n_ann>0
        ann_path = str(tmp_path / "ann.bin")    # 但文件不存在

    monkeypatch.setattr(
        "app.services.kg.scale_index.load_scale_index", lambda d: _Idx()
    )
    assert probe_scale_index_integrity(tmp_path) == 1   # ann.bin 缺 → 损坏
    (tmp_path / "ann.bin").write_bytes(b"x")
    assert probe_scale_index_integrity(tmp_path) == 0   # 补上 → 健康


# ------------------------------------------------------ sample boundedness
def test_h2_sample_capped_at_20(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for _ in range(25):
        _seed_source(repo, nb.id, parse_status="extracted", n_elements=0)
    h2 = _check(repo._runtime.checkup.run(nb.id), "H2")
    assert h2.count == 25  # 计数是全量
    assert len(h2.sample) == 20  # 样本有界
    assert h2.sample == sorted(h2.sample)  # 稳定排序


def test_probe_validates_ann_content_not_just_existence(tmp_path, monkeypatch):
    """codex 第2轮 P2:ann.bin 存在但被**截断/损坏**时,probe 真 load 一次主 ANN 判损坏(返 1),
    不再只看文件存在(load_scale_index 只校验 .npy、ANN 懒加载,截断照样报健康、检索侧才炸)。

    **变异锚点**:把内容级 load 那段删掉(退回只判 os.path.exists)→ 截断分支返 0 → `== 1` 红。"""
    import numpy as np
    import hnswlib
    from types import SimpleNamespace
    from app.services.kg import scale_index as scale_index_mod

    scale_dir = tmp_path / "scale"
    scale_dir.mkdir()
    (scale_dir / "manifest.json").write_text("{}", encoding="utf-8")  # 存在即可(probe 先判存在)
    ann_path = str(scale_dir / "ann.bin")
    dim, n = 4, 3
    h = hnswlib.Index(space="cosine", dim=dim)
    h.init_index(max_elements=n, ef_construction=16, M=8)
    h.add_items(np.eye(n, dim, dtype="float32"), list(range(n)))
    h.save_index(ann_path)

    stub = SimpleNamespace(
        ann_labels=["ko-0", "ko-1", "ko-2"], ann_path=ann_path, manifest={"dim": dim}
    )
    monkeypatch.setattr(scale_index_mod, "load_scale_index", lambda d: stub)

    # 健康 ann.bin:内容校验通过 → 0。
    assert probe_scale_index_integrity(str(scale_dir)) == 0
    # 截断 ann.bin:load_index raise → 1(不再假报健康)。
    with open(ann_path, "r+b") as f:
        f.truncate(8)
    assert probe_scale_index_integrity(str(scale_dir)) == 1
