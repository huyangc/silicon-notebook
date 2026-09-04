"""批 3·W2 PR-3:链 b 补漏轮 + §2.1 进程内交叉检查 + 融合失败结构化事件。

链 b 的洞:抽取主循环的 keyset 按 (created_at, id) 前进,一个 created_at
早于游标、元素在循环中途才落齐的来源会被整轮错过(新上传天然会被下一次
「分析新增」扫到,不构成这个洞——所以注入形态必须是**回填时间戳**的源,
v1 的注入形态杀不死变异,设计 §5.3 明文作废)。
"""
from __future__ import annotations

import inspect
import json
import re

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
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

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

    # 主循环 1 次 + 补漏 3 轮;身份过滤保证每轮只抽新落齐的源,已试过的
    # (失败/被跳过)不被重付模型钱。
    assert calls == [["src-0"], ["src-1"], ["src-2"], ["src-3"]]
    exhausted = [e for e in events if e.get("kind") == "kg_backfill_exhausted"]
    assert exhausted and exhausted[0]["rounds"] == 3
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["error_code"] == "kg_backfill_partial"


def test_backfill_shares_the_loop_predicate_function():
    """结构守卫(复评 P1-5):谓词已两次被复述写错(漏 is_partial/
    analyzed_empty)。钉「主循环与补漏轮共用 _kg_target_batches」——运行函数
    里恰好两处调用、零处内联谓词(kg_analyzed_without_objects 只许出现在
    谓词函数自己里)。"""
    run_src = inspect.getsource(KnowledgeLifecycleService._run_notebook_kg_job)
    assert run_src.count("self._kg_target_batches(") == 3, (
        "主循环与补漏轮之外多/少了目标枚举站点——谓词有被复述的风险")
    assert "kg_analyzed_without_objects" not in run_src
    assert "has_kg" not in run_src
    predicate_src = inspect.getsource(KnowledgeLifecycleService._kg_target_batches)
    assert "kg_analyzed_without_objects" in predicate_src


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


def test_fuse_swallow_sites_emit_the_structured_event():
    """融合 except 结构化事件(PR-3 第三件):两处吞融合异常的站点必须在
    except 里 emit incremental_fuse_failed——只进日志的失败在事件流里
    隐形。文本级形状守卫(行为是一行 emit,站点已逐个手验)。"""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    for rel in ("app/services/source_ingestion.py",
                "app/services/scale_index_builder.py"):
        text = (backend / rel).read_text(encoding="utf-8")
        for match in re.finditer(r"incremental_fuse_source\(", text):
            window = text[match.start():match.start() + 1200]
            if "except Exception" not in window:
                continue   # 定义处/注释引用,不是吞异常站点
            assert '"incremental_fuse_failed"' in window, (
                f"{rel}: 吞融合异常的站点缺结构化事件")
