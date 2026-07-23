"""UI 上传的同 notebook 去重——对齐 batch_ingest 既有行为。

跨 notebook 刻意不去重：用户通常确实想在自己库里拥有这份文件，且跨用户共享
source 行会引爆权限、删除级联与归属问题。

复用 ≠ 撒手不管：file_hash 是在解析之前写进行里的，它只说明「这份内容进过库」。
解析失败（本项目里 MinerU/网络抖动是常态）后指纹照样在，所以短路前必须看那条既有
源是不是真的摄取成功了，否则用户最自然的重试动作（把同一个文件再传一次）会静默
变成 no-op 还弹「已上传」。
"""
import threading
import types
from concurrent.futures import ThreadPoolExecutor
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


def _seed_source(
    repo, notebook_id, *, file_hash, created_at, status="extracted",
    source_type="markdown",
):
    """直接插一行 source（模拟历史库里的既有行）。"""
    sid = f"src-{uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook_id, sid, source_type, status, status,
             "doc.md", "/tmp/s.md", 0, file_hash, "", "", created_at, created_at),
        )
    return sid


def _boom(*_args, **_kwargs):
    raise RuntimeError("kg llm returned 502 Bad Gateway")


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


def test_retrying_a_failed_source_leaves_the_failed_terminal_state_at_once(
    repo, notebook_id
):
    """排队前必须先把行翻出 'failed'，否则这次重试对用户完全隐形。

    scheduler 是异步的，它返回时 process_source 未必已经开始；前端的轮询判据是
    「extracted/failed 之外一律继续轮」，所以响应里仍是终态 failed 的行会被判定
    「已结束」，界面停在失败上不动，直到用户自己刷新页面。"""
    scheduled = []
    sid = repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world")],
        scheduler=scheduled.append,
    )[0].id
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

    assert scheduled == [sid]
    assert again[0].parse_status == "queued", (
        "响应里就得是非终态，前端才会开始轮询这条重试"
    )
    row = repo.get_source(sid)
    assert row.parse_status == "queued"
    assert row.error_message == "", "上一轮的失败原因已经不描述现状了，不该留着"


