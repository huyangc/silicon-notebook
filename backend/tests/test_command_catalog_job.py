"""命令目录抽取的 job / 持久化 / API(方案 C·C1b)。

钉的是这批改动里**只要被后来的人顺手改掉就会造成静默损失**的那几条:

1. **迁移与单飞守卫** —— 全新库建表、已部署库(user_version=37)补建、条件唯一索引
   同时覆盖 queued 与 running。守卫漏 queued,重复 POST 就会在「行已建、线程未起」
   窗口里排出两个写同一份候选的 job。
2. **终态纪律** —— 正常、取消、熔断、`BaseException` 四条退出路径都必须把行落成终态。
   `KeyboardInterrupt`/`SystemExit` 继承 `BaseException`,`except Exception` 接不到;
   行留在 queued/running 就会把这个来源永久挡在守卫上,而离线进程无权自清。
3. **熔断双轴** —— 处理 ≥10 节后,命令名整条否决率 >20% 或 args 保留率 <50% 就
   必须把 job 判失败并给用户可读理由,绝不静默产出近空目录。
4. **length/空 content 两条重试** —— 前者二分再问,后者重试一次后记账;两者都不许
   把「模型没给出可用回答」吞成「这一节本来就没有命令」。
5. **apply 的保守合并** —— 同名命令一律不改行,只回报 conflict。覆盖用户手工编辑
   过的内容是这批里唯一不可逆的破坏。

三条变异(熔断阈值恒不触发 / 删掉 BaseException 兜底 / 删掉 conflict 保护)都实测
过会让对应用例报红,记录在本任务的交付报告里。

C1b 双评审修复补充覆盖(同样都做过变异验证,见交付报告):

6. **飞行中取消不判 failed** —— 一次模型调用中途被取消(`AskCancelled`)必须落
   `cancelled`,不能掠过归类掉进 `except Exception` 判成「请稍后重试」。
7. **preview 形状信号吃全文** —— `detect_manual_shape` 必须看到文档全部节,不能被
   喂 `command_sections()` 自己分组后的子集(那会让 `total_sections` 恒等于命令节数、
   比率闸永远读不到否决空间)。
8. **apply 目标解析按 job 记住的 table_id** —— 表被改名后,同一个 job 的第二次
   apply 仍必须写回第一次落过的那张表,而不是按标题重新查找。
9. **apply 存在性检查有界** —— 不整表 hydrate 目标表,只按本页候选命令名做一次
   有界 IN 查询。
10. **模型自撰字段有上界** —— `description`/`examples` 是仅有的两个不接地校验的
    字段,必须在候选落库前截断,防一次回答把一行撑成不可控大小。
11. **reject_info 落库有界** —— 单个候选、以及跨片累积的合并候选,`reject_info`
    都必须封顶并如实报告溢出计数。

R1(codex PR #412 评审)修复补充覆盖(变异验证:改回违规形态确认对应用例报红,
记录在本任务的交付报告里):

12. **apply 目标锁身份归一** —— `_target_lock_key` 对「已知 applied_table_id 的
    job」与「同一目标首次 apply 的 job」必须解析到同一把锁,否则两把不互斥的锁
    各自通过存在性检查就会把同名命令写两遍。(R2 把这把锁从表 id 改成派生标题,
    R14 又把标题也拿掉——见下面第 18 条。)
13. **锚点列形状校验** —— 目标表必须恰好一列名为「命令」且其 `role == "anchor"`;
    第二个「命令」列、或锚点被移到别的列,都必须拒绝而不是写进非锚点/歧义列。

R7(codex PR #412 R7 评审 P1)修复补充覆盖(变异验证:改回违规形态确认对应用例
报红,见交付报告):

14. **dismiss 与 apply 共用同一把 per-target 锁** —— 二者都是「读哪些候选还是
    candidate、再写 state」的读后写序列,且都会调用同一个
    `mark_candidates_dismissed`;没有共用锁,一次跟在飞 apply 抢同一条候选的
    dismiss 可能在那条候选已经被写进目标表之后才抢到 `state` 列,把它标成
    `dismissed`(每个其他调用方都把这读成「从未写入」),而
    `mark_candidates_applied` 的 `WHERE state='candidate'` 守卫届时只会静默
    更新 0 行,不会报错。
15. **待审候选拦重跑的守卫必须真的可解除** —— dismiss 之后 pending 归零,
    同一来源必须能重新发起识别(不再 409)。这是 R5/R6 那句「请先确认或跳过」
    第一次有了对应的实现。

R10(codex PR #412 R10 评审 P1)修复补充覆盖(变异验证见交付报告):

16. **代次校验与 knowhow 落库必须在同一段持锁窗口内** —— R8 的代次守卫只在
    per-target 锁内,而 `replace_elements` 走的是 SourceIngestionService 自己的
    per-source 分块锁,两个写者从来不在同一把锁上:校验通过后、行落库前,重解析
    仍能提交,过期候选照样写进表。apply/dismiss 因此还要持有那把**来源侧**的锁
    (锁序:来源锁在外、目标锁在内)。有界等待超时时回一条与 stale **不同**的
    409,且**不过期**任何候选——解析可能在 `replace_elements` 之前就失败,那批
    候选仍然有效。

R14(codex PR #412 R14 评审 P1)修复补充覆盖(变异验证见交付报告):

17. **锁键必须是不可变身份** —— 这把锁先后用过三种身份:R1 的表 id(首次建表窗口
    内会变)、R2 的派生标题(论文元数据接地会异步把上传名换成论文标题)、现在的
    `("catalog", notebook_id)`。前两种都是并发写者能改的状态,改了就等于同一个目标
    上的两个写者各持一把互不互斥的锁。可达形状是**跨来源**的:来源写栅栏是 per
    source,只挡同一来源;两个派生标题相同的不同来源解析到同一张表,中间任何一侧
    被接地改名,R2 的键就劈开了。判据是「结构断言 + 并发计数」两条一起:只数并发
    不够(键劈开但恰好没撞上的一次也会绿),只看键也不够(键相同但锁没真拿也会绿)。
18. **锁序全景在一处成文** —— 只剩两把锁(来源栅栏在外、每库目录锁在内),完整
    枚举写在 `_target_lock_key` 的 docstring 里,`_apply_locked` 只指回去,避免同
    一份推演在多处各写一半、又各自过期。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import catalog_job
from app.repositories.ports import CatalogJobAlreadyRunning
from app.services.cancellation import AskCancelled
from app.services.catalog_job import (
    APPLY_TABLE_SHAPE_MESSAGE,
    CANCELLED_MESSAGE,
    INTERNAL_FAILURE_MESSAGE,
    CATALOG_TABLE_COLUMNS,
    CATALOG_TABLE_TITLE_PREFIX,
    CIRCUIT_OPEN_MESSAGE,
    INTERRUPTED_MESSAGE,
    MAX_APPLY_CANDIDATES,
    MAX_CALLS_PER_SLICE,
    MAX_MODEL_EXAMPLES,
    MODEL_ARG_DESC_CHARS,
    MODEL_ARG_DESC_TOTAL_CHARS,
    MODEL_DESCRIPTION_CHARS,
    MODEL_EXAMPLE_CHARS,
    MODEL_UNAVAILABLE_MESSAGE,
    SOURCE_BUSY_MESSAGE,
    SOURCE_NOT_PARSED_MESSAGE,
    SOURCE_PARSE_FAILED_MESSAGE,
    SOURCE_STALE_MESSAGE,
    CatalogModelUnavailable,
    CatalogPendingCandidates,
    CatalogSourceBusy,
    CatalogSourceChanged,
    pending_candidates_message,
)
from app.services.command_catalog import (
    MAX_SECTION_REJECTIONS,
    MIN_SECTIONS_BEFORE_ALERT,
)
from app.services.knowhow import api as knowhow_api
from app.services.model_work import ModelProviderError
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-07-31T00:00:00+08:00"


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    return instance


def _manual_elements(source_id: str, commands: int, *, params: int = 3) -> list[dict]:
    """A command-reference shaped source: one identifier heading per command,
    each carrying a usage line and a flag-bearing parameter list.

    Element ids are zero-padded and strictly increasing because the real
    element reader orders ``BY id`` and relies on that being insertion order
    (see ``ChunkStore.source_elements_for_chunking``). A fixture that numbers
    them any other way silently reorders the document — a body sorting before
    its own heading folds it into the PREVIOUS command's section, which reads
    like a grouping bug rather than a fixture bug.

    `params=0` means a genuinely FLAGLESS manual: no parameter list AND no flag
    on the usage line. The usage line used to carry `-density` even at
    `params=0`, which made "flagless" a fiction — `parameter_names` reads the
    whole section text, so those commands were assigned one parameter each and
    a model answering `args: []` was under-extracting, not answering correctly.
    That distinction only became observable once the args axis started counting
    assigned-but-unanswered parameters (R2 fix 1); before it, the fixture's
    quiet extra flag was invisible in both directions.
    """
    elements: list[dict] = []
    for index in range(commands):
        name = f"set_thing_{index}"
        flags = "\n".join(
            f"- `-{flag}` the {flag} option" for flag in _FLAGS[:params]
        )
        usage = f"{name} -{_FLAGS[0]} value" if params else f"{name} value"
        elements.append(
            {
                "id": f"el-{source_id}-{len(elements) + 1:04d}",
                "element_type": "heading",
                "text": name,
                "section_path": name,
            }
        )
        elements.append(
            {
                "id": f"el-{source_id}-{len(elements) + 1:04d}",
                "element_type": "paragraph",
                "text": f"{usage}\n\n{flags}",
                "section_path": name,
            }
        )
    return elements


_FLAGS = ("density", "pad_left", "pad_right", "layer", "site", "region")


def _add_elements(
    repo, notebook_id: str, source_id: str, title: str, elements: list[dict]
) -> list[dict]:
    """Insert a source and its (already id-assigned) elements. Shared by every
    fixture builder in this file so each one only has to describe its own
    element SHAPE, not repeat the INSERT plumbing."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "markdown", "extracted",
             "extracted", NOW, NOW),
        )
        for element in elements:
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    element["id"],
                    source_id,
                    element["element_type"],
                    "p1",
                    element["text"],
                    json.dumps({"section_path": element["section_path"]}),
                    NOW,
                ),
            )
    return elements


def _add_manual(repo, notebook_id: str, source_id: str, commands: int, **kwargs):
    elements = _manual_elements(source_id, commands, **kwargs)
    return _add_elements(repo, notebook_id, source_id, "OpenROAD 手册", elements)


def _mark_grounded_paper(repo, notebook_id: str, source_id: str, paper_title: str) -> None:
    """Attach a grounded ``source_paper_meta`` row the way paper-metadata
    extraction does, for R13's `source_display_title` table-naming tests —
    minimal columns, the rest keep their schema defaults (see
    `tests/test_collection_enumeration.py` for the same shape)."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_paper_meta (source_id,notebook_id,is_paper,"
            "paper_title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (source_id, notebook_id, 1, paper_title, NOW, NOW),
        )


def _numbered(source_id: str, raw: list[dict]) -> list[dict]:
    return [
        {**item, "id": f"el-{source_id}-{index + 1:04d}"}
        for index, item in enumerate(raw)
    ]


def _prose_and_commands(source_id: str, *, prose: int, commands: int) -> list[dict]:
    """`prose` plain (non-command-shaped) sections, followed by `commands`
    real command sections — for proving `detect_manual_shape` sees the WHOLE
    document's section count (`prose + commands`), not just the subset
    `command_sections()` would have grouped into commands."""
    raw: list[dict] = []
    for index in range(prose):
        title = f"背景第{index}节"
        raw.append(
            {"element_type": "heading", "text": title, "section_path": title}
        )
        raw.append(
            {
                "element_type": "paragraph",
                "text": "这是一段普通的说明文字，不涉及任何命令，仅用于充数。",
                "section_path": title,
            }
        )
    for index in range(commands):
        name = f"set_thing_{index}"
        raw.append(
            {"element_type": "heading", "text": name, "section_path": name}
        )
        raw.append(
            {
                "element_type": "paragraph",
                "text": f"{name} -density value\n\n- `-density` the density option",
                "section_path": name,
            }
        )
    return _numbered(source_id, raw)


def _large_param_manual(source_id: str, *, param_count: int) -> list[dict]:
    """One command whose parameter table is large enough to need multiple
    `extraction_slices` (`SLICE_PARAM_LIMIT` params per slice) — `_FLAGS` only
    has 6 real flag names, so a fixture needing more slices than that has to
    spell its own flag lines out rather than reuse `_manual_elements`."""
    name = "set_thing_0"
    flag_lines = "\n".join(f"- `-flag_{i}` a flag" for i in range(param_count))
    raw = [
        {"element_type": "heading", "text": name, "section_path": name},
        {
            "element_type": "paragraph",
            "text": f"{name} -flag_0 value\n\n{flag_lines}",
            "section_path": name,
        },
    ]
    return _numbered(source_id, raw)


def _positional_manual(source_id: str) -> list[dict]:
    """One FLAGLESS command with a positional argument, in the corpus's own
    words: OpenROAD `rsz` documents `set_dont_use lib_cells` under a prose
    heading, with the usage line alone in a code block and no parameter table
    anywhere. `_manual_elements(params=0)` is the same class but names itself
    `set_thing_N value`; this fixture keeps the real text because it is the
    shape the review found losing its argument metadata, and the one the
    prompt's no-flag branch names as its worked example.
    """
    path = "Gate Resizer > Commands > Set Don't Use"
    raw = [
        {"element_type": "heading", "text": "Set Don't Use", "section_path": path},
        {
            "element_type": "paragraph",
            "text": (
                "The `set_dont_use` command removes library cells from "
                "consideration by the `resizer` engine and the `CTS` engine."
            ),
            "section_path": path,
        },
        {
            "element_type": "code_block",
            "text": "set_dont_use lib_cells",
            "section_path": path,
        },
    ]
    return _numbered(source_id, raw)


class _Client:
    """A stub extraction model. ``answer`` maps a slice's requested command
    name to the reply; anything it returns is fed through the real grounding."""

    configured = True
    settings = None  # no ``settings`` -> cap_kwargs/response_validator stay off

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        return self.answer(prompt, len(self.calls))


def _command_of(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("- set_thing_"):
            return line[2:].strip()
    return ""


def _good_reply(prompt: str, _call: int) -> str:
    name = _command_of(prompt)
    return json.dumps(
        {
            "command_name": name,
            "syntax": f"{name} -density value",
            "description": "does a thing",
            "args": [{"name": f"-{flag}", "required": False, "desc": "", "default": ""}
                     for flag in _FLAGS[:3]],
            "examples": [f"{name} -density 0.6"],
        }
    )


def _service(repo):
    return repo.command_catalog


def _await_terminal(repo, job_id: str, *, timeout: float = 10.0) -> dict:
    """Wait for the route's background worker to settle its job row."""
    store = _service(repo).catalog
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"catalog job {job_id} never reached a terminal state")


# ------------------------------------------------------------------- migration
def test_fresh_database_has_both_catalog_tables_and_the_active_guard(repo):
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) >= 39
        names = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%catalog%'"
            ).fetchall()
        }
    assert {"catalog_jobs", "catalog_candidates"} <= names
    assert "idx_catalog_jobs_one_active" in names


def test_deployed_v38_database_gains_the_catalog_tables(tmp_path, monkeypatch):
    """A database already at v38 must have _migration_39 applied to it.

    The version gate short-circuits on `current >= SCHEMA_VERSION`, so DDL
    smuggled into a sealed migration would never run on a deployed database.
    Forging a v38 deployment is the only way to catch that.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'd.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    first = SQLiteRepository(Settings())
    first.close()
    with sqlite3.connect(tmp_path / "d.db") as db:
        db.executescript(
            "DROP TABLE IF EXISTS catalog_candidates;"
            "DROP TABLE IF EXISTS catalog_jobs;"
            "PRAGMA user_version = 38;"
        )
    second = SQLiteRepository(Settings())
    try:
        with second._connect() as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'catalog%'"
                ).fetchall()
            }
        assert {"catalog_jobs", "catalog_candidates"} <= names
    finally:
        second.close()


def test_single_flight_guard_covers_queued_as_well_as_running(repo):
    """The row exists before the worker starts. If the guard only covered
    `running`, a duplicate POST in that window would schedule a second writer
    for the same candidate set."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    store = repo._runtime.command_catalog.catalog
    job = store.create_job(notebook.id, "s1", "u")
    assert job["status"] == "queued"
    with pytest.raises(CatalogJobAlreadyRunning):
        store.create_job(notebook.id, "s1", "u")
    store.start_job(job["id"], 2)
    with pytest.raises(CatalogJobAlreadyRunning):
        store.create_job(notebook.id, "s1", "u")
    store.finish_job(job["id"], "succeeded")
    assert store.create_job(notebook.id, "s1", "u")["status"] == "queued"


def test_startup_sweep_settles_queued_and_running_catalog_jobs(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    _add_manual(repo, notebook.id, "s2", 2)
    store = repo._runtime.command_catalog.catalog
    queued = store.create_job(notebook.id, "s1", "u")
    running = store.create_job(notebook.id, "s2", "u")
    store.start_job(running["id"], 2)
    repo._recover_interrupted_jobs()
    assert store.get_job(queued["id"])["status"] == "failed"
    assert store.get_job(running["id"])["status"] == "failed"
    failure_reason = store.get_job(queued["id"])["failure_reason"]
    assert failure_reason
    # 措辞钉死: 与 INTERRUPTED_MESSAGE 同口径用「识别」,不是内部叫法「抽取」——这条
    # 启动兜底 SQL 字面量绕过 user_error()/前端词汇门,原样上屏。
    assert "抽取" not in failure_reason
    assert "识别" in failure_reason


# ------------------------------------------------------------------- happy path
def test_end_to_end_run_writes_candidates_and_advances_progress(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["sections_total"] == 3
    assert settled["sections_done"] == 3
    assert settled["entries"] == 3
    assert settled["uncovered"] == 0
    assert settled["finished_at"]

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == [
        "set_thing_0", "set_thing_1", "set_thing_2"
    ]
    first = page["items"][0]
    assert first["payload"]["syntax"] == "set_thing_0 -density value"
    assert [arg["name"] for arg in first["payload"]["args"]] == [
        "-density", "-pad_left", "-pad_right"
    ]
    assert first["payload"]["anchors"]
    assert first["payload"]["excerpt"]
    assert page["counts"]["candidate"] == 3


def test_one_planned_model_call_per_slice(repo):
    """The efficiency contract: calls == slices, no second opinion pass."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 4)
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])
    assert result["calls"] == len(client.calls) == 4


def test_rejected_entries_are_persisted_with_their_reasons(repo):
    """A run that grounds nothing must still be explainable. The rejected rows
    and their reject_info are the only evidence a person has for telling "the
    model went wrong" from "this source is not a manual"."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)

    def wrong_command(prompt, _call):
        return json.dumps({"command_name": "totally_other_command", "args": []})

    bind_chat_client(repo, "kg_extract", _Client(wrong_command))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="rejected", cursor=0, limit=50)
    assert len(page["items"]) == 2
    reasons = {
        entry["reason"]
        for row in page["items"]
        for entry in row["reject_info"]["fields"]
    }
    assert reasons == {"not_in_candidates"}
    assert page["items"][0]["command_name"] == "totally_other_command"
    assert service.catalog.get_job(job["id"])["rejected"] == 2


