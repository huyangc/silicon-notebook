"""命令目录抽取的 job / 持久化 / API(方案 C·C1b)。

钉的是这批改动里**只要被后来的人顺手改掉就会造成静默损失**的那几条:

1. **迁移与单飞守卫** —— 全新库建表、已部署库(user_version=37)补建、条件唯一索引
   同时覆盖 queued 与 running。守卫漏 queued,重复 POST 就会在「行已建、线程未起」
   窗口里排出两个写同一份候选的 job。
2. **终态纪律** —— 正常、取消、熔断、`BaseException` 四条退出路径都必须把行落成终态。
   `KeyboardInterrupt`/`SystemExit` 继承 `BaseException`,`except Exception` 接不到;
   行留在 queued/running 就会把这个来源永久挡在守卫上,而离线进程无权自清。
3. **熔断双轴** —— 处理 ≥10 个**实际发过调用的窗口**后,命令名整条否决率 >20% 或
   args 保留率 <50% 就必须把 job 判失败并给用户可读理由,绝不静默产出近空目录。
   跳过的窗口不进这个样本:一份大部分是散文的手册不该因为「散文很多」被判失败。
4. **length/空 content 两条重试** —— 前者二分再问,后者重试一次后记账;两者都不许
   把「模型没给出可用回答」吞成「这一节本来就没有命令」。
5. **apply 的保守合并** —— 同名命令一律不改行,只回报 conflict。覆盖用户手工编辑
   过的内容是这批里唯一不可逆的破坏。

三条变异(熔断阈值恒不触发 / 删掉 BaseException 兜底 / 删掉 conflict 保护)都实测
过会让对应用例报红,记录在本任务的交付报告里。

v2(全文档窗口抽取)改动的几何 —— 规则分节退役,整篇按字符切成 `WINDOW_CHARS`
窗口,一次回答列举窗口里的**所有**命令。本文件因此多钉四条:

A. **跳过窗零调用、但照常推进进度** —— 没有可认领命令名、也没有 flag 的窗口不发
   模型调用;它仍必须计入 `sections_total` 并 `sections_done` 加一,否则进度条永远
   走不完(或者分母在撒谎)。
B. **跨窗合并一命令一行** —— 参数表跨窗口边界时,后一个窗口**改写**前一个窗口写下
   的那一行,而不是给同一个命令再追加一行;被拦下的条目永远只追加。
C. **`entries: []` 是合法回答** —— 「这段文本里没有命令」不是「模型没给出可用回答」,
   不进熔断的第三轴;而**缺少 `entries` 列表**的回答仍按 malformed 处置。
D. **接力(carry)** —— 续窗没有自己的命令名,只能认领上一窗传下来的名字;prompt 必须
   把两份名单分开列,否则模型无从把孤立的参数键到正确的命令上。

除 A 的「跳过窗也推进 carry」一条外(见 `_run_windows` 里的注释:按构造不可观测),
上述每条都做过删除+移动变异验证,记录在交付报告里。

C1b 双评审修复补充覆盖(同样都做过变异验证,见交付报告):

6. **飞行中取消不判 failed** —— 一次模型调用中途被取消(`AskCancelled`)必须落
   `cancelled`,不能掠过归类掉进 `except Exception` 判成「请稍后重试」。
7. **preview 的两个数字来源不同** —— 窗口数是对来源**全文字符数**的算术(一次 SQL
   聚合,不读正文),调用数只能在有界前缀里精确测量、前缀之外每窗按 1 次计。
   (v1 的形状检测 `detect_manual_shape` 随规则分节一起退役。)
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

R19(codex PR #412 R19 评审 P2)修复补充覆盖(变异验证见交付报告):

19. **抽取 worker 对来源/笔记本删除的探活** —— `catalog_jobs`/`catalog_candidates`
    都是 `ON DELETE CASCADE`,但删除不会设置 worker 自己进程内的取消事件(没人调
    `cancel()`)。没有探活,worker 会一直付费做模型调用,直到下一次偶然读回那行——
    而那次读回本身(`run()` 里为 `_emit` 取的 `self.catalog.get_job(job_id)`)会在
    `except` 分支内部再抛一次 `KeyError`,未捕获。`_raise_if_stopped` 在既有取消
    检查的**同一位置**(含 `_call`——每次模型调用、每次二分/补救重试唯一必经的
    choke point)加一次有界点查,行消失即抛专属的 `CatalogJobGone`(不与
    `CatalogCancelled` 共用处理分支,原因见该类型自己的 docstring)。`_settle`
    对已删行的容忍与「探活触发的停止不发事件」是两件分开的事:前者是
    `finish_job` 本就无操作之外再加一次「已删 vs 已终态」的区分性日志,后者是
    `run()` 的 `except CatalogJobGone` 干脆不取行、不 `_emit`。

R20(codex PR #412 R20 评审 P2)修复补充覆盖(变异验证见交付报告):

20. **继承目标的归属校验按 id 直接点查,不再按标题回查** —— R18 的
    `_inherit_applied_table` 用 `knowhow_table_id_by_title(notebook_id, title)
    == candidate_table_id` 判断继承目标属于当前笔记本,但标题不唯一:被改名的
    候选表若撞上**同一笔记本内、更早创建**的另一张同名表,`knowhow_
    table_id_by_title` 按其自身文档写明的创建时间 tie-break 会解析到那张更早
    的表而不是候选表,等值判断失败——合法的继承目标被误拒,`_inherit_applied_
    table` 落回按标题 create-or-find,凭空多分叉出第三张表,正是这个方法本该
    防止的「静默分叉」被它自己的归属校验复活。新增 `KnowhowStorePort.
    knowhow_table_notebook_id`(有界主键点查,契约与相邻的 `knowhow_table_
    title` 一致)让归属校验直接读候选表自己的 `notebook_id` 列,标题相同与否
    不再参与判断。
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
    MAX_CANDIDATES,
    MAX_WINDOW_REJECTIONS,
    MIN_WINDOWS_BEFORE_ALERT,
    WINDOW_CHARS,
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


# Filler that is prose and nothing else: no `_`/`.`-joined identifier, no
# flag-shaped token, no sentence punctuation an identifier scanner could latch
# onto. Its only job is to occupy characters, because v2's geometry is
# character-driven — "one command per window" is a statement about SIZE now,
# not about headings.
_WINDOW_FILLER_UNIT = "filler prose for window sizing "


def _filler(size: int) -> str:
    repeats = size // len(_WINDOW_FILLER_UNIT) + 1
    return (_WINDOW_FILLER_UNIT * repeats)[:size]


# One filler paragraph per window by default. Element COUNT is what costs
# real time in this file — every element is re-scanned by `window_candidates`
# (twice per window: once by the run loop, once by the gate) and inserted
# row-by-row — so fixtures pay for small elements only where small elements are
# the point, which is `preview` (it clips each element at
# `PREVIEW_ELEMENT_CHARS`, so one 12k paragraph would make every preview test
# `sampled=True` and its prefix analysis measure nothing).
_BIG_CHUNK = WINDOW_CHARS
_PREVIEW_CHUNK = 900


def _pad_to_window(texts: list[str], *, chunk: int = _BIG_CHUNK) -> list[str]:
    """Append filler PARAGRAPHS until `texts` fill exactly one window.

    Two properties, and both are load-bearing:

    * **exactly** one window. The packer is greedy, so a window left even 15
      characters short pulls the NEXT command's heading into it — and a fixture
      that says "12 commands" then quietly produces windows serving two
      candidate names each reads like a packing bug when it is a fixture bug.
    * **many small elements** rather than one huge one. `preview` clips each
      element at `PREVIEW_ELEMENT_CHARS` (1200), so a fixture that fills a
      window with a single 12k paragraph is `sampled=True` for every preview
      test and its prefix analysis measures nothing. Real manuals are made of
      ordinary-sized paragraphs; so is this.
    """
    padded = list(texts)
    total = sum(len(text) for text in padded) + max(0, len(padded) - 1)
    while WINDOW_CHARS - total > 1:
        size = min(max(1, chunk), WINDOW_CHARS - total - 1)
        padded.append(_filler(size))
        total += 1 + size
    if WINDOW_CHARS - total == 1:
        padded[-1] = padded[-1] + "x"
    return padded


def _window_elements(
    source_id: str,
    offset: int,
    heading: str,
    body: str,
    section_path: str,
    *,
    first_type: str = "heading",
    chunk: int = _BIG_CHUNK,
) -> list[dict]:
    """One command's (or one prose section's) elements, filling one window.

    `first_type="paragraph"` builds a window with no heading at all — a
    continuation window, which is what most of a long options table looks
    like. `chunk` trades element COUNT against element SIZE: the default keeps
    every element under `PREVIEW_ELEMENT_CHARS` (so preview tests see unclipped
    text), while a large chunk keeps the element count under
    `MAX_ANCHOR_ELEMENTS` (so anchor-union assertions have room to observe
    anything).
    """
    texts = _pad_to_window([heading, body], chunk=chunk)
    return [
        {
            "id": f"el-{source_id}-{offset + index + 1:04d}",
            "element_type": first_type if index == 0 else "paragraph",
            "text": text,
            "section_path": section_path,
        }
        for index, text in enumerate(texts)
    ]


def _relay_manual(source_id: str) -> list[dict]:
    """One command documented across THREE windows.

    Window 0 is the command itself (heading, usage line, `-density`). Windows
    1 and 2 are continuations: prose and more parameters, with no heading, no
    usage line and the command's name nowhere in them. Neither can claim
    anything without the relay, and between them they hold most of what the
    command's documentation actually says — which is what a long options table
    looks like on a real manual.

    Three contributing windows rather than two, deliberately: with only two,
    the merge would still produce the right row if the registry forgot to carry
    the MERGED accumulator forward (window 2 would seed from window 0 and add
    its own). The third window is what makes that mistake visible.
    """
    name = "set_thing_0"
    elements = _window_elements(
        source_id, 0, name,
        f"{name} -density value\n\n- `-density` the density option",
        name,
    )
    elements.extend(
        _window_elements(
            source_id, len(elements),
            "这是一段普通的说明文字", "- `-site` the site option", "",
            first_type="paragraph",
        )
    )
    continuation = "\n".join(
        f"- `-{flag}` the {flag} option" for flag in _FLAGS[1:4]
    )
    elements.extend(
        _window_elements(
            source_id, len(elements), continuation, "", "",
            first_type="paragraph",
        )
    )
    return elements


def _manual_elements(
    source_id: str,
    commands: int,
    *,
    params: int = 3,
    per_window: bool = True,
    chunk: int = _BIG_CHUNK,
) -> list[dict]:
    """A command-reference shaped source: one identifier heading per command,
    each carrying a usage line and a flag-bearing parameter list.

    Element ids are zero-padded and strictly increasing because the real
    element reader orders ``BY id`` and relies on that being insertion order
    (see ``ChunkStore.source_elements_for_chunking``). A fixture that numbers
    them any other way silently reorders the document.

    `per_window=True` (the default) pads each command to fill one whole window,
    so this fixture keeps meaning what it used to mean under v1: N commands
    produce N model calls, each prompt serves exactly one candidate name, and
    the job's `sections_*` columns count N. `per_window=False` is the OTHER v2
    shape — several commands sharing one window, one call answering for all of
    them — and the tests that are about that say so explicitly.

    `params=0` means a genuinely FLAGLESS manual: no parameter list AND no flag
    on the usage line. The usage line used to carry `-density` even at
    `params=0`, which made "flagless" a fiction — `parameter_names` reads the
    whole window text, so those commands were assigned one parameter each and
    a model answering `args: []` was under-extracting, not answering correctly.
    """
    elements: list[dict] = []
    for index in range(commands):
        name = f"set_thing_{index}"
        flags = "\n".join(
            f"- `-{flag}` the {flag} option" for flag in _FLAGS[:params]
        )
        usage = f"{name} -{_FLAGS[0]} value" if params else f"{name} value"
        body = f"{usage}\n\n{flags}"
        if per_window:
            elements.extend(
                _window_elements(
                    source_id, len(elements), name, body, name, chunk=chunk
                )
            )
            continue
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
                "text": body,
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


def _prose_windows_then_commands(
    source_id: str, *, prose_windows: int, commands: int,
    chunk: int = _PREVIEW_CHUNK,
) -> list[dict]:
    """`prose_windows` windows of pure prose, then `commands` command windows.

    Prose first and commands after, deliberately: the relay
    (`carry_candidates`) hands a window's candidate names forward, so a prose
    window that FOLLOWS a command is legitimately still worth a call (it may
    hold that command's worked example). A prose window with nothing before it
    is the unambiguous case the zero-model-call gate exists for, and it is the
    one this fixture stages.

    Each prose window is padded to fill exactly one window for the same reason
    the command fixture is: v2's geometry is character-driven, so "one window"
    is a size, not a heading.
    """
    elements: list[dict] = []
    for index in range(prose_windows):
        title = f"背景第{index}节"
        elements.extend(
            _window_elements(
                source_id, len(elements), title, "这是一段普通的说明文字", title,
                chunk=chunk,
            )
        )
    for index in range(commands):
        name = f"set_thing_{index}"
        body = f"{name} -density value\n\n- `-density` the density option"
        elements.extend(
            _window_elements(
                source_id, len(elements), name, body, name, chunk=chunk
            )
        )
    return elements


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
    name to the reply; anything it returns is fed through the real grounding.

    **The one-entry shorthand.** v2's wire shape is `{"entries": [ ... ]}`, but
    the overwhelming majority of these tests are about ONE command's grounding
    (a dropped dash, a default that is not in the text, a cap on model prose),
    and rewriting sixty of them to wrap their single object in a list would
    have added noise to every one of them without testing anything new. So a
    reply that parses as a JSON object WITHOUT an `entries` key is wrapped into
    a one-entry list here, at the double.

    That convenience is exactly as dangerous as it sounds for the tests that
    are about the SHAPE ITSELF — a reply missing `entries` must be treated as
    malformed by production — so those pass ``wrap=False`` and speak the raw
    wire shape. Anything that is not a JSON object at all (a truncated string,
    prose) is passed through untouched in both modes; wrapping it would destroy
    the very failure the test is staging.
    """

    configured = True
    settings = None  # no ``settings`` -> cap_kwargs/response_validator stay off

    def __init__(self, answer, *, wrap: bool = True):
        self.answer = answer
        self.wrap = wrap
        self.calls: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        raw = self.answer(prompt, len(self.calls))
        if not self.wrap:
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(parsed, dict) or "entries" in parsed:
            return raw
        return json.dumps({"entries": [parsed]})


def _prompt_block(prompt: str, header: str) -> list[str]:
    """The `- name` bullets under `header`, up to the first line that is not
    one. Used to read back what the prompt actually SERVED, rather than
    assuming it."""
    names: list[str] = []
    started = False
    for line in prompt.splitlines():
        if not started:
            started = line.startswith(header)
            continue
        if line.startswith("- "):
            names.append(line[2:].strip())
        elif names:
            break
    return names


def _candidates_of(prompt: str) -> list[str]:
    return [
        name
        for name in _prompt_block(prompt, "Choose each command name from this list")
        if name != "(none)"
    ]


def _carried_of(prompt: str) -> list[str]:
    return _prompt_block(
        prompt, "Commands possibly continuing from earlier text"
    )


def _command_of(prompt: str) -> str:
    """The first command name the prompt's OWN candidate block serves.

    Still exact for `per_window=True` fixtures (one candidate per window), and
    deliberately reads the own-candidates block rather than any `- set_thing_`
    line: the relay block lists names too, and a reply claiming one of those
    would be testing something else entirely.
    """
    own = _candidates_of(prompt)
    return own[0] if own else ""


def _entry_for(name: str) -> dict:
    return {
        "command_name": name,
        "syntax": f"{name} -density value",
        "description": "does a thing",
        "args": [
            {"name": f"-{flag}", "required": False, "desc": "", "default": ""}
            for flag in _FLAGS[:3]
        ],
        "examples": [f"{name} -density 0.6"],
    }


def _relay_reply(prompt: str, _call: int) -> str:
    """Answer for whichever name the prompt served — own candidate, or relayed.

    Deliberately returns `syntax`/`description` only when the name is the
    window's OWN: a continuation window has no usage line, so a model claiming
    one there would be inventing it, and the merge's first-writer-wins is what
    has to preserve the real one.
    """
    own = _candidates_of(prompt)
    carried = _carried_of(prompt)
    name = (own or carried)[0]
    entry = {
        "command_name": name,
        "syntax": f"{name} -density value" if own else "",
        "description": "does a thing" if own else "",
        "args": [
            {"name": param, "required": False, "desc": "", "default": ""}
            for param in _requested_params(prompt)
        ],
        "examples": [],
    }
    return json.dumps({"entries": [entry]})


def _good_reply(prompt: str, _call: int) -> str:
    """One entry per command the prompt served — the v2 answer shape.

    On a `per_window=True` fixture that is one entry, exactly as v1's reply
    was; on a shared window it is the whole list, which is the behaviour that
    replaced sectioning.
    """
    return json.dumps(
        {"entries": [_entry_for(name) for name in _candidates_of(prompt)]}
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
    `MAX_WINDOW_REJECTIONS` and report the true overflow count rather than
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
    assert len(reject_info["fields"]) <= MAX_WINDOW_REJECTIONS
    assert len(reject_info["fields"]) == MAX_WINDOW_REJECTIONS
    assert reject_info["overflow"] == 400 - MAX_WINDOW_REJECTIONS
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


# ------------------------------------------------------------ v2: the geometry
def test_one_window_documenting_several_commands_is_one_call_and_several_rows(
    repo,
):
    """The change sectioning was replaced BY. Three commands small enough to
    share one window are one model call answering with three entries, and the
    catalog still holds one row per command."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3, per_window=False)
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert result["windows_total"] == 1
    assert len(client.calls) == 1
    assert _candidates_of(client.calls[0]) == [
        "set_thing_0", "set_thing_1", "set_thing_2"
    ]
    settled = service.catalog.get_job(job["id"])
    assert settled["sections_total"] == 1
    assert settled["entries"] == 3
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == [
        "set_thing_0", "set_thing_1", "set_thing_2"
    ]


def test_a_window_documenting_more_commands_than_one_list_holds_loses_none(
    repo,
):
    """Forty commands sharing one window, end to end — none of them dropped.

    The candidate list caps at `MAX_CANDIDATES`, and windows never overlap, so
    truncating that list is not "the rest are served later": under the cap the
    thirty-third command onwards was never in any prompt, in this run or any
    other, and nothing downstream could see it go (an entry can only be WRONG
    about a name it was offered). The window is split instead, which costs one
    extra call and loses nothing.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 40, per_window=False)
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=100)
    assert {row["command_name"] for row in page["items"]} == {
        f"set_thing_{index}" for index in range(40)
    }
    # Split, not truncated: no prompt over-served, and nothing was left over.
    assert result["windows_total"] > 1
    assert result["candidates_overflowed"] == 0
    assert all(
        len(_candidates_of(prompt)) <= MAX_CANDIDATES for prompt in client.calls
    )


