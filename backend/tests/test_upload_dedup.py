"""UI 上传的同 notebook 去重——对齐 batch_ingest 既有行为。

跨 notebook 刻意不去重：用户通常确实想在自己库里拥有这份文件，且跨用户共享
source 行会引爆权限、删除级联与归属问题。

复用 ≠ 撒手不管：file_hash 是在解析之前写进行里的，它只说明「这份内容进过库」。
解析失败（本项目里 MinerU/网络抖动是常态）后指纹照样在，所以短路前必须看那条既有
源是不是真的摄取成功了，否则用户最自然的重试动作（把同一个文件再传一次）会静默
变成 no-op 还弹「已上传」。
"""
import types
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return Settings()


@pytest.fixture
def live_repo(tmp_path, monkeypatch):
    """真流水线（process_source 不打桩）：只把文件解析器换成可控的桩，
    这样「解析失败 → 重新上传」走的是生产里那条 except 分支。"""
    return SQLiteRepository(_settings(tmp_path, monkeypatch))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = SQLiteRepository(_settings(tmp_path, monkeypatch))
    # 不传 scheduler 时 upload_sources 会同步跑完整 parse→extract 流水线；
    # 这些用例只关心去重短路，把它打桩成「只记录调用」。
    r.process_calls = []
    monkeypatch.setattr(
        r._runtime.source_ingestion,
        "process_source",
        lambda sid, hooks: r.process_calls.append(sid),
    )
    return r


@pytest.fixture
def notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def other_notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb2")).id


def _upload(repo, notebook_id, content=b"hello world", name="a.txt", doc_type=""):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(
            file_name=name, content_type="text/plain", content=content,
            doc_type=doc_type)],
    )


def _element(text):
    return types.SimpleNamespace(
        element_type="paragraph", location_label="p1", text=text, metadata={}
    )


def _patch_parse(monkeypatch, result):
    """把文件解析器换掉：result 是异常类型则抛，否则当作 elements 返回。"""
    import app.services.sqlite_repository as facade_mod

    def fake(*_args, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(facade_mod, "parse_source_file", fake)


def _seed_source(repo, notebook_id, *, file_hash, created_at, status="extracted"):
    """直接插一行 source（模拟历史库里的既有行）。"""
    sid = f"src-{uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook_id, sid, "markdown", status, status,
             "doc.md", "/tmp/s.md", 0, file_hash, "", "", created_at, created_at),
        )
    return sid


def test_same_content_twice_in_one_notebook_creates_one_source(repo, notebook_id):
    first = _upload(repo, notebook_id)
    second = _upload(repo, notebook_id)
    assert first[0].id == second[0].id, "同 notebook 内相同内容应复用既有源"
    assert len(repo.list_sources(notebook_id)) == 1


def test_different_content_still_creates_a_new_source(repo, notebook_id):
    _upload(repo, notebook_id, content=b"one")
    _upload(repo, notebook_id, content=b"two")
    assert len(repo.list_sources(notebook_id)) == 2


def test_same_content_in_another_notebook_is_not_deduped(
    repo, notebook_id, other_notebook_id
):
    a = _upload(repo, notebook_id)
    b = _upload(repo, other_notebook_id)
    assert a[0].id != b[0].id, "跨 notebook 刻意不去重"


# --------------------------------------------------------------- 失败源必须能重试

def test_reupload_of_a_failed_source_really_retries(live_repo, monkeypatch):
    """解析失败的源，重新上传同一个文件必须真的重跑流水线，而不是静默返回坏源。"""
    nb = live_repo.create_notebook(NotebookCreate(name="nb")).id

    _patch_parse(monkeypatch, RuntimeError("mineru down"))
    first = _upload(live_repo, nb)
    sid = first[0].id
    assert live_repo.get_source(sid).parse_status == "failed", "前提：这条源确实失败了"
    assert live_repo.get_source(sid).file_hash != "", "指纹保留（否则 CLI 侧会重新建行）"

    _patch_parse(monkeypatch, [_element("recovered body")])
    second = _upload(live_repo, nb)

    assert second[0].id == sid, "复用同一行，不新建第二条"
    assert len(live_repo.list_sources(nb)) == 1
    assert live_repo.get_source(sid).parse_status == "extracted", (
        "重新上传必须真的重跑解析；停在 failed 说明又被指纹短路成了 no-op"
    )
    assert second[0].reused is True