def test_reject_info_is_bounded_across_a_multi_slice_commands_slices(repo):
    """F1: a 200-parameter command needs 10 slices at `SLICE_PARAM_LIMIT`
    (20); if every slice invents its whole assignment, `_merge_entry`'s
    cross-slice accumulator would otherwise grow to 200 rejection records for
    ONE row. Both the in-memory accumulator and the final write must cap at
    `MAX_SECTION_REJECTIONS` and report the true overflow count rather than
    silently dropping it.

    Each slice contributes TWO ledgers' worth of records after R2 fix 1 — the
    20 invented names it returned, and the 20 assigned parameters it therefore
    never answered — so the true overflow is 400 records, not 200. They are
    not the same fact counted twice: one says "these values were rejected",
    the other says "these parameters are missing from the row", and a reviewer
    needs both. What matters here is that the ledger still caps and that the
    reported overflow is the real one."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=200),
    )

    def every_arg_invented(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt),
                "syntax": "",
                "description": "",
                "args": [
                    {
                        "name": f"-not_a_real_flag_{i}",
                        "required": False,
                        "desc": "",
                        "default": "",
                    }
                    for i in range(20)
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(every_arg_invented))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert len(page["items"]) == 1
    reject_info = page["items"][0]["reject_info"]
    assert len(reject_info["fields"]) <= MAX_SECTION_REJECTIONS
    assert len(reject_info["fields"]) == MAX_SECTION_REJECTIONS
    assert reject_info["overflow"] == 400 - MAX_SECTION_REJECTIONS
    # 10 slices × 20 invented names = 200 rejections; 10 slices × 20 assigned
    # parameters nothing answered for = 200 more.
    assert {entry["reason"] for entry in reject_info["fields"]} <= {
        "not_in_text", "arg_not_returned"
    }


def test_dropped_argument_is_recorded_on_the_accepted_candidate(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)

    def dash_stripped(prompt, _call):
        name = _command_of(prompt)
        return json.dumps(
            {
                "command_name": name,
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "density", "required": False, "desc": "", "default": ""},
                    {"name": "-pad_left", "required": False, "desc": "", "default": ""},
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(dash_stripped))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    row = page["items"][0]
    assert [arg["name"] for arg in row["payload"]["args"]] == ["-pad_left"]
    # `density` is charged as a dropped dash (the accurate diagnosis) and NOT
    # also as `-density` never being answered — a dash-insensitive claim match
    # keeps one mistake from being counted twice. `-pad_right` really was never
    # answered, so it is reported, which is R2 fix 1's whole point.
    assert [
        (entry["field"], entry["value"], entry["reason"])
        for entry in row["reject_info"]["fields"]
    ] == [
        ("arg", "density", "dash_stripped"),
        ("arg", "-pad_right", "arg_not_returned"),
    ]


def test_model_authored_description_and_examples_are_capped_before_the_write(repo):
    """M3: `description`/`examples` are the two fields `validate_entry` never
    grounds (prose cannot be matched verbatim), so nothing else stops a
    misbehaving model from writing an unbounded description or dozens of
    examples into one candidate row."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, params=0)

    def verbose(prompt, _call):
        name = _command_of(prompt)
        return json.dumps(
            {
                "command_name": name,
                "syntax": "",
                "description": "很长的说明。" * 300,
                "args": [],
                "examples": ["y" * 800] + [f"{name} -density {i}" for i in range(20)],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(verbose))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    payload = page["items"][0]["payload"]
    assert len(payload["description"]) == MODEL_DESCRIPTION_CHARS
    assert len(payload["examples"]) == MAX_MODEL_EXAMPLES
    assert all(len(example) <= MODEL_EXAMPLE_CHARS for example in payload["examples"])
    assert len(payload["examples"][0]) == MODEL_EXAMPLE_CHARS


def test_model_authored_argument_descriptions_are_capped_per_arg_and_per_row(repo):
    """R2 P2: `args[].desc` is the third ungrounded, model-authored field, and
    the only one with a multiplier in front of it — one candidate row carries
    as many descriptions as the command has parameters, so a per-field cap
    alone still lets a 200-parameter command write an unbounded row.

    Both bounds are pinned here, and so is the difference between them: the
    per-arg clip is routine and is NOT reported, while a description cut short
    because the ROW's budget ran out is a real loss and lands in
    `reject_info.desc_overflow`.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    per_row = MODEL_ARG_DESC_TOTAL_CHARS // MODEL_ARG_DESC_CHARS  # 20 args fit
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=per_row + 10),
    )

    def verbose_descriptions(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt) or "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {
                        "name": name,
                        "required": False,
                        # Deliberately longer than the per-arg cap.
                        "desc": "d" * (MODEL_ARG_DESC_CHARS + 500),
                        "default": "",
                    }
                    for name in _requested_params(prompt)
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(verbose_descriptions))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    row = page["items"][0]
    descriptions = [arg["desc"] for arg in row["payload"]["args"]]
    assert len(descriptions) == per_row + 10
    assert all(len(desc) <= MODEL_ARG_DESC_CHARS for desc in descriptions)
    assert descriptions[0] == "d" * MODEL_ARG_DESC_CHARS
    assert sum(len(desc) for desc in descriptions) == MODEL_ARG_DESC_TOTAL_CHARS
    # The 10 parameters past the row budget keep their grounded name/required/
    # default and lose only the prose — and the count of them is reported.
    assert descriptions[per_row:] == [""] * 10
    assert row["reject_info"]["desc_overflow"] == 10


# --------------------------------------------------- flagless commands (R4 P1)
def test_a_flagless_command_keeps_its_positional_argument(repo):
    """R4 P1: `parameter_names` is a FLAG scanner, so a command documented as
    `set_dont_use lib_cells` gets an empty assignment — and the prompt used to
    turn that into "return `args`: []", losing the argument metadata of every
    such command even though `_usage_identifier` accepts the shape as command
    evidence and `validate_entry` accepts a bare positional name.

    Both halves are asserted: the prompt has to ASK (a stub model would happily
    answer either way, so a payload-only assertion would go green on the old
    prompt), and the answer has to survive the real grounding.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "OpenROAD rsz", _positional_manual("s1"))

    def positional(_prompt, _call):
        return json.dumps(
            {
                "command_name": "set_dont_use",
                "syntax": "set_dont_use lib_cells",
                "description": "Removes library cells from consideration.",
                "args": [
                    {"name": "lib_cells", "required": True,
                     "desc": "Cells to exclude.", "default": ""}
                ],
                "examples": [],
            }
        )

    client = _Client(positional)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert len(client.calls) == 1
    prompt = client.calls[0]
    assert "POSITIONAL" in prompt
    assert "This command has no flag-shaped parameters" not in prompt

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    row = page["items"][0]
    assert row["command_name"] == "set_dont_use"
    assert [arg["name"] for arg in row["payload"]["args"]] == ["lib_cells"]
    assert row["reject_info"]["fields"] == []
    # No assignment means nothing can be uncovered — the keep ratio is decided
    # by grounding alone (see `assignment_coverage`).
    assert (result["args_seen"], result["args_kept"], result["args_uncovered"]) == (
        1, 1, 0,
    )


def test_a_flagless_commands_invented_argument_is_still_rejected(repo):
    """The exemption is from ATTRIBUTION, not from grounding. Asking for
    positional arguments where no list can be served would be a licence to
    invent if the verbatim check stopped applying — it does not."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "OpenROAD rsz", _positional_manual("s1"))

    def invented(_prompt, _call):
        return json.dumps(
            {
                "command_name": "set_dont_use",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "cell_list", "required": True, "desc": "",
                     "default": ""}
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(invented))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    row = page["items"][0]
    assert row["payload"]["args"] == []
    assert [
        (entry["field"], entry["value"], entry["reason"])
        for entry in row["reject_info"]["fields"]
    ] == [("arg", "cell_list", "not_in_text")]
    assert (result["args_seen"], result["args_kept"]) == (1, 0)


def test_the_cache_gate_refuses_a_flagless_slices_malformed_args_object(repo):
    """R9 P2: a flagless slice (`extraction.param_names == ()`) is exempt from
    the validator's args-axis clause by design — zero kept args is the
    correct, cacheable answer for a genuinely flagless command. But that
    exemption must not ALSO cover a reply whose `args` field is the wrong
    JSON shape entirely (an object instead of a list): `validate_entry`
    degrades that to zero kept args too (never fatal to the entry, by
    design), which is indistinguishable from the correct empty answer once it
    reaches the exemption. Without an independent structural check in the
    validator itself, a broken reply to a flagless slice would freeze into
    the content-addressed cache for the full TTL.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "OpenROAD rsz", _positional_manual("s1"))
    captured: list[object] = []

    class _Configured(_Client):
        settings = object()

        def chat_json(self, messages, schema_hint, **kwargs):
            captured.append(kwargs.get("response_validator"))
            return super().chat_json(messages, schema_hint, **kwargs)

    def positional(_prompt, _call):
        return json.dumps(
            {
                "command_name": "set_dont_use",
                "syntax": "set_dont_use lib_cells",
                "description": "Removes library cells from consideration.",
                "args": [
                    {"name": "lib_cells", "required": True,
                     "desc": "Cells to exclude.", "default": ""}
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Configured(positional))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    assert len(captured) == 1
    validator = captured[0]
    assert callable(validator)

    def reply(args_field) -> str:
        return json.dumps({"command_name": "set_dont_use", "args": args_field})

    # The correct flagless answer (no parameters at all) still admits.
    assert validator(reply([])) is True
    # A structurally malformed `args` must not be indistinguishable from that
    # correct empty answer.
    assert validator(reply({"name": "lib_cells"})) is False


# ------------------------------------------------------- slice assignment (R2)
def test_a_slice_that_answers_one_of_twenty_records_the_other_nineteen(repo):
    """R2 P1: the failure the review found. A slice assigned 20 parameters that
    comes back with 1 used to be a complete success — `args_kept >= 1` admitted
    it to the cache, `args_seen`/`args_kept` were both 1, and the run reported a
    100% parameter keep rate while 19 parameters silently vanished.

    The keep ratio's denominator now counts what was ASKED for, so a model can
    no longer raise it by answering less.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=20),
    )

    def one_of_twenty(prompt, _call):
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "-flag_0", "required": False, "desc": "", "default": ""}
                ],
                "examples": [],
            }
        )

    client = _Client(one_of_twenty)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    # One slice, so the coverage remedy fires once and both halves answer just
    # as narrowly — "still low, so record it honestly, and do not retry again".
    assert len(client.calls) == 3
    assert result["args_uncovered"] == 19
    assert result["args_kept"] == result["args_seen"] == 3  # one per payload
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    row = page["items"][0]
    assert [arg["name"] for arg in row["payload"]["args"]] == ["-flag_0"]
    missing = [
        entry["value"]
        for entry in row["reject_info"]["fields"]
        if entry["reason"] == "arg_not_returned"
    ]
    assert missing == [f"-flag_{i}" for i in range(1, 20)]


def test_parameters_belonging_to_another_slice_are_dropped_not_credited(repo):
    """R2 P1: a slice's answer is judged against ITS OWN assignment.

    Every parameter of a 40-parameter command appears in the same section text,
    so answering slice 0 with slice 1's parameters grounds perfectly — verbatim,
    dash and all. Without attribution they would be accepted, and the ledger
    would show a clean keep rate for a run in which slice 0's assignment was
    never touched.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=40),
    )
    other_slice = [f"-flag_{i}" for i in range(20, 40)]

    def answers_the_other_slice(prompt, _call):
        requested = _requested_params(prompt)
        # Slice 0 answers slice 1's parameters; slice 1 answers nothing, so a
        # name that survives can only have come from the wrong slice.
        args = other_slice if requested and requested[0] == "-flag_0" else []
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": name, "required": False, "desc": "", "default": ""}
                    for name in args
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(answers_the_other_slice))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    row = page["items"][0]
    assert row["payload"]["args"] == []
    assert result["args_kept"] == 0
    assert result["args_uncovered"] == 40
    outside = [
        entry["value"]
        for entry in row["reject_info"]["fields"]
        if entry["reason"] == "arg_outside_slice"
    ]
    assert outside == other_slice


def test_a_thin_answer_is_halved_and_re_asked_once(repo):
    """R2 P1: an intact but partial answer reuses the halving remedy — asking
    for fewer parameters at a time is the same fix for the same complaint (the
    answer did not fit the ask), and here both halves come back complete."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=8),
    )

    def thin_then_complete(prompt, call):
        requested = _requested_params(prompt)
        answered = ["-flag_0"] if call == 1 else requested
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": name, "required": False, "desc": "", "default": ""}
                    for name in answered
                ],
                "examples": [],
            }
        )

    client = _Client(thin_then_complete)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert len(client.calls) == 3  # the thin answer, then both halves
    assert _requested_params(client.calls[1]) == [f"-flag_{i}" for i in range(4)]
    assert _requested_params(client.calls[2]) == [f"-flag_{i}" for i in range(4, 8)]
    assert result["args_uncovered"] == 0
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [arg["name"] for arg in page["items"][0]["payload"]["args"]] == [
        f"-flag_{i}" for i in range(8)
    ]


def test_a_coverage_retry_stays_inside_the_same_call_bound(repo):
    """The coverage remedy must not raise `MAX_CALLS_PER_SLICE`.

    It can only fire at depth 0, and at depth 0 the malformed remedy is
    mutually exclusive with it (that one fires when there is no payload, this
    one when there is), so both paths cost `1 + 2·f(1)`. Worst case here: a
    thin first answer triggers the halving, and every half then goes malformed
    all the way down to exhaustion — which lands on exactly the number the
    malformed-only worst case already pins.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, params=6)

    def thin_then_unusable(prompt, call):
        if call > 1:
            return '{"command_name": "set_thing_0", "args": [{"na'
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "-density", "required": False, "desc": "", "default": ""}
                ],
                "examples": [],
            }
        )

    client = _Client(thin_then_unusable)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    assert len(client.calls) == MAX_CALLS_PER_SLICE
    assert service.catalog.get_job(job["id"])["status"] == "succeeded"


def test_a_short_assignment_is_not_worth_a_coverage_retry(repo):
    """`MIN_ASSIGNED_FOR_COVERAGE_RETRY`: one parameter answered out of three
    is a coin flip, not a truncation signal, and there is nothing to halve
    into. The remedy must not become a general-purpose retry."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, params=3)

    def one_of_three(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt),
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "-density", "required": False, "desc": "", "default": ""}
                ],
                "examples": [],
            }
        )

    client = _Client(one_of_three)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])
    assert len(client.calls) == 1
    assert result["args_uncovered"] == 2  # still recorded, just not re-asked


def test_a_complete_but_wrong_answer_is_not_re_asked(repo):
    """The cost gate on the coverage remedy: it fires only on a SHORT answer.

    A model that returned as many parameters as it was assigned answered the
    whole ask and simply got the names wrong; halving buys a second wrong
    answer at full price, and the breaker's args axis is what that case is for.
    Without this clause the pathological "every parameter invented" run would
    triple its model spend on the way to being rejected anyway.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=8),
    )

    def all_invented(prompt, _call):
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {
                        "name": f"-not_a_real_flag_{i}",
                        "required": False,
                        "desc": "",
                        "default": "",
                    }
                    for i in range(8)
                ],
                "examples": [],
            }
        )

    client = _Client(all_invented)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])
    assert len(client.calls) == 1
    assert result["args_kept"] == 0
    assert result["args_uncovered"] == 8


