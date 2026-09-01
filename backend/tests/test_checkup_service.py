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
        queries=repo._runtime.queries,  # 真 QueryStore 实例(H2/H3/H6 走真 SQL)
        count_missing_chunk_vectors=lambda nb, exclude: 0,
        count_missing_element_vectors=lambda nb, exclude: 0,
        # 默认恒 0(等价「seq 从不前进」)——H4/H5 memo 的租约键/TTL/事件失效行为不被
        # seq 分量干扰;seq 驱动的失效由注入受控 seq 的专测覆盖。
        kg_version=lambda db, nb: 0,
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
    assert isinstance(repo.checkup, CheckupService)


def test_checkup_h45_wiring_returns_the_epoch_seq_pair_not_a_bare_int(repo):
    """R1 (P1-1, post-review): the REAL ``kg_mutation_seq`` seam wired into
    ``SQLiteRepository.checkup`` (not the fake seam ``_service``/
    ``_counting_service`` inject below) must return ``(kg_reset_epoch,
    kg_mutation_seq)``. PostgreSQL twin:
    ``backend/tests/postgres/test_checkup_h45_cache.py::
    test_checkup_h45_wiring_returns_the_epoch_seq_pair_not_a_bare_int`` —
    both backends must wire the SAME shared ``checkup.h45_version_key``
    helper; this pair of tests is the guard that would have caught PG
    independently wiring a bare ``int`` seam while SQLite had already moved
    to the pair (P1-1's actual finding)."""
    nb = repo.create_notebook(NotebookCreate(name="checkup-h45-wiring")).id
    seam = repo.checkup._kg_version
    with repo._runtime.database.connect() as db:
        version = seam(db, nb)
    assert isinstance(version, tuple) and len(version) == 2
    assert version == (0, 0)

    repo.store_kg(
        nb, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "x"}, "evidence": []}],
        [],
    )
    with repo._connect() as db:
        after_write = seam(db, nb)
    assert after_write == (0, 1)

    repo.delete_notebook_kg(nb)
    with repo._connect() as db:
        after_delete = seam(db, nb)
    assert after_delete == (1, 0)


def test_healthy_fresh_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    result = repo.checkup.run(nb.id)
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
    result = repo.checkup.run(nb.id)
    h2 = _check(result, "H2")
    assert h2.count == 1
    assert h2.sample == [sid]
    assert h2.fix == "reparse"
    assert result.healthy is False


def test_h2_miss_has_elements(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=2)
    assert _check(repo.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_still_parsing(repo):
    """正在解析(parse_status IN queued/parsing)的空源不算损坏——瞬时无 elements 属正常。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="parsing", n_elements=0)
    _seed_source(repo, nb.id, parse_status="queued", n_elements=0)
    assert _check(repo.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_metadata_only(repo):
    """metadata-only 源(仅导入元数据、内容待用户上传,source_ingestion.py:315)按**设计**就没有
    source_elements——不是损坏,正确动作是「上传文件」而非 reparse。白名单谓词把它排除
    (评审阻塞项:黑名单只排 queued/parsing 会把它误报成损坏+建议 reparse)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="metadata-only", n_elements=0)
    assert _check(repo.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_failed_parse(repo):
    """解析已明确失败(parse_status='failed')的源无 elements 不算 H2——失败已作为错误态呈现
    给用户,不是「看着成功却静默没落 elements」的那种损坏,不该被体检当空源复报。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="failed", n_elements=0)
    assert _check(repo.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_hidden_source(repo):
    """隐藏合成源(memory/knowhow)不走文档解析,无 elements 是常态,不该被 H2 捞出。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, source_type="memory", parse_status="extracted", n_elements=0)
    _seed_source(repo, nb.id, source_type="knowhow", parse_status="extracted", n_elements=0)
    assert _check(repo.checkup.run(nb.id), "H2").count == 0


def test_h2_miss_active_lease(repo):
    """在途处理(活跃租约里)的空源被 service 层减法排除——即便 SQL 候选集命中。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=0)
    _stamp_lease(repo, sid)
    assert _check(repo.checkup.run(nb.id), "H2").count == 0


# --------------------------------------------------------------------- H3
def test_h3_hit_missing_chunks(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", chunked_at=None, n_elements=2)
    h3 = _check(repo.checkup.run(nb.id), "H3")
    assert h3.count == 1
    assert h3.sample == [sid]
    assert h3.fix == "reparse"


def test_h3_miss_chunked_marker_set(repo):
    """chunked_at 有值(分块成功,含纯标题 0-chunk)→ 不是缺分块。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=2)
    assert _check(repo.checkup.run(nb.id), "H3").count == 0