def test_the_names_an_unsplittable_index_could_not_serve_are_reported(repo):
    """Where the split stops (a bare index of names, under the floor), the
    truncation comes back — and the run has to say so.

    It is the one loss with no other trace: the model is never asked about
    those names, so there is no rejection, no ratio movement and no report
    line. A count on the run ledger and on the finished event is the whole
    remedy, and it is worth more than the silence it replaces.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "命令索引",
        _numbered("s1", [
            {
                "element_type": "code_block",
                "text": "\n".join(f"cmd_{index:02d} -d" for index in range(40)),
                "section_path": "",
            }
        ]),
    )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    events: list[dict] = []
    service.event_log.emit = events.append  # type: ignore[assignment]
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert result["windows_total"] == 1
    assert result["candidates_overflowed"] == 40 - MAX_CANDIDATES
    finished = [
        event for event in events if event.get("kind") == "catalog_job_finished"
    ]
    assert finished[-1]["candidates_overflowed"] == 40 - MAX_CANDIDATES


def test_an_ordinary_run_reports_no_overflow_at_all(repo):
    """The counterweight: the field is absent from the event on every ordinary
    document, so its PRESENCE is the signal a reader can act on."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    events: list[dict] = []
    service.event_log.emit = events.append  # type: ignore[assignment]
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert result["candidates_overflowed"] == 0
    finished = [
        event for event in events if event.get("kind") == "catalog_job_finished"
    ]
    assert "candidates_overflowed" not in finished[-1]