def test_reupload_of_a_failed_source_reschedules_when_scheduler_is_given(
    repo, notebook_id
):
    """有 scheduler（真实 API 路径）时，重试同样要排进后台队列，而不是同步跑。"""
    scheduled = []
    first = repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world")],
        scheduler=scheduled.append,
    )
    sid = first[0].id
    repo._runtime.source_ingestion.set_source_status(
        sid, "failed", error_message="mineru down"
    )
    scheduled.clear()

    again = repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world")],
        scheduler=scheduled.append,
    )
    assert scheduled == [sid], "失败源重新上传要重新排队"
    assert again[0].id == sid
    assert repo.process_calls == [], "有 scheduler 时绝不同步跑"


def test_reupload_of_a_healthy_source_does_not_reprocess(repo, notebook_id):
    """成功摄取过的源原样复用：重复跑一遍解析/抽取是白烧的算力。"""
    first = _upload(repo, notebook_id)
    repo._runtime.source_ingestion.set_source_status(first[0].id, "extracted")
    repo.process_calls.clear()

    second = _upload(repo, notebook_id)
    assert second[0].id == first[0].id
    assert repo.process_calls == [], "已成功的源不该被重跑"


def test_reupload_of_an_in_flight_source_does_not_start_a_second_pipeline(
    repo, notebook_id
):
    """还在排队/解析中的源不重入：同一行并发两条流水线 = 重复的解析与模型开销。"""
    first = _upload(repo, notebook_id)          # 打桩的 process_source 让它停在 queued
    assert repo.get_source(first[0].id).parse_status == "queued"
    repo.process_calls.clear()

    _upload(repo, notebook_id)
    assert repo.process_calls == []


def test_restart_fails_out_abandoned_queued_sources_so_reupload_can_retry(
    tmp_path, monkeypatch
):
    """崩溃遗留在 queued/parsing 的源在下次启动时判死，从而能被重新上传救回。

    这两个状态只由上传/加链接两个入口写下，且都在同一进程里紧接着排队或直接跑
    process_source，所以启动时还停着的行定义上就是被遗弃的。
    """
    settings = _settings(tmp_path, monkeypatch)
    first_boot = SQLiteRepository(settings)
    nb = first_boot.create_notebook(NotebookCreate(name="nb")).id
    queued = first_boot.upload_sources(
        nb,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world")],
        scheduler=lambda _sid: None,   # 排队后进程就"崩"了，没人跑
    )[0].id
    assert first_boot.get_source(queued).parse_status == "queued"

    second_boot = SQLiteRepository(settings)   # 重启：initialize() 跑崩溃兜底
    assert second_boot.get_source(queued).parse_status == "failed"

    scheduled = []
    again = second_boot.upload_sources(
        nb,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world")],
        scheduler=scheduled.append,
    )
    assert again[0].id == queued and scheduled == [queued], "重启后重新上传应能重试"


# ------------------------------------------------- 复用不得吞掉用户新选的文档类型