def test_h3_miss_active_lease(repo):
    """在途 reparse 的源瞬时 chunked_at=NULL 属正常,被活跃租约减法排除。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", chunked_at=None, n_elements=2)
    _stamp_lease(repo, sid)
    assert _check(repo.checkup.run(nb.id), "H3").count == 0


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
    result = repo.checkup.run(nb.id)
    h6 = _check(result, "H6")
    assert h6.count == 1
    assert h6.fix == "extract_kg"
    assert h6.sample == []  # 计数型,不返回样本


def test_h6_miss_completed_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", chunked_at=_NOW, n_elements=1)
    _seed_completed_kg(repo, nb.id, sid)
    _bump_kg_seq(repo, nb.id)
    assert _check(repo.checkup.run(nb.id), "H6").count == 0


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
    assert _check(repo.checkup.run(nb.id), "H6").count == 0


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
    mnt = repo.maintenance
    assert mnt.count_missing_element_vectors(nb.id) == 5           # 全缺(2+3)
    assert mnt.count_missing_element_vectors(nb.id, {a}) == 3      # 排除 a → 只剩 b 的 3
    assert mnt.count_missing_element_vectors(nb.id, {a, b}) == 0   # 全排除


def test_missing_element_rows_only_source_id(repo):
    """missing_element_embedding_rows 的 only_source_id 只取某源的缺向量行——体检 backfill 逐源
    持分块锁、锁内按此现读现嵌(codex P1:element id 在 reparse 时复用,锁内读→嵌避免挂陈旧向量)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, parse_status="extracted", n_elements=2)
    b = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    mnt = repo.maintenance
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id)} == {a, b}
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id, only_source_id=a)} == {a}
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id, only_source_id=b)} == {b}


def test_missing_element_queries_exclude_memory_and_knowhow(repo):
    """codex 第6轮 P2:knowhow/memory 的 source_elements **设计上**不走通用 element 嵌入(knowhow 只嵌
    生成的 chunk、memory 走独立 memory_embedding)→ H5 的 count/rows/source_ids 三查询都排除它们,否则
    成功投影后仍报 H5 损坏 + backfill 白嵌派生格(触效率红线)。document 源的缺向量 element 仍照常计入。

    **变异锚点**:去掉某个 element 查询的 ``AND s.source_type NOT IN ('memory','knowhow')`` → knowhow/
    memory 的 element 被算进 → 对应断言红。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    doc = _seed_source(repo, nb.id, source_type="document", parse_status="extracted", n_elements=2)
    _seed_source(repo, nb.id, source_type="knowhow", parse_status="extracted", n_elements=3)
    _seed_source(repo, nb.id, source_type="memory", parse_status="extracted", n_elements=4)
    mnt = repo.maintenance
    # 只 document 的 2 个 element 算缺向量;knowhow(3)+ memory(4)被排除。
    assert mnt.count_missing_element_vectors(nb.id) == 2
    assert {r["source_id"] for r in mnt.missing_element_embedding_rows(nb.id)} == {doc}
    assert set(mnt.missing_element_vector_source_ids(nb.id)) == {doc}


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
    mnt = repo.maintenance
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
    mnt = repo.maintenance
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
    是正常在途,不该算缺向量——由 count 的 exclude_source_ids 排除)。

    ⚠ 红线:传进去的是**原样的进程全局快照**,一个字不动——即便里面有不属于本 notebook 的
    源 id(收窄只作用在 memo 键上,见 _h45_missing_vector_counts)。变异锚点:把 run() 里
    传给 seam 的 ``active`` 换成 ``local_active`` → 下面第二条断言红。"""
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
    # 别库(或根本不存在)的租约 id 照样原样进 exclude:排除口径不因收窄而变窄。
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    svc2 = _service(
        repo,
        count_missing_chunk_vectors=lambda nb_, exclude: seen.__setitem__("chunk2", set(exclude)) or 0,
        count_missing_element_vectors=lambda nb_, exclude: seen.__setitem__("elem2", set(exclude)) or 0,
        active_source_ids=lambda: {"src-elsewhere"},
    )
    svc2.run(nb.id)
    assert seen["chunk2"] == {"src-elsewhere"}
    assert seen["elem2"] == {"src-elsewhere"}


# ----------------------------------------- H4/H5 memo(审计批4)
def _counting_service(repo, **overrides):
    """记录 H4/H5 计数 seam 的每次调用(nb, 传进去的活跃租约快照)。"""
    seen: list = []
    svc = _service(
        repo,
        count_missing_chunk_vectors=(
            lambda nb, exclude: seen.append(("chunk", nb, frozenset(exclude))) or 3
        ),
        count_missing_element_vectors=(
            lambda nb, exclude: seen.append(("elem", nb, frozenset(exclude))) or 5
        ),
        **overrides,
    )
    return svc, seen


def test_h45_counts_memoized_so_polling_stops_re_running_the_anti_joins(repo):
    """看板打开 + 修复期间 ~8s 轮询,每次都要跑两条全表 anti-join(element 侧还要逐行
    TRIM/btrim,PG 上强制读 TOAST)。体检是诊断面、不是检索热路径,故按
    (活跃租约快照, TTL) memo。

    变异锚点:把 ``_h45_missing_vector_counts`` 里的缓存命中分支删掉(每次直算)→
    ``len(seen) == 2`` 红。"""
    svc, seen = _counting_service(repo)
    first = svc.run("nb-x")
    second = svc.run("nb-x")
    assert len(seen) == 2                        # 只有第一次真的查了(chunk + element 各一次)
    assert _check(first, "H4").count == 3 and _check(first, "H5").count == 5
    assert _check(second, "H4").count == 3 and _check(second, "H5").count == 5


def test_h45_memo_is_scoped_per_notebook(repo):
    """缓存按 notebook 分槽,别的库不会串到本库的计数上。"""
    svc, seen = _counting_service(repo)
    svc.run("nb-1")
    svc.run("nb-2")
    svc.run("nb-1")
    assert [nb for (_k, nb, _a) in seen] == ["nb-1", "nb-1", "nb-2", "nb-2"]