def test_a_prose_window_costs_no_model_call_and_still_advances_progress(repo):
    """The zero-model-call gate, and the progress-bar half of it.

    Two prose windows and one command window: one call, but THREE windows ticked
    off. A denominator of 3 with a numerator that stops at 1 is a progress bar
    that never finishes, and a denominator of 1 is a cost estimate that lied.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "散文加命令",
        _prose_windows_then_commands("s1", prose_windows=2, commands=1),
    )
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert len(client.calls) == 1
    assert result["windows_total"] == 3
    assert result["windows_skipped"] == 2
    assert result["windows"] == 1  # windows that actually made a call
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["sections_total"] == 3
    assert settled["sections_done"] == 3
    assert settled["entries"] == 1


def test_the_relay_reaches_a_continuation_window_past_intervening_prose(repo):
    """A command, a prose window, then a window that is nothing but more
    parameters — no heading, no usage line, no command name anywhere in it.

    Without the relay that third window can claim nothing at all and its whole
    parameter table is lost, which on a real manual is most of a large
    command's documentation.

    The middle window is NOT skipped, and that is the relay's own documented
    cost rather than an oversight: a window following a command may well be
    that command's worked example or prose description, so a non-empty relay
    keeps the gate open. `window_needs_model`'s docstring owns that trade-off;
    this test pins that the run really behaves that way, which is also why the
    skip-gate tests stage their prose BEFORE any command.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "接力手册", _relay_manual("s1"))
    client = _Client(_relay_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert result["windows_total"] == 3
    assert result["windows_skipped"] == 0
    assert len(client.calls) == 3
    # The continuation window serves NO name of its own, and the relayed one
    # instead — which is the only reason its parameters can be attributed.
    assert _candidates_of(client.calls[2]) == []
    assert _carried_of(client.calls[2]) == ["set_thing_0"]
    assert service.catalog.get_job(job["id"])["status"] == "succeeded"


def test_a_command_split_across_windows_lands_in_one_row(repo):
    """One command, one row — even when its parameters arrive two windows
    apart. The second window revises the first window's row instead of
    appending a second row for the same name."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "接力手册", _relay_manual("s1"))
    bind_chat_client(repo, "kg_extract", _Client(_relay_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == ["set_thing_0"]
    payload = page["items"][0]["payload"]
    # The union of both windows' parameters, in first-seen order.
    assert [arg["name"] for arg in payload["args"]] == [
        "-density", "-site", "-pad_left", "-pad_right", "-layer"
    ]
    # First writer wins for the overview fields: only the first window had a
    # usage line to ground a syntax against, and the second must not blank it.
    assert payload["syntax"] == "set_thing_0 -density value"
    assert payload["description"] == "does a thing"
    # Anchors are the union of the windows the command was documented in,
    # still bounded; the excerpt stays the FIRST window's.
    assert len(payload["anchors"]) <= 12
    assert len(set(payload["anchors"])) > 1
    assert payload["excerpt"].startswith("set_thing_0")
    # The job's own entry tally counts ROWS added to the review queue, so a
    # merge must not inflate it.
    assert service.catalog.get_job(job["id"])["entries"] == 1


def test_a_later_windows_rejected_entry_never_merges_into_the_earlier_row(repo):
    """Only `candidate` rows merge. A rejected entry is appended, always — a
    grounding failure two windows later must not be able to rewrite the row a
    reviewer is already looking at."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "接力手册", _relay_manual("s1"))

    def first_good_then_hallucinated(prompt, call):
        if call == 1:
            return _relay_reply(prompt, call)
        return json.dumps(
            {"entries": [{"command_name": "totally_other_command", "args": []}]}
        )

    bind_chat_client(repo, "kg_extract", _Client(first_good_then_hallucinated))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == ["set_thing_0"]
    # Untouched by the second window: still exactly what window 0 grounded.
    assert [arg["name"] for arg in page["items"][0]["payload"]["args"]] == ["-density"]
    rejected = service.candidates_page(
        job["id"], state="rejected", cursor=0, limit=50
    )
    # One per hallucinating window (the prose window and the continuation
    # window), appended — never folded into the row that already exists.
    assert [row["command_name"] for row in rejected["items"]] == [
        "totally_other_command", "totally_other_command"
    ]


def _mention_then(source_id: str, *, documented: bool) -> list[dict]:
    """Window 0 only MENTIONS `set_thing_0`; window 1 either continues it or
    documents it properly.

    Window 0 is the shape `suspect_related` exists for: the name is in the
    text (inline code, so it is claimable) but in no heading and no usage
    line, which is what "possibly a cross-reference rather than the command
    this section documents" looks like.

    `documented=False` makes window 1 a continuation — a parameter table with
    no name of its own, claimable only through the relay. `documented=True`
    makes it the command's real section, heading and usage line included.
    """
    elements = _window_elements(
        source_id, 0,
        "背景说明",
        "The `set_thing_0` command is described in the vendor handbook.\n\n"
        "- `-density` the density option",
        "背景说明",
    )
    if documented:
        elements.extend(
            _window_elements(
                source_id, len(elements), "set_thing_0",
                "set_thing_0 -site value\n\n- `-site` the site option",
                "set_thing_0",
            )
        )
        return elements
    elements.extend(
        _window_elements(
            source_id, len(elements),
            "- `-site` the site option", "", "", first_type="paragraph",
        )
    )
    return elements


def _no_syntax_reply(prompt: str, _call: int) -> str:
    """Answer for whichever name the prompt served, claiming no syntax.

    The fixtures above stage windows that mention or continue a command
    without a usage line to ground a syntax against, so a model inventing one
    would only add a `not_contiguous` rejection to every assertion here.
    """
    name = (_candidates_of(prompt) or _carried_of(prompt))[0]
    return json.dumps(
        {
            "entries": [
                {
                    "command_name": name,
                    "description": "does a thing",
                    "args": [
                        {
                            "name": param,
                            "required": False,
                            "desc": "",
                            "default": "",
                        }
                        for param in _requested_params(prompt)
                    ],
                    "examples": [],
                }
            ]
        }
    )


def test_a_continuation_window_cannot_clear_an_earlier_suspect_mark(repo):
    """The merge folds `suspect_related` with AND, so any window that
    documents the command properly clears the mark. A RELAYED entry is not
    such a window.

    Its clean flag is the relay's exemption, not a finding: the continuation
    window holds a parameter table, no heading and no usage line, and it is
    not being asked whether the command is documented there. Letting that
    False win would erase the warning on exactly the shape the relay exists to
    produce — i.e. on every long options table in the book.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "只是提及", _mention_then("s1", documented=False)
    )
    bind_chat_client(repo, "kg_extract", _Client(_no_syntax_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == ["set_thing_0"]
    payload = page["items"][0]["payload"]
    # The continuation really did merge — this is one row, holding both
    # windows' parameters…
    assert [arg["name"] for arg in payload["args"]] == ["-density", "-site"]
    # …and it did NOT wash out the warning window 0 earned.
    assert payload["suspect_related"] is True


def test_a_later_window_that_documents_the_command_does_clear_the_mark(repo):
    """The other half, or the fix would just be "never clear it".

    A window with the command's own heading and usage line judged it on the
    merits, and its clean result is a finding — so the AND still does what it
    was written for.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "先提及后正文",
        _mention_then("s1", documented=True),
    )
    bind_chat_client(repo, "kg_extract", _Client(_no_syntax_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == ["set_thing_0"]
    payload = page["items"][0]["payload"]
    assert [arg["name"] for arg in payload["args"]] == ["-density", "-site"]
    assert payload["suspect_related"] is False


def test_a_candidate_row_from_a_headingless_document_gets_an_ordinal_label(repo):
    """`section_path` is a provenance LABEL, and a window boundary falls where
    the character budget put it — so a document with no heading anywhere has no
    breadcrumb to inherit and the row carries the window's ordinal instead of an
    empty string. (How that reaches the review panel is the frontend's call;
    this only pins that the column is never blank.)"""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "无标题手册",
        _numbered("s1", [
            {
                "element_type": "paragraph",
                "text": "set_thing_0 -density value\n\n- `-density` the option",
                "section_path": "",
            }
        ]),
    )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=10)
    assert page["items"][0]["command_name"] == "set_thing_0"
    assert page["items"][0]["section_path"] == "window 1"


def test_an_empty_entries_list_is_a_legal_answer_not_an_unusable_slice(repo):
    """`entries: []` means "this text documents no command", which is the
    honest answer for a window of prose that only got sent because a relayed
    name kept the gate open. It must not count toward the breaker's
    unusable-response axis — a prose-heavy manual would otherwise be killed for
    a model doing exactly as it was told."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    # A FLAGLESS manual, deliberately: with an assignment in hand, answering
    # `entries: []` twelve windows running is a model ignoring what it was
    # asked for, and the args axis is supposed to fire on exactly that (see
    # `test_the_args_axis_counts_what_was_asked_for_not_only_what_came_back`).
    # The claim here is narrower and is the one that matters for prose-heavy
    # manuals: an empty list is never an UNUSABLE response.
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 2, params=0)

    def one_command_then_nothing(prompt, call):
        # The first window names its command; every window after it answers
        # honestly that it documents none. One real entry is what keeps this
        # test about the unusable AXIS rather than about the wholly-empty run
        # (`test_a_run_that_produces_nothing_at_all_is_not_a_success`).
        if call == 1:
            return json.dumps(
                {"entries": [{"command_name": _command_of(prompt), "args": []}]}
            )
        return json.dumps({"entries": []})

    bind_chat_client(
        repo,
        "kg_extract",
        # `wrap=False`: this test is about the WIRE shape, so the double must
        # not be allowed to reshape it.
        _Client(one_command_then_nothing, wrap=False),
    )
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == 1
    # The claim: eleven `entries: []` answers produced no slice-failure rows
    # and did not open the breaker's unusable axis.
    assert settled["rejected"] == 0
    assert settled["sections_done"] == MIN_WINDOWS_BEFORE_ALERT + 2


def test_a_reply_without_an_entries_list_is_malformed_not_an_empty_answer(repo):
    """The other half of the same contract: a reply that never produced an
    `entries` LIST has not answered the question, and gets the malformed
    remedy (retry, then a recorded slice failure) rather than being read as
    "no commands here"."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    client = _Client(
        lambda prompt, call: json.dumps({"command_name": "set_thing_0"}),
        wrap=False,
    )
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == 0
    assert settled["rejected"] == 1
    page = service.candidates_page(job["id"], state="rejected", cursor=0, limit=50)
    assert page["items"][0]["reject_info"]["fields"][0]["reason"] == (
        "model_response_unusable"
    )


def test_skipped_windows_do_not_count_toward_the_breakers_sample(repo):
    """`MIN_WINDOWS_BEFORE_ALERT` counts windows that actually made a CALL.

    Nine command windows all rejecting, plus five prose windows that cost
    nothing: fourteen windows, but only nine ratios' worth of evidence. Letting
    the skipped ones fill the sample would fire the breaker on a document whose
    only sin is being mostly prose.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "散文加命令",
        _prose_windows_then_commands(
            "s1", prose_windows=5, commands=MIN_WINDOWS_BEFORE_ALERT - 1
        ),
    )
    bind_chat_client(
        repo,
        "kg_extract",
        _Client(lambda prompt, call: json.dumps(
            {"command_name": "not_a_documented_command", "args": []}
        )),
    )
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert result["windows"] == MIN_WINDOWS_BEFORE_ALERT - 1
    assert result["windows_skipped"] == 5
    assert settled["sections_done"] == MIN_WINDOWS_BEFORE_ALERT + 4


# ------------------------------------------- T2b: the two-condition cost gate
def _commands_then_prose(
    source_id: str, *, commands: int, prose_windows: int,
    chunk: int = _BIG_CHUNK,
) -> list[dict]:
    """The mirror of `_prose_windows_then_commands`: commands FIRST.

    This is the order the "any relay keeps the gate open" rule billed for. The
    relay never empties once a command has been seen, so every prose window
    after the manual's first command inherited a claimable name and was sent —
    on a real manual that is most of the book, paid for one window at a time.
    """
    elements: list[dict] = []
    for index in range(commands):
        name = f"set_thing_{index}"
        body = f"{name} -density value\n\n- `-density` the density option"
        elements.extend(
            _window_elements(
                source_id, len(elements), name, body, name, chunk=chunk
            )
        )
    for index in range(prose_windows):
        title = f"背景第{index}节"
        elements.extend(
            _window_elements(
                source_id, len(elements), title, "这是一段普通的说明文字", title,
                chunk=chunk,
            )
        )
    return elements


def test_prose_after_a_command_is_not_billed_for_the_rest_of_the_book(repo):
    """The half the earlier gate got wrong, and the expensive half.

    A relay is a NAME, not evidence there is anything in this window to
    extract, and it never empties once a command has been seen — so "carried
    keeps the gate open" charges a model call for every prose page after the
    manual's first command. The measured cost of the old rule is the whole
    back half of a document; its measured benefit is the occasional worked
    example. Prose windows AFTER a command are the shape that isolates it —
    the skip-gate tests that stage prose FIRST pass under either rule.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "命令加散文",
        _commands_then_prose("s1", commands=1, prose_windows=3),
    )
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert len(client.calls) == 1
    assert result["windows_total"] == 4
    assert result["windows_skipped"] == 3
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["sections_done"] == 4
    assert settled["entries"] == 1