@pytest.fixture
def settled(repo, notebook_id, monkeypatch):
    """工厂：造一条「已摄取完成」的源（可带初始 doc_type）。

    同时把 notebook 布置成「已有 KG」——摄取期是否抽 KG 由 should_extract_kg
    判定（全局开关默认关，或该 notebook 已有 KG），不布置的话重抽根本不会触发，
    测试就成了空转。真正的抽取打桩成记账，不去点 LLM。"""
    ingestion = repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "notebook_has_kg", lambda _nb: True)
    repo.extract_calls = []
    monkeypatch.setattr(
        ingestion,
        "run_extraction",
        lambda source_id, **_kw: repo.extract_calls.append(source_id),
    )

    def make(doc_type=""):
        sid = _upload(repo, notebook_id, doc_type=doc_type)[0].id
        ingestion.set_source_status(sid, "extracted")
        repo.process_calls.clear()
        repo.extract_calls.clear()
        return sid

    return make


def test_reupload_with_a_corrected_doc_type_retypes_and_reextracts(
    repo, notebook_id, settled
):
    """「类型判错了，我改成教材再传一遍」是最自然的纠正动作。内容判重不代表
    类型选择也该丢：doc_type 决定抽取 profile 并进抽取 prompt（因而进 LLM 缓存
    键），静默丢掉 = 这条源永远按错的类型入图。"""
    sid = settled()
    assert repo.get_source(sid).doc_type == ""

    second = _upload(repo, notebook_id, doc_type="textbook")

    assert second[0].id == sid and second[0].reused is True, "仍然复用同一行"
    assert repo.get_source(sid).doc_type == "textbook", (
        "新选的文档类型必须落库；停在 '' 说明去重把它静默吞了"
    )
    assert second[0].doc_type == "textbook", "返回值要如实带上现在生效的类型"
    assert repo.extract_calls == [sid], "类型变了必须按新类型重抽 KG"
    assert repo.process_calls == [], "内容一模一样，绝不重新解析（白烧且可能打死好源）"
    assert repo.get_source(sid).parse_status == "extracted", "重抽跑完回到终态"


def test_reupload_without_a_doc_type_never_clobbers_the_stored_one(
    repo, notebook_id, settled
):
    """前端在用户没选类型时就是不传——那是「没意见」，不是「改成自动检测」。"""
    sid = settled(doc_type="textbook")

    second = _upload(repo, notebook_id)

    assert repo.get_source(sid).doc_type == "textbook", "空值不得覆盖已存的非空类型"
    assert second[0].doc_type == "textbook"
    assert repo.extract_calls == [], "没改动就没有重抽"


def test_reupload_with_the_same_doc_type_does_not_reextract(
    repo, notebook_id, settled
):
    """类型没变就是纯复用：再抽一遍是白烧的模型开销。"""
    sid = settled(doc_type="textbook")

    _upload(repo, notebook_id, doc_type="textbook")

    assert repo.get_source(sid).doc_type == "textbook"
    assert repo.extract_calls == []
    assert repo.process_calls == []


def test_unknown_doc_type_counts_as_no_opinion_not_as_a_change(
    repo, notebook_id, settled
):
    """比较在两侧归一化之后做：'auto'/未知值归一成 ''，等同于没意见。"""
    sid = settled(doc_type="textbook")

    _upload(repo, notebook_id, doc_type="auto")

    assert repo.get_source(sid).doc_type == "textbook"
    assert repo.extract_calls == []


def test_retyping_an_in_flight_source_updates_the_type_but_starts_no_pipeline(
    repo, notebook_id
):
    """还在排队/解析中的行：类型照记，但绝不重入——同一行两条流水线是重复开销。"""
    sid = _upload(repo, notebook_id)[0].id
    assert repo.get_source(sid).parse_status == "queued"
    repo.process_calls.clear()

    _upload(repo, notebook_id, doc_type="textbook")

    assert repo.get_source(sid).doc_type == "textbook"
    assert repo.process_calls == []