def test_h45_memo_key_includes_the_active_lease_snapshot(repo):
    """⚠ 红线:**排除活跃租约的口径逐字不变**。缓存键含**本库**这次的租约快照,本库租约一变
    即失配、必然重算——绝不会把某个源在途时算出的数在租约释放后继续端出去。

    变异锚点:把缓存键里的 ``frozenset(local_active)`` 去掉(只按 notebook + TTL 缓存)→
    第二次 run 会命中旧条目,``len(seen) == 4`` 红。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    leases = [set()]
    svc, seen = _counting_service(repo, active_source_ids=lambda: set(leases[0]))
    svc.run(nb.id)
    leases[0] = {sid}
    svc.run(nb.id)
    assert len(seen) == 4                        # 租约变了 → 重算,没有复用
    assert seen[0][2] == frozenset()
    assert seen[2][2] == frozenset({sid})


def test_h45_memo_key_ignores_leases_owned_by_other_notebooks(repo):
    """⚠ 评审 P1:缓存键是**本库**的租约子集,不是进程全局快照。别的库在上传/解析(全局
    ``_active_sources`` 里多出它的源)时,本库的体检必须照常命中缓存——否则「任一别库有活动
    就把每个库的缓存都冲掉」,而本库状态一点没变。

    变异锚点:把 run() 里的 ``notebook_source_ids_among`` 收窄去掉(键直接用全局 ``active``)
    → 第二次 run 失配重算,``len(seen) == 2`` 红。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    other_sid = _seed_source(repo, other.id, parse_status="extracted", n_elements=1)
    leases = [set()]
    svc, seen = _counting_service(repo, active_source_ids=lambda: set(leases[0]))
    svc.run(nb.id)
    leases[0] = {other_sid}                      # 别的库开始解析/嵌入
    svc.run(nb.id)
    assert len(seen) == 2                        # 本库仍命中缓存:别库活动不冲本库
    # 但它确实被原样传给了计数 seam(排除口径不动),只是没进键。
    assert seen[0][2] == frozenset()


def test_h45_memo_single_slot_so_a_finished_repair_is_visible_at_once(repo):
    """单槽(每 notebook 只留最近一条)是刻意的:点「补齐向量」→ job 持源锁(租约变)→
    job 结束(租约变回空)这条时间线上,中间那次已经把修复前 local_active=∅ 的旧条目覆盖
    掉了,最后一次轮询必然重算、立刻看到降下来的计数。多槽缓存会在这里端出修复前的数、
    把「补齐中…」的忙碌态多按住一个 TTL。

    变异锚点:把单槽换成 ``dict[(nb, active_key)]`` 的多槽缓存 → 第三次 run 命中修复前的
    条目,``len(seen) == 6`` 红。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    leases = [set()]
    svc, seen = _counting_service(repo, active_source_ids=lambda: set(leases[0]))
    svc.run(nb.id)                               # 修复前:active=∅
    leases[0] = {sid}
    svc.run(nb.id)                               # 补齐 job 持锁中
    leases[0] = set()
    svc.run(nb.id)                               # job 结束:必须重算,不得复用第一条
    assert len(seen) == 6


def test_notebook_source_ids_among_narrows_to_this_notebook(repo):
    """H4/H5 memo 键用的收窄查询:只留属于本 notebook 的 id,别库的与不存在的都落选。
    空输入不发查询、直接返回空集。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    mine = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    mine2 = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    theirs = _seed_source(repo, other.id, parse_status="extracted", n_elements=1)
    q = repo._runtime.queries
    with repo._runtime.database.connect() as db:
        assert q.notebook_source_ids_among(db, nb.id, set()) == set()
        assert q.notebook_source_ids_among(db, nb.id, {mine}) == {mine}
        assert q.notebook_source_ids_among(
            db, nb.id, {mine, mine2, theirs, "src-nope"}
        ) == {mine, mine2}
        assert q.notebook_source_ids_among(db, other.id, {mine, theirs}) == {theirs}


def test_notebook_source_ids_among_batches_large_id_lists(repo):
    """参数分批(SQLite 绑定变量上限):id 数远超一批时仍返回完整结果、不炸。"""
    import app.repositories.sqlite.query_store as qs_mod

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    real = {_seed_source(repo, nb.id, parse_status="extracted") for _ in range(5)}
    padding = {f"src-absent-{i:05d}" for i in range(1200)}
    q = repo._runtime.queries
    assert qs_mod.QueryStore._SOURCE_COUNT_IN_CHUNK < len(real | padding)
    with repo._runtime.database.connect() as db:
        assert q.notebook_source_ids_among(db, nb.id, real | padding) == real


def test_h45_memo_expires_after_ttl(repo, monkeypatch):
    """**背底** TTL:键(租约+seq)与进程内事件都看不见的跨进程写(离线 CLI 在别的进程
    里补向量/导入)靠它兜底——该场景下 H4/H5 至多陈旧 ``_H45_CACHE_TTL`` 秒(300s)。
    进程内写路径不等 TTL(事件失效,见下面的 invalidation 用例)。口径已写进
    docs/product-and-api*.md 的端点条目。

    变异锚点:去掉 TTL 判定(只比键)→ 时钟推过 TTL 后仍命中,``len(seen) == 4`` 红。"""
    import app.services.checkup as checkup_mod

    clock = [1000.0]
    monkeypatch.setattr(checkup_mod.time, "monotonic", lambda: clock[0])
    svc, seen = _counting_service(repo)
    svc.run("nb-x")
    svc.run("nb-x")
    assert len(seen) == 2                        # TTL 内命中
    clock[0] += checkup_mod._H45_CACHE_TTL + 1
    svc.run("nb-x")
    assert len(seen) == 4                        # 超 TTL → 重算