def test_the_cache_gate_refuses_a_reply_about_another_slices_parameters(repo):
    """R2 P1: `response_validator` is the sole admission ticket into the
    content-addressed cache, so a reply that answers the WRONG slice must not
    pass it — freezing that reply for the whole TTL would serve it back on
    every later hit of a prompt it never answered. Every name in it grounds
    verbatim against the section, so only attribution can refuse it.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=40),
    )
    captured: list[tuple[list[str], object]] = []

    class _Configured(_Client):
        settings = object()

        def chat_json(self, messages, schema_hint, **kwargs):
            captured.append(
                (
                    _requested_params(messages[0]["content"]),
                    kwargs.get("response_validator"),
                )
            )
            return super().chat_json(messages, schema_hint, **kwargs)

    def answer_own_slice(prompt, _call):
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": name, "required": False, "desc": "", "default": ""}
                    for name in _requested_params(prompt)
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Configured(answer_own_slice))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    first_slice = next(
        validator for params, validator in captured if params[0] == "-flag_0"
    )
    assert callable(first_slice)

    def reply(names: list[str]) -> str:
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "args": [
                    {"name": name, "required": False, "desc": "", "default": ""}
                    for name in names
                ],
            }
        )

    assert first_slice(reply(["-flag_0"])) is True
    # Grounded verbatim in the same section, but assigned to slice 1.
    assert first_slice(reply(["-flag_25", "-flag_26"])) is False


def test_a_settings_bearing_client_receives_the_cache_admission_validator(repo):
    """`response_validator` is the SOLE admission ticket into the content-
    addressed cache (read AND write) — nothing to do with retrying a
    malformed reply, which is `_extract_slice`'s own halving logic. Without
    it a slice is not merely uncached, every retry repays the full model
    cost. The ordinary stub in this file has
    `settings = None` (mirroring the hand-rolled doubles the KG extractors keep
    working), which means the branch that passes the validator is never
    executed by any other test here: deleting those two lines would silently
    drop the whole cache-admission guarantee and stay green.

    This stub therefore carries `settings`, asserts the kwarg arrives, and runs
    the validator it was handed to prove it judges by real grounding rather
    than by shape."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    seen: dict = {}

    class _Configured(_Client):
        settings = object()  # only presence is tested, exactly like kg/extract

        def chat_json(self, messages, schema_hint, **kwargs):
            seen.update(kwargs)
            return super().chat_json(messages, schema_hint, **kwargs)

    client = _Configured(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    validator = seen.get("response_validator")
    assert callable(validator), sorted(seen)
    assert "cancel_event" in seen
    # It judges by the REAL grounding, not by shape: the same reply the run just
    # accepted is admissible, and one naming a command this section does not
    # document is refused — which is what keeps a laxer-era cached value from
    # being served on a later hit.
    assert validator(_good_reply(client.calls[-1], 1)) is True
    assert validator(json.dumps({"command_name": "nope", "args": []})) is False
    assert validator("not json") is False
    assert service.catalog.get_job(job["id"])["status"] == "succeeded"


# ---------------------------------------------------------------- length/empty
def test_the_provider_error_the_scheduled_adapter_actually_raises_is_handled(repo):
    """The one hole a green suite hides.

    The raw client raises `MalformedModelResponse`, but nothing downstream ever
    sees that type: `ScheduledJsonChatClient._resolve` re-raises everything as
    `ModelInvocationError`, a SIBLING of it under `ModelProviderError`. A
    handler written against the raw type therefore matches only a test double
    and never production — where the first budget-truncated reply would instead
    fail the whole job, throwing away every section already paid for. So this
    double raises the PRODUCTION-shaped error, and asserts the halving remedy
    actually runs.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, params=4)
    seen: list[tuple[str, ...]] = []

    def truncated_then_good(prompt, call):
        seen.append(tuple(_requested_params(prompt)))
        if call == 1:
            raise ModelProviderError("truncated", code="malformed_response")
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": name, "required": False, "desc": "", "default": ""}
                    for name in _requested_params(prompt)
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(truncated_then_good))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    assert len(seen) == 3, seen  # one full attempt, then both halves
    assert seen[1] == ("-density", "-pad_left")
    assert seen[2] == ("-pad_right", "-layer")
    assert service.catalog.get_job(job["id"])["status"] == "succeeded"


def test_a_transient_provider_failure_fails_the_job_instead_of_halving(repo):
    """A rate limit or an upstream 5xx is not a too-long answer. The raw client
    already retried it with backoff; asking for fewer parameters cannot help,
    and swallowing it would turn an outage into "this section had no commands"
    — the silent-empty outcome this feature exists to prevent."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)

    def rate_limited(prompt, call):
        raise ModelProviderError("slow down", code="provider_rate_limited")

    client = _Client(rate_limited)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    with pytest.raises(ModelProviderError):
        service.run(job["id"])
    assert len(client.calls) == 1  # no halving, no retry storm
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == INTERNAL_FAILURE_MESSAGE
    assert service.catalog.active_job("s1") is None


def test_unparseable_reply_halves_the_slice_and_retries(repo):
    """C0's measured failure: a big parameter list overruns the output budget.
    The remedy is asking for fewer parameters, and it must be visible as extra
    calls carrying a strict subset of the original assignment."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, params=4)
    seen: list[tuple[str, ...]] = []

    def truncated_then_good(prompt, call):
        seen.append(tuple(_requested_params(prompt)))
        if call == 1:
            return '{"command_name": "set_thing_0", "args": [{"name": "-den'
        return json.dumps(
            {
                "command_name": "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {"name": name, "required": False, "desc": "", "default": ""}
                    for name in _requested_params(prompt)
                ],
                "examples": [],
            }
        )

    client = _Client(truncated_then_good)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    assert len(seen) == 3, seen  # one full attempt, then both halves
    assert seen[0] == ("-density", "-pad_left", "-pad_right", "-layer")
    assert seen[1] == ("-density", "-pad_left")
    assert seen[2] == ("-pad_right", "-layer")
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert len(page["items"]) == 1
    assert [arg["name"] for arg in page["items"][0]["payload"]["args"]] == [
        "-density", "-pad_left", "-pad_right", "-layer"
    ]
    assert service.catalog.get_job(job["id"])["status"] == "succeeded"


def test_a_slice_that_never_answers_stays_within_its_call_bound(repo):
    """The halving remedy multiplies rather than halving the cost: both halves
    are re-asked. `MAX_CALLS_PER_SLICE` spells out that worst case, and this
    pins it so a future depth increase cannot quietly change the arithmetic."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, params=6)
    client = _Client(lambda prompt, call: '{"command_name": "set_thing_0", "args": [{"na')
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    assert len(client.calls) == MAX_CALLS_PER_SLICE
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == 0
    assert settled["rejected"] > 0  # every failed slice is recorded, never silent


def _requested_params(prompt: str) -> list[str]:
    lines = prompt.splitlines()
    try:
        start = lines.index("Extract ONLY these parameters, and nothing else:")
    except ValueError:
        return []
    out = []
    for line in lines[start + 1:]:
        if not line.startswith("- -"):
            break
        out.append(line[2:].strip())
    return out


def test_empty_content_is_retried_once_then_recorded_as_a_failed_slice(repo):
    """Silent data loss is the failure mode this closes: an empty reply must
    become a visible rejected row, never a section that "had no commands"."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    client = _Client(lambda prompt, call: "")
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    assert len(client.calls) == 2  # the call plus exactly one retry
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == 0
    assert settled["rejected"] == 1
    page = service.candidates_page(job["id"], state="rejected", cursor=0, limit=50)
    assert [entry["reason"] for entry in page["items"][0]["reject_info"]["fields"]] == [
        "model_response_unusable"
    ]


# -------------------------------------------------------------- circuit breaker
def test_circuit_opens_on_the_command_name_axis(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_SECTIONS_BEFORE_ALERT + 2)
    bind_chat_client(
        repo,
        "kg_extract",
        _Client(lambda prompt, call: json.dumps(
            {"command_name": "not_a_documented_command", "args": []}
        )),
    )
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == CIRCUIT_OPEN_MESSAGE
    assert json.loads(settled["diagnostic"])["axis"] == "command_name"
    # Stopped AT the threshold rather than running the whole manual.
    assert settled["sections_done"] == MIN_SECTIONS_BEFORE_ALERT


def test_circuit_opens_on_the_args_axis_even_with_clean_command_names(repo):
    """The second axis is not redundant: every command name is right here and
    every parameter is invented, which the name axis cannot see."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_SECTIONS_BEFORE_ALERT + 2)

    def right_name_invented_args(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt),
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "-invented_a", "required": False, "desc": "", "default": ""},
                    {"name": "-invented_b", "required": False, "desc": "", "default": ""},
                    {"name": "-density", "required": False, "desc": "", "default": ""},
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(right_name_invented_args))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == CIRCUIT_OPEN_MESSAGE
    assert json.loads(settled["diagnostic"])["axis"] == "args"


def test_circuit_opens_when_the_model_never_returns_anything_usable(repo):
    """The third axis, and the one the other two are structurally blind to.

    A slice that never answers contributes no entry and no arg, so
    `entries_seen` and `args_seen` both stay 0 and both published ratios stay
    innocuous. Without this axis, a deployment pointed at an incompatible model
    endpoint runs the entire manual — every section, every halving retry — and
    then reports `succeeded` with an empty catalog.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_SECTIONS_BEFORE_ALERT + 5)
    client = _Client(lambda prompt, call: "not json at all")
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == CIRCUIT_OPEN_MESSAGE
    assert json.loads(settled["diagnostic"])["axis"] == "unusable_response"
    # Stopped AT the threshold instead of paying for the whole manual.
    assert settled["sections_done"] == MIN_SECTIONS_BEFORE_ALERT


def test_the_args_axis_counts_what_was_asked_for_not_only_what_came_back(repo):
    """R2 P1: the keep ratio's denominator is `args_seen + args_uncovered`.

    Every parameter this model returns is correct, so on the old denominator
    the run scores a perfect 1.0 keep rate — while answering one of six
    assigned parameters in every section. A ratio a model can raise by
    answering LESS cannot detect the failure it exists to detect, and this run
    (12 sections of 1/6) must open the breaker on the args axis.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_SECTIONS_BEFORE_ALERT + 2, params=6)

    def one_correct_arg(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt),
                "syntax": "",
                "description": "",
                "args": [
                    {"name": "-density", "required": False, "desc": "", "default": ""}
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(one_correct_arg))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == CIRCUIT_OPEN_MESSAGE
    diagnostic = json.loads(settled["diagnostic"])
    assert diagnostic["axis"] == "args"
    # Nothing the model returned was ever rejected — the axis fired purely on
    # what it never answered.
    assert diagnostic["args_kept"] == diagnostic["args_seen"]
    assert diagnostic["args_uncovered"] > 0


def test_a_flagless_manual_does_not_trip_the_args_axis(repo):
    """``args_seen > 0`` is a real guard: catalog_stats reports an args-keep
    ratio of 0.0 when nothing was seen, so without it a manual of flagless
    commands would fail on its tenth clean section."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_SECTIONS_BEFORE_ALERT + 2, params=0)

    def no_args(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt),
                "syntax": "",
                "description": "does a thing",
                "args": [],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(no_args))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == MIN_SECTIONS_BEFORE_ALERT + 2


def test_a_slice_half_rescued_by_halving_does_not_count_as_a_slice_failure(repo):
    """The third circuit-breaker axis is defined as "this slice produced NO
    usable payload at all" — not "something along the way went wrong". After
    a halving, ONE successful half must be enough for the WHOLE original
    slice to not count against `SLICE_FAILURE_ALERT_RATIO`, even though its
    sibling half never recovers. Every section here has exactly this shape
    (one half always answers, the other never does), so under the OLD "OR the
    two halves' `failed`" semantics every section would count as a slice
    failure and the breaker would trip at the tenth section; under the fixed
    semantics it must not trip at all."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_SECTIONS_BEFORE_ALERT + 2, params=4)
    good_half = {"-density", "-pad_left"}
    whole_slice = {"-density", "-pad_left", "-pad_right", "-layer"}

    def half_succeeds_half_fails(prompt, _call):
        requested = set(_requested_params(prompt))
        name = _command_of(prompt)
        if requested in (whole_slice, good_half):
            if requested == good_half:
                return json.dumps(
                    {
                        "command_name": name,
                        "syntax": "",
                        "description": "",
                        "args": [
                            {"name": flag, "required": False, "desc": "", "default": ""}
                            for flag in sorted(good_half)
                        ],
                        "examples": [],
                    }
                )
            # The undivided slice (all four params): force the halving remedy.
            return '{"command_name": "' + name + '", "args": [{"na'
        # The OTHER half (-pad_right/-layer) and every sub-slice it further
        # halves into: never usable, all the way down to exhaustion.
        return '{"command_name": "' + name + '", "args": [{"na'

    bind_chat_client(repo, "kg_extract", _Client(half_succeeds_half_fails))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == MIN_SECTIONS_BEFORE_ALERT + 2

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert len(page["items"]) == MIN_SECTIONS_BEFORE_ALERT + 2
    for row in page["items"]:
        assert {arg["name"] for arg in row["payload"]["args"]} == good_half


# ------------------------------------------------------------- terminal states
def test_cancel_between_slices_settles_the_row(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 5)
    service = _service(repo)

    def cancel_after_first(prompt, call):
        if call == 1:
            service.cancel("s1")
        return _good_reply(prompt, call)

    bind_chat_client(repo, "kg_extract", _Client(cancel_after_first))
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "cancelled"
    assert settled["finished_at"]
    assert settled["sections_done"] < 5
    # Guard released, but R6 P1 widened `_reject_if_pending_candidates` to
    # every terminal status: the section that finished before the cancel
    # landed left a real, unreviewed candidate row behind. Clear it the same
    # way a real reviewer would, so this test's second run exercises what it
    # is actually about (the guard releasing after settlement), not the
    # unrelated pending-candidates guard.
    service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="tester")
    assert service.start(notebook.id, "s1")["status"] == "queued"


def test_cancel_while_a_model_call_is_in_flight_settles_cancelled_not_failed(repo):
    """B1: `chat_json`'s OWN `raise_if_cancelled` (guarding queue admission and
    completion — see `core/llm.py`) raises `AskCancelled`, a SIBLING of
    `ModelProviderError`, not a subclass. `_call` used to only catch
    `ModelProviderError`, so this escaped uncaught, past `except
    CatalogCancelled` in `run()`, and landed in `except Exception` — settling
    the row `failed` with `INTERNAL_FAILURE_MESSAGE` and re-raising into
    `background_jobs`' error log for a cancel the OWNER asked for. This double
    reproduces the PRODUCTION shape: it carries `settings` (so `_call` passes
    `cancel_event`) and raises `AskCancelled` itself from inside the call,
    exactly like the real scheduled client's own cancellation check does.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service = _service(repo)

    class _CancelsMidCall(_Client):
        settings = object()

        def chat_json(self, messages, schema_hint, **kwargs):
            cancel_event = kwargs.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
                raise AskCancelled()
            return super().chat_json(messages, schema_hint, **kwargs)

    bind_chat_client(repo, "kg_extract", _CancelsMidCall(_good_reply))
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])
    assert result.get("cancelled") is True
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "cancelled"
    assert settled["finished_at"]
    # Guard released, and no exception escaped `run()` into background_jobs.
    assert service.start(notebook.id, "s1")["status"] == "queued"


def test_cancel_immediately_after_start_skips_the_whole_source_read(repo):
    """M4: `start_job` claims the row (queued -> running) but is not itself a
    cancellation check. Without an explicit one right after it, a cancel that
    lands in the instant between `cancel()` setting the event and this thread
    reaching here is invisible until the FIRST per-slice check — by which
    point the whole-source element read has already run for nothing."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    assert service.cancel("s1")["status"] == "cancelling"

    def must_not_be_called(source_id):
        raise AssertionError(
            "source_elements_for_chunking must not run after an early cancel"
        )

    service.chunks.source_elements_for_chunking = must_not_be_called
    service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "cancelled"


def test_base_exception_still_settles_the_row(repo):
    """KeyboardInterrupt/SystemExit inherit BaseException and never reach
    `except Exception`. A row left running holds this source's single-flight
    guard until the next backend restart, and an offline process has no
    standing to clean it up."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)

    def interrupt(prompt, call):
        raise KeyboardInterrupt()

    bind_chat_client(repo, "kg_extract", _Client(interrupt))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    with pytest.raises(KeyboardInterrupt):
        service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == INTERRUPTED_MESSAGE
    assert settled["finished_at"]
    assert service.catalog.active_job("s1") is None


def test_internal_failure_settles_the_row_and_re_raises(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)

    def boom(prompt, call):
        raise RuntimeError("upstream exploded")

    bind_chat_client(repo, "kg_extract", _Client(boom))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    with pytest.raises(RuntimeError):
        service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["diagnostic"] == "internal_error"
    assert service.catalog.active_job("s1") is None


def test_failed_submission_releases_the_guard(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    assert service.fail_submission(job["id"]) is True
    assert service.catalog.active_job("s1") is None


def test_start_requires_a_configured_extraction_model(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    with pytest.raises(CatalogModelUnavailable):
        _service(repo).start(notebook.id, "s1")
    assert _service(repo).catalog.active_job("s1") is None


# --------------------------------------------------------------- service scope
def test_service_methods_reject_a_source_from_another_notebook(repo):
    """The routes guard this too. The service re-checks anyway: every public
    method here takes a caller-supplied source_id, and being safe only because
    of what the current caller happens to do is one refactor from not safe."""
    owner = repo.create_notebook(NotebookCreate(name="a"))
    other = repo.create_notebook(NotebookCreate(name="b"))
    _add_manual(repo, owner.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    for call in (
        lambda: service.preview(other.id, "s1"),
        lambda: service.start(other.id, "s1"),
    ):
        with pytest.raises(KeyError):
            call()
    assert service.catalog.active_job("s1") is None

    job = service.start(owner.id, "s1")
    service.run(job["id"])
    assert service.scoped_job(other.id, "s1", job["id"]) is None
    with pytest.raises(KeyError):
        service.apply(other.id, "s1", job["id"], all_pending=True, actor="t")


# --------------------------------------------------------------------- preview
def test_preview_is_bounded_and_declares_when_it_sampled(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 6)
    service = _service(repo)
    preview = service.preview(notebook.id, "s1")
    assert preview.estimated_sections == 6
    assert preview.estimated_calls == 6
    assert preview.signal["command_shaped_sections"] == 6
    assert preview.signal["total_sections"] == 6
    # OpenROAD-manual shape (six clean identifier headings, no prose) still
    # reads as a manual after B2's fix — this is the non-regression half of
    # that fix; the discriminating half is the mixed-shape test below.
    assert preview.signal["is_manual"] is True
    assert preview.sampled is False

    monkeypatch.setattr("app.services.catalog_job.PREVIEW_ELEMENT_LIMIT", 4)
    clipped = service.preview(notebook.id, "s1")
    assert clipped.sampled is True
    assert clipped.estimated_sections < 6


def test_preview_shape_signal_sees_every_section_not_just_command_sections(repo):
    """B2: `detect_manual_shape` used to be fed `command_sections()`'s own
    output — that function already GROUPS blocks under a command heading and
    DROPS everything that never joins one, so `total_sections` came out
    trivially equal to `command_shaped_sections` and `command_ratio` was
    always 1.0. A prose document with a handful of real commands buried in it
    (206 total sections, only 6 of them commands) must score `is_manual=False`
    and report the TRUE section count — not the post-grouping subset."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "混合文档",
        _prose_and_commands("s1", prose=200, commands=6),
    )
    service = _service(repo)
    preview = service.preview(notebook.id, "s1")
    assert preview.signal["total_sections"] == 206
    assert preview.signal["command_shaped_sections"] == 6
    assert preview.signal["is_manual"] is False
    # estimated_* still comes from command_sections() — "how many commands
    # would be extracted" is exactly what a cost estimate needs, unaffected
    # by this fix.
    assert preview.estimated_sections == 6


def test_preview_declares_sampled_when_an_element_was_clipped(repo, monkeypatch):
    """Row cap and per-element clipping are two different bounds, and the
    second distorts harder: a clipped options table loses parameter names,
    which loses slices, so the estimate comes back several times too low. If
    only the row cap fed `sampled`, that estimate would be presented as a
    census."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2, params=6)
    service = _service(repo)
    assert service.preview(notebook.id, "s1").sampled is False

    monkeypatch.setattr("app.services.catalog_job.PREVIEW_ELEMENT_CHARS", 20)
    clipped = service.preview(notebook.id, "s1")
    assert clipped.sampled is True
    # The row cap was never reached — only the character bound bit.
    assert clipped.estimated_sections <= 2


def test_candidates_are_never_dropped_when_a_section_exceeds_one_insert_batch(repo):
    """The store CHUNKS its inserts; it must not truncate them. The caller has
    already counted these rows into the job's progress, so a dropped tail is the
    exact "registered under-report" this feature exists to eliminate."""
    from app.repositories.ports import CATALOG_MAX_CANDIDATE_BATCH

    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    store = _service(repo).catalog
    job = store.create_job(notebook.id, "s1", "u")
    oversized = CATALOG_MAX_CANDIDATE_BATCH + 7
    store.add_candidates([
        {
            "job_id": job["id"],
            "notebook_id": notebook.id,
            "source_id": "s1",
            "position": index + 1,
            "command_name": f"cmd_{index}",
        }
        for index in range(oversized)
    ])
    assert store.candidate_counts(job["id"])["candidate"] == oversized


def test_truncated_sections_is_counted_and_reaches_the_job_row(repo):
    """C1a already computes `SectionOutcome.truncation.applied`; this only
    checks C1b actually PLUMBS it through `record_section` into the job row
    (and from there, `CommandCatalogJob.of()` into the API response — see the
    API test) rather than leaving it computed and unused."""
    from app.services.command_catalog import MAX_SECTION_CHARS

    notebook = repo.create_notebook(NotebookCreate(name="n"))
    name = "set_thing_0"
    oversized_paragraph = f"{name} -density value\n\n" + "x" * (MAX_SECTION_CHARS + 500)
    raw = [
        {"element_type": "heading", "text": name, "section_path": name},
        {"element_type": "paragraph", "text": oversized_paragraph, "section_path": name},
    ]
    _add_elements(repo, notebook.id, "s1", "超长手册", _numbered("s1", raw))
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["truncated_sections"] == 1


def test_cancel_reports_the_row_not_the_branch_it_took(repo):
    """The worker can settle between the lookup and the return. Answering
    "cancelling" next to `status: succeeded` is a contradiction the caller
    cannot resolve, so the status is derived from the re-read row."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service = _service(repo)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    job = service.start(notebook.id, "s1")
    service.run(job["id"])  # settles as succeeded, guard released
    assert service.cancel("s1")["status"] == "not_running"
    # R5 P2: a second start on a SUCCEEDED job with unreviewed candidates is
    # now blocked (`CatalogPendingCandidates`) — clear them the same way a
    # real reviewer would, so this test's second run exercises what it is
    # actually about (cancel's dual `queued`/`running` reporting), not that
    # unrelated guard.
    service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="tester")

    second = service.start(notebook.id, "s1")
    result = service.cancel("s1")
    assert result["status"] == "cancelling"
    assert result["job"]["status"] in {"queued", "running"}
    service.run(second["id"])
    assert service.catalog.get_job(second["id"])["status"] == "cancelled"


