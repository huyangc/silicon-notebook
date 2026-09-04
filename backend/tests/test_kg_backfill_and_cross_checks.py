"""批 3·W2 PR-3:链 b 补漏轮 + §2.1 进程内交叉检查 + 融合失败结构化事件。

链 b 的洞:抽取主循环的 keyset 按 (created_at, id) 前进,一个 created_at
早于游标、元素在循环中途才落齐的来源会被整轮错过(新上传天然会被下一次
「分析新增」扫到,不构成这个洞——所以注入形态必须是**回填时间戳**的源,
v1 的注入形态杀不死变异,设计 §5.3 明文作废)。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import KgMaintenanceAlreadyRunning
from app.services.embedding import FakeEmbedder
from app.services.kg import scheduler as kg_scheduler
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


class _AlwaysExtractClient:
    configured = True
    model = "test-kg"

    def chat_json(self, messages, response_schema_hint, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith('Return {"ok":true}'):
            return '{"ok":true}'
        return json.dumps({
            "nodes": [{"local_id": "engram", "type": "Concept",
                       "name": "Engram", "ev": 0}],
            "edges": [],
        })


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'kg.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings(_env_file=None)
    settings.kg_llm_max_retries = 0
    settings.paper_meta_enabled = False
    settings.kg_refine_enabled = False
    settings.kg_gleaning_enabled = False
    settings.kg_conflict_resolution_enabled = False
    settings.kg_relink_enabled = False
    settings.kg_incremental_fusion_enabled = False
    result = SQLiteRepository(settings)
    bind_all_embedding_clients(result, FakeEmbedder(dim=settings.embed_dim))
    kg_scheduler.configure(window_workers=1, job_workers=1)
    try:
        yield result
    finally:
        kg_scheduler.reset()


def _seed_source(repo, notebook_id, source_id, created_at):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status,"
            " parse_status, file_name, file_path, file_size, file_hash,"
            " summary, doc_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'markdown', 'parsed', 'parsed', ?, '', 0, ?, '',"
            " 'academic_paper', ?, ?)",
            (source_id, notebook_id, source_id, f"{source_id}.md",
             f"hash-{source_id}", created_at, created_at),
        )
        db.execute(
            "INSERT INTO source_elements (id, source_id, element_type,"
            " location_label, text, metadata, created_at) "
            "VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)",
            (f"el-{source_id}", source_id,
             f"Engram memory architecture in {source_id}.", created_at),
        )


def _kg_source_ids(repo, notebook_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT source_id FROM knowledge_objects "
            "WHERE notebook_id=? AND source_id!=''", (notebook_id,),
        ).fetchall()
    return {row["source_id"] for row in rows}


def test_backfill_round_catches_a_source_behind_the_cursor(repo, monkeypatch):
    """链 b pin(设计 §5.3):主循环第一批抽取期间,一个 created_at 早于
    既有来源的源才落齐元素——主 keyset 已越过它,唯一入口是补漏轮。删掉
    补漏轮,这个源的 KG 断言当场红。"""
    notebook = repo.create_notebook(NotebookCreate(name="chain-b"))
    _seed_source(repo, notebook.id, "src-main", "2026-07-20T10:00:00")
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())
    service = repo._runtime.knowledge_lifecycle
    original_extract = service._extract_targets
    injected = []

    def inject_then_extract(notebook_id, targets, *args, **kwargs):
        if not injected:
            injected.append(True)
            # 回填时间戳的源:created_at 早于 src-main(游标已在它之后)。
            _seed_source(repo, notebook_id, "src-late", "2026-07-20T09:00:00")
        return original_extract(notebook_id, targets, *args, **kwargs)

    monkeypatch.setattr(service, "_extract_targets", inject_then_extract)
    # 生产形状(评审 P1):「分析新增」按钮与 MCP build_kg 恒传
    # retry_partial=True——pin 必须钉这条真实路径,省掉它的绿灯与生产无关。
    job = repo.prepare_notebook_kg_job(
        notebook.id, "incremental", retry_partial=True)
    repo.execute_notebook_kg_job(
        notebook.id, job["id"], "incremental", retry_partial=True)

    assert _kg_source_ids(repo, notebook.id) == {"src-main", "src-late"}, (
        "补漏轮没接住游标身后落齐的源——链 b 重新开洞")
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["error_code"] == ""


def test_backfill_exhaustion_reports_partial_honestly(repo, monkeypatch):
    """补漏轮有界:3 轮耗尽仍有余量 → 结构化事件 + job 记 partial
    (succeeded + error_code),不虚构「总会收敛」。注入方式:抽取被替换成
    不落任何 KG 的 no-op,来源永远符合 incremental 谓词。"""
    notebook = repo.create_notebook(NotebookCreate(name="exhaust"))
    _seed_source(repo, notebook.id, "src-0", "2026-07-20T10:00:00")
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())
    service = repo._runtime.knowledge_lifecycle
    calls = []

    def spawning_noop_extract(notebook_id, targets, *args, **kwargs):
        calls.append([source_id for source_id, _p in targets])
        # 每次抽取期间又有一个回填时间戳的源落齐——持续追不上的形态。
        # no-op 抽取(不落 KG),已试过的源由身份过滤兜住,每轮只看见新源。
        _seed_source(repo, notebook_id, f"src-{len(calls)}",
                     f"2026-07-20T0{len(calls)}:00:00")
        return {key: [] for key in (
            "built", "failed", "skipped", "skipped_no_elements",
            "partial_retried", "partial_failed_preserved", "model_skipped")}

    monkeypatch.setattr(service, "_extract_targets", spawning_noop_extract)
    events = []
    monkeypatch.setattr(repo._runtime.event_log, "emit",
                        lambda event: events.append(event))
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    # 身份过滤的可观察面:四个源各被尝试恰一次(已试过的不重付模型钱),
    # 不钉批次切分形状。
    flattened = [source_id for page in calls for source_id in page]
    assert sorted(flattened) == ["src-0", "src-1", "src-2", "src-3"]
    exhausted = [e for e in events if e.get("kind") == "kg_backfill_exhausted"]
    assert exhausted and exhausted[0]["rounds"] == 3
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["error_code"] == "kg_backfill_partial"


def test_target_limit_run_never_enters_the_backfill(repo, monkeypatch):
    """变异钉(质量评 P2):target_limit 是用户显式限定的范围——删掉
    `target_limit is None` 闸,batch_ingest --limit 会在主循环之后把整库
    剩余来源全抽一遍,越过用户设的模型花费上限。"""
    notebook = repo.create_notebook(NotebookCreate(name="limit"))
    _seed_source(repo, notebook.id, "src-a", "2026-07-20T10:00:00")
    _seed_source(repo, notebook.id, "src-b", "2026-07-20T11:00:00")
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())
    job = repo.prepare_notebook_kg_job(
        notebook.id, "incremental", target_limit=1)
    repo.execute_notebook_kg_job(
        notebook.id, job["id"], "incremental", target_limit=1)
    assert _kg_source_ids(repo, notebook.id) == {"src-a"}, (
        "limit 之外的源被抽了——补漏轮越权")


def test_backfill_batch_honours_the_deleting_checkpoint(repo, monkeypatch):
    """变异钉(质量评 P2):补漏轮的批边界与主循环同一条 deleting 检查点
    ——删掉它,删除笔记本的 quiesce 只能等完整 3 轮补漏抽取超时收场。
    注入:主循环批检查全部放行,补漏轮第一批才置 deleting。"""
    from app.services.kg.run_control import KgBuildAborted

    notebook = repo.create_notebook(NotebookCreate(name="del-checkpoint"))
    _seed_source(repo, notebook.id, "src-main", "2026-07-20T10:00:00")
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())
    service = repo._runtime.knowledge_lifecycle
    original_extract = service._extract_targets
    state = {"main_done": False}

    def inject_then_extract(notebook_id, targets, *args, **kwargs):
        if not state["main_done"]:
            state["main_done"] = True
            # 让补漏轮有活干(否则空轮直接 break,走不到检查点)。
            _seed_source(repo, notebook_id, "src-late", "2026-07-20T09:00:00")
        return original_extract(notebook_id, targets, *args, **kwargs)

    monkeypatch.setattr(service, "_extract_targets", inject_then_extract)
    monkeypatch.setattr(
        service, "_notebook_deleting",
        lambda notebook_id: state["main_done"])
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")
    assert _kg_source_ids(repo, notebook.id) == {"src-main"}, (
        "deleting 置位后补漏轮不得再抽")


def test_build_endpoint_returns_409_when_maintenance_holds_the_slot(repo):
    """端点级 pin(评审 P1):prepare 抛 KgMaintenanceAlreadyRunning 时
    /kg/build 与 /kg/rebuild 必须落 409 + holder 点名文案,不是 500。
    直接调路由函数(免起 app):user_error 抛 HTTPException。"""
    from fastapi import HTTPException

    from app.api import kg_routes

    notebook = repo.create_notebook(NotebookCreate(name="endpoint-409"))
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())
    service = repo._runtime.knowledge_lifecycle
    claimed = service.kg_maintenance.claim(
        notebook.id, "rebuild", "ukj",
        dict(service.kg_maintenance.REBUILD_COUNTERS))
    try:
        import app.api.deps as deps
        original = deps.repository
        deps.repository = lambda: repo
        kg_routes.repository = lambda: repo
        try:
            with pytest.raises(HTTPException) as exc:
                kg_routes.build_kg(notebook.id)
            assert exc.value.status_code == 409
            assert exc.value.detail == "当前笔记本正在重新合并，请等它完成"
            with pytest.raises(HTTPException) as exc:
                kg_routes.rebuild_kg(notebook.id)
            assert exc.value.status_code == 409
        finally:
            deps.repository = original
            kg_routes.repository = original
    finally:
        service.kg_maintenance.settle(notebook.id, claimed["job_id"], "succeeded")


def test_backfill_respects_the_shared_eligibility_predicate(repo, monkeypatch):
    """行为版结构守卫(复评 P1-5 + codex #673 R3 P2:不测源码文本)。谓词
    已两次被复述写错——真实 incremental 分支排除 analyzed_empty(零对象
    来源不重付模型钱)。注入两个游标身后的源:正常的必须被补漏抽到,已判
    「分析过且为空」的必须被跳过——谓词若被复述漏掉该排除、或 mode 被改成
    rebuild(全量重抽的反转),这里当场红。"""
    from app.models.sources import KG_EMPTY_RUN_MESSAGE_PREFIX

    notebook = repo.create_notebook(NotebookCreate(name="predicate"))
    _seed_source(repo, notebook.id, "src-main", "2026-07-20T10:00:00")
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())
    service = repo._runtime.knowledge_lifecycle
    original_extract = service._extract_targets
    injected = []

    def inject_then_extract(notebook_id, targets, *args, **kwargs):
        if not injected:
            injected.append(True)
            _seed_source(repo, notebook_id, "src-late", "2026-07-20T09:00:00")
            _seed_source(repo, notebook_id, "src-empty", "2026-07-20T08:00:00")
            with repo._write() as db:
                db.execute(
                    "INSERT INTO extraction_runs "
                    "(id,notebook_id,source_id,run_type,status,error_message,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    ("run-empty", notebook_id, "src-empty", "kg", "completed",
                     f"{KG_EMPTY_RUN_MESSAGE_PREFIX}windows=1/1",
                     "2026-07-20T08:30:00", "2026-07-20T08:30:00"),
                )
        return original_extract(notebook_id, targets, *args, **kwargs)

    monkeypatch.setattr(service, "_extract_targets", inject_then_extract)
    job = repo.prepare_notebook_kg_job(
        notebook.id, "incremental", retry_partial=True)
    repo.execute_notebook_kg_job(
        notebook.id, job["id"], "incremental", retry_partial=True)

    assert _kg_source_ids(repo, notebook.id) == {"src-main", "src-late"}, (
        "analyzed-empty 的源被补漏轮重抽了——谓词被复述/换成了 rebuild 口径")
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "succeeded" and saved["error_code"] == ""


def test_build_in_flight_gates_maintenance_claim(repo):
    """§2.1 交叉:buildkg- 在飞时维护槽 claim 被拒(holder=buildkg),且
    刚登记的槽被撤销——不留悬挂 running 项。"""
    notebook = repo.create_notebook(NotebookCreate(name="cross-a"))
    service = repo._runtime.knowledge_lifecycle
    with service.kg_building_lock:
        service.kg_building.add(notebook.id)
    try:
        with pytest.raises(KgMaintenanceAlreadyRunning) as exc:
            service.kg_maintenance.start_notebook_relink(notebook.id)
        assert exc.value.holder == "buildkg"
        assert service.kg_maintenance.active_kind(notebook.id) is None
    finally:
        with service.kg_building_lock:
            service.kg_building.discard(notebook.id)


def test_build_own_tail_relink_is_exempt_from_the_cross_check(repo):
    """§2.1 的边界:build 作业自己的收尾 relink 在 build 仍持 kg_building
    时顺序调进来——标记是自己的,不该被交叉检查闸死(回归自
    test_kg_relink_repository 的 skips_relink_when_disabled)。外部入口
    (不带豁免)仍被拒。"""
    notebook = repo.create_notebook(NotebookCreate(name="cross-tail"))
    service = repo._runtime.knowledge_lifecycle
    with service.kg_building_lock:
        service.kg_building.add(notebook.id)
    try:
        job = service.kg_maintenance.start_notebook_relink(
            notebook.id, exempt_build_marker=True)
        assert job["kind"] == "relink"
        service.kg_maintenance.settle(notebook.id, job["job_id"], "succeeded")
        with pytest.raises(KgMaintenanceAlreadyRunning):
            service.kg_maintenance.start_notebook_relink(notebook.id)
    finally:
        with service.kg_building_lock:
            service.kg_building.discard(notebook.id)


def test_admission_paths_share_one_arbitration_lock(repo):
    """codex #673 R2 P2 的钉:维护槽 claim 与 build/delete 预占的
    「登记自己 + 查对方」必须持同一把仲裁锁——对象同一性即契约,断了
    (各自建锁/传 None)对开就会退化回双双退让。"""
    service = repo._runtime.knowledge_lifecycle
    assert (service.kg_maintenance._cross_admission_lock
            is service.kg_cross_admission_lock)
    assert service.kg_cross_admission_lock is not None


def test_maintenance_in_flight_gates_build_and_standalone_delete(repo):
    """§2.1 交叉的另一向:维护槽在飞时 prepare_notebook_kg_job 与
    standalone delete_notebook_kg 都按维护种类 409,且 kg_building 预占
    被撤销(不把后续动作一并锁死)。"""
    notebook = repo.create_notebook(NotebookCreate(name="cross-b"))
    service = repo._runtime.knowledge_lifecycle
    claimed = service.kg_maintenance.claim(
        notebook.id, "rebuild", "ukj",
        dict(service.kg_maintenance.REBUILD_COUNTERS))
    try:
        with pytest.raises(KgMaintenanceAlreadyRunning) as exc:
            service.prepare_notebook_kg_job(
                notebook.id, "incremental", allow_without_model=True)
        assert exc.value.holder == "rebuild"
        assert not service._kg_build_active(notebook.id), (
            "被拒后 kg_building 预占必须撤销")
        with pytest.raises(KgMaintenanceAlreadyRunning):
            repo.delete_notebook_kg(notebook.id)
        assert not service._kg_build_active(notebook.id)
    finally:
        service.kg_maintenance.settle(notebook.id, claimed["job_id"], "succeeded")


def test_buildkg_holder_gets_the_build_vocabulary_409():
    """409 文案泛化(§2.1):holder=buildkg 时给出与 build 侧逐字同款的
    文案,而不是落进「整理知识图谱」的兜底。"""
    from app.api.kg_routes import _kg_maintenance_busy

    detail = _kg_maintenance_busy(
        KgMaintenanceAlreadyRunning("nb-x", "buildkg")).detail
    assert detail == "当前笔记本已有知识图谱分析任务正在运行"


def test_fuse_failure_emits_the_structured_event_and_stays_fail_open(
        repo, monkeypatch):
    """融合 except 结构化事件(PR-3 第三件,行为版):真实抽取路径上融合
    抛异常 → 抽取照常成功(fail-open),事件流里有 incremental_fuse_failed
    且只带异常**类名**(AGENTS 遥测红线:不带异常原文)。"""
    notebook = repo.create_notebook(NotebookCreate(name="fuse-event"))
    _seed_source(repo, notebook.id, "src-main", "2026-07-20T10:00:00")
    bind_chat_client(repo, "kg_extract", _AlwaysExtractClient())

    def exploding_fuse(notebook_id, source_id):
        raise RuntimeError("secret /private/path leaked?")

    monkeypatch.setattr(repo._runtime.knowledge_lifecycle,
                        "incremental_fuse_source", exploding_fuse)
    events = []
    monkeypatch.setattr(repo._runtime.event_log, "emit",
                        lambda event: events.append(event))
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    assert _kg_source_ids(repo, notebook.id) == {"src-main"}, "融合失败不得掀翻抽取"
    fused = [e for e in events if e.get("kind") == "incremental_fuse_failed"]
    assert fused and fused[0]["source_id"] == "src-main"
    assert fused[0]["error"] == "RuntimeError", "只许异常类名,不许原文"