def test_retype_with_a_scheduler_reextracts_out_of_band(
    repo, notebook_id, settled, monkeypatch
):
    """真实 API 路径（有 scheduler）：重抽必须离开请求线程，且响应要立刻如实
    显示「在重抽」，否则前端不会开始轮询，改类型在界面上又成了一次静默 no-op。"""
    from app.services.kg import scheduler as kg_scheduler

    sid = settled()
    submitted = []
    monkeypatch.setattr(
        kg_scheduler, "submit_job", lambda fn, *args, **_kw: submitted.append(args)
    )

    row = repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world", doc_type="textbook")],
        scheduler=lambda _sid: None,
    )[0]

    assert repo.get_source(sid).doc_type == "textbook"
    assert row.parse_status == "extracting"
    assert [args[0] for args in submitted] == [sid], "重抽要进 KG job 池"
    assert repo.extract_calls == [], "绝不在请求线程里同步抽"


# ------------------------------------------------------- 新建/复用要能被调用方区分

def test_upload_result_marks_new_rows_and_reused_rows(repo, notebook_id):
    first = _upload(repo, notebook_id)
    assert [s.reused for s in first] == [False]

    # 同一份内容换个文件名再传：仍然沿用原条目，且保留原来的标题
    second = _upload(repo, notebook_id, name="report-FINAL.txt")
    assert [s.reused for s in second] == [True]
    assert second[0].title == "a.txt", "内容寻址复用：新文件名不会改写既有源的标题"
    assert len(repo.list_sources(notebook_id)) == 1


def test_upload_result_keeps_every_field_the_old_contract_returned(repo, notebook_id):
    """reused 是纯增字段：老调用方读到的 SourceSummary 字段一个不少。"""
    from app.models.sources import SourceSummary

    row = _upload(repo, notebook_id)[0]
    assert isinstance(row, SourceSummary)
    for field in SourceSummary.model_fields:
        assert hasattr(row, field), field


# ------------------------------------------------------------ 指纹查询自身的边界

def test_source_id_by_hash_never_matches_the_empty_hash(repo, notebook_id):
    """历史上 file_hash='' 的行（URL 来源、只导元数据的源）不得被当成「已存在」。"""
    store = repo._runtime.source_store
    empty = _seed_source(repo, notebook_id, file_hash="", created_at="2020-01-01T00:00:00")
    assert store.source_id_by_hash(notebook_id, "") is None
    assert store.source_id_by_hash(notebook_id, "a" * 64) is None
    assert empty in {s.id for s in repo.list_sources(notebook_id)}

    # 上传路径同样不会掉进那条空指纹的行里：它要新建自己的源。
    uploaded = _upload(repo, notebook_id)
    assert uploaded[0].id != empty
    assert uploaded[0].reused is False


def test_source_id_by_hash_finds_and_scopes_by_notebook(
    repo, notebook_id, other_notebook_id
):
    store = repo._runtime.source_store
    digest = "b" * 64
    sid = _seed_source(repo, notebook_id, file_hash=digest, created_at="2021-01-01T00:00:00")
    assert store.source_id_by_hash(notebook_id, digest) == sid
    assert store.source_id_by_hash(other_notebook_id, digest) is None
    assert store.source_id_by_hash(notebook_id, "c" * 64) is None


def test_source_id_by_hash_is_deterministic_when_duplicates_already_exist(
    repo, notebook_id
):
    """已部署库里本来就可能有同 hash 的重复行（正是本特性要治的那批）：
    必须稳定返回最早的那条，而不是"看 SQLite 心情"。

    先插入 created_at 更晚的那行，让插入顺序与时间顺序相反——没有 ORDER BY 时
    索引扫出的第一条就是它。"""
    store = repo._runtime.source_store
    digest = "d" * 64
    newer = _seed_source(repo, notebook_id, file_hash=digest, created_at="2024-05-05T00:00:00")
    older = _seed_source(repo, notebook_id, file_hash=digest, created_at="2022-02-02T00:00:00")
    assert store.source_id_by_hash(notebook_id, digest) == older, (
        f"应返回最早的 {older}，实际 {newer} 说明查询顺序未定义"
    )
    assert store.source_id_by_hash(notebook_id, digest) == older, "重复调用必须一致"