def test_parameters_with_nothing_to_claim_cost_nothing_but_a_continuation_pays(
    repo,
):
    """Both flag-bearing shapes in one document, because the gate tells them
    apart by the RELAY and nothing else.

    Window 0 is an options table that opens the document: flags, but no name
    anywhere before it, so nothing a model returned could survive grounding —
    the served list would be empty and `validate_entry` vetoes every name off
    it. Window 1 documents a command. Window 2 is that command's table
    continuing past the boundary: the same flag-only shape, and now worth
    every cent, because the relay gives the model a name to key it onto.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    # Each of the three has to fill a window of its OWN — the packer is greedy,
    # so an orphan table left short of a full window simply swallows the
    # command's heading and the shape under test never occurs.
    orphan = _window_elements(
        "s1", 0, "| `-orphan_flag` | belongs to nobody |", "", "",
        first_type="paragraph",
    )
    command = _window_elements(
        "s1", len(orphan), "set_thing_0",
        "set_thing_0 -density value\n\n- `-density` the density option",
        "set_thing_0",
    )
    continuation = _window_elements(
        "s1", len(orphan) + len(command),
        "- `-pad_left` the pad_left option", "", "",
        first_type="paragraph",
    )
    _add_elements(
        repo, notebook.id, "s1", "孤儿表加命令", orphan + command + continuation
    )
    client = _Client(_relay_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert result["windows_total"] == 3
    # The opening table skipped, the command and its continuation billed.
    assert result["windows_skipped"] == 1
    assert len(client.calls) == 2
    assert _candidates_of(client.calls[0]) == ["set_thing_0"]
    # The continuation claims the relayed name — the case the gate must keep.
    assert _candidates_of(client.calls[1]) == []
    assert _carried_of(client.calls[1]) == ["set_thing_0"]


def test_a_skipped_window_neither_probes_the_job_row_nor_emits_an_event(repo):
    """A skipped window spends nothing and does nothing, so it must not pay
    for observing itself.

    The liveness probe exists to stop paying a model for a job whose row a
    cascade deleted, and `catalog_section_done` reports work that happened;
    on a skip there is neither. A mostly-prose PDF has thousands of these
    windows, and probe + event + `record_section` is three database round
    trips each where one is enough. `record_section` is the one that stays:
    the progress bar the frontend polls reads the job ROW, not the event
    stream.

    Asserted as a SHAPE (no event at all for a skipped window) and as a
    SCALING property (five times the prose costs no more probes than one),
    since the absolute probe count is an implementation detail and the growth
    is not.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    for source_id, prose in (("s1", 1), ("s2", 5)):
        _add_elements(
            repo, notebook.id, source_id, "散文加命令",
            _prose_windows_then_commands(
                source_id, prose_windows=prose, commands=1, chunk=_BIG_CHUNK
            ),
        )
    service = _service(repo)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))

    def run_counting(source_id: str) -> tuple[int, list[dict]]:
        probes = 0
        events: list[dict] = []
        real_get_job = service.catalog.get_job
        real_emit = service.event_log.emit

        def counting_get_job(job_id):
            nonlocal probes
            probes += 1
            return real_get_job(job_id)

        service.catalog.get_job = counting_get_job  # type: ignore[assignment]
        service.event_log.emit = events.append  # type: ignore[assignment]
        try:
            job = service.start(notebook.id, source_id)
            service.run(job["id"])
        finally:
            service.catalog.get_job = real_get_job  # type: ignore[assignment]
            service.event_log.emit = real_emit  # type: ignore[assignment]
        return probes, events

    one_probe_count, one_events = run_counting("s1")
    five_probe_count, five_events = run_counting("s2")

    def section_events(events):
        return [
            event for event in events
            if event.get("kind") == "catalog_section_done"
        ]

    # One event per window that actually made a call, in both runs — the
    # skipped windows contribute none, however many of them there are.
    assert len(section_events(one_events)) == 1
    assert len(section_events(five_events)) == 1
    assert five_probe_count == one_probe_count
    # …while the progress bar still counts every window.
    assert service.catalog.latest_job("s2")["sections_done"] == 6


def test_a_cancel_still_stops_a_run_inside_a_stretch_of_skipped_windows(repo):
    """Dropping the liveness probe from the skip path must not drop the
    CANCEL check with it: the in-process event is free to read and an owner
    who pressed stop is entitled to have the run end, even in the middle of a
    hundred prose windows that are individually costing nothing."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "散文加命令",
        _prose_windows_then_commands(
            "s1", prose_windows=4, commands=1, chunk=_BIG_CHUNK
        ),
    )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service._cancels[job["id"]].set()

    result = service.run(job["id"])

    assert result == {"job_id": job["id"], "cancelled": True}
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "cancelled"
    # Stopped on the FIRST window, not after walking every prose window.
    assert settled["sections_done"] == 0


# ------------------------------------------- T2b: the whole-window slice view
def _source_text_of(prompt: str) -> str:
    """What the prompt actually SHOWED the model, read back off the prompt.

    Everything between the `<<<` / `>>>` fence, so this reads the same string
    the model does — not `window.text`, which is what makes the assertion
    below a statement about the prompt rather than about the fixture.
    """
    body = prompt.split("<<<\n", 1)[1]
    return body.rsplit("\n>>>", 1)[0]


def test_every_command_the_prompt_offers_is_in_the_text_the_prompt_shows(repo):
    """The guard for the narrowed per-slice view, at the seam that matters.

    A name in the candidate list is a name the prompt invites the model to
    claim and `validate_entry` then demands verbatim in the window. Serving a
    name without showing where it lives leaves two outcomes — silence, or a
    fabrication that gets vetoed — and neither leaves a trace: a command that
    was never really offered produces no rejection and no ledger line, so the
    run looks clean while a third of the manual quietly does not exist.

    Thirty FLAGLESS commands in one window is the shape that made it visible:
    with no flag-shaped parameter anywhere, the old view was a 600-character
    head and nothing else, so roughly the first eighteen names were shown and
    the other twelve were offered into the dark.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 30, params=0, per_window=False)
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    assert client.calls, "the fixture must have produced a call"
    for prompt in client.calls:
        served = _candidates_of(prompt)
        assert len(served) >= 30
        shown = _source_text_of(prompt)
        for name in served:
            assert name in shown, f"{name} was offered but never shown"


# ------------------------------------------ T2b: the carried-only prompt shape
def test_a_window_with_its_own_candidates_lists_the_relay_separately(repo):
    """Two commands, one window each: the second window has a candidate of its
    own AND a relay, and the two must stay in separate blocks — the relay's
    whole value is telling the model which names are in the "documented
    earlier, continuing here" position."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    second = client.calls[1]
    assert _candidates_of(second) == ["set_thing_1"]
    assert _carried_of(second) == ["set_thing_0"]
    assert "(none)" not in second


def test_a_carried_only_window_serves_the_relay_as_the_claim_list(repo):
    """When the window names nothing itself, the relay IS the list of names
    that may be claimed, and it is rendered as such.

    The two-block form would print 「choose a name from this list: - (none)」
    above the relay block — an instruction to return nothing, handed to a model
    whose actual job on this window is to attribute an orphaned parameter table
    to the command in the block below. Continuation windows are most of every
    large command's documentation, so that reads as a wording detail and
    behaves as switching the feature off for them.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "接力手册", _relay_manual("s1"))
    client = _Client(_relay_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    continuation = client.calls[2]
    assert _candidates_of(continuation) == []
    assert _carried_of(continuation) == ["set_thing_0"]
    # No empty claim list anywhere in the prompt…
    assert "(none)" not in continuation
    # …and the relay block says outright that choosing from it is the task.
    assert "choose each command name from this list" in continuation
    # The relayed name really does land on the row it belongs to.
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == ["set_thing_0"]


# ------------------------------------- T2b: a run that produced nothing fails
def test_a_run_that_produces_nothing_at_all_is_not_a_success(repo):
    """`succeeded` next to an empty review panel asserts "this document
    documents no commands", and a run that paid for calls and got back not one
    attempted entry has no evidence for that claim. The honest terminal state
    is `failed`, with copy that says what to check."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    client = _Client(lambda prompt, call: json.dumps({"entries": []}), wrap=False)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert len(client.calls) == 2
    assert result["nothing_extracted"] is True
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["failure_reason"] == catalog_job.NOTHING_EXTRACTED_MESSAGE
    assert settled["diagnostic"] == "nothing_extracted"


def test_a_run_with_only_rejected_rows_still_succeeds(repo):
    """The boundary. Rejected rows ARE the answer to "why is the catalog
    empty" — a reviewer opens the 「已拦截」 tab and sees the command names the
    model claimed and the reason each was vetoed. A run that produced them
    delivered something, so it is not the silent-empty failure above."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    bind_chat_client(
        repo,
        "kg_extract",
        _Client(lambda prompt, call: json.dumps(
            {"command_name": "not_a_documented_command", "args": []}
        )),
    )
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert "nothing_extracted" not in result
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["entries"] == 0
    assert settled["rejected"] == 2


def test_a_run_that_skipped_every_window_still_succeeds(repo):
    """The other boundary, and the one that would break the cost gate's own
    success case: a source that is simply not a command manual never asks a
    model anything. Nothing was extracted because nothing was spent, which is
    the gate working, not a failure."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "纯散文",
        _prose_windows_then_commands(
            "s1", prose_windows=3, commands=0, chunk=_BIG_CHUNK
        ),
    )
    client = _Client(_good_reply)
    bind_chat_client(repo, "kg_extract", client)
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert client.calls == []
    assert result["windows"] == 0
    assert result["windows_skipped"] == 3
    assert "nothing_extracted" not in result
    settled = service.catalog.get_job(job["id"])
    assert settled["status"] == "succeeded"
    assert settled["sections_done"] == 3


# ------------------ T2b: a revision the reviewer already overtook, and blame
def test_a_merge_onto_an_already_reviewed_row_appends_instead_of_vanishing(repo):
    """`update_candidate_payload` writes only rows still awaiting review, so a
    continuation window's merge can be refused: between the window that wrote
    the row and the one revising it, a reviewer can have confirmed or skipped
    it, and both are terminal. Overwriting either would rewrite something a
    person already acted on.

    The parameters that window found exist nowhere else, so the fallback is
    v1's own behaviour — append a second row for the command, visible in the
    review queue. A duplicate a person can see and skip beats a silent loss.

    The registry then has to point at the NEW row, which the third window is
    what proves: without it, window 2 keeps retrying the same refused update
    and its parameters vanish too.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "接力手册", _relay_manual("s1"))
    service = _service(repo)

    def reviewed_after_the_first_window(prompt, call):
        if call == 2:
            # The reviewer skipped window 0's row between the two windows.
            with repo._write() as db:
                db.execute(
                    "UPDATE catalog_candidates SET state='dismissed' "
                    "WHERE state='candidate'"
                )
        return _relay_reply(prompt, call)

    bind_chat_client(repo, "kg_extract", _Client(reviewed_after_the_first_window))
    job = service.start(notebook.id, "s1")
    service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    # Exactly ONE live row — window 2 merged into window 1's replacement
    # rather than appending a third.
    assert [row["command_name"] for row in page["items"]] == ["set_thing_0"]
    args = [arg["name"] for arg in page["items"][0]["payload"]["args"]]
    # Seeded from what the dismissed row held, plus both later windows'.
    assert args == ["-density", "-site", "-pad_left", "-pad_right", "-layer"]
    dismissed = service.candidates_page(
        job["id"], state="dismissed", cursor=0, limit=50
    )
    assert [row["command_name"] for row in dismissed["items"]] == ["set_thing_0"]