def test_h45_memo_is_lru_bounded(repo, monkeypatch):
    """进程内缓存有界(同 H7/H8):超上界淘汰最久未访问的 notebook。"""
    import app.services.checkup as checkup_mod

    monkeypatch.setattr(checkup_mod, "_H45_CACHE_MAX", 2)
    svc, seen = _counting_service(repo)
    svc.run("nb-1")
    svc.run("nb-2")
    svc.run("nb-1")                              # 命中 → move_to_end,nb-1 成最近
    assert [nb for (k, nb, _a) in seen if k == "chunk"] == ["nb-1", "nb-2"]
    svc.run("nb-3")                              # 越界 → 淘汰最久未访问的 nb-2
    svc.run("nb-2")                              # 已被淘汰 → 重算
    assert [nb for (k, nb, _a) in seen if k == "chunk"] == [
        "nb-1", "nb-2", "nb-3", "nb-2",
    ]


def test_h45_count_failure_is_not_cached(repo):
    """计数抛错整体上抛(与改动前一致),缓存里不留半份结果——下次仍现算。"""
    boom = [True]

    def _chunk(nb, exclude):
        if boom[0]:
            raise RuntimeError("count boom")
        return 0

    svc = _service(repo, count_missing_chunk_vectors=_chunk)
    with pytest.raises(RuntimeError, match="count boom"):
        svc.run("nb-x")
    boom[0] = False
    assert _check(svc.run("nb-x"), "H4").count == 0   # 没被失败结论粘住


def test_h45_memo_key_includes_kg_mutation_seq(repo):
    """键分量二:kg_mutation_seq。chunk/element 集合本身的增删(build_chunks、
    delete_source 的 FK 级联)bump 它——「删掉一个没向量的源」不经 embedding 写路径、
    也未必持租约,没有 seq 分量就要等背底 TTL(300s)才可见。

    变异锚点:键里去掉 seq 分量(退回纯租约键)→ seq 前进后第三次 run 命中旧条目,
    ``len(seen) == 4`` 红。"""
    seqs = [7]
    svc, seen = _counting_service(repo, kg_version=lambda db, nb: seqs[0])
    svc.run("nb-x")
    svc.run("nb-x")
    assert len(seen) == 2                        # seq 未动 → 命中
    seqs[0] = 8
    svc.run("nb-x")
    assert len(seen) == 4                        # seq 前进 → 失配重算


def test_h45_memo_key_includes_kg_reset_epoch(repo):
    """batch-3-W1 PR-2 (design doc Sec 3.2 table #11): the H4/H5 memo's
    version key is ``(kg_reset_epoch, kg_mutation_seq)``, not a bare seq.
    delete_notebook_kg RESETS kg_mutation_seq to 0 and can legitimately
    re-climb it back to a raw value this memo already cached counts under —
    kg_reset_epoch is what makes that not alias. This test holds the raw
    seq CONSTANT (7 throughout) and advances only the epoch half on the
    SAME service instance (same cache), mirroring exactly the delete+
    reingest scenario the design doc's Sec 3.2 table registers as reader
    #11 — same shape as ``test_h45_memo_key_includes_kg_mutation_seq``
    above, epoch instead of seq.

    变异锚点:``_h45_missing_vector_counts`` 的键去掉 version 分量的 epoch 半
    (退回裸 seq)→ 第三次 run(epoch 已变、seq 未变)会命中第一次的缓存条目,
    ``len(seen) == 4`` 红(本应 6)。"""
    epoch = [0]
    svc, seen = _counting_service(
        repo, kg_version=lambda db, nb: (epoch[0], 7)
    )
    svc.run("nb-x")
    svc.run("nb-x")
    assert len(seen) == 2  # same (epoch=0, seq=7) -> hit

    epoch[0] = 1  # simulated delete_notebook_kg: epoch advances, raw seq (7) unchanged
    svc.run("nb-x")
    assert len(seen) == 4, (
        "(epoch=1, seq=7) must MISS the (epoch=0, seq=7) entry even though "
        "the raw seq coincides -- serving the cached counts here would mean "
        "a post-delete/reingest checkup saw the pre-delete graph's stale "
        "missing-vector counts"
    )


def test_h45_explicit_invalidation_forces_recompute(repo):
    """事件失效通道:向量 embed 成功不 bump seq,「补齐 job 整个落在两次轮询之间」时
    租约快照又回到原值——键的两个分量都不动。invalidate_missing_vector_counts 是那扇
    窗唯一的失效通道(旧方案靠 30s TTL 硬兜的正是它;现在用户盯着看的数是事件级新鲜)。

    变异锚点:invalidate 里去掉 pop(以及代次推进)→ 第二次 run 命中修复前条目,
    ``len(seen) == 2`` 红。"""
    svc, seen = _counting_service(repo)
    svc.run("nb-x")
    svc.invalidate_missing_vector_counts("nb-x")
    svc.run("nb-x")
    assert len(seen) == 4


def test_h45_invalidation_is_per_notebook(repo):
    """失效按 notebook 分槽:nb-2 的嵌入完成不冲 nb-1 的缓存(与「键收窄到本库租约」
    同一动机——别库活动不该让本库白付两条 anti-join)。"""
    svc, seen = _counting_service(repo)
    svc.run("nb-1")
    svc.run("nb-2")
    svc.invalidate_missing_vector_counts("nb-2")
    svc.run("nb-1")                              # 未被 nb-2 的失效波及 → 命中
    assert len(seen) == 4
    svc.run("nb-2")                              # 被失效方 → 重算
    assert len(seen) == 6