# ----------------------------------------------------------------------- apply
def _run_ok(repo, notebook_id, source_id, commands):
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook_id, source_id)
    service.run(job["id"])
    return service, job


def test_apply_creates_the_table_and_records_a_change_entry(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    result = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert result["created"] is True
    assert result["rows_added"] == 2
    assert result["conflicts"] == []

    table = repo.get_knowhow_table(result["table_id"])
    assert table["title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"
    assert [column["name"] for column in table["columns"]] == [
        "命令", "语法", "参数", "说明", "示例", "出处"
    ]
    assert table["columns"][0]["role"] == "anchor"
    anchor = table["columns"][0]["id"]
    provenance = table["columns"][5]["id"]
    assert {row["cells"][anchor] for row in table["rows"]} == {
        "set_thing_0", "set_thing_1"
    }
    assert "OpenROAD 手册" in table["rows"][0]["cells"][provenance]

    # Every knowhow write path appends a change entry in its own transaction;
    # going through the service layer is what buys that for free.
    kinds = [entry["kind"] for entry in repo.list_knowhow_changes(result["table_id"])]
    assert "table_create" in kinds
    assert "import_append" in kinds

    # Candidates are marked applied, so a second all_pending apply is a no-op.
    again = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert again["rows_added"] == 0
    assert again["created"] is False
    assert again["table_id"] == result["table_id"]


# R13 (codex PR #412 评审第 13 轮 P2) 修复补充覆盖:目标表名/提要标题改走
# `source_display.source_display_title` 这个单一真源,不再用来源原始上传标题
# (`sources.title`)绕过它——已接地判定为论文且解析出非空 `paper_title` 的
# 来源,引用卡/证据卡/清单卡处处显示论文标题,目录表名不能是唯一的例外。
def test_apply_table_name_uses_the_grounded_paper_title_not_the_upload_name(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    _mark_grounded_paper(repo, notebook.id, "s1", "A Grounded Paper Title")
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    result = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table = repo.get_knowhow_table(result["table_id"])
    assert table["title"] == f"{CATALOG_TABLE_TITLE_PREFIX}A Grounded Paper Title"
    provenance = table["columns"][5]["id"]
    assert "A Grounded Paper Title" in table["rows"][0]["cells"][provenance]
    assert "OpenROAD 手册" not in table["rows"][0]["cells"][provenance]


def test_preview_source_title_uses_the_grounded_paper_title(repo):
    """`preview` shows the source's canonical name in its cost estimate, the
    same one the eventual apply would name the target table after — a
    grounded paper must not be called two different things across the two
    calls of the same feature."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    _mark_grounded_paper(repo, notebook.id, "s1", "A Grounded Paper Title")
    preview = _service(repo).preview(notebook.id, "s1")
    assert preview.source_title == "A Grounded Paper Title"


def test_apply_table_name_ignores_a_non_paper_or_ungrounded_source(repo):
    """A stale/ungrounded `is_paper=0` row (or no row at all) must not
    surface a title — the ordinary source name keeps naming the table,
    matching `source_display_title`'s own precedence rule."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_paper_meta (source_id,notebook_id,is_paper,"
            "paper_title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("s1", notebook.id, 0, "Stale title from a rejected extraction",
             NOW, NOW),
        )
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    result = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table = repo.get_knowhow_table(result["table_id"])
    assert table["title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"


def test_apply_table_name_snapshot_survives_a_paper_title_backfill_mid_job(repo):
    """The SAME job's second apply must keep writing to the table its first
    apply created, even though a paper-metadata backfill lands in between and
    changes what `_display_source_title` would now return.

    This is `_resolve_target_table`'s existing `applied_table_id`-first
    resolution (see its docstring) carrying a NEW kind of drift it was never
    exercised against before: `sources.title` is immutable for the life of a
    source, so before this fix the derived title could never actually change
    between two applies of one job. A canonical title CAN — paper-metadata
    extraction runs asynchronously and can complete after the catalog job's
    first apply already created the table. The job's own remembered
    `applied_table_id` is what keeps that from splitting the table in two;
    this proves it actually does for the NEW kind of drift too.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    first_id, second_id = page["items"][0]["id"], page["items"][1]["id"]

    first = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[first_id], actor="tester"
    )
    assert first["created"] is True
    table_id = first["table_id"]
    assert repo.get_knowhow_table(table_id)["title"] == (
        f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"
    )

    _mark_grounded_paper(repo, notebook.id, "s1", "A Later-Grounded Title")

    second = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[second_id], actor="tester"
    )
    assert second["created"] is False
    assert second["table_id"] == table_id

    # Exactly one command-catalog table exists for this source — the
    # post-backfill canonical title never spawned a second one.
    titles = {t["title"] for t in repo.list_knowhow_tables(notebook.id)}
    assert titles == {f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"}

    # The provenance cell written by the SECOND apply reflects what the
    # source was canonically called AT THAT MOMENT — a descriptive detail,
    # not a table identity, so it is free to pick up the new title even
    # though the table itself did not move.
    table = repo.get_knowhow_table(table_id)
    provenance = table["columns"][5]["id"]
    by_anchor = {
        row["cells"][table["columns"][0]["id"]]: row["cells"][provenance]
        for row in table["rows"]
    }
    assert "OpenROAD 手册" in by_anchor["set_thing_0"]
    assert "A Later-Grounded Title" in by_anchor["set_thing_1"]


def test_start_stale_sweep_lock_key_matches_apply_after_a_paper_title_backfill(repo):
    """`start`'s stale-candidate sweep (`_reject_if_pending_candidates`) takes
    the SAME catalog lock `apply`/`dismiss` take (see `_target_lock_key`'s
    docstring: "every writer that could collide computes the same key").

    Before R14 this test guarded a much more fragile property: the two call
    sites both had to resolve the canonical title through
    `_display_source_title`, or a paper-metadata backfill would silently split
    them onto two lock keys. R14 removed the hazard rather than re-checking it
    — the key is the notebook id, which `start` is handed directly — so the
    backfill here now proves the STRONGER statement: the sweep's key is
    unaffected by grounding that changes what the source is called.

    This drives the actual `start()` code path (not just the pure helper) by
    spying on `_target_lock_key`: a reparse after an unreviewed job leaves the
    stale-sweep branch of `_reject_if_pending_candidates` the only thing that
    calls it during `start`. The spy takes `*args` so it stays honest about
    WHAT the production call passes — a version that went back to feeding the
    key a mutable title would be recorded here, not silently normalised away.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)  # leaves 1 pending candidate
    _mark_grounded_paper(repo, notebook.id, "s1", "A Grounded Paper Title")
    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 1), LATER)

    seen_keys: list[tuple] = []
    seen_args: list[tuple] = []
    original = service._target_lock_key

    def spy(*args, **kwargs):
        key = original(*args, **kwargs)
        seen_args.append(args)
        seen_keys.append(key)
        return key

    service._target_lock_key = spy
    try:
        second_job = service.start(notebook.id, "s1")
    finally:
        service._target_lock_key = original

    expected = service._target_lock_key(notebook.id)
    assert expected == ("catalog", notebook.id)
    # The stale sweep ran (the prior job's one candidate is no longer pending)
    # and computed EXACTLY this key.
    assert seen_keys == [expected]
    # And it did so from the notebook id alone: no title reached the key, so
    # the grounded title above could not have moved it.
    assert seen_args == [(notebook.id,)]
    assert second_job["id"] != job["id"]


# R11 (codex PR #412 R11 评审 P2) 修复补充覆盖:`_find_table` 改走
# `knowhow_table_id_by_title` 有界点查,不再拿 `list_knowhow_tables` 的健康聚合
# 全表扫描去过一遍标题匹配。
def test_find_table_uses_the_bounded_point_lookup_and_matches_list_ordering(repo):
    """新点查路径必须与旧 list 版行为等价——包括同名多表时的确定性:两者都要
    选中同一张表。旧版按 `list_knowhow_tables` 自己的 `ORDER BY created_at,id`
    取第一个匹配,点查复刻同一条排序,所以这里既证明「选中同一张」也证明
    「选中的正是创建序最早的那张」。"""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    service = _service(repo)
    title = f"{CATALOG_TABLE_TITLE_PREFIX}重名手册"

    first_id = repo.create_knowhow_table(
        notebook.id, title, "", [{"name": "命令", "role": "anchor"}], "tester",
    )
    second_id = repo.create_knowhow_table(
        notebook.id, title, "", [{"name": "命令", "role": "anchor"}], "tester",
    )
    assert first_id != second_id

    listed = repo.list_knowhow_tables(notebook.id)
    expected = next(t["id"] for t in listed if t["title"] == title)
    assert expected == first_id  # creation order: the earliest table wins

    assert service._find_table(notebook.id, "重名手册") == expected
    assert service.knowhow.knowhow_table_id_by_title(notebook.id, title) == expected

    # A title nobody used resolves to "", not an exception.
    assert service._find_table(notebook.id, "从未出现过的标题") == ""


def test_find_table_point_lookup_is_index_backed_not_a_notebook_scan(repo):
    """R14 P2: `_migration_39` installs `idx_knowhow_tables_nb_title` on
    `(notebook_id, title, created_at, id)`, and the by-title resolution must
    actually plan onto it.

    R11 already moved this off `list_knowhow_tables`, which was the expensive
    half — but the replacement still had only `idx_knowhow_tables_nb` to work
    with, a single-column index on `notebook_id`. That plans as "read every
    table row in the notebook, filter by title, sort by (created_at, id)":
    bounded by the notebook's table count rather than by anything about the
    query, and paid inside the locked apply/dismiss window. Asserting the plan
    (rather than just the behaviour) is the only thing that catches a
    regression here: dropping the index leaves every functional test green.

    The plan must be a SEARCH (an index seek), not a SCAN, and must carry no
    "USE TEMP B-TREE FOR ORDER BY" step — the last two index columns already
    supply the tie-break order, so `LIMIT 1` resolves without sorting.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    title = f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"
    repo.create_knowhow_table(
        notebook.id, title, "", [{"name": "命令", "role": "anchor"}], "tester",
    )

    with repo._write() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id FROM knowhow_tables WHERE notebook_id=? AND title=? "
            "ORDER BY created_at,id LIMIT 1",
            (notebook.id, title),
        ).fetchall()
    detail = " | ".join(str(row["detail"]) for row in plan)

    assert "idx_knowhow_tables_nb_title" in detail, detail
    assert "SEARCH knowhow_tables" in detail, detail
    assert "SCAN knowhow_tables" not in detail, detail
    assert "TEMP B-TREE" not in detail, detail
    # Both equality columns are consumed by the seek, so the title is not left
    # as a residual filter over every table in the notebook.
    assert "notebook_id=? AND title=?" in detail, detail

    # And the behaviour the plan serves is unchanged.
    assert _service(repo)._find_table(notebook.id, "OpenROAD 手册") != ""


def test_find_table_never_calls_the_health_aggregated_table_scan(repo, monkeypatch):
    """成本形状守卫:`_find_table`(以及经它解析目标表的 `apply`)必须走有界
    点查,绝不再落回 `list_knowhow_tables` 的健康聚合全表扫描——那一次扫描要
    对 notebook 里*每一张*表算行数、投影状态、单元格活跃度和代码附件,只为
    一次标题匹配纯属浪费。这条只做行为断言的用例(哪怕退回旧实现)也会照样
    绿——只有对着重活方法的调用计数才能抓住这类成本形状回归。"""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    def fail(*args, **kwargs):
        raise AssertionError(
            "table resolution must use the bounded knowhow_table_id_by_title "
            "point lookup, not the health-aggregated list_knowhow_tables scan"
        )

    monkeypatch.setattr(service.knowhow, "list_knowhow_tables", fail)

    result = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert result["created"] is True
    assert result["rows_added"] == 2


def test_apply_appends_only_commands_the_table_does_not_have(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    first = [page["items"][0]["id"]]
    rest = [row["id"] for row in page["items"][1:]]

    created = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=first, actor="tester"
    )
    assert created["rows_added"] == 1
    appended = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=rest, actor="tester"
    )
    assert appended["created"] is False
    assert appended["rows_added"] == 2
    assert appended["table_id"] == created["table_id"]
    table = repo.get_knowhow_table(created["table_id"])
    assert len(table["rows"]) == 3


def test_all_pending_apply_reports_what_it_could_not_confirm(repo):
    """One `all_pending` call confirms at most a page. Answering
    `rows_added: 100` on a 300-candidate run without saying so would read as
    "done" — the same "claimed everything, delivered a page" shape the
    collection-enumeration contract forbids elsewhere in this codebase."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)
    result = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert result["rows_added"] == 3
    assert result["pending_remaining"] == 0

    # Force the page bound to bite without inventing 100 fake candidates. A
    # SECOND notebook, because `_add_manual` gives every source the same title
    # and the target table is keyed on it — reusing this notebook would make all
    # three land as conflicts against the table just created.
    second = repo.create_notebook(NotebookCreate(name="n2"))
    _add_manual(repo, second.id, "s2", 3)
    other_service, other_job = _run_ok(repo, second.id, "s2", 3)
    original = other_service.catalog.pending_candidates

    def one_page(job_id, *, limit):
        return original(job_id, limit=1)

    other_service.catalog.pending_candidates = one_page  # type: ignore[assignment]
    try:
        partial = other_service.apply(
            second.id, "s2", other_job["id"], all_pending=True, actor="tester"
        )
    finally:
        other_service.catalog.pending_candidates = original  # type: ignore[assignment]
    assert partial["rows_added"] == 1
    assert partial["pending_remaining"] == 2


def test_apply_with_nothing_nameable_does_not_conjure_an_empty_table(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)

    def unnamed(prompt, _call):
        return json.dumps({"command_name": "not_a_documented_command", "args": []})

    bind_chat_client(repo, "kg_extract", _Client(unnamed))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    result = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert result["rows_added"] == 0
    assert result["created"] is False
    assert result["table_id"] == ""
    assert repo.list_knowhow_tables(notebook.id) == []


def test_apply_never_touches_an_existing_row_for_the_same_command(repo):
    """v1's one irreversible risk. A person may have corrected a row by hand
    after the previous apply; nothing here can tell that from a stale row, so
    an existing row is reported as a conflict and left exactly as it is."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    table = repo.get_knowhow_table(table_id)
    description_column = table["columns"][3]["id"]
    edited_row = table["rows"][0]["id"]
    repo.update_knowhow_cell(
        edited_row, description_column, "人工订正过的说明", actor="tester"
    )

    # A second run produces fresh candidates for the same commands.
    second_service, second_job = _run_ok(repo, notebook.id, "s1", 2)
    conflicted = second_service.apply(
        notebook.id, "s1", second_job["id"], all_pending=True, actor="tester"
    )
    assert conflicted["rows_added"] == 0
    assert {item["command_name"] for item in conflicted["conflicts"]} == {
        "set_thing_0", "set_thing_1"
    }
    after = repo.get_knowhow_table(table_id)
    assert len(after["rows"]) == 2
    assert after["rows"][0]["cells"][description_column] == "人工订正过的说明"


def test_repeated_all_pending_apply_on_a_fully_conflicting_rerun_converges(repo):
    """Before the fix, a conflict candidate never left `state='candidate'`, so
    `pending_candidates`'s cursor=0 keyset read returned the SAME conflicting
    page every time and repeated "confirm all" sat forever at
    `(rows_added=0, len(conflicts)=N, pending_remaining=N)`. This is the
    regression: a source re-run whose commands ALL already have rows must
    settle after being reported once, not resurface as fresh conflicts on
    every later click."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert first["rows_added"] == 2

    # A second run over the same source produces fresh candidates for the
    # exact same two commands — every one of them conflicts.
    second_service, second_job = _run_ok(repo, notebook.id, "s1", 2)
    call_one = second_service.apply(
        notebook.id, "s1", second_job["id"], all_pending=True, actor="tester"
    )
    assert call_one["rows_added"] == 0
    assert {item["command_name"] for item in call_one["conflicts"]} == {
        "set_thing_0", "set_thing_1"
    }
    assert call_one["pending_remaining"] == 0
    # The conflicting candidates converged out of `candidate` state into
    # `dismissed`, carrying why — not `rejected` (that means "grounding
    # itself produced nothing usable"; these were legitimate commands that
    # merely already had a row).
    counts = second_service.catalog.candidate_counts(second_job["id"])
    assert counts["candidate"] == 0
    assert counts["dismissed"] == 2
    dismissed = second_service.catalog.list_candidates(
        second_job["id"], state="dismissed", cursor=0, limit=10
    )
    assert {row["reject_info"].get("reason") for row in dismissed} == {
        "conflict_existing_row"
    }

    # Clicking "confirm all" again must NOT resurface the same two rows as
    # conflicts a second time — that is exactly the stuck-forever bug.
    call_two = second_service.apply(
        notebook.id, "s1", second_job["id"], all_pending=True, actor="tester"
    )
    assert call_two["rows_added"] == 0
    assert call_two["conflicts"] == []
    assert call_two["pending_remaining"] == 0


