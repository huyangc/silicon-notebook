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


def _upload(
    repo, notebook_id, content=b"hello world", name="a.txt", doc_type="",
    doc_type_explicit=False,
):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(
            file_name=name, content_type="text/plain", content=content,
            doc_type=doc_type, doc_type_explicit=doc_type_explicit)],
    )


def _element(text):
    return types.SimpleNamespace(
        element_type="paragraph", location_label="p1", text=text, metadata={}
    )


def _patch_parse(monkeypatch, result):
    """把文件解析器换掉：result 是异常类型则抛，否则当作 elements 返回。"""
    import app.services.parser_chain_execution as parser_execution

    def fake(*_args, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(parser_execution, "parse_builtin_source_file", fake)


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
    键），静默丢掉 = 这条源永远按错的类型入图。用户手动选了类型 = 显式表态。"""
    sid = settled()
    assert repo.get_source(sid).doc_type == ""

    second = _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

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
    """类型没变就是纯复用：再抽一遍是白烧的模型开销。哪怕用户显式又选了一遍同一个
    类型（explicit=True 但新旧值相等），也不该重抽——判据的 `incoming != stored` 一半
    挡住它。"""
    sid = settled(doc_type="textbook")

    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

    assert repo.get_source(sid).doc_type == "textbook"
    assert repo.extract_calls == []
    assert repo.process_calls == []


def test_explicit_auto_detect_resets_the_stored_doc_type_and_reextracts(
    repo, notebook_id, settled
):
    """用户把类型下拉框**显式**选回「自动检测」（doc_type="" 且 doc_type_explicit=True）
    必须能把一条已按具体类型定型的源重置回自动——否则 UI 上根本无法把类型改回自动。
    与「没表态」相反（后者保留旧值，见下一条）。这修好了原 bug。

    变异验证：把 reuse 里 retyped 判据的 doc_type_explicit 去掉、退回 bool(incoming)
    → 空值被当没意见 → doc_type 停在 textbook → 本测试转红。"""
    sid = settled(doc_type="textbook")

    second = _upload(repo, notebook_id, doc_type="", doc_type_explicit=True)

    assert second[0].id == sid and second[0].reused is True, "仍复用同一行"
    assert repo.get_source(sid).doc_type == "", "显式自动检测必须把类型重置回自动（''）"
    assert second[0].doc_type == "", "返回值如实带上现在生效的（自动）类型"
    assert repo.extract_calls == [sid], "重置类型必须按新（自动）profile 重抽 KG"
    assert repo.process_calls == [], "内容没变，绝不重新解析"


def test_non_explicit_reupload_never_retypes_even_with_a_specific_type(
    repo, notebook_id, settled
):
    """重传时没动类型下拉框（doc_type_explicit=False）绝不改既有源的类型——哪怕发来的
    doc_type 是具体值（auto-detect 顺手填的建议）、甚至与旧值不同。这正是内容去重的
    核心场景：重传同一文件不该因为没显式表态就把已定型的类型静默抹掉重抽。

    变异验证：把 reuse 里 retyped 判据里的 doc_type_explicit 无视掉（退回只认
    bool(incoming)）→ 非显式的 "academic_paper" 会把 textbook 冲掉重抽 → 本测试转红。"""
    sid = settled(doc_type="textbook")

    # auto-detect 填了个具体（且不同的）值，但用户没动下拉框 → 非显式。
    _upload(repo, notebook_id, doc_type="academic_paper", doc_type_explicit=False)

    assert repo.get_source(sid).doc_type == "textbook", (
        "非显式重传不得覆盖已存类型（哪怕发来的是具体值/不同值）"
    )
    assert repo.extract_calls == [], "没有显式表态就没有重抽"
    assert repo.process_calls == [], "内容没变，绝不重新解析"


def test_explicit_paper_retypes_the_reused_source_to_paper(
    repo, notebook_id, settled
):
    """显式 + 具体类型（paper）→ 改成该类型并按新 profile 重抽。"""
    sid = settled(doc_type="textbook")

    second = _upload(
        repo, notebook_id, doc_type="academic_paper", doc_type_explicit=True
    )

    assert second[0].id == sid and second[0].reused is True
    assert repo.get_source(sid).doc_type == "academic_paper", "显式改类型必须落库"
    assert repo.extract_calls == [sid], "类型变了必须按新类型重抽"


# ------------------------------------- retype 到非论文类型要清掉旧论文元数据

def _seed_paper_meta(repo, notebook_id, sid):
    """给一条源塞一份论文元数据(模拟它此前被当 academic 抽过标题/作者)。"""
    repo._runtime.source_store.upsert_paper_meta(
        sid, notebook_id,
        {
            "is_paper": True, "paper_title": "A Grand Unified Theory",
            "venue": "Nature", "pub_year": 2024, "doi": "10.1000/xyz",
            "keywords": ["superconductivity"],
            "authors": [
                {"position": 0, "name": "Ada Lovelace",
                 "affiliation": "Analytical Engine Lab"},
            ],
            "model": "test-model", "raw_json": "{}",
        },
    )


def _author_count(repo, sid):
    with repo._write() as db:
        return db.execute(
            "SELECT COUNT(*) AS c FROM source_authors WHERE source_id = ?", (sid,)
        ).fetchone()["c"]


def test_explicit_retype_to_ineligible_type_clears_stale_paper_meta(
    repo, notebook_id, settled
):
    """academic 源已抽过论文元数据,用户**显式**改成 textbook(非论文类型)→ 旧标题/
    作者必须清空:元数据抽取 gate 从此跳过这条源、不再刷新它,不清就会永远把它当
    论文展示/搜索/计数。

    变异验证:去掉 reuse 里的 `self.sources.clear_paper_meta(...)` → 元数据残留、
    仍计为论文 → 本测试转红。"""
    sid = settled(doc_type="academic_paper")
    _seed_paper_meta(repo, notebook_id, sid)
    assert repo.get_paper_meta(sid)["is_paper"] is True, "前提:这条源此刻被当论文"
    assert _author_count(repo, sid) == 1

    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

    assert repo.get_source(sid).doc_type == "textbook"
    assert repo.get_paper_meta(sid) is None, "改成非论文类型后旧论文元数据必须清空"
    assert _author_count(repo, sid) == 0, "作者行也要一并清掉"


def test_retype_between_eligible_types_keeps_paper_meta(repo, notebook_id, settled):
    """auto→academic 都是合格论文类型:类型变了要重抽,但论文元数据仍适用、不该清。

    变异验证:把清理条件从「新类型不合格」放宽成「只要 retype 就清」→ 本测试转红。"""
    sid = settled(doc_type="")  # 自动检测(合格)
    _seed_paper_meta(repo, notebook_id, sid)

    _upload(repo, notebook_id, doc_type="academic_paper", doc_type_explicit=True)

    assert repo.get_source(sid).doc_type == "academic_paper"
    meta = repo.get_paper_meta(sid)
    assert meta is not None and meta["is_paper"] is True, "合格→合格不清元数据"
    assert _author_count(repo, sid) == 1


def test_non_explicit_reupload_never_clears_paper_meta(repo, notebook_id, settled):
    """非显式重传(没动类型下拉框)绝不 retype,自然也不清元数据——哪怕发来的是非论文
    类型。这条守住「重传同一文件」这个去重核心场景不误伤既有元数据。"""
    sid = settled(doc_type="academic_paper")
    _seed_paper_meta(repo, notebook_id, sid)

    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=False)

    assert repo.get_source(sid).doc_type == "academic_paper", "非显式不改类型"
    assert repo.get_paper_meta(sid) is not None, "没 retype 就不该清元数据"
    assert _author_count(repo, sid) == 1


def test_a_brand_new_source_applies_its_doc_type_regardless_of_the_explicit_flag(
    repo, notebook_id
):
    """新建源（内容首见）照常应用发来的 doc_type——显式 flag 只 gate「复用源的
    retype」，不影响新源；auto-detect 的建议值对新源当然要生效（explicit=False 也是）。"""
    first = _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=False)

    assert first[0].reused is False, "内容首见 = 新建，不是复用"
    assert repo.get_source(first[0].id).doc_type == "textbook", (
        "新源必须按发来的 doc_type 定型，与 explicit flag 无关"
    )


def test_doc_type_explicit_defaults_to_false_for_callers_that_omit_it(
    repo, notebook_id, settled
):
    """不发 doc_type_explicit 的调用方（老前端 / batch_ingest）：字段缺省 = False =
    「没显式表态」= 保留旧类型，绝不 retype 既有源（它们本就不该动既有源的类型）。"""
    sid = settled(doc_type="textbook")

    # 构造时**不传** doc_type_explicit（模拟老调用方），却带了具体且不同的 doc_type。
    repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(
            file_name="a.txt", content_type="text/plain",
            content=b"hello world", doc_type="academic_paper")],
    )

    assert repo.get_source(sid).doc_type == "textbook", (
        "缺省 explicit 视为 False，绝不改既有源类型"
    )
    assert repo.extract_calls == []


def test_retyping_an_in_flight_source_updates_the_type_but_starts_no_pipeline(
    repo, notebook_id
):
    """还在排队/解析中的行：类型照记，但绝不重入——同一行两条流水线是重复开销。"""
    sid = _upload(repo, notebook_id)[0].id
    assert repo.get_source(sid).parse_status == "queued"
    repo.process_calls.clear()

    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

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
                            content=b"hello world", doc_type="textbook",
                            doc_type_explicit=True)],
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

    row = _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)[0]

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
    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)
    assert repo.get_source(sid).parse_status == "failed"
    repo.process_calls.clear()

    # 同一文件 + 同一类型（此刻 stored 已是 textbook，故 retyped 为 false，靠失败源分支救）
    again = _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

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
            # 抽取正在跑（这一行此刻就是 'extracting'），用户显式用另一个类型重传
            assert repo.get_source(source_id).parse_status == "extracting"
            _upload(repo, notebook_id, doc_type="academic_paper", doc_type_explicit=True)

    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", fake_extract)

    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

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
            _upload(live_repo, nb, doc_type="textbook", doc_type_explicit=True)

    monkeypatch.setattr(ingestion, "run_extraction", fake_extract)

    sid = _upload(live_repo, nb, doc_type="academic_paper", doc_type_explicit=True)[0].id

    assert seen_while_extracting == ["academic_paper", "textbook"]
    assert live_repo.get_source(sid).doc_type == "textbook"
    assert live_repo.get_source(sid).parse_status == "extracted"
    assert len(live_repo.list_sources(nb)) == 1


def test_an_unchanged_doc_type_costs_exactly_one_extraction(
    repo, notebook_id, settled, monkeypatch
):
    """自校验是一次主键读，不是「总是抽两遍」——抽取期间类型没再动就只抽一次。"""
    sid = settled(doc_type="academic_paper")
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion, "run_extraction",
        lambda source_id, **_kw: calls.append(source_id),
    )

    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

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


# ---------------------------- 换后缀（解析器）不再重解析：复用既有源、保留原解析

def _upload_named(repo, notebook_id, *, content, name, doc_type="", scheduler=None):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(file_name=name, content_type="", content=content,
                            doc_type=doc_type)],
        scheduler=scheduler,
    )


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


def test_reupload_with_a_different_suffix_reuses_the_source_and_keeps_the_original_parse(
    repo, notebook_id
):
    """移除「换后缀自动重解析」子特性后的新行为：同一份内容以不同后缀重传（如先
    .csv 后 .md）复用既有源、**保留原解析**——原 file_name/file_path/parse_status 不变，
    绝不重跑 process_source。要换解析器请删除该源再重传。"""
    body = b"col1,col2\n1,2\n3,4"
    first = _upload_named(repo, notebook_id, content=body, name="data.csv")
    sid = first[0].id
    repo._runtime.source_ingestion.set_source_status(sid, "extracted")   # 定型
    original = repo.get_source(sid)
    repo.process_calls.clear()

    second = _upload_named(repo, notebook_id, content=body, name="data.md")

    assert second[0].id == sid, "仍复用同一行，不新建第二条"
    assert second[0].reused is True
    assert len(repo.list_sources(notebook_id)) == 1
    assert repo.process_calls == [], "换后缀不再触发任何重解析"
    assert repo.get_source(sid).file_name == "data.csv", "原 file_name 保留（不改名）"
    assert repo.get_source(sid).file_path == original.file_path, (
        "原 file_path 保留（不重写落盘文件）"
    )
    assert repo.get_source(sid).parse_status == "extracted", "原终态保留（不回退）"


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
            # 抽取刚跑完、终态未落的那一刻，行此刻是 'extracting'：模拟用户显式用又一个
            # 类型重传（reuse 只记类型、claim 认领落空、不调度）。
            _upload(repo, notebook_id, doc_type="academic_paper", doc_type_explicit=True)
        return real_mark(source_id, expected_doc_type, **kw)

    monkeypatch.setattr(store, "mark_extracted_if_doc_type", racing_mark)

    # academic_paper→textbook（用户显式选了 textbook）触发重抽
    _upload(repo, notebook_id, doc_type="textbook", doc_type_explicit=True)

    assert repo.extract_calls == [sid, sid], (
        f"窗口里被 retype 必须按新类型补跑一轮；实际 extract={repo.extract_calls}"
    )
    assert repo.get_source(sid).doc_type == "academic_paper", (
        "存库 doc_type 必须等于最后一轮抽取真正用的类型"
    )
    assert repo.get_source(sid).parse_status == "extracted", "补跑完照样落终态"


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
                            content=b"hello world", doc_type="textbook",
                            doc_type_explicit=True)],
        scheduler=lambda _s: None,
    )

    assert repo.get_source(sid).doc_type == "textbook", "新类型照记（存库）"
    assert submitted == [], "在跑的行绝不另起第二条重抽"
    assert repo.get_source(sid).parse_status == "extracting", "仍是在跑，未被认领翻动"