def test_h45_invalidate_during_compute_prevents_stale_store(repo):
    """失效代次守卫(镜像 postgres/knowledge_counts_cache 的 epoch):计算已在途、失效
    才到——光 pop 槽拦不住计算完成后的写回把失效**前**的快照钉回去(H4/H5 的计数查询
    在大库上要跑几秒,这扇窗是真实的)。写回前核对 (全局, 本库) 代次,期间被失效就丢弃
    本次结果、下次现算。

    变异锚点:去掉写回前的代次核对 → 陈旧结果被钉回,第二次 run 命中它,
    ``len(seen) == 2`` 红。"""
    svc_box: list = []
    seen: list = []

    def _chunk(nb, exclude):
        seen.append(("chunk", nb, frozenset(exclude)))
        # 模拟:计数查询进行中,嵌入 worker 恰好写完向量、触发失效。
        svc_box[0].invalidate_missing_vector_counts(nb)
        return 3

    svc = _service(
        repo,
        count_missing_chunk_vectors=_chunk,
        count_missing_element_vectors=(
            lambda nb, exclude: seen.append(("elem", nb, frozenset(exclude))) or 5
        ),
    )
    svc_box.append(svc)
    svc.run("nb-x")                              # 计算期间被失效 → 结果不得写回
    svc.run("nb-x")                              # 必须重算,不得命中被钉回的条目
    assert len(seen) == 4


def test_h45_epoch_eviction_fails_closed(repo, monkeypatch):
    """代次表是有界 LRU,**淘汰一条就推进全局代次**(codex #621 R1 的 fail-closed 教训,
    与 postgres/knowledge_counts_cache 同款):在途计算采样到本库默认代次 0 → 本库被失效
    (代次 0→1)→ 别库的失效把这条代次挤出有界表 → 代次读数又退回默认 0、与采样时相同
    ——没有全局代次,写回守卫会误判「没被失效」,把陈旧快照钉回去且再无 seq 兜底。

    变异锚点:淘汰时不推进全局代次 → 写回误放行,第二次 run 命中陈旧条目,
    ``len(seen) == 2`` 红。"""
    import app.services.checkup as checkup_mod

    monkeypatch.setattr(checkup_mod, "_H45_CACHE_MAX", 1)
    svc_box: list = []
    seen: list = []

    def _chunk(nb, exclude):
        seen.append(("chunk", nb, frozenset(exclude)))
        svc_box[0].invalidate_missing_vector_counts(nb)          # 本库代次 0→1
        svc_box[0].invalidate_missing_vector_counts("nb-other")  # 挤出本库的代次条目
        return 3

    svc = _service(
        repo,
        count_missing_chunk_vectors=_chunk,
        count_missing_element_vectors=(
            lambda nb, exclude: seen.append(("elem", nb, frozenset(exclude))) or 5
        ),
    )
    svc_box.append(svc)
    svc.run("nb-x")                              # 写回必须被全局代次拒绝
    svc.run("nb-x")                              # 重算,不得命中
    assert len(seen) == 4


def test_backfill_job_end_invalidates_h45_via_facade_wiring(repo):
    """接线闭环(facade 构造期挂 rt.on_source_vectors_written 转发器 → 补齐 job 结束时
    note_source_vectors_written → checkup memo 失效):真 repo 上灌缺向量的 chunk 与
    element,体检缓存住 H4/H5>0;跑真的 ``_backfill_vectors_job``(逐源持锁、FakeEmbedder
    真嵌入、job 边界一次通知)后,**不推时钟、不动租约、seq 不变**,下一次体检立即归零。

    变异锚点:facade 不挂插槽、或 job 末尾不通知(embed_*_batch 是分页原语、刻意不通知,
    故没有别的失效通道)→ 第二次 run 命中修复前缓存,计数不归零红。"""
    from app.api.source_routes import _backfill_vectors_job

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=1)
    with repo._write() as db:
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ck-{sid}", nb.id, sid, "chunk text", _NOW),
        )
    first = repo.checkup.run(nb.id)
    assert _check(first, "H4").count == 1                        # 缓存住修复前计数
    assert _check(first, "H5").count == 1
    _backfill_vectors_job(repo, nb.id)           # 直接同步跑真 job(不经 scheduler)
    second = repo.checkup.run(nb.id)
    assert _check(second, "H4").count == 0                       # job 边界事件,立即可见
    assert _check(second, "H5").count == 0


def test_backfill_job_notifies_h45_exactly_once(repo, monkeypatch):
    """P1 修复确立的不变量:H4/H5 失效只在 job **边界**发一次,分页原语刻意不通知。
    只断言「失效发生过」的 e2e 挡不住「把通知搬回 embed_*_batch 方法级」的回归——那正是
    被评审否掉的按页放大形态(每页一次失效 × 轮询 = 每轮一对注定同值的全表 anti-join,
    修复中的源被租约排除在计数外)。25 元素 + 页 10 → 3 页、3 次 embed_elements_batch,
    通知仍须恰好一次。

    变异锚点:①删掉 job finally 的通知 → calls == [] 红;②把通知搬回
    embed_elements_batch 方法级 → 3 页 3 次,calls == [nb] 红。"""
    from app.api import source_routes

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, parse_status="extracted", n_elements=25)
    monkeypatch.setattr(
        repo, "configured", lambda wid: wid == "source_element_embedding"
    )
    monkeypatch.setattr(source_routes, "_BACKFILL_PAGE_ROWS", 10)   # 25 行 → 3 页
    calls: list = []
    forward = repo._runtime.on_source_vectors_written
    repo._runtime.on_source_vectors_written = (
        lambda notebook_id: (calls.append(notebook_id), forward(notebook_id))[-1]
    )
    source_routes._backfill_vectors_job(repo, nb.id)
    assert calls == [nb.id]