def test_all_pending_apply_advances_past_a_fully_conflicting_first_page(repo):
    """300-candidate scenario from the review report: if the first
    `MAX_APPLY_CANDIDATES` (100) candidates by position ALL conflict, a
    second `all_pending` call must reach position 101+ rather than being
    handed the identical first-100 page again. Seeded directly through the
    store (no model calls) so the fixture stays fast and the position
    ordering is exact."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)  # only need a real source_id to hang the job off
    service = _service(repo)
    source_title = "OpenROAD 手册"
    table_id = repo.create_knowhow_table(
        notebook.id,
        f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}",
        "",
        [dict(column) for column in CATALOG_TABLE_COLUMNS],
        created_by="tester",
        actor="tester",
    )
    table = repo.get_knowhow_table(table_id)
    anchor = table["columns"][0]["id"]
    repo.append_knowhow_rows(
        table_id,
        [{anchor: f"cmd_{i:03d}"} for i in range(MAX_APPLY_CANDIDATES)],
        actor="tester",
        origin="import",
    )

    # `source_generation` is snapshotted by `start()` in production; a job built
    # by hand has to record it too, or `apply`'s R8 staleness guard reads this
    # fixture as "the source was reparsed after the run".
    job = service.catalog.create_job(
        notebook.id,
        "s1",
        "tester",
        source_generation=service.catalog.source_element_generation("s1"),
    )
    # position is 1-indexed in real runs (`_process_section`'s `next_position`
    # is pre-incremented before the first row) — `list_candidates`'s keyset
    # read is `position > cursor` with cursor=0, so a position=0 row would
    # silently never surface on a `cursor=0` page. Mirror that here rather
    # than starting at 0.
    conflicting = [
        {
            "job_id": job["id"],
            "notebook_id": notebook.id,
            "source_id": "s1",
            "position": i + 1,
            "section_path": f"cmd_{i:03d}",
            "command_name": f"cmd_{i:03d}",
            "payload": {},
            "state": "candidate",
        }
        for i in range(MAX_APPLY_CANDIDATES)
    ]
    fresh = [
        {
            "job_id": job["id"],
            "notebook_id": notebook.id,
            "source_id": "s1",
            "position": MAX_APPLY_CANDIDATES + i + 1,
            "section_path": f"new_{i:03d}",
            "command_name": f"new_{i:03d}",
            "payload": {},
            "state": "candidate",
        }
        for i in range(50)
    ]
    service.catalog.add_candidates(conflicting + fresh)

    call_one = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert call_one["rows_added"] == 0
    assert len(call_one["conflicts"]) == MAX_APPLY_CANDIDATES
    assert call_one["pending_remaining"] == 50

    call_two = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert call_two["conflicts"] == []
    assert call_two["rows_added"] == 50
    assert {row["cells"][anchor] for row in repo.get_knowhow_table(table_id)["rows"]} >= {
        f"new_{i:03d}" for i in range(50)
    }
    assert call_two["pending_remaining"] == 0


def test_apply_addresses_columns_by_name_after_a_column_is_removed(repo):
    """The target table is an ordinary knowhow table with live column
    add/delete/rename endpoints. Dropping 参数 shifts every later column one
    slot left, and a positional mapping would keep "working" — filing the
    description under 示例 and the provenance under 说明. Only a name-keyed
    mapping survives, and no test of the happy path can see the difference."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    first_id, second_id = page["items"][0]["id"], page["items"][1]["id"]

    applied = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[first_id], actor="tester"
    )
    table_id = applied["table_id"]
    before = repo.get_knowhow_table(table_id)
    dropped = next(c["id"] for c in before["columns"] if c["name"] == "参数")
    repo.delete_knowhow_column(dropped)
    table = repo.get_knowhow_table(table_id)
    names = [column["name"] for column in table["columns"]]
    assert names == ["命令", "语法", "说明", "示例", "出处"], names
    by_name = {column["name"]: column["id"] for column in table["columns"]}

    service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[second_id], actor="tester"
    )
    after = repo.get_knowhow_table(table_id)
    assert len(after["rows"]) == 2
    row = after["rows"][1]
    assert row["cells"][by_name["命令"]] == "set_thing_1"
    assert "set_thing_1 -density value" in row["cells"][by_name["语法"]]
    assert row["cells"][by_name["说明"]] == "does a thing"
    assert "set_thing_1 -density 0.6" in row["cells"][by_name["示例"]]
    assert "OpenROAD 手册" in row["cells"][by_name["出处"]]


def test_apply_after_the_table_is_renamed_still_targets_it_via_the_job(repo):
    """M1: apply used to resolve its target by TITLE on every call. Rename the
    table between two apply calls for the SAME job and the second call would
    no longer find it by title — either missing it, or worse, matching some
    OTHER table that coincidentally now carries the derived title. The job
    remembers `applied_table_id` after its first successful apply, and every
    later apply for that job must resolve through it first."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    first_id, second_id = page["items"][0]["id"], page["items"][1]["id"]

    first = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[first_id], actor="tester"
    )
    table_id = first["table_id"]
    repo.update_knowhow_table_meta(table_id, title="手工重命名后的表")

    second = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[second_id], actor="tester"
    )
    assert second["table_id"] == table_id
    assert second["created"] is False
    table = repo.get_knowhow_table(table_id)
    assert len(table["rows"]) == 2
    # No second 「命令目录：…」 table was created under the OLD title.
    assert len(repo.list_knowhow_tables(notebook.id)) == 1


def test_apply_existence_check_does_not_hydrate_the_whole_target_table(repo):
    """M2: apply's "does this command already have a row" check used to load
    the ENTIRE target table (`get_knowhow_table`, every row and cell) just to
    scan its anchor column in Python. Pad the table to 3000 rows first, then
    prove a second apply — which must consult that same anchor column to spot
    a conflict — never calls the whole-table hydrate at all."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]

    table = repo.get_knowhow_table(table_id)
    anchor = table["columns"][0]["id"]
    padding = [{anchor: f"padding_{i}"} for i in range(3000)]
    repo.append_knowhow_rows(table_id, padding, actor="tester", origin="import")

    def must_not_hydrate(_table_id):
        raise AssertionError(
            "apply's existence check must not hydrate the whole target table"
        )

    service.knowhow.get_knowhow_table = must_not_hydrate
    _add_manual(repo, notebook.id, "s2", 1)
    second_service, second_job = _run_ok(repo, notebook.id, "s2", 1)
    result = second_service.apply(
        notebook.id, "s2", second_job["id"], all_pending=True, actor="tester"
    )
    # "s2" derives the SAME title as "s1" (`_add_manual` always titles a
    # source "OpenROAD 手册") and produces the SAME single command name
    # (`set_thing_0`), so this must land as a conflict against the row "s1"
    # already applied — exercising the existence check for real, not just
    # skipping it because nothing collided.
    assert result["rows_added"] == 0
    assert {item["command_name"] for item in result["conflicts"]} == {"set_thing_0"}


def test_concurrent_first_applies_for_two_sources_sharing_a_title_do_not_split_the_table(
    repo,
):
    """M1/R1: two DIFFERENT sources that derive the SAME target title
    (`_add_manual` always titles a source "OpenROAD 手册") must not each
    create their own table when their FIRST applies race — the lock has to
    key on the TARGET (the title, before either job has an `applied_table_id`
    of its own), not on `(notebook_id, source_id)`: two different per-source
    locks would let both threads observe "no table yet" and each create one.

    `_add_manual(..., commands=1)` also always names the single command
    `set_thing_0` regardless of `source_id`, so this simultaneously proves
    the R1 fix for the lock-KEY-mismatch race: before it, a job with a known
    `applied_table_id` and a job applying for the first time could compute
    two DIFFERENT lock keys for the SAME resolved target and both pass the
    anchor-column existence check before either wrote, landing the SAME
    command name twice. Here both jobs are first-time appliers proposing the
    identical command name, so the fixed unified lock key must still let only
    ONE of them land the row — the other must observe it as a conflict, not
    write a second row for `set_thing_0`.

    The block point is INSIDE the held lock (`knowhow.create_knowhow_table`,
    reached only from `_resolve_target_table` called by `_apply_locked`),
    not the read-only `_find_table` lookup `_target_lock_key` now also makes
    BEFORE acquiring any lock — blocking that earlier, unlocked call would
    no longer prove mutual exclusion, since it runs before either thread has
    taken a lock at all.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    _add_manual(repo, notebook.id, "s2", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    _, job_b = _run_ok(repo, notebook.id, "s2", 1)

    entered = threading.Event()
    release = threading.Event()
    original_create = service.knowhow.create_knowhow_table

    def blocking_create(*args, **kwargs):
        entered.set()
        release.wait(10)
        return original_create(*args, **kwargs)

    service.knowhow.create_knowhow_table = blocking_create
    results: dict[str, dict] = {}
    try:
        first_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "a",
                service.apply(
                    notebook.id, "s1", job_a["id"], all_pending=True, actor="a"
                ),
            )
        )
        first_thread.start()
        assert entered.wait(10), "first apply never reached table creation"

        second_started = threading.Event()

        def run_second():
            second_started.set()
            results["b"] = service.apply(
                notebook.id, "s2", job_b["id"], all_pending=True, actor="b"
            )

        second_thread = threading.Thread(target=run_second)
        second_thread.start()
        assert second_started.wait(5)
        time.sleep(0.2)  # give it a chance to race ahead if the lock is wrong
        assert "b" not in results, (
            "second apply proceeded despite the first apply's title lock"
        )

        release.set()
        first_thread.join(10)
        second_thread.join(10)
    finally:
        service.knowhow.create_knowhow_table = original_create

    tables = repo.list_knowhow_tables(notebook.id)
    assert len(tables) == 1
    assert results["a"]["table_id"] == results["b"]["table_id"] == tables[0]["id"]

    # Exactly one side actually wrote `set_thing_0`; the other must observe it
    # as a conflict, never write a second row for the same command.
    outcomes = [results["a"], results["b"]]
    written = [item for item in outcomes if item["rows_added"] == 1]
    conflicted = [item for item in outcomes if item["conflicts"]]
    assert len(written) == 1, results
    assert len(conflicted) == 1, results
    assert {c["command_name"] for c in conflicted[0]["conflicts"]} == {"set_thing_0"}
    table = repo.get_knowhow_table(tables[0]["id"])
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    assert [row["cells"][anchor] for row in table["rows"]] == ["set_thing_0"]


def test_concurrent_applies_of_the_same_job_from_two_tabs_write_the_command_once(
    repo,
):
    """R1: two browser tabs double-clicking "确认全部待审阅" for the SAME job
    at (nearly) the same time must converge on a single written row for the
    one candidate, never two — mirroring the cross-job race above but with
    both concurrent callers sharing one job id (and therefore, before the
    fix, initially the SAME lock key already — the mismatch this test
    additionally guards is a job whose `applied_table_id` gets set by the
    FIRST call while the SECOND call's already-in-flight `apply()` is still
    working off its own pre-call job snapshot with an empty
    `applied_table_id`).
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)

    entered = threading.Event()
    release = threading.Event()
    original_create = service.knowhow.create_knowhow_table

    def blocking_create(*args, **kwargs):
        entered.set()
        release.wait(10)
        return original_create(*args, **kwargs)

    service.knowhow.create_knowhow_table = blocking_create
    results: dict[str, dict] = {}
    try:
        first_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "a",
                service.apply(
                    notebook.id, "s1", job["id"], all_pending=True, actor="a"
                ),
            )
        )
        first_thread.start()
        assert entered.wait(10), "first apply never reached table creation"

        second_started = threading.Event()

        def run_second():
            second_started.set()
            results["b"] = service.apply(
                notebook.id, "s1", job["id"], all_pending=True, actor="b"
            )

        second_thread = threading.Thread(target=run_second)
        second_thread.start()
        assert second_started.wait(5)
        time.sleep(0.2)
        assert "b" not in results, (
            "second apply proceeded despite the first apply's lock"
        )

        release.set()
        first_thread.join(10)
        second_thread.join(10)
    finally:
        service.knowhow.create_knowhow_table = original_create

    tables = repo.list_knowhow_tables(notebook.id)
    assert len(tables) == 1
    table = repo.get_knowhow_table(tables[0]["id"])
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    assert [row["cells"][anchor] for row in table["rows"]] == ["set_thing_0"]

    # The second call's own candidate row is the SAME row the first already
    # marked `applied` by the time it runs — it converges to a safe no-op
    # (`rows_added: 0`) rather than a `conflicts` entry, because it is one
    # candidate id competed for twice, not two different candidates sharing a
    # command name. Either way, only one write ever lands.
    outcomes = [results["a"], results["b"]]
    written = [item for item in outcomes if item["rows_added"] == 1]
    assert len(written) == 1, results
    assert sum(item["rows_added"] for item in outcomes) == 1


def test_target_lock_key_is_immutable_across_every_thing_that_can_change(repo):
    """R14 P1 (direct): the lock key is ``("catalog", notebook_id)`` — it
    carries no derived title and no table id, so nothing a writer does or a
    background job backfills can move it.

    Three identities were tried here across three review rounds. R1 keyed on
    ``("table", id)`` as soon as a table could be found; that answer flips the
    instant the first applier commits its ``create_knowhow_table`` INSIDE the
    lock. R2 replaced it with the derived title, which looked immutable only
    because `sources.title` is — but `_display_source_title` resolves through
    `source_display_title`, and paper-metadata grounding promotes the upload
    name to the paper title asynchronously, which can land BETWEEN two applies
    of one job. Both are state a concurrent writer can change; only the
    notebook id is not.

    Deterministic and non-timing-dependent on purpose: this walks every event
    that used to move the key — a real apply creating the table, the job
    learning its `applied_table_id`, a paper-title backfill, and a second
    source in the same notebook — and asserts the key never budges.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    expected = ("catalog", notebook.id)

    assert service._target_lock_key(notebook.id) == expected

    applied = service.apply(
        notebook.id, "s1", job_a["id"], all_pending=True, actor="a"
    )
    assert service.catalog.get_job(job_a["id"])["applied_table_id"] == applied[
        "table_id"
    ]
    assert service._target_lock_key(notebook.id) == expected

    # The R14 event itself: grounding lands after the table already exists and
    # changes what this source is canonically called.
    _mark_grounded_paper(repo, notebook.id, "s1", "A Later-Grounded Title")
    assert service._display_source_title(
        "s1", service._scoped_source(notebook.id, "s1")
    ) == "A Later-Grounded Title"
    assert service._target_lock_key(notebook.id) == expected

    # A different source in the same notebook takes the same key too — that is
    # what makes "could collide" and "same lock" the same statement, and it no
    # longer depends on the two deriving a matching title.
    _add_manual(repo, notebook.id, "s2", 1)
    assert service._target_lock_key(notebook.id) == expected

    # Nothing title-shaped or table-shaped survives in the key at all.
    assert CATALOG_TABLE_TITLE_PREFIX not in "".join(
        str(part) for part in service._target_lock_key(notebook.id)
    )
    assert applied["table_id"] not in service._target_lock_key(notebook.id)


def test_concurrent_table_known_and_first_time_applies_for_the_same_target_serialize(
    repo,
):
    """R1 P2 (integration): a job that already knows its target table
    (``applied_table_id`` set by an earlier, sequential apply) and a
    DIFFERENT job applying for the FIRST time to a source that derives the
    SAME title — proposing the SAME new command name neither side has
    written yet — must never run their existence-check + write
    concurrently. Before the fix, the table-known job locked on
    ``("table", id)`` while the first-time job locked on ``("title", ...)``:
    two DIFFERENT locks that do not exclude each other, so both could read
    "not yet present" before either wrote and land two rows for one command.

    Checked by directly counting concurrent entries into the existence
    check (``max_active``) rather than by "did the second call return
    early" wall-clock racing: a mismatched-lock second caller that is ALSO
    stuck behind this same blocking mock looks identical, from the outside,
    to one correctly queued behind the first caller's lock. Counting
    concurrent entries is the only assertion that actually distinguishes
    "excluded" from "raced in and got stuck at the same place".
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    _add_manual(repo, notebook.id, "s2", 2)
    service, job_a = _run_ok(repo, notebook.id, "s1", 2)
    _, job_b = _run_ok(repo, notebook.id, "s2", 2)

    def candidate_id(job_id: str, name: str) -> str:
        page = service.catalog.list_candidates(
            job_id, state="candidate", cursor=0, limit=10
        )
        return next(row["id"] for row in page if row["command_name"] == name)

    first = service.apply(
        notebook.id, "s1", job_a["id"],
        candidate_ids=[candidate_id(job_a["id"], "set_thing_0")],
        actor="a",
    )
    assert first["created"] is True
    table_id = first["table_id"]
    assert service.catalog.get_job(job_a["id"])["applied_table_id"] == table_id

    active = 0
    max_active = 0
    guard = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    original = service.knowhow.knowhow_anchor_existing_values

    def tracking(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(10)
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    service.knowhow.knowhow_anchor_existing_values = tracking
    results: dict[str, dict] = {}
    try:
        first_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "a",
                service.apply(
                    notebook.id, "s1", job_a["id"],
                    candidate_ids=[candidate_id(job_a["id"], "set_thing_1")],
                    actor="a",
                ),
            )
        )
        first_thread.start()
        assert entered.wait(10), "first apply never reached the existence check"

        second_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "b",
                service.apply(
                    notebook.id, "s2", job_b["id"],
                    candidate_ids=[candidate_id(job_b["id"], "set_thing_1")],
                    actor="b",
                ),
            )
        )
        second_thread.start()
        time.sleep(0.3)  # give a mismatched lock a chance to race in

        release.set()
        first_thread.join(10)
        second_thread.join(10)
    finally:
        service.knowhow.knowhow_anchor_existing_values = original

    assert max_active == 1, "both applies entered the existence check concurrently"

    table = repo.get_knowhow_table(table_id)
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    names = sorted(row["cells"][anchor] for row in table["rows"])
    assert names == ["set_thing_0", "set_thing_1"]

    outcomes = [results["a"], results["b"]]
    written = [item for item in outcomes if item["rows_added"] == 1]
    assert len(written) == 1, results