def test_an_unanswered_parameter_is_blamed_on_the_only_command_in_the_window(
    repo,
):
    """One accepted command means no ambiguity: the parameter it was asked
    about and never returned is a gap in ITS row, and that is where a reviewer
    should see it."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1, per_window=False)

    def names_only(prompt, _call):
        return json.dumps(
            {"entries": [
                {"command_name": name, "args": []}
                for name in _candidates_of(prompt)
            ]}
        )

    bind_chat_client(repo, "kg_extract", _Client(names_only, wrap=False))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    assert result["args_uncovered"] == 3
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    reasons = [
        field["reason"] for field in page["items"][0]["reject_info"]["fields"]
    ]
    assert reasons == ["arg_not_returned"] * 3


def test_a_multi_command_window_does_not_blame_every_command_for_one_gap(repo):
    """With several commands in the window the assignment is the WINDOW's
    whole flag list, so an unanswered parameter says a parameter went missing,
    not WHICH command lost it — the model keys parameters onto entries, and a
    parameter nobody returned was keyed onto nothing.

    Writing the note onto each accepted command (as this used to) marks up
    commands that answered everything they were asked for: the reviewer reads
    「missing -density」 on a command that never took it. The window-level
    ledger records the fact regardless, so declining to name a culprit loses
    nothing.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2, per_window=False)

    def names_only(prompt, _call):
        return json.dumps(
            {"entries": [
                {"command_name": name, "args": []}
                for name in _candidates_of(prompt)
            ]}
        )

    bind_chat_client(repo, "kg_extract", _Client(names_only, wrap=False))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    result = service.run(job["id"])

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert [row["command_name"] for row in page["items"]] == [
        "set_thing_0", "set_thing_1"
    ]
    for row in page["items"]:
        assert row["reject_info"].get("fields", []) == []
    # …and the run's own ledger still knows the parameters went unanswered.
    assert result["args_uncovered"] == 3


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
        # The real wire shape, spelled out: the validator is handed raw model
        # content, never the `_Client` double's one-entry shorthand.
        return json.dumps(
            {"entries": [{"command_name": "set_dont_use", "args": args_field}]}
        )

    # The correct flagless answer (no parameters at all) still admits.
    assert validator(reply([])) is True
    # A structurally malformed `args` must not be indistinguishable from that
    # correct empty answer.
    assert validator(reply({"name": "lib_cells"})) is False
    # v2 shape checks, at the same seam: a reply that is not an entries LIST
    # has not answered the question that was asked, and an entries list that
    # grounds nothing must not be frozen for the cache TTL.
    assert validator(json.dumps({"command_name": "set_dont_use"})) is False
    assert validator(json.dumps({"entries": []})) is False
    assert validator(json.dumps({"entries": ["set_dont_use"]})) is False


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
        # Raw wire shape — the validator never sees the double's shorthand.
        return json.dumps(
            {
                "entries": [
                    {
                        "command_name": "set_thing_0",
                        "args": [
                            {"name": name, "required": False, "desc": "",
                             "default": ""}
                            for name in names
                        ],
                    }
                ]
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
    assert validator(json.dumps({"entries": [{"command_name": "nope", "args": []}]})) is False
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
    """The slice's assignment, read back off the prompt.

    Matched by PREFIX rather than by the whole line: v2 appends "keying each
    one onto the command it belongs to" (a window can document several
    commands), and a fixture that silently returns `[]` when that sentence
    changes would make every assignment-shaped test pass by answering nothing.
    """
    lines = prompt.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("Extract ONLY these parameters")
        ),
        -1,
    )
    if start < 0:
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
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 2)
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
    assert settled["sections_done"] == MIN_WINDOWS_BEFORE_ALERT


def test_circuit_opens_on_the_args_axis_even_with_clean_command_names(repo):
    """The second axis is not redundant: every command name is right here and
    every parameter is invented, which the name axis cannot see."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 2)

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
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 5)
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
    assert settled["sections_done"] == MIN_WINDOWS_BEFORE_ALERT


def test_the_args_axis_counts_what_was_asked_for_not_only_what_came_back(repo):
    """R2 P1: the keep ratio's denominator is `args_seen + args_uncovered`.

    Every parameter this model returns is correct, so on the old denominator
    the run scores a perfect 1.0 keep rate — while answering one of six
    assigned parameters in every section. A ratio a model can raise by
    answering LESS cannot detect the failure it exists to detect, and this run
    (12 sections of 1/6) must open the breaker on the args axis.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 2, params=6)

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
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 2, params=0)

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
    assert settled["entries"] == MIN_WINDOWS_BEFORE_ALERT + 2


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
    _add_manual(repo, notebook.id, "s1", MIN_WINDOWS_BEFORE_ALERT + 2, params=4)
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
    assert settled["entries"] == MIN_WINDOWS_BEFORE_ALERT + 2

    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=50)
    assert len(page["items"]) == MIN_WINDOWS_BEFORE_ALERT + 2
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
def test_preview_counts_windows_from_the_sources_total_text(repo):
    """`estimated_windows` is arithmetic over the source's whole text, so it
    must be right for a document far larger than the prefix the preview reads.

    Six commands, each padded to fill exactly one window, is six windows — and
    the preview has to say six without reading six windows' worth of text.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 6, chunk=_PREVIEW_CHUNK)
    preview = _service(repo).preview(notebook.id, "s1")
    assert preview.estimated_windows == 6
    assert preview.estimated_calls == 6
    assert preview.skipped_windows_in_prefix == 0
    assert preview.sampled is False


def test_preview_of_an_empty_source_estimates_nothing(repo):
    """Zero characters is zero windows — not one. A source with no elements
    must not be quoted a price."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(repo, notebook.id, "s1", "空来源", [])
    preview = _service(repo).preview(notebook.id, "s1")
    assert preview.estimated_windows == 0
    assert preview.estimated_calls == 0
    assert preview.skipped_windows_in_prefix == 0


def test_preview_reports_the_windows_the_cost_gate_would_skip(repo):
    """The number that explains a cheap estimate on a big document.

    Two prose windows and one command window: only the command window is worth
    a call, and `skipped_windows_in_prefix` is what keeps "3 windows, 1 call"
    from reading as a miscount.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "散文加命令",
        _prose_windows_then_commands("s1", prose_windows=2, commands=1),
    )
    preview = _service(repo).preview(notebook.id, "s1")
    assert preview.estimated_windows == 3
    assert preview.skipped_windows_in_prefix == 2
    assert preview.estimated_calls == 1
    assert preview.sampled is False


def test_preview_declares_sampled_and_charges_unread_windows_one_call_each(
    repo, monkeypatch
):
    """The row cap and the per-element clip are two different bounds, and both
    feed `sampled`. Past the prefix the gate cannot be evaluated at all, so
    every unread window is charged the MINIMUM of one call — never zero, which
    is the one direction a cost estimate must not err in."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 6, chunk=_PREVIEW_CHUNK)
    service = _service(repo)
    assert service.preview(notebook.id, "s1").sampled is False

    monkeypatch.setattr("app.services.catalog_job.PREVIEW_ELEMENT_LIMIT", 4)
    clipped = service.preview(notebook.id, "s1")
    assert clipped.sampled is True
    # The window count is unaffected: it comes from the SQL aggregate, not
    # from the prefix.
    assert clipped.estimated_windows == 6
    assert clipped.estimated_calls == 6


def test_preview_still_declares_sampled_when_only_an_element_was_clipped(
    repo, monkeypatch
):
    """Per-element clipping distorts harder than the row cap: a clipped options
    table loses parameter names, which loses slices, so the estimate comes back
    too low. If only the row cap fed `sampled`, that estimate would be
    presented as a census."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2, params=6, chunk=_PREVIEW_CHUNK)
    service = _service(repo)
    assert service.preview(notebook.id, "s1").sampled is False

    monkeypatch.setattr("app.services.catalog_job.PREVIEW_ELEMENT_CHARS", 20)
    clipped = service.preview(notebook.id, "s1")
    assert clipped.sampled is True
    # The row cap was never reached — only the character bound bit — and the
    # window count still comes from the untruncated SQL total.
    assert clipped.estimated_windows == 2


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


def test_job_row_deleted_mid_run_stops_further_model_calls(repo):
    """codex PR #412 R19 P2: a source or notebook can be deleted while an
    extraction job is mid-run (`catalog_jobs`/`catalog_candidates` are both
    `ON DELETE CASCADE`), and nothing sets the worker's own cancel event —
    `cancel()` is never called for a delete. Without a liveness probe before
    every model call, the worker just keeps paying for calls whose output
    has nowhere left to land, until the next place it happens to read the
    row back — which used to be `_emit`'s own `self.catalog.get_job(job_id)`
    argument, raising `KeyError` uncaught deep inside an `except` clause.

    Two slices (`param_count=40`) so the delete, landing right after slice
    1's call, is caught by the SAME per-slice checkpoint
    `raise_if_catalog_cancelled` already used — before slice 2's call, and
    before this section's `add_candidates` (which only runs once, after ALL
    of a section's slices are done) ever gets a chance to write a row for a
    job that no longer exists.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "大参数手册",
        _large_param_manual("s1", param_count=40),
    )
    service = _service(repo)
    job_ref: dict[str, str] = {}

    def delete_after_first_call(prompt, call):
        if call == 1:
            with repo._write() as db:
                db.execute(
                    "DELETE FROM catalog_candidates WHERE job_id=?",
                    (job_ref["id"],),
                )
                db.execute(
                    "DELETE FROM catalog_jobs WHERE id=?", (job_ref["id"],)
                )
        return _good_reply(prompt, call)

    client = _Client(delete_after_first_call)
    bind_chat_client(repo, "kg_extract", client)
    job = service.start(notebook.id, "s1")
    job_ref["id"] = job["id"]
    result = service.run(job["id"])

    assert len(client.calls) == 1, "slice 2's call must never have happened"
    assert result == {"job_id": job["id"], "job_deleted": True}
    with pytest.raises(KeyError):
        service.catalog.get_job(job["id"])
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM catalog_candidates WHERE source_id=?", ("s1",)
        ).fetchone()[0]
    assert remaining == 0, "no candidate row may be written after the delete"
    # The worker thread ended normally — no uncaught exception, and no stale
    # guard row left behind for the source (there is no row left at all).
    assert service.catalog.active_job("s1") is None