def test_embed_source_completion_invalidates_h45(repo):
    """整源嵌入(ingestion / re-embed 路径的边界)完成也通知一次:embed_source 的
    finally 每源一次、无按页放大。变异锚点:删掉 embed_source finally 里的通知 →
    第二次 run 命中旧 H5(seq 与租约都没动,没有别的失效通道)红。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=2)
    assert _check(repo.checkup.run(nb.id), "H5").count == 2      # 缓存住修复前计数
    repo._runtime.source_embedding.embed_source(sid)
    assert _check(repo.checkup.run(nb.id), "H5").count == 0      # 边界事件,立即可见


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
    h2 = _check(repo.checkup.run(nb.id), "H2")
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


def test_probe_detects_ann_entry_count_below_labels(tmp_path, monkeypatch):
    """codex 第5轮 P2:结构合法但**条目数 < labels** 的 ann.bin(如从早期/半截 build 拷来)load 也
    成功,但检索会静默漏掉没进 ANN 的 labeled 节点 → 应判损坏(返 1)。**变异锚点**:删掉
    ``get_current_count() != len(labels)`` 比较 → 本用例红(条目少也报健康)。"""
    import numpy as np
    import hnswlib
    from types import SimpleNamespace
    from app.services.kg import scale_index as scale_index_mod

    scale_dir = tmp_path / "scale"
    scale_dir.mkdir()
    (scale_dir / "manifest.json").write_text("{}", encoding="utf-8")
    ann_path = str(scale_dir / "ann.bin")
    dim, n = 4, 2
    h = hnswlib.Index(space="cosine", dim=dim)
    h.init_index(max_elements=n, ef_construction=16, M=8)
    h.add_items(np.eye(n, dim, dtype="float32"), list(range(n)))
    h.save_index(ann_path)  # ann.bin 只有 2 个条目

    # labels 声称 3 个(比 ann.bin 多)→ 检索漏第 3 个 → 损坏。
    stub = SimpleNamespace(
        ann_labels=["a", "b", "c"], ann_path=ann_path, manifest={"dim": dim}
    )
    monkeypatch.setattr(scale_index_mod, "load_scale_index", lambda d: stub)
    assert probe_scale_index_integrity(str(scale_dir)) == 1


# --------------------------------------------- backfill 分页(审计批4)
def test_backfill_page_rows_is_a_multiple_of_embed_batch_size(repo):
    """页大小必须是 ``embed_batch_size`` 的整数倍(至少一整批),**对任意配置成立**。

    这是「embedder **调用次数**跟不分页时逐位相同」的机制性前提:embed_*_batch 把收到的
    行按 embed_batch_size 切成一次次调用,页大小不是整数倍的话,每页末尾都会多出一个残批,
    调用次数就会变(每次调用装哪几行则本就可能不同,见 ``_backfill_page_rows`` docstring)。

    ⚠ 必须扫**不整除**的 batch size:仓库默认 (500, 10) 恰好整除,只测默认值的话
    ``return _BACKFILL_PAGE_ROWS`` 这个变异会静默通过。

    变异锚点:``_backfill_page_rows`` 改成 ``return _BACKFILL_PAGE_ROWS`` → 32/64/700 三档红。"""
    from app.api import source_routes

    class _FakeSettings:
        def __init__(self, size):
            self.embed_batch_size = size

    class _FakeRuntime:
        def __init__(self, size):
            self.settings = _FakeSettings(size)

    class _FakeRepo:
        def __init__(self, size):
            self._runtime = _FakeRuntime(size)

    for size in (1, 7, 10, 32, 64, 300, 700):
        page = source_routes._backfill_page_rows(_FakeRepo(size))
        assert page % size == 0, f"page {page} not a multiple of batch size {size}"
        assert page >= size                      # 至少一整批,绝不退化成 0
    # 真 repo 的实际配置同样成立(默认 500/10)。
    real_size = repo._runtime.settings.embed_batch_size
    assert source_routes._backfill_page_rows(repo) % real_size == 0


def test_backfill_job_discovers_once_then_hydrates_by_page(repo, monkeypatch):
    """审计批4评审修订:锁内是**一次发现 + 按页主键 hydrate**,不是每页重跑发现查询。

    为什么不能每页重发现:本仓库从不对生产库跑 ``ANALYZE``,``missing_*_embedding_page``
    的 ``id > ?`` 在无统计信息时被 planner 选成整表主键区间扫、``source_id`` 降级成残余
    过滤(EXPLAIN 实测),21k 行的源 = 43 页 × 43 次全扫。

    钉五件事:①**每个源恰好一次**发现查询(单次扫描守卫);②生产路径**零次**调用无界
    rows 版与 keyset page 版;③正文按页取、每页不超过页大小(驻留有界);④被处理的
    element 集合与 rows 版逐一致(判据没变);⑤每页一次 embed 调用、页大小即批大小
    (embedder 调用切分不变)。

    变异锚点:把发现改回每页一次 ``missing_element_embedding_page`` → ①②同时红。"""
    from app.api import source_routes

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=25)
    mnt = repo.maintenance
    # 参考集合:无界 rows 版判定的待补行(改动前生产路径读的就是它)。
    reference = {r["id"] for r in mnt.missing_element_embedding_rows(nb.id, only_source_id=sid)}
    assert len(reference) == 25

    # 只配 element 侧,chunk 侧整段短路(不发现、不 hydrate)。
    monkeypatch.setattr(repo, "configured", lambda wid: wid == "source_element_embedding")
    monkeypatch.setattr(source_routes, "_BACKFILL_PAGE_ROWS", 10)   # → 页 = 10 行(1 整批)

    discoveries: list = []
    hydrations: list = []
    real_ids = mnt.missing_element_embedding_ids
    real_hydrate = mnt.element_texts_by_ids

    def spy_ids(notebook_id, *, only_source_id=None):
        ids = real_ids(notebook_id, only_source_id=only_source_id)
        discoveries.append((notebook_id, only_source_id, list(ids)))
        return ids

    def spy_hydrate(ids):
        rows = real_hydrate(ids)
        hydrations.append(list(ids))
        return rows

    monkeypatch.setattr(mnt, "missing_element_embedding_ids", spy_ids)
    monkeypatch.setattr(mnt, "element_texts_by_ids", spy_hydrate)
    for banned in ("missing_element_embedding_rows", "missing_element_embedding_page"):
        monkeypatch.setattr(
            mnt, banned,
            lambda *a, _n=banned, **k: (_ for _ in ()).throw(
                AssertionError(f"生产路径不得再调 {_n}")
            ),
        )
    embedded: list = []
    monkeypatch.setattr(
        mnt, "embed_elements_batch",
        lambda notebook_id, items: embedded.append([it["element_id"] for it in items])
        or len(items),
    )
    monkeypatch.setattr(mnt, "embed_chunks_batch", lambda notebook_id, items: None)

    source_routes._backfill_vectors_job(repo, nb.id)

    # ① 单次扫描:该源只发现了一次,且限定在本源上。
    assert len(discoveries) == 1
    assert discoveries[0][1] == sid
    assert set(discoveries[0][2]) == reference
    assert discoveries[0][2] == sorted(discoveries[0][2])       # 发现序 = id 升序
    # ③ 正文按页取:25 行 / 页 10 → 3 次 hydrate,每次 ≤ 页大小,合起来不重不漏。
    assert [len(page) for page in hydrations] == [10, 10, 5]
    assert [i for page in hydrations for i in page] == discoveries[0][2]
    # ④⑤ 处理到的行与 rows 版逐一致;每页一次 embed 调用,页大小即批大小。
    assert {eid for page in embedded for eid in page} == reference
    assert [len(page) for page in embedded] == [10, 10, 5]


def test_backfill_job_discovers_chunks_once_too(repo, monkeypatch):
    """chunk 侧同款(与 element 侧对称):一次发现 + 按页 hydrate,不碰 rows/page 版。

    这里刻意让行数**恰好是页大小的整数倍**(20 行 / 页 10):切页算术的两侧边界(整除 /
    有余数)由两条一起钉住,off-by-one 会让其中一条漏行或多发一次空 hydrate。"""
    from app.api import source_routes

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_source(repo, nb.id, parse_status="extracted", n_elements=0)
    with repo._write() as db:
        for i in range(20):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) VALUES (?,?,?,?,?)",
                (f"ch-{sid}-{i:04d}", nb.id, sid, f"chunk {i}", _NOW),
            )
    mnt = repo.maintenance
    reference = {r["id"] for r in mnt.missing_chunk_embedding_rows(nb.id, only_source_id=sid)}
    assert len(reference) == 20

    monkeypatch.setattr(repo, "configured", lambda wid: wid == "chunk_embedding")
    monkeypatch.setattr(source_routes, "_BACKFILL_PAGE_ROWS", 10)
    discoveries: list = []
    real_ids = mnt.missing_chunk_embedding_ids

    def spy_ids(notebook_id, *, only_source_id=None):
        ids = real_ids(notebook_id, only_source_id=only_source_id)
        discoveries.append(only_source_id)
        return ids

    monkeypatch.setattr(mnt, "missing_chunk_embedding_ids", spy_ids)
    for banned in ("missing_chunk_embedding_rows", "missing_chunk_embedding_page"):
        monkeypatch.setattr(
            mnt, banned,
            lambda *a, _n=banned, **k: (_ for _ in ()).throw(
                AssertionError(f"生产路径不得再调 {_n}")
            ),
        )
    embedded: list = []
    monkeypatch.setattr(
        mnt, "embed_chunks_batch",
        lambda notebook_id, items: embedded.append([it["_oid"] for it in items]),
    )

    source_routes._backfill_vectors_job(repo, nb.id)

    assert discoveries == [sid]                          # 一次发现,不是每页一次
    assert [len(page) for page in embedded] == [10, 10]   # 整除:两满页,没有多余的空页
    assert {cid for page in embedded for cid in page} == reference


def test_backfill_job_discovers_once_per_source(repo, monkeypatch):
    """多个源时,发现查询是**每源一次**(而不是每页一次):单次扫描守卫的规模侧。"""
    from app.api import source_routes

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sids = [
        _seed_source(repo, nb.id, parse_status="extracted", n_elements=25)
        for _ in range(3)
    ]
    mnt = repo.maintenance
    monkeypatch.setattr(repo, "configured", lambda wid: wid == "source_element_embedding")
    monkeypatch.setattr(source_routes, "_BACKFILL_PAGE_ROWS", 10)
    seen: list = []
    real_ids = mnt.missing_element_embedding_ids

    def spy_ids(notebook_id, *, only_source_id=None):
        seen.append(only_source_id)
        return real_ids(notebook_id, only_source_id=only_source_id)

    monkeypatch.setattr(mnt, "missing_element_embedding_ids", spy_ids)
    monkeypatch.setattr(mnt, "embed_elements_batch", lambda notebook_id, items: len(items))
    monkeypatch.setattr(mnt, "embed_chunks_batch", lambda notebook_id, items: None)

    source_routes._backfill_vectors_job(repo, nb.id)

    assert sorted(seen) == sorted(sids)      # 每个源恰好一次,不多不少
    assert len(seen) == len(set(seen))


def test_backfill_hydration_is_re_sorted_into_discovery_order(repo):
    """``id IN (...)`` 不保证行序,而发现查询是 ``ORDER BY id``——hydrate 回来必须按 id
    重排,否则「哪几行进同一次 embedder 调用」会随后端/执行计划漂。

    变异锚点:把 ``_hydrate_in_id_order`` 改成 ``return rows`` → 本条红。"""
    from app.api import source_routes

    shuffled = [
        {"id": "el-3", "source_id": "s", "text": "c"},
        {"id": "el-1", "source_id": "s", "text": "a"},
        {"id": "el-2", "source_id": "s", "text": "b"},
    ]
    assert [r["id"] for r in source_routes._hydrate_in_id_order(shuffled)] == [
        "el-1", "el-2", "el-3",
    ]
    assert source_routes._hydrate_in_id_order([]) == []


def test_missing_embedding_ids_match_the_unbounded_rows_predicate(repo):
    """单次发现版的判据与无界 rows 版**逐字一致**——只是把投影换成 id 并加了 ORDER BY。
    (隐藏合成源 memory/knowhow 的排除、TRIM 非空、NOT EXISTS 三条都在两边同款。)

    变异锚点:发现 SQL 里去掉 ``source_type NOT IN ('memory','knowhow')`` 或 TRIM 非空 →
    本条红。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, source_type="document", parse_status="extracted", n_elements=7)
    b = _seed_source(repo, nb.id, source_type="document", parse_status="extracted", n_elements=3)
    _seed_source(repo, nb.id, source_type="knowhow", parse_status="extracted", n_elements=4)
    _seed_source(repo, nb.id, source_type="memory", parse_status="extracted", n_elements=5)
    with repo._write() as db:      # 纯空白文本的 element:两侧都必须排除
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"el-{a}-blank", a, "paragraph", "p", "   \n\t ", "{}", _NOW),
        )
        for i in range(4):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) VALUES (?,?,?,?,?)",
                (f"ch-{b}-{i:04d}", nb.id, b, f"chunk {i}", _NOW),
            )
    mnt = repo.maintenance

    for only in (a, b, None):
        elem_ids = mnt.missing_element_embedding_ids(nb.id, only_source_id=only)
        assert set(elem_ids) == {
            r["id"] for r in mnt.missing_element_embedding_rows(nb.id, only_source_id=only)
        }
        assert elem_ids == sorted(elem_ids)          # 升序,页边界确定
        chunk_ids = mnt.missing_chunk_embedding_ids(nb.id, only_source_id=only)
        assert set(chunk_ids) == {
            r["id"] for r in mnt.missing_chunk_embedding_rows(nb.id, only_source_id=only)
        }
        assert chunk_ids == sorted(chunk_ids)