def test_a_paper_title_backfill_between_applies_cannot_split_the_lock(repo):
    """R14 P1 (integration): a paper-metadata backfill landing between two
    applies must not put two writers for ONE table on two different locks.

    The reachable shape, and why the parse barrier does not already cover it:
    the barrier is per SOURCE, so it only serializes confirms of the SAME
    source. Two DIFFERENT sources whose derived titles agree resolve to one
    table (`_add_manual` titles every source "OpenROAD 手册", and by-title
    resolution finds the first one's table), and their barriers are different
    mutexes, so the catalog lock is the ONLY thing between them.

    Under R2's title key that lock split the moment grounding changed one
    side's canonical name:

      * job A (s1) applies once, creating T and recording `applied_table_id`;
      * grounding lands on s1, so A's derived title becomes
        "A Later-Grounded Title" while its target is still T;
      * job B (s2) applies for the first time, derives "OpenROAD 手册", and
        resolves by title to that same T.

    Two writers, one table, two keys — both free through the anchor-column
    existence check, both appending a row for `set_thing_1`. The key is the
    notebook id now, so they share one lock whatever either is called.

    Concurrency is COUNTED at the existence check rather than inferred from
    "did B return early": a B that raced in and then parked on this same mock
    is indistinguishable from a correctly queued B from the outside.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    _add_manual(repo, notebook.id, "s2", 2)
    service, job_a = _run_ok(repo, notebook.id, "s1", 2)
    _, job_b = _run_ok(repo, notebook.id, "s2", 2)

    def candidate_id(job_id: str, name: str) -> str:
        page = service.catalog.list_candidates(
            job_id, state="candidate", cursor=0, limit=10
        )
        return next(row["id"] for row in page if row["command_name"] == name)

    first = service.apply(
        notebook.id, "s1", job_a["id"],
        candidate_ids=[candidate_id(job_a["id"], "set_thing_0")],
        actor="a",
    )
    assert first["created"] is True
    table_id = first["table_id"]
    assert service.catalog.get_job(job_a["id"])["applied_table_id"] == table_id

    # The R14 event: s1 is now canonically called something else, so its
    # derived title no longer matches s2's — and no longer matches the title
    # of the table it is still writing into.
    _mark_grounded_paper(repo, notebook.id, "s1", "A Later-Grounded Title")
    assert repo.get_knowhow_table(table_id)["title"] == (
        f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"
    )

    seen_keys: list[tuple] = []
    original_key = service._target_lock_key
    keys_guard = threading.Lock()

    def key_spy(*args, **kwargs):
        key = original_key(*args, **kwargs)
        with keys_guard:
            seen_keys.append(key)
        return key

    active = 0
    max_active = 0
    guard = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    original = service.knowhow.knowhow_anchor_existing_values

    def tracking(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(10)
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    service._target_lock_key = key_spy
    service.knowhow.knowhow_anchor_existing_values = tracking
    results: dict[str, dict] = {}
    try:
        first_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "a",
                service.apply(
                    notebook.id, "s1", job_a["id"],
                    candidate_ids=[candidate_id(job_a["id"], "set_thing_1")],
                    actor="a",
                ),
            )
        )
        first_thread.start()
        assert entered.wait(10), "first apply never reached the existence check"

        second_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "b",
                service.apply(
                    notebook.id, "s2", job_b["id"],
                    candidate_ids=[candidate_id(job_b["id"], "set_thing_1")],
                    actor="b",
                ),
            )
        )
        second_thread.start()
        time.sleep(0.3)  # give a split lock a chance to race in

        release.set()
        first_thread.join(10)
        second_thread.join(10)
    finally:
        service.knowhow.knowhow_anchor_existing_values = original
        service._target_lock_key = original_key

    assert max_active == 1, "both applies entered the existence check concurrently"

    # Both still wrote into the SAME table, and `set_thing_1` landed once.
    assert results["a"]["table_id"] == results["b"]["table_id"] == table_id
    assert len(repo.list_knowhow_tables(notebook.id)) == 1
    table = repo.get_knowhow_table(table_id)
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    assert sorted(row["cells"][anchor] for row in table["rows"]) == [
        "set_thing_0", "set_thing_1"
    ]
    written = [item for item in results.values() if item["rows_added"] == 1]
    conflicted = [item for item in results.values() if item["conflicts"]]
    assert len(written) == 1, results
    assert len(conflicted) == 1, results

    # The structural half: every key computed during the race is the SAME
    # immutable one. A key carrying either source's derived title would show
    # up here as two distinct values — the exact defect, visible without
    # depending on the timing above.
    assert set(seen_keys) == {("catalog", notebook.id)}, seen_keys


def test_an_applier_that_arrives_after_the_table_appears_still_waits(repo):
    """R2 P2 (integration): the first-creation window, which is the one R1's
    lock key could not cover.

    A applies for a target with no table yet, so under R1 it locked on
    ``("title", ...)``. It then creates the table INSIDE that lock — and the
    creation commits. B arrives afterwards, and its lock key was computed from
    what it could see: a table now exists, so R1 handed it ``("table", id)``.
    Two different locks over one target, both free to run the existence check
    and append. The window is not theoretical — it is exactly a second confirm
    landing while the first is still working.

    Blocking point is the existence check, which runs AFTER table creation, so
    B genuinely can see the new table by the time it computes its key.
    Concurrency is counted (``max_active``) rather than inferred from "did B
    return early": a B that raced in and then got stuck behind this same mock
    looks identical from the outside to a B correctly queued behind A's lock.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    _add_manual(repo, notebook.id, "s2", 2)
    service, job_a = _run_ok(repo, notebook.id, "s1", 2)
    _, job_b = _run_ok(repo, notebook.id, "s2", 2)
    assert not repo.list_knowhow_tables(notebook.id), "the target must not exist yet"

    def candidate_id(job_id: str, name: str) -> str:
        page = service.catalog.list_candidates(
            job_id, state="candidate", cursor=0, limit=10
        )
        return next(row["id"] for row in page if row["command_name"] == name)

    active = 0
    max_active = 0
    guard = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    original = service.knowhow.knowhow_anchor_existing_values

    def tracking(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        release.wait(10)
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    service.knowhow.knowhow_anchor_existing_values = tracking
    results: dict[str, dict] = {}
    try:
        first_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "a",
                service.apply(
                    notebook.id, "s1", job_a["id"],
                    candidate_ids=[candidate_id(job_a["id"], "set_thing_0")],
                    actor="a",
                ),
            )
        )
        first_thread.start()
        assert entered.wait(10), "first apply never reached the existence check"
        # A is inside its lock and has already committed the table — which is
        # precisely what used to change B's lock key out from under it.
        assert len(repo.list_knowhow_tables(notebook.id)) == 1

        second_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "b",
                service.apply(
                    notebook.id, "s2", job_b["id"],
                    candidate_ids=[candidate_id(job_b["id"], "set_thing_1")],
                    actor="b",
                ),
            )
        )
        second_thread.start()
        time.sleep(0.3)  # give a mismatched lock a chance to race in

        release.set()
        first_thread.join(10)
        second_thread.join(10)
    finally:
        service.knowhow.knowhow_anchor_existing_values = original

    assert max_active == 1, "both applies entered the existence check concurrently"
    tables = repo.list_knowhow_tables(notebook.id)
    assert len(tables) == 1
    table = repo.get_knowhow_table(tables[0]["id"])
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    assert sorted(row["cells"][anchor] for row in table["rows"]) == [
        "set_thing_0", "set_thing_1"
    ]
    assert results["a"]["rows_added"] == results["b"]["rows_added"] == 1


def test_apply_refuses_a_table_that_lost_its_command_column(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table = repo.get_knowhow_table(first["table_id"])
    command_column = next(c["id"] for c in table["columns"] if c["name"] == "命令")
    repo.delete_knowhow_column(command_column)

    second_service, second_job = _run_ok(repo, notebook.id, "s1", 1)
    with pytest.raises(ValueError) as excinfo:
        second_service.apply(
            notebook.id, "s1", second_job["id"], all_pending=True, actor="tester"
        )
    assert str(excinfo.value) == APPLY_TABLE_SHAPE_MESSAGE


def test_apply_refuses_a_table_whose_anchor_moved_off_the_command_column(repo):
    """R1 P2: a name match alone is not the anchor. If a person moves the
    table's anchor designation to a DIFFERENT column (via the table's own
    "行标题列" setting) while a column still named 「命令」 survives as a
    plain attribute, a by-name-only lookup would happily resolve that
    demoted attribute column and write command names into it — a column
    that is no longer what makes a row a graph node named after the
    command. Apply must refuse instead of writing to a non-anchor column.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table = repo.get_knowhow_table(first["table_id"])
    syntax_column = next(c["id"] for c in table["columns"] if c["name"] == "语法")
    repo.set_knowhow_anchor_column(first["table_id"], syntax_column)

    refreshed = repo.get_knowhow_table(first["table_id"])
    command_column = next(c for c in refreshed["columns"] if c["name"] == "命令")
    assert command_column["role"] == "attribute"

    second_service, second_job = _run_ok(repo, notebook.id, "s1", 1)
    with pytest.raises(ValueError) as excinfo:
        second_service.apply(
            notebook.id, "s1", second_job["id"], all_pending=True, actor="tester"
        )
    assert str(excinfo.value) == APPLY_TABLE_SHAPE_MESSAGE


def test_apply_refuses_a_table_with_a_second_command_named_column(repo):
    """R1 P2: exactly one column may be named 「命令」. A second column that
    happens to share that name (ambiguous which one is the row title) must
    also be refused rather than silently resolved by whichever one a
    dict-by-name lookup keeps last.

    The store's own public API (`add_knowhow_column` / `rename_knowhow_column`)
    already refuses a duplicate name, so this state is not reachable through
    today's UI — this fixture writes the row directly to prove the guard
    still holds if that ever changes (defense in depth, not a claim that
    this is normally reachable).
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
            "VALUES (?, ?, ?, ?, ?)",
            ("khcol-dup-command", table_id, "命令", "attribute", 99),
        )

    second_service, second_job = _run_ok(repo, notebook.id, "s1", 1)
    with pytest.raises(ValueError) as excinfo:
        second_service.apply(
            notebook.id, "s1", second_job["id"], all_pending=True, actor="tester"
        )
    assert str(excinfo.value) == APPLY_TABLE_SHAPE_MESSAGE


# --------------------------------------------------------------------- dismiss
# R7 (codex PR #412 R7 review): the R5/R6 pending-candidates guard blocks a new
# run while the source's latest job still has unreviewed candidates, but
# nothing let a reviewer voluntarily give up on one — `apply` only ever moves a
# row out of `candidate` state without applying it on a CONFLICT. A candidate a
# person simply does not want had no route out of `candidate` at all, which
# would permanently lock the source out of re-extraction. These tests cover the
# explicit dismiss path this module was missing.
def test_dismiss_marks_selected_candidates_and_reports_what_it_could_not_a_second_time(
    repo,
):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    ids = [row["id"] for row in page["items"]]
    first, rest = ids[:1], ids[1:]

    result = service.dismiss(notebook.id, "s1", job["id"], candidate_ids=first)
    assert result["dismissed"] == first
    assert result["pending_remaining"] == 2

    # A candidate that is no longer `candidate` (already dismissed by the call
    # above) is silently excluded, not re-reported and not an error — the same
    # filter `apply`'s own `selected` applies to a candidate that moved on.
    again = service.dismiss(notebook.id, "s1", job["id"], candidate_ids=first)
    assert again["dismissed"] == []
    assert again["pending_remaining"] == 2

    dismissed_rows = service.catalog.list_candidates(
        job["id"], state="dismissed", cursor=0, limit=10
    )
    assert [row["id"] for row in dismissed_rows] == first
    # The reason code is the ONE thing that distinguishes this write path from
    # `_apply_locked`'s own `conflict_existing_row` — the frontend's
    # `dismissReasonText()` renders the two as different Chinese copy.
    assert dismissed_rows[0]["reject_info"]["reason"] == "user_dismissed"

    all_result = service.dismiss(notebook.id, "s1", job["id"], all_pending=True)
    assert sorted(all_result["dismissed"]) == sorted(rest)
    assert all_result["pending_remaining"] == 0

    counts = service.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 0
    assert counts["dismissed"] == 3


def test_dismiss_never_touches_knowhow(repo):
    """The whole point of a dismiss is that it does NOT write a row — unlike
    `apply`, it must never create or append to the `命令目录：<source>` table."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    service.dismiss(notebook.id, "s1", job["id"], all_pending=True)
    assert repo.list_knowhow_tables(notebook.id) == []


def test_dismiss_clears_the_pending_candidates_guard(repo):
    """The end-to-end assertion the guard's own copy has always promised:
    "confirm OR dismiss" only becomes true once dismissing pending candidates
    actually unblocks a new run — see `pending_candidates_message` and
    `catalogPendingReviewNote`."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    with pytest.raises(CatalogPendingCandidates):
        service.start(notebook.id, "s1")

    service.dismiss(notebook.id, "s1", job["id"], all_pending=True)

    # The guard is now clear.
    second = service.start(notebook.id, "s1")
    assert second["id"] != job["id"]


def test_dismiss_rejects_a_job_from_another_notebook_or_source(repo):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    other = repo.create_notebook(NotebookCreate(name="m"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    with pytest.raises(KeyError):
        service.dismiss(other.id, "s1", job["id"], all_pending=True)


def test_dismiss_serializes_behind_a_concurrent_apply_of_the_same_candidate(repo):
    """R7 mutation guard: `dismiss` must share `apply`'s per-target lock, not
    just mirror its read-then-write shape. `_apply_locked`'s conflict branch
    calls the SAME `mark_candidates_dismissed` `dismiss()` calls — both are a
    read-then-write sequence (read which rows are still `candidate`, then
    write) — so without a shared lock a dismiss racing an in-flight apply for
    the SAME candidate can win the `state` column AFTER that apply has already
    written the candidate's row into the knowhow table: the candidate would
    end up reporting `dismissed` (which every other caller reads as "never
    written") while the target table holds it, and
    `mark_candidates_applied`'s own `WHERE state='candidate'` guard then
    silently updates zero rows instead of surfacing the clash.

    Blocks `append_knowhow_rows` (not `create_knowhow_table`, unlike the
    apply/apply concurrency tests above) because the race this proves needs
    apply to still be mid-flight AFTER it has already decided to write this
    exact candidate but BEFORE `mark_candidates_applied` runs.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=5)
    candidate_id = page["items"][0]["id"]

    entered = threading.Event()
    release = threading.Event()
    original_append = service.knowhow.append_knowhow_rows

    def blocking_append(*args, **kwargs):
        entered.set()
        release.wait(10)
        return original_append(*args, **kwargs)

    service.knowhow.append_knowhow_rows = blocking_append
    results: dict[str, dict] = {}
    try:
        apply_thread = threading.Thread(
            target=lambda: results.__setitem__(
                "apply",
                service.apply(
                    notebook.id, "s1", job["id"], all_pending=True, actor="a"
                ),
            )
        )
        apply_thread.start()
        assert entered.wait(10), "apply never reached the knowhow write"

        dismiss_started = threading.Event()

        def run_dismiss():
            dismiss_started.set()
            results["dismiss"] = service.dismiss(
                notebook.id, "s1", job["id"], candidate_ids=[candidate_id]
            )

        dismiss_thread = threading.Thread(target=run_dismiss)
        dismiss_thread.start()
        assert dismiss_started.wait(5)
        time.sleep(0.2)  # give it a chance to race ahead if the lock is missing
        assert "dismiss" not in results, (
            "dismiss proceeded despite the in-flight apply's target lock"
        )

        release.set()
        apply_thread.join(10)
        dismiss_thread.join(10)
    finally:
        service.knowhow.append_knowhow_rows = original_append

    assert results["apply"]["rows_added"] == 1
    # The lock made dismiss wait until apply had already flipped the row to
    # `applied`; a dismiss that lost that race must be a no-op, never able to
    # relabel an already-applied candidate.
    assert results["dismiss"]["dismissed"] == []
    row = service.catalog.candidates_by_ids(job["id"], [candidate_id], limit=1)[0]
    assert row["state"] == "applied"


# ------------------------------------------------------------------------- API
@pytest.fixture
def http(tmp_path, monkeypatch):
    """The app and the test must share ONE repository instance.

    The cancel registry and the extraction service live on that instance, so a
    test that drove the run through a second repository over the same database
    file would be exercising a different service object than the routes do.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'http.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "hs"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")

    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    application = create_app()
    instance = deps.repository()
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    yield TestClient(application), instance
    deps.repository.cache_clear()


def test_api_preview_start_job_candidates_and_apply(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    preview = client.get(f"{base}/preview")
    assert preview.status_code == 200
    assert preview.json()["estimated_calls"] == 2
    assert preview.json()["signal"]["is_manual"] is False  # only two sections

    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    started = client.post(base)
    assert started.status_code == 200, started.text
    job_id = started.json()["job"]["id"]
    # `truncated_sections` is C1a's own count, surfaced through so the review
    # UI can eventually say "N sections were cut for length"; present in the
    # API response from the moment a job starts, not just once a section has
    # actually been truncated.
    assert "truncated_sections" in started.json()["job"]["progress"]
    assert "diagnostic" not in started.json()["job"]

    # The route owns the run (a background daemon thread); never drive it a
    # second time from here — two runs would share one cancel event and one
    # `queued -> running` claim, and the loser settles the row as cancelled.
    _await_terminal(repo, job_id)

    job = client.get(f"{base}/job")
    assert job.json()["job"]["status"] == "succeeded"
    assert job.json()["job"]["progress"]["entries"] == 2

    page = client.get(f"{base}/candidates", params={"job_id": job_id})
    assert page.status_code == 200
    body = page.json()
    assert len(body["items"]) == 2
    assert body["counts"]["candidate"] == 2
    assert body["items"][0]["args"][0]["name"] == "-density"

    applied = client.post(
        f"{base}/apply", params={"job_id": job_id}, json={"all_pending": True}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["rows_added"] == 2
    assert applied.json()["created"] is True


def test_api_candidates_state_dismissed_surfaces_the_conflict_reason(http):
    """R1 P2: `dismissed` candidates(确认时因目标表已有同名行被跳过)must have
    an API-visible page, not just a store-level state with no UI reachability.
    A second run over the same source produces candidates that all conflict
    with the first run's already-applied rows; after confirming, those
    candidates must be listable via `state=dismissed` and carry a
    `dismiss_reason` the frontend can translate into 「已存在同名行」.
    """
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    first_job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, first_job_id)
    first_apply = client.post(
        f"{base}/apply", params={"job_id": first_job_id}, json={"all_pending": True}
    )
    assert first_apply.json()["rows_added"] == 1

    second_job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, second_job_id)
    second_apply = client.post(
        f"{base}/apply", params={"job_id": second_job_id}, json={"all_pending": True}
    )
    assert second_apply.status_code == 200, second_apply.text
    assert second_apply.json()["rows_added"] == 0
    assert {c["command_name"] for c in second_apply.json()["conflicts"]} == {
        "set_thing_0"
    }

    dismissed_page = client.get(
        f"{base}/candidates",
        params={"job_id": second_job_id, "state": "dismissed"},
    )
    assert dismissed_page.status_code == 200, dismissed_page.text
    body = dismissed_page.json()
    assert body["counts"]["dismissed"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["command_name"] == "set_thing_0"
    assert body["items"][0]["state"] == "dismissed"
    assert body["items"][0]["dismiss_reason"] == "conflict_existing_row"
    # `rejections`(接地校验逐字段拦截)与 `dismiss_reason`(顶层跳过原因)是两套
    # 互不相关的载荷 —— 一条 dismissed 候选没有 per-field rejections。
    assert body["items"][0]["rejections"] == []


# ----------------------------------------------- R6 P2: apply recovery scheduling
def test_apply_recovery_retry_with_only_conflicts_still_schedules_projection(
    http, monkeypatch
):
    """A crash between `append_knowhow_rows` (rows land) and
    `mark_candidates_applied` means the ORIGINAL apply call — the one that
    actually wrote the rows — never reaches `catalog_routes.py`'s own
    post-return scheduling line: it raised before returning. The retry that
    follows resolves the SAME already-populated table and finds every one of
    its own candidates already present, so it reports `rows_added=0` with an
    all-conflicts page. Gating the projection schedule on `rows_added` would
    leave that already-appended data permanently unindexed; gating it on a
    resolved `table_id` catches this retry too.

    This reuses `test_api_candidates_state_dismissed_surfaces_the_conflict_
    reason`'s exact shape (a clean second run whose one candidate collides
    with the first run's already-applied row) — that IS the retry shape,
    modulo the crash itself being unobservable from here — and adds a spy on
    the real `ProjectionScheduler.schedule` to prove it still fires.
    """
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    scheduled: list[str] = []
    scheduler = knowhow_api.get_scheduler(repo)
    monkeypatch.setattr(scheduler, "schedule", scheduled.append)

    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    first_job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, first_job_id)
    first_apply = client.post(
        f"{base}/apply", params={"job_id": first_job_id}, json={"all_pending": True}
    )
    assert first_apply.json()["rows_added"] == 1
    table_id = first_apply.json()["table_id"]
    assert table_id
    assert scheduled == [table_id]
    scheduled.clear()

    # The "retry" leg: a second run over the same source whose one candidate
    # conflicts with the row the first apply already landed — the same
    # response shape a genuine crash-then-retry produces.
    second_job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, second_job_id)
    second_apply = client.post(
        f"{base}/apply", params={"job_id": second_job_id}, json={"all_pending": True}
    )
    assert second_apply.status_code == 200, second_apply.text
    assert second_apply.json()["rows_added"] == 0
    assert second_apply.json()["conflicts"]
    assert second_apply.json()["table_id"] == table_id
    # The crux of R6 P2: scheduling must fire even though this call itself
    # added zero rows, because it resolved a real (already-populated) table.
    assert scheduled == [table_id]


def test_apply_with_nothing_nameable_does_not_schedule_an_empty_table_id(http, monkeypatch):
    """The OTHER branch of `_apply_locked` — no candidate had a usable
    command name at all — legitimately returns an empty `table_id` when no
    table exists yet. That must NOT schedule a projection for `""`."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    scheduled: list[str] = []
    scheduler = knowhow_api.get_scheduler(repo)
    monkeypatch.setattr(scheduler, "schedule", scheduled.append)

    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)
    page = client.get(f"{base}/candidates", params={"job_id": job_id})
    candidate_id = page.json()["items"][0]["id"]
    # Reject the only candidate first so the apply call below has nothing
    # nameable left to select — exercising the early "nothing to write"
    # return in `_apply_locked`, which never resolves/creates a table.
    with repo._write() as db:
        db.execute(
            "UPDATE catalog_candidates SET state='rejected' WHERE id=?",
            (candidate_id,),
        )
    apply_resp = client.post(
        f"{base}/apply", params={"job_id": job_id}, json={"all_pending": True}
    )
    assert apply_resp.status_code == 200, apply_resp.text
    assert apply_resp.json()["table_id"] == ""
    assert apply_resp.json()["rows_added"] == 0
    assert scheduled == []


def test_api_duplicate_start_is_a_user_readable_409(http):
    """The guard has to hold while the first run is still in flight.

    The stub blocks inside its first model call so the worker is provably still
    running when the duplicate POST lands. Letting the first job finish first
    would make this pass for the wrong reason — and intermittently, since it
    would then be racing the daemon thread.
    """
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    entered = threading.Event()
    release = threading.Event()

    def block_once(prompt, call):
        if call == 1:
            entered.set()
            release.wait(10)
        return _good_reply(prompt, call)

    bind_chat_client(repo, "kg_extract", _Client(block_once))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    try:
        assert client.post(base).status_code == 200
        assert entered.wait(10), "worker never reached its first model call"
        duplicate = client.post(base)
        assert duplicate.status_code == 409
        assert duplicate.headers.get("X-User-Message") == "1"
        assert "命令目录" in duplicate.json()["detail"]
    finally:
        release.set()


def test_api_start_without_a_configured_model_is_a_user_readable_409(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    )
    assert response.status_code == 409
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == MODEL_UNAVAILABLE_MESSAGE


# --------------------------------------------------- R5 P2: pending candidates
def test_start_raises_pending_candidates_with_the_exact_count(repo):
    """The service-level guard: a SUCCEEDED job with unreviewed candidates
    blocks a second `start`, and the exception carries the exact count so the
    route can hand the user a specific number."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)
    assert service.catalog.candidate_counts(job["id"])["candidate"] == 3

    with pytest.raises(CatalogPendingCandidates) as excinfo:
        service.start(notebook.id, "s1")
    assert excinfo.value.pending == 3
    # The guard fires BEFORE `create_job` — the source's latest job is still
    # the first one, not a second row stuck at `queued` behind it.
    assert service.catalog.latest_job("s1")["id"] == job["id"]


def test_api_second_start_with_unreviewed_candidates_is_a_user_readable_409(http):
    """`.../job` only ever returns a source's latest run, so starting a second
    one while the first's candidates are still unreviewed would orphan them —
    reachable by nobody (the frontend's own gate is the first line of
    defence; this is the data-layer backstop for a caller that goes around
    it — a retry, another tab, a stale page)."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    first = client.post(base).json()["job"]
    _await_terminal(repo, first["id"])

    second = client.post(base)
    assert second.status_code == 409
    assert second.headers.get("X-User-Message") == "1"
    assert second.json()["detail"] == pending_candidates_message(2)
    assert "2 条待审阅候选" in second.json()["detail"]

    # No orphaned second job was created.
    assert repo.command_catalog.latest_job("s1")["id"] == first["id"]


def test_api_second_start_is_allowed_once_all_candidates_are_applied(http):
    """The guard only ever looks at UNREVIEWED (`candidate`-state) rows —
    once every candidate has been confirmed (or would have been dismissed as
    a conflict), a new run is not orphaning anything and must proceed."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    first = client.post(base).json()["job"]
    _await_terminal(repo, first["id"])

    apply_response = client.post(
        f"{base}/apply", params={"job_id": first["id"]}, json={"all_pending": True}
    )
    assert apply_response.status_code == 200, apply_response.text
    assert apply_response.json()["rows_added"] == 1

    second = client.post(base)
    assert second.status_code == 200, second.text


def test_a_failed_run_with_retained_candidates_is_now_blocked(repo):
    """R6 P1: reverses R5's `succeeded`-only scoping — codex's review of that
    version pointed out that "retained" was never the same thing as
    "reachable": `.../job` only ever returns a source's MOST RECENT job, so
    a restart from a `failed` run orphans its candidates in the exact same
    way a restart from `succeeded` does (the review UI has no route back to
    a job id it never learned). `INTERRUPTED_MESSAGE` was reworded at the
    same time to stop promising an unconditional restart, so this guard and
    that copy now agree.

    Built by hand (start → running → one candidate row → fail) rather than
    driving a real extraction to `succeeded` and back: `finish_job` is only
    idempotent FORWARD (`WHERE status IN ('queued','running')`), so a
    genuinely succeeded row cannot be turned into `failed` afterwards — this
    directly models the run that actually produces `INTERRUPTED_MESSAGE`
    (settled `failed` while still `running`, candidates already written for
    whichever sections got through).
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.catalog.start_job(job["id"], sections_total=2)
    service.catalog.add_candidates([
        {
            "job_id": job["id"],
            "notebook_id": notebook.id,
            "source_id": "s1",
            "position": 1,
            "section_path": "set_thing_0",
            "command_name": "set_thing_0",
            "payload": {"syntax": "set_thing_0 -density value"},
            "state": "candidate",
            "reject_info": {},
        }
    ])
    service.catalog.finish_job(job["id"], "failed", failure_reason=INTERRUPTED_MESSAGE)
    assert service.catalog.candidate_counts(job["id"])["candidate"] == 1

    # A second start IS now blocked — same rule as the `succeeded` case.
    with pytest.raises(CatalogPendingCandidates) as excinfo:
        service.start(notebook.id, "s1")
    assert excinfo.value.pending == 1
    assert service.catalog.latest_job("s1")["id"] == job["id"]

    # The recovery route the reworded INTERRUPTED_MESSAGE actually points to
    # (审阅面板确认或跳过) unblocks it, exactly like the `succeeded` case.
    service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="tester")
    second = service.start(notebook.id, "s1")
    assert second["status"] == "queued"


def test_a_cancelled_run_with_retained_candidates_is_also_blocked(repo):
    """Direct parallel of the `failed` case above: the guard is keyed on
    `CATALOG_TERMINAL_STATUSES` (`succeeded`, `failed`, `cancelled`), not on
    one specific status, so `cancelled` must behave identically."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.catalog.start_job(job["id"], sections_total=2)
    service.catalog.add_candidates([
        {
            "job_id": job["id"],
            "notebook_id": notebook.id,
            "source_id": "s1",
            "position": 1,
            "section_path": "set_thing_0",
            "command_name": "set_thing_0",
            "payload": {"syntax": "set_thing_0 -density value"},
            "state": "candidate",
            "reject_info": {},
        }
    ])
    service.catalog.finish_job(job["id"], "cancelled", failure_reason=CANCELLED_MESSAGE)
    assert service.catalog.candidate_counts(job["id"])["candidate"] == 1

    with pytest.raises(CatalogPendingCandidates) as excinfo:
        service.start(notebook.id, "s1")
    assert excinfo.value.pending == 1

    service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="tester")
    second = service.start(notebook.id, "s1")
    assert second["status"] == "queued"


