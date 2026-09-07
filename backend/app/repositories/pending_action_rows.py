"""待确认中心「进行中的提问」的后端中性常量与行整形 —— 两个后端共用一份。

放在中性层的理由与 `group_rows.py` / `like_pattern.py` 同款:这里是纯 Python 的行
整形与两个数值围栏,不含任何 SQL 方言。两侧各写一份的唯一结果就是某天它们折得不
一样——一个后端把问题摘要截到 60 字、另一个截到 80,或者一个取最新 20 条、另一个
取 50 条。这类分叉不报错,只是同一个用户在 SQLite 与 PostgreSQL 上看到的铃铛内容
不同,没有任何测试会自然抓到。

⚠ 两个数值是**用户可见契约**,登记在 `docs/product-and-api*.md`;改这里必须同步
改那两份文档。
"""
from __future__ import annotations

from typing import Any


#: 「这次提问还在跑」的**精确**状态集合。
#:
#: `ask_jobs` 没有 `queued`:`_insert_job_row`(两个后端逐字相同)直接插 `'running'`。
#: 终态是 `done` / `failed` / `cancelled`,外加重启兜底把残留 `running` 改写成的
#: `interrupted`(见 `sqlite/migrations.py::_recover_interrupted_jobs`)。
#:
#: 刻意写成**正向精确匹配**而不是 `status NOT IN (<终态…>)`:否定式在将来新增一个
#: 中间状态(比如真的排队)时会把它悄悄放进铃铛,而铃铛条目是可点击的深链——指向一个
#: 语义未经设计的状态是静默的错。正向匹配 fail-safe:新状态默认不进铃铛,要进就得
#: 有人显式把它加到这里。
RUNNING_ASK_STATUSES: tuple[str, ...] = ("running",)

#: 一次投影里最多带回多少条在途提问(取最新的)。
#:
#: 一个人同时在跑的提问本来就是个位数(每条都占着一个 worker),这个上限几乎不会
#: 生效;它存在是为了让一个异常账户(脚本刷提问、或者一次故障留下大量 running 行)
#: 不能把铃铛的一帧快照撑爆——快照会经 SSE 推给每条连接。
RUNNING_ASK_ROWS = 20

#: 问题摘要的截断长度(码点)。铃铛里只放**摘要**,不放整段提问:提示词可能很长,
#: 而铃铛的每一帧都要序列化进 SSE 快照。与「深度报告待确认」条目的 `title` 同一
#: 口径(那一半也是 60)。
ASK_QUESTION_PREVIEW_CHARS = 60


def ask_question_preview(question: str) -> str:
    """把提问正文折成铃铛里那一行摘要:先**归一空白**,再按码点截断。

    归一在截断之前,不是之后。提问经常是多行粘贴进来的(换行 + 缩进 + 连续空格),
    直接切前 60 个码点会把换行和成串空格一起算进预算,再原样塞进快照——铃铛的
    条目是**单行**呈现,那些空白既占满了摘要的额度,又在渲染时折成一段可疑的空隙。
    归一之后 60 个码点全都是内容字符,两个后端的摘要也才真正逐字相同。
    """
    return " ".join((question or "").split())[:ASK_QUESTION_PREVIEW_CHARS]


def running_ask_item(
    *,
    job_id: str,
    notebook_id: str,
    notebook_name: str,
    conversation_id: str,
    question: str,
    status: str,
    asked_at: str,
) -> dict[str, Any]:
    """把一行在途 `ask_jobs` 折成待确认中心的条目。

    字段名刻意复用已有的 `title` / `state`,而不是另造 `question` / `status`:前端
    `PendingItem` 的这两个字段已经服务报告/索引/论文元数据三类条目,复用它们让新分组
    只多两个可选 id 字段(`job_id` / `conversation_id`),而不是多一整套并行字段。

    `asked_at` 是浏览器提交时刻(`ask_jobs.asked_at`,可能是空串——该列是
    `NOT NULL DEFAULT ''`,旧行与不带该字段的客户端都会留空);空时由调用方回落到
    服务端 `created_at`。
    """
    return {
        "type": "ask",
        "state": status,
        "job_id": job_id,
        "notebook_id": notebook_id,
        "notebook_name": notebook_name or "",
        "conversation_id": conversation_id or "",
        "title": ask_question_preview(question),
        "asked_at": asked_at or "",
    }