def test_texts_by_ids_hydrate_only_what_was_asked_for(repo):
    """主键 hydrate:只取给定 id 的正文,空输入不发查询;缺 id(并发删除)静默少一行。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, parse_status="extracted", n_elements=3)
    with repo._write() as db:
        for i in range(2):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) VALUES (?,?,?,?,?)",
                (f"ch-{a}-{i:04d}", nb.id, a, f"chunk {i}", _NOW),
            )
    mnt = repo.maintenance
    assert mnt.element_texts_by_ids([]) == []
    assert mnt.chunk_texts_by_ids([]) == []
    elem_ids = mnt.missing_element_embedding_ids(nb.id, only_source_id=a)
    rows = mnt.element_texts_by_ids(elem_ids[:2] + ["el-does-not-exist"])
    assert {r["id"] for r in rows} == set(elem_ids[:2])
    assert all(r["source_id"] == a and r["text"] for r in rows)
    chunk_rows = mnt.chunk_texts_by_ids([f"ch-{a}-0000"])
    assert [r["id"] for r in chunk_rows] == [f"ch-{a}-0000"]
    assert chunk_rows[0]["text"] == "chunk 0"


def test_texts_by_ids_batches_large_id_lists(repo):
    """按 id hydrate 的参数分批(SQLite 绑定变量上限):id 数超过一批仍取回完整结果。"""
    from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = _seed_source(repo, nb.id, parse_status="extracted", n_elements=0)
    n = SQLiteMaintenanceAdapter._EMBEDDING_ID_IN_CHUNK + 5
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{a}-{i:05d}", a, "paragraph", "p", f"text {i}", "{}", _NOW),
            )
    mnt = repo.maintenance
    ids = mnt.missing_element_embedding_ids(nb.id, only_source_id=a)
    assert len(ids) == n
    assert {r["id"] for r in mnt.element_texts_by_ids(ids)} == set(ids)