def test_uploading_a_failed_source_twice_in_a_row_starts_only_one_pipeline(
    repo, notebook_id
):
    """重试排队后、后台真正开跑前，用户又传了一次同一个文件（很自然：界面上
    还看不出动静）。第二次绝不能把同一条源再排一次——同一行两条流水线会互相
    清掉对方的产物，还白烧一份解析/模型开销。"""
    scheduled = []
    payload = [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                                  content=b"hello world")]
    sid = repo.upload_sources(notebook_id, payload, scheduler=scheduled.append)[0].id
    repo._runtime.source_ingestion.set_source_status(
        sid, "failed", error_message="mineru down"
    )
    scheduled.clear()

    repo.upload_sources(notebook_id, payload, scheduler=scheduled.append)  # 重试
    repo.upload_sources(notebook_id, payload, scheduler=scheduled.append)  # 手快又传一次

    assert scheduled == [sid], "只能有一条流水线被排队"
    assert repo.process_calls == []


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
    """崩溃遗留在 queued/parsing 的源在下次服务端启动时判死，从而能被重新上传救回。

    这两个状态只由上传/加链接两个入口写下，且都在同一进程里紧接着排队或直接跑
    process_source，所以服务端启动时还停着的行定义上就是被遗弃的。

    ⚠ 兜底**不在** `SQLiteRepository(...)` 构造里跑——它由服务端 lifespan 的
    `startup_warmup.run_startup()` 显式调用一次（`scripts/` 下 15+ 个 CLI 同样构造
    repository，若随构造执行会把后端正在 parsing 的源当场判死）。所以这里必须显式
    调 `_recover_interrupted_jobs()` 来模拟服务端启动，而不能指望构造本身触发。
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

    second_boot = SQLiteRepository(settings)   # 重启
    assert second_boot.get_source(queued).parse_status == "queued", (
        "构造 repository 本身不得判死任何源——CLI 也走这条路"
    )
    second_boot._recover_interrupted_jobs()    # 服务端 lifespan 的那一次
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


# ------------------------------------------------------- 重抽失败必须落终态

def test_failed_reextraction_lands_on_failed_with_a_user_readable_reason(
    repo, notebook_id, settled, monkeypatch
):
    """重抽失败必须落 'failed' 并留下原因。

    退回 'parsed' 是两重错：前端只把 extracted/failed 当终态，'parsed' 会让界面
    一路轮询到假的「处理超时」；而且这个来源在库里看着像「解析好了」，实际抽取
    从没成功过。错误信息也不能清空——那是唯一还能说明「这里出过事」的痕迹。"""
    sid = settled()
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", _boom)

    row = _upload(repo, notebook_id, doc_type="textbook")[0]

    assert row.parse_status == "failed", "响应里就得是终态 failed，前端才会停轮询"
    detail = repo.get_source(sid)
    assert detail.parse_status == "failed"
    assert detail.error_message, "错误信息不得被清空"
    for leaked in ("Traceback", "RuntimeError", "502", "Bad Gateway"):
        assert leaked not in detail.error_message, (
            f"面向用户的文案不该暴露技术细节：{detail.error_message!r}"
        )


def test_a_failed_reextraction_is_retryable_by_reuploading_the_same_file(
    repo, notebook_id, settled, monkeypatch
):
    """重抽失败后重传同一个文件必须真的重跑。

    这一条是「落 failed」的真正理由：doc_type 那时已经落库，retyped 为 false，
    所以只有失败源那条分支能救它；停在 'parsed' 的话整条路径是静默 no-op，用户
    再传多少次都没有任何反应。"""
    sid = settled()
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", _boom)
    _upload(repo, notebook_id, doc_type="textbook")
    assert repo.get_source(sid).parse_status == "failed"
    repo.process_calls.clear()

    again = _upload(repo, notebook_id, doc_type="textbook")  # 同一文件 + 同一类型

    assert again[0].id == sid
    assert repo.process_calls == [sid], "重传必须重跑整条流水线，而不是静默 no-op"


# ---------------------------------------- 抽取进行中发生的 retype 不得丢失

def test_retype_during_an_in_flight_reextraction_is_applied_not_lost(
    repo, notebook_id, settled, monkeypatch
):
    """重抽跑到一半用户又改了类型：跑完必须按新类型补跑一次。

    doc_type 在 run_extraction 一开始就被读走（它选 profile、进抽取 prompt），
    所以这个窗口里改类型，落库的类型会与真正用来抽取的 profile 不一致，而
    reuse_uploaded_source 对 'extracting' 的行不调度任何东西——没有自校验就
    没有任何人来纠正它。"""
    sid = settled(doc_type="academic_paper")
    seen_while_extracting = []

    def fake_extract(source_id, **_kw):
        seen_while_extracting.append(repo.get_source(source_id).doc_type)
        if len(seen_while_extracting) == 1:
            # 抽取正在跑（这一行此刻就是 'extracting'），用户用另一个类型重传
            assert repo.get_source(source_id).parse_status == "extracting"
            _upload(repo, notebook_id, doc_type="academic_paper")

    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", fake_extract)

    _upload(repo, notebook_id, doc_type="textbook")

    assert seen_while_extracting == ["textbook", "academic_paper"], (
        "抽取期间改的类型必须被补跑一次；只有一次说明那次 retype 被丢了"
    )
    assert repo.get_source(sid).doc_type == "academic_paper"
    assert repo.get_source(sid).parse_status == "extracted", "补跑完照样落终态"


def test_retype_during_the_first_uploads_extraction_is_applied_not_lost(
    live_repo, monkeypatch
):
    """同一个窗口在「第一次上传」的流水线里也存在，而且更容易撞上：用户嫌慢，
    用正确的类型把同一个文件又传了一次，这时第一条流水线正卡在 extracting。"""
    nb = live_repo.create_notebook(NotebookCreate(name="nb")).id
    ingestion = live_repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "notebook_has_kg", lambda _nb: True)
    _patch_parse(monkeypatch, [_element("body")])
    seen_while_extracting = []

    def fake_extract(source_id, **_kw):
        seen_while_extracting.append(live_repo.get_source(source_id).doc_type)
        if len(seen_while_extracting) == 1:
            assert live_repo.get_source(source_id).parse_status == "extracting"
            _upload(live_repo, nb, doc_type="textbook")

    monkeypatch.setattr(ingestion, "run_extraction", fake_extract)

    sid = _upload(live_repo, nb, doc_type="academic_paper")[0].id

    assert seen_while_extracting == ["academic_paper", "textbook"]
    assert live_repo.get_source(sid).doc_type == "textbook"
    assert live_repo.get_source(sid).parse_status == "extracted"
    assert len(live_repo.list_sources(nb)) == 1


# ------------------------------- 在飞行中发生的后缀（解析器）纠正不得丢失（P2-2）

def test_in_flight_suffix_correction_is_applied_at_completion_not_lost(
    live_repo, monkeypatch
):
    """在飞行中换后缀重传（不同解析器）：流水线跑完必须补一次整条重解析（用新解析器），
    而不是静默丢掉用户的后缀纠正。

    换后缀重传撞上正在跑的流水线时，reuse 只把新 file_name 记到行上（绝不动正在被读
    的盘上文件），由流水线跑完时的 _reconcile_pending_suffix 收口：一旦定型，行上
    file_name 的 parser_class 与本次实际用的 parser 不一致就整条重跑一次。抽取每条
    流水线跑一次，故它看到的 file_name 序列就是「实际解析用的名字」序列。"""
    nb = live_repo.create_notebook(NotebookCreate(name="nb")).id
    ingestion = live_repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "notebook_has_kg", lambda _nb: True)
    _patch_parse(monkeypatch, [_element("body")])

    body = b"col1,col2\n1,2"
    parsed_names: list[str] = []   # 每次流水线跑到抽取时行上的 file_name = 本次解析用的名字

    def fake_extract(source_id, **_kw):
        parsed_names.append(live_repo.get_source(source_id).file_name)
        if len(parsed_names) == 1:
            # 抽取正在跑（行此刻 'extracting'）：用户用不同解析器的后缀重传同一字节。
            assert live_repo.get_source(source_id).parse_status == "extracting"
            _upload_named(live_repo, nb, content=body, name="data.md")

    monkeypatch.setattr(ingestion, "run_extraction", fake_extract)

    sid = _upload_named(live_repo, nb, content=body, name="data.csv")[0].id

    assert parsed_names == ["data.csv", "data.md"], (
        f"跑完必须用新解析器的名字整条重解析一次；实际解析序列 {parsed_names}"
    )
    assert live_repo.get_source(sid).file_name == "data.md", "后缀纠正最终落库"
    assert live_repo.get_source(sid).file_path.endswith("data.md"), (
        "收口 repoint 把盘上文件按新名重写，file_path 与 file_name 同步"
    )
    assert live_repo.get_source(sid).parse_status == "extracted", "补跑完照样落终态"
    assert len(live_repo.list_sources(nb)) == 1, "始终只有一行"


def test_in_flight_suffix_correction_never_rewrites_the_file_being_parsed(
    live_repo, monkeypatch
):
    """安全约束：在飞行中收到换后缀重传时，绝不能重写/删除正在被这条流水线读的盘上
    文件（会破坏在跑的 parse）。只把新 file_name 记到行上，盘上文件原样不动。"""
    nb = live_repo.create_notebook(NotebookCreate(name="nb")).id
    ingestion = live_repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "notebook_has_kg", lambda _nb: True)
    _patch_parse(monkeypatch, [_element("body")])

    body = b"col1,col2\n1,2"
    # 观测记到列表、断言放在流水线**外面**——若在 fake_extract 内断言，会被
    # process_source 的 try/except 吞掉（还会被收口重跑掩盖），变异就抓不到。
    observations: list = []

    def fake_extract(source_id, **_kw):
        if observations:
            return   # 只观测第一次抽取（in-flight）；收口重跑时不再动作
        # 抽取正在跑（in-flight）：拿到这条流水线正在读的盘上文件的内容快照。
        path = live_repo.get_source(source_id).file_path
        before = ingestion.source_files.read_bytes(path)
        _upload_named(live_repo, nb, content=body, name="data.md")   # 换后缀重传
        after = ingestion.source_files.read_bytes(path)
        observations.append((before, after))

    monkeypatch.setattr(ingestion, "run_extraction", fake_extract)
    _upload_named(live_repo, nb, content=body, name="data.csv")

    assert len(observations) == 1, "安全观测必须真的执行过"
    before, after = observations[0]
    assert before == body, "前提：流水线正在读这份字节"
    assert after is not None, "在飞行中不得删除正在被解析的盘上文件"
    assert after == before, "在飞行中不得重写正在被解析的盘上文件"


def test_rename_after_the_reconcile_claim_uses_the_latest_suffix_not_the_stale_one(
    live_repo, monkeypatch
):
    """P2-2（第 5 轮收口自己引入的竞态，Codex 第 6 轮）：_reconcile_pending_suffix
    的认领把行翻成 'queued'（在飞）后、到 repoint 之间，若并发的换后缀重传走在飞分支
    又记了更新的 file_name，repoint 必须用**认领之后重读**的最新后缀，而不是认领之前
    读到的旧值——否则更新的纠正会被旧值盖掉，且下一轮 recheck（fresh vs used）随即看到
    二者一致而永不回访，静默丢失。

    时间线：上传 data.csv（P1 跑）→ 抽取中并发重传 data.md（记为待收口意图）→ P1 定型
    → 收口 reconcile 认领赢（行 'queued'）→ **认领后、repoint 前** 另一条真实线程重传
    data.txt（在飞分支再改名）→ repoint 必须用 data.txt 整条重解析，最终落 data.txt。

    变异验证：把 _reconcile_pending_suffix 里 repoint 的 latest.file_name 换回认领前的
    fresh.file_name 后，最终落 data.md，本测试转红。"""
    nb = live_repo.create_notebook(NotebookCreate(name="nb")).id
    ingestion = live_repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "notebook_has_kg", lambda _nb: True)
    _patch_parse(monkeypatch, [_element("body")])

    body = b"col1,col2\n1,2"

    # 抽取中（in-flight）用不同解析器的后缀重传一次，制造「待收口的后缀纠正」，让流水线
    # 跑完触发 _reconcile_pending_suffix（与上面 in-flight 收口用例同一手法）。
    extract_calls = {"n": 0}

    def fake_extract(source_id, **_kw):
        extract_calls["n"] += 1
        if extract_calls["n"] == 1:
            _upload_named(live_repo, nb, content=body, name="data.md")

    monkeypatch.setattr(ingestion, "run_extraction", fake_extract)

    # 认领赢之后、repoint 之前：另一条**真实线程**再换一次后缀（行此刻 'queued'，走在飞
    # 分支只改名）。join 保证它在 reconcile 重读 file_name 之前落库——正是 P2-2 的窗口。
    real_claim = ingestion.sources.claim_reparse_if_settled
    injected = {"done": False}

    def claim_then_concurrent_rename(source_id):
        won = real_claim(source_id)
        if won and not injected["done"]:
            injected["done"] = True
            t = threading.Thread(
                target=lambda: _upload_named(live_repo, nb, content=body, name="data.txt")
            )
            t.start()
            t.join(5)
        return won

    monkeypatch.setattr(
        ingestion.sources, "claim_reparse_if_settled", claim_then_concurrent_rename
    )

    sid = _upload_named(live_repo, nb, content=body, name="data.csv")[0].id

    assert injected["done"], "前提：收口 reconcile 必须真的认领成功并触发并发改名注入"
    final = live_repo.get_source(sid)
    assert final.file_name == "data.txt", (
        f"repoint 必须用认领后重读的最新后缀 data.txt，而非认领前的 data.md；实际 {final.file_name}"
    )
    assert final.file_path.endswith("data.txt"), (
        "repoint 把盘上文件按最新后缀重写，file_path 与 file_name 同步"
    )
    assert final.parse_status == "extracted", "补跑完照样落终态"
    assert len(live_repo.list_sources(nb)) == 1, "始终只有一行"


def test_an_unchanged_doc_type_costs_exactly_one_extraction(
    repo, notebook_id, settled, monkeypatch
):
    """自校验是一次主键读，不是「总是抽两遍」——类型没动就只抽一次。"""
    sid = settled(doc_type="academic_paper")
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion, "run_extraction",
        lambda source_id, **_kw: calls.append(source_id),
    )

    _upload(repo, notebook_id, doc_type="textbook")

    assert calls == [sid]


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


def test_upload_never_dedups_onto_a_hidden_memory_projection_row(
    repo, notebook_id, monkeypatch
):
    """Memory 投影源是隐藏的派生行，但它的 file_hash **非空**——存的是
    sha256(title + "\\n" + content) 指纹。上传一个字节恰好等于那段原文的文件时，
    去重不能落到它身上：那一行不在可见来源列表里，用户会看到「已上传」，刷新后
    什么都没有，而且他的文件被嫁接到了一条 Memory 投影上。"""
    import hashlib

    ingestion = repo._runtime.source_ingestion
    # 真实的 Memory 投影入口建这一行（指纹由生产代码算），只把 KG 抽取打桩掉。
    monkeypatch.setattr(ingestion, "run_extraction", lambda *_a, **_kw: None)
    title, body = "记一条 memo", "hello world"
    memory_sid = ingestion.ingest_memory_source(notebook_id, "memory-1", title, body)

    raw = f"{title}\n{body}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    assert repo.get_source(memory_sid).file_hash == digest, "前提：指纹就是这段原文"
    assert memory_sid not in {s.id for s in repo.list_sources(notebook_id)}, (
        "前提：它是隐藏行，不出现在用户能看到的来源列表里"
    )

    store = repo._runtime.source_store
    assert store.source_id_by_hash(notebook_id, digest) is None, (
        "隐藏/派生来源不参与内容去重"
    )

    uploaded = _upload(repo, notebook_id, content=raw, name="memo.txt")

    assert uploaded[0].id != memory_sid, "上传绝不能复用那条隐藏的 Memory 投影行"
    assert uploaded[0].reused is False
    assert uploaded[0].id in {s.id for s in repo.list_sources(notebook_id)}, (
        "新建的这条必须真的出现在可见来源列表里"
    )


def test_hidden_knowhow_projection_rows_are_excluded_from_dedup_too(
    repo, notebook_id
):
    """knowhow 表投影是同一套隐藏源机制（今天它的 file_hash 是空串，靠空指纹
    那道门也进不来）。这里钉住的是「按 source_type 排除」这一层本身，免得将来
    有人给它写上指纹就把同一个洞重新打开。"""
    store = repo._runtime.source_store
    digest = "e" * 64
    hidden = _seed_source(
        repo, notebook_id, file_hash=digest, created_at="2023-01-01T00:00:00",
        source_type="knowhow",
    )
    assert store.source_id_by_hash(notebook_id, digest) is None
    assert hidden not in {s.id for s in repo.list_sources(notebook_id)}


# ---------------------------- 后缀（解析器）也是不能撒手的维度：换后缀要重解析

def _upload_named(repo, notebook_id, *, content, name, doc_type="", scheduler=None):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name=name, content_type="", content=content,
                            doc_type=doc_type)],
        scheduler=scheduler,
    )


def test_reupload_with_a_different_parser_suffix_reparses_the_existing_row(
    repo, notebook_id
):
    """同样字节先传 .csv 再传 .md：parse_source_file 按后缀选解析器（.csv 逐行
    vs .md 结构块），新名被丢弃、不重解析的话用户无法通过重传纠正一个命名错误
    的文件。像失败源那样在既有行上重解析 + 更新文件名。"""
    body = b"col1,col2\n1,2\n3,4"
    first = _upload_named(repo, notebook_id, content=body, name="data.csv")
    sid = first[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "extracted")   # 定型
    repo.process_calls.clear()

    second = _upload_named(repo, notebook_id, content=body, name="data.md")

    assert second[0].id == sid, "仍复用同一行，不新建第二条"
    assert len(repo.list_sources(notebook_id)) == 1
    assert repo.get_source(sid).file_name == "data.md", "文件名必须更新为新后缀"
    assert repo.get_source(sid).type == "markdown", "source_type（模型里叫 type）随后缀更新"
    assert repo.get_source(sid).file_path.endswith("data.md"), (
        "落盘文件按新名重写，file_path 与 file_name 同步"
    )
    assert repo.process_calls == [sid], "后缀（解析器）变了必须整条流水线重跑"
    assert second[0].reused is True
    assert second[0].parse_status == "queued", "响应里就得是非终态，前端才会开始轮询"


def test_reupload_with_a_different_suffix_reschedules_when_scheduler_is_given(
    repo, notebook_id
):
    """有 scheduler（真实 API 路径）时，换后缀同样要排进后台队列，而不是同步跑。"""
    body = b"a,b\n1,2"
    scheduled = []
    sid = _upload_named(
        repo, notebook_id, content=body, name="d.csv", scheduler=scheduled.append
    )[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "extracted")
    scheduled.clear()

    again = _upload_named(
        repo, notebook_id, content=body, name="d.md", scheduler=scheduled.append
    )

    assert scheduled == [sid], "换后缀要重新排队"
    assert again[0].id == sid
    assert again[0].parse_status == "queued"
    assert repo.process_calls == [], "有 scheduler 时绝不同步跑"


def test_reupload_same_suffix_same_content_still_dedups_without_reparsing(
    repo, notebook_id
):
    """相同后缀 + 相同内容：仍然去重、绝不重跑（本特性的核心价值，别改坏）。"""
    body = b"# heading\n\nbody"
    sid = _upload_named(repo, notebook_id, content=body, name="doc.md")[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "extracted")
    repo.process_calls.clear()

    second = _upload_named(repo, notebook_id, content=body, name="doc.md")

    assert second[0].id == sid
    assert repo.process_calls == [], "同后缀同内容不该重跑"
    assert repo.get_source(sid).file_name == "doc.md"


def test_reupload_with_a_different_suffix_that_maps_to_the_same_parser_is_not_a_reparse(
    repo, notebook_id
):
    """.md 与 .markdown 映射到同一个解析器（parser_class 同为 'markdown'）——
    不算解析器变化，纯复用不重跑、不改名。判据复用 parse_source_file 的分类，
    不是「后缀字符串不同就重跑」。"""
    body = b"# heading\n\nbody"
    sid = _upload_named(repo, notebook_id, content=body, name="a.md")[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "extracted")
    repo.process_calls.clear()

    second = _upload_named(repo, notebook_id, content=body, name="a.markdown")

    assert second[0].id == sid
    assert repo.process_calls == [], "同解析器（.md/.markdown）不触发重解析"
    assert repo.get_source(sid).file_name == "a.md", "解析器没变就不改名"


def test_reupload_with_a_different_suffix_while_in_flight_persists_intent_but_does_not_reparse(
    repo, notebook_id
):
    """还在飞行中（queued/parsing/extracting）的行不因换后缀而当场重排——那条正在跑
    的流水线拥有这个文件，重排会让同一行并发两条流水线（与既有「不重入」一致）。但
    用户的后缀纠正**不能静默丢失**：把新 file_name 持久化到行上（不动盘上文件），
    由那条流水线跑完时收口（见 _reconcile_pending_suffix）。"""
    body = b"a,b\n1,2"
    sid = _upload_named(repo, notebook_id, content=body, name="d.csv")[0].id
    assert repo.get_source(sid).parse_status == "queued"   # 打桩的 process_source 让它停在 queued
    original_path = repo.get_source(sid).file_path
    repo.process_calls.clear()

    second = _upload_named(repo, notebook_id, content=body, name="d.md")

    assert repo.process_calls == [], "在飞行中的行绝不因换后缀当场重排"
    assert repo.get_source(sid).file_name == "d.md", (
        "用户的后缀纠正必须持久化到行上（意图），跑完才收口——不能静默丢失"
    )
    assert repo.get_source(sid).file_path == original_path, (
        "但绝不动正在被这条流水线读的盘上文件（file_path 不变）"
    )
    assert second[0].file_name == "d.md", "返回值如实带上现在的文件名"
    assert repo.get_source(sid).parse_status == "queued", "状态未被翻动（仍在飞行中）"


# ============================================================ 并发竞态（真线程）
# 这些用例用真线程 + Barrier 强制两个上传同时抵达认领点，复现串行测试挡不住的
# TOCTOU。变异（把原子认领换回无条件写）后它们必须转红。


def test_concurrent_reupload_of_a_failed_source_starts_only_one_pipeline(
    repo, notebook_id, monkeypatch
):
    """P1：两个并发上传同一失败源。两者都在任一方写 'queued' 之前读到
    parse_status=='failed'，若各自无条件调度就会给同一行起两条流水线（互相清对方的
    抽取状态/KG 产物 + 白烧一份解析/模型开销）。原子认领（WHERE parse_status='failed'
    的守卫 UPDATE，全局写锁串行）保证恰有一个 rowcount==1 去调度。"""
    payload = [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                                  content=b"hello world")]
    sid = repo.upload_sources(notebook_id, payload, scheduler=lambda _s: None)[0].id
    repo._runtime.source_ingestion.set_source_status(
        sid, "failed", error_message="mineru down"
    )

    # Barrier 把两个线程钉在认领点上同时放行——都已在方法开头读到 'failed'，于是
    # 都进失败分支、都调用 claim_failed_for_retry。真正的串行由认领里的写锁决定。
    store = repo._runtime.source_store
    real_claim = store.claim_failed_for_retry
    barrier = threading.Barrier(2, timeout=5)

    def racing_claim(source_id):
        barrier.wait()
        return real_claim(source_id)

    monkeypatch.setattr(store, "claim_failed_for_retry", racing_claim)

    scheduled: list[str] = []
    sched_lock = threading.Lock()

    def sched(source_id):
        with sched_lock:
            scheduled.append(source_id)

    def do_upload():
        return repo.upload_sources(notebook_id, payload, scheduler=sched)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_upload)
        f2 = pool.submit(do_upload)
        f1.result(timeout=10)
        f2.result(timeout=10)

    assert scheduled == [sid], (
        f"并发重试同一失败源只能排队一条流水线，实际排了 {scheduled}"
    )
    assert repo.get_source(sid).parse_status == "queued"
    assert len(repo.list_sources(notebook_id)) == 1


def test_concurrent_first_upload_of_identical_bytes_creates_one_row(
    tmp_path, monkeypatch
):
    """P2：两个并发首次上传相同字节到同一 notebook。此前「source_id_by_hash(读)→
    insert(写)」是两步，两者都查不到 → 各插一行（migration 索引非唯一拦不住）。
    两个独立 repo（两把写锁）共享一个 DB 文件，模拟后端 + 离线 batch_ingest 跨进程：
    原子由 insert_source_if_absent 里 BEGIN IMMEDIATE + 事务内重查兑现。Barrier 钉在
    begin_immediate 上让两者同抵临界区，真正的串行交给 SQLite 的 RESERVED 锁。"""
    from app.core.config import Settings

    _settings(tmp_path, monkeypatch)  # 设一次共享的 DATABASE_URL / STORAGE_DIR
    repo_a = SQLiteRepository(Settings())
    repo_b = SQLiteRepository(Settings())  # 同一 DB 文件、独立 SqliteDatabase（独立写锁）
    nb = repo_a.create_notebook(NotebookCreate(name="nb")).id

    barrier = threading.Barrier(2, timeout=5)
    db_a = repo_a._runtime.source_store.database
    db_b = repo_b._runtime.source_store.database
    begin_a, begin_b = db_a.begin_immediate, db_b.begin_immediate

    def sync_begin_a(conn):
        barrier.wait()
        begin_a(conn)

    def sync_begin_b(conn):
        barrier.wait()
        begin_b(conn)

    monkeypatch.setattr(db_a, "begin_immediate", sync_begin_a)
    monkeypatch.setattr(db_b, "begin_immediate", sync_begin_b)

    payload = [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                                  content=b"identical bytes")]
    sched_a: list[str] = []
    sched_b: list[str] = []

    def do(repo, sched):
        return repo.upload_sources(nb, payload, scheduler=sched.append)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(do, repo_a, sched_a)
        fb = pool.submit(do, repo_b, sched_b)
        ra = fa.result(timeout=10)
        rb = fb.result(timeout=10)

    assert len(repo_a.list_sources(nb)) == 1, "并发首传相同字节只能建一行"
    assert len(sched_a) + len(sched_b) == 1, "只有插入方那一条流水线被调度"
    assert sorted([ra[0].reused, rb[0].reused]) == [False, True], (
        "恰好一方新建(reused=False)、一方复用(reused=True)"
    )


def test_serial_first_upload_still_inserts_and_schedules(repo, notebook_id):
    """原子插入路径不改串行首传语义：新建一行、reused=False、跑一次 process。"""
    result = _upload(repo, notebook_id, content=b"fresh bytes", name="doc.txt")
    assert result[0].reused is False
    assert repo.process_calls == [result[0].id]
    assert len(repo.list_sources(notebook_id)) == 1


def test_retype_landing_in_the_terminal_mark_window_forces_a_reextract(
    repo, notebook_id, settled, monkeypatch
):
    """P2-doc_type：并发 retype 恰好落在「本轮抽取读到的 doc_type」与「标记 extracted」
    之间的窄窗口。旧写法这两步不原子——retype 在此改了类型、且因行是 'extracting'
    而不调度，抽取又无条件落终态 → 存库 doc_type 与实际抽取 profile 永久失配。
    mark_extracted_if_doc_type 把「比对 + 终态」合成一条 WHERE doc_type=本轮值 的
    UPDATE：窗口里被改 → rowcount 0 → 按新类型再跑一轮收口。

    窄窗口用真线程复现会 flaky，这里用确定性注入（monkeypatch 落终态的那一步，在它
    真正写库前塞进一次并发 retype）——与既有 doc_type 用例同一手法，精确命中窗口。"""
    sid = settled(doc_type="academic_paper")   # 已定型 extracted、has KG
    store = repo._runtime.source_store
    real_mark = store.mark_extracted_if_doc_type
    injected: list[int] = []

    def racing_mark(source_id, expected_doc_type, **kw):
        if not injected:
            injected.append(1)
            # 抽取刚跑完、终态未落的那一刻，行此刻是 'extracting'：模拟用户用又一个
            # 类型重传（reuse 只记类型、claim 认领落空、不调度）。
            _upload(repo, notebook_id, doc_type="academic_paper")
        return real_mark(source_id, expected_doc_type, **kw)

    monkeypatch.setattr(store, "mark_extracted_if_doc_type", racing_mark)

    _upload(repo, notebook_id, doc_type="textbook")   # academic_paper→textbook 触发重抽

    assert repo.extract_calls == [sid, sid], (
        f"窗口里被 retype 必须按新类型补跑一轮；实际 extract={repo.extract_calls}"
    )
    assert repo.get_source(sid).doc_type == "academic_paper", (
        "存库 doc_type 必须等于最后一轮抽取真正用的类型"
    )
    assert repo.get_source(sid).parse_status == "extracted", "补跑完照样落终态"


def test_concurrent_reupload_with_a_different_suffix_reparses_exactly_once(
    repo, notebook_id, monkeypatch
):
    """P2-1：两个并发上传同一 settled 源、后缀不同（.csv→.md）。两者都在任一方写
    'queued' 之前读到旧终态；若各自无条件 _repoint_reused_file（重写盘上文件）+ 调度，
    就给同一行起两条流水线（互清 elements/KG），而且**同一个盘上文件被重写两次**。
    claim_reparse_if_settled 的守卫 UPDATE（WHERE parse_status IN ('failed','extracted')）
    保证恰有一个 rowcount==1 去 repoint+调度；输家什么都不做。"""
    body = b"col1,col2\n1,2\n3,4"
    sid = _upload_named(repo, notebook_id, content=body, name="data.csv")[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "extracted")   # 定型
    repo.process_calls.clear()

    ingestion = repo._runtime.source_ingestion
    store = repo._runtime.source_store

    # 数盘上文件被重写几次——只有认领赢家该 repoint。
    repoint_calls: list[int] = []
    repoint_lock = threading.Lock()
    real_repoint = ingestion._repoint_reused_file

    def counting_repoint(*args, **kwargs):
        with repoint_lock:
            repoint_calls.append(1)
        return real_repoint(*args, **kwargs)

    monkeypatch.setattr(ingestion, "_repoint_reused_file", counting_repoint)

    # Barrier 把两个线程钉在认领点上同时放行——都已读到 'extracted'，于是都进 reparse
    # 分支、都调用 claim_reparse_if_settled。真正的串行由认领里的写锁决定。
    real_claim = store.claim_reparse_if_settled
    barrier = threading.Barrier(2, timeout=5)

    def racing_claim(source_id):
        barrier.wait()
        return real_claim(source_id)

    monkeypatch.setattr(store, "claim_reparse_if_settled", racing_claim)

    scheduled: list[str] = []
    sched_lock = threading.Lock()

    def sched(source_id):
        with sched_lock:
            scheduled.append(source_id)

    def do_upload():
        return _upload_named(
            repo, notebook_id, content=body, name="data.md", scheduler=sched
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_upload)
        f2 = pool.submit(do_upload)
        f1.result(timeout=10)
        f2.result(timeout=10)

    assert scheduled == [sid], (
        f"并发换后缀重传同一 settled 源只能排队一条流水线，实际 {scheduled}"
    )
    assert len(repoint_calls) == 1, (
        f"盘上文件只能被重写一次（只有认领赢家 repoint），实际 {len(repoint_calls)} 次"
    )
    assert repo.get_source(sid).file_name == "data.md"
    assert repo.get_source(sid).parse_status == "queued"
    assert len(repo.list_sources(notebook_id)) == 1


def test_retype_on_an_in_flight_source_never_starts_a_second_reextract(
    repo, notebook_id, settled, monkeypatch
):
    """retype 撞上「行还在 'extracting'」（in-flight）：claim_reextract_if_extracted
    以 WHERE parse_status='extracted' 认领落空 → 绝不另起第二条重抽（同一行两条抽取
    互相清 KG 产物），交给正在跑的那条流水线的终态 doc_type 收口。这是 doc_type 竞态
    retype 侧的原子表决——补上「读 parse_status 再决定」的 TOCTOU。"""
    from app.services.kg import scheduler as kg_scheduler

    sid = settled(doc_type="academic_paper")
    repo._runtime.source_ingestion.set_source_status(sid, "extracting")  # 假装正在跑
    submitted: list = []
    monkeypatch.setattr(
        kg_scheduler, "submit_job", lambda fn, *args, **_kw: submitted.append(args)
    )

    repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name="a.txt", content_type="text/plain",
                            content=b"hello world", doc_type="textbook")],
        scheduler=lambda _s: None,
    )

    assert repo.get_source(sid).doc_type == "textbook", "新类型照记（存库）"
    assert submitted == [], "在跑的行绝不另起第二条重抽"
    assert repo.get_source(sid).parse_status == "extracting", "仍是在跑，未被认领翻动"


# ------------------------------- idle 'parsed' 也要能认领换后缀重解析（P2-1，round-7）


def test_reupload_with_a_different_suffix_reparses_an_idle_parsed_row(
    repo, notebook_id
):
    """P2-1：搁浅在 idle 'parsed' 的源（KG 取消/失败、启动恢复把 'extracting' 退回
    'parsed' 且无 worker 在跑）换后缀重传必须真的整条重跑。此前 'parsed' 一律当在飞——
    只持久化意图、由「那条正在跑的流水线」收口，但 idle 时根本没有流水线，纠正永久悬置。

    变异：把 claim_reparse_if_settled 的 WHERE 去掉 'parsed'（或 reuse 的 claimable 去掉
    'parsed'），idle 'parsed' 走 else 只改名不调度 → process_calls==[] → 本测试转红。"""
    body = b"col1,col2\n1,2\n3,4"
    sid = _upload_named(repo, notebook_id, content=body, name="data.csv")[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "parsed")  # 搁浅 idle 'parsed'
    repo.process_calls.clear()

    second = _upload_named(repo, notebook_id, content=body, name="data.md")

    assert second[0].id == sid, "仍复用同一行"
    assert repo.get_source(sid).file_name == "data.md", "后缀纠正必须落库"
    assert repo.get_source(sid).file_path.endswith("data.md"), "落盘文件按新名重写"
    assert repo.process_calls == [sid], (
        "idle 'parsed' 换后缀必须整条重跑；[] 说明又被当在飞、纠正永久悬置"
    )
    assert repo.get_source(sid).parse_status == "queued", "响应里就得是非终态"


def test_reupload_same_suffix_on_an_idle_parsed_row_does_not_reparse(
    repo, notebook_id
):
    """守卫的补集：idle 'parsed' + 相同解析器（.md/.markdown 同类）不触发重跑——
    只有 parser_class 变了才认领，别把「'parsed' 也能认领」误扩成「'parsed' 总重跑」。"""
    body = b"# h\n\nbody"
    sid = _upload_named(repo, notebook_id, content=body, name="a.md")[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "parsed")
    repo.process_calls.clear()

    _upload_named(repo, notebook_id, content=body, name="a.markdown")

    assert repo.process_calls == [], "同解析器不触发重解析"
    assert repo.get_source(sid).file_name == "a.md", "解析器没变就不改名"
    assert repo.get_source(sid).parse_status == "parsed", "状态未被翻动"


def test_reparse_claim_loss_on_a_parsed_row_persists_intent_for_reconcile(
    repo, notebook_id, monkeypatch
):
    """P2-1：'parsed' 行的 reparse 认领**落空**（并发的 pipeline 守卫、或另一次 reparse
    抢先把行翻出 'parsed'）时绝不能什么都不做——必须把新 file_name 持久化到行上、不动盘上
    文件，交给拥有者的 _reconcile 收口。此前 settled 认领落空是「什么都不做」，把 'parsed'
    纳入认领后若不补这条，in-flight 'parsed'（pipeline 赢守卫）的后缀纠正会被静默丢。

    变异：删掉 else 分支的 rename_source_file（认领落空什么都不做）→ file_name 停在旧后缀
    → 转红。"""
    body = b"a,b\n1,2"
    sid = _upload_named(repo, notebook_id, content=body, name="d.csv")[0].id
    ingestion = repo._runtime.source_ingestion
    ingestion.set_source_status(sid, "parsed")
    original_path = repo.get_source(sid).file_path
    repo.process_calls.clear()
    # 模拟认领落空：拥有者（pipeline 守卫或并发 reparse）已抢先翻出 'parsed'。
    monkeypatch.setattr(
        ingestion.sources, "claim_reparse_if_settled", lambda _sid: False
    )

    _upload_named(repo, notebook_id, content=body, name="d.md")

    assert repo.process_calls == [], "认领落空绝不调度（拥有者会整条重跑）"
    assert repo.get_source(sid).file_name == "d.md", (
        "认领落空必须持久化意图（rename）交拥有者收口；停在 d.csv 说明纠正被静默丢"
    )
    assert repo.get_source(sid).file_path == original_path, (
        "但绝不动拥有者正在读的盘上文件（file_path 不变）"
    )


def test_extract_guard_declines_a_row_a_reparse_already_claimed(repo, notebook_id):
    """P2-1 守卫语义（确定性）：并发 reparse 已把行认领成 'queued' 后，pipeline 的
    parsed→extracting 守卫必须落空（rowcount 0），使流水线优雅让出、不 clobber 认领。

    变异：把 claim_extracting_if_parsed 的 `AND parse_status='parsed'` 去掉（无条件 set
    'extracting'）→ 它会把 'queued' 翻成 'extracting' 并返回 True → 本测试转红。"""
    sid = _upload_named(repo, notebook_id, content=b"x", name="d.csv")[0].id
    store = repo._runtime.source_store
    store.set_status(sid, "queued")  # 模拟并发 reparse 已认领

    assert store.claim_extracting_if_parsed(sid) is False, (
        "并发 reparse 已认领（'queued'）的行，守卫必须落空"
    )
    assert repo.get_source(sid).parse_status == "queued", "守卫落空绝不得翻动行"


def test_extract_guard_claims_a_still_parsed_row(repo, notebook_id):
    """守卫正例：无并发时行仍是 'parsed' → 认领成功、翻成 'extracting'、流水线照常继续。
    钉住「正常首传流水线不被误让出」（rowcount==1 照常）。"""
    sid = _upload_named(repo, notebook_id, content=b"x", name="d.csv")[0].id
    store = repo._runtime.source_store
    store.set_status(sid, "parsed")

    assert store.claim_extracting_if_parsed(sid) is True
    assert repo.get_source(sid).parse_status == "extracting"


def test_idle_parsed_row_guard_and_reparse_claim_are_mutually_exclusive(
    repo, notebook_id
):
    """P2-1 真线程 + Barrier：pipeline 的 parsed→extracting 守卫与 reparse 的
    parsed→queued 认领并发争同一 'parsed' 行，进程全局写锁让恰好一个翻出 'parsed'
    （XOR）——不是两条都赢去起两条互清产物的流水线。"""
    sid = _upload_named(repo, notebook_id, content=b"x", name="d.csv")[0].id
    store = repo._runtime.source_store
    store.set_status(sid, "parsed")

    barrier = threading.Barrier(2, timeout=5)
    results: dict[str, bool] = {}
    lock = threading.Lock()

    def run_guard():
        barrier.wait()
        won = store.claim_extracting_if_parsed(sid)
        with lock:
            results["guard"] = won

    def run_reparse():
        barrier.wait()
        won = store.claim_reparse_if_settled(sid)
        with lock:
            results["reparse"] = won

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run_guard)
        f2 = pool.submit(run_reparse)
        f1.result(timeout=10)
        f2.result(timeout=10)

    assert results["guard"] ^ results["reparse"], (
        f"恰好一个认领翻出 'parsed'（不得两条都赢），实际 {results}"
    )
    assert repo.get_source(sid).parse_status in ("extracting", "queued")


def test_pipeline_yields_when_a_concurrent_reparse_claims_the_parsed_row(
    live_repo, monkeypatch
):
    """P2-1 端到端让出：一条真实流水线跑到 parsed→extracting 守卫点时，一次并发换后缀
    reparse 抢先认领了这个 'parsed' 行。守卫落空 → 本流水线优雅让出（不抽取、不落终态、
    不清对方状态），reparse 的新流水线整条重跑并落终态。**只有一条**流水线做抽取。

    并发用确定性注入（在守卫真正认领前塞进一次并发 reparse，与既有 doc_type/后缀窗口
    用例同一手法），避免窄窗口真线程 flaky。变异（守卫改无条件 set 'extracting'）后本流水线
    不再让出 → 它也抽一次（还会 clobber 对方终态、触发自己的收口再抽）→ extract 次数 > 1
    → 转红。"""
    nb = live_repo.create_notebook(NotebookCreate(name="nb")).id
    ingestion = live_repo._runtime.source_ingestion
    monkeypatch.setattr(ingestion, "notebook_has_kg", lambda _nb: True)
    _patch_parse(monkeypatch, [_element("body")])

    body = b"col1,col2\n1,2"
    extract_calls: list[str] = []
    monkeypatch.setattr(
        ingestion, "run_extraction",
        lambda source_id, **_kw: extract_calls.append(source_id),
    )

    real_guard = ingestion.sources.claim_extracting_if_parsed
    injected = {"done": False}

    def guard_with_concurrent_reparse(source_id):
        # 本流水线到达守卫、真正认领之前：并发换后缀 reparse 抢先认领这个 'parsed' 行并
        # 整条重跑（scheduler=None → 同步跑到底），把行推到 'extracted'。随后真守卫看到
        # 行已非 'parsed' → 落空 → 本流水线让出。
        if not injected["done"]:
            injected["done"] = True
            _upload_named(live_repo, nb, content=body, name="data.md")
        return real_guard(source_id)

    monkeypatch.setattr(
        ingestion.sources, "claim_extracting_if_parsed", guard_with_concurrent_reparse
    )

    sid = _upload_named(live_repo, nb, content=body, name="data.csv")[0].id

    assert injected["done"], "前提：本流水线必须真的到达守卫并触发并发 reparse 注入"
    final = live_repo.get_source(sid)
    assert final.file_name == "data.md", "reparse 的新流水线按新后缀整条重跑并落库"
    assert final.parse_status == "extracted", "reparse 流水线跑完落终态"
    assert extract_calls == [sid], (
        f"只有 reparse 那条流水线抽了一次；本流水线让出不抽。实际 extract={extract_calls}"
    )
    assert len(live_repo.list_sources(nb)) == 1, "始终只有一行"