def test_settle_tolerates_a_job_row_thats_already_gone(repo):
    """`_settle` is the single choke point every exit path in `run()` writes
    a terminal status through — including the new `CatalogJobGone` branch,
    which settles through it as a documented no-op. Any settle attempt can
    in principle land after the row is already gone (a concurrent delete
    finishing between the liveness probe and the settle write), and it must
    be tolerated the exact same way: quietly, without raising, so the worker
    thread's `finally` (which releases the in-process cancel-event entry)
    still runs."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    with repo._write() as db:
        db.execute("DELETE FROM catalog_candidates WHERE job_id=?", (job["id"],))
        db.execute("DELETE FROM catalog_jobs WHERE id=?", (job["id"],))

    settled = service._settle(job["id"], "failed", "内部消息", "diag")

    assert settled is False  # nothing to write, and it must not raise


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


def _provenance_cells(repo, table_id) -> list[str]:
    table = repo.get_knowhow_table(table_id)
    column = next(
        item["id"] for item in table["columns"] if item["name"] == "出处"
    )
    return [row["cells"][column] for row in table["rows"]]


def test_the_applied_provenance_keeps_a_breadcrumb_and_drops_the_ordinal(repo):
    """The 「出处」 column is where the candidate row's INTERNAL label stops
    being internal.

    On the review panel that label is transient and the frontend translates the
    ordinal form; here it is written into a knowhow table, where a person keeps
    it, reads it months later and sees it in the graph. `window 3` says nothing
    there — it names a boundary the character budget put somewhere in a
    document, in a word that is not even the product's vocabulary — so the
    ordinal form degrades to the source name alone. A real breadcrumb is kept:
    that one genuinely says where in the document the command lives.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    # A heading-bearing manual (breadcrumb) and a heading-less one (ordinal).
    _add_manual(repo, notebook.id, "s1", 1)
    _add_elements(
        repo, notebook.id, "s2", "无标题手册",
        _numbered("s2", [
            {
                "element_type": "paragraph",
                "text": "set_thing_0 -density value\n\n- `-density` the option",
                "section_path": "",
            }
        ]),
    )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)

    applied = {}
    for source_id in ("s1", "s2"):
        job = service.start(notebook.id, source_id)
        service.run(job["id"])
        applied[source_id] = service.apply(
            notebook.id, source_id, job["id"], all_pending=True, actor="tester"
        )
    with_heading, without = applied["s1"], applied["s2"]

    assert _provenance_cells(repo, with_heading["table_id"]) == [
        "OpenROAD 手册 · set_thing_0"
    ]
    # The row still carries the internal label for the review panel…
    page = service.candidates_page(
        service.catalog.latest_job("s2")["id"],
        state="applied", cursor=0, limit=10,
    )
    assert page["items"][0]["section_path"] == "window 1"
    # …and the knowhow cell a person keeps does not.
    assert _provenance_cells(repo, without["table_id"]) == ["无标题手册"]


def test_a_real_breadcrumb_that_merely_starts_like_the_ordinal_label_survives(
    repo,
):
    """The match is anchored, deliberately. A manual whose own section is
    called 「window 3 configuration」 has a real breadcrumb that happens to
    open with the same two words, and dropping it would lose provenance the
    reader wants. Only a label that is EXACTLY the internal form is it."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_elements(
        repo, notebook.id, "s1", "窗口手册",
        _numbered("s1", [
            {
                "element_type": "heading",
                "text": "window 3 configuration",
                "section_path": "window 3 configuration",
            },
            {
                "element_type": "paragraph",
                "text": "set_thing_0 -density value\n\n- `-density` the option",
                "section_path": "window 3 configuration",
            },
        ]),
    )
    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    service = _service(repo)
    job = service.start(notebook.id, "s1")
    service.run(job["id"])
    applied = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )

    assert _provenance_cells(repo, applied["table_id"]) == [
        "窗口手册 · window 3 configuration"
    ]


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
    # R15: `table_title` must report the table's REAL current title (still
    # 「OpenROAD 手册」 — it did not move), never a freshly re-derived guess
    # from the now-changed canonical source title. Getting this wrong here
    # would be the same class of lie the frontend prediction fix (R15) exists
    # to remove — just reintroduced on the backend's own response.
    assert second["table_title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"

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
    # R15: no table exists, so there is nothing to name — an empty string,
    # not a hypothetical derived title for a table that was never written.
    assert result["table_title"] == ""
    assert repo.list_knowhow_tables(notebook.id) == []


def test_apply_with_nothing_nameable_reports_the_real_title_of_a_remembered_table(repo):
    """R15: the same no-op branch as
    `test_apply_with_nothing_nameable_does_not_conjure_an_empty_table`, but
    for a job whose FIRST apply already landed a table — a second apply that
    adds nothing (e.g. a page of pure conflicts/rejections) must still report
    that table's REAL title, through the same `applied_table_id`-first path
    `_resolve_target_table` uses, not a freshly re-derived guess that a
    mid-job paper-metadata backfill could have made stale."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert first["rows_added"] == 1
    table_id = first["table_id"]

    _mark_grounded_paper(repo, notebook.id, "s1", "A Later-Grounded Title")

    # Nothing left to confirm for this job — the one candidate was already
    # applied — so this call takes the "nothing nameable" branch.
    second = service.apply(
        notebook.id, "s1", job["id"], all_pending=True, actor="tester"
    )
    assert second["rows_added"] == 0
    assert second["table_id"] == table_id
    assert second["table_title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"


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


# ------------------- R16 P2: apply's anchor check must be atomic with the write
def test_append_knowhow_rows_skipping_existing_anchors_store_semantics(repo):
    """Direct store-level coverage of the new atomic write port
    (``KnowhowStorePort.append_knowhow_rows_skipping_existing_anchors``):
    only a row whose anchor value has NEITHER an existing row NOR an earlier
    duplicate within the SAME batch is inserted; everything else is reported
    back in ``skipped_anchor_values`` and left untouched. Exactly one change
    entry is recorded, covering only the rows that actually landed — not the
    skipped ones."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    table_id = repo.create_knowhow_table(
        notebook.id,
        "命令目录：测试",
        "",
        [dict(column) for column in CATALOG_TABLE_COLUMNS],
        created_by="tester",
        actor="tester",
    )
    table = repo.get_knowhow_table(table_id)
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    store = repo.command_catalog.knowhow

    store.add_knowhow_row(table_id, {anchor: "set_thing_0"}, actor="tester")
    before_changes = len(repo.list_knowhow_changes(table_id))

    result = store.append_knowhow_rows_skipping_existing_anchors(
        table_id,
        anchor,
        [
            {anchor: "set_thing_0"},  # already has a row -> skipped
            {anchor: "set_thing_1"},  # new -> inserted
            {anchor: "set_thing_1"},  # duplicate WITHIN this batch -> skipped
            {anchor: " set_thing_2 "},  # incidental whitespace, still inserted
        ],
        actor="tester",
        origin="import",
    )
    assert result["skipped_anchor_values"] == {"set_thing_0", "set_thing_1"}
    assert set(result["row_ids"]) == {"set_thing_1", "set_thing_2"}

    # Cell content is stored verbatim (untrimmed) exactly like
    # `append_knowhow_rows` — js-trim is a MATCHING normalization, not a
    # storage one — so the padded name keeps its incidental whitespace (a
    # leading space sorts before any letter, hence its position here).
    after = repo.get_knowhow_table(table_id)
    names = sorted(row["cells"].get(anchor, "") for row in after["rows"])
    assert names == [" set_thing_2 ", "set_thing_0", "set_thing_1"]

    # One new change entry for the two rows that landed — not four.
    changes = repo.list_knowhow_changes(table_id)
    assert len(changes) == before_changes + 1
    latest = changes[0]
    assert latest["kind"] == "import_append"
    assert len(latest["payload"]["rows"]) == 2

    # A batch that skips EVERY row (all already present/duplicated) is a
    # silent no-op: no new row, no new change entry — mirrors
    # `append_knowhow_rows`'s own empty-batch convention.
    before_changes = len(repo.list_knowhow_changes(table_id))
    noop = store.append_knowhow_rows_skipping_existing_anchors(
        table_id, anchor, [{anchor: "set_thing_0"}], actor="tester", origin="import"
    )
    assert noop == {"row_ids": {}, "skipped_anchor_values": {"set_thing_0"}}
    assert len(repo.list_knowhow_changes(table_id)) == before_changes
    assert len(repo.get_knowhow_table(table_id)["rows"]) == 3

    # A blank anchor value has nothing to collide with — always inserted.
    blank_result = store.append_knowhow_rows_skipping_existing_anchors(
        table_id, anchor, [{anchor: ""}], actor="tester", origin="import"
    )
    assert blank_result["skipped_anchor_values"] == set()
    assert len(blank_result["row_ids"]) == 0  # blank isn't a keyable anchor value
    assert len(repo.get_knowhow_table(table_id)["rows"]) == 4

    # Empty batch: no transaction, no history.
    before_changes = len(repo.list_knowhow_changes(table_id))
    empty = store.append_knowhow_rows_skipping_existing_anchors(
        table_id, anchor, [], actor="tester", origin="import"
    )
    assert empty == {"row_ids": {}, "skipped_anchor_values": set()}
    assert len(repo.list_knowhow_changes(table_id)) == before_changes


def test_append_knowhow_rows_skipping_existing_anchors_requires_the_live_anchor_column(
    repo,
):
    """``anchor_column_id`` is re-verified as the table's CURRENT anchor
    column under the same lock, mirroring the guarded-write anchor check —
    not trusted from a caller's earlier read. A column that is not (or is no
    longer) the anchor raises, same as `apply`'s own shape guard."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    table_id = repo.create_knowhow_table(
        notebook.id,
        "命令目录：测试",
        "",
        [dict(column) for column in CATALOG_TABLE_COLUMNS],
        created_by="tester",
        actor="tester",
    )
    table = repo.get_knowhow_table(table_id)
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    attribute = next(c["id"] for c in table["columns"] if c["role"] != "anchor")
    store = repo.command_catalog.knowhow

    with pytest.raises(ValueError):
        store.append_knowhow_rows_skipping_existing_anchors(
            table_id, attribute, [{anchor: "set_thing_0"}], actor="tester",
        )

    # The anchor moves off this column entirely.
    repo.set_knowhow_anchor_column(table_id, None, actor="tester")
    with pytest.raises(ValueError):
        store.append_knowhow_rows_skipping_existing_anchors(
            table_id, anchor, [{anchor: "set_thing_0"}], actor="tester",
        )

    # Nothing was written by either failed call.
    assert repo.get_knowhow_table(table_id)["rows"] == []