# ------------------------------------------- R5 P2: overflow counts transmitted
def test_api_candidates_transmit_desc_overflow(http):
    """`reject_info["desc_overflow"]` was written to the row by
    `_merge_entry`/`_reject_info` all along, but `CommandCatalogCandidate.of()`
    (the pydantic transmission model) dropped it — the review UI had no way
    to know a parameter description had been cut short by the row's aggregate
    budget. Drives it through the REAL HTTP `/candidates` endpoint, not the
    raw service dict, so this pins the model's field rather than the store's
    (which `test_model_authored_argument_descriptions_are_capped_per_arg_and_
    per_row` above already covers)."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    per_row = MODEL_ARG_DESC_TOTAL_CHARS // MODEL_ARG_DESC_CHARS  # 20 args fit
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=per_row + 10),
    )

    def verbose_descriptions(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt) or "set_thing_0",
                "syntax": "",
                "description": "",
                "args": [
                    {
                        "name": name,
                        "required": False,
                        "desc": "d" * (MODEL_ARG_DESC_CHARS + 500),
                        "default": "",
                    }
                    for name in _requested_params(prompt)
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(verbose_descriptions))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    job = client.post(base).json()["job"]
    _await_terminal(repo, job["id"])

    page = client.get(f"{base}/candidates", params={"job_id": job["id"], "state": "candidate"})
    assert page.status_code == 200, page.text
    row = page.json()["items"][0]
    assert row["desc_overflow"] == 10
    assert row["rejections_overflow"] == 0


def test_api_candidates_transmit_rejections_overflow(http):
    """Same gap, the other overflow axis: `reject_info["overflow"]` (per-row
    field-rejection ledger) was capped and reported at `MAX_SECTION_REJECTIONS`
    by the store (see `test_reject_info_is_bounded_across_a_multi_slice_
    commands_slices` above) but dropped by the response model before this
    fix. Same 200-parameter every-arg-invented shape, driven through HTTP."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=200),
    )

    def every_arg_invented(prompt, _call):
        return json.dumps(
            {
                "command_name": _command_of(prompt),
                "syntax": "",
                "description": "",
                "args": [
                    {
                        "name": f"-not_a_real_flag_{i}",
                        "required": False,
                        "desc": "",
                        "default": "",
                    }
                    for i in range(20)
                ],
                "examples": [],
            }
        )

    bind_chat_client(repo, "kg_extract", _Client(every_arg_invented))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    job = client.post(base).json()["job"]
    _await_terminal(repo, job["id"])

    page = client.get(f"{base}/candidates", params={"job_id": job["id"], "state": "candidate"})
    assert page.status_code == 200, page.text
    row = page.json()["items"][0]
    assert row["rejections_overflow"] == 400 - MAX_SECTION_REJECTIONS
    assert row["desc_overflow"] == 0


def test_api_apply_refuses_a_broken_table_with_a_user_readable_400(http):
    """F3: the route hands `user_error()` the CURATED
    `APPLY_TABLE_SHAPE_MESSAGE` constant, not `str(exc)` — same precedent as
    `CatalogModelUnavailable`'s 409 above."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    table = repo.get_knowhow_table(first["table_id"])
    command_column = next(c["id"] for c in table["columns"] if c["name"] == "命令")
    repo.delete_knowhow_column(command_column)

    second_service, second_job = _run_ok(repo, notebook.id, "s1", 1)
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/apply",
        params={"job_id": second_job["id"]},
        json={"all_pending": True},
    )
    assert response.status_code == 400
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == APPLY_TABLE_SHAPE_MESSAGE


def test_api_rejects_a_source_from_another_notebook(http):
    client, repo = http
    first = repo.create_notebook(NotebookCreate(name="a"))
    second = repo.create_notebook(NotebookCreate(name="b"))
    _add_manual(repo, first.id, "s1", 2)
    response = client.get(
        f"/api/notebooks/{second.id}/sources/s1/command-catalog/preview"
    )
    assert response.status_code == 404


def test_api_apply_without_a_selection_is_a_user_readable_400(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/apply",
        json={"candidate_ids": [], "all_pending": False},
    )
    assert response.status_code == 400
    assert response.headers.get("X-User-Message") == "1"


def test_api_apply_with_too_many_explicit_candidates_is_a_user_readable_422(http):
    """An explicit selection wider than `MAX_APPLY_CANDIDATES` used to be
    silently truncated to a page by the store (`candidates_by_ids`'s own
    `[:limit]` slice) — reporting `rows_added` for whichever subset happened
    to survive dedup-and-slice, with no signal that anything was dropped. It
    must instead be refused outright with a user-readable 422."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/apply",
        json={
            "candidate_ids": [f"cand-{i}" for i in range(MAX_APPLY_CANDIDATES + 1)],
            "all_pending": False,
        },
    )
    assert response.status_code == 422
    assert response.headers.get("X-User-Message") == "1"
    assert str(MAX_APPLY_CANDIDATES) in response.json()["detail"]


def test_api_apply_with_both_scopes_is_a_user_readable_422_and_writes_nothing(http):
    """R13 (codex PR #412 评审第 13 轮 P2): `all_pending=true` alongside a
    non-empty `candidate_ids` used to silently prefer `all_pending` — a
    WIDER write than the caller explicitly enumerated, with no signal that
    `candidate_ids` was ever read. It must be refused before anything is
    read or written, not resolved by picking one of the two."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    before = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    first_id = before["items"][0]["id"]

    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/apply",
        json={"candidate_ids": [first_id], "all_pending": True},
    )
    assert response.status_code == 422
    assert response.headers.get("X-User-Message") == "1"

    # Zero writes: no table was created, and every candidate — including the
    # one explicitly named — is still `candidate`, not `applied`.
    assert repo.list_knowhow_tables(notebook.id) == []
    after = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert {item["id"] for item in after["items"]} == {
        item["id"] for item in before["items"]
    }


# ----------------------------------------------------------------- API dismiss
def test_api_dismiss_clears_the_pending_guard_so_a_new_run_is_allowed(http):
    """R7's core end-to-end assertion. The guard's own copy has always said
    "confirm OR dismiss" (`pending_candidates_message`,
    `catalogPendingReviewNote`); this proves that promise is real: a source
    blocked by unreviewed candidates becomes startable again purely by
    dismissing them through this endpoint, no apply involved.
    """
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))

    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)

    # The guard is live before any dismiss: 2 candidates are still unreviewed.
    blocked = client.post(base)
    assert blocked.status_code == 409, blocked.text
    assert blocked.headers.get("X-User-Message") == "1"

    dismissed = client.post(
        f"{base}/dismiss", params={"job_id": job_id}, json={"all_pending": True}
    )
    assert dismissed.status_code == 200, dismissed.text
    assert len(dismissed.json()["dismissed"]) == 2
    assert dismissed.json()["pending_remaining"] == 0

    page = client.get(
        f"{base}/candidates", params={"job_id": job_id, "state": "dismissed"}
    )
    assert page.json()["counts"]["dismissed"] == 2
    assert all(
        item["dismiss_reason"] == "user_dismissed" for item in page.json()["items"]
    )

    # The guard is now clear — a fresh run on the SAME source is allowed.
    second = client.post(base)
    assert second.status_code == 200, second.text


def test_api_dismiss_a_selection_marks_only_those_candidates(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)

    page = client.get(f"{base}/candidates", params={"job_id": job_id}).json()
    first_id = page["items"][0]["id"]

    response = client.post(
        f"{base}/dismiss",
        params={"job_id": job_id},
        json={"candidate_ids": [first_id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["dismissed"] == [first_id]
    assert response.json()["pending_remaining"] == 2

    remaining = client.get(f"{base}/candidates", params={"job_id": job_id}).json()
    assert remaining["counts"]["candidate"] == 2
    assert first_id not in {item["id"] for item in remaining["items"]}


def test_api_dismiss_without_a_selection_is_a_user_readable_400(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/dismiss",
        json={"candidate_ids": [], "all_pending": False},
    )
    assert response.status_code == 400
    assert response.headers.get("X-User-Message") == "1"


def test_api_dismiss_with_too_many_explicit_candidates_is_a_user_readable_422(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/dismiss",
        json={
            "candidate_ids": [f"cand-{i}" for i in range(MAX_APPLY_CANDIDATES + 1)],
            "all_pending": False,
        },
    )
    assert response.status_code == 422
    assert response.headers.get("X-User-Message") == "1"
    assert str(MAX_APPLY_CANDIDATES) in response.json()["detail"]


def test_api_dismiss_with_both_scopes_is_a_user_readable_422_and_writes_nothing(http):
    """R13: mirrors the apply-side dual-scope refusal — same constant, same
    reason (see that test's docstring)."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    before = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    first_id = before["items"][0]["id"]

    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/dismiss",
        json={"candidate_ids": [first_id], "all_pending": True},
    )
    assert response.status_code == 422
    assert response.headers.get("X-User-Message") == "1"

    after = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert {item["id"] for item in after["items"]} == {
        item["id"] for item in before["items"]
    }


def test_api_dismiss_with_an_unknown_job_id_is_404(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    response = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog/dismiss",
        params={"job_id": "cjb-does-not-exist"},
        json={"all_pending": True},
    )
    assert response.status_code == 404


# ------------------------------------- R8: source generation + parse readiness
def _reparse(repo, notebook_id: str, source_id: str, elements: list[dict], when: str):
    """Swap a source's elements the way the parse pipeline does.

    Goes through the REAL ``replace_elements`` rather than hand-rolled SQL,
    because that method is the whole reason the element generation is a usable
    signal: it deletes every row and re-inserts the batch under ONE
    ``created_at``, in the same write transaction that advances
    ``sources.updated_at``. A fixture that wrote its own INSERTs would still
    pass while silently no longer exercising the writer being tracked.
    """
    from app.repositories.ports import SourceElementWrite

    with repo._write() as db:
        repo._runtime.source_store.replace_elements(
            db,
            source_id,
            [
                SourceElementWrite(
                    id=element["id"],
                    element_type=element["element_type"],
                    location_label="p1",
                    text=element["text"],
                    metadata={"section_path": element["section_path"]},
                )
                for element in elements
            ],
            created_at=when,
        )


LATER = "2026-08-01T00:00:00+08:00"


def test_source_generation_is_recorded_at_start_and_tracks_replace_elements(repo):
    """The two halves of the binding, proven separately: `start` snapshots the
    live element generation onto the job row, and the live token really does
    move when — and only when — `replace_elements` swaps the elements."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    stored = service.catalog.get_job(job["id"])["source_generation"]
    assert stored == NOW
    assert service.catalog.source_element_generation("s1") == NOW

    # A lifecycle write that does NOT touch the elements must not move it —
    # this is exactly why `sources.updated_at` was rejected as the signal.
    # (The two columns `set_status` writes, spelled out here because the facade
    # does not expose it: a re-extraction really does move `updated_at`.)
    repo._runtime.source_store.set_status("s1", "extracting")
    assert repo.get_source("s1").parse_status == "extracting"
    assert service.catalog.source_element_generation("s1") == NOW

    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 2), LATER)
    assert service.catalog.source_element_generation("s1") == LATER


def test_apply_after_a_reparse_is_refused_and_expires_the_candidates(repo):
    """The core of R8. The candidates name commands, excerpts and section paths
    taken from elements that no longer exist; confirming them would write
    content the document does not contain."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)
    assert service.catalog.candidate_counts(job["id"])["candidate"] == 3

    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 3), LATER)

    with pytest.raises(CatalogSourceChanged):
        service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="t")

    # Nothing was written...
    assert repo.list_knowhow_tables(notebook.id) == []
    # ...and the dead candidates were expired with a recorded reason, so the
    # restart guard is released by the same call that refused the confirm.
    counts = service.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 0
    assert counts["dismissed"] == 3
    dismissed = service.catalog.list_candidates(
        job["id"], state="dismissed", cursor=0, limit=10
    )
    assert {row["reject_info"]["reason"] for row in dismissed} == {"source_reparsed"}