def test_apply_atomic_write_wins_a_race_against_a_stale_pre_read(repo):
    """R16 P2 (codex PR #412 review round 16): the anchor-membership
    pre-read (`knowhow_anchor_existing_values`) and the actual knowhow write
    used to be two separate calls, with no lock in between covering the
    ordinary knowhow row/cell edit endpoints — only `_apply_lock` serializes
    concurrent catalog APPLIES, not a plain `add_knowhow_row` through the
    live table UI. A row landing in that gap used to get double-inserted.

    Simulated deterministically instead of with real threads: the pre-read
    is monkeypatched to answer honestly for the moment it actually ran (truly
    nothing existed yet), and as a side effect of that very call a row for
    the SAME command is inserted directly through the plain `add_knowhow_row`
    endpoint — modelling a concurrent manual edit landing in exactly that
    gap, between the pre-read returning and the write starting.

    `append_knowhow_rows_skipping_existing_anchors`'s OWN re-check, inside
    its own write transaction, must catch it anyway: no double insert, the
    candidate is reported back as a conflict (never `applied`), and it must
    end up `dismissed` — not silently dropped and not stuck in `candidate`
    forever (see the fully-conflicting-rerun convergence tests above for why
    that would matter).
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 2)
    service, job = _run_ok(repo, notebook.id, "s1", 2)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=5)
    first_id = next(
        row["id"] for row in page["items"] if row["command_name"] == "set_thing_0"
    )

    # Seed the target table for real with the FIRST command, so the table
    # (and its anchor column) already exist before the race — mirrors a
    # re-run against a source that was already confirmed once.
    first = service.apply(
        notebook.id, "s1", job["id"], candidate_ids=[first_id], actor="tester"
    )
    table_id = first["table_id"]
    assert first["rows_added"] == 1
    table = repo.get_knowhow_table(table_id)
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")

    # The first job's OTHER candidate (`set_thing_1`) is still sitting
    # unreviewed in `candidate` state — `start`'s own pending-candidates
    # guard would refuse a fresh run over the same source while that is
    # true, so dismiss it before starting the second run (a real reviewer
    # choice, unrelated to the race this test is proving).
    remaining_id = next(
        row["id"] for row in page["items"] if row["command_name"] == "set_thing_1"
    )
    service.dismiss(notebook.id, "s1", job["id"], candidate_ids=[remaining_id])

    # A fresh run over the same source reproduces the SECOND command as a
    # new candidate — the race target. Nothing named `set_thing_1` exists in
    # the table yet at this point.
    second_service, second_job = _run_ok(repo, notebook.id, "s1", 2)
    second_page = second_service.candidates_page(
        second_job["id"], state="candidate", cursor=0, limit=5
    )
    race_id = next(
        row["id"] for row in second_page["items"]
        if row["command_name"] == "set_thing_1"
    )

    original_pre_read = second_service.knowhow.knowhow_anchor_existing_values

    def stale_pre_read(column_id, values):
        # Answers as of THIS instant — truthfully empty, nothing exists yet.
        result = original_pre_read(column_id, values)
        # ...and only THEN does the race land: a concurrent ordinary edit
        # through the plain add-row endpoint, which `_apply_lock` never
        # covers.
        repo.add_knowhow_row(
            table_id, {anchor: "set_thing_1"}, actor="concurrent-editor"
        )
        return result

    second_service.knowhow.knowhow_anchor_existing_values = stale_pre_read
    try:
        result = second_service.apply(
            notebook.id, "s1", second_job["id"],
            candidate_ids=[race_id], actor="tester",
        )
    finally:
        second_service.knowhow.knowhow_anchor_existing_values = original_pre_read

    # No double insert: exactly the one row the concurrent editor landed.
    table_after = repo.get_knowhow_table(table_id)
    names = sorted(row["cells"].get(anchor, "") for row in table_after["rows"])
    assert names == ["set_thing_0", "set_thing_1"], (
        "the stale pre-read let apply double-insert set_thing_1"
    )

    assert result["rows_added"] == 0
    assert result["applied"] == []
    assert {c["command_name"] for c in result["conflicts"]} == {"set_thing_1"}

    row = second_service.catalog.candidates_by_ids(
        second_job["id"], [race_id], limit=1
    )[0]
    assert row["state"] == "dismissed"
    assert row["reject_info"].get("reason") == "conflict_existing_row"


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


# --------------------- R18 P2: a RERUN job inherits the SOURCE's last target
def test_apply_by_a_rerun_job_inherits_the_previous_jobs_renamed_table(repo):
    """R18 (codex PR #412 review round 18): the fix above only covers the
    SAME job applying twice. A rerun ("重新识别") creates a brand NEW job
    whose OWN `applied_table_id` starts empty — and before this fix,
    `_resolve_target_table` fell straight through to a by-title lookup for a
    job in that state. Rename the table between job A's apply and job B's
    (the SAME event M1 already covers, but across two DIFFERENT jobs for the
    same source): job B's by-title guess ("命令目录：OpenROAD 手册") no
    longer finds it, and without inheritance would fork a second table under
    the OLD title — same-command conflict detection would then be blind to
    the row job A already wrote. `latest_applied_table_id` must let job B
    converge on job A's table instead."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job_a["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    assert first["created"] is True
    repo.update_knowhow_table_meta(table_id, title="手工重命名后的表")

    _, job_b = _run_ok(repo, notebook.id, "s1", 1)
    second = service.apply(
        notebook.id, "s1", job_b["id"], all_pending=True, actor="tester"
    )
    assert second["table_id"] == table_id
    assert second["created"] is False
    # The re-extracted "set_thing_0" already has a row from job A -> conflict,
    # not a second write.
    assert second["rows_added"] == 0
    assert {item["command_name"] for item in second["conflicts"]} == {"set_thing_0"}
    assert service.catalog.get_job(job_b["id"])["applied_table_id"] == table_id
    # No second 「命令目录：…」 table exists under the derived title.
    assert len(repo.list_knowhow_tables(notebook.id)) == 1


def test_apply_by_a_rerun_job_inherits_the_target_after_a_source_title_change(repo):
    """R18: the OTHER known way a rerun's derived title stops resolving the
    original table — a paper-metadata backfill lands on the source AFTER job
    A already created the table, promoting `_display_source_title` to a
    different string than job A ever used. The table itself was never
    renamed, but job B's fresh by-title guess ("命令目录：<new paper
    title>") does not match it either. Inheritance is keyed by SOURCE, not
    by title, so job B must still land in job A's table."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job_a["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    assert repo.get_knowhow_table(table_id)["title"] == (
        f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"
    )

    _mark_grounded_paper(repo, notebook.id, "s1", "A Later-Grounded Title")
    assert service._display_source_title(
        "s1", service._scoped_source(notebook.id, "s1")
    ) == "A Later-Grounded Title"

    _, job_b = _run_ok(repo, notebook.id, "s1", 1)
    second = service.apply(
        notebook.id, "s1", job_b["id"], all_pending=True, actor="tester"
    )
    assert second["table_id"] == table_id
    assert second["created"] is False
    assert second["rows_added"] == 0
    assert {item["command_name"] for item in second["conflicts"]} == {"set_thing_0"}
    assert len(repo.list_knowhow_tables(notebook.id)) == 1


def test_apply_by_a_rerun_job_recreates_the_table_when_the_inherited_target_was_deleted(
    repo,
):
    """R18: inheritance is a hint, re-verified like the same-job fast path
    already is — a deleted table falls through to create-or-find rather than
    raising or resurrecting the id."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job_a["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    repo.delete_knowhow_table(table_id)

    _, job_b = _run_ok(repo, notebook.id, "s1", 1)
    second = service.apply(
        notebook.id, "s1", job_b["id"], all_pending=True, actor="tester"
    )
    assert second["created"] is True
    assert second["table_id"] != table_id
    assert second["rows_added"] == 1
    new_table = repo.get_knowhow_table(second["table_id"])
    assert new_table["title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"


def test_apply_by_a_rerun_job_never_inherits_a_target_from_a_different_notebook(repo):
    """R18: `latest_applied_table_id` is scoped by SOURCE alone, not by
    notebook — nothing in the store query itself proves the id it returns
    still lives in the notebook the current job belongs to. A real transfer
    (`app/services/knowhow/transfer.py`) never mutates an existing table
    row's `notebook_id` in place (move deletes the source row and creates a
    new one in the target notebook under a new id), so this simulates the
    only shape that could put a foreign id in `applied_table_id` — a stale
    job row recorded against this source but naming a table that lives
    elsewhere — directly, the same way this file already writes fixture rows
    by hand elsewhere (`_mark_grounded_paper`). Apply must refuse to write
    into it and fall back to an ordinary local create."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    other_notebook = repo.create_notebook(NotebookCreate(name="other"))
    _add_manual(repo, notebook.id, "s1", 1)
    foreign_table_id = repo.create_knowhow_table(
        other_notebook.id, "外库表", "",
        [{"name": "命令", "role": "anchor"}],
        "tester",
    )
    with repo._write() as db:
        db.execute(
            "INSERT INTO catalog_jobs (id,notebook_id,source_id,created_by,"
            "status,sections_total,sections_done,entries,rejected,uncovered,"
            "truncated_sections,failure_reason,diagnostic,applied_table_id,"
            "source_generation,created_at,updated_at,finished_at) VALUES "
            "(?,?,?,?,'succeeded',1,1,1,0,0,0,'','',?,'',?,?,?)",
            (
                "cjb-r18-foreign-stale", notebook.id, "s1", "tester",
                foreign_table_id, NOW, NOW, NOW,
            ),
        )

    service, job_b = _run_ok(repo, notebook.id, "s1", 1)
    result = service.apply(
        notebook.id, "s1", job_b["id"], all_pending=True, actor="tester"
    )
    assert result["table_id"] != foreign_table_id
    assert result["created"] is True
    assert repo.get_knowhow_table(foreign_table_id)["rows"] == []
    local_table = repo.get_knowhow_table(result["table_id"])
    assert local_table["rows"]
    assert local_table["title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"


# --------------------- R20 P2: inheritance membership by id, not by title ---
def test_apply_by_a_rerun_job_inherits_by_id_past_a_colliding_earlier_title(repo):
    """R20 (codex PR #412 评审第 20 轮,P2): R18's membership re-check for an
    inherited apply target used to be a TITLE round-trip
    (`knowhow_table_id_by_title(notebook_id, title) == candidate_table_id`).
    A table's title is not unique — a person can rename job A's table to a
    string that collides with an EARLIER, unrelated table already living in
    the SAME notebook. `knowhow_table_id_by_title`'s documented tie-break
    (creation order) then resolves that collided title to the EARLIER table,
    not job A's — so the equality check failed for an inherited target that
    in fact still belonged to this notebook, and `_inherit_applied_table`
    fell through to create-or-find, forking a THIRD table under the derived
    title and losing sight of job A's row entirely (the same "irreversible
    fork" this whole method exists to prevent, reintroduced by its own
    membership check). Reading the candidate's own `notebook_id` column
    directly must let job B still converge on job A's table."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    collide_title = "手工重命名后的表"
    unrelated_table_id = repo.create_knowhow_table(
        notebook.id, "不相关的表", "",
        [{"name": "命令", "role": "anchor"}],
        "tester",
    )
    # Give the unrelated table an unambiguously EARLIER creation order than
    # job A's table (created below), so the documented `created_at, id`
    # tie-break resolves the collided title to THIS row, not job A's.
    with repo._write() as db:
        db.execute(
            "UPDATE knowhow_tables SET title=?, created_at=? WHERE id=?",
            (collide_title, "2000-01-01T00:00:00+00:00", unrelated_table_id),
        )

    _add_manual(repo, notebook.id, "s1", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job_a["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    assert table_id != unrelated_table_id
    repo.update_knowhow_table_meta(table_id, title=collide_title)

    _, job_b = _run_ok(repo, notebook.id, "s1", 1)
    second = service.apply(
        notebook.id, "s1", job_b["id"], all_pending=True, actor="tester"
    )
    assert second["table_id"] == table_id
    assert second["created"] is False
    # The re-extracted "set_thing_0" already has a row from job A -> conflict,
    # not a second write into a freshly forked table.
    assert second["rows_added"] == 0
    assert {item["command_name"] for item in second["conflicts"]} == {"set_thing_0"}
    # The unrelated, earlier-created table sharing the collided title was
    # never touched, and no third table was forked under the derived title.
    assert repo.get_knowhow_table(unrelated_table_id)["rows"] == []
    assert len(repo.list_knowhow_tables(notebook.id)) == 2


def test_apply_refuses_rather_than_forks_when_an_inherited_table_lost_its_shape(repo):
    """R18: the anchor-shape check (`APPLY_TABLE_SHAPE_MESSAGE`) already
    covers the same-job fast path (`test_apply_refuses_a_table_that_lost_its_
    command_column` et al.). It must also fire for a table reached through
    inheritance, not get silently bypassed into forking a second table —
    both the rename (so by-title fallback cannot find it either) and the
    anchor removal are applied to the SAME table so only inheritance, not
    the ordinary by-title path, can even reach it."""
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job_a = _run_ok(repo, notebook.id, "s1", 1)
    first = service.apply(
        notebook.id, "s1", job_a["id"], all_pending=True, actor="tester"
    )
    table_id = first["table_id"]
    repo.update_knowhow_table_meta(table_id, title="重命名后的破损表")
    repo.set_knowhow_anchor_column(table_id, None, actor="tester")

    _, job_b = _run_ok(repo, notebook.id, "s1", 1)
    with pytest.raises(ValueError) as excinfo:
        service.apply(
            notebook.id, "s1", job_b["id"], all_pending=True, actor="tester"
        )
    assert str(excinfo.value) == APPLY_TABLE_SHAPE_MESSAGE
    # Refused, not silently forked into a second (correctly-shaped) table.
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

    Blocks `append_knowhow_rows_skipping_existing_anchors` (not
    `create_knowhow_table`, unlike the apply/apply concurrency tests above)
    because the race this proves needs apply to still be mid-flight AFTER it
    has already decided to write this exact candidate but BEFORE
    `mark_candidates_applied` runs.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    service, job = _run_ok(repo, notebook.id, "s1", 1)
    page = service.candidates_page(job["id"], state="candidate", cursor=0, limit=5)
    candidate_id = page["items"][0]["id"]

    entered = threading.Event()
    release = threading.Event()
    original_append = service.knowhow.append_knowhow_rows_skipping_existing_anchors

    def blocking_append(*args, **kwargs):
        entered.set()
        release.wait(10)
        return original_append(*args, **kwargs)

    service.knowhow.append_knowhow_rows_skipping_existing_anchors = blocking_append
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
        service.knowhow.append_knowhow_rows_skipping_existing_anchors = original_append

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
    body = preview.json()
    assert body["estimated_windows"] == 2
    assert body["estimated_calls"] == 2
    assert body["skipped_windows_in_prefix"] == 0
    # v1's shape signal retired with sectioning, and so did the count of
    # "command sections" — no compatibility alias, so a stale client fails
    # loudly instead of reading a field that is permanently 0.
    assert "signal" not in body
    assert "estimated_sections" not in body

    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    started = client.post(base)
    assert started.status_code == 200, started.text
    job_id = started.json()["job"]["id"]
    # v2 has no truncation concept, so the field is gone from the transport
    # layer rather than transmitted as a permanent 0 the UI would render a
    # never-fires warning from.
    assert "truncated_sections" not in started.json()["job"]["progress"]
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
    # R15 (codex PR #412 评审第 15 轮,P2): 响应必须带服务端权威的目标表标题,
    # 前端不再自己用来源标题预测——这里是普通(非论文)来源,与来源名一致。
    assert applied.json()["table_title"] == f"{CATALOG_TABLE_TITLE_PREFIX}OpenROAD 手册"


def test_api_apply_table_title_uses_the_grounded_paper_title_not_the_upload_name(http):
    """R15 (codex PR #412 评审第 15 轮,P2):`apply` 的 HTTP 响应必须带服务端解析
    到的真实表标题。这条钉住的是引发本轮评审的具体场景——一份被接地判定为论文
    的来源,`sources.title`(上传文件名)与后端实际建表所用的规范标题(论文标题)
    不同,前端此前只能靠自己预测(`catalogTableTitle(sourceDetail.title)`),对
    论文来源必然预测错;这里必须原样等于 `CATALOG_TABLE_TITLE_PREFIX` 拼接后端
    真正写入的论文标题,而不是拼接 `OpenROAD 手册` 这个上传名。"""
    client, repo = http
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 1)
    _mark_grounded_paper(repo, notebook.id, "s1", "A Grounded Paper Title")
    base = f"/api/notebooks/{notebook.id}/sources/s1/command-catalog"

    bind_chat_client(repo, "kg_extract", _Client(_good_reply))
    job_id = client.post(base).json()["job"]["id"]
    _await_terminal(repo, job_id)

    applied = client.post(
        f"{base}/apply", params={"job_id": job_id}, json={"all_pending": True}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["rows_added"] == 1
    assert applied.json()["table_title"] == (
        f"{CATALOG_TABLE_TITLE_PREFIX}A Grounded Paper Title"
    )
    assert "OpenROAD 手册" not in applied.json()["table_title"]
    table = repo.get_knowhow_table(applied.json()["table_id"])
    # 响应里的标题必须是这张表**真实**的标题,不是巧合等值。
    assert table["title"] == applied.json()["table_title"]


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
    field-rejection ledger) was capped and reported at `MAX_WINDOW_REJECTIONS`
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
    assert row["rejections_overflow"] == 400 - MAX_WINDOW_REJECTIONS
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
# `append_knowhow_rows_skipping_existing_anchors` then lands rows describing
# sections the document no longer has. These tests pin the barrier that
# closes it.
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

    def append_knowhow_rows_skipping_existing_anchors(self, *args, **kwargs):
        self._reached.set()
        assert self._release.wait(timeout=10), "apply was never released"
        return self._inner.append_knowhow_rows_skipping_existing_anchors(
            *args, **kwargs
        )


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


# --------------------- R17 (rebutted): the status check stays point-in-time
def test_apply_tolerates_a_reparse_flipping_to_parsing_after_the_status_check(repo):
    """This is the REVERSE guard for R17 (codex PR #412 review) — a P1 the
    maintainer rebutted rather than fixed. This test is what makes that a
    decision instead of a drift: turning `_require_not_parsing` (or
    `_require_current_generation`) into a value re-checked against the write
    that follows it — e.g. re-reading `parse_status` a second time right
    before `_apply_locked` writes — must fail THIS test. If it does not, the
    rebuttal in `_require_not_parsing`'s own docstring (R17 paragraph) is no
    longer true of the code and needs to be revisited on purpose, not
    discovered by accident.

    codex's claim: a reparse that flips the source to `parsing` right AFTER
    this check passes, but before `_apply_locked` finishes landing its rows,
    leaves "permanently stale" knowhow rows with no later signal ever marking
    them stale.

    Rebuttal, pinned in two parts:
      (1) `apply` already holds `_source_write_barrier(source_id)` across its
          ENTIRE write (R10; see `test_apply_holds_the_parse_barrier_across_
          its_write`), and `replace_elements` cannot run without that same
          barrier — so no element the write reads can possibly have changed
          in this window no matter what the STATUS column does. The flip
          modelled here is deliberately just the status write (the one
          `process_source` performs before ever touching the barrier — see
          `_require_not_parsing`'s own docstring on why that ordering exists
          in production) with no accompanying `replace_elements`, because
          that status-only write is the entire scenario R17 describes: real
          `hold_source_chunk_lock` contention is already covered by the R10
          tests above.
      (2) A reparse whose parse stage starts only after this check returns is
          indistinguishable, from the source's own history, from a user
          clicking 「重新解析」 the instant AFTER this apply call finishes —
          plain serial ordering, not a race this call is part of. The R8
          generation guard (`_require_current_generation`) already owns that
          ordering: the SAME job's next touch sees the reparse that actually
          completed and expires every surviving candidate as
          `source_reparsed`. Part 3 below proves that half is not aspirational.
    """
    notebook = repo.create_notebook(NotebookCreate(name="n"))
    _add_manual(repo, notebook.id, "s1", 3)
    service, job = _run_ok(repo, notebook.id, "s1", 3)

    def candidate_id(name: str) -> str:
        page = service.catalog.list_candidates(
            job["id"], state="candidate", cursor=0, limit=10
        )
        return next(row["id"] for row in page if row["command_name"] == name)

    # The flip R17 is about, modelled at the narrowest possible grain: since
    # `_require_not_parsing` is a single point-in-time read, wrapping it to
    # perform the flip the INSTANT it returns normally (i.e. only once the
    # check has already passed) IS the gap the claim describes — no thread
    # or timing assumption required to land in it.
    original_check = service._require_not_parsing

    def _check_then_a_reparse_starts(notebook_id, source_id):
        original_check(notebook_id, source_id)
        repo._runtime.source_ingestion.set_source_status(source_id, "parsing")

    service._require_not_parsing = _check_then_a_reparse_starts

    # Partial apply — two of three commands — so a genuinely unreviewed
    # candidate survives into part 3 below.
    result = service.apply(
        notebook.id, "s1", job["id"],
        candidate_ids=[candidate_id("set_thing_0"), candidate_id("set_thing_1")],
        actor="tester",
    )

    # Part 1: the confirm SUCCEEDED — neither `CatalogSourceBusy` nor
    # `CatalogSourceChanged` — even though the source now reads `parsing`,
    # because that flip landed after the only check that looks at it.
    assert result["rows_added"] == 2
    assert repo.get_source("s1").parse_status == "parsing"

    # Part 2: the rows that landed describe the PRE-flip elements — the only
    # elements that ever existed during this call.
    table = repo.get_knowhow_table(result["table_id"])
    anchor = table["columns"][0]["id"]
    names = sorted(row["cells"].get(anchor, "") for row in table["rows"])
    assert names == ["set_thing_0", "set_thing_1"]

    # Part 3: the serial case the rebuttal leans on. A REAL reparse now
    # actually completes — elements swapped through `replace_elements`, status
    # settled back the way `process_source` leaves it — the thing the flip
    # above only announced without ever finishing. The same job's surviving
    # pending candidate (`set_thing_2`) is stale against it, and the R8
    # generation guard is what is supposed to catch that on the very next
    # touch of this job.
    _reparse(repo, notebook.id, "s1", _manual_elements("s1", 3), LATER)
    repo._runtime.source_ingestion.set_source_status("s1", "extracted")

    with pytest.raises(CatalogSourceChanged):
        service.apply(
            notebook.id, "s1", job["id"], all_pending=True, actor="tester"
        )

    # Nothing new was written — `_apply_locked` never ran a second time, so
    # this is still the one table part 2 already inspected.
    tables = repo.list_knowhow_tables(notebook.id)
    assert len(tables) == 1
    assert tables[0]["id"] == result["table_id"]
    counts = service.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 0
    assert counts["dismissed"] == 1
    dismissed = service.catalog.list_candidates(
        job["id"], state="dismissed", cursor=0, limit=10
    )
    assert {row["reject_info"]["reason"] for row in dismissed} == {
        "source_reparsed"
    }


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