def test_dismiss_after_a_reparse_is_refused_with_the_honest_reason(repo):
    """Dismiss never writes a row, so it is not refused to protect a table —
    it is refused so the whole set dies under `source_reparsed` instead of
    being cleared one page at a time under `user_dismissed`."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 2), LATER)

    with pytest.raises(CatalogSourceChanged):
        service.dismiss(notebook.id, "s1", job["id"], all_pending=True)
    dismissed = service.catalog.list_candidates(
        job["id"], state="dismissed", cursor=0, limit=10
    )
    assert {row["reject_info"]["reason"] for row in dismissed} == {"source_reparsed"}


def test_a_reparse_lets_the_next_run_start_and_clears_the_stale_candidates(repo):
    """Without this escape hatch the R5/R6 restart guard and the R8 staleness
    guard deadlock each other: every confirm is refused because the candidates
    are stale, and every re-run is refused because the candidates are
    unreviewed. A reparse is precisely when a user wants to re-run."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, first = _run_ok(repo, notebook.id, "s1", 2)
    assert service.catalog.candidate_counts(first["id"])["candidate"] == 2

    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 2), LATER)

    second = service.start(notebook.id, "s1")
    assert second["id"] != first["id"]
    assert second["source_generation"] == LATER
    stale = service.catalog.candidate_counts(first["id"])
    assert stale["candidate"] == 0
    assert stale["dismissed"] == 2


def test_unreviewed_candidates_still_block_a_restart_without_a_reparse(repo):
    """The unchanged path, pinned next to the escape hatch: same guard, same
    counts, still a `CatalogPendingCandidates` when the source did NOT change.
    Without this, widening the sweep by accident would look like a pass."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, first = _run_ok(repo, notebook.id, "s1", 2)

    with pytest.raises(CatalogPendingCandidates) as excinfo:
        service.start(notebook.id, "s1")
    assert excinfo.value.pending == 2
    assert service.catalog.candidate_counts(first["id"])["candidate"] == 2


def test_api_apply_after_a_reparse_is_a_user_readable_409(http):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)

    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 2), LATER)

    response = client.post(
        f"{base}/apply", params={"job_id": job_id}, json={"all_pending": True}
    )
    assert response.status_code == 409
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == SOURCE_STALE_MESSAGE

    dismissed = client.post(
        f"{base}/dismiss", params={"job_id": job_id}, json={"all_pending": True}
    )
    assert dismissed.status_code == 409
    assert dismissed.json()["detail"] == SOURCE_STALE_MESSAGE

    # The 409 released the restart guard rather than trapping the source.
    restarted = client.post(base)
    assert restarted.status_code == 200, restarted.text


@pytest.mark.parametrize("parse_status", ["queued", "parsing", "uploaded", "metadata-only"])
def test_api_start_and_preview_refuse_a_source_that_is_not_parsed_yet(
    http, parse_status
):
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET parse_status=? WHERE id=?", (parse_status, "s1")
        )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    started = client.post(base)
    assert started.status_code == 409
    assert started.headers.get("X-User-Message") == "1"
    assert started.json()["detail"] == SOURCE_NOT_PARSED_MESSAGE
    # Nothing was queued — the refusal happens before `create_job`.
    assert repo.command_catalog.latest_job("s1") is None

    preview = client.get(f"{base}/preview")
    assert preview.status_code == 409
    assert preview.json()["detail"] == SOURCE_NOT_PARSED_MESSAGE


def test_api_start_and_preview_refuse_a_source_whose_parse_failed(http):
    """`failed` gets its own copy because the remedy differs: waiting will
    never help, reparsing or re-uploading might."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    with repo._write() as db:
        db.execute("UPDATE sources SET parse_status='failed' WHERE id=?", ("s1",))
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    started = client.post(base)
    assert started.status_code == 409
    assert started.json()["detail"] == SOURCE_PARSE_FAILED_MESSAGE
    preview = client.get(f"{base}/preview")
    assert preview.status_code == 409
    assert preview.json()["detail"] == SOURCE_PARSE_FAILED_MESSAGE


@pytest.mark.parametrize("parse_status", ["parsed", "extracting", "extracted"])
def test_api_start_accepts_every_status_that_means_the_elements_have_landed(
    http, parse_status
):
    """The whitelist is the repository-wide one. `extracting`/`extracted` are
    KG-extraction stages that happen AFTER parsing, so refusing them would
    block catalog extraction on a source whose elements are complete."""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET parse_status=? WHERE id=?", (parse_status, "s1")
        )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    started = client.post(
        f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    )
    assert started.status_code == 200, started.text
    _await_terminal(repo, started.json()["job"]["id"])


# ------------------------------------------- R10: the source parse barrier
#
# R8 put the source-generation guard inside the per-TARGET lock. That
# serializes confirms against each OTHER, but `replace_elements` — the one
# writer that swaps a source's elements — runs under a completely different
# mutex (`SourceIngestionService`'s per-source chunk lock), so check and write
# were still a TOCTOU pair: generation reads current, the reparse commits,
# `append_knowhow_rows` then lands rows describing sections the document no
# longer has. These tests pin the barrier that closes it.
class _BlockingKnowhow:
    """The knowhow store, with a controllable stall inside the ONE call that
    actually lands rows.

    Everything else delegates untouched, so `apply` runs its real table
    resolution, anchor-shape check and bounded existence query — the stall is
    injected exactly at the instant a stale write would become irreversible.
    """

    def __init__(self, inner, reached: threading.Event, release: threading.Event):
        self._inner = inner
        self._reached = reached
        self._release = release

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def append_knowhow_rows(self, *args, **kwargs):
        self._reached.set()
        assert self._release.wait(timeout=10), "apply was never released"
        return self._inner.append_knowhow_rows(*args, **kwargs)


def _reparse_the_way_the_pipeline_does(repo, notebook_id, source_id, elements, when):
    """`_reparse`, but under the SAME per-source lock `process_source` holds.

    That lock is the whole point: production never calls `replace_elements`
    for an existing document source outside it (see `process_source`, which
    holds it continuously from the element swap through `build_chunks`), so a
    fixture that swapped elements without it would be testing a race that
    cannot happen and missing the one that can.
    """
    ingestion = repo._runtime.source_ingestion
    with ingestion.hold_source_chunk_lock(source_id):
        _reparse(repo, notebook_id, source_id, elements, when)


def test_apply_holds_the_parse_barrier_across_its_write(repo):
    """The regression this closes: a reparse landing between the generation
    check and `append_knowhow_rows`.

    The reparse is launched at the exact instant `apply` is inside its row
    write. With the barrier held, that thread cannot even begin its element
    swap — proven twice over, structurally (it is still blocked) and by
    observation (the live generation the write is landing against is still the
    one the job recorded). Without the barrier both assertions fail: the swap
    completes while the write waits, and the rows land describing a document
    that no longer exists.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)

    reached, release = threading.Event(), threading.Event()
    service.knowhow = _BlockingKnowhow(service.knowhow, reached, release)

    outcome: dict = {}

    def confirm():
        try:
            outcome["result"] = service.apply(
                notebook.id, "s1", job["id"], all_pending=True, actor="tester"
            )
        except BaseException as exc:  # noqa: BLE001 - reported through the dict
            outcome["error"] = exc

    applier = threading.Thread(target=confirm, name="apply")
    applier.start()
    assert reached.wait(timeout=10), "apply never reached its row write"

    reparser = threading.Thread(
        target=_reparse_the_way_the_pipeline_does,
        args=(repo, notebook.id, "s1", _manual_elements("s1", 3), LATER),
        name="reparse",
    )
    reparser.start()
    # The barrier assertion. `apply` is parked inside its write holding the
    # per-source lock, so this thread is stuck on `hold_source_chunk_lock` and
    # CANNOT make progress no matter how long we wait — the join below can
    # only ever time out. (Deleting the barrier makes it finish immediately,
    # which is the mutation this line catches.)
    reparser.join(timeout=0.4)
    assert reparser.is_alive(), (
        "a reparse was able to swap the elements while apply was mid-write — "
        "the per-source parse barrier is not being held"
    )
    # ...and the same fact stated as data rather than as scheduling: the write
    # in flight is landing against the generation the job was built on.
    assert service.catalog.source_element_generation("s1") == NOW

    release.set()
    applier.join(timeout=10)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"]["rows_added"] == 3

    reparser.join(timeout=10)
    assert not reparser.is_alive()
    assert service.catalog.source_element_generation("s1") == LATER

    # The rows that landed are the pre-reparse ones, written exactly once.
    table = repo.get_knowhow_table(outcome["result"]["table_id"])
    anchor = table["columns"][0]["id"]
    names = sorted(row["cells"].get(anchor, "") for row in table["rows"])
    assert names == [f"set_thing_{index}" for index in range(3)]
    # And the NEXT confirm — now genuinely stale — is refused by the R8 guard.
    with pytest.raises(CatalogSourceChanged):
        service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="t")


def _hold_the_barrier(repo, source_id: str):
    """Occupy the source's parse barrier from another thread, the way a live
    reparse would. Returns (stop_event, thread) — the caller must set and join.
    """
    holding, stop = threading.Event(), threading.Event()

    def hold():
        with repo._runtime.source_ingestion.hold_source_chunk_lock(source_id):
            holding.set()
            stop.wait(timeout=30)

    thread = threading.Thread(target=hold, name="reparse-holder")
    thread.start()
    assert holding.wait(timeout=10), "barrier holder never acquired"
    return stop, thread


def test_apply_during_an_in_flight_reparse_refuses_without_expiring_anything(
    repo, monkeypatch
):
    """A reparse is in flight but has not swapped anything yet.

    `CatalogSourceChanged` would be two lies at once here: the elements have
    NOT changed, and its sweep would destroy a reviewable candidate set that a
    parse failing before `replace_elements` would leave perfectly valid. So the
    refusal is its own exception, and the candidates are untouched.
    """
    # The wait length is not what makes the write safe (the barrier is); it
    # only decides how long a user sits before being told. Shortened here so
    # the suite does not pay the production value.
    monkeypatch.setattr(catalog_job, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    stop, holder = _hold_the_barrier(repo, "s1")
    try:
        with pytest.raises(CatalogSourceBusy):
            service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="t")
    finally:
        stop.set()
        holder.join(timeout=10)

    assert repo.list_knowhow_tables(notebook.id) == []
    counts = service.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 2
    assert counts.get("dismissed", 0) == 0
    # Nothing was consumed, so the confirm works the moment the barrier frees.
    assert service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="t"
    )["rows_added"] == 2


def test_dismiss_during_an_in_flight_reparse_refuses_without_expiring_anything(
    repo, monkeypatch
):
    """Dismiss writes no table row, but its R8 sweep IS a write: unbarriered it
    could record `user_dismissed` on a set that in fact died of
    `source_reparsed`, mislabelling the very reason the 「已跳过」 tab shows."""
    monkeypatch.setattr(catalog_job, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    stop, holder = _hold_the_barrier(repo, "s1")
    try:
        with pytest.raises(CatalogSourceBusy):
            service.dismiss(notebook.id, "s1", job["id"], all_pending=True)
    finally:
        stop.set()
        holder.join(timeout=10)

    assert service.catalog.candidate_counts(job["id"])["candidate"] == 2


# ------------------------------- R12: the parse-stage window before the lock
def test_apply_refuses_while_the_parse_stage_precedes_the_barrier(repo):
    """The regression this closes: `process_source` marks the source
    `parsing` as the very first thing it does — long before it ever reaches
    `hold_source_chunk_lock`. A real reparse's parse stage (MinerU) can run
    for minutes in that window, entirely outside the barrier's reach, and the
    elements have not been swapped yet either, so `_require_current_generation`
    sees an unchanged generation and lets a confirm straight through. Neither
    existing guard sees this window; `_require_not_parsing` is the one that
    does.

    Set directly via `set_source_status`, deliberately WITHOUT
    `hold_source_chunk_lock` — that absence IS the scenario: proving the
    barrier alone (already covered by the tests above) would not catch this.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    repo._runtime.source_ingestion.set_source_status("s1", "parsing")

    with pytest.raises(CatalogSourceBusy):
        service.apply(notebook.id, "s1", job["id"], all_pending=True, actor="t")

    # Nothing was written and nothing was expired — same R10 principle: a
    # parse that fails before `replace_elements` leaves every candidate
    # perfectly valid, so this is a wait, not a destructive refusal.
    assert repo.list_knowhow_tables(notebook.id) == []
    counts = service.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 2
    assert counts.get("dismissed", 0) == 0

    # Once the parse genuinely settles, the SAME job's confirm works.
    repo._runtime.source_ingestion.set_source_status("s1", "extracted")
    assert service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="t"
    )["rows_added"] == 2


def test_dismiss_refuses_while_the_parse_stage_precedes_the_barrier(repo):
    """Mirrors the apply case above. Dismiss writes no document content, but
    its own `mark_candidates_dismissed` write would mislabel these candidates
    `user_dismissed` when a reparse — already in flight, just not yet at the
    chunk lock — may be about to invalidate them for `source_reparsed`
    instead. Same guard, same reason R10 gives for taking the barrier here."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    repo._runtime.source_ingestion.set_source_status("s1", "parsing")

    with pytest.raises(CatalogSourceBusy):
        service.dismiss(notebook.id, "s1", job["id"], all_pending=True)

    assert service.catalog.candidate_counts(job["id"])["candidate"] == 2
    assert service.catalog.candidate_counts(job["id"]).get("dismissed", 0) == 0


def test_apply_rereads_parse_status_inside_the_lock_not_before_it(repo):
    """Pins WHERE `_require_not_parsing` must read from: freshly, inside the
    same locked window as the generation check — never the `source` `apply`
    fetched earlier to compute the lock key. That earlier read happens before
    either lock is taken, so it can go stale for as long as the target lock
    stays contended.

    Proven with genuine lock contention rather than a same-thread ordering
    assumption: another holder occupies the per-target lock while the source
    is still `extracted` (so `apply`'s own pre-lock read sees `extracted`,
    not `parsing`), the status flips to `parsing` while `apply` sits queued
    on that lock, and only a FRESH read taken once the lock is finally
    acquired can see it. A version of the guard that captured `parse_status`
    at the top of `apply` (before the lock) would read the same STALE
    `extracted` here and let the write through — this is the scenario that
    distinguishes the two.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)

    lock_key = service._target_lock_key(notebook.id)
    target_lock = service._apply_lock(lock_key)
    assert target_lock.acquire(timeout=5)

    outcome: dict = {}

    def confirm():
        try:
            outcome["result"] = service.apply(
                notebook.id, "s1", job["id"], all_pending=True, actor="t"
            )
        except BaseException as exc:  # noqa: BLE001 - reported through the dict
            outcome["error"] = exc

    applier = threading.Thread(target=confirm, name="apply")
    applier.start()
    # Let the thread fetch its pre-lock `source` copy (status still
    # `extracted`) and block trying to acquire the target lock we hold. It
    # must still be alive and empty-handed here — proof it is genuinely
    # queued on the lock, not racing the status flip below.
    applier.join(timeout=0.3)
    assert applier.is_alive() and not outcome, (
        "apply finished before the target lock was released — this test is "
        "not exercising contention on that lock"
    )
    repo._runtime.source_ingestion.set_source_status("s1", "parsing")
    target_lock.release()
    applier.join(timeout=10)

    assert isinstance(outcome.get("error"), CatalogSourceBusy), outcome
    assert repo.list_knowhow_tables(notebook.id) == []
    counts = service.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 2


# "failed" 放行是刻意的:解析失败若在元素替换前,候选仍接地且代次匹配;
# 若在替换后,代次校验(同一持锁窗口)自会拦——阻塞 failed 既过度又让
# 「正在解析中」文案撒谎。start/preview 的白名单(_require_parsed)不含 failed,不变。
@pytest.mark.parametrize("status", ["parsed", "extracting", "extracted", "failed"])
def test_apply_allows_every_already_parsed_status(repo, status):
    """The whitelist `_require_not_parsing` checks is `PARSED_SOURCE_STATUSES`
    — the same one `_require_parsed` uses — so none of its three members are
    mistaken for an in-flight reparse, only `parsing` (and anything else
    outside it) is."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    repo._runtime.source_ingestion.set_source_status("s1", status)
    assert service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="t"
    )["rows_added"] == 2


# "failed" 放行是刻意的:解析失败若在元素替换前,候选仍接地且代次匹配;
# 若在替换后,代次校验(同一持锁窗口)自会拦——阻塞 failed 既过度又让
# 「正在解析中」文案撒谎。start/preview 的白名单(_require_parsed)不含 failed,不变。
@pytest.mark.parametrize("status", ["parsed", "extracting", "extracted", "failed"])
def test_dismiss_allows_every_already_parsed_status(repo, status):
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    repo._runtime.source_ingestion.set_source_status("s1", status)
    result = service.dismiss(notebook.id, "s1", job["id"], all_pending=True)
    assert len(result["dismissed"]) == 2


def test_a_runtime_without_ingestion_still_confirms(repo):
    """The unwired barrier yields straight through, and that is correct rather
    than a hole: the only writer it defends against lives on the very service
    that is absent, so such a runtime cannot reparse anything at all. Pinned
    because the alternative reading — "no barrier, refuse everything" — would
    break every read-only composition root."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    service.source_locks = None
    assert service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="t"
    )["rows_added"] == 2


def test_api_apply_during_an_in_flight_reparse_is_a_user_readable_409(
    http, monkeypatch
):
    """Distinct copy from the stale 409: nothing was expired, so 「请重新识别」
    would send the user at a restart the pending-candidates guard still
    blocks. The wording names the parse instead."""
    monkeypatch.setattr(catalog_job, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)

    stop, holder = _hold_the_barrier(repo, "s1")
    try:
        response = client.post(
            f"{base}/apply", params={"job_id": job_id}, json={"all_pending": True}
        )
        dismissed = client.post(
            f"{base}/dismiss", params={"job_id": job_id}, json={"all_pending": True}
        )
    finally:
        stop.set()
        holder.join(timeout=10)

    assert response.status_code == 409
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == SOURCE_BUSY_MESSAGE
    assert dismissed.status_code == 409
    assert dismissed.headers.get("X-User-Message") == "1"
    assert dismissed.json()["detail"] == SOURCE_BUSY_MESSAGE
    assert response.json()["detail"] != SOURCE_STALE_MESSAGE


def test_api_dismiss_during_an_in_flight_reparse_is_a_user_readable_409(
    http, monkeypatch
):
    """Same contract from the dismiss endpoint's own entry point, so removing
    the barrier from `dismiss` alone cannot pass on `apply`'s coverage."""
    monkeypatch.setattr(catalog_job, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"
    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)

    stop, holder = _hold_the_barrier(repo, "s1")
    try:
        response = client.post(
            f"{base}/dismiss", params={"job_id": job_id}, json={"all_pending": True}
        )
    finally:
        stop.set()
        holder.join(timeout=10)

    assert response.status_code == 409
    assert response.json()["detail"] == SOURCE_BUSY_MESSAGE
    # Untouched: a busy refusal never consumes the reviewer's work.
    assert _service(repo).catalog.candidate_counts(job_id)["candidate"] == 2
