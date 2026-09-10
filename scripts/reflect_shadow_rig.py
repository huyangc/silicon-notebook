#!/usr/bin/env python3
"""reflect v2 开闸前 T0 · 影子轨迹 rig(设计规格 2026-09-08 §2.3)。

在**本地一次性 PG 测试库**上灌真实语料与真实提问,同一批问题在 legacy 与 v2
两种策略下各跑一遍,Ask 与 Report 都跑。Ask 的轨迹落库、由
`scripts/export_reasoning_traces.py` 导出;Report 的逐节轨迹不落库,由本脚本
**进程内**捕获成 JSONL(§2.3)。

另有一条**不建测试库**的路:`search` 直接对**主库(只读)**里两个既有笔记本
跑检索过程(plan + reflect 循环),轨迹在进程内投影成 JSONL。它不建库、不建图、
不合成答案,也因此不写 ask_jobs / answers / conversations 任何一行。

第三条路是 `ab`(A/B 设计规格 `2026-09-09-reflect-ab-design_zh.md`):在 `seed`
建出来的**一次性测试库**上进程内跑**完整 Ask**(意图契约 → 检索 → 合成 → 引用
绑定),同题同档的 legacy / v2 **背靠背**跑、臂序随机。它与 `search` 的分工是
「search-only 量不出答案质量」;与 `ask`(HTTP)的分工是「HTTP 换策略必须重启
后端,两条臂只能整批分开、相隔数小时,provider 的漂移会整块落在臂的差上」。

    python scripts/reflect_shadow_rig.py --dry-run seed
    python scripts/reflect_shadow_rig.py --dry-run ask
    python scripts/reflect_shadow_rig.py --dry-run report
    python scripts/reflect_shadow_rig.py --dry-run --limit 5 search
    python scripts/reflect_shadow_rig.py --dry-run --limit 2 \
        --database-url postgresql://127.0.0.1:5432/silicon_notebook_t0_test \
        --source-db-url postgresql://127.0.0.1:5432/silicon_notebook ab
    python scripts/reflect_shadow_rig.py --dry-run --seed 7 prefix-probe

子命令:`seed` / `ask` / `report` / `search` / `ab` / `restart` / `export` /
`teardown` / `prefix-probe`。

第四条路是 `prefix-probe`(E1,前缀敏感性探针,实施规格
`2026-09-11-reflect-prefix-experiments-plan_zh.md` §3 T-EX4):**零数据库、零
检索、零 reflect**,只在同一个 `reasoning_agent` workload 上跑一段固定正文,
两臂只在头/尾标记的稳定性上不同,量的是「稳定前缀有没有可重复的墙钟收益」这
一件事,不回答命中率/token 节省/provider 是否关闭缓存。
**`--dry-run` 打印将要做的每一步与每个 `client_request_id` 的编码,不连库、不
起后端、不发任何请求**——它是唯一进标准门的路径(rig 本体要网络与真实模型)。

三条红线,代码层面的落点在下面各处注释里:

* **主库只读**:`seed` 碰主库时只用 `--source-db-url` 显式给出的连接、只跑一条
  SELECT(重建 A 语料的 markdown),写入全在测试库;`search` 整条路对
  `--database-url`(必须显式给出的主库连接)只读——仓储走 `_search_repository`,
  即 `create_repository(..., migrate=False, seed=False)`(codex #700 R1
  P1):默认的 `migrate=True, seed=True` 会在 PostgreSQL 上无条件跑一次
  `bundle._initialize`,先迁移再用 `settings.admin_password` 重写 admin 密码
  哈希——这是一次真实的、非幂等的写,已经在生产主库上真实发生过一次
  (`users` 表 admin 行的 `updated_at` 被改)。跑前跑后各点一次
  `READONLY_TABLES` 的行数 + `users.updated_at` 的最大值(`_readonly_counts`),
  不等就报错;后一项是因为 admin 密码重写是 `UPDATE`,行数断言对它是瞎的,
  这次真实事故就是从这个盲区钻过去的。**没有**额外在连接串上强制
  `default_transaction_read_only=on`:`app/repositories/postgres/search.py`
  的知识图谱检索用到 `CREATE TEMP TABLE` scratch 表,而这条路径恰恰是
  `search` 的 `B_kg` 语料格要跑的那一半——把整个会话钉成服务端强制只读,
  真出问题时会把"这条防线生效了"和"知识图谱检索本身跑不动了"混成同一个
  报错,分不清是谁挡的。防线因此只有两层:构造入口不迁移/不 seed,加上
  跑前跑后的值断言,而不是第三层"会话级只读"。
* **策略靠重启**(HTTP 路径):`REASONING_REFLECT_V2_ENABLED` 是进程级 Settings,
  rig 不去热改它;`ask` / `report` 每次只跑**一个** `--policy`,换策略 = 换一次
  后端。`restart` 是唯一的换策略手段:优雅停掉 state 里记的那台后端、按新
  `--policy` 重起同一份端口/DB/env-file 配置;`ask` / `report` 开跑前会核对
  state 里记的策略与这次的 `--policy` 是否一致,不一致直接报错,而不是悄悄
  跑出一批策略对不上号的轨迹。**`search` 不在此列**:它是进程内直调
  `ReasoningRetriever`,而那个类读的是**传进去的**那份 Settings,所以两个策略
  各构造一份 Settings 就能在同一次进程里交替跑(见 `_settings_by_policy`)。
* **编号不进问题文本**:题号/语料格/策略/档位只编进 `client_request_id`(见
  `encode_client_request_id`),导出时据它打标。模型永远看不到这些编号。
  `search` 不经 job,标签直接进投影行;问题原文一个字都不进它的日志与 JSONL。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.domain.reasoning_trace_stats import (  # noqa: E402
    CORPUS_CELLS,
    MAX_REFLECT_STEPS,
    assert_closed,
    assert_projection_values,
    merge_key,
    project_report_section,
    project_run,
    project_search_run,
)
#: 臂的解析与命名是**纯函数**(零 I/O、不读 Settings、不连库),与上面那份
#: domain 投影同一条理由放在模块顶部:`--dry-run` 要用它们枚举计划,而 dry-run
#: 的硬约束是「不 import `app.core.config`」。`reflect_ab` 只依赖 domain 与
#: `citation_markers`,两者都零配置。
from app.eval.reflect_ab import (  # noqa: E402
    ARMS,
    PAIR_ARM_COUNT,
    ArmSpecError,
    arm_label,
    format_arm,
    parse_arms,
)
#: T-EX8 manifest:纯构造 + 闭集 + 隐私断言,与上面那两个 import 同一条
#: 「零 I/O、零 Settings」理由——manifest 的形状校验是 dry-run 也够得到的
#: 纯函数(dry-run 本身不写 manifest,但闭集/隐私守卫要能在标准门里被测)。
from app.eval.reflect_manifest import build_manifest  # noqa: E402
from app.eval.reflect_t0 import load_questions  # noqa: E402
from export_reasoning_traces import RIG_FIELDS, RIG_PREFIX  # noqa: E402

DEFAULT_TEST_DB = "silicon_notebook_t0_test"
DEFAULT_PORT = 8001
EFFORTS: tuple[str, ...] = ("standard", "deep")
POLICIES: tuple[str, ...] = ("legacy", "v2")
#: 测试库要装的扩展(与 memory `local-pg-test-db` 同一份清单)。
PG_EXTENSIONS: tuple[str, ...] = ("pg_trgm", "btree_gin")
#: fixture 用户名。**必须过 `app.domain.auth_utils.USERNAME_RE`(单个小写字母 +
#: 八位数字)**——注册端点用同一条正则,任何别的形状都会被 400 挡在门外,而
#: 那时前面的建库/起后端已经白做了一遍。
DEFAULT_USER = "t00000001"
#: 一次上传的文件数上限,与 `app.core.config.SOURCE_UPLOAD_MAX_FILES_PER_BATCH`
#: 同值。超出的批会被解析器以 413 拒绝,所以 rig 自己先切片。
UPLOAD_BATCH = 20
#: 来源解析的终态(`source_ingestion` 的 `_emit_status` 三个落点)。轮询等的是
#: 「不再变」,不是「成功」——failed 也要停,否则一个坏文件把整次 seed 挂死。
PARSE_TERMINAL: frozenset[str] = frozenset({"extracted", "failed", "metadata-only"})
#: `search` 跑的两个语料格。**它们是主库里既有的事实,不是 rig 造出来的**:
#: `--source-notebook-a` 那个笔记本一个 knowledge_object 都没有,
#: `--source-notebook-b` 那个有四万多个。`search` 不建库也不建图,所以另外两格
#: (`A_kg` / `B_nokg`)在主库上根本不存在,不在这条路的枚举里。
SEARCH_CELLS: tuple[str, ...] = ("A_nokg", "B_kg")
#: `search` 的只读断言盯的表。前四张是 Ask / 答案 / 会话 / 图的写入面;
#: `retrieval_experiences` 是 `ReasoningRetriever.run` 全程**唯一**的写路径
#: (收尾那一次有界 `note_adopted` UPDATE,只在注入开着时可达——而 rig 另外把
#: 注入开关强制关掉,见 `_search_process_env`)。表不存在时那一格记 `None` =
#: 「这次不看它」,而不是当成 0:否则一次改名会静默通过。
READONLY_TABLES: tuple[str, ...] = (
    "ask_jobs", "answers", "conversations", "knowledge_objects",
    "retrieval_experiences",
)


# --- client_request_id 编码 -------------------------------------------------


def encode_client_request_id(
    *, question_key: str, corpus_cell: str, policy: str, effort: str,
    requested_mode: str,
) -> str:
    """把「题号/语料格/策略/档位/请求 mode」编成一个幂等键。

    `export_reasoning_traces.decode_client_request_id` 是它的另一半,两者共用
    `RIG_PREFIX` / `RIG_FIELDS`,round-trip 由
    `backend/tests/test_reflect_t0_scripts.py` 双向钉住。

    半角冒号是字段分隔符,所以任何字段都不许含冒号——题集 key 里出现一个冒号就
    会让导出侧解出错位的标签,而错位的标签比没有标签更坏(它会静默地把两个格子
    的样本混进同一行对照)。这里当场拒绝,而不是留给导出侧去发现。
    """
    values = {
        "question_key": question_key, "corpus_cell": corpus_cell,
        "policy": policy, "effort": effort, "requested_mode": requested_mode,
    }
    for name, value in values.items():
        text = str(value)
        if not text or ":" in text:
            raise ValueError(f"client_request_id 字段非法({name}={value!r})")
    return RIG_PREFIX + ":".join(str(values[field]) for field in RIG_FIELDS)


# --- 计划(dry-run 与真跑共用同一份枚举) -----------------------------------


def corpus_cells(selected: Sequence[str]) -> list[str]:
    return [cell for cell in CORPUS_CELLS if not selected or cell in selected]


def ask_plan(
    questions: dict, *, cells: Sequence[str], policy: str, limit: int | None,
    lang: str, mode: str, efforts: Sequence[str],
    only_questions: Sequence[str] = (),
) -> list[dict]:
    """`ask` 要发的全部 run,一条一行。**dry-run 与真跑读的是同一个函数**。

    dry-run 若另起一份枚举,它验证的就不是将要跑的那件事了。

    `only_questions` 非空时按题号(`row["key"]`,不带 `-en` 后缀)先筛一遍,
    **筛在 `limit` 切片之前**——`search` 的 `--only-question`/`--only-cell` 与
    `--limit` 同时给时约定「先筛后限」,这里是唯一的切点。默认空 = 不筛,
    `ask` 命令不传这个参数,行为逐字不变。
    """
    plan: list[dict] = []
    langs = ("zh", "en") if lang == "both" else (lang,)
    for cell in cells:
        corpus = cell.split("_", 1)[0]
        rows = [row for row in questions["ask"] if row["corpus"] == corpus]
        if only_questions:
            rows = [row for row in rows if row["key"] in only_questions]
        if limit is not None:
            rows = rows[:limit]
        for row in rows:
            for language in langs:
                text = row.get(language)
                if not text:
                    continue
                key = row["key"] if language == "zh" else f"{row['key']}-en"
                for effort in efforts:
                    plan.append({
                        "question_key": key,
                        "corpus_cell": cell,
                        "policy": policy,
                        "effort": effort,
                        "requested_mode": mode,
                        "lang": language,
                        "shape": row.get("shape", ""),
                        # 显式来源标题(见题集 B-q09 的 `note`)。空元组 = 这道题
                        # 没有声明范围;两条消费路径(`search`/`ask`)各自把它解析
                        # 成目标笔记本里的 source_id 列表,解析不到唯一匹配就把这
                        # 道题标 `status=failed`,不静默退化成全库检索。
                        "scope_source_titles": tuple(
                            row.get("scope_source_titles") or ()
                        ),
                        "client_request_id": encode_client_request_id(
                            question_key=key, corpus_cell=cell, policy=policy,
                            effort=effort, requested_mode=mode,
                        ),
                        "question": text,
                    })
    return plan


def search_cells(selected: Sequence[str]) -> list[str]:
    return [cell for cell in SEARCH_CELLS if not selected or cell in selected]


def search_plan(
    questions: dict, *, cells: Sequence[str], policies: Sequence[str],
    limit: int | None, lang: str, efforts: Sequence[str],
    only_questions: Sequence[str] = (),
) -> list[dict]:
    """`search` 要跑的全部 run,一条一行。**枚举复用 `ask_plan`**。

    两条路的枚举必须是同一份:`search` 与 `ask` 出的行进的是同一张对照表
    (`analyze_reasoning_trace.py` 按 `question_key` + `corpus_cell` + `effort`
    配对),各写一份枚举就等于给「哪些题在哪些格上跑」立两个权威。

    与 `ask` 的两点不同:

    * **一次跑两侧策略**。`search` 是进程内直调,换策略只是换一份 Settings
      (`_settings_by_policy`),不用重启后端,所以 legacy/v2 在同一次调用里
      跑完——这正是配对最干净的形状。
    * **丢掉 `client_request_id`**。那个键的全部意义是「幂等键落进 `ask_jobs`、
      导出时据它打标」,而 `search` 一条 `ask_jobs` 都不写。留着它会让人以为
      能按它在库里查到这批 run。

    `only_questions` 是 `--only-question` 的落点,转发给 `ask_plan`(那边负责
    「先筛后限」的次序)。`--only-cell` 不在这里处理——它收窄的是调用方传入的
    `cells` 本身(见 `cmd_search`),不属于这份枚举内部的过滤规则。
    """
    plan: list[dict] = []
    for policy in policies:
        for item in ask_plan(
            questions, cells=cells, policy=policy, limit=limit, lang=lang,
            mode="reasoning", efforts=efforts, only_questions=only_questions,
        ):
            item.pop("client_request_id", None)
            plan.append(item)
    return plan


def report_plan(
    questions: dict, *, cell: str, policy: str, limit: int | None,
) -> list[dict]:
    rows = questions["report"]
    if limit is not None:
        rows = rows[:limit]
    return [
        {
            "question_key": row["key"],
            "corpus_cell": cell,
            "policy": policy,
            "depth": int(row["depth"]),
            "profile": row["profile"],
            "question": row["question"],
        }
        for row in rows
    ]


# --- A 语料:从主库只读重建 markdown ---------------------------------------

#: 标题元素的层级键。`source_elements` **没有** `section_path` 列(那住在
#: chunks 与 metadata 里),所以重建按 `ordinal` 顺序拼,标题靠 `element_type`
#: 与 `metadata.heading_level` 还原成 markdown 的 `#` 级别。
HEADING_LEVEL_KEY = "heading_level"
DEFAULT_HEADING_LEVEL = 2


def _element_markdown(element_type: str, text: str, metadata: object) -> str:
    if element_type == "heading":
        meta = metadata if isinstance(metadata, dict) else {}
        try:
            level = int(meta.get(HEADING_LEVEL_KEY, DEFAULT_HEADING_LEVEL))
        except (TypeError, ValueError):
            level = DEFAULT_HEADING_LEVEL
        return "#" * max(1, min(6, level)) + " " + text
    if element_type == "list_item":
        return "- " + text
    if element_type == "code_block":
        return "```\n" + text + "\n```"
    if element_type == "formula":
        return "$$\n" + text + "\n$$"
    return text


def rebuild_source_markdown(source_db_url: str, notebook_id: str) -> dict[str, str]:
    """主库 `source_elements` → 每个来源一份 markdown。**只读,一条 SELECT**。

    连接是 `--source-db-url` 显式给的,不从 `.env` 猜;PG 侧连接开
    `read_only`,所以就算这里写错了 SQL 也写不进主库。
    """
    from export_reasoning_traces import _Reader

    with _Reader(source_db_url) as reader:
        rows = reader.query(
            "SELECT e.source_id, e.element_type, e.text, "
            "CAST(e.metadata AS TEXT) AS metadata "
            "FROM source_elements e JOIN sources s ON s.id = e.source_id "
            "WHERE s.notebook_id = ? ORDER BY e.source_id, e.ordinal",
            (notebook_id,),
        )
    documents: dict[str, list[str]] = {}
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        raw_meta = row.get("metadata")
        try:
            metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        except (TypeError, ValueError):
            metadata = {}
        documents.setdefault(row["source_id"], []).append(
            _element_markdown(str(row.get("element_type") or ""), text, metadata)
        )
    return {sid: "\n\n".join(blocks) + "\n" for sid, blocks in documents.items()}


# --- 真跑侧的薄执行器 -------------------------------------------------------


_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


def _safe_http_error_detail(body: bytes) -> str:
    """HTTP 错误正文 → 只保留**短错误码**的诊断串(codex #700 R19 P2)。

    校验错误的正文常常回显被拒的输入(问题原文、意图契约),整段抄进异常再打到
    stderr 就是一次原文泄露,截 500 字不算脱敏。这里只认 JSON 里的
    `detail.code` / `detail.error_code` / `code` / `error_code`(且形如短码);
    其余一律记 `<body redacted>`。状态码由调用方拼。
    """
    try:
        parsed = json.loads(body.decode("utf-8", "replace") or "null")
    except ValueError:
        return "<body redacted>"
    candidates: list[object] = []
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, dict):
            candidates.extend([detail.get("code"), detail.get("error_code")])
        elif isinstance(detail, str):
            candidates.append(detail)
        candidates.extend([parsed.get("code"), parsed.get("error_code")])
    for candidate in candidates:
        if isinstance(candidate, str) and _SAFE_ERROR_CODE.match(candidate):
            return f"code={candidate}"
    return "<body redacted>"


class Runner:
    """把「要做什么」和「真的去做」分开的那一层。

    `--dry-run` 时每个方法只打印,不产生任何副作用——包括不 import psycopg、不
    起 uvicorn、不发 HTTP。这样 dry-run 在一台没有 PG、没有模型配置的机器上也
    能跑,而它验证的枚举与编码正是真跑要用的那一份。
    """

    def __init__(self, *, dry_run: bool, out_dir: Path) -> None:
        self.dry_run = dry_run
        self.out_dir = out_dir
        self.steps = 0

    def say(self, action: str, detail: str = "") -> None:
        self.steps += 1
        prefix = "[dry-run]" if self.dry_run else "[run]"
        print(f"{prefix} {self.steps:03d} {action}" + (f"  {detail}" if detail else ""))

    def shell(self, command: Sequence[str], *, check: bool = True) -> None:
        self.say("shell", shlex.join(command))
        if self.dry_run:
            return
        subprocess.run(list(command), check=check)

    def http(self, method: str, url: str, *, json_body: object = None,
             body: bytes | None = None, content_type: str = "",
             token: str = "", timeout: float = 1800.0, quiet: bool = False) -> Any:
        """一个请求。`quiet` 给轮询用——否则一次 seed 的日志全是 index-status。"""
        if not quiet:
            self.say("http", f"{method} {url}")
        if self.dry_run:
            return None
        import urllib.error
        import urllib.request

        data = body
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 后端的 4xx 正文是唯一说得清「为什么被拒」的东西(用户名不合法、
            # 文件类型不支持、LLM 未配置…)。默认的 HTTPError 只带一个状态码,
            # 那会让一次跑了半小时的 seed 死在一句「HTTP Error 400」上。但正文
            # 可能回显被拒的输入(问题原文、意图契约),只能取其中的短错误码
            # (`_safe_http_error_detail`;codex #700 R19 P2)。
            detail = _safe_http_error_detail(exc.read())
            raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc
        return json.loads(text) if text else None

    def stream(self, method: str, url: str, *, json_body: object,
               token: str = "", timeout: float = 3600.0) -> dict:
        """发一个 NDJSON 流式请求,读到底,返回最后一个事件。

        必须**读到底**:后端的答案落库发生在流跑完的时候,提前断开等于把这一个
        run 变成一次半程。事件正文一行都不留(只记 `type`)——那里面是答案原文,
        而 rig 的全部输出都受 §6 的闭集约束。
        """
        self.say("stream", f"{method} {url}")
        if self.dry_run:
            return {}
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url, data=json.dumps(json_body).encode("utf-8"), headers=headers,
            method=method,
        )
        last: dict = {}
        events = 0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    events += 1
                    try:
                        last = json.loads(line)
                    except ValueError:
                        continue
        except urllib.error.HTTPError as exc:
            detail = _safe_http_error_detail(exc.read())
            raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc
        self.say("stream done",
                 f"{events} event(s), last event={last.get('event', '<none>')}")
        return last

    def write(self, path: Path, text: str) -> None:
        self.say("write", str(path))
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def state_path(self) -> Path:
        return self.out_dir / "rig-state.json"

    def load_state(self) -> dict:
        path = self.state_path()
        if self.dry_run or not path.exists():
            # dry-run 不读上一次真跑留下的状态:它要打印的是「从零开始会做什么」。
            return {"notebooks": {cell: f"<{cell}>" for cell in CORPUS_CELLS}}
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        self.write(self.state_path(), json.dumps(state, ensure_ascii=False, indent=2))


# --- multipart 上传体(只用标准库) -----------------------------------------

#: `/api/notebooks/{id}/sources` 受理的**全部**表单字段,与
#: `app.api.source_routes._SOURCE_UPLOAD_FORM_KEYS` 同一份清单。多出任何一个键
#: 都是 422(那条校验是 `set(form) - _SOURCE_UPLOAD_FORM_KEYS`),所以这里不是
#: 「随便发点什么」,而是逐字对着后端的闭集发。
UPLOAD_FORM_KEYS = ("files", "doc_types", "doc_type_explicit")


def build_upload_body(paths: Sequence[Path]) -> tuple[bytes, str]:
    """(multipart 体, Content-Type)。

    与前端 `frontend/app/page.tsx:3536` 逐字同形:每个文件一个 `files` part,
    再为**每个文件**并列发一个 `doc_types`(空串 = 自动检测)和一个
    `doc_type_explicit`(`"0"` = 用户没手动选过类型)。后端按**位置**把三组对
    齐,所以三者的条数必须一致、顺序必须一致——少发一个 `doc_types` 不会报错,
    只会让后面每个文件都错位拿到上一个文件的类型。
    """
    boundary = "----t0rig" + uuid.uuid4().hex
    marker = f"--{boundary}\r\n".encode()
    parts: list[bytes] = []
    for path in paths:
        content_type = mimetypes.guess_type(path.name)[0] or "text/markdown"
        parts.append(
            marker
            + (
                'Content-Disposition: form-data; name="files"; '
                f'filename="{path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + path.read_bytes()
            + b"\r\n"
        )
    for path in paths:
        for name, value in (("doc_types", ""), ("doc_type_explicit", "0")):
            parts.append(
                marker
                + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value.encode()
                + b"\r\n"
            )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_corpus(
    runner: Runner, base: str, notebook_id: str, paths: Sequence[Path],
    *, token: str,
) -> list[dict]:
    """把一组 markdown 传进一个笔记本。超过一批上限就切片,不指望后端放宽。"""
    uploaded: list[dict] = []
    for start in range(0, len(paths), UPLOAD_BATCH):
        batch = list(paths[start:start + UPLOAD_BATCH])
        body, content_type = build_upload_body(batch)
        runner.say("upload sources",
                   f"{len(batch)} file(s), {len(body)} bytes -> {notebook_id}")
        result = runner.http(
            "POST", f"{base}/api/notebooks/{notebook_id}/sources",
            body=body, content_type=content_type, token=token,
        )
        uploaded.extend(result or [])
    return uploaded


def _list_all_sources(
    runner: Runner, base: str, notebook_id: str, *, token: str,
) -> list[dict]:
    """翻完 `sources` 的**每一页**(codex #700 R20 P2):端点单页上限 200,
    语料超过 200 个来源时只看第一页会在后面几页还在排队/解析时就宣布解析完成,
    后页的失败也永远数不到。以「短页」为终止,不依赖 `total` 字段。"""
    items: list[dict] = []
    offset = 0
    limit = 200
    while True:
        page = runner.http(
            "GET",
            f"{base}/api/notebooks/{notebook_id}/sources?offset={offset}&limit={limit}",
            token=token, timeout=60, quiet=True,
        ) or {}
        batch = list(page.get("items") or [])
        items.extend(batch)
        if len(batch) < limit:
            return items
        offset += len(batch)


def await_parse(
    runner: Runner, base: str, notebook_id: str, *, token: str, timeout: float,
) -> list[dict]:
    """轮询到该笔记本每个来源的 `parse_status` 都进终态(见 `PARSE_TERMINAL`)。

    解析是 `kg_scheduler` 里的后台任务,上传响应返回时通常还是 `queued`;不等它
    就建图 / 提问,拿到的是一个空语料。等的是**终态**而不是成功态:一个坏文件该
    被报出来,不该把整次 seed 挂到超时。
    """
    deadline = time.monotonic() + timeout
    while True:
        items = _list_all_sources(runner, base, notebook_id, token=token)
        pending = [s for s in items if str(s.get("parse_status") or "")
                   not in PARSE_TERMINAL]
        if items and not pending:
            failed = [s for s in items if s.get("parse_status") == "failed"]
            runner.say("parse done",
                       f"{notebook_id}: {len(items)} source(s), "
                       f"{len(failed)} failed")
            return items
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"parse timeout on {notebook_id}: "
                f"{len(pending)}/{len(items)} still pending"
            )
        time.sleep(5)


def await_index(
    runner: Runner, base: str, notebook_id: str, *, token: str, timeout: float,
) -> dict:
    """轮询 `index-status` 直到 KG 建图不在飞且没有待建来源。

    判据取 `kg.building` / `kg.pending_sources`(见
    `app.services.scale_artifact_runtime.index_status`),不看 `kg.ready`:一个
    **本来就没有可抽实体的语料**会正常地建完却不 ready,把 ready 当判据会让 rig
    在一个已经完成的图上等到超时。
    """
    deadline = time.monotonic() + timeout
    while True:
        status = runner.http(
            "GET", f"{base}/api/notebooks/{notebook_id}/index-status",
            token=token, timeout=60, quiet=True,
        ) or {}
        kg = dict(status.get("kg") or {})
        if not kg.get("building") and not int(kg.get("pending_sources") or 0):
            runner.say("index ready",
                       f"{notebook_id}: kg_ready={bool(kg.get('ready'))}")
            return status
        if time.monotonic() >= deadline:
            raise RuntimeError(f"index timeout on {notebook_id}: {kg}")
        time.sleep(10)


# --- 日志脱敏(codex #700 R1 P2) --------------------------------------------
#
# `rig-state.json` 与内部变量该存什么原样存什么——`_teardown_targets_this_run`
# 就是靠 state 里那份逐字的 `database_url` 才能核对"这次真的是同一个库"
# (见上面的 docstring)。脱敏只发生在**打印给人看**的这一层,不动任何被后续
# 逻辑读回去比较的值。

#: 大小写不敏感,子串匹配。命中就把整个值换成 `<redacted>`,不猜哪一部分是
#: 密钥——环境变量的值从不需要在日志里部分可读。
_SENSITIVE_ENV_KEY_MARKERS: tuple[str, ...] = ("DATABASE_URL", "API_KEY", "PASSWORD")


def _redact_url(url: str) -> str:
    """去掉连接串里 `user:pass@` 的密码部分,只留 user、host、port、db。

    只读工具的日志/进度打印会把 `--admin-url` / `--database-url` 摊出来给人
    看;这两个参数在生产环境里可能带真实密码。SQLite 路径没有凭据,原样返回。
    解析失败(不是一个能识别的 URL)时返回一个占位符,而不是原样打印——脱敏
    失败不该退化成"没脱敏"。
    """
    from urllib.parse import urlsplit, urlunsplit

    if not url or url.startswith("sqlite"):
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parsed.hostname:
        return "<unparseable-url>"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    if parsed.username:
        netloc = f"{parsed.username}@{netloc}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _redact_env_for_log(env: dict[str, str]) -> str:
    """`k=v,k=v` 形式的环境变量摘要,敏感键的值一律 `<redacted>`。"""
    parts = []
    for key, value in env.items():
        upper = key.upper()
        shown = (
            "<redacted>"
            if any(marker in upper for marker in _SENSITIVE_ENV_KEY_MARKERS)
            else value
        )
        parts.append(f"{key}={shown}")
    return ",".join(parts)


# --- 子命令 -----------------------------------------------------------------


def backend_env(args: argparse.Namespace, policy: str) -> dict[str, str]:
    """临时后端的环境变量。策略开关在这里,而不是在运行时改 Settings。

    `SILICON_NOTEBOOK_ENV_FILE` 是把主 checkout 的 `.env`(模型服务配置与
    密钥)接进来的**唯一**手段——`Settings` 的 `env_file` 默认指向**当前
    checkout** 的 `.env`,而 worktree 里没有那个文件,不指过去就是一台没有任何
    模型的后端。显式环境变量优先于 env_file,所以下面这几项照样盖得住。
    """
    env = {
        "DATABASE_URL": args.database_url,
        "SILICON_NOTEBOOK_STORAGE_DIR": args.storage_dir,
        "SILICON_NOTEBOOK_AUTH_OPTIONAL": "true",
        "REASONING_REFLECT_V2_ENABLED": "true" if policy == "v2" else "false",
        # 语料格的「有图/无图」靠 rig 只对 *_kg 格调 `/kg/build` 来区分;继承的
        # 环境或 `--env-file` 若开着 KG_AUTO_EXTRACT,上传即自动建图,A_nokg /
        # B_nokg 就不再无图、`--skip-kg` 也拦不住(codex #700 R12 P2)。显式关掉。
        "KG_AUTO_EXTRACT": "false",
        # 临时后端的事件日志也圈进本次 out-dir(与进程内两条路同一条理由,见
        # `rig_event_log_dir`):rig 起的后端不该往用户平时看的
        # `.local/logs/events-*.jsonl` 里灌几百条实验事件。
        "EVENT_LOG_DIR": rig_event_log_dir(args.out_dir),
        "PORT": str(args.port),
    }
    if args.env_file:
        env["SILICON_NOTEBOOK_ENV_FILE"] = str(Path(args.env_file).expanduser())
    return env


def _apply_process_env(args: argparse.Namespace) -> None:
    """把后端那份环境变量装进**本进程**——`report` 是进程内跑的。

    必须在 `import app.core.config` 之前调:`_ENV_FILE` 与 `Settings` 的字段都
    是 import 期读的,晚一步设 `SILICON_NOTEBOOK_ENV_FILE` 就等于没设。
    `cmd_report` 因此在函数第一行调它,而 `app.*` 的 import 全在更下面的函数体
    里(模块顶部只 import 了零 I/O 的 domain 投影)。
    """
    os.environ.update(backend_env(args, args.policy))


def _backend_command(args: argparse.Namespace) -> list[str]:
    return [
        "uvicorn", "app.main:app", "--port", str(args.port),
        "--app-dir", str(ROOT / "backend"),
    ]


def cmd_seed(args: argparse.Namespace, runner: Runner) -> int:
    questions = load_questions()
    mismatch = _seed_target_mismatch(args)
    if mismatch is not None:
        runner.say("refuse", mismatch)
        return 2
    runner.say(
        "create database",
        f"{_redact_url(args.admin_url)} :: CREATE DATABASE {args.db_name}",
    )
    runner.say("install extensions", ", ".join(PG_EXTENSIONS))
    if not runner.dry_run and not args.skip_create_db:
        _create_test_database(args)

    cells = corpus_cells(args.cell)
    corpus = _stage_corpus(args, runner, questions, cells)
    if corpus is None:
        return 2

    runner.say("start backend",
               shlex.join(_backend_command(args))
               + "  env=" + _redact_env_for_log(backend_env(args, "legacy")))
    backend = _start_backend(args, "legacy") if not runner.dry_run else None
    try:
        return _seed_notebooks(args, runner, corpus, cells,
                               backend_pid=backend.pid if backend else 0,
                               backend_identity=_process_identity(backend.pid)
                               if backend else None)
    except BaseException:
        # 后端在成功路径上刻意**留着**(seed 之后紧接着就是 ask,重起一次要再等
        # 一遍就绪探测),但失败时必须收掉:否则下一次 seed 会撞在同一个端口上,
        # 而错误信息会变成一句无关的「address already in use」。
        if backend is not None:
            backend.terminate()
        raise


def _stage_corpus(
    args: argparse.Namespace, runner: Runner, questions: dict,
    cells: Sequence[str],
) -> dict[str, list[Path]] | None:
    """把要用到的语料落成磁盘上的 markdown。返回 `{"A": [...], "B": [...]}`。

    A 来自主库 `source_elements` 的**只读**重建(整条 seed 里唯一碰主库的一步);
    B 是磁盘上已有的 MinerU markdown,原地引用、不复制。

    **只为被选中的语料格取语料**:`--cell B_kg` 这样只跑 B 的一次 seed 不该
    要求给出主库连接——那会让每一次 B 侧的小验证都得先把主库的凭据摊在命令行上。
    """
    needed = {cell.split("_", 1)[0] for cell in cells}
    work = runner.out_dir / "corpus"
    corpus: dict[str, list[Path]] = {"A": [], "B": []}
    if "A" in needed:
        runner.say(
            "rebuild corpus A (read-only on the source database)",
            f"--source-db-url=<given> notebook={args.source_notebook or '<required>'}",
        )
        if runner.dry_run:
            runner.say("write", str(work / "A" / "<source_id>.md"))
        else:
            if not (args.source_db_url and args.source_notebook):
                print("ERROR: seed 的 A 语料格需要 --source-db-url 与 --source-notebook",
                      file=sys.stderr)
                return None
            for source_id, text in rebuild_source_markdown(
                args.source_db_url, args.source_notebook
            ).items():
                path = work / "A" / f"{source_id}.md"
                runner.write(path, text)
                corpus["A"].append(path)
            if not corpus["A"]:
                print(f"ERROR: 主库 {args.source_notebook} 没有可重建的 source_elements",
                      file=sys.stderr)
                return None

    if "B" not in needed:
        return corpus
    corpus_dir = Path(args.corpus_dir or questions["corpus"]["B"]["dir"])
    for name in questions["corpus"]["B"]["files"]:
        path = corpus_dir / name
        runner.say("stage corpus B file", str(path))
        if not runner.dry_run:
            if not path.is_file():
                print(f"ERROR: B 语料缺文件: {path}", file=sys.stderr)
                return None
            corpus["B"].append(path)
    return corpus


def _seed_notebooks(
    args: argparse.Namespace, runner: Runner, corpus: dict[str, list[Path]],
    cells: Sequence[str], *, backend_pid: int = 0,
    backend_identity: str | None = None,
) -> int:
    """注册 fixture 用户 → 四个语料格各建一个笔记本 → 上传 → 等解析 → 建图。

    **token 全程带上**:`AUTH_OPTIONAL=true` 只是兜底,不带 token 的请求会落到
    seeded admin 名下,而 `report` 是按 `--user` 解析属主的——两边不是同一个人
    时,进程内那半程会在自己刚建的报告上吃权限拒绝。
    """
    base = args.base_url
    password = os.environ.get("T0_FIXTURE_PASSWORD") or secrets.token_urlsafe(24)
    auth = runner.http("POST", f"{base}/api/auth/register",
                       json_body={"username": args.user, "password": password})
    token = str((auth or {}).get("token") or "")
    state: dict[str, Any] = {
        "user": args.user, "token": token, "backend_pid": backend_pid,
        "backend_identity": backend_identity,
        "notebooks": {},
        # `restart` 靠这几项在只有 `--policy` 的情况下重起同一台后端(§2.3):
        # 它读的是这次 seed 真的用了哪套配置,而不是 `restart` 调用自己的
        # 命令行默认值。
        "policy": "legacy",
        "port": args.port,
        "database_url": args.database_url,
        "storage_dir": args.storage_dir,
        "env_file": args.env_file,
    }
    for cell in cells:
        created = runner.http("POST", f"{base}/api/notebooks",
                              json_body={"name": f"t0-{cell}"}, token=token)
        notebook = str((created or {}).get("id") or f"<{cell}>")
        state["notebooks"][cell] = notebook
        paths = corpus[cell.split("_", 1)[0]]
        if runner.dry_run:
            runner.say("upload sources", f"{cell} <- corpus {cell[0]}")
        else:
            upload_corpus(runner, base, notebook, paths, token=token)
            await_parse(runner, base, notebook, token=token,
                        timeout=args.parse_timeout)
        if not cell.endswith("_kg"):
            # 无图格刻意**不**建图:有图/无图 × 单篇/多篇 = 四个语料格,§2.3。
            continue
        if args.skip_kg:
            runner.say("skip kg/build", f"{cell}(--skip-kg)")
            continue
        runner.http("POST", f"{base}/api/notebooks/{notebook}/kg/build",
                    json_body={}, token=token)
        if args.skip_embed:
            runner.say("skip index wait", f"{cell}(--skip-embed)")
        elif not runner.dry_run:
            await_index(runner, base, notebook, token=token,
                        timeout=args.index_timeout)
        else:
            runner.say("await index-status ready",
                       f"GET {base}/api/notebooks/{notebook}/index-status")
    runner.save_state(state)
    return 0


def _url_db_name(url: str) -> str:
    """连接串里的库名(去掉尾斜杠与查询串);SQLite 等非 PG 串返回路径末段。"""
    return str(url or "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def _seed_target_mismatch(args: argparse.Namespace) -> str | None:
    """`seed` 要写的库(`--database-url`)必须就是它要建的库(`--db-name`)。

    两个参数各自有默认值,单改其中一个就会让 CREATE DATABASE 与后面的扩展安装、
    迁移、播种落在两个不同的库上(codex #700 R5 P2)。字面核对,不连库;返回
    `None` 表示一致,否则是给人看的原因。
    """
    if not str(args.database_url or "").startswith(("postgres://", "postgresql://")):
        # 这两条核对保护的是 CREATE DATABASE / 扩展安装那两笔 PG 写;SQLite 冒烟
        # (`--skip-create-db` + `sqlite:///...`,见 scripts/README.md)没有这两笔,
        # 库名与 admin 端点对它都没有意义(codex #700 R7 P2)。
        return None
    actual = _url_db_name(args.database_url)
    if actual != args.db_name:
        return (
            f"--database-url 指向库 {actual!r},但 --db-name 是 {args.db_name!r}:"
            "CREATE DATABASE 与扩展/迁移/播种会落在两个不同的库上,拒绝继续"
        )
    ok, reason = _same_endpoint_literal(args.admin_url, args.database_url)
    if not ok:
        return reason + ":CREATE DATABASE 与扩展/迁移/播种会落在两台服务器上,拒绝继续"
    return None


def _assert_endpoint_free(base_url: str) -> None:
    """起临时后端**之前**确认端口上没有别的后端在应答(codex #700 R5 P2)。

    就绪探测只问「这个 URL 有没有 ready 的后端」,分不清应答的是刚起的子进程还是
    早已占着端口的别人:后者会让 `_start_backend` 在子进程 bind 失败之前就把
    别人的 PID 当成功返回,随后 seed 的注册、上传、建图全打到那台后端上。任何
    HTTP 应答(包括 503/404)都算占用;只有连接被拒/超时才算空闲。
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url}/api/ready", timeout=3) as r:
            r.read()
    except urllib.error.HTTPError:
        pass  # 有 HTTP 应答 ⇒ 端口被占,下面统一报
    except Exception:  # noqa: BLE001 — 连接被拒/超时 = 空闲,是这里想要的结果
        return
    raise RuntimeError(
        f"{base_url} 上已经有后端在应答;拒绝在被占用的端点上起临时后端——"
        "先停掉它或换 --port"
    )


def _create_test_database(args: argparse.Namespace) -> None:
    import psycopg

    # 字面核对在 `_seed_target_mismatch` 已做。带真连接的那一半要等库建出来
    # 才能连 `--database-url`:先建,再比两条连接的服务器身份,不一致就把刚建的
    # 那个库(这次自己建的,别人不可能有数据)收回并响亮失败——扩展/迁移/播种
    # 一笔都不下。
    with psycopg.connect(args.admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{args.db_name}"')
    ok, reason = _admin_targets_same_server(args.admin_url, args.database_url)
    if not ok:
        with psycopg.connect(args.admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{args.db_name}"')
        raise RuntimeError(
            reason.replace("拒绝 DROP", "已回收刚建的库,拒绝安装扩展与播种")
        )
    with psycopg.connect(args.database_url, autocommit=True) as conn:
        for extension in PG_EXTENSIONS:
            conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')


def _start_backend(args: argparse.Namespace, policy: str) -> subprocess.Popen:
    """起一台临时后端,等到它**真的**就绪。

    判据是 `/api/ready` 的 `{"ready": true}`,不是「这个 URL 能连上」:那个探针
    刻意在迁移与预热还在跑时就应答(`app/main.py:492`),而在它翻牌之前所有业务
    路由都是 503。只看连通性会让 seed 的第一个上传撞在 503 上,而错误信息里没有
    一个字提到「后端还没热好」。
    """
    import urllib.request

    _assert_endpoint_free(args.base_url)
    env = dict(os.environ)
    env.update(backend_env(args, policy))
    process = subprocess.Popen(_backend_command(args), env=env, cwd=str(ROOT))
    deadline = time.monotonic() + args.ready_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"temporary backend exited with {process.returncode} during startup"
            )
        try:
            with urllib.request.urlopen(f"{args.base_url}/api/ready", timeout=5) as r:
                if json.loads(r.read().decode("utf-8") or "{}").get("ready"):
                    return process
        except Exception:  # noqa: BLE001 — readiness poll, any failure = not ready
            pass
        time.sleep(2)
    process.terminate()
    raise RuntimeError("temporary backend did not become ready")


def _read_raw_state(state_path: Path) -> dict:
    """原始 state 读取,**不经过** `Runner.load_state` 的 dry-run 短路。

    `Runner.load_state` 在 `--dry-run` 下故意不读磁盘上真的那份 state(其他
    命令的 dry-run 要打印的是「从零开始会做什么」)。但 `restart` 与策略核对
    天生只对「现在这台后端」有意义——dry-run 也该照真实 state 打印停/起计划、
    照真实 state 核对策略,而不是对着一份假的占位状态算。这只是读一个已经
    存在的文件,不写任何东西,不违反 dry-run 的零副作用承诺。
    """
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_state_policy(state_path: Path) -> str | None:
    """state 里记的策略,`None` = 不知道(文件不存在,或从没写过这个键)。"""
    policy = _read_raw_state(state_path).get("policy")
    return policy if isinstance(policy, str) else None


def _assert_policy_matches_backend(runner: Runner, policy: str) -> None:
    """`ask` / `report` 开跑前的策略闸(§2.3)。

    后端当前真的跑在哪个策略上,只有 `restart`(和 `seed`)落过的 state 知道。
    不一致时继续跑只会把 legacy 轨迹当成 v2 导出(或反过来)——一个要等到
    分析阶段才会被发现的静默错误,这里让它当场报响亮失败。
    """
    recorded = _read_state_policy(runner.state_path())
    if recorded is not None and recorded != policy:
        raise RuntimeError(
            f"state 记录的后端策略是 {recorded!r},但这次要用 --policy="
            f"{policy!r}。先执行 `restart --policy {policy}` 切到位,"
            "再跑这条命令"
        )


def cmd_ask(args: argparse.Namespace, runner: Runner) -> int:
    _assert_policy_matches_backend(runner, args.policy)
    questions = load_questions()
    state = runner.load_state()
    plan = ask_plan(
        questions, cells=corpus_cells(args.cell), policy=args.policy,
        limit=args.limit, lang=args.lang, mode=args.mode, efforts=EFFORTS,
    )
    runner.say("policy", f"{args.policy} (REASONING_REFLECT_V2_ENABLED="
                         f"{backend_env(args, args.policy)['REASONING_REFLECT_V2_ENABLED']}"
                         "; 换策略必须重启后端)")
    runner.say("planned runs", str(len(plan)))
    token = str(state.get("token") or "")
    verified = runner.dry_run
    gated = 0
    failed = 0
    for item in plan:
        notebook = state["notebooks"].get(item["corpus_cell"], "<unknown>")
        base = f"{args.base_url}/api/notebooks/{notebook}"
        titles = tuple(item.get("scope_source_titles") or ())
        source_scope_body: dict[str, Any] | None = None
        scope_note = ""
        if titles:
            scope_note = f" scope={len(titles)} sources"
            if not runner.dry_run:
                # 只读一次测试库:`ask` 全程只碰它,不是主库(`search` 那条路
                # 才读主库)。解析不到唯一匹配就跳过这道题的提交——绝不能把
                # 「范围解析失败」悄悄退化成一次不设限的全库检索
                # (codex #700 R3 P2)。dry-run 不做这次 DB 读:模块 docstring
                # 的承诺是「不连库」,这里的 `scope=N sources` 只是静态计数。
                scope_ids = resolve_scope_source_ids(
                    args.database_url, notebook, titles,
                )
                if scope_ids is None:
                    gated += 1
                    runner.say(
                        "ask scope unresolved",
                        f"{item['question_key']} cell={item['corpus_cell']} "
                        f"titles={list(titles)} -> status=failed "
                        "reason=scope_unresolved,跳过提交",
                    )
                    continue
                source_scope_body = {"mode": "include", "source_ids": scope_ids}
        runner.say(
            "ask",
            f"{item['question_key']} cell={item['corpus_cell']} "
            f"effort={item['effort']} mode={item['requested_mode']} "
            f"crid={item['client_request_id']}{scope_note}",
        )
        # 高级界面路径:先 /ask/intent 拿契约,再带着确认过的契约提交。
        # 只有这条路径才让方面账拿到真实的 mandatory_topics(§2.3)。范围一并
        # 带上:生产的 `/ask/intent`(`app.api.ask_routes`)本来就在
        # `source_scope_context` 里算契约,题目声明了范围时不能只在
        # `/ask/stream` 才收窄,那样算出来的方面账会是全库口径的。
        intent_body: dict[str, Any] = {"question": item["question"]}
        if source_scope_body is not None:
            intent_body["source_scope"] = source_scope_body
        contract = runner.http(
            "POST", f"{base}/ask/intent",
            json_body=intent_body, token=token,
        )
        body: dict[str, Any] = {
            "question": item["question"],
            "mode": item["requested_mode"],
            "retrieval_effort": item["effort"],
            "client_request_id": item["client_request_id"],
        }
        if source_scope_body is not None:
            body["source_scope"] = source_scope_body
        if contract is not None:
            # `answers=[]` 会被 `finalize_query_intent` 的门拒掉,只要契约带
            # 必填澄清项(`auto_clarification_answers` 那条确定性代答规则同
            # `_plan_intent`,见其上方注释;codex #700 R1 P2)——没有人在场
            # 回答,但整批 `ask` 也不能因为一道题目的契约带了必填项就起不来。
            body["intent"] = {
                "contract": contract,
                "resolved_question": contract.get("resolved_question")
                or item["question"],
                "answers": auto_clarification_answers(contract),
            }
        # **必须走 /ask/stream,不是 /ask**:同步端点经 `begin_durable_job` 建
        # 行,而那条路 `client_request_id=None` 是写死的
        # (`repositories/postgres/ask_state_store.py:231`,SQLite 侧同形);
        # 只有 `begin_or_attach_durable_job`(即 stream 端点)会把幂等键落库。
        # 走同步端点的话,整批 run 的 `question_key`/`corpus_cell` 全是 unknown
        # ——§4.2 的对照表恒空,而且要跑完几百个 run 才看得出来。
        last = runner.stream("POST", f"{base}/ask/stream", json_body=body, token=token)
        # 执行阶段的失败/取消走 HTTP 200 里的 `event="error"` / `"cancelled"`
        # 帧,不是 HTTP 错误;只看「标签落库」只证明 job 建了,整批全失败也会
        # 退出码 0(codex #700 R9 P2)。终帧不是 `final` 就算失败;只记事件名,
        # 不记 error 正文。
        terminal = "final" if runner.dry_run else str(last.get("event") or "<none>")
        if terminal != "final":
            failed += 1
            runner.say(
                "ask failed",
                f"crid={item['client_request_id']} terminal event={terminal}",
            )
        if not verified:
            _assert_tag_landed(args, runner, item["client_request_id"])
            verified = True
    if failed:
        print(f"ERROR: {failed} 个 ask run 未以 final 帧收尾", file=sys.stderr)
    return 2 if gated else (1 if failed else 0)


def _assert_tag_landed(
    args: argparse.Namespace, runner: Runner, client_request_id: str,
) -> None:
    """第一个 run 之后当场回读:标签真的落进 `ask_jobs` 了吗?

    §4.2 的整套对照都架在这个键上,而它落没落库只取决于走了哪个提交端点——一个
    静默的、要跑完整批才暴露的失败。这里在第一个 run 之后就把它变成响亮失败,
    代价是对**测试库**的一条只读 SELECT。
    """
    from export_reasoning_traces import _Reader

    with _Reader(args.database_url) as reader:
        rows = reader.query(
            "SELECT id FROM ask_jobs WHERE client_request_id = ?",
            (client_request_id,),
        )
    if not rows:
        raise RuntimeError(
            "client_request_id 没有落进 ask_jobs —— 提交端点选错了 "
            f"({client_request_id})。对照表会恒空,先修再跑整批"
        )
    runner.say("tag landed", f"{client_request_id} -> {rows[0]['id']}")


def cmd_report(args: argparse.Namespace, runner: Runner) -> int:
    """4 道综述题 × 当前策略 × B 有图格,**进程内**调 `ReportEngine`。

    逐节轨迹不落库(`reports.sections_json` 只存结果级字段),所以这里不走
    HTTP,而是在进程内拿到生产接线的引擎,用一个只加 tee 的 `ReportEngine`
    子类把 `_deep_dive` 的 `on_step` 复制一份进 JSONL。**子类住在 rig 里,生产
    代码一行不动。**
    """
    _assert_policy_matches_backend(runner, args.policy)
    if not runner.dry_run:
        # dry-run 连 os.environ 都不碰:它的全部承诺是「只打印」。
        _apply_process_env(args)
    questions = load_questions()
    state = runner.load_state()
    plan = report_plan(
        questions, cell=args.cell[0] if args.cell else "B_kg",
        policy=args.policy, limit=args.limit,
    )
    out = runner.out_dir / f"report-trace-{args.policy}.jsonl"
    runner.say("policy", f"{args.policy}(进程内 Settings 由环境变量决定;"
                         "REASONING_REFLECT_V2_ENABLED 由本命令自己设进 os.environ,"
                         "在 import app.core.config 之前)")
    runner.say("in-process ReportEngine",
               "app.bootstrap.create_application_repository(Settings()) -> "
               "_runtime.report_execution.engine_factory")
    runner.say("on_step JSONL", str(out))
    for item in plan:
        notebook = state["notebooks"].get(item["corpus_cell"], "<unknown>")
        runner.say(
            "report",
            f"{item['question_key']} cell={item['corpus_cell']} "
            f"depth={item['depth']}({item['profile']}) notebook={notebook}",
        )
    if runner.dry_run:
        return 0
    return _run_reports(args, runner, plan, state, out)


def _tracing_engine_class() -> type:
    """一个只加 tee 的 `ReportEngine` 子类。**生产代码一行不动。**

    覆盖两个点,都只加旁路、不改行为:

    * `_run_sections` —— 只为把这一轮的 `outline` **按对象身份**记下来。
      `_run_sections` 用 `ThreadPoolExecutor` 并发跑各节
      (`report_engine.py:2570` 一带),所以「第几次调用 `_deep_dive`」不等于
      「第几节」;而 `_retrieve_one(i, section)` 里的那个 `i` 是闭包变量,外面
      拿不到。
    * `_deep_dive` —— 把每一步复制一份给 rig,再逐字转发给原回调(进度文案、
      大纲计数、节流落库还是原来那个闭包在做)。节号用
      `outline.index(section)` 的**身份**版求:`_deep_dive` 拿到的 `section`
      就是 `outline` 里的那个对象。用 `section["title"]` 对号会在两节同名时
      把两节的步混进同一行——报告大纲同名并不稀奇(「小结」「对比」),而混行
      比丢行更坏:它悄悄地把两节的动作序列拼成一条假轨迹。
      标题一个字都不进 JSONL,写出去的只有 `section_index`。
    """
    from app.services.report_engine import ReportEngine

    class _TracingReportEngine(ReportEngine):
        sink: Any = None
        rig_outline: list = []

        def _run_sections(self, notebook_id, rid, outline, question, depth):
            self.rig_outline = list(outline)
            return super()._run_sections(
                notebook_id, rid, outline, question, depth
            )

        def _deep_dive(self, notebook_id, section, question, depth=None,
                       on_step=None):
            index = next(
                (i for i, row in enumerate(self.rig_outline) if row is section),
                -1,
            )

            def tee(step):
                self.sink(index, step)
                if on_step is not None:
                    on_step(step)

            return super()._deep_dive(notebook_id, section, question, depth, tee)

    return _TracingReportEngine


def _run_reports(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    state: dict, out: Path,
) -> int:
    from app.bootstrap import create_application_repository
    from app.core.config import Settings
    from app.core.request_context import reset_request_user, set_request_user
    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.source_scope import source_scope_context

    engine_class = _tracing_engine_class()
    repo = create_application_repository(Settings())
    profile = repo.maintenance.resolve_owner_profile(args.user)
    if profile is None:
        print(f"ERROR: 找不到 fixture 用户: {args.user}", file=sys.stderr)
        return 2
    ctx = set_request_user(profile)
    out.parent.mkdir(parents=True, exist_ok=True)
    log = (runner.out_dir / "report-runs.log").open("a", encoding="utf-8")
    gated = 0
    try:
        with out.open("a", encoding="utf-8") as handle:
            for item in plan:
                notebook = state["notebooks"][item["corpus_cell"]]
                report_id = repo.create_report(
                    notebook, item["question"], depth=item["depth"]
                )
                captured, gate_reason = _generate_report(
                    repo, engine_class, profile, notebook, report_id, item,
                    model_work_scope=model_work_scope,
                    model_priority=ModelPriority.REPORT,
                    source_scope_context=source_scope_context,
                )
                for row in _report_rows(
                    repo, notebook, report_id, item, captured,
                    gate_reason=gate_reason,
                ):
                    assert_closed(row)
                    assert_projection_values(row)
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                if gate_reason is not None:
                    # 卡在澄清门、代答也没能解开(见 `_generate_report`):
                    # 不能再打「report done」——那是这条 codex 发现的原始
                    # 症状,静默看起来完全成功(codex #700 R2 P2)。
                    gated += 1
                    line = f"failed {item['question_key']} reason={gate_reason}"
                    log.write(line + "\n")
                    log.flush()
                    runner.say("report failed", line)
                else:
                    log.write(f"done {item['question_key']}\n")
                    log.flush()
                    runner.say("report done", item["question_key"])
    finally:
        reset_request_user(ctx)
        log.close()
    runner.say("wrote report trace", str(out))
    return 2 if gated else 0


def _generate_report(
    repo: Any, engine_class: type, profile: Any, notebook: str, report_id: str,
    item: dict, *, model_work_scope: Any, model_priority: Any,
    source_scope_context: Any,
) -> tuple[dict[int, list[dict]], str | None]:
    """跑一份报告,返回(`{节号: [步, ...]}`, 门控失败原因或 `None`)。

    两层 scope 与 `report_execution.ReportExecutionCoordinator.start_plan` 逐字
    同形:`model_work_scope`(模型调用的归属与优先级)+ `source_scope_context`
    (范围冻结)。少哪一层都不是「少记一点日志」——模型调度会按另一份归属排队,
    检索会在另一份范围上跑,那就不是生产接线下的行为了。

    入口选 `run(..., auto_generate=True, require_intent_review=True)` 而不是
    `generate(...)`:`generate` 要一份已确认的大纲,报告行是空的时候直接调它没有
    任何东西可生成。这一条正是协调器 `start_plan` 在 `intent_contract is None`
    时走的那条(理解 → 自动确认契约 → 规划大纲 → 生成)。

    `ReportEngine._auto_confirm_intent` 按设计 fail-open 到人工门:契约带必填
    澄清项时,没有人在场回答,`run()` 就原地停在 `intent_ready`(其 docstring
    「Confirm a *clear* intent... or fail open」)。没有这一步的话,报告的
    `sections` 恒空,调用方会把它当成「已完成、只是没写什么」悄悄放过
    (codex #700 R2 P2)。这里代为确认——不是新写一套编排,而是走与
    `POST .../reports/{id}/intent` 手工确认端点**同一条入口**
    (`confirmed_understanding` + `claim_report_intent`,两者都是
    `app.services.reports.intent_confirmation` / repo 上已有的公开函数),
    答案取 `auto_clarification_answers` 那条与 `/ask/intent` 同款的确定性
    规则,确保两侧策略、两次重跑拿到同一份确认。只有这条确认入口本身也走
    不通(代答后仍有必填项没被覆盖,或 CAS 输给了别的写者)时才返回非空的
    `gate_reason`,交给调用方记 `status=failed`。
    """
    from app.services.reports.intent_confirmation import (
        ReportIntentConfirmationError,
        confirmed_understanding,
    )

    captured: dict[int, list[dict]] = {}

    def sink(index: int, step: Any) -> None:
        captured.setdefault(int(index), []).append({
            "step_type": step.step_type,
            "detail": dict(getattr(step, "detail", None) or {}),
            "duration_ms": getattr(step, "duration_ms", None),
        })

    coordinator = repo.report_execution
    # 每份报告新建一台引擎(engine_factory 每次都新装一个 CommunityQueryService,
    # 生产侧也是一份 job 一台),只是把依赖搬进 rig 的子类。
    dependencies = coordinator.engine_factory(
        user_id=str(profile.id), cancel_event=None
    ).dependencies
    engine = engine_class(dependencies, user_id=str(profile.id))
    engine.sink = sink
    gate_reason: str | None = None
    with model_work_scope(
        priority=model_priority, parent_id=report_id,
        actor_id=str(profile.id), notebook_id=notebook,
        question=item["question"],
    ):
        with source_scope_context(notebook, None, None):
            engine.run(
                notebook, report_id, item["question"], "",
                depth=item["depth"], auto_generate=True,
                require_intent_review=True,
            )
            current = repo.get_report(notebook, report_id)
            if str(current.get("status") or "") == "intent_ready":
                contract = dict(current.get("understanding") or {})
                try:
                    frozen = confirmed_understanding(
                        contract,
                        resolved_question=str(
                            contract.get("resolved_question") or ""
                        ),
                        answers=auto_clarification_answers(contract),
                    )
                except ReportIntentConfirmationError:
                    frozen = None
                claimed = bool(
                    frozen is not None
                    and repo.claim_report_intent(notebook, report_id, frozen)
                )
                if claimed:
                    engine.run(
                        notebook, report_id, item["question"], "",
                        depth=item["depth"], auto_generate=True,
                        intent_contract=frozen,
                    )
                    current = repo.get_report(notebook, report_id)
                if not claimed:
                    gate_reason = "clarification_gate"
    # codex #700 R4 P2: two execution paths converge here; any non-done
    # terminal status (planning failure, generation failure, still gated)
    # must project as a failed run instead of being logged as "report done".
    final_status = str(current.get("status") or "")
    if final_status != "done" and gate_reason is None:
        gate_reason = f"report_{final_status or 'unknown'}"
    return captured, gate_reason


def _report_rows(
    repo: Any, notebook: str, report_id: str, item: dict,
    captured: dict[int, list[dict]], *, gate_reason: str | None = None,
) -> Iterable[dict]:
    """逐节**一行**:轨迹投影与结果级投影按 `merge_key` 当场合成。

    `gate_reason` 非 `None`(见 `_generate_report`)时是这条不变量唯一的例外:
    报告卡在澄清门、代答也没能解开,没有一节可对——但也不能一行都不产出,
    「静默 0 行」和「这份报告本来就没内容」在聚合表里长得一模一样
    (codex #700 R2 P2)。这里显式记一行 `status=failed`,不带 `merge_key`
    (没有轨迹半份可以对)。

    轨迹那一半走的是与 Ask 完全相同的 `project_run`——报告的逐节深挖跑的就是
    `ReasoningRetriever.run`,换一份投影只会让两边的口径分叉。结果级那一半走
    `project_report_section`,与 `export_reasoning_traces.py --reports` 是同一个
    函数,所以「rig 出的」和「从库里导的」不可能长出不同的键。

    **合成成一行,而不是各写一行**:分析脚本按格子数 `n_runs`,每节写两行会让
    一份 3 节的报告报成 6 个 run,而每个指标恰好一半「缺失」——§4.1 那套
    「n_observed 用来分辨『真的没记』和『本来就没有』」的分母就此报废,而且它
    看起来完全像一份正常的表。`merge_key(report_id, section_index)` 本来就是为
    了把两半对上(单向哈希,读不回 report id),这里直接用它当场对。

    两半的键除 `policy_version` 外取值相同。冲突的那一个让**轨迹**赢:它是按
    「有没有 v2 终态步」判出来的证据,而结果级那一半只能照抄 rig 的声明——rig
    声明跑的是 v2、而轨迹里一个 v2 终态步都没有时,该被记下来的正是这件事。
    """
    from app.services.report_engine import report_retrieval_effort

    report = repo.get_report(notebook, report_id)
    sections = list(report.get("sections") or [])
    tags = {
        "corpus_cell": item["corpus_cell"],
        "question_key": item["question_key"],
        "consumer": "report_section",
        "trace_source": "in_process",
        "policy_version": item["policy"],
    }
    # 报告没有 Ask 那份 answer payload,但投影要读的三样东西报告都有确定的来源
    # (codex #700 R6 P2):档位按生产的 `report_retrieval_effort(depth)` 映射
    # (`report_retrieval_limits` 就是 `ask_retrieval_limits(该档位)`,所以 legacy
    # 步数预算推断用同一档位的天花板即是报告的有效天花板);意图契约是报告
    # 已确认的 `understanding`。传 None 会让每节都记 effort=unknown、
    # has_intent_contract=false,聚合时把各深度的报告混成一格。
    effort = report_retrieval_effort(int(item["depth"]))
    payload = {
        "mode": "reasoning",
        "retrieval_effort": effort,
        "intent": report.get("understanding") or None,
    }
    # 逐节深挖的有效反思轮上限:`ReportEngine._deep_dive` 传 `max_steps=depth`,
    # 检索器取 `min(depth, 档位上限)`(codex #700 R10 P2)。只按档位表判,depth=2
    # 的报告两轮就停会被记成 unknown 而不是 step_budget。
    step_ceiling = min(int(item["depth"]), MAX_REFLECT_STEPS.get(effort, int(item["depth"])))
    if gate_reason is not None:
        row = project_report_section(
            {}, section_index=0, section_total=0,
            report_depth=item["depth"], report_id=report_id, rig_tags=tags,
        )
        row.pop("merge_key", None)
        row["section_index"] = None
        row["status"] = "failed"
        # 成功行是「结果级 + 轨迹级」合成的一行,带 effort/mode/trace_source/
        # policy_version;失败行也要带同样的工作负载维度,否则 `pair_table` 按这
        # 几个维度分格时,失败的那一侧掉进另一个格,对照整个消失(codex #700 R14
        # P2)。这些值都是 rig 已知的声明,不是编造的证据。
        row.update({
            "effort": effort, "mode": "reasoning", "trace_source": "in_process",
            "policy_version": item["policy"],
            "has_intent_contract": bool(report.get("understanding")),
            # `project_report_section({})` 把 `failed` 初始化成 False——那是「这一
            # 节没失败」的观测值,而这里根本没有节:整份报告在规划/生成阶段失败
            # 了。记 True,`summarize_group` 的 failed 指标才不会把一批全失败的
            # 报告统计成零失败(codex #700 R15 P2)。
            "failed": True,
        })
        assert_closed(row)
        assert_projection_values(row)
        yield row
        return
    for index, section in enumerate(sections):
        # 节号来自 `_deep_dive` 时按对象身份求出的那个 index,与
        # `sections_json` 的下标同源(`_run_sections` 按 `enumerate(outline)`
        # 发任务、按同一顺序收结果)。对不上时留空列表,而不是猜。
        steps = captured.get(index, [])
        row = project_report_section(
            section, section_index=index, section_total=len(sections),
            report_depth=item["depth"], report_id=report_id, rig_tags=tags,
        )
        trace_row = project_run(
            {"mode": "reasoning", "status": "done"}, steps, payload,
            sources_count=None, rig_tags=tags, step_ceiling=step_ceiling,
        )
        row.update(trace_row)
        row["merge_key"] = merge_key(report_id, index)
        row["section_index"] = index
        row["section_total"] = len(sections)
        row["report_depth"] = item["depth"]
        yield row


# --- search:主库只读的进程内检索 run --------------------------------------


def cmd_search(args: argparse.Namespace, runner: Runner) -> int:
    """两个既有笔记本 × 两个策略 × 两个档位,**进程内只跑检索过程**。

    与 `ask` 的分工:`ask` 是「建测试库 → 起后端 → 走 HTTP → 答案落库 → 轨迹
    从库里导」;`search` 是「对**主库只读**跑 plan + reflect 循环 → 轨迹在进程
    内投影成 JSONL」。它**不建测试库、不建图、不合成答案**,所以 ask_jobs /
    answers / conversations 一行都不写,`knowledge_objects` 也一个字节不动。

    策略切换不重启:`reasoning_reflect_v2_enabled` 是 Settings 字段,而
    `ReasoningRetriever.from_repository(repo, settings)` 用的是**传入的**那份
    settings(`reflect_v2_active()` 只读 `self.settings` 与 `allow_reflect_v2`,
    两者都不经 repo)。所以这里为两个策略各构造一份 Settings,在同一个 repo 上
    交替跑,一次进程跑完整批。每个 run 开跑前还会当场核对
    `retriever.reflect_v2_active()` 与这条 run 声明的策略一致——协议对不对号是
    这批数据唯一的立身之本,不能只靠"我设过那个环境变量"。
    """
    questions = load_questions()
    cells = search_cells(args.cell)
    if args.only_cell:
        # `--only-cell` 收窄的是「这次要跑哪些格」本身,所以在这里做,而不是
        # 塞进 `search_plan` 内部——下面 `if not cells` 的预检、「corpus cell」
        # 的逐条打印,与真跑要用的 `notebooks` 查找,都得看到同一份收窄结果。
        cells = [cell for cell in cells if cell in args.only_cell]
    notebooks = {
        "A_nokg": args.source_notebook_a, "B_kg": args.source_notebook_b,
    }
    if not cells:
        # 这一条在 `--dry-run` 下也拦:选空了的 `--cell`/`--only-cell` 会让
        # dry-run 打印一份「零个 run」的计划,而那看起来完全像一次正常的预演。
        print("ERROR: " + _search_preflight(args, cells, notebooks),
              file=sys.stderr)
        return 2
    # `--only-policy` 只在重跑某一侧(如一侧被网络故障整批打废)时用:配对表
    # 靠 question_key+corpus_cell+effort 对上,两侧分两次进程跑不影响配对。
    policies = tuple(args.only_policy) if args.only_policy else POLICIES
    plan = search_plan(
        questions, cells=cells, policies=policies, limit=args.limit,
        lang=args.lang, efforts=EFFORTS, only_questions=args.only_question,
    )
    unique_questions = {
        (item["corpus_cell"], item["question_key"]) for item in plan
    }
    runner.say("target",
               "主库只读:不建库、不建图、不合成,只跑 plan + reflect 循环")
    runner.say("database-url",
               "<given>" if args.database_url_explicit else "<MISSING:必须显式给>")
    for cell in cells:
        runner.say("corpus cell",
                   f"{cell} -> notebook={notebooks.get(cell) or '<required>'}")
    runner.say("policies",
               ", ".join(policies) + "(同进程换 Settings,不重启后端)")
    runner.say("efforts", ", ".join(EFFORTS))
    runner.say("planned runs",
               f"{len(plan)}({len(unique_questions)} 题 × {len(policies)} 策略 "
               f"× {len(EFFORTS)} 档)")
    runner.say("model calls (estimate)",
               _search_call_estimate(plan, len(unique_questions),
                                     no_intent=args.no_intent))
    runner.say("out",
               f"{runner.out_dir}/search-<policy>.jsonl + search-runs.log"
               + ("" if args.no_intent else
                  f" + {runner.out_dir}/intents.jsonl(**不进数据集/仓库**)"))
    if args.keep_raw_trace:
        runner.say(
            "raw trace",
            f"{runner.out_dir}/raw/<policy>/<question_key>_<corpus_cell>_"
            "<effort>.json(每个 run 一份原始 TraceStep 列表 + termination DTO,"
            "**含标题与模型 reason,不进数据集/仓库**)",
        )
    runner.say(
        "concurrency",
        f"{args.concurrency}(两阶段:先并发算意图契约,再并发跑 run;"
        "真跑时 SQLite 主库强制 1,PG 主库按 POSTGRES_POOL_MAX_SIZE clamp——"
        "跑之前先确认模型服务限流与连接池都吃得下这个数)",
    )
    if runner.dry_run:
        for item in plan:
            titles = item.get("scope_source_titles") or ()
            scope_note = f" scope={len(titles)} sources" if titles else ""
            runner.say(
                "search",
                f"{item['question_key']} cell={item['corpus_cell']} "
                f"policy={item['policy']} effort={item['effort']}{scope_note}",
            )
        return 0
    return _run_search(args, runner, plan, notebooks, cells)


def _search_call_estimate(
    plan: Sequence[dict], question_count: int, *, no_intent: bool,
) -> str:
    """每 run 的模型调用估计。**是上界,不是预测**。

    意图契约每题只算一次并缓存(同一题的四个 run 共用那一份,契约是
    corpus-blind 的);**带**契约的 run 不再调 plan —— 已确认意图直接作首轮
    种子,`ReasoningRetriever` 的 `reviewed_queries` 分支整个跳过 `plan()`。
    `--no-intent` 反过来:零 intent 调用,每个 run 付一次 plan。reflect 每轮
    一次,上界是该档位的 `max_reasoning_steps`(`MAX_REFLECT_STEPS` 读的是
    `ask_retrieval_policy` 那一份唯一真源)。

    末尾另报一条**请求上界**,与 `_ab_call_estimate` 同一口径、同一条理由:上面
    那个数是**逻辑调用**数,请求数比它大(重试与静默 fallback),两个数不要混用。
    """
    intent_calls = 0 if no_intent else question_count
    plan_calls = len(plan) if no_intent else 0
    reflect_ceiling = sum(
        MAX_REFLECT_STEPS.get(item["effort"], 0) for item in plan
    )
    total = intent_calls + plan_calls + reflect_ceiling
    return (
        f"intent {intent_calls} + plan {plan_calls} + reflect ≤ "
        f"{reflect_ceiling}  ⇒  ≤ {total} 次逻辑调用;"
        f"请求上界 ≤ {total * REASONING_ATTEMPT_BUDGET}"
        f"(重试预算 ×{REASONING_ATTEMPT_BUDGET};静默 fallback 另计)"
    )


def rig_llm_log_path(out_dir: str) -> str:
    """rig 的两条进程内路径把 LLM 交互日志改落到**这次跑批自己的 out-dir**。

    默认落点(`.local/logs/llm.jsonl`)是**整台机器共用**的:同一天里在同一个
    checkout 上跑过的每个后端、每次冒烟、每个别的 rig 会话,都往同一个
    `llm-YYYY-MM-DD.jsonl` 里追加。`ab` 的成本三键靠时间窗切片归因(§7.2),
    在共用日志上切片会把**同机别的进程**的模型调用记到本 run 头上——那不是
    「数得不准」,是数了别人的(codex 质量评审 P1-2 / 规格评审 P2-2)。

    换成 out-dir 之后,读侧(`_ab_llm_log_dir`)读的是同一份 `Settings
    .llm_log_path`,所以两侧天然同源:改这一条不需要同时改读侧。
    """
    return str((Path(out_dir).expanduser() / "llm" / "llm.jsonl").resolve())


def rig_event_log_dir(out_dir: str) -> str:
    """rig 的两条进程内路径把**事件日志**也改落到这次跑批自己的 out-dir。

    与 `rig_llm_log_path` 是同一条理由的另一半,而这一半此前是漏的:
    `EVENT_LOG_DIR` 的默认值 `.local/logs` 同样是**整台机器共用**的,rig 一跑就
    往用户平时看的 `events-YYYY-MM-DD.jsonl` 里灌几百条 `model_scheduler`——rig
    的调度事件与这台机器上真实使用产生的事件从此混在一份文件里,两个方向都坏:
    看日志的人被灌一批实验噪声,而 per-call 表(`reflect_context_bench.join_calls`)
    的右表里混进了别的进程的调用行(计划 §3 T-PS5 的 G5)。

    落点是 `<out-dir>/events`,与 llm 日志的 `<out-dir>/llm` **并列而不同目录**。
    这会让 `repository_runtime` 打一行「LLM_LOG_PATH 的目录与 EVENT_LOG_DIR 不
    一致」的告警——那条告警说的是「日志查看器读不到 per-user 的 llm 日志」,而
    rig 的产物没有查看器要读,两份日志各自按自己的 glob 读(见
    `AB_LLM_LOG_GLOB` / `RIG_EVENT_LOG_GLOB`)。合成一个目录能消掉那行告警,但
    会让两串按天分文件的日志挤在一起,`export`/归档时更难分。
    """
    return str((Path(out_dir).expanduser() / "events").resolve())


def _rig_process_env(
    args: argparse.Namespace, *, database_url: str,
) -> dict[str, str]:
    """rig 的进程内路径(`search` / `ab`)装进**本进程**的环境变量。
    必须在 import `app.core.config` 之前设。

    **一份清单、两个调用方**(codex 质量评审 P2-9):这几项此前在
    `_search_process_env` 与 `_ab_process_env` 里逐字重复了两遍,而漏一项在数据
    上看不出任何区别——`search` 加了闸、`ab` 忘了加,产出的两批数据形状完全
    一样。键集相同由用例钉住。

    `DATABASE_URL` 由调用方给:`search` 指主库(只读跑检索),`ab` 指
    `seed` 建出来的一次性测试库(要往里写 conversation 与 answer 行)。两条都
    显式覆盖、不从 `.env` 猜——那个文件里写的是这台机器平时连的库。

    三把**注入闸强制关**(§5.5-4)。它们本来就默认关、而且 `from_repository`
    压根不接 `agent_profile` / `retrieval_experiences` / `identity_store` 三个
    端口(所以注入在结构上就不可达),这里再关一次是为了让「注入面全关」不
    依赖于「那三个端口恰好没接线」这个会随重构漂移的前提:注入闸一关,
    `experience_wiring_active` 恒假,收尾那条 `note_adopted` UPDATE 就连分支
    都进不去。

    `LLM_LOG_PATH` 见 `rig_llm_log_path`,`EVENT_LOG_DIR` 见 `rig_event_log_dir`
    ——两条都把机器共用的默认落点换成本次 out-dir,rig 因此**不再往
    `.local/logs/events.jsonl` 写一个字节**。
    """
    env = {
        "DATABASE_URL": database_url,
        "RETRIEVAL_EXPERIENCE_INJECT_ENABLED": "false",
        "REASONING_CONSULT_MEMORY_ENABLED": "false",
        "AGENT_PROFILE_ENABLED": "false",
        "LLM_LOG_PATH": rig_llm_log_path(args.out_dir),
        "EVENT_LOG_DIR": rig_event_log_dir(args.out_dir),
    }
    if args.env_file:
        env["SILICON_NOTEBOOK_ENV_FILE"] = str(Path(args.env_file).expanduser())
    return env


def _search_process_env(args: argparse.Namespace) -> dict[str, str]:
    """`search` 的那一份。`DATABASE_URL` 指**主库**(全程只读)。"""
    return _rig_process_env(args, database_url=args.database_url)


def _settings_by_policy() -> dict[str, Any]:
    """两个策略各一份 `Settings`。**不重启后端,也不热改已构造好的那一份**。

    构造走环境变量而不是 `model_copy(update=...)`:`Settings` 带跨字段校验与
    别名解析,绕过构造器改一个布尔位不会重跑它们,而这里要的正是「像后端那样
    按 `REASONING_REFLECT_V2_ENABLED` 起来的一份配置」。显式环境变量优先于
    env_file,所以 `--env-file` 里就算写了这一项也盖不住;万一被盖住,下面那
    条断言当场报错,而不是安静地跑出一批策略对不上号的轨迹。
    """
    from app.core.config import Settings

    built: dict[str, Any] = {}
    for policy in POLICIES:
        os.environ["REASONING_REFLECT_V2_ENABLED"] = (
            "true" if policy == "v2" else "false"
        )
        settings = Settings()
        if bool(settings.reasoning_reflect_v2_enabled) is not (policy == "v2"):
            raise RuntimeError(
                f"Settings 没有按 {policy!r} 起来:REASONING_REFLECT_V2_ENABLED "
                "被别处盖掉了"
            )
        built[policy] = settings
    return built


def _settings_by_arm(
    arms: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], Any]:
    """`ab` 的每条臂各一份 `Settings`,按 `(policy, optimization)` 索引。

    与 `_settings_by_policy`(`search` 用的那一份)同一条构造纪律——走环境变量而
    不是 `model_copy(update=...)`,因为 `Settings` 带跨字段校验与别名解析,绕过
    构造器改一个字段不会重跑它们,而这里要的正是「像后端那样起来的一份配置」。
    多出来的只有**第二维**:每条臂在构造前把 `REASONING_REFLECT_OPTIMIZATION`
    设成自己那一格。

    两条断言都是「不断言就会安静跑出一批臂对不上号的数据」:

    * v2 总闸按 policy 对号(与 `_settings_by_policy` 逐字同一条);
    * optimization 按声明对号。不在 `Literal` 四格里的拼写(比如手滑写成
      `prefix_delta_leen`)由 pydantic 在**构造期**就抛 `ValidationError`,所以
      那条路径不会走到这里;这条断言挡的是**别名或接线漂移**——`Settings` 的
      字段别名改了名、`--arms` 的取值与配置枚举分了叉、或者这个函数哪天不再
      逐臂设那个环境变量。
      它挡不住的是「被 `--env-file` 盖住」:pydantic-settings 默认 env > dotenv,
      而 `--arms` 那几行是显式环境变量,`--env-file` 盖不过它(实测确认)。
    * 测量开关按 `_ab_process_env` 的约定核一次:两臂都必须是开的,否则差值表
      只有一侧有数。

    进程环境在这里被**逐臂改写**,离开时留在最后一条臂上——与
    `_settings_by_policy` 同一个既有形态。它不影响任何东西:两条臂各自读的是
    自己那份已经构造好的 `Settings`(臂由 repo 承载,见 `run_ab_once`),而
    `Settings()` 在这个函数之后不再被构造。
    """
    from app.core.config import Settings

    built: dict[tuple[str, str], Any] = {}
    for policy, optimization in arms:
        os.environ["REASONING_REFLECT_V2_ENABLED"] = (
            "true" if policy == "v2" else "false"
        )
        os.environ["REASONING_REFLECT_OPTIMIZATION"] = optimization
        settings = Settings()
        label = format_arm(policy, optimization)
        if bool(settings.reasoning_reflect_v2_enabled) is not (policy == "v2"):
            raise RuntimeError(
                f"Settings 没有按 {label!r} 起来:REASONING_REFLECT_V2_ENABLED "
                "被别处盖掉了"
            )
        if str(settings.reasoning_reflect_optimization) != optimization:
            raise RuntimeError(
                f"Settings 没有按 {label!r} 起来:"
                "REASONING_REFLECT_OPTIMIZATION 实际是 "
                f"{settings.reasoning_reflect_optimization!r}。显式环境变量胜过 "
                "--env-file,所以这不是被 .env 盖住——多半是字段别名或这里的接线"
                "改了名,rig 设的那个环境变量已经没人读"
            )
        if not bool(settings.reasoning_reflect_measure_context):
            raise RuntimeError(
                f"臂 {label!r} 的 REASONING_REFLECT_MEASURE_CONTEXT 没开:两臂必须"
                "用同一把尺子(拍板 Q2),只有一侧有测量列的差值表出不了结论"
            )
        built[(policy, optimization)] = settings
    return built


#: `_readonly_counts` 除了 `READONLY_TABLES` 的行数,还额外带一个**按值**的
#: 信号(codex #700 R1 P1)。`bundle._initialize` 的 admin 密码重写是一次
#: `UPDATE users`,行数一根毫毛不动——`READONLY_TABLES` 那套按 `COUNT(*)` 的
#: 断言对它是瞎的,这条真实事故(生产主库 `users` 表 admin 行的 `updated_at`
#: 被改)就是从这个盲区钻过去的。
USERS_UPDATED_AT_MAX_KEY = "users_updated_at_max"


def _users_updated_at_max(database_url: str) -> str | None:
    """`users` 表 `updated_at` 的表内最大值。表不存在/空表记 `None`。"""
    from export_reasoning_traces import _Reader

    try:
        with _Reader(database_url) as reader:
            rows = reader.query("SELECT MAX(updated_at) AS m FROM users")
        value = rows[0]["m"] if rows else None
        return str(value) if value is not None else None
    except Exception:  # noqa: BLE001 — 数不出来就是 unknown,不当成"没变"
        return None


def _readonly_counts(database_url: str) -> dict[str, int | str | None]:
    """`READONLY_TABLES` 的行数快照 + `users.updated_at` 的最大值。**每张表
    一条独立的只读连接**。

    一条连接跑五次 count 更省,但 PG 上任何一条失败(表不存在)会把整个事务打
    成 aborted、后面四张跟着报错——那会让「有一张表改名了」看起来像「五张表都
    没了」。一张表一条连接,一次失败只影响它自己。连接由 `_Reader` 开
    `read_only`,所以这几条 count 在服务端就写不动任何东西。
    """
    from export_reasoning_traces import _Reader

    counts: dict[str, int | str | None] = {}
    for table in READONLY_TABLES:
        try:
            with _Reader(database_url) as reader:
                rows = reader.query(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(rows[0]["n"]) if rows else 0
        except Exception:  # noqa: BLE001 — 表不存在 ⇒ 这次不看它(记 None)
            counts[table] = None
    counts[USERS_UPDATED_AT_MAX_KEY] = _users_updated_at_max(database_url)
    return counts


def _source_count(database_url: str, notebook_id: str) -> int | None:
    """一个笔记本的来源数,只读。投影里只以 `notebook_bucket`(桶)出现。"""
    from export_reasoning_traces import _Reader

    try:
        with _Reader(database_url) as reader:
            rows = reader.query(
                "SELECT COUNT(*) AS n FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            )
        return int(rows[0]["n"]) if rows else 0
    except Exception:  # noqa: BLE001 — 数不出来就是 unknown,不猜 0
        return None


def resolve_scope_source_ids(
    database_url: str, notebook_id: str, titles: Sequence[str],
) -> list[str] | None:
    """把题集里的显式来源标题解析成目标笔记本里的 `source_id` 列表,只读。

    按**子串**匹配,不是精确相等:`search` 读的是主库既有笔记本,
    `sources.title` 是上传时的原始文件名;`ask`/`seed` 把**同一份**磁盘文件
    重新上传进一次性测试库,那份磁盘文件名本身就带着主库存储层加的
    `{source_id}_` 前缀(`app.repositories.source_files.stored_upload_name`),
    所以测试库那份 `title` 是"主库标题前面多一段前缀"的超串。子串匹配让题集
    只需要写一份标题,两条消费路径都能解析到位;写精确相等则测试库那侧
    永远解析不到(见题集 B-q09 的 `note`)。

    每个标题必须在这个笔记本里**恰好**命中一个来源;命中 0 个或 ≥2 个,整体
    判失败、返回 `None`。调用方据此把这道题标 `status=failed`(原因
    `scope_unresolved`),不能把「解析不到位」悄悄退化成不设限的全库检索
    ——那正是 codex #700 R3 P2 指出的根因:两条消费路径此前都无条件装配
    「不收窄」的范围,`scope_sources` 这个字段只在题集里存在、从没有一处
    真的按它建过范围。
    """
    from export_reasoning_traces import _Reader

    try:
        with _Reader(database_url) as reader:
            rows = reader.query(
                "SELECT id, title FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            )
    except Exception:  # noqa: BLE001 — 查不出来就是解析失败,不是「没有范围」
        return None
    resolved: list[str] = []
    for title in titles:
        matches = [
            str(row["id"]) for row in rows
            if title and title in str(row.get("title") or "")
        ]
        if len(matches) != 1:
            return None
        resolved.append(matches[0])
    return resolved


#: `_assert_readonly` 逐个比对的键:`READONLY_TABLES` 的行数,加上按值比对的
#: `USERS_UPDATED_AT_MAX_KEY`(行数断言看不见的那一类写)。
READONLY_CHECKED_KEYS: tuple[str, ...] = (*READONLY_TABLES, USERS_UPDATED_AT_MAX_KEY)


def _format_counts(counts: dict[str, int | str | None]) -> str:
    return ", ".join(
        f"{key}={counts.get(key) if counts.get(key) is not None else 'n/a'}"
        for key in READONLY_CHECKED_KEYS
    )


#: 「主库这次真的连上了」的证据表。`_readonly_counts` 对每张数不出来的表都记
#: `None`,而 before/after 全 `None` 会**相等** ⇒ `_assert_readonly` 报「readonly
#: ok」——一条指错库、或者压根连不上的 `--source-db-url`,于是让 §5.5-2 那条
#: 「主库零接触」的硬断言静默通过(codex 质量评审 P1-1)。这三张是 Ask 这条路
#: 的写入面,任何一台真正的主库上都必然存在;三张全数不出来只能是「没连上」。
READONLY_PROOF_TABLES: tuple[str, ...] = ("ask_jobs", "answers", "conversations")


def readonly_baseline_problem(counts: dict[str, int | str | None]) -> str:
    """基线快照够不够格当证据。返回空串 = 够,否则是要打给用户的那句话。

    未验证不等于通过:三张证据表一张都数不出来时,后面那次 before==after 的
    比较证明不了任何事,所以这里直接把整批挡在第一个模型调用之前。
    """
    if any(counts.get(table) is not None for table in READONLY_PROOF_TABLES):
        return ""
    return (
        "主库基线点不出任何一张证据表("
        + "/".join(READONLY_PROOF_TABLES)
        + ")——`--source-db-url` 多半指错库或连不上。"
        "跑前跑后全是 unknown 会让「主库零接触」这条断言恒等成立(§5.5-2),"
        "而未验证不等于通过"
    )


def _assert_readonly(
    runner: Runner,
    before: dict[str, int | str | None],
    after: dict[str, int | str | None],
    *,
    command: str = "跑批",
) -> None:
    """跑前跑后,`READONLY_CHECKED_KEYS` 逐项必须相等。不等就标红并报错。

    `command` 只进错误文案。写死「search」会让 `ab` 的读者以为报错来自另一条
    根本没跑的命令。
    """
    drifted = {
        key: (before.get(key), after.get(key))
        for key in READONLY_CHECKED_KEYS
        if before.get(key) != after.get(key)
    }
    if not drifted:
        runner.say("readonly ok", _format_counts(after))
        return
    detail = ", ".join(
        f"{key}: {old} -> {new}" for key, (old, new) in sorted(drifted.items())
    )
    print(f"\033[31m[READ-ONLY VIOLATION]\033[0m {detail}", file=sys.stderr)
    raise RuntimeError(f"主库在这次 {command} 之后变了: " + detail)


class _IntentCache:
    """每题只算一次意图契约,落 `intents.jsonl` 复用。

    **这个文件不进数据集、不进仓库**:契约里带着问题原文与模型改写过的
    `resolved_question`,是自由文本,而 rig 写进 JSONL 的每一行都受闭集约束
    (§6)。它落在 `--out-dir`(默认在 `.local/` 下)只为一件事:一次中断的
    rig 重跑时不用再付一遍 intent 的模型调用。

    键取 `question_key`,不带语料格:契约是 **corpus-blind** 的
    (`plan_query_intent` 一个字的语料都不读),而 A/B 两格的题号本来就不同。

    **`_lock` 只保护「查缓存 / 落缓存 / 写文件」这三步,不盖住模型调用那一段**:
    并发预算的意义就在于让多题的模型调用真的并行,锁只用来防两件事——同一题被
    两个线程各算一次(双重检查:锁内确认仍然缺失才去算),以及并发写
    `intents.jsonl` 在磁盘层面交错成半行。
    """

    def __init__(self, path: Path, *, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()
        if not (enabled and path.exists()):
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = str(row.get("question_key") or "")
            if key and isinstance(row.get("contract"), dict):
                self._rows[key] = row["contract"]

    def get(
        self, repo: Any, settings: Any, item: dict, *, actor_id: str,
        notebook: str,
    ) -> dict | None:
        if not self.enabled:
            return None
        key = item["question_key"]
        with self._lock:
            if key in self._rows:
                return self._rows[key]
        contract = _plan_intent(
            repo, settings, item["question"], actor_id=actor_id,
            notebook=notebook,
        )
        with self._lock:
            if key not in self._rows:
                self._rows[key] = contract
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(
                        {"question_key": key, "contract": contract},
                        ensure_ascii=False,
                    ) + "\n")
            return self._rows[key]


def _plan_intent(
    repo: Any, settings: Any, question: str, *, actor_id: str, notebook: str,
) -> dict:
    """一次 `/ask/intent` 的进程内等价物 + 「问题清晰、直接确认」那一下。

    两步逐字照 `AskService.preview_reasoning_intent` 与
    `_confirmed_reasoning_intent`:`plan_query_intent` 出契约,
    `finalize_query_intent(..., answers=[])` 冻结它。**没有人在场回答澄清项,
    rig 也不替用户编答案**——那正是高级界面里「问题清晰 ⇒ 自动确认」走的那条,
    也是 `ask` 子命令通过 HTTP 走的那条。

    契约是 corpus-blind 的,所以这一步不需要 notebook 也不产生任何检索;
    `notebook` 只用来给模型调度的归属打标(与检索 run 同一个 scope 形状)。
    """
    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.query_intent import finalize_query_intent, plan_query_intent

    with model_work_scope(
        priority=ModelPriority.INTERACTIVE, actor_id=actor_id,
        notebook_id=notebook, question=question,
    ):
        seed = plan_query_intent(
            repo.chat("reasoning_agent"), question, "",
            max_topics=settings.reasoning_max_subqueries,
            purpose="step-by-step evidence-grounded answer",
        )
    return finalize_query_intent(
        seed,
        resolved_question=str(seed.get("resolved_question") or ""),
        answers=auto_clarification_answers(seed),
    )


# 无人在场时替用户回答必填澄清项的**确定性**规则:有选项取第一个,没有就用一句
# 「不额外限定」。首跑实测 8 题里有题目的契约带必填澄清项,`answers=[]` 会被
# `finalize_query_intent` 的门拒掉、整批 run 起不来。规则写死是为了两侧策略、两次
# 重跑拿到**同一份**契约;答案本身只影响 `clarification_answers` 与范围信号,不进
# 数据集。
AUTO_CLARIFICATION_ANSWER = "按问题原意理解，不额外限定"


def auto_clarification_answers(seed: dict) -> list[dict]:
    """`finalize_query_intent(answers=...)` 的确定性代答:只答必填项。"""
    answers: list[dict] = []
    for row in seed.get("ambiguities") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if row.get("required") is False:
            continue
        options = [
            str(option).strip() for option in (row.get("options") or [])
            if str(option).strip()
        ]
        answers.append({
            "id": str(row["id"]),
            "answer": options[0] if options else AUTO_CLARIFICATION_ANSWER,
        })
    return answers


def _prepare_search_intent(
    contract: dict | None, question: str, effort: str,
) -> dict | None:
    """已确认契约 → `run()` 的三个入参,逐字照 `_prepare_reasoning_ask`。

    * `research_question`:合成后的检索问题(`confirmed_research_question`);
    * `intent_queries`:首轮种子。**非空时 `run()` 不调 plan 的 LLM**——已确认
      意图是权威,检索器直接拿它当首轮子查询;
    * `intent_detail`:`ReasoningIntentProjection.as_json_mapping()`,v2 的方面
      账从它拿 `mandatory_topics`。

    `max_queries` 与 Ask 同式 `max(max_initial_subqueries, 1 + 必答方面数)`:
    档位限的是首轮**并发宽度**,不是「哪些必答方面配得到一个种子」;按宽度截会
    在检索器看到它们之前就丢掉靠后的方面。
    """
    if contract is None:
        return None
    from app.application.ask_reasoning import ReasoningIntentProjection
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.models.ask import QueryIntentContract
    from app.services.query_intent import (
        confirmed_intent_queries,
        confirmed_research_question,
    )

    frozen = QueryIntentContract(**contract)
    payload = frozen.model_dump()
    # 与 `_prepare_reasoning_ask` 的 `auto_confirmed_clear_intent` 同一个判据:
    # 提交了契约、没有待澄清项、**没有提交过澄清答案** ⇒ 用户原文仍是首要权威
    # (codex #700 R2 P2)。`finalize_query_intent` 恒把 `needs_clarification`
    # 清空,所以只看这一位在 rig 里恒真——真正把「代答过」和「本来就没歧义」
    # 分开的是 `clarification_answers`:代答过必填项的题,合成后的
    # `resolved_question` 才是权威(研究问题与首轮种子随之取确认后的方向,而
    # 不是用户原文)。
    authoritative = (
        not frozen.needs_clarification and not frozen.clarification_answers
    )
    limits = ask_retrieval_limits(effort)
    return {
        "research_question": confirmed_research_question(
            payload, question, objective_is_authoritative=authoritative,
        ),
        "intent_queries": confirmed_intent_queries(
            payload, question, objective_is_authoritative=authoritative,
            max_queries=max(
                limits.max_initial_subqueries, 1 + len(frozen.mandatory_topics)
            ),
        ),
        "intent_detail": ReasoningIntentProjection(
            resolved_question=frozen.resolved_question,
            result_scope=frozen.result_scope,
            completeness_required=frozen.completeness_required,
            retrieval_effort=effort,
            entities=tuple(frozen.entities),
            constraints=tuple(frozen.constraints),
            excluded_topics=tuple(frozen.excluded_topics),
            assumptions=tuple(frozen.assumptions),
            expected_output=frozen.expected_output,
            mandatory_topics=tuple(
                topic.question for topic in frozen.mandatory_topics
            ),
        ).as_json_mapping(),
    }


def run_search_once(
    repo: Any, settings: Any, *, notebook: str, item: dict,
    prepared: dict | None, on_step: Any, cancel_event: Any, actor_id: str,
    scope_source_ids: Sequence[str] | None = None,
) -> Any:
    """一个 run:三层 scope + `ReasoningRetriever.run`。生产接线的逐字复刻。

    三层 scope 与 Ask 生产路径同形,少哪一层都不是「少记一点日志」:

    * `model_work_scope(INTERACTIVE)` —— 模型调用的归属与优先级(Ask 是交互档,
      不是报告档;两者的 deadline 差一个数量级);
    * `retrieval_run(run_kind="ask_reasoning")` —— 请求级的查询向量 memo 与扇出
      预算,`kg_in_scope_for` 的 memo 也挂在它上面。**`event_log=None`**:那个
      事件汇是这条路上唯一会往库里写的旁路,主库只读,所以刻意不接;
    * `source_scope_context(notebook, scope, None)` —— `scope_source_ids` 为空
      时 `scope=None`,是个 no-op,与 `_generate_report` 那半程同形;非空时
      装配成 `mode="include"` 的本地范围(与生产 `AskRequest.source_scope` /
      `SourceScope` 同一形状),调用方(`_search_loop`)已经用
      `resolve_scope_source_ids` 把题集声明的来源标题解析成这里的 id 列表
      ——解析不到位的题从不会走到这里(codex #700 R3 P2)。

    注入面全部不接:`from_repository` 不传 `agent_profile` /
    `retrieval_experiences` / `identity_store`(见 `_construct_reasoning_retriever`
    的说明),所以 `profile_owner_id` 保持空串——那是**合法且安全**的取值,意思
    是「只用共享底座,不碰任何人的私有覆盖层」。属主身份仍然真实存在:它走
    `set_request_user` 与这里的 `actor_id`。
    """
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval_run import retrieval_run
    from app.services.source_scope import source_scope_context

    retriever = ReasoningRetriever.from_repository(repo, settings, cancel_event)
    if retriever.reflect_v2_active() is not (item["policy"] == "v2"):
        raise RuntimeError(
            f"retriever 的协议与声明的 --policy={item['policy']!r} 不符:"
            f"reflect_v2_active()={retriever.reflect_v2_active()}"
        )
    question = (prepared or {}).get("research_question") or item["question"]
    seeds = list((prepared or {}).get("intent_queries") or [])
    scope = (
        {"mode": "include", "source_ids": list(scope_source_ids)}
        if scope_source_ids else None
    )
    with model_work_scope(
        priority=ModelPriority.INTERACTIVE, actor_id=actor_id,
        notebook_id=notebook, question=question,
    ):
        with retrieval_run(
            run_kind="ask_reasoning", event_log=None, actor_id=actor_id,
            cancel_event=cancel_event,
        ):
            with source_scope_context(notebook, scope, None):
                return retriever.run(
                    notebook, question, "", on_step=on_step,
                    intent_queries=seeds or None,
                    limits=ask_retrieval_limits(item["effort"]),
                    intent_detail=(prepared or {}).get("intent_detail"),
                )


def trace_step_row(step: Any) -> dict:
    """`TraceStep` → 投影能吃的 dict。与 `_generate_report` 的 sink 同形。

    `normalize_steps` 的解码点只认 Mapping 与 JSON 串,而 `TraceStep` 是一个
    数据类;`record()` 在调 `on_step` **之前**就回填了 `duration_ms`,所以这里
    拿到的耗时与落库那一份是同一个数。摘要(`summary`,一句人话)刻意不带。
    """
    return {
        "step_type": step.step_type,
        "detail": dict(getattr(step, "detail", None) or {}),
        "duration_ms": getattr(step, "duration_ms", None),
    }


def raw_trace_step_row(step: Any) -> dict:
    """`TraceStep` → **未收窄**的 dict,`--keep-raw-trace` 专用。

    与 `trace_step_row` 的唯一区别是带上 `summary`(那条人话摘要,里面可能有
    问题原文的只言片语与模型的自由文本)。只在这一条命令行开关打开时才收集,
    产物写进 `--out-dir` 下的 `raw/`,不进 `search-<policy>.jsonl`(见
    `SYNTHESIS_ONLY_KEYS`/§6 的隐私闭集与 `_raw_trace_path` 的说明)。
    """
    return {
        "step_type": step.step_type,
        "summary": step.summary,
        "detail": dict(getattr(step, "detail", None) or {}),
        "duration_ms": getattr(step, "duration_ms", None),
    }


def _raw_trace_path(out_dir: Path, policy: str, item: dict) -> Path:
    return (
        out_dir / "raw" / policy
        / f"{item['question_key']}_{item['corpus_cell']}_{item['effort']}.json"
    )


def raw_trace_payload(raw_steps: Sequence[dict], result: Any) -> dict:
    """`--keep-raw-trace` 一个 run 的落盘内容:原始步 + 结束事实(若有)。

    `result.termination` 只在 v2 下不是 `None`(`RetrievalTermination`,一个
    `frozen` dataclass,见 `app.domain.retrieval_termination`);legacy 恒
    `None`,这里如实写 `None`,不用 `infer_legacy_termination` 反推去填——这份
    文件是给人核对反推口径用的原始证据,自己先做一次反推就失去了核对的意义。
    """
    import dataclasses

    termination = getattr(result, "termination", None)
    return {
        "trace_steps": list(raw_steps),
        "termination": (
            dataclasses.asdict(termination) if termination is not None else None
        ),
    }


def write_raw_trace(
    out_dir: Path, policy: str, item: dict, raw_steps: Sequence[dict], result: Any,
) -> None:
    """把一个 run 的 `raw_trace_payload` 写到 `<out-dir>/raw/<policy>/…json`。

    **这份文件含标题与模型 reason,刻意不进数据集/不进仓库**(§6 的闭集约束
    只管 `search-<policy>.jsonl`,不管这里)——它的用途是人工核对反推口径,
    不是喂给 `analyze_reasoning_trace.py`。
    """
    path = _raw_trace_path(out_dir, policy, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw_trace_payload(raw_steps, result), ensure_ascii=False,
                   indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _search_preflight(
    args: argparse.Namespace, cells: Sequence[str], notebooks: dict[str, str],
) -> str:
    """真跑前的硬前提。返回空串 = 通过,否则是要打给用户的那句话。"""
    if not cells:
        return (f"--cell/--only-cell 选中的格子不在 search 的范围里(它只跑 "
                f"{', '.join(SEARCH_CELLS)};另外两格在主库上不存在)")
    if not args.database_url_explicit:
        return ("search 必须显式给 --database-url(主库连接;全程只读)。"
                "默认值指向的是 ask/seed 用的一次性测试库,不是主库")
    missing = [cell for cell in cells if not notebooks.get(cell)]
    if missing:
        flags = ", ".join(
            f"--source-notebook-{cell.split('_', 1)[0].lower()}" for cell in missing
        )
        return f"search 的 {', '.join(missing)} 需要 {flags}"
    return ""


def _resolve_concurrency(
    runner: Runner, database_url: str, settings: Any, requested: int,
) -> int:
    """真跑时的有效并发度。**只在这里做 clamp**,`--dry-run` 不算(它连
    `Settings()` 都不构造,零副作用的承诺不因为多打一行诊断而破例)。

    SQLite 主库强制 1:这条路的多线程只在 PG(`psycopg_pool.ConnectionPool`)
    上验证过安全,SQLite 分支只出现在冒烟/dry-run 场景,不为它另开一条并发
    正确性的举证责任。PG 按 `postgres_pool_max_size` clamp:一次并发拿的连接
    数超过池容量,后到的线程会在拿连接那一步死等到 `postgres_pool_acquire_
    timeout_seconds`,而不是报一个指向"调小 --concurrency"的好懂错误——clamp
    在这里做,永远不会死等。
    """
    requested = max(1, int(requested))
    if database_url.startswith("sqlite"):
        if requested > 1:
            runner.say(
                "concurrency clamp",
                f"SQLite 主库不支持这条路的真并发,{requested} -> 1",
            )
        return 1
    pool_max = int(
        getattr(settings, "postgres_pool_max_size", requested) or requested
    )
    if requested > pool_max:
        runner.say(
            "concurrency clamp",
            f"--concurrency={requested} 超过 POSTGRES_POOL_MAX_SIZE="
            f"{pool_max},clamp 到 {pool_max}(否则并发跑会在拿连接时死等)",
        )
        return pool_max
    return requested


def _search_repository(settings: Any) -> Any:
    """`search` 专用的仓储构造入口。**`migrate=False, seed=False`,不是
    `create_repository` 的默认值**(codex #700 R1 P1)。

    `search` 对主库整条路只读(模块 docstring「主库只读」),但
    `create_repository` 默认的 `migrate=True, seed=True` 会在 PostgreSQL 上
    无条件跑一次 `bundle._initialize`:先跑迁移,再用 `settings.admin_password`
    重写 admin 密码哈希——这是一次真实的、非幂等的写,已经在生产主库上真实
    发生过一次(`users` 表 admin 行的 `updated_at` 被改)。SQLite 侧
    `SqliteMigrator.seed()` 有同一形状的 `UPDATE users`,一并关掉。

    `create_repository` 而不是 `create_application_repository`:后者还会装
    插件宿主并 prime 一次扩展准入表。这条路只跑检索、而且对主库只读,接一层
    可能带写路径的扩展面没有收益。这个函数是**唯一**接线点,好让用例直接
    monkeypatch `create_repository` 断言这两个关键字,而不必真的跑一遍
    `_run_search` 那一整条 HTTP/并发路径。
    """
    from app.repositories.factory import create_repository

    return create_repository(settings, migrate=False, seed=False)


def _run_search(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    notebooks: dict[str, str], cells: Sequence[str],
) -> int:
    problem = _search_preflight(args, cells, notebooks)
    if problem:
        print(f"ERROR: {problem}", file=sys.stderr)
        return 2
    # 必须在 import `app.core.config` 之前:`_ENV_FILE` 与 Settings 的字段都是
    # import 期读的(与 `_apply_process_env` 同一条理由)。
    os.environ.update(_search_process_env(args))

    from app.core.request_context import reset_request_user, set_request_user
    from app.services.reasoning_retrieval import kg_in_scope_for

    settings_by_policy = _settings_by_policy()
    concurrency = _resolve_concurrency(
        runner, args.database_url, settings_by_policy["legacy"],
        args.concurrency,
    )
    repo = _search_repository(settings_by_policy["legacy"])
    profile = repo.maintenance.resolve_owner_profile(args.owner)
    if profile is None:
        print(f"ERROR: 主库里找不到属主: {args.owner or '<第一个 admin>'}",
              file=sys.stderr)
        return 2
    actor_id = str(getattr(profile, "id", "") or "")
    before = _readonly_counts(args.database_url)
    runner.say("readonly baseline", _format_counts(before))

    context = set_request_user(profile)
    try:
        facts = _search_corpus_facts(
            args, runner, repo, cells, notebooks, kg_in_scope_for,
        )
        failed = 0
        if concurrency == 1:
            # **不经过线程池**:`--concurrency 1` 的行为必须与并发功能落地前
            # 逐字一致,包括「所有调用都在主线程里发生」这件事本身——哪怕是
            # `ThreadPoolExecutor(max_workers=1)`也会把每次调用挪到另一个
            # 线程,单是这一点就足以让 ContextVar 的可见性行为变掉。
            failed = _search_loop(
                args, runner, plan, facts, repo, settings_by_policy,
                actor_id=actor_id, cancel_event=threading.Event(),
            )
        else:
            failed = _search_loop_concurrent(
                args, runner, plan, facts, repo, settings_by_policy,
                actor_id=actor_id, profile=profile, concurrency=concurrency,
            )
    finally:
        reset_request_user(context)
    _assert_readonly(
        runner, before, _readonly_counts(args.database_url), command="search",
    )
    if failed:
        # 失败 run 已各自落成 `status=failed` 行;整批仍以非零退出码收尾,不能让
        # 「每个 run 都失败」看起来像一次成功的跑批(codex #700 R8 P2)。
        print(f"ERROR: {failed} 个 run FAILED,见 search-runs.log", file=sys.stderr)
        return 1
    return 0


def _search_corpus_facts(
    args: argparse.Namespace, runner: Runner, repo: Any,
    cells: Sequence[str], notebooks: dict[str, str], kg_in_scope_for: Any,
) -> dict[str, dict]:
    """每个语料格的两件事实,**跑之前一次点清**。

    `kg_in_scope` 取 `kg_in_scope_for` 的直接判定(检索器自己用的同一个函数,
    一次 EXISTS),而不是事后从轨迹形状反推——投影里那个字段因此是事实而不是
    推断。`sources` 只为算 `notebook_bucket`(桶,不是计数:小样本里一个精确的
    来源数就是准 id)。
    """
    facts: dict[str, dict] = {}
    for cell in cells:
        notebook = notebooks[cell]
        repo.get_notebook(notebook)  # 不存在直接 KeyError,而不是跑出一批空 run
        facts[cell] = {
            "notebook": notebook,
            "sources": _source_count(args.database_url, notebook),
            "kg_in_scope": bool(kg_in_scope_for(repo.retrieval, notebook)),
        }
        runner.say(
            "corpus fact",
            f"{cell}: sources={facts[cell]['sources']} "
            f"kg_in_scope={facts[cell]['kg_in_scope']}",
        )
    return facts


def _scope_unresolved_row(item: dict, fact: dict) -> dict:
    """一道声明了 `scope_source_titles`、却解析不到唯一匹配的题:仍然产出
    **一行**,`status="failed"`、`scope_narrowed=None`(没跑,不知道)——不是
    悄悄跳过、也不是退化成一次不设限的全库检索(codex #700 R3 P2)。

    走的还是 `project_search_run` 那一份闭集投影(`steps=[]`、
    `result=None`):空轨迹让 `termination`/`reflect_turns` 等字段如实落成
    「零步」「unknown」,不是编造的假证据。这一行**不参与**
    `_search_loop`/`_search_loop_concurrent` 的「声明策略 vs 轨迹证据」核对
    ——它没跑,没有协议证据可对。
    """
    return _failed_run_row(item, fact)


def _failed_run_row(
    item: dict, fact: dict, *, steps: Sequence[dict] = (),
    has_intent_contract: bool = False,
) -> dict:
    """没跑成的 run 的那一行:`status="failed"`、`scope_narrowed=None`。

    `steps` 是失败前已经捕获到的轨迹步(codex #700 R13 P2):检索在发出若干
    reflect 步之后才抛异常时,丢掉它们会把 `reflect_turns`/`trace_steps`/
    `fallback_count` 记成 0、`has_intent_contract` 记成 false——都是编造的零,
    会让失败多的那一侧看起来"反思轮更少"。范围解析失败根本没跑,传空即可。

    范围解析失败与 worker 里抛异常(codex #700 R8 P2:并发分支此前只写一行日志
    就 `continue`,分析脚本只读 JSONL,失败 run 从状态分布里凭空消失,样本数
    偏向跑成的那一侧)共用这一行——投影是闭集,失败原因只进 `search-runs.log`。
    """
    row = project_search_run(
        list(steps), result=None, effort=item["effort"], policy=item["policy"],
        question_key=item["question_key"], corpus_cell=item["corpus_cell"],
        kg_in_scope=fact["kg_in_scope"], has_intent_contract=has_intent_contract,
        sources_count=fact["sources"],
    )
    row["status"] = "failed"
    row["scope_narrowed"] = None
    # 空轨迹没有协议证据,`project_search_run` 会把它推成 legacy——v2 的失败
    # run 就会被分析脚本记到 legacy 那一列(codex #700 R9 P2)。失败行按 rig
    # **声明**的策略归组(它本来就写在 `search-<policy>.jsonl` 里),不参与
    # 「声明 vs 证据」核对。
    row["policy_version"] = item["policy"]
    # 手改过 `project_search_run` 已经验过的行,再验一遍:调用方直接把这一行
    # 写进 JSONL,不会再经过一次共同的校验点。
    assert_closed(row)
    assert_projection_values(row)
    return row


def stamp_run_wall_ms(row: dict, elapsed_ms: int | None) -> dict:
    """把 rig 手上的 **run 墙钟**写进投影行的 `run_wall_ms`,就地返回同一行。

    这一列的产地只有一个,就是 rig(计划 §3 T-PS5;`project_run` 那一侧恒写
    `None` 并在注释里说明理由):轨迹里没有这件事实——`total_ms` 是各步耗时之
    和,漏掉排队、模型侧调度与步与步之间的空隙,而设计 §8.1 要的「检索 run ·
    `run_wall_ms`」正是含这些空隙的那个数。

    **这条「轨迹里没有」只对 `search` 成立**:那条路的 `project_search_run` 压根
    不收 latency。`ab` 那条路的 `latency_ms_total` 与这里的 `run_wall_ms` 是**同
    一个**变量(`_run_ab_arm` 里那个 `latency_ms`,一次 `time.monotonic()` 差),
    成功行上两列逐字相同,只在失败行分岔——`AB_FAILED_UNKNOWN_KEYS` 把
    `latency_ms_total` 清成 `None`,而墙钟照写。两列同源不是 bug,但读这张表的
    人别把它们当成两次独立的测量,改其中一处也要想到另一处。

    **失败的 run 也写**:它崩在半路,但它确实占了这么久的墙钟,而「哪一侧更容易
    崩、崩之前烧了多少时间」正是要量的东西之一。

    `None` 留给**压根没开跑**的行(范围解析失败:两臂各落一行 `status=failed`,
    却一次模型都没调)——给它写 0 会被读成「这个 run 零耗时跑完了」,那是一句
    关于这一行的假话,而且 0 会真的进 P50/P95 的分位数。

    写完当场重验一次形状:调用方拿到的这一行接着就被 `json.dumps` 进 JSONL,
    中间不会再经过一次共同的校验点(与 `_failed_run_row` 同一条纪律)。
    """
    row["run_wall_ms"] = elapsed_ms
    assert_projection_values(row)
    return row


def _resolve_item_scope(
    args: argparse.Namespace, item: dict, fact: dict,
) -> tuple[list[str] | None, bool]:
    """一道题的范围解析结果:`(source_ids或None, 解析失败?)`。

    没声明 `scope_source_titles` 时恒 `(None, False)`——`run_search_once` 的
    `scope_source_ids=None` 是 no-op,与改动前逐字一致。
    """
    titles = tuple(item.get("scope_source_titles") or ())
    if not titles:
        return None, False
    scope_ids = resolve_scope_source_ids(args.database_url, fact["notebook"], titles)
    return scope_ids, scope_ids is None


def _search_loop(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    facts: dict[str, dict], repo: Any, settings_by_policy: dict[str, Any],
    *, actor_id: str, cancel_event: Any,
) -> int:
    """整批 run。每个 run 一行 JSONL + 一行日志,**都不含任何问题原文**。"""
    from app.services.cancellation import AskCancelled

    intents = _IntentCache(
        runner.out_dir / "intents.jsonl", enabled=not args.no_intent
    )
    runner.out_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, Any] = {}
    verified: set[str] = set()
    failed = 0
    log = (runner.out_dir / "search-runs.log").open("a", encoding="utf-8")
    try:
        for index, item in enumerate(plan, 1):
            fact = facts[item["corpus_cell"]]
            policy = item["policy"]
            settings = settings_by_policy[policy]
            scope_ids, scope_failed = _resolve_item_scope(args, item, fact)
            if scope_failed:
                # 没开跑 ⇒ `run_wall_ms` 是 `None`,不是 0(见 `stamp_run_wall_ms`)。
                row = stamp_run_wall_ms(_scope_unresolved_row(item, fact), None)
                elapsed_ms = 0
                runner.say(
                    "search scope unresolved",
                    f"{item['question_key']} {item['corpus_cell']} "
                    f"titles={list(item.get('scope_source_titles') or ())}"
                    " -> status=failed reason=scope_unresolved",
                )
            else:
                prepared = _prepare_search_intent(
                    intents.get(repo, settings, item, actor_id=actor_id,
                                notebook=fact["notebook"]),
                    item["question"], item["effort"],
                )
                steps: list[dict] = []
                raw_steps: list[dict] = []

                def _on_step(step: Any) -> None:
                    # 闭包捕获的是这次迭代新建的 `steps`/`raw_steps`(每轮都
                    # 重新绑定,不是共享的循环变量),调用又发生在同一轮
                    # `run_search_once` 返回之前,不存在延迟绑定的坑。
                    steps.append(trace_step_row(step))
                    if args.keep_raw_trace:
                        raw_steps.append(raw_trace_step_row(step))

                started = time.monotonic()
                try:
                    result = run_search_once(
                        repo, settings, notebook=fact["notebook"], item=item,
                        prepared=prepared, on_step=_on_step,
                        cancel_event=cancel_event, actor_id=actor_id,
                        scope_source_ids=scope_ids,
                    )
                except AskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 — 单个 run 失败要隔离
                    # 与并发路径同一口径(codex #700 R12 P2):失败 run 落一行
                    # `status=failed`,日志只记异常类名,整批以非零退出码收尾;
                    # 不能让串行(SQLite 强制、默认并发 1)那条路的失败凭空消失。
                    elapsed_ms = round((time.monotonic() - started) * 1000)
                    row = stamp_run_wall_ms(_failed_run_row(
                        item, fact, steps=steps,
                        has_intent_contract=prepared is not None,
                    ), elapsed_ms)
                    runner.say(
                        "search failed",
                        f"{item['question_key']} {item['corpus_cell']} "
                        f"{policy} {type(exc).__name__}",
                    )
                    log.write(
                        f"{item['question_key']} {item['corpus_cell']} "
                        f"{policy} {item['effort']} FAILED {type(exc).__name__}\n"
                    )
                    log.flush()
                else:
                    elapsed_ms = round((time.monotonic() - started) * 1000)
                    row = project_search_run(
                        steps, result=result, effort=item["effort"], policy=policy,
                        question_key=item["question_key"],
                        corpus_cell=item["corpus_cell"],
                        kg_in_scope=fact["kg_in_scope"],
                        has_intent_contract=prepared is not None,
                        sources_count=fact["sources"],
                    )
                    row["scope_narrowed"] = bool(scope_ids)
                    stamp_run_wall_ms(row, elapsed_ms)
                    if args.keep_raw_trace:
                        write_raw_trace(runner.out_dir, policy, item, raw_steps, result)
            assert_closed(row)
            assert_projection_values(row)
            handle = handles.get(policy)
            if handle is None:
                handle = handles[policy] = (
                    runner.out_dir / f"search-{policy}.jsonl"
                ).open("a", encoding="utf-8")
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            status_suffix = "" if row.get("status") != "failed" else " status=failed"
            line = (
                f"{index:03d}/{len(plan)} {item['question_key']} "
                f"{item['corpus_cell']} {policy} {item['effort']} "
                f"{elapsed_ms}ms reflect_turns={row['reflect_turns']} "
                f"termination={row['termination_reason']}{status_suffix}"
            )
            log.write(line + "\n")
            log.flush()
            runner.say("search done", line)
            if row.get("status") == "failed":
                # 没跑,没有协议证据可对——不参与下面这道「声明策略 vs 轨迹
                # 证据」的核对,也不该被它当成第一个样本消费掉;但要计进失败数
                # (codex #700 R9 P2)。
                failed += 1
                continue
            if policy not in verified:
                # **每个策略的第一个 run 之后**就把「声明的策略」与「轨迹里真的
                # 发生了什么」对上一次。整批跑完再发现 v2 那半其实跑的是 legacy,
                # 代价是几百次模型调用;这里的代价是一次比较。
                if row["policy_version"] != policy:
                    raise RuntimeError(
                        f"声明 --policy={policy!r},但这个 run 的证据是 "
                        f"{row['policy_version']!r}(v2 的判据是 run 产出了 "
                        "termination 事实)。先修再跑整批"
                    )
                verified.add(policy)
                runner.say(
                    "first run ok",
                    f"policy={row['policy_version']} "
                    f"reflect_turns={row['reflect_turns']} "
                    f"termination={row['termination_reason']}",
                )
    finally:
        log.close()
        for handle in handles.values():
            handle.close()
    return failed


def _precompute_intents(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    facts: dict[str, dict], repo: Any, settings_by_policy: dict[str, Any],
    *, actor_id: str, profile: Any, concurrency: int,
) -> "_IntentCache":
    """并发路的第一阶段:把这批题**各算一次**意图契约,缓存写文件加锁。

    契约是 corpus-blind 且不分策略的(`_IntentCache` 的说明),固定用
    `settings_by_policy["legacy"]` 这一份去算——两个策略共用同一份意图契约本来
    就是这条路的前提(`search_plan` 的说明),这里不该让"契约用哪份 Settings"
    变成一个隐藏的、可能因字典迭代顺序而漂移的不确定量。

    去重在**提交前**做:`plan` 里每题最多出现 `len(POLICIES) × len(EFFORTS)`
    次,不去重会让同一题被排进多个 worker、靠 `_IntentCache` 内部的锁互相
    等——能跑对,但白白把并发度浪费在锁等待上。
    """
    intents = _IntentCache(
        runner.out_dir / "intents.jsonl", enabled=not args.no_intent,
    )
    if not intents.enabled:
        return intents
    unique_items: dict[str, dict] = {}
    for item in plan:
        unique_items.setdefault(item["question_key"], item)
    with intents._lock:
        pending = [
            item for key, item in unique_items.items() if key not in intents._rows
        ]
    if not pending:
        runner.say("precompute intents", f"0/{len(unique_items)}(全部缓存命中)")
        return intents
    runner.say(
        "precompute intents",
        f"{len(pending)}/{len(unique_items)} 题,concurrency={concurrency}",
    )
    settings = settings_by_policy["legacy"]

    def worker(item: dict) -> None:
        from app.core.request_context import reset_request_user, set_request_user

        ctx = set_request_user(profile)
        try:
            fact = facts[item["corpus_cell"]]
            intents.get(
                repo, settings, item, actor_id=actor_id, notebook=fact["notebook"],
            )
        finally:
            reset_request_user(ctx)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker, item) for item in pending]
        for future in as_completed(futures):
            # 意图算不出来是硬失败:先修再跑正式检索,不把一批坏契约悄悄地
            # 传给 `run_search_once`(它会拿一份 `contract=None` 继续跑,而
            # 那看起来完全像"这题没有意图契约"而不是"算它的时候炸了")。
            future.result()
    return intents


def _search_loop_concurrent(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    facts: dict[str, dict], repo: Any, settings_by_policy: dict[str, Any],
    *, actor_id: str, profile: Any, concurrency: int,
) -> int:
    """并发版整批 run(`concurrency > 1`)。

    两阶段:`_precompute_intents` 先把意图契约并发算完,再把全部 run(legacy /
    v2 **合并进同一个池**,总吞吐最高)提交给 `ThreadPoolExecutor`。

    ContextVar 与取消事件都是**每个 worker 自己的**:`model_work_scope` /
    `retrieval_run` / `source_scope_context` 已经在 `run_search_once` 内部按
    `with` 块设置/重置,天然是"每次调用一份"、不需要额外处理;但
    `set_request_user` 是在 `_run_search` 里对**整段**外层调用设的一次
    ContextVar——线程池的 worker 线程不继承调用方线程已经 `.set()` 过的那份
    context(这与 asyncio 任务默认 `copy_context()` 不同),所以这里在每个
    worker 内部重新 `set_request_user(profile)`,用完立即 reset。`cancel_event`
    同理:每个 run 自建一个 `threading.Event()`,不共享——共享一个事件对象会让
    "取消其中一个 run" 变成"取消全体在跑的 run"。

    输出用一把 `write_lock` 保护:`search-<policy>.jsonl` 与
    `search-runs.log` 的写入(含 flush)都在这把锁内做,完成顺序可以乱,但
    每一行仍是一份完整的投影,不会被另一个线程的写入截断或交错。

    第一个**完成**(不一定是第一个提交)的 run 校验一次"声明的策略与轨迹证据
    是否一致";不一致就 `abort()`——给所有仍在跑的 run 的 `cancel_event` 各
    `.set()` 一次,再 `executor.shutdown(cancel_futures=True)` 撤掉还没开始跑的
    任务。`AskCancelled` 与 `KeyboardInterrupt` 走同一条 `abort()` 路径。
    """
    from app.core.request_context import reset_request_user, set_request_user
    from app.services.cancellation import AskCancelled

    intents = _precompute_intents(
        args, runner, plan, facts, repo, settings_by_policy,
        actor_id=actor_id, profile=profile, concurrency=concurrency,
    )
    runner.out_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, Any] = {}
    write_lock = threading.Lock()
    verified: set[str] = set()
    active_events: list[threading.Event] = []
    active_lock = threading.Lock()
    aborted = threading.Event()
    log = (runner.out_dir / "search-runs.log").open("a", encoding="utf-8")
    total = len(plan)
    completed = 0
    started_at = time.monotonic()

    def abort(reason: str) -> None:
        # `aborted.set()` 与快照 `active_events` 在同一把锁里(codex #700 R21
        # P2):否则一个刚过了入口 `aborted` 检查、还在解析范围的 worker 会在快照
        # 之后才登记事件,带着一个永远不会被 set 的 cancel_event 开始调模型,
        # 而 `shutdown(wait=True)` 只能等它跑完。
        with active_lock:
            if aborted.is_set():
                return
            aborted.set()
            events = list(active_events)
        for event in events:
            event.set()
        runner.say("search aborted", reason)

    def worker(item: dict) -> tuple[dict, float, list[dict] | None, Any] | None:
        if aborted.is_set():
            # 已经决定收摊:还没真的开始跑的任务(`executor.shutdown(
            # cancel_futures=True)` 撤不掉已经被 worker 线程取出来的这一个)
            # 直接退出,不再发起新的模型调用。
            return None
        fact = facts[item["corpus_cell"]]
        # 范围解析只是一次只读 DB 查询,不碰模型、不需要 `cancel_event`/
        # `set_request_user`——放在那两样之前做,解析失败时干脆不进入下面那段
        # 需要清理的临界区。
        scope_ids, scope_failed = _resolve_item_scope(args, item, fact)
        if scope_failed:
            # 没开跑 ⇒ `run_wall_ms` 是 `None`,不是 0(见 `stamp_run_wall_ms`)。
            row = stamp_run_wall_ms(_scope_unresolved_row(item, fact), None)
            return row, 0.0, None, None
        cancel_event = threading.Event()
        with active_lock:
            # 与 `abort()` 在同一把锁下复核:收摊已经开始就不再登记、不再开跑
            # (codex #700 R21 P2)。
            if aborted.is_set():
                return None
            active_events.append(cancel_event)
        ctx = set_request_user(profile)
        try:
            policy = item["policy"]
            settings = settings_by_policy[policy]
            prepared = _prepare_search_intent(
                intents.get(repo, settings, item, actor_id=actor_id,
                            notebook=fact["notebook"]),
                item["question"], item["effort"],
            )
            steps: list[dict] = []
            raw_steps: list[dict] = []

            def _on_step(step: Any) -> None:
                steps.append(trace_step_row(step))
                if args.keep_raw_trace:
                    raw_steps.append(raw_trace_step_row(step))

            started = time.monotonic()
            try:
                result = run_search_once(
                    repo, settings, notebook=fact["notebook"], item=item,
                    prepared=prepared, on_step=_on_step,
                    cancel_event=cancel_event, actor_id=actor_id,
                    scope_source_ids=scope_ids,
                )
            except AskCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 — 单个 run 失败要隔离
                # 在 worker 里就把失败投影出来,失败前捕获到的轨迹步不丢
                # (codex #700 R13 P2);异常对象放在 `result` 位上带回主循环记
                # 日志(只记类名)。主循环那条 `except` 只剩 run 之外的失败。
                elapsed_ms = round((time.monotonic() - started) * 1000)
                row = stamp_run_wall_ms(_failed_run_row(
                    item, fact, steps=steps,
                    has_intent_contract=prepared is not None,
                ), elapsed_ms)
                return row, elapsed_ms, None, exc
            elapsed_ms = round((time.monotonic() - started) * 1000)
            row = project_search_run(
                steps, result=result, effort=item["effort"], policy=policy,
                question_key=item["question_key"],
                corpus_cell=item["corpus_cell"],
                kg_in_scope=fact["kg_in_scope"],
                has_intent_contract=prepared is not None,
                sources_count=fact["sources"],
            )
            row["scope_narrowed"] = bool(scope_ids)
            stamp_run_wall_ms(row, elapsed_ms)
            assert_closed(row)
            assert_projection_values(row)
            # `raw_steps`/`result` 只在 `--keep-raw-trace` 时真的被消费(写盘发生
            # 在 `write_lock` 内,见下方 `as_completed` 循环);不开这个开关就不带
            # 出去,免得线程池里堆着一堆没人要用的原始轨迹。
            return row, elapsed_ms, (raw_steps if args.keep_raw_trace else None), result
        finally:
            reset_request_user(ctx)
            with active_lock:
                if cancel_event in active_events:
                    active_events.remove(cancel_event)

    def _jsonl_handle(policy: str) -> Any:
        # 只在 `write_lock` 内调用。
        handle = handles.get(policy)
        if handle is None:
            handle = handles[policy] = (
                runner.out_dir / f"search-{policy}.jsonl"
            ).open("a", encoding="utf-8")
        return handle

    pool = ThreadPoolExecutor(max_workers=concurrency)
    fatal: BaseException | None = None
    failed = 0
    try:
        futures = {pool.submit(worker, item): item for item in plan}
        try:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    outcome = future.result()
                except AskCancelled as exc:
                    abort(f"AskCancelled: {item['question_key']}")
                    fatal = exc
                    break
                except Exception as exc:  # noqa: BLE001 — 单个 run 失败要隔离,
                    # 不带原文:只记异常类名与题号,问题原文一个字都不进日志。
                    # 但 JSONL 里必须有它那一行(`status=failed`),否则分析脚本
                    # 看不见失败;整批退出码也要非零(codex #700 R8 P2)。
                    failed += 1
                    with write_lock:
                        log.write(
                            f"{item['question_key']} {item['corpus_cell']} "
                            f"{item['policy']} {item['effort']} FAILED "
                            f"{type(exc).__name__}\n"
                        )
                        log.flush()
                        handle = _jsonl_handle(item["policy"])
                        handle.write(json.dumps(
                            # run **之外**失败(worker 都没进到计时那一段),没有
                            # 墙钟可报 ⇒ unknown,不是 0(见 `stamp_run_wall_ms`)。
                            stamp_run_wall_ms(
                                _failed_run_row(item, facts[item["corpus_cell"]]),
                                None,
                            ),
                            ensure_ascii=False, sort_keys=True,
                        ) + "\n")
                        handle.flush()
                    runner.say(
                        "search failed",
                        f"{item['question_key']} {item['corpus_cell']} "
                        f"{item['policy']} {type(exc).__name__}",
                    )
                    continue
                if outcome is None:
                    continue
                row, elapsed_ms, raw_steps, result = outcome
                policy = item["policy"]
                with write_lock:
                    handle = _jsonl_handle(policy)
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                    handle.flush()
                    if raw_steps is not None:
                        # 每条路径唯一(题号+语料格+档位各一份),锁在这里只为
                        # 与其余输出共用同一个「不交错」的写纪律,不是为了防真的
                        # 路径冲突。
                        write_raw_trace(
                            runner.out_dir, policy, item, raw_steps, result,
                        )
                    completed += 1
                    if row.get("status") == "failed":
                        # 范围解析不到位、或 run 内部抛异常的 run 都以正常
                        # outcome 回来,但它没跑成——计进失败数,整批不能退出码 0
                        # (codex #700 R9 P2)。
                        failed += 1
                    done_n = completed
                    reason = (
                        type(result).__name__ if isinstance(result, BaseException)
                        else "scope_unresolved"
                    )
                    status_suffix = (
                        "" if row.get("status") != "failed"
                        else f" status=failed reason={reason}"
                    )
                    line = (
                        f"done {done_n}/{total}  {item['question_key']} "
                        f"{item['corpus_cell']} {policy} {item['effort']} "
                        f"reflect={row['reflect_turns']} "
                        f"term={row['termination_reason']} "
                        f"{elapsed_ms / 1000:.1f}s{status_suffix}"
                    )
                    log.write(line + "\n")
                    log.flush()
                    # `status=failed`(范围解析不到位、没真的跑)不参与「声明
                    # 策略 vs 轨迹证据」核对,也不消费这个策略的「第一个样本」
                    # 名额——留给下一个真的跑了的 run 去验。
                    first_for_policy = (
                        row.get("status") != "failed" and policy not in verified
                    )
                    if first_for_policy:
                        verified.add(policy)
                runner.say("search done", line)
                if first_for_policy:
                    # **第一个完成的 run**,不一定是第一个提交的:并发下完成
                    # 顺序由调度决定,校验按到达顺序做,而不是按 `plan` 里的
                    # 下标——按下标等,会在下标 0 的那个 run 恰好跑得慢时白白
                    # 拖住已经能验的另一侧。
                    if row["policy_version"] != policy:
                        message = (
                            f"声明 --policy={policy!r},但第一个完成的 run 证据"
                            f"是 {row['policy_version']!r}(v2 的判据是 run 产出"
                            "了 termination 事实)。先修再跑整批"
                        )
                        abort(message)
                        fatal = RuntimeError(message)
                        break
                    runner.say(
                        "first run ok(第一个完成的 run)",
                        f"policy={row['policy_version']} "
                        f"reflect_turns={row['reflect_turns']} "
                        f"termination={row['termination_reason']}",
                    )
        except KeyboardInterrupt as exc:
            abort("KeyboardInterrupt")
            fatal = exc
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        log.close()
        for handle in handles.values():
            handle.close()
    elapsed_total = time.monotonic() - started_at
    runner.say(
        "search totals",
        f"{completed}/{total} run(s) 完成,{failed} 个 FAILED,"
        f"并发度={concurrency},总耗时={elapsed_total:.1f}s",
    )
    if fatal is not None:
        raise fatal
    return failed


# ===========================================================================
# `ab`:进程内跑**完整 Ask**,两臂背靠背配对
# (A/B 设计规格 `docs/superpowers/specs/2026-09-09-reflect-ab-design_zh.md`)
# ===========================================================================

#: `ab` 的主矩阵语料格(§4.1)。`B_nokg` 是 P1 探针,要跑它得显式 `--cell B_nokg`
#: ——它不进主表,单出一节。四个格子都必须已经由 `seed` 建在测试库里。
AB_DEFAULT_CELLS: tuple[str, ...] = ("A_nokg", "B_kg")
#: `ab` **不给 `--arms` 时**的两条臂。与二维化之前逐字等价(`legacy` / `v2` 各
#: 一条,optimization 都是 `off`),所以既有命令与既有产物路径一个字节没变。
#:
#: 臂是 rig 的**声明**,一条 run 的身份是 `(policy, optimization)` 这一对;两维
#: 各自的证据来源不同——`policy_version` 从轨迹反推,optimization 只有运行时
#: 直接读数(见 `reflect_ab.assert_optimization_matches_evidence`)。合法组合的
#: 闭集在 `reflect_ab.ARMS`,这里只是默认值。
AB_DEFAULT_ARMS: tuple[tuple[str, str], ...] = (("legacy", "off"), ("v2", "off"))


def ab_arms(args: argparse.Namespace) -> list[tuple[str, str]]:
    """这一批真的要跑的臂。`--dry-run` 与真跑读的是**同一份**解析。

    三条口径:

    * **不给** `--arms` ⇒ `AB_DEFAULT_ARMS`(一维两臂,与二维化之前逐字相同)。
      判据是 `is None`(argparse 的默认值)而不是真值:`--arms ""` 是**给了一个
      空写法**,得走下面那条响亮拒绝,不能悄悄退化成默认两臂——脚本里写
      `--arms "$ARMS"` 而变量恰好为空时,整批上百次调用会安静地跑成
      legacy-vs-v2,而那不是要做的实验;
    * 给了 `--arms` ⇒ `reflect_ab.parse_arms` 的结果,非法写法当场抛
      `ArmSpecError`(空写法、空段、未知取值、`legacy:prefix_snapshot` 这类合法
      值的非法组合、重复臂);
    * `--only-policy` 仍然按 policy 过滤**默认**臂(既有语义,用于一侧被网络故障
      整批打废后的重跑)。它与 `--arms` 同时给是**矛盾指令**,由 `_ab_preflight`
      挡在跑批之前:`--arms v2:off,v2:prefix_snapshot --only-policy v2` 里的
      「只跑 v2」既可以读成「两条都留」也可以读成「留一条」,而两种读法产出的
      数据集不一样,paired 也不一样。
    """
    if getattr(args, "arms", None) is not None:
        return parse_arms(args.arms)
    if args.only_policy:
        return [
            arm for arm in AB_DEFAULT_ARMS if arm[0] in set(args.only_policy)
        ]
    return list(AB_DEFAULT_ARMS)
#: 测试库名的强制后缀(§5.4「必填,不读 .env」+ §5.5-2「主库零接触」)。`ab` 会
#: 往库里写 conversation + answer 行,所以拿错连接的代价不是一次读脏数据,而是
#: 往用户的活库里写。一个后缀挡不住所有误用,但它挡住了最常见的那一种:把
#: `search` 用惯的主库 URL 直接抄过来。
AB_TEST_DB_SUFFIX = "_test"
#: `seed` 给每个语料格建的笔记本名(见 `_seed_notebooks`)。`ab` 按它在测试库里
#: 反查 notebook id,而不是读 `--out-dir` 下的 `rig-state.json`:两条命令的
#: `--out-dir` 通常不是同一个(§5.4 的产物落在 `.local/ab`),而库里那个名字是
#: `seed` 自己写下的、与 out-dir 无关的事实。
AB_NOTEBOOK_NAME = "t0-{cell}"


def ab_cells(selected: Sequence[str]) -> list[str]:
    """`--cell` 选中的格。**不给就是主矩阵两格**,不是「四格全跑」。

    与 `corpus_cells`(seed 的口径:不给 = 全跑)刻意不同:`ab` 的第三、第四格
    (`A_kg` / `B_nokg`)一个是刻意不做的(§11:A 是单篇,KG 增益小),一个是
    单出一节的 P1 探针(§4.3)。把它们卷进默认值,等于让每一次 `ab` 都默默多
    跑一倍预算。
    """
    if not selected:
        return list(AB_DEFAULT_CELLS)
    return [cell for cell in CORPUS_CELLS if cell in selected]


def ab_plan(
    questions: dict, *, cells: Sequence[str], efforts: Sequence[str],
    repeats: int, round_: int | None, lang: str, limit: int | None,
    only_questions: Sequence[str] = (),
) -> list[dict]:
    """`ab` 要跑的全部**配对单元**,一条一行。跑批顺序就是这份列表的顺序。

    顺序是 `repeat → effort → question`(§4.2),两条臂在**每个单元内部**背靠背
    跑、臂序按 run 随机(见 `_run_ab_unit`)。两条约束各自换来一件事:

    * **重复轮在最外层** ⇒ 任何被预算或故障截断的前缀都是一份配对完整、平衡的
      数据集(第 1 轮跑完就有一份能出结论的 n=1 数据);
    * **两臂在最内层背靠背** ⇒ provider 的排队与限流漂移对两臂等量,臂序本身
      不成为系统偏差。这正是选进程内路径(而不是 HTTP)的直接原因(§5.1)。

    题面枚举复用 `ask_plan`——`ab` 与 `search`/`ask` 出的行进的是同一张对照表,
    各写一份枚举就等于给「哪些题在哪些格上跑」立两个权威。`policy` 那一维在这里
    被丢掉:臂是单元内部的事,不是枚举的一维。
    """
    rounds = [round_] if round_ is not None else list(range(1, repeats + 1))
    units: list[dict] = []
    for repeat in rounds:
        for effort in efforts:
            for item in ask_plan(
                # `policy` 这一维在 `ab` 里被丢掉(下面就 `pop`),给哪一个都
                # 一样;写死 `POLICIES[0]` 而不是「臂列表的第一条」,是因为
                # 这份枚举与这一批跑几条臂无关。
                questions, cells=cells, policy=POLICIES[0], limit=limit,
                lang=lang, mode="reasoning", efforts=(effort,),
                only_questions=only_questions,
            ):
                item.pop("client_request_id", None)
                item.pop("policy", None)
                item["repeat"] = repeat
                units.append(item)
    return units


#: 一次**逻辑调用**最多真正发出去几个请求(设计 §8.1「真实模型调用次数」)。
#: `app/core/llm.py` 的重试循环上界是 `1 + max_retries`,reflect 那条路的
#: `max_retries` 来自 `REASONING_MAX_RETRIES`(`config.py` 默认 1)。
#:
#: 这个乘数**只是上界的一半**:静默 fallback(`response_format` 被拒后的明文
#: 重试、`stream_options` 被拒后的重建流)各自会多发一次请求,却不留任何日志行,
#: 而它发不发取决于对面的端点,配置里查不出来。所以 rig 打的那一行明说这是
#: 「重试预算内的上界」——真实请求数由 llm.jsonl 行上的 `attempts` 事后给出,
#: 不由这里预测。改了 `REASONING_MAX_RETRIES` 要同 diff 改这里。
REASONING_ATTEMPT_BUDGET = 1 + 1

#: 与 `Settings.reasoning_timeout_seconds` 的**默认值**同步(`config.py`
#: `REASONING_TIMEOUT_SECONDS`,默认 90)。`ab` 的 dry-run 不构造 `Settings`
#: (零副作用承诺,理由见 `_resolve_concurrency`),T-EX8 的「单次超时」那一行
#: 因此打这个镜像常量,不打真实配置;真跑时 manifest 的 `budgets.
#: reasoning_timeout_seconds` 改读 `settings_by_arm[reference_arm]` 的真实值。
#: 改了 config.py 那个默认值要同 diff 改这里——`REASONING_ATTEMPT_BUDGET` 有一条
#: 钉住配置默认值的守卫,这个常量按同一条纪律也有一条(见
#: `test_reflect_t0_scripts.py` 的
#: `test_the_single_call_timeout_mirror_tracks_the_config_default`):不钉的失败
#: 场景是配置默认调到 120 之后 rig 与用例**一致地**停在陈旧的 90,操作者按 90s
#: 估整批墙钟预算、把 `--max-wall-minutes` 定小了(评审 F3 / P3-1)。
REASONING_TIMEOUT_SECONDS_DEFAULT = 90


def _ab_call_estimate(
    units: Sequence[dict], question_count: int, *, no_intent: bool,
    arms: Sequence[tuple[str, str]] = AB_DEFAULT_ARMS,
) -> str:
    """每个单元的模型调用估计。**是上界,不是预测**(措辞沿用 T0
    `_search_call_estimate`,理由也一样:reflect 的轮数是模型自己决定的,只有
    档位的硬上限是确定的)。

    与 `search` 的两点不同,都是「跑的是完整 Ask」带来的:

    * 每个单元有**两个** run(两条臂),所以检索侧的每一项都乘臂数;
    * 每个 run 在检索之后还有一次**合成**调用(`search` 整条路不合成)。合成
      的重试与按节合成不计——那是下界之上的浮动,而这是一条上界。

    末尾另报一条**请求上界**(计划 §3 T-PS5:dry-run 打印调用数与请求上界)。
    两个数不是一个:上面那个数的是**逻辑调用**(llm.jsonl 的行数、`model_calls`
    的口径),请求上界数的是**真正发出去的请求**(行上 `attempts` 的口径)。混
    用会把一次静默 fallback 记成一次额外的推理,反过来则会低报端点负载
    (见 `reflect_ab.slice_llm_usage` 的同一条说明)。

    意图契约仍是每题一次并缓存,两臂共用同一份冻结契约(§5.4)——这不是省钱的
    小聪明,而是 A/B 成立的前提:契约不同,必答方面就不同,两臂根本不在比同一
    件事。
    """
    runs = len(units) * len(arms)
    intent_calls = 0 if no_intent else question_count
    plan_calls = runs if no_intent else 0
    reflect_ceiling = sum(
        MAX_REFLECT_STEPS.get(unit["effort"], 0) for unit in units
    ) * len(arms)
    total = intent_calls + plan_calls + reflect_ceiling + runs
    return (
        f"intent {intent_calls} + plan {plan_calls} + reflect ≤ "
        f"{reflect_ceiling} + synthesis {runs}  ⇒  ≤ {total} 次逻辑调用;"
        f"请求上界 ≤ {total * REASONING_ATTEMPT_BUDGET}"
        f"(重试预算 ×{REASONING_ATTEMPT_BUDGET};静默 fallback 另计,不留日志行)"
    )


def _wall_budget_problem(max_wall_minutes: float | None) -> str:
    """`--max-wall-minutes` 的取值判据。空串 = 通过,否则是要打给用户的那句话。

    **`ab` 与 `prefix-probe` 共用这一处**(评审 F8 / P2-3):那两条命令读的是
    同一个全局参数,判据分成两份的失败场景是「`ab` 拒了、`prefix-probe` 照跑」
    ——而后者产出的坏数据集看起来和一次正常的跑批一模一样。

    与 `--round` 同一条纪律(不 clamp、越界响亮报错),两种坏值各自的后果都是
    「一份看起来正常的计划产出一份不能用的数据集」(评审 F10 / P3-2):

    * `<= 0` ⇒ deadline 一开始就在过去:走完全部 preflight(含主库快照、gold
      解析)之后零派发、写一份 `stopped_by_budget=true` 的 manifest、退 1——读
      的人分不清是参数打错还是预算真用完;
    * `nan` ⇒ 所有 `>= deadline` 比较**恒假**,预算从此永不生效(既不早停也不
      报错),同时 `budgets` 里落一个 `NaN`,写出的 `manifest.json` 不再是合法
      JSON(`reflect_manifest` 模块 docstring 已登记这条与兄弟模块共担的限制)。

    `budget != budget` 是 NaN 的判据(不 import math:这个模块的 dry-run 路径
    刻意只用标准库里已经在用的那几个名字)。`inf` 不拦——它的语义「不设上限」
    与不给这个参数一致,而且 `>= deadline` 恒假不会产出坏数据。
    """
    if max_wall_minutes is None:
        return ""
    budget = float(max_wall_minutes)
    if budget != budget or budget <= 0:
        return (f"--max-wall-minutes 必须是一个正数分钟数,拿到 {budget}。"
                "不 clamp:0/负数会让整批走完全部 preflight 之后零派发、"
                "留下一份空数据集 + stopped_by_budget=true 的 manifest;"
                "nan 会让预算判据恒假(永不早停)且 manifest.json 不再是"
                "合法 JSON。不想设上限就干脆不给这个参数")
    return ""


def _ab_preflight(args: argparse.Namespace, cells: Sequence[str]) -> str:
    """真跑前的硬前提。返回空串 = 通过,否则是要打给用户的那句话。

    每一条都在 `--dry-run` 下也拦(枚举为空的那条尤其):一份「零个 run」的
    计划看起来完全像一次正常的预演。
    """
    if not cells:
        return (f"--cell 选中的格子不在 ab 的范围里(可选 "
                f"{', '.join(CORPUS_CELLS)};默认 {', '.join(AB_DEFAULT_CELLS)})")
    if args.arms is not None and args.only_policy:
        # 矛盾指令,不猜(见 `ab_arms`):`--arms v2:off,v2:prefix_snapshot
        # --only-policy v2` 的「只跑 v2」既可以读成「两条都留」也可以读成「留
        # 一条」,两种读法产出的数据集与 `paired` 都不一样。
        return ("--arms 与 --only-policy 不能同时给:前者已经把这一批的臂点全了,"
                "后者是一维时代按 policy 过滤默认两臂的写法。只跑一条臂就直接写 "
                "`--arms v2:prefix_snapshot`")
    if args.arms is not None:
        # `is not None`(不是真值):`--arms ""` 是给了一个空写法,由 `parse_arms`
        # 的第 1 类响亮拒绝,不能悄悄跑成默认两臂(见 `ab_arms`)。
        try:
            parsed = parse_arms(args.arms)
        except ArmSpecError as exc:
            return str(exc)
        if len(parsed) > PAIR_ARM_COUNT:
            # 一次一对。三条臂的批次里每个配对单元落三行,`mark_paired` 的门槛
            # 是 `PAIR_ARM_COUNT`,于是整批 `paired` 全 False、配对差值表凭空
            # 空掉——而每一行看起来完全正常,行数、投影、日志都对。这是「各自
            # 正确的一半拼出错误整体」的那种失败,只能拦在跑批之前。
            return (
                f"--arms 一次只收一对臂(给了 {len(parsed)} 条:"
                + ", ".join(format_arm(*arm) for arm in parsed)
                + ")。配对差值表按**对**出:三条臂的单元里 `paired` 会全线 False,"
                  "整批数据看起来正常却出不了任何配对结论。分两批跑,每批一对"
            )
    if not ab_arms(args):
        # `--only-policy` 过滤之后零臂。**今天在 CLI 上到不了**:那个参数的
        # `choices` 就是 `POLICIES`,两个值都在 `AB_DEFAULT_ARMS` 里,过滤永远
        # 非空;与 `--arms` 同时给已被上一条拦掉。留着是防两个集合日后分叉
        # (往 `POLICIES` 加了第三种协议、却没往 `AB_DEFAULT_ARMS` 加对应臂),
        # 因为零臂的计划长得完全像一次正常的预演——连 dry-run 都看不出来。
        return (f"--only-policy {args.only_policy} 过滤之后一条臂都不剩(默认臂 "
                + ", ".join(format_arm(*arm) for arm in AB_DEFAULT_ARMS) + ")")
    if not args.database_url_explicit:
        return ("ab 必须显式给 --database-url(§5.4:必填,不读 .env)。"
                "它是 `seed` 建出来的一次性测试库——ab 会往里写 conversation "
                "与 answer 行(§5.2)")
    if not str(args.database_url).rstrip("/").endswith(AB_TEST_DB_SUFFIX):
        return (f"ab 的 --database-url 必须指向名字以 {AB_TEST_DB_SUFFIX!r} 结尾"
                "的一次性测试库(§5.5-2 主库零接触)")
    if not args.source_db_url:
        return ("ab 需要 --source-db-url(**主库**连接,全程只读):§5.5-2 的"
                "「主库零接触」是一条硬断言,跑前跑后各点一次主库的 "
                f"{'/'.join(READONLY_PROOF_TABLES)} 行数。没有它这条断言只能记"
                "「未验证」,而未验证不等于通过")
    if (str(args.source_db_url).rstrip("/")
            == str(args.database_url).rstrip("/")):
        # 两个 URL 同指一处时,「主库」快照量的是 ab 自己正在写的那个库:跑完
        # 必然对不上,而对不上会被读成「主库被污染了」。更坏的是反过来——同一个
        # 库既当测试库又当主库,这条断言从此什么都证明不了(codex 质量评审 P1-1)。
        return ("ab 的 --source-db-url(主库,只读)不能与 --database-url"
                "(一次性测试库,会被写)是同一个连接:§5.5-2 的快照要量的是"
                "**另一个**库")
    if args.no_intent:
        # 两臂共用同一份冻结契约是 A/B 成立的前提(§5.4/§5.5-3)。`--no-intent`
        # 下 `_IntentCache` 恒返回 `None`,§5.5-3 那条同一性断言退化成
        # `digest(None) == digest(None)` 恒真,而两条臂各自现算的检索方向可以
        # 完全不同——那时比的已经不是同一件事(codex 质量评审 P2-8)。
        return ("ab 不接受 --no-intent:两臂必须引用同一条冻结契约(§5.5-3),"
                "而 --no-intent 下根本没有契约可比,§5.5-3 的断言会退化成恒真。"
                "省那一次/题的意图调用换不来一份能出结论的数据")
    if args.round is not None and not (1 <= args.round <= args.repeats):
        # `--round` 是一个下标,不 clamp:越界该报错。clamp 成边界值会让
        # `--round 4 --repeats 3` 安静地重跑第 3 轮,把两轮数据混进一格。
        return (f"--round 必须落在 1..{args.repeats} 之间(--repeats="
                f"{args.repeats}),拿到 {args.round}")
    return _wall_budget_problem(args.max_wall_minutes)


def cmd_ab(args: argparse.Namespace, runner: Runner) -> int:
    """两条臂 × 两个档位 × N 轮重复,**进程内跑完整 Ask**(§5)。

    与 `search` 的分工:`search` 是「对主库只读跑 plan + reflect 循环,不合成
    答案」;`ab` 是「在 `seed` 的一次性测试库上跑意图契约 + 检索 + 合成 + 引用
    绑定」。search-only 量不出答案质量,而 A/B 要补的正是那一半(§0-5)。

    与 `ask`(HTTP)的分工:`ask` 换策略必须重启后端,两条臂只能整批分开、相隔
    数小时,provider 的漂移会整块落在臂的差上;`ab` 在同一个进程里为两个策略各
    构造一份 `Settings` **和一个 repo**,同题同档背靠背跑(§5.1)。两个 repo 而
    不是两份 Settings 的理由见 `run_ab_once`。
    """
    questions = load_questions()
    cells = ab_cells(args.cell)
    # **dry-run 也拦**:一份跑在错库上、或者枚举为空的计划,预演出来看起来完全
    # 像一次正常的预演(见 `_ab_preflight`)。
    problem = _ab_preflight(args, cells)
    if problem:
        print("ERROR: " + problem, file=sys.stderr)
        return 2
    efforts = tuple(args.efforts) if args.efforts else EFFORTS
    units = ab_plan(
        questions, cells=cells, efforts=efforts, repeats=args.repeats,
        round_=args.round, lang=args.lang, limit=args.limit,
        only_questions=args.only_question,
    )
    unique_questions = {
        (unit["corpus_cell"], unit["question_key"]) for unit in units
    }
    runner.say("target", "一次性测试库:意图契约 + 检索 + 合成 + 引用绑定")
    runner.say("database-url", f"<given>(测试库,以 {AB_TEST_DB_SUFFIX} 结尾)")
    runner.say("main database (read-only probe)", "<given>")
    for cell in cells:
        runner.say("corpus cell", f"{cell} -> notebook name="
                                  + AB_NOTEBOOK_NAME.format(cell=cell))
    arms = ab_arms(args)
    runner.say("arms", ", ".join(format_arm(*arm) for arm in arms)
               + "(同进程各一个 repo;每单元内背靠背,臂序按 run 随机)")
    runner.say(
        "measurement",
        "REASONING_REFLECT_MEASURE_CONTEXT=true(**两臂都开**,拍板 Q2:测量开关"
        "与 optimization 正交,对照要的正是同一把尺子);每条臂另按声明设 "
        "REASONING_REFLECT_OPTIMIZATION",
    )
    runner.say("efforts", ", ".join(efforts))
    rounds = 1 if args.round is not None else args.repeats
    runner.say("repeats", f"{args.repeats}"
               + (f",只跑第 {args.round} 轮" if args.round is not None else ""))
    runner.say("planned runs",
               f"{len(units) * len(arms)}({len(units)} 个配对单元 × "
               f"{len(arms)} 臂;{len(unique_questions)} 题 × "
               f"{len(efforts)} 档 × {rounds} 轮)")
    runner.say("model calls (estimate)",
               _ab_call_estimate(units, len(unique_questions),
                                 no_intent=args.no_intent, arms=arms))
    # T-EX8 要点 3:三行新增文案(整批墙钟预算 / 单次超时 / 到点行为),
    # dry-run 与真跑共用这段前缀,与上面几行同一条打印纪律。
    runner.say(
        "wall-clock budget",
        (
            f"--max-wall-minutes {args.max_wall_minutes}"
            if args.max_wall_minutes is not None
            else "未设置(--max-wall-minutes 不给,今天的行为逐字相同:"
                 "不设上限、不早停)"
        ),
    )
    runner.say(
        "single-call timeout",
        f"{REASONING_TIMEOUT_SECONDS_DEFAULT}s"
        "(REASONING_TIMEOUT_SECONDS 的默认值;dry-run 不构造 Settings,"
        "真跑按各臂 settings_by_arm 的实际值,见 manifest 的 budgets)",
    )
    runner.say(
        "on budget",
        "到点会停止派发新单元、cancel_event 唤醒在途、"
        "shutdown(cancel_futures=True) 撤掉队列里未派发的;"
        "在途未完成单元落 status=cancelled 行,保留未完成/不成对标记,"
        "不偷偷补跑到矩阵齐全",
    )
    runner.say("out", f"{runner.out_dir}/ab-runs.jsonl + ab-runs.log + "
                      + ", ".join(f"calls-{arm_label(*arm)}.jsonl"
                                  for arm in arms)
                      + "(per-call 表:llm.jsonl ⋈ events.jsonl on support_id)")
    runner.say(
        "isolated logs",
        f"LLM_LOG_PATH={runner.out_dir}/llm/llm.jsonl、"
        f"EVENT_LOG_DIR={runner.out_dir}/events"
        "——rig 不往机器共用的 .local/logs 写一个字节(计划 §3 T-PS5 G5)",
    )
    runner.say(
        "local only",
        f"{runner.out_dir}/raw/<arm>/<key>_<cell>_<effort>_r<n>.json"
        "(答案正文/引用/锚点/契约/原始 TraceStep)+ "
        f"{runner.out_dir}/intents.jsonl(含问题原文)"
        "——**两者都不进数据集/不进仓库**(§7.3)",
    )
    if runner.dry_run:
        # 真跑时这一行改在 clamp **之后**打(见 `_run_ab`):`--concurrency 2`
        # 被连接池 clamp 回 1 时,clamp 之前那句「成本三键将是 unknown」是一句
        # 假话——那一批的成本三键其实照常记了值(codex 规格评审 P3)。dry-run
        # 不构造 Settings、也就没有 clamp,所以这里打的是「请求值」。
        runner.say("concurrency (requested)",
                   _ab_concurrency_note(args.concurrency)
                   + "。--dry-run 不做连接池 clamp,真跑时按池容量再收一次")
        for index, unit in enumerate(units, 1):
            titles = unit.get("scope_source_titles") or ()
            scope_note = f" scope={len(titles)} sources" if titles else ""
            runner.say(
                "ab unit",
                f"{index:03d} {unit['question_key']} "
                f"cell={unit['corpus_cell']} effort={unit['effort']} "
                f"r{unit['repeat']} "
                f"arms={'/'.join(format_arm(*arm) for arm in arms)}(序随机)"
                f"{scope_note}",
            )
        return 0
    return _run_ab(args, runner, units, cells)


def _ab_concurrency_note(concurrency: int) -> str:
    """`--concurrency` 那一行日志。>1 时必须当场说清成本三键会变成 unknown。

    理由不是保守(§4.4):token 归因靠 LLM 日志的**时间窗切片**(§7.2),并发下
    窗口重叠,切片会把别的 run 的 token 记到本 run 上。那种数比没有更坏,所以
    rig 强制把 `prompt_tokens` / `completion_tokens` / `model_calls` 三个键写成
    unknown,只保留自己掐的 `latency_ms_total`。
    """
    if concurrency <= 1:
        return "1(默认;成本三键按 LLM 日志的时间窗切片归因)"
    return (
        f"{concurrency}(并发单元数;每个单元内部两臂仍背靠背)。"
        "⚠ 成本三键(prompt_tokens / completion_tokens / model_calls)将强制写成 "
        "unknown:窗口重叠,时间窗切片会把别的 run 的 token 记到本 run 上"
    )


def _ab_process_env(args: argparse.Namespace) -> dict[str, str]:
    """`ab` 的那一份。`DATABASE_URL` 指向**测试库**(与 `search` 相反)。

    其余每一项都与 `_search_process_env` 逐字同源(`_rig_process_env`)——
    包括把 LLM 交互日志与事件日志圈进本次 out-dir 的那两条,成本三键与 per-call
    表的归因正确性直接建在它们上面。

    `ab` 独有的一项是**测量开关恒开**(拍板 Q2):
    `REASONING_REFLECT_MEASURE_CONTEXT` 与 `optimization` 正交,`off` 臂也能出
    `message_prefix_bytes`——对照实验要的正是两条臂用**同一把尺子**量。只给
    `prefix_snapshot` 那一臂开,`off` 臂的那几列会整列缺失,差值表于是只剩一侧
    有数,而每一行看起来都很正常。
    每条臂各自的 `REASONING_REFLECT_OPTIMIZATION` **不在这里**:它按臂不同,由
    `_settings_by_arm` 在构造每一份 `Settings` 时设(进程环境只能有一个值)。
    """
    env = _rig_process_env(args, database_url=args.database_url)
    env["REASONING_REFLECT_MEASURE_CONTEXT"] = "true"
    return env


def _ab_notebook_ids(database_url: str, cells: Sequence[str]) -> dict[str, str]:
    """测试库里每个语料格的 notebook id,按 `seed` 写下的名字反查。

    每个名字必须**恰好**命中一个笔记本。命中 0 个 = 这个格还没 seed;命中 ≥2 个
    = 同一个测试库被 seed 过两次,两份语料混在一起——两种都必须响亮失败,不能
    随便挑一个跑出一批不知道跑在哪份语料上的数据。
    """
    from export_reasoning_traces import _Reader

    out: dict[str, str] = {}
    with _Reader(database_url) as reader:
        for cell in cells:
            name = AB_NOTEBOOK_NAME.format(cell=cell)
            rows = reader.query(
                "SELECT id FROM notebooks WHERE name = ?", (name,)
            )
            if len(rows) != 1:
                raise RuntimeError(
                    f"测试库里名为 {name!r} 的笔记本有 {len(rows)} 个(必须恰好 "
                    "1 个)。先跑 `seed`,或换一个干净的测试库"
                )
            out[cell] = str(rows[0]["id"])
    return out


def _ab_source_rows(database_url: str, notebook_id: str) -> list[dict]:
    """一个笔记本的 `(id, title, updated_at)` 全表,只读。四处消费:

    1. `resolve_gold_sources` —— B 格 gold 短名的唯一解析(§5.5-6);
    2. `citations_out_of_scope` 的允许集合(题目没声明范围时 = 全库);
    3. `anchors_on_gold` 的 B 格第三跳(来源 → 标题)。
    4. `_ab_corpus_signature` —— 语料指纹的 `(id, title, updated_at)` 那一段
       (codex #703 R1 P2)。

    标题/`updated_at` **只留在进程内**:它们进不了投影行(闭集里没有这两个
    键),只在 `.local` 的 raw 存档里出现(§7.3);进指纹的只是它们的短哈希。
    """
    from export_reasoning_traces import _Reader

    with _Reader(database_url) as reader:
        rows = reader.query(
            "SELECT id, title, updated_at FROM sources WHERE notebook_id = ?",
            (notebook_id,),
        )
    return [
        {
            "id": str(row["id"]), "title": str(row["title"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    ]


def _ab_unique_ids(ids: Sequence[str]) -> list[str]:
    """一批 id 去重去空后的排序列表。**空 id 先滤掉,再判空集**。

    `["", "", ""]` 是一个非空列表,但它的唯一 id 集合是空的——直接拿它拼
    `IN ()`,在 SQLite 上返回 `[]`(于是每个锚点都「查不到」⇒ 整键 unknown),
    在 PostgreSQL 上则是一句语法错、被外层的大 `except` 吞成同一个 unknown
    (codex 规格评审 P2-1)。两条都错,而且错得静悄悄。
    """
    return sorted({str(value).strip() for value in ids if str(value or "").strip()})


def _ab_chunk_sections(
    database_url: str, chunk_ids: Sequence[str],
) -> dict[str, str] | None:
    """`chunk_id` → `chunks.section_path`。`anchors_on_gold` 的 chunk 那一跳。

    与 `_ab_element_sections` 不同,`chunks.section_path` 是一列**真实的列**
    (`0001_initial.sql:113`),不需要在 Python 侧解 metadata。

    这一跳只在锚点自己没带 `location_label` 时才走得到(`reflect_ab
    ._anchor_section` 的第三条路):chunk 锚点的 `location_label` 恒等于
    `chunk.section_path`,所以正常情况下这个函数一次也不会被查到东西——它兜的
    是「证据上下文那一侧哪天不再把 section_path 写进锚点」。查库失败 ⇒ `None`
    = unknown,与元素那一跳同口径。
    """
    from export_reasoning_traces import _Reader

    unique = _ab_unique_ids(chunk_ids)
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    try:
        with _Reader(database_url) as reader:
            rows = reader.query(
                f"SELECT id, section_path FROM chunks WHERE id IN ({placeholders})",
                tuple(unique),
            )
    except Exception:  # noqa: BLE001 — 查不出来就是 unknown,不猜「零命中」
        return None
    return {str(row["id"]): str(row["section_path"] or "") for row in rows}


def _ab_element_sections(
    database_url: str, element_ids: Sequence[str],
) -> dict[str, str] | None:
    """`element_id` → `metadata.section_path`。`anchors_on_gold` 的 A 格第二跳。

    `source_elements` 表**没有** `section_path` 列(2026-09-09 复核
    `0001_initial.sql:609`:只有 id / source_id / element_type / location_label
    / text / metadata / created_at / ordinal),那个值住在 `metadata` 这一列的
    jsonb 里——两侧仓储都按 `metadata.get("section_path")` 读它
    (`postgres/catalog_store.py:445`、`sqlite/catalog_store.py:539`),所以这里
    也在 Python 侧解 metadata,而不是写一句只在 PG 上成立的 `->>` SQL。

    查库本身失败 ⇒ 返回 `None` = unknown(调用方据此把 `anchors_on_gold` 整键
    落 `None`),不是「一个都没解析到」。查得到但某个 id 不在结果里,是**另一
    件事**:那是一次真实的解析失败,由 `count_anchors_on_gold` 判成 unknown。
    """
    from export_reasoning_traces import _Reader

    unique = _ab_unique_ids(element_ids)
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    try:
        with _Reader(database_url) as reader:
            rows = reader.query(
                f"SELECT id, metadata FROM source_elements WHERE id IN ({placeholders})",
                tuple(unique),
            )
    except Exception:  # noqa: BLE001 — 查不出来就是 unknown,不猜「零命中」
        return None
    out: dict[str, str] = {}
    for row in rows:
        raw = row["metadata"]
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        meta = raw if isinstance(raw, dict) else {}
        out[str(row["id"])] = str(meta.get("section_path") or "")
    return out


def _ab_model_contract(settings: Any) -> tuple[str | None, str]:
    """`(短码, 人读串)`。短码进投影行,人读串只进 `ab-runs.log`。

    §3.3 要求复用 `ModelSamplingContract` 的**形状**(provider / model /
    prompt_version / temperature / top_p / seed,另加 `thinking_mode`);这里
    刻意只把它**压成一个短码**写进数据集,理由是投影的值形状闭集
    (`assert_projection_values`):字典的值半只到数值这一层,一个字段是字符串的
    dataclass 塞不进去,而为它放宽那道闸就等于给整份数据集开一个自由文本口子。
    短码是 `merge_key` 的单向哈希——「跨批次混用当场可见」这件事它办得到(不同
    配置 ⇒ 不同短码),而「看得懂是哪个模型」由日志那一份负责。

    取值来自 `SystemModelServiceRegistry.load(settings)`,不连库、不发请求:
    服务定义自带 `fingerprint`(配置指纹),两个反思相关的 workload
    (`reasoning_agent` 出反思决策,`ask_answer` 出最终答案)各取一份。

    读不出来(没有部署 TOML、或它的形状变了)⇒ 短码是 `None` = unknown,整批
    照跑:这一列是「跨批次混用当场可见」的辅助信号,不是判据;为它把一次几小时
    的批跑挡在门外,换来的只是少一个提示。真正拦门的是 §5.5 那六条。
    """
    from app.services.model_registry import SystemModelServiceRegistry

    try:
        registry = SystemModelServiceRegistry.load(settings)
    except Exception as exc:  # noqa: BLE001 — 读不出配置就是 unknown,不是硬失败
        return None, f"<unavailable: {type(exc).__name__}>"
    parts: list[str] = []
    readable: list[str] = []
    for workload_id in ("reasoning_agent", "ask_answer"):
        service = registry.service_for(workload_id)
        thinking = registry.thinking_mode_for(workload_id)
        model = getattr(service, "model", "") if service is not None else ""
        top_p = getattr(service, "top_p", None) if service is not None else None
        fingerprint = (
            getattr(service, "fingerprint", "") if service is not None else ""
        )
        parts.extend([workload_id, model, fingerprint, str(top_p), str(thinking)])
        readable.append(
            f"{workload_id}={model or '<unbound>'} top_p={top_p} "
            f"thinking={thinking}"
        )
    return merge_key(*parts), "; ".join(readable)


def _ab_corpus_signature(
    cell: str, fact: Mapping, database_url: str,
) -> str:
    """语料/索引指纹(§7.1)。同样是短码:跨 `seed` 混用时当场可见。

    单靠语料格、来源条数、图在不在范围内(旧实现)测不出「同一个格换了一篇
    论文」或「同一批来源重新分块/重新抽取元素/重建了 KG」——这三件事都不改
    来源**条数**,签名却必须跟着变(codex #703 R1 P2)。进指纹的因此还有:

    * 每个来源的 `(id, title, updated_at)` 按 `id` 排序后有序拼接——换一篇
      论文(标题变)、重新解析同一份文件(`updated_at` 变)都会让这一段变;
    * `chunks` / `source_elements` 的行数——重新分块/重新抽取元素会改行数,
      即使来源条数、标题、`updated_at` 都不变;
    * `unified_kg_state.object_count`——KG 对象数,是这张状态表上现成的计数
      列,不用现场数 `knowledge_objects` 的行。这个笔记本还没建过 KG(表里没
      有那一行)时记 `None` = unknown,不是 `0`——「还没建过」与「建过但空」是
      两件不同的事,不能用同一个数字表示。

    `notebook id` 本身**不**进指纹——它是一个数据库 id,投影行里一个 id 都不
    许有(§7.3);它对指纹的贡献已经被上面这几项(尤其是来源的 `id`,只作为
    指纹的哈希输入,不是原样落进投影行)覆盖。查询失败(测试库连不上/表不存
    在)不兜底——这是跑批前一次性的语料事实收集(`_ab_corpus_facts`),与
    `_ab_source_rows` 同一批查询同一条口径:查不出来就是配置错误,响亮失败,
    不能悄悄退化成一份看起来正常、其实没量到东西的签名。
    """
    from export_reasoning_traces import _Reader

    notebook_id = fact["notebook"]
    source_key = "|".join(
        f"{row['id']}:{row['title']}:{row.get('updated_at', '')}"
        for row in sorted(fact["source_rows"], key=lambda row: row["id"])
    )
    with _Reader(database_url) as reader:
        chunk_rows = reader.query(
            "SELECT COUNT(*) AS n FROM chunks WHERE notebook_id = ?",
            (notebook_id,),
        )
        element_rows = reader.query(
            "SELECT COUNT(*) AS n FROM source_elements se "
            "JOIN sources s ON se.source_id = s.id WHERE s.notebook_id = ?",
            (notebook_id,),
        )
        kg_rows = reader.query(
            "SELECT object_count FROM unified_kg_state WHERE notebook_id = ?",
            (notebook_id,),
        )
    chunk_count = int(chunk_rows[0]["n"]) if chunk_rows else 0
    element_count = int(element_rows[0]["n"]) if element_rows else 0
    kg_object_count = int(kg_rows[0]["object_count"]) if kg_rows else None
    return merge_key(
        cell, fact.get("sources"), fact.get("kg_in_scope"),
        source_key, chunk_count, element_count, kg_object_count,
    )


def _ab_llm_log_dir(settings: Any) -> Path:
    """LLM 交互日志的目录(`app.core.llm_logging` 的落点)。

    `LLMInteractionLogger` 把 `llm_log_path` 的**目录**当 base,按 owner 分子
    目录、按天分文件(`llm-YYYY-MM-DD.jsonl`,见 `EventLogger._target_path_for_day`)。
    rig 因此不认文件名,只认这个目录,读的时候按 `**/llm-*.jsonl` 铺开。
    """
    path = Path(getattr(settings, "llm_log_path", ".local/logs/llm.jsonl"))
    if not path.is_absolute():
        path = ROOT / path
    return path.parent


#: LLM 交互日志的文件名形状(`EventLogger._target_path_for_day` 按天分文件,
#: 按 owner 分子目录,所以只能按 glob 认)。
AB_LLM_LOG_GLOB = "**/llm-*.jsonl"

#: 事件日志的文件名形状。同一条 `EventLogger` 规则,只是 channel 叫 `events`
#: (`repository_runtime` 里 `EventLogger(settings, channel="events",
#: per_user=True)`)。两串日志落在两个并列目录里(见 `rig_event_log_dir`),
#: 所以两个 glob 互不相交,不需要在同一个目录里靠前缀区分。
RIG_EVENT_LOG_GLOB = "**/events-*.jsonl"


def _ab_llm_offsets(
    log_dir: Path, *, glob: str = AB_LLM_LOG_GLOB,
) -> dict[str, int]:
    """跑一个 run **之前**,把日志目录里每个文件当下的字节长度记一份。

    `glob` 让**事件日志**复用同一份实现(`RIG_EVENT_LOG_GLOB`):两串日志的分文件
    规则出自同一个 `EventLogger`,各写一遍偏移逻辑只会分叉。

    没有这一份 offset,每个 run 结束都要把当天整份日志重读一遍并 json.loads
    每一行:一批 408 个 run 于是把同一批行读了 408 遍(O(N²)),内存尖峰还是
    整天日志的大小(codex 质量评审 P2-6)。有了它,一个 run 只读它自己那一段。

    读不到目录(第一个 run 之前它还不存在)⇒ 空字典 = 「每个文件都从头读」,
    与旧行为一致。
    """
    offsets: dict[str, int] = {}
    try:
        paths = sorted(log_dir.glob(glob))
    except OSError:
        return offsets
    for path in paths:
        try:
            offsets[str(path)] = path.stat().st_size
        except OSError:  # 刚被轮转掉的文件:当它不存在,从头读
            continue
    return offsets


def _ab_read_llm_records(
    log_dir: Path, offsets: Mapping[str, int], *, glob: str = AB_LLM_LOG_GLOB,
) -> list[dict]:
    """只读 `offsets` → EOF 的**增量**,解析成记录列表。

    `glob` 见 `_ab_llm_offsets`:事件日志走同一条读法。

    按字节读(`"rb"` + `seek`)而不是文本模式:文本模式的 `seek` 只接受
    `tell()` 发的不透明 cookie,拿一个字节偏移去 seek 是未定义行为。解码放在
    读完之后做,`errors="replace"` 与旧实现一致。

    **只取数值字段的那几行原样返回**,正文的裁剪交给 `slice_llm_usage`(它只读
    `ts` / `usage` / `finish_reason`,prompt / response 片段一个字都不碰,§7.2)。

    日志目录本身还不存在(第一个 run 之前它还没被建出来)⇒ 空列表,与
    `_ab_llm_offsets` 同一条口径,这是正常的第一次状态,不算失败。但目录**存在**
    之后,任何一个被 glob 到的文件打不开/读不出来(权限、磁盘错误)⇒ `OSError`
    原样往上抛,不吞掉——调用方(`_ab_usage_for_window`)据此把这个 run 的成本
    三键落成 unknown,而不是「这个文件此刻没有新增行」那种沉默的 0(codex #703
    R1 P2)。
    """
    records: list[dict] = []
    try:
        paths = sorted(log_dir.glob(glob))
    except OSError:
        return records
    for path in paths:
        start = int(offsets.get(str(path), 0) or 0)
        with path.open("rb") as handle:
            handle.seek(start)
            blob = handle.read()
        for line in blob.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                records.append(row)
    return records


def _ab_usage_for_window(
    log_dir: Path, started: "datetime", ended: "datetime", *, concurrency: int,
    offsets: Mapping[str, int] | None = None, llm_log_enabled: bool = True,
) -> Any:
    """一个 run 的成本三键。`concurrency > 1` 时**恒 unknown**(§4.4/§7.2)。

    `offsets` 是这个 run 开跑之前 `_ab_llm_offsets` 记下的那一份;时间窗切片
    仍然照做——offset 只负责「不重读别的 run 已经数过的行」,归因的判据还是
    `[started, ended]` 这个闭区间。

    `llm_log_enabled=False`(`settings.llm_log_enabled` 关着)⇒ 恒 unknown,连
    读都不读:Ask 在这个开关关着时照样调模型,只是不写交互日志,这时候把空
    列表喂给 `slice_llm_usage` 会投出一整批看起来精确、实则全错的
    `model_calls=0`(codex #703 R1 P2)。
    """
    from app.eval.reflect_ab import UNKNOWN_USAGE, slice_llm_usage

    if concurrency > 1 or not llm_log_enabled:
        return UNKNOWN_USAGE
    try:
        records = _ab_read_llm_records(log_dir, offsets or {})
    except OSError:
        # 文件打不开/读不出来就是 unknown,不当成「这个 run 一次模型都没调」。
        return UNKNOWN_USAGE
    return slice_llm_usage(records, start=started, end=ended)


def _write_manifest(manifest: Mapping, *, runner: Runner) -> None:
    """`manifest.json`(最新,覆盖)+ `manifests.jsonl`(历史,追加)——**三条
    通道共用这一个函数**(pr5-followups.md「T-EX8 quality 评审 → 拍板」P3-7:
    T-EX4/T-EX7 照做,T-EX11 回填 Q9;T-EX4 评审 P3-18:E3 那一份内联实现改调
    这里,不留第二份)。

    两份都要的理由:同一个 out-dir 里 `ab-runs.jsonl` / `probe-*.jsonl` 本来就
    是 append 模式、默认 out-dir 又是固定的 `.local/t0`。只覆盖写的话,在 SHA
    a1 跑一批、改代码到 b2 再跑一批之后,数据文件里两批的行都在,manifest 只剩
    b2 ⇒ a1 那些行被归到 b2 的代码上,`code_sha` 这个锚点反过来说了假话。

    `manifest.json` 走 `Runner.write` 而不是自己 `write_text`:那一层自带
    `dry_run` 短路、`mkdir(parents=True)` 与一行 `write` 日志(评审 P3-6)。
    追加那一份没有 `dry_run` 短路——两条调用方都只在非 dry-run 路径上到这里
    (那条短路是死分支,评审 P3-17),`mkdir` 保证 `Runner.write` 之后目录一定
    在,不靠调用顺序。
    """
    payload = dict(manifest)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    runner.write(runner.out_dir / "manifest.json", text)
    history_path = runner.out_dir / "manifests.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    runner.say("manifest", f"{runner.out_dir}/manifest.json(最新)+ "
                           f"{runner.out_dir}/manifests.jsonl(历史,追加)")


# --- per-call 表(`calls-<arm>.jsonl`;计划 §3 T-PS5) ------------------------

#: 并发下 per-call 行落在这个标签下。臂那一维在并发跑批里**不成立**:run→call
#: 的归因靠时间窗,窗口重叠;`support_id` 只把日志行与调度事件对上号,它不知道
#: 这次调用属于哪个 run。所以那一批的 run 级标签(含 `arm`)一律 unknown,文件
#: 名跟着叫 unknown——不是把它们塞进某条臂的文件里,那才叫猜。
CALLS_UNATTRIBUTED_LABEL = "unknown"


def _rig_call_rows(
    log_dir: Path, event_dir: Path, *,
    llm_offsets: Mapping[str, int], event_offsets: Mapping[str, int],
    tags: Mapping | None,
) -> list[dict]:
    """一段窗口里的 per-call 行。**薄适配**:找文件、读增量、递给纯函数。

    归因逻辑一行都不在这里——它在 `app.eval.reflect_context_bench.join_calls`
    (纯函数,fixture 可钉;设计 §11「CLI 保持薄适配」)。这里只做三件 I/O:
    按 glob 铺开两个目录、按偏移读增量、把两串记录递进去。

    读不出来(目录还不存在、文件权限、磁盘错误)⇒ **空列表**,不抛:per-call
    表是诊断产物,读不到它不该让一个跑成了的 run 整体作废(与 `_ab_usage_for_window`
    把 `OSError` 折成 unknown 同一条口径,只是这里 unknown 的形态是「这一段没有
    行」而不是「有行但值是 None」)。

    `ValueError` 一并折掉,是**第二层**而不是第一层:`join_calls` 里那几列已经
    自己把不合形状的 provider 串收成 unknown(见 `reflect_context_bench._short_code`)
    ——第一层保住的是「这一行的别的列照样是真的」,这一层保住的只是「整批不会
    在第 N 个单元中止」。只留这一层会让一整段调用连着表一起消失(串行路这个
    表达式在 `_run_ab_arm` 的 `return` 里求值,并发路它在 `_ab_loop` 的
    `finally` 里,抛出去还会顶掉在途异常),那不是降级,那是丢账。
    """
    from app.eval.reflect_context_bench import join_calls

    try:
        llm_records = _ab_read_llm_records(log_dir, llm_offsets)
        events = _ab_read_llm_records(
            event_dir, event_offsets, glob=RIG_EVENT_LOG_GLOB,
        )
        return join_calls(llm_records, events, tags=tags)
    except (OSError, ValueError):
        return []


def _rig_call_tags(
    unit: Mapping, arm: tuple[str, str], *, attributed: bool,
) -> dict | None:
    """per-call 行的 run 级标签。`attributed=False` ⇒ `None` = 一列都不写。

    并发下这几列必须是 unknown 而不是某个看起来合理的臂名(见
    `CALLS_UNATTRIBUTED_LABEL`)。传 `None` 而不是传一份全 `None` 的字典,是为了
    让「不归因」这件事在调用点上看得见。
    """
    if not attributed:
        return None
    return {
        "arm": arm[0],
        "optimization": arm[1],
        "question_key": unit["question_key"],
        "corpus_cell": unit["corpus_cell"],
        "effort": unit["effort"],
        "repeat": int(unit["repeat"]),
    }


def _write_call_rows(out_dir: Path, label: str, rows: Sequence[dict]) -> None:
    """把 per-call 行追加进 `calls-<label>.jsonl`。零行就不建文件。

    调用方负责串行化(`ab` 那条路在 `state["lock"]` 里调):同一条臂的这个文件
    在并发下会被多个单元写,一行被另一个线程截断就再也解析不回来。
    """
    if not rows:
        return
    path = out_dir / f"calls-{label}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True)
                         + "\n")


def ab_intent_confirmation(contract: Mapping, question: str) -> Any:
    """冻结契约 → `AskRequest.intent`。**同一份契约两臂共用**(§5.5-3)。

    这一步不是简单地把契约塞进请求:`AskService._confirmed_reasoning_intent`
    会对提交上来的契约**再跑一次** `finalize_query_intent`,而 T0 的
    `_IntentCache` 存的已经是 finalize **之后**的那一份(`ambiguities` 被清空、
    `clarification_answers` 已填)。直接提交会让那批代答在第二次 finalize 里
    被清掉(`submitted` 只认还在 `ambiguities` 里的 id),于是:

    * 契约的 `clarification_answers` 变空 ⇒ `confirmed_research_question` 少了
      「用户确认」那几条补充,检索问题与 T0 的 `search` 那条路不再逐字相同;
    * `auto_confirmed_clear_intent` 反而变真 ⇒ 权威从「确认后的方向」翻回
      「用户原文」,与 `_prepare_search_intent` 的判据相反。

    所以这里把 `ambiguities` 按 `clarification_answers` 原样**还原**再提交:
    id 与 question 都是当初记下来的原值,不是编的;第二次 finalize 于是拿到与
    第一次逐字相同的输入,输出也逐字相同(`finalize_query_intent` 在这条路上
    是幂等的)。没有代答的题走 else 分支,两侧同样逐字不变。
    """
    from app.models.ask import (
        AskIntentConfirmation, QueryIntentAmbiguity, QueryIntentContract,
    )

    payload = dict(contract)
    answers = [
        row for row in (payload.get("clarification_answers") or [])
        if isinstance(row, Mapping) and str(row.get("id") or "").strip()
    ]
    payload["ambiguities"] = [
        QueryIntentAmbiguity(
            id=str(row["id"]),
            question=str(row.get("question") or row["id"]),
            required=True,
        ).model_dump()
        for row in answers
    ]
    return AskIntentConfirmation(
        contract=QueryIntentContract(**payload),
        resolved_question=str(payload.get("resolved_question") or question),
        answers=[
            {"id": str(row["id"]), "answer": str(row.get("answer") or "")}
            for row in answers
            if str(row.get("answer") or "").strip()
        ],
    )


def assert_knowhow_reflect_v2_off(repo: Any, settings: Any) -> None:
    """§5.5-5:Knowhow 路径的 `reflect_v2_active()` 必须为假,**即使 v2 开着**。

    Knowhow 不设臂(§4.3):它构造检索器时把 `allow_reflect_v2` 关掉,两条臂对
    它逐字相同。这里按 `app/services/knowhow/api.py` 的那一行原样复现一次构造
    并当场断言——断言失败就整批停,因为那意味着「Knowhow 不受这次开闸影响」这
    个前提在这份代码上已经不成立了,而整批 A/B 的结论都建在它上面。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever

    retriever = ReasoningRetriever.from_repository(repo, settings, None)
    retriever.allow_reflect_v2 = False
    if retriever.reflect_v2_active():
        raise RuntimeError(
            "Knowhow 断言失败:allow_reflect_v2=False 的检索器仍然报告 "
            "reflect_v2_active()=True。开闸会连带改到 Knowhow,先修再跑"
        )


def run_ab_once(
    repo: Any, *, notebook: str, item: dict, arm: str,
    contract: dict | None, on_trace: Any, cancel_event: Any, actor_id: str,
    scope_source_ids: Sequence[str] | None = None,
    optimization: str = "off",
) -> Any:
    """一个 run:三层 scope + `repo.ask_reasoning`。生产接线的逐字复刻。

    三层 scope 与 `run_search_once` 同形、同理由(模型调度归属 / 请求级检索预算
    / 来源范围)。两处不同,都是「这次要跑完整 Ask」带来的:

    * 调的是 `repo.ask_reasoning(...)`,不是 `ReasoningRetriever.run`。它 =
      `_prepare_reasoning_ask` + `_run_reasoning_stage(...).response`,所以意图
      契约、gap 补取、结构化预览、合成与引用绑定全在里面——rig 侧不重造任何一
      段(§5.3:重造就变成「评测 rig 自己的合成」)。
    * `job_id` 走默认空串 ⇒ `_prepare_turn` 的 legacy create-or-continue 分支:
      不建 durable job、不写 `ask_trace_steps`。轨迹经 `on_trace` 在进程内拿,
      所以也不需要 `export` 那一步(§5.2)。

    **合成会落库**(conversation + answer),落在一次性测试库里,这是接受的、
    不是绕过:语料是公开论文,题面公开,答案是模型对公开论文的作答。`.local`
    的隔离是仓库卫生,不是保密(§5.2 / §7.3)。

    **臂由 `repo` 本身承载,不由一个额外的 settings 参数承载。** 这一点与
    `run_search_once` 相反,而且是这条路唯一容易写错、写错了还照样跑得出数据的
    地方:`search` 直调 `ReasoningRetriever.from_repository(repo, settings)`,
    检索器读的是**传进去**的那一份;而 Ask 走
    `AskService._build_reasoning_retriever`,它写死 `settings=self.settings`
    ——即构造这个 repo 时用的那一份(已核实 `repo.settings is
    ask_component.settings`)。给 `ask_reasoning` 递一份别的 Settings 没有任何
    入口,所以两条臂必须是**两个 repo**(见 `_run_ab` 的 `repos_by_arm`)。用
    一个 repo 加一个 settings 参数,两条臂会双双跑 legacy,而产出的数据从形状
    上完全看不出这件事。

    开跑前当场核对 `retriever.reflect_v2_active()` 与这条 run 声明的臂一致
    (§5.5-1)。核对用的检索器是另外构造的一份,但它读的正是 `repo.settings`
    ——与 Ask 内部那一个逐字同源。这一步是「协议对不对号」的唯一事前证据;
    事后还有一次,由投影出来的 `policy_version` 对(见
    `assert_arm_matches_evidence`)。

    **臂的第二维(`optimization`)只有这一次核对**,没有事后那一半:两条 v2 臂的
    轨迹形状逐字相同,产物里反推不出它——差别全在发给 provider 的消息怎么分块
    (见 `reflect_ab.assert_optimization_matches_evidence`)。所以这里读的
    `probe.reflect_optimization()` 是**全仓唯一读点**的直接读数,而不是从
    settings 字段自己再判一次:v2 总闸关、Knowhow 否决、已实现闭集之外的取值
    折回 `off` 这三条降级路径都只在那个方法里,绕过它等于把降级本身漏掉。
    """
    from app.eval.reflect_ab import assert_optimization_matches_evidence
    from app.models.ask import AskRequest
    from app.models.source_scope import SourceScope
    from app.services.model_work import ModelPriority, model_work_scope
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval_run import retrieval_run
    from app.services.source_scope import source_scope_context

    probe = ReasoningRetriever.from_repository(repo, repo.settings, cancel_event)
    if probe.reflect_v2_active() is not (arm == "v2"):
        raise RuntimeError(
            f"repo 的协议与声明的 arm={arm!r} 不符:"
            f"reflect_v2_active()={probe.reflect_v2_active()}"
            "(臂由 repo 承载——Ask 读的是构造这个 repo 的那一份 Settings)"
        )
    assert_optimization_matches_evidence(
        optimization, probe.reflect_optimization())
    scope = (
        SourceScope(
            mode="include", source_ids=list(scope_source_ids), narrowed=True,
        )
        if scope_source_ids else None
    )
    payload = AskRequest(
        question=item["question"], mode="reasoning",
        retrieval_effort=item["effort"], source_scope=scope,
        intent=(
            ab_intent_confirmation(contract, item["question"])
            if contract is not None else None
        ),
    )
    with model_work_scope(
        priority=ModelPriority.INTERACTIVE, actor_id=actor_id,
        notebook_id=notebook, question=item["question"],
    ):
        with retrieval_run(
            run_kind="ask_reasoning", event_log=None, actor_id=actor_id,
            cancel_event=cancel_event,
        ):
            with source_scope_context(notebook, scope, None):
                return repo.ask_reasoning(
                    notebook, payload, on_trace, cancel_event,
                )


def ab_raw_payload(item: dict, arm: str, response: Any, raw_steps: Sequence[dict],
                   contract: dict | None) -> dict:
    """`.local` 的一份 run 存档:答案正文、引用、锚点、契约、原始 TraceStep。

    **只在 `.local`,不进数据集/不进仓库**(§5.4 的产物表、§7.3)。它的用途是
    人工抽样(§6 第二层)与核对确定性判据的命中,所以这里刻意**不**做任何收
    窄——收窄过的存档没法用来裁决收窄本身。
    """
    return {
        "question_key": item["question_key"],
        "corpus_cell": item["corpus_cell"],
        "effort": item["effort"],
        "repeat": item["repeat"],
        "arm": arm,
        "question": item["question"],
        "intent_contract": contract,
        "answer": str(getattr(response, "answer", "") or ""),
        "conclusion": str(getattr(response, "conclusion", "") or ""),
        "evidence_level": str(getattr(response, "evidence_level", "") or ""),
        "citations": [
            row.model_dump() if hasattr(row, "model_dump") else dict(row)
            for row in (getattr(response, "citations", None) or ())
        ],
        "anchors": [
            row.model_dump() if hasattr(row, "model_dump") else dict(row)
            for row in (getattr(response, "anchors", None) or ())
        ],
        "trace_steps": list(raw_steps),
    }


def _ab_raw_path(out_dir: Path, label: str, item: dict) -> Path:
    """`.local` 存档的路径。`label` 是 `arm_label(policy, optimization)`,**不是**
    光秃秃的 policy:两条 v2 臂的存档否则会撞进同一个 `raw/v2/` 文件名(理由与
    取舍见 `reflect_ab.arm_label`)。"""
    return (
        out_dir / "raw" / label
        / f"{item['question_key']}_{item['corpus_cell']}_{item['effort']}"
          f"_r{item['repeat']}.json"
    )


def _ab_coverage_complete(response: Any) -> bool | None:
    """这次 run 的**枚举链终态**:complete / 不 complete / 没有枚举(`None`)。

    权威在 `AskResponse.result_sets[*].coverage.complete`,不在
    `result_coverage`(codex 规格评审 P1-1)。两者是**两条不同的链**:

    * `result_coverage` 是 `StructuredBatchCoverage`,只有表格批量那条路会写
      (`ask_service.py` 里它恒是 `structured_batch.coverage() if
      structured_batch else None`);
    * 目录/元素/知识对象的枚举终态住在 `result_sets` 里那个
      `TypedCollectionResult.coverage`——模型文件原话:「their coverage object
      is the authority for complete/partial」。

    只读 `result_coverage` 的后果不是少一个信号,而是**反号**:一道目录题真的
    枚举完整时 `result_coverage` 仍是 `None`,`completeness_claim_candidate` 于
    是把答案里那句「共 12 篇」判成虚报候选,两条臂的目录题因此全被强制进人工。

    合并规则(codex #703 R3 P2):**所有**在场的 coverage 都 complete 才算
    complete;有任何一条 partial 就是 False——答案里那句「共 N 篇」可能正是关于
    那条没枚举完的集合说的,一条无关的 complete 集合不能替它免掉人工复核
    (表格批量里「单表耗尽但批次漏了别的表」正是文档点名的那种区别)。一条
    coverage 都没有才是 `None` = 「没有任何东西背书那句完整性断言」。
    """
    coverages = [
        getattr(row, "coverage", None)
        for row in (getattr(response, "result_sets", None) or ())
    ]
    coverages.append(getattr(response, "result_coverage", None))
    values = [
        getattr(coverage, "complete", None)
        for coverage in coverages
        if coverage is not None
    ]
    seen = [value for value in values if isinstance(value, bool)]
    if not seen:
        return None
    return all(seen)


def _run_ab(
    args: argparse.Namespace, runner: Runner, units: Sequence[dict],
    cells: Sequence[str],
) -> int:
    """`ab` 的真跑路径。§5.5 的六条硬断言全部落在这个函数与它直接调的那几个里。

    断言与落点的对账(改这里之前先读一遍):

    1. **策略对号** —— 事前 `run_ab_once` 里核 `reflect_v2_active()`;事后每条
       臂的第一个 run 之后核 `policy_version`(`assert_arm_matches_evidence`)。
    2. **主库零接触** —— `--database-url` 必须以 `_test` 结尾(`_ab_preflight`),
       跑前跑后各点一次**主库**的 `_readonly_counts`(`_assert_readonly`)。
    3. **契约一致** —— 两臂共用 `_IntentCache` 里同一条缓存行,`_run_ab_unit`
       在提交前比一次 hash。
    4. **注入面全关** —— `_ab_process_env` 的三把闸。
    5. **Knowhow 断言** —— `assert_knowhow_reflect_v2_off`,一次性。
    6. **gold 可解析** —— `resolve_gold_sources` 在跑批**之前**把每个短名解析
       成恰好一个 `source_id`,解析不到就整批不跑。

    **T-EX8**:`--max-wall-minutes` 给了才计算 `deadline`(单调时钟,这里**一次**
    算出绝对到点时刻,不在批里反复重算);不给 ⇒ `deadline=None`,`_ab_loop`/
    `_ab_run_batch`/`_run_ab_arm` 的每一条新分支都先判 `is not None`,行为逐字
    不变。收尾处写 `manifest.json`(`app.eval.reflect_manifest.build_manifest`
    的 E3 通道),`stopped_by_budget` 如实记这批是不是因为预算到点提前收尾——
    是的话即便 `failed == 0` 也以非零退出码收尾(§13「不偷偷补跑到矩阵齐全」
    的另一半:也不能让预算掐断看起来像一次干净的成功跑批)。
    """
    from datetime import datetime

    # 必须在 import `app.core.config` 之前(与 `_search_process_env` 同一条理由)。
    os.environ.update(_ab_process_env(args))

    from app.core.request_context import reset_request_user, set_request_user
    from app.eval.reflect_ab import load_ab_gold, resolve_gold_sources
    from app.services.reasoning_retrieval import kg_in_scope_for

    # 单调时钟,一次计算(T-EX8 要点 1)。`None` = 不给 `--max-wall-minutes`,
    # 下游每一条新分支都先判 `is not None`,行为逐字不变。
    deadline = (
        None if args.max_wall_minutes is None
        else time.monotonic() + float(args.max_wall_minutes) * 60.0
    )
    arms = ab_arms(args)
    settings_by_arm = _settings_by_arm(arms)
    # **参照臂**:一批里那些与臂无关的事实(连接池容量、模型契约、`llm_log_enabled`、
    # 日志目录、语料点数、意图契约)只需要一份,固定取 `arms[0]`——即用户在
    # `--arms` 里写的第一条。取「第一条」而不是「legacy 那一条」,是因为二维化之
    # 后一批可以压根没有 legacy 臂(`--arms v2:off,v2:prefix_snapshot`);取有序
    # 列表的下标 0 而不是从 dict 里挑,是为了不让它随字典迭代顺序漂移(与
    # `_precompute_intents` 同一条口径)。
    reference_arm = arms[0]
    # 按**一份**连接池的容量 clamp,不按两份除以二:`ab` 是每条臂一个 repo、
    # 因此两个 `psycopg_pool.ConnectionPool`,但并发的是**单元**,而一个单元
    # 内部两条臂是背靠背跑的(`_run_ab_unit`)——任一时刻一个单元最多占住一条
    # 连接,而且只占其中一个池子的。所以最坏情况是 N 个单元恰好都停在同一条臂
    # 上,即某一个池子被要走 N 条连接;`N ≤ pool_max` 正是这里 clamp 的那一条
    # (codex 规格评审存疑项)。
    concurrency = _resolve_concurrency(
        runner, args.database_url, settings_by_arm[reference_arm],
        args.concurrency,
    )
    runner.say("concurrency", _ab_concurrency_note(concurrency))
    # **每条臂一个 repo**,不是「一个 repo + 两份 Settings」。`search` 那条路能
    # 共用一个 repo,是因为它直调 `ReasoningRetriever.from_repository(repo,
    # settings)`,检索器读的是传进去的那一份;而 Ask 走
    # `AskService._build_reasoning_retriever`,那里写死 `settings=self.settings`
    # ——构造这个 repo 时用的那一份(`repo.settings is ask_component.settings`,
    # 已核实)。共用一个 repo 会让两条臂双双跑 legacy,而数据从形状上看不出来。
    # `create_repository(settings, migrate=False, seed=False)`,不是默认值:默认
    # 的 `seed=True` 会用 `settings.admin_password` 重写 admin 密码哈希(codex
    # #700 R1 P1)。测试库已经由 `seed` 子命令建好并迁移过,这里只需要连上去。
    repos_by_arm = {
        arm: _search_repository(settings_by_arm[arm]) for arm in arms
    }
    runner.say("arm repositories",
               f"{len(arms)} 条臂各一个 repo(Ask 读构造期的 Settings,换不了)")
    repo = repos_by_arm[reference_arm]
    profile = repo.maintenance.resolve_owner_profile(args.user)
    if profile is None:
        print(f"ERROR: 测试库里找不到 fixture 用户: {args.user}", file=sys.stderr)
        return 2
    actor_id = str(getattr(profile, "id", "") or "")
    # §5.5-5 只对 **v2** 那一侧成立(判据是 `allow_reflect_v2=False` 把 v2 否
    # 决掉)。二维化之后一批可以压根没有 v2 臂(`--arms legacy`),那时这条断言
    # 没有对象——**说出来**而不是静默跳过:一条没跑过的硬断言不等于通过。
    v2_arm = next((arm for arm in arms if arm[0] == "v2"), None)
    if v2_arm is None:
        runner.say("knowhow assertion",
                   "这一批没有 v2 臂,§5.5-5 没有对象(未验证 ≠ 通过)")
    else:
        assert_knowhow_reflect_v2_off(
            repos_by_arm[v2_arm], settings_by_arm[v2_arm])
        runner.say("knowhow assertion",
                   "allow_reflect_v2=False ⇒ reflect_v2_active() 为假(§5.5-5)")

    notebooks = _ab_notebook_ids(args.database_url, cells)
    facts = _ab_corpus_facts(args, runner, repo, cells, notebooks, kg_in_scope_for)
    gold_by_key = load_ab_gold(load_questions())
    _ab_assert_gold_resolves(
        runner, units, gold_by_key, facts, resolve_gold_sources,
    )
    contract_short, contract_readable = _ab_model_contract(
        settings_by_arm[reference_arm])
    runner.say("model contract", f"{contract_short}  ({contract_readable})")

    # 每条臂共用同一份进程环境构造(`_settings_by_arm` 只翻那两个 reflect 开关),
    # `llm_log_enabled` 恒同,取参照臂那一份就够。
    # 关着时 Ask 照样调模型,只是不写交互日志——这时候成本三键必须整批记
    # unknown,不能把空日志喂给 `slice_llm_usage` 投出一批假的 0(codex #703
    # R1 P2)。
    llm_log_enabled = bool(
        getattr(settings_by_arm[reference_arm], "llm_log_enabled", True)
    )
    if not llm_log_enabled:
        runner.say(
            "llm log disabled",
            "LLM_LOG_ENABLED=false,这一批的成本三键"
            "(model_calls/prompt_tokens/completion_tokens)整批记 unknown",
        )

    before = _readonly_counts(args.source_db_url)
    runner.say("main database baseline", _format_counts(before))
    baseline_problem = readonly_baseline_problem(before)
    if baseline_problem:
        print("ERROR: " + baseline_problem, file=sys.stderr)
        return 2
    context = set_request_user(profile)
    failed = 0
    stopped_by_budget = False
    intent_digests: dict[str, str] = {}
    started_at = datetime.now().isoformat()
    try:
        failed, stopped_by_budget, intent_digests = _ab_loop(
            args, runner, units, facts, repos_by_arm,
            actor_id=actor_id, profile=profile, concurrency=concurrency,
            gold_by_key=gold_by_key, model_contract=contract_short,
            log_dir=_ab_llm_log_dir(settings_by_arm[reference_arm]),
            event_dir=Path(rig_event_log_dir(args.out_dir)),
            arms=arms, reference_arm=reference_arm,
            clock=datetime.now, llm_log_enabled=llm_log_enabled,
            deadline=deadline,
        )
    finally:
        reset_request_user(context)
        # **在 `finally` 里**(codex 质量评审 P2-5):跑批中途抛出去时,「主库
        # 零接触」这条断言此前整个被跳过——而那正是最该量一次的时刻(异常路径
        # 上谁也说不清收尾走到了哪一步)。断言自己再抛的话会盖掉原始异常,所以
        # 这里只在没有别的异常在飞时才让它抛;有异常在飞时降级成一行告警。
        _assert_readonly_on_exit(runner, before, args.source_db_url)
    finished_at = datetime.now().isoformat()
    _write_ab_manifest(
        runner, args, units=units, cells=cells, arms=arms, facts=facts,
        model_contract=contract_short, settings=settings_by_arm[reference_arm],
        intent_contract_digest_by_question=intent_digests,
        started_at=started_at, finished_at=finished_at,
        stopped_by_budget=stopped_by_budget,
    )
    if stopped_by_budget:
        # 预算到点是这一批提前收尾的**理由**,即便 0 个 run 落成 failed 也不能
        # 让它看起来像一次干净跑完的成功批次(T-EX8 要点 1「整批以预算到点为由
        # 非零退出」)。
        #
        # 这条 `return` 在 `if failed:` **之前**,所以两件事同时成立时只打这一
        # 句——那就把 `failed` 数带进这一句里(评审 P3-8):失败数既不进 manifest
        # 闭集(没有这个键)、也不该因为预算先掐断就从 stderr 上整个消失,否则
        # 「预算到点」会盖掉「这一批里还有 N 个 run 真的崩了」。
        print(
            "ERROR: 整批墙钟预算到点提前收尾"
            f"(--max-wall-minutes={args.max_wall_minutes}),不补跑;"
            f"另有 {failed} 个 run FAILED(见 ab-runs.log);"
            "manifest.json 的 stopped_by_budget=true", file=sys.stderr,
        )
        return 1
    if failed:
        # 失败 run 已各自落成 `status=failed` 行;整批仍以非零退出码收尾,不能
        # 让「每个 run 都失败」看起来像一次成功的跑批(与 `search` 同口径)。
        print(f"ERROR: {failed} 个 run FAILED,见 ab-runs.log", file=sys.stderr)
        return 1
    return 0


def _assert_readonly_on_exit(
    runner: Runner, before: dict[str, int | str | None], source_db_url: str,
) -> None:
    """收尾那次「主库零接触」核对。**不掩盖正在飞的异常**。

    `sys.exc_info()` 非空 ⇒ 我们正在一条异常路径上退出:此时再抛一个
    `RuntimeError` 会把根因换成一句关于行数的话。那种情况下把结果打成告警,
    原始异常照常往上走。
    """
    in_flight = sys.exc_info()[0] is not None
    try:
        _assert_readonly(
            runner, before, _readonly_counts(source_db_url), command="ab",
        )
    except Exception:  # noqa: BLE001 — 见 docstring
        if not in_flight:
            raise
        print("\033[31m[READ-ONLY VIOLATION]\033[0m "
              "(另有异常正在向上抛,见下方 traceback)", file=sys.stderr)


def _ab_corpus_facts(
    args: argparse.Namespace, runner: Runner, repo: Any, cells: Sequence[str],
    notebooks: dict[str, str], kg_in_scope_for: Any,
) -> dict[str, dict]:
    """每个语料格的事实,**跑之前一次点清**。与 `_search_corpus_facts` 同形,
    另外多带两样 A/B 才用得上的东西:来源的 `(id, title)` 全表(gold 解析、范围
    判据、锚点第三跳都要它)与该格的 `corpus_signature`。
    """
    facts: dict[str, dict] = {}
    for cell in cells:
        notebook = notebooks[cell]
        repo.get_notebook(notebook)  # 不存在直接 KeyError,而不是跑出一批空 run
        rows = _ab_source_rows(args.database_url, notebook)
        fact = {
            "notebook": notebook,
            "sources": len(rows),
            "source_rows": rows,
            "source_titles": {row["id"]: row["title"] for row in rows},
            "kg_in_scope": bool(kg_in_scope_for(repo.retrieval, notebook)),
        }
        fact["corpus_signature"] = _ab_corpus_signature(
            cell, fact, args.database_url,
        )
        facts[cell] = fact
        runner.say(
            "corpus fact",
            f"{cell}: sources={fact['sources']} "
            f"kg_in_scope={fact['kg_in_scope']} "
            f"corpus_signature={fact['corpus_signature']}",
        )
    return facts


def _ab_assert_gold_resolves(
    runner: Runner, units: Sequence[dict], gold_by_key: Mapping,
    facts: Mapping, resolve_gold_sources: Any,
) -> int:
    """§5.5-6:跑批**之前**把每道题的 `gold_sources` 解析一遍,解析不到就整批停。

    §3.2 的原话是「解析不到任何一个就是配置错误,跑批之前响亮失败,不能静默变
    成 0」。这一步刻意放在第一个模型调用之前:一次 408 run 的批要跑几个小时,
    而这条错在第一秒就看得见。

    缓存键是 **`(corpus_cell, question_key)`**,不是只有 `question_key`:同时
    选 `B_kg` 与 `B_nokg` 时,同一道题会跑在两个不同的笔记本上,`gold_sources`
    短名在其中一个笔记本里缺失或歧义,不能因为另一个笔记本先解析通过了就被
    绕过——那会让这道题在跑坏的那一格上悄悄产出一行误导的 0 或虚高
    (codex #703 R2 P2-2)。

    解析结果本身**刻意不留**:`anchors_on_gold` 的 B 格第三跳走的是同一条
    「标题包含短名」的规则(`count_anchors_on_gold` 里),把一份等价的 id 集合
    再传一遍,只会多出一个可能与它分叉的第二真源。这里要的是那条断言,不是它
    的返回值。返回的题数只用来打日志。
    """
    from app.eval.reflect_ab import GoldError, gold_for

    checked: set[tuple[str, str]] = set()
    for unit in units:
        key = unit["question_key"]
        cell = unit["corpus_cell"]
        cache_key = (cell, key)
        if cache_key in checked:
            continue
        gold = gold_for(gold_by_key, key)
        if gold is None or not gold.gold_sources:
            continue
        try:
            resolve_gold_sources(gold, facts[cell]["source_rows"])
        except GoldError as exc:
            raise GoldError(f"[{cell}] {exc}") from exc
        checked.add(cache_key)
    runner.say("gold sources resolved",
               f"{len(checked)} 组(题×格)的 gold_sources 各解析到唯一来源"
               "(§5.5-6)")
    return len(checked)


def _ab_loop(
    args: argparse.Namespace, runner: Runner, units: Sequence[dict],
    facts: dict[str, dict], repos_by_arm: dict[tuple[str, str], Any],
    *, actor_id: str, profile: Any, concurrency: int, gold_by_key: Mapping,
    model_contract: str | None, log_dir: Path, clock: Any,
    event_dir: Path | None = None,
    arms: Sequence[tuple[str, str]] | None = None,
    reference_arm: tuple[str, str] | None = None,
    llm_log_enabled: bool = True,
    deadline: float | None = None,
) -> tuple[int, bool, dict[str, str]]:
    """整批配对单元。每个单元产出**两行**(两臂),一起写、一起标 `paired`。

    返回 `(failed, stopped_by_budget, intent_contract_digest_by_question)`:

    * `failed` —— 落成 `status="failed"` 的行数(不含 `status="cancelled"`,
      T-EX8 M5-4:预算掐断是删失观察,不是失败)。整批据此以非零退出码收尾
      (与 `search` 同口径):「每个 run 都失败」不能看起来像一次成功的跑批。
    * `stopped_by_budget` —— `deadline`(T-EX8 `--max-wall-minutes`)是否真的
      让这一批提前收尾;`deadline is None`(不给)时恒 `False`。
    * `intent_contract_digest_by_question` —— `question_key → ab_contract_
      digest(...)`,manifest 的 `intent_contract_digest_by_question` 键直接
      消费这份返回值,不重新遍历 `_IntentCache`。

    `--concurrency > 1` 时并发的是**单元**,不是臂:臂序随机与背靠背是 A/B 的
    立身之本(§4.2),把两条臂拆到不同线程会让「同题同档同时刻」这句话不再成
    立。重复轮之间加一道栅栏(逐轮 `_ab_round_units`),这样被截断的前缀仍然是
    一份**完整轮**的数据集——预算到点提前收尾时这条性质仍然成立:一轮里被掐
    掉的单元落 `cancelled` 行,后续轮次(`state["stopped_by_budget"]` 已真)整
    个不再开始(见下面循环顶部的检查),已完成的前缀因此还是**完整轮**。

    并发时 per-call 表在**整批收尾**时一次出(见下面那段 `finally`):逐 run 的
    时间窗在并发下切不干净,而 `support_id` 的 ⋈ 不受窗口影响。

    `arms` / `reference_arm` / `event_dir` 的 `None` 语义见 `_run_ab_unit`。
    """
    arms = list(arms) if arms is not None else ab_arms(args)
    reference_arm = reference_arm or arms[0]
    if event_dir is None:
        event_dir = Path(rig_event_log_dir(args.out_dir))
    runner.out_dir.mkdir(parents=True, exist_ok=True)
    intents = _IntentCache(
        runner.out_dir / "intents.jsonl", enabled=not args.no_intent
    )
    # 进度分母按**这次真的要跑的臂数**算,不是恒按两臂:只跑一侧时恒按闭集大小
    # 会让每一行日志都写着 `0007/0016`,而这批一共只有 8 个 run(codex 规格/质量
    # 评审 P3)。二维化之后 `arms` 已经是「这一批要跑的臂」,直接取长度。
    state = {
        "verified": set(), "index": 0, "total": len(units) * len(arms),
        "failed": 0, "lock": threading.Lock(),
        # T-EX8:`deadline is None` 时这个键永远不会被翻成 `True`(见
        # `_ab_run_batch`),下面 `return` 时恒 `False`——不给 `--max-wall-
        # minutes` 的路径逐字相同。
        "stopped_by_budget": False,
    }
    # 并发路径的整批窗口:批开跑之前记一次偏移,收尾时读到 EOF。
    batch_llm_offsets = _ab_llm_offsets(log_dir)
    batch_event_offsets = _ab_llm_offsets(event_dir, glob=RIG_EVENT_LOG_GLOB)
    rows_handle = (runner.out_dir / "ab-runs.jsonl").open("a", encoding="utf-8")
    log = (runner.out_dir / "ab-runs.log").open("a", encoding="utf-8")
    if not llm_log_enabled:
        # 整批一次(codex #703 R1 P2):落进 `ab-runs.log`,不是只打在终端——
        # 事后翻这份数据的人不该靠回想「当时是不是关了日志」来解释一整批
        # unknown。
        log.write(
            "NOTE: LLM_LOG_ENABLED=false,本批全部 run 的成本三键"
            "(model_calls/prompt_tokens/completion_tokens)记 unknown\n"
        )
        log.flush()
    try:
        for repeat, batch in _ab_round_units(units):
            # 预算已经在前一轮被掐断时,后续轮次**整个不开始**——不偷偷补跑到
            # 矩阵齐全(§13 原文),这些单元不落行、不发调用。那道判在
            # `_ab_run_batch` 的批入口(「已经过期 ⇒ 零派发」),这里**没有**
            # 第二道:此前那一层是死分支(变异验证:删掉行为不变),而它自称的
            # 唯一收益「少打一行 `repeat round rN`」在真实用例里也不成立——串行
            # 路每轮只有一个单元时循环顶的 `break` 从不触发,进下一轮前
            # `stopped_by_budget` 还是 `False`,那行日志照打(评审 F8 / P3-3)。
            runner.say("repeat round", f"r{repeat}: {len(batch)} 个配对单元")
            _ab_run_batch(
                batch, args=args, runner=runner, facts=facts,
                repos_by_arm=repos_by_arm, actor_id=actor_id,
                profile=profile, concurrency=concurrency, intents=intents,
                gold_by_key=gold_by_key, model_contract=model_contract,
                log_dir=log_dir, event_dir=event_dir, arms=arms,
                reference_arm=reference_arm, clock=clock, state=state,
                rows_handle=rows_handle, log=log,
                llm_log_enabled=llm_log_enabled, deadline=deadline,
            )
    finally:
        log.close()
        rows_handle.close()
        if concurrency > 1:
            # 并发跑批的 per-call 表:整批一次,run 级标签**全部 unknown**
            # (`_rig_call_tags(..., attributed=False)`)。臂那一维在这里不成立
            # ——`support_id` 只把日志行与它自己的调度事件对上号,它不知道这次
            # 调用属于哪个 run。硬塞一条臂进去比不出表更坏:那是一批看起来精确、
            # 实则一半记错了臂的行(与并发下成本三键强制 unknown 同一条口径)。
            # 在 `finally` 里:跑批中途抛出去时,已经烧掉的那些调用仍然值得留一
            # 份账。
            _write_call_rows(runner.out_dir, CALLS_UNATTRIBUTED_LABEL,
                             _rig_call_rows(
                                 log_dir, event_dir,
                                 llm_offsets=batch_llm_offsets,
                                 event_offsets=batch_event_offsets,
                                 tags=None,
                             ))
    # **只取这一批实跑的题**(评审 P2-1):`_IntentCache.__init__` 会把 out-dir
    # 上已有的 `intents.jsonl` 全部读进 `_rows`(那个文件存在的理由就是断点重跑
    # 不重付模型钱),而默认 out-dir 是固定的 `.local/t0`。不求交的话「先跑一次
    # 全量、再跑 `--limit 1` 复现某题」会让 manifest 声称一道题的批用了两道题的
    # 契约,`matrix.questions=1` 与这张表当场自相矛盾——而 manifest 的用途正是
    # 「这批用的是哪一份冻结契约」。
    batch_questions = {unit["question_key"] for unit in units}
    intent_digests = {
        key: ab_contract_digest(contract)
        for key, contract in intents._rows.items()
        if key in batch_questions
    }
    return int(state["failed"]), bool(state["stopped_by_budget"]), intent_digests


def _ab_round_units(units: Sequence[dict]) -> list[tuple[int, list[dict]]]:
    """按重复轮切段,**保持原顺序**。重复轮是最外层循环(§4.2)。"""
    batches: dict[int, list[dict]] = {}
    for unit in units:
        batches.setdefault(int(unit["repeat"]), []).append(unit)
    return [(repeat, batches[repeat]) for repeat in sorted(batches)]


def _ab_run_batch(
    batch: Sequence[dict], *, concurrency: int, deadline: float | None = None,
    **kwargs: Any,
) -> None:
    """一轮里的全部配对单元。**首个逃逸出来的异常 ⇒ 整轮收摊**。

    「逃逸出来的异常」是一个很窄的集合:单个 run 内部的失败已经在
    `_run_ab_arm` 里被投影成 `status="failed"` 行并隔离掉了(与 `search` 同
    口径),能到这里的只剩 §5.5 那几条硬断言不成立(契约不同一、声明的臂与
    轨迹证据对不上)、`AskCancelled` 与 `KeyboardInterrupt`——每一条的正确反应
    都是「先修再跑整批」,而不是接着烧几百次模型调用。

    收摊要做两件事,少一件都不成立(codex 质量评审 P2-3):

    * `cancel_event.set()` 唤醒**已经在跑**的单元——`shutdown(cancel_futures=
      True)` 撤不掉已经被 worker 取出来的那些,它们只能靠自己的取消事件退出;
    * `shutdown(cancel_futures=True)` 撤掉**还在队列里**的。此前这里是
      `with ThreadPoolExecutor(...)` + `as_completed`,退出时走的是默认的
      `shutdown(wait=True)`:剩下的单元一个不少地照跑完,Ctrl-C 也停不下来。

    **T-EX8 整批墙钟预算**(`deadline` 非 `None` 时才生效,`None` = 今天的行为
    逐字相同,下面每条新分支都先判 `deadline is not None`):

    * 到点后**停止派发**——串行路在 `for unit in batch` 循环顶部判(`kwargs
      ["state"]["stopped_by_budget"] = True` 后 `break`,批里剩下的单元不落行、
      不发调用);并发路在整批开跑前若已过点直接**零派发**返回,mid-batch 那半
      靠一个 `threading.Timer` 在 `deadline` 那一刻触发,复用**既有**的
      `abort()`(设 `aborted` ⇒ `worker()` 入口不再派发新单元;唤醒
      `active_events` ⇒ 在途单元的 `cancel_event` 被设,`run_ab_once` 尽快抛
      `AskCancelled`)。`worker()` 入口另外**自己按墙钟判一次**:Timer 线程被
      延迟时 `aborted` 在到点后短暂仍为假,只判它会漏派一个单元出去(评审
      F5);那条早退也如实翻 `stopped_by_budget`,理由见该处注释。
    * 在途单元的 `AskCancelled` 由 `_run_ab_arm` 按 `deadline` 转成一行
      `status="cancelled"`(见该函数文档),**不**escaping 出 `worker()`,所以
      这条预算路径不会触发 `except BaseException` 那条「先修再跑整批」的
      `abort()`/`fatal` 级联——整批仍然干净地跑完,只是 `state
      ["stopped_by_budget"]` 被标记为真,由 `_ab_loop`/`_run_ab` 读出并写进
      manifest、决定退出码。
    """
    state = kwargs["state"]
    if deadline is not None and time.monotonic() >= deadline:
        # 已经过期的 deadline(常见于：预算在上一轮结束时就已经耗尽,这一整
        # 轮从第一个单元开始就不该派发)。
        state["stopped_by_budget"] = True
        return
    if concurrency <= 1:
        # **不经过线程池**:`--concurrency 1` 的行为必须与并发落地前逐字一致,
        # 包括「所有调用都在主线程里发生」这件事本身(与 `_search_loop` 同一条
        # 理由:`ThreadPoolExecutor(max_workers=1)` 也会把调用挪到另一个线程,
        # 单这一点就足以让 ContextVar 的可见性行为变掉)。
        for unit in batch:
            if deadline is not None and time.monotonic() >= deadline:
                # 串行路没有「唤醒在途」这件事:检查点只在循环顶部,已经开跑的
                # 单元此前从未被中途打断过,这里也不新引入这条能力(§13 只要求
                # 「停止派发」,不要求串行路半路抢占)。
                state["stopped_by_budget"] = True
                break
            _run_ab_unit(unit, concurrency=concurrency, deadline=deadline, **kwargs)
        return
    profile = kwargs["profile"]
    runner: Runner = kwargs["runner"]
    aborted = threading.Event()
    active_events: list[threading.Event] = []
    active_lock = threading.Lock()

    def abort(reason: str) -> None:
        # `aborted.set()` 与快照 `active_events` 在**同一把锁**里,`worker()` 的
        # 登记处再复核一次——与 `_search_loop_concurrent` 逐字同形(codex #700
        # R21 P2 在 T0 rig 上修过的同型缺陷,`ab` 这条路此前没跟上,评审 F5 /
        # P2-3)。不这么做的失败场景:一个刚过了入口 `aborted` 检查的 worker 会
        # 在快照之后才 append,带着一个**永远不会被 set** 的 `cancel_event` 开始
        # 调模型,而 `shutdown(wait=True)` 只能等它跑完——`--max-wall-minutes`
        # 因此被越过整整一次 Ask(最坏 `reasoning_timeout_seconds ×
        # attempt_budget`),而那一行落的是 `status=done`,manifest 看不出来。
        with active_lock:
            if aborted.is_set():
                return
            aborted.set()
            events = list(active_events)
        for event in events:
            event.set()
        runner.say("ab aborted", reason)

    budget_timer: threading.Timer | None = None
    if deadline is not None:
        def _budget_hit() -> None:
            state["stopped_by_budget"] = True
            abort("--max-wall-minutes 预算到点:停止派发、唤醒在途单元")

        budget_timer = threading.Timer(
            max(0.0, deadline - time.monotonic()), _budget_hit,
        )
        budget_timer.daemon = True
        budget_timer.start()

    def worker(unit: dict) -> None:
        from app.core.request_context import reset_request_user, set_request_user

        if deadline is not None and time.monotonic() >= deadline:
            # 入口按**墙钟**直接判一次(T-EX8 要点 1 的字面要求),不只依赖
            # `aborted`:Timer 线程被 GIL/OS 延迟时 `aborted` 在到点后短暂仍为
            # 假,只判它的 worker 会在那个窗口里又从队列取一个新单元开跑
            # (评审 F5)。
            #
            # 同时**如实翻** `stopped_by_budget`:队列里的单元确实因为预算没被
            # 派发,而下面 `finally` 里的 `budget_timer.cancel()` 可能赶在 Timer
            # 回调之前(全部 worker 都走这条早退、批循环因此立刻结束时),那一刻
            # 只有这里能记下这件事。单键赋值在 GIL 下原子,与 `_budget_hit` 同
            # 一条口径。
            state["stopped_by_budget"] = True
            return
        if aborted.is_set():
            # 已经决定收摊:还没真的开始跑的单元直接退出,不再发起模型调用。
            # 预算到点(`_budget_hit` 调 `abort()`)与 §5.5 硬断言/
            # `KeyboardInterrupt` 触发的收摊走的是同一个 `aborted` 标志,效果
            # 相同——都是「不再派发新单元」。
            return
        cancel_event = threading.Event()
        with active_lock:
            # 与 `abort()` 在同一把锁下复核:收摊已经开始就不再登记、不再开跑
            # (见 `abort()` 的说明;`_search_loop_concurrent` 同形)。
            if aborted.is_set():
                return
            active_events.append(cancel_event)
        ctx = set_request_user(profile)
        try:
            _run_ab_unit(
                unit, concurrency=concurrency, cancel_event=cancel_event,
                deadline=deadline, **kwargs,
            )
        finally:
            reset_request_user(ctx)
            with active_lock:
                if cancel_event in active_events:
                    active_events.remove(cancel_event)

    pool = ThreadPoolExecutor(max_workers=concurrency)
    fatal: BaseException | None = None
    try:
        futures = [pool.submit(worker, unit) for unit in batch]
        try:
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:  # noqa: BLE001 — 见 docstring
                    # 只传类名(codex #703 R1 P2):`str(exc)` 可能带本机路径
                    # (如 `_write_ab_raw` 撞上 `PermissionError` 时把
                    # `.local` 的绝对路径编进异常文本)或 provider 侧的请求
                    # 细节,截 200 字不构成脱敏,进度日志与终端都不该收它。
                    abort(type(exc).__name__)
                    fatal = exc
                    break
        except KeyboardInterrupt as exc:
            abort("KeyboardInterrupt")
            fatal = exc
    finally:
        if budget_timer is not None:
            budget_timer.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    if fatal is not None:
        raise fatal


def ab_contract_digest(contract: object) -> str:
    """一份冻结契约的短码。§5.5-3「两臂必须引用同一条契约缓存行」的**比较对象**。

    两臂共用同一份契约是 A/B 成立的前提(§5.4:契约不同,必答方面就不同,两臂
    根本不在比同一件事)。今天它由构造保证——`_run_ab_unit` 只调一次
    `_IntentCache.get`,两臂拿到的是同一个对象。断言的意义在于那条构造哪天被
    改掉:把 `intents.get` 挪进臂的循环里,数据看起来照样出得来,而两臂已经在
    比两件不同的事了。契约正文不进日志也不进数据集,所以比的是短码。
    """
    return merge_key(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                                default=str))


#: 工作树有未提交改动时拼在 `code_sha` 后面的后缀;`git status` 自己也读不出来
#: 时拼另一个(**不**默认干净:那会让 manifest 说一句它没验过的话)。两个后缀都
#: 只用短码字符集里的字符(`-` 在 `[A-Za-z0-9_:\-.+→]` 内),40 位全 SHA 加上
#: 它们仍在 `_is_short_code` 的 64 字符门槛内。
_GIT_DIRTY_SUFFIX = "-dirty"
_GIT_DIRTY_UNKNOWN_SUFFIX = "-dirty_unknown"


def _ab_git_sha() -> str:
    """`git rev-parse HEAD` 的**全 SHA**(40 位十六进制),读不出来(浅克隆、
    非 git checkout、`git` 不在 PATH 上、超时)⇒ `"unknown"`,**不抛**
    (T-EX8 要点 2)。

    `code_sha` 是「冻结这一批跑在哪份代码上」的锚点,但它读不出来不该让一次
    跑了几小时的批因为收尾时的一次诊断性 `git` 调用而报废——那不是这次跑批
    要验的东西。`cwd=ROOT` 而不是当前工作目录:rig 可能被从任意目录调用。

    **工作树脏就拼 `-dirty`**(评审 P3-5):rig 被 worktree 里的未提交改动驱动
    是这个程序的常态,一个光秃秃的 commit SHA 会把这批数据锚到一份**不含实际
    跑的代码**的提交上——而这个键的全部意义就是那条锚。`git status` 自己失败时
    拼 `-dirty_unknown`,不静默当干净。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = result.stdout.strip()
    if not sha:
        return "unknown"
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
            text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return sha + _GIT_DIRTY_UNKNOWN_SUFFIX
    return sha + (_GIT_DIRTY_SUFFIX if status.stdout.strip() else "")


def _write_ab_manifest(
    runner: Runner, args: argparse.Namespace, *, units: Sequence[dict],
    cells: Sequence[str], arms: Sequence[tuple[str, str]],
    facts: Mapping[str, dict], model_contract: str | None, settings: Any,
    intent_contract_digest_by_question: Mapping[str, str],
    started_at: str, finished_at: str, stopped_by_budget: bool,
) -> None:
    """`ab` 收尾写 `<out-dir>/manifest.json`(T-EX8 要点 2;计划 Q9 · E3 通道)。

    真正的闭集/隐私/`matrix` 子键契约全在 `app.eval.reflect_manifest`——这里
    只把 `ab` 已经算好的事实翻成那份契约要的形状,不重新发明校验规则。

    `matrix` 的基数子键(评审拍板,见 `reflect_manifest.
    REQUIRED_MATRIX_KEYS_BY_CHANNEL["e3"]`)全部从 `units`/`arms` 直接数出来,
    不借用 `cmd_ab` dry-run 那份 `unique_questions`(`(corpus_cell,
    question_key)` 配对集合——按格去重会把「2 格 × 12 题」记成 24,这里要的是
    12 道**真实**题)。`repeats` 是 `--round` 下的**这一轮**(等于 1),不是
    `--repeats` 的总档位;轮号本身另记 `matrix["round_index"]`,不塞进
    `repeats`。

    **`cells` 与 `corpus_signature_by_cell` 从 `units` 实跑格收窄**(评审 F4 /
    P1-2):`cells` 参数是 `--cell` 的**声明**格数,而 `ab_plan` 走 `ask_plan`
    按 `row["corpus"] == corpus` 过滤——`--only-question A-q08`(只存在于 A 格)
    在默认两格下出 6 个单元、全在 `A_nokg`,声明格数却是 2。不收窄的话
    `corpus_signature_by_cell` 会带上一个这批压根没跑的格的签名,「冻结这批跑在
    哪份语料上」这个事实当场变成假的。

    **`planned_runs` 是这批 run 数的账**(额外子键,评审拍板;三条通道同规则):
    E3 各格的题集**不相交**,所以五个基数相乘(`questions × cells × …`)比真实
    run 数多出一个 `cells` 倍——`cells` 不是乘数。真正对得上的那个数只有
    `len(units) × len(arms)`,单独写一个子键,不去改基数子键的语义。
    """
    ran_cells = {unit["corpus_cell"] for unit in units}
    # 顺序按 `cells` 的声明顺序(与 dry-run 打印、`_ab_corpus_facts` 同序),
    # 只是把没跑的格滤掉;不按集合迭代顺序,那会让 manifest 在两次相同的跑批
    # 之间漂移。
    ordered_cells = [cell for cell in cells if cell in ran_cells]
    matrix: dict[str, Any] = {
        "questions": len({unit["question_key"] for unit in units}),
        "cells": len(ran_cells),
        "efforts": len({unit["effort"] for unit in units}),
        "arms": len(arms),
        "repeats": 1 if args.round is not None else args.repeats,
        "planned_runs": len(units) * len(arms),
    }
    if args.round is not None:
        matrix["round_index"] = int(args.round)
    budgets = {
        "reasoning_timeout_seconds": int(
            getattr(settings, "reasoning_timeout_seconds",
                    REASONING_TIMEOUT_SECONDS_DEFAULT)
        ),
        # 复用那个常量,不再第三次抄一遍 `reasoning_max_retries` 的默认值
        # (评审 P3-1:`getattr(settings, "reasoning_max_retries", 1)` 的兜底在
        # 真跑里永远走不到,只被用例的 `SimpleNamespace()` 触发,而那个 `1` 是
        # 同一个配置默认值的第三份镜像)。常量本身由一条守卫钉在
        # `Settings().reasoning_max_retries` 上,与 dry-run 那行「请求上界」读的
        # 是同一个数——两处从此不可能分叉。**登记的边界**:进程环境里把
        # `REASONING_MAX_RETRIES` 显式调离默认值时,这个键仍写配置默认值算出的
        # 预算;真实请求数由 llm.jsonl 行上的 `attempts` 事后给出(与
        # `_ab_call_estimate` 同一条口径)。
        "reasoning_attempt_budget": REASONING_ATTEMPT_BUDGET,
        "max_wall_minutes": args.max_wall_minutes,
        "batch_deadline_seconds": (
            None if args.max_wall_minutes is None
            else round(float(args.max_wall_minutes) * 60)
        ),
    }
    row = build_manifest(
        code_sha=_ab_git_sha(),
        channel="e3",
        arms=[format_arm(*arm) for arm in arms],
        optimization_by_arm={format_arm(*arm): arm[1] for arm in arms},
        common_baseline="off",
        corpus_signature_by_cell={
            cell: facts[cell]["corpus_signature"] for cell in ordered_cells
        },
        intent_contract_digest_by_question=dict(
            intent_contract_digest_by_question),
        model_contract=model_contract,
        arm_order_seed=None,
        matrix=matrix,
        budgets=budgets,
        started_at=started_at,
        finished_at=finished_at,
        stopped_by_budget=stopped_by_budget,
    )
    # `build_manifest` 内部已经验过一遍、并且 `deepcopy` 过,从它返回到写盘之间
    # 这一行**没有任何手改**——所以这里不再复验一次(评审 F7 / P3-6:那是一行
    # 死守卫,而它引的「与 `_ab_failed_row` 同一条纪律」也不成立:那边是先构造
    # 再逐键覆写 `AB_FAILED_UNKNOWN_KEYS` 才复验,这里没有对应动作)。
    # 落盘那两份走 `_write_manifest`——三条通道共用同一个写法(评审 P3-7 /
    # T-EX4 P2 汇合项:E1/E2/E3 各内联一份的话,「最新 + 历史」这条规则会在
    # 三处各自漂移)。
    _write_manifest(row, runner=runner)


#: 失败 run 那一行里必须落成 unknown 的答案级/成本级键。`answer_chars=0` 会被
#: 读成「模型答了个空字符串」,而真相是「这个 run 没跑到有答案的那一步」;成本
#: 三键同理——崩在半路的 run,时间窗切到的是半截账。T0 那一半(轨迹派生的
#: `reflect_turns` / `skip_reasons` / `termination_reason` …)刻意**保留**:失败
#: 前捕获到的步是真的发生过的证据(codex #700 R13 P2 的同一条口径)。
AB_FAILED_UNKNOWN_KEYS: tuple[str, ...] = (
    "answer_chars", "answer_empty", "citations", "anchors_total",
    "anchors_on_gold", "anchors_unresolved", "citations_out_of_scope",
    "completeness_claim", "model_calls", "prompt_tokens", "completion_tokens",
    "finish_reason_codes", "latency_ms_total",
)


def _ab_failed_row(
    unit: dict, *, arm: str, fact: dict, model_contract: str | None,
    steps: Sequence[dict] = (), has_intent_contract: bool = False,
    optimization: str = "off", run_wall_ms: int | None = None,
    status: str = "failed",
) -> dict:
    """没跑成的一条臂的那一行:键集与成功行**逐字相同**。

    `status` 默认 `"failed"`(既有语义不变);T-EX8 的整批墙钟预算到点时,调用方
    传 `status="cancelled"`——两者共用同一条「没有答案/没有成本三键」的落行
    路径,唯一区别是 `JOB_STATUSES` 里那个闭集值,读侧据它把预算掐断的删失
    观察与真实失败分开计(§10 M5-4)。

    `optimization` 与 `arm` 同一条口径:声明照常落,失败行才能和成功行放进同一
    张按臂分组的表。`run_wall_ms` 见 `stamp_run_wall_ms`——崩在半路的 run 有墙钟
    (它确实占了这么久),压根没开跑的(范围解析失败)是 `None`。

    范围解析失败(整个单元作废)与臂内抛异常共用这一行。此前两条路都是直接
    `return` / 让异常往上抛,于是失败的 run 从 `ab-runs.jsonl` 里凭空消失——
    分析脚本只读 JSONL,样本数因此偏向跑成的那一侧,而「哪一侧更容易崩」恰恰
    是 A/B 要回答的问题之一(codex 质量评审 P2-4)。失败原因是自由文本,只进
    `ab-runs.log`,不进数据集。

    工作负载维度(题号 / 语料格 / 档位 / 轮次 / 臂 / kg_in_scope / 来源数 /
    契约有无 / 语料指纹 / 模型短码)全部照常落:失败行要能和成功行放进同一张
    分组表,否则「这一格失败多」这句话没法说(codex #700 R14 P2 的同一条口径)。
    """
    from app.domain.reasoning_trace_stats import assert_projection_values
    from app.eval.reflect_ab import assert_ab_closed, project_ab_run

    row = project_ab_run(
        list(steps), arm=arm, effort=unit["effort"], repeat=int(unit["repeat"]),
        optimization=optimization,
        question_key=unit["question_key"], corpus_cell=unit["corpus_cell"],
        answer="", citations=(), anchors=(), coverage_complete=None,
        kg_in_scope=fact["kg_in_scope"], sources_count=fact["sources"],
        has_intent_contract=has_intent_contract,
        notebook_id=fact["notebook"], gold=None,
        model_contract=model_contract,
        corpus_signature=fact["corpus_signature"],
        status=status,
    )
    for key in AB_FAILED_UNKNOWN_KEYS:
        row[key] = None
    stamp_run_wall_ms(row, run_wall_ms)
    # 手改过 `project_ab_run` 已经验过的行,再验一遍:调用方直接把它写进 JSONL。
    assert_ab_closed(row)
    assert_projection_values(row)
    return row


def _run_ab_unit(
    unit: dict, *, args: argparse.Namespace, runner: Runner,
    facts: dict[str, dict], repos_by_arm: dict[tuple[str, str], Any],
    actor_id: str, profile: Any, concurrency: int, intents: "_IntentCache",
    gold_by_key: Mapping, model_contract: str | None, log_dir: Path,
    clock: Any, state: dict, rows_handle: Any, log: Any,
    event_dir: Path | None = None,
    arms: Sequence[tuple[str, str]] | None = None,
    reference_arm: tuple[str, str] | None = None,
    cancel_event: Any = None, llm_log_enabled: bool = True,
    deadline: float | None = None,
) -> None:
    """一个配对单元:同题同档同轮,两条臂背靠背跑完,两行一起落。

    `deadline`(T-EX8)只是原样转发给 `_run_ab_arm`——这一层不判断是否到点,
    到点后是否还继续跑本单元由调用方(`_ab_run_batch`)在派发前决定;这里只
    保证已经派发的单元把 `deadline` 带到位,好让 `_run_ab_arm` 分清「预算掐断
    的取消」与「§5.5 硬断言/KeyboardInterrupt 触发的取消」。

    **臂序按 run 随机**(§4.2):provider 的排队与限流漂移对两臂等量,臂序本身
    不成为系统偏差。随机不带种子——它要的就是不可预测,而这批数据的可复现性由
    题号/档位/轮次那三维负责,不由臂序负责。

    **意图契约每题只算一次**(`_IntentCache`,键取 `question_key`——英文题面有
    自己的题号 `<key>-en`,所以 §5.4 说的 `question_key + lang` 已经被这一个键
    覆盖),两臂共用同一份;同一性由 `ab_contract_digest` 当场比一次(§5.5-3)。
    契约固定用**参照臂**那个 repo 去算:契约是 corpus-blind 且与反思策略无关的,
    固定一侧是为了不让「契约用哪个 repo」变成一个会随字典迭代顺序漂移的隐藏不
    确定量(与 `_precompute_intents` 同一条口径)。参照臂是 `--arms` 里写的第一
    条,不是「legacy 那一条」——二维化之后一批可以压根没有 legacy 臂。

    只跑一条臂时(`--only-policy` 或单臂 `--arms`),这一行的 `paired` 是
    `False`:它进单臂基线表,不进配对差值表(§4.2)。

    `arms` / `reference_arm` / `event_dir` 留 `None` ⇒ 就地从 `args` 推
    (`ab_arms(args)`、`arms[0]`、`rig_event_log_dir(args.out_dir)`)。真跑路径
    (`_run_ab` → `_ab_loop`)一律**显式传**,推导只是给直接调这一层的调用方
    (用例)一条与 `args` 同源的省略写法——两条路读的是同一个 `ab_arms`,不会
    因为一个默认值分叉出第二套臂解析。
    """
    from app.eval.reflect_ab import assert_arm_matches_evidence, gold_for, mark_paired

    fact = facts[unit["corpus_cell"]]
    unit_arms = list(arms) if arms is not None else ab_arms(args)
    reference_arm = reference_arm or unit_arms[0]
    if event_dir is None:
        event_dir = Path(rig_event_log_dir(args.out_dir))
    # **本地一份再洗**:`arms` 是整批共用的那一份,原地 shuffle 会让并发中的别的
    # 单元看到一个正在被改的列表(而它同时还是进度分母与参照臂的来源)。
    unit_arms = list(unit_arms)
    random.shuffle(unit_arms)
    intent_repo = repos_by_arm[reference_arm]

    def _contract() -> dict | None:
        return intents.get(
            intent_repo, intent_repo.settings, unit, actor_id=actor_id,
            notebook=fact["notebook"],
        )

    digest = ab_contract_digest(_contract())
    scope_ids, scope_failed = _resolve_item_scope(args, unit, fact)
    rows: list[dict] = []
    reasons: dict[tuple[str, str], str] = {}
    call_rows: dict[str, list[dict]] = {}
    if scope_failed:
        # 声明了范围却解析不到唯一匹配:整个单元作废,不退化成一次不设限的全库
        # 检索(codex #700 R3 P2 的同一条口径)。两臂都不跑,但**每条臂各落一行
        # `status=failed`**——直接 `return` 会让这个单元从数据集里凭空消失
        # (codex 质量评审 P2-4)。
        runner.say(
            "ab scope unresolved",
            f"{unit['question_key']} {unit['corpus_cell']} "
            f"titles={list(unit.get('scope_source_titles') or ())}"
            " -> 两臂各落一行 status=failed reason=scope_unresolved",
        )
        for arm in unit_arms:
            rows.append(_ab_failed_row(
                unit, arm=arm[0], optimization=arm[1], fact=fact,
                model_contract=model_contract,
                # 压根没开跑 ⇒ `run_wall_ms` 是 `None`,不是 0
                # (见 `stamp_run_wall_ms`)。
                run_wall_ms=None,
            ))
            reasons[arm] = "scope_unresolved"
    else:
        for arm in unit_arms:
            # **在臂的循环里各自取一次再比**(codex 规格评审 P2-3):在循环外
            # 取一次、循环内拿同一个对象比自己的短码,那条断言恒真,而它要挡的
            # 恰恰是「哪天有人把 `intents.get` 挪进臂的循环」——挪进来之后数据
            # 照样出得来,两臂却已经在比两件不同的事了。
            contract = _contract()
            if ab_contract_digest(contract) != digest:
                raise RuntimeError(
                    f"{unit['question_key']}: 两臂拿到的意图契约不是同一份"
                    "(§5.5-3)。这道题整题作废,先修再跑"
                )
            row, failure, calls = _run_ab_arm(
                unit, arm=arm, fact=fact, repo=repos_by_arm[arm],
                actor_id=actor_id, contract=contract, concurrency=concurrency,
                scope_ids=scope_ids,
                gold=gold_for(gold_by_key, unit["question_key"]),
                model_contract=model_contract, log_dir=log_dir,
                event_dir=event_dir, clock=clock,
                out_dir=runner.out_dir, database_url=args.database_url,
                cancel_event=cancel_event, llm_log_enabled=llm_log_enabled,
                deadline=deadline,
            )
            rows.append(row)
            if calls:
                call_rows.setdefault(arm_label(*arm), []).extend(calls)
            if failure:
                # 一条臂崩了不作废另一条:同题同档的另一半仍然是一份有效读数,
                # 而「哪一侧更容易崩」本身就是要量的东西之一。
                reasons[arm] = failure
                # 预算掐断在**人看的通道**上也不叫 FAILED(评审 P2-4):
                # `--max-wall-minutes` 的批到点时,终端曾刷出 N 行
                # `ab failed ... FAILED cancelled` 紧接一句「整批墙钟预算到点」
                # ——与 §10 M5-4「cancelled 单列成删失、不混进失败率」在同一屏里
                # 自相矛盾,操作者据此判断「这批模型全崩了」。JSONL 行与
                # `ab-runs.log` 的 `status=cancelled` 本来就是对的,错的只有这一
                # 条 say,所以只给它换一个独立标签与措辞。
                cancelled = failure == "cancelled"
                runner.say(
                    "ab cancelled" if cancelled else "ab failed",
                    f"{unit['question_key']} {unit['corpus_cell']} "
                    f"{format_arm(*arm)} "
                    f"{unit['effort']} r{unit['repeat']} "
                    + ("CANCELLED 预算到点(删失观察,不计失败率)"
                       if cancelled else f"FAILED {failure}"),
                )
    mark_paired(rows)
    with state["lock"]:
        for label, calls in call_rows.items():
            # per-call 表与投影行在**同一把锁**里写:两者是同一份跑批账目的两半,
            # 分开加锁会让「行写了、call 没写」出现在一次 Ctrl-C 上。
            _write_call_rows(runner.out_dir, label, calls)
        for row, arm in zip(rows, unit_arms):
            state["index"] += 1
            rows_handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )
            reason = reasons.get(arm, "")
            line = (
                f"{state['index']:04d}/{state['total']} {unit['question_key']} "
                f"{unit['corpus_cell']} {format_arm(*arm)} {unit['effort']} "
                f"r{unit['repeat']} {row['latency_ms_total']}ms "
                f"run_wall_ms={row['run_wall_ms']} "
                f"reflect_turns={row['reflect_turns']} "
                f"termination={row['termination_reason']} "
                f"answer_chars={row['answer_chars']} paired={row['paired']}"
                + (f" status={row['status']} reason={reason}" if reason else "")
            )
            log.write(line + "\n")
            runner.say("ab done", line)
            if reason:
                # 没跑成,没有协议证据可对——不参与「声明的臂 vs 轨迹证据」核对,
                # 也不该消费掉这条臂的「第一个样本」名额。`reason == "cancelled"`
                # (T-EX8 整批墙钟预算掐断)**不**计进 `state["failed"]`:那是一次
                # 删失观察,不是一次真实失败——混进失败率会让预算掐断看起来像
                # 「这一批模型全崩了」(§10 M5-4)。真实失败仍计入,整批因此以
                # 非零退出码收尾(codex #700 R9 P2 的同一条口径)。
                if reason != "cancelled":
                    state["failed"] += 1
                continue
            if arm not in state["verified"]:
                # **每条臂的第一个 run 之后**就把「声明的臂」与「轨迹里真的发生
                # 了什么」对上一次(§5.5-1)。整批跑完再发现 v2 那半其实跑的是
                # legacy,代价是几百次模型调用。
                # 「第一个样本」的名额按 `(policy, optimization)` 记:只按 policy
                # 记的话,`v2:off` 验过之后 `v2:prefix_snapshot` 就再也不验了。
                # 第二维的对号在 `run_ab_once` 里事前做(轨迹反推不出它)。
                assert_arm_matches_evidence(arm[0], row["policy_version"])
                state["verified"].add(arm)
        rows_handle.flush()
        log.flush()


def _ab_section_lookups(
    database_url: str, gold: Any, anchors: Sequence[Any],
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """A 格锚点解析要用的两张查询表 `(element_sections, chunk_sections)`。

    **B 格不查库**(codex 质量评审 P3):`gold_sources` 那条路走的是
    `source_titles`,它已经在 `_ab_corpus_facts` 里一次点清了;为它每个 run 再
    往测试库打两条 `IN (...)` 查询,查出来的东西一次也不会被读。没有 gold 的题
    同理——`count_anchors_on_gold` 对它恒返回 `None`。
    """
    if gold is None or not getattr(gold, "gold_section_path", ()):
        return None, None
    element_ids = [
        str(getattr(anchor, "element_id", "") or "") for anchor in anchors
    ]
    # chunk 那一跳只在锚点自己没带 `location_label` 时才用得上(见
    # `reflect_ab._anchor_section`),所以只把那几个 id 递进去。
    chunk_ids = [
        str(getattr(anchor, "object_id", "") or "")
        for anchor in anchors
        if not str(getattr(anchor, "element_id", "") or "")
        and not str(getattr(anchor, "location_label", "") or "")
        and str(getattr(anchor, "object_type", "") or "") == "chunk"
    ]
    return (
        _ab_element_sections(database_url, element_ids),
        _ab_chunk_sections(database_url, chunk_ids),
    )


def _run_ab_arm(
    unit: dict, *, arm: tuple[str, str], fact: dict, repo: Any,
    actor_id: str, contract: dict | None, concurrency: int,
    scope_ids: Sequence[str] | None, gold: Any, model_contract: str | None,
    log_dir: Path, event_dir: Path, clock: Any, out_dir: Path,
    database_url: str,
    cancel_event: Any = None, llm_log_enabled: bool = True,
    deadline: float | None = None,
) -> tuple[dict, str, list[dict]]:
    """一条臂的一个 run。返回 `(投影行, 失败原因类名或空串, per-call 行)`。

    `arm` 是 `(policy, optimization)` 这一对;`repo` 就是这条臂的那一个
    (`repos_by_arm[arm]`)——臂由 repo 承载,见 `run_ab_once` 的说明。

    单个 run 崩掉**不往上抛**:它被投影成一行 `status="failed"`,失败原因回给
    调用方记日志(与 `search` 同口径,理由见 `_ab_failed_row`)。往上抛的只有
    `AskCancelled`——那是整批收摊的信号,不是这一个 run 的失败。失败前捕获到的
    轨迹步照常带进那一行(codex #700 R13 P2),`run_wall_ms` 也照常记:它崩在半
    路,但确实占了这么久的墙钟。

    **例外(T-EX8)**:`deadline` 非空且已经到点时接住 `AskCancelled`,落一行
    `status="cancelled"` 而不是继续往上抛。判据是墙钟(`time.monotonic() >=
    deadline`),不是「谁调用了 `cancel_event.set()`」——同一个 `cancel_event`
    也被 `_ab_run_batch` 的既有异常收摊路复用(§5.5 硬断言不成立 /
    `KeyboardInterrupt`),那两条路**没有**给 `deadline`(`_run_ab_unit` 只在
    T-EX8 的批预算路上传非 `None` 的 `deadline`),所以这条分支只在预算到点的
    场景下成立,不改变既有的「先修再跑整批」级联(`--max-wall-minutes` 不给时
    `deadline` 恒 `None`,这条分支永远走不到,行为逐字不变)。

    `clock` 是**墙钟**(`datetime.now`),不是 `time.monotonic`:成本三键靠 LLM
    日志的时间窗切片归因(§7.2),而日志里那个 `ts` 是
    `datetime.now().isoformat()` 写的,两边必须是同一个钟。`latency_ms_total`
    与 `run_wall_ms` 另外用 `time.monotonic` 掐——它们量的是时长,不该受系统时间
    调整影响。

    **per-call 行只在串行下按 run 归因**(第三个返回值,并发下恒空):run→call
    的归因靠同一份时间窗/偏移,并发下窗口重叠。并发那一批的 per-call 表由
    `_ab_loop` 收尾时整批一次出,run 级标签全 unknown。
    """
    from app.eval.reflect_ab import project_ab_run
    from app.services.cancellation import AskCancelled

    item = dict(unit)
    steps: list[dict] = []
    raw_steps: list[dict] = []

    def _on_trace(step: Any) -> None:
        steps.append(trace_step_row(step))
        raw_steps.append(raw_trace_step_row(step))

    # 这个 run 开跑之前的日志字节位置:结束后只读它到 EOF 那一段(见
    # `_ab_llm_offsets`)。并发下成本三键恒 unknown,那一份 glob 也就省掉。
    serial = concurrency <= 1
    offsets = _ab_llm_offsets(log_dir) if serial else {}
    event_offsets = (
        _ab_llm_offsets(event_dir, glob=RIG_EVENT_LOG_GLOB) if serial else {}
    )

    def _calls() -> list[dict]:
        if not serial:
            return []
        return _rig_call_rows(
            log_dir, event_dir, llm_offsets=offsets,
            event_offsets=event_offsets,
            tags=_rig_call_tags(unit, arm, attributed=True),
        )

    started_at = clock()
    started = time.monotonic()
    try:
        response = run_ab_once(
            repo, notebook=fact["notebook"], item=item, arm=arm[0],
            optimization=arm[1],
            contract=contract, on_trace=_on_trace,
            cancel_event=cancel_event or threading.Event(), actor_id=actor_id,
            scope_source_ids=scope_ids,
        )
    except AskCancelled:
        if deadline is not None and time.monotonic() >= deadline:
            # T-EX8:预算到点唤醒的在途单元——落一行 `status="cancelled"`
            # (删失观察),不往上抛。`reason="cancelled"` 让调用方(`_run_ab_unit`)
            # 既跳过 `assert_arm_matches_evidence`(没有协议证据可对,与失败行同
            # 口径),又不把它计进「N 个 run FAILED」——预算掐断是预期收尾,不是
            # 一次真实失败(§10 M5-4「cancelled 单列成删失,不混进失败率」)。
            return _ab_failed_row(
                unit, arm=arm[0], optimization=arm[1], fact=fact,
                model_contract=model_contract,
                steps=steps, has_intent_contract=contract is not None,
                run_wall_ms=round((time.monotonic() - started) * 1000),
                status="cancelled",
            ), "cancelled", _calls()
        raise
    except Exception as exc:  # noqa: BLE001 — 单个 run 失败要隔离
        return _ab_failed_row(
            unit, arm=arm[0], optimization=arm[1], fact=fact,
            model_contract=model_contract,
            steps=steps, has_intent_contract=contract is not None,
            run_wall_ms=round((time.monotonic() - started) * 1000),
        ), type(exc).__name__, _calls()
    latency_ms = round((time.monotonic() - started) * 1000)
    ended_at = clock()
    anchors = list(getattr(response, "anchors", None) or ())
    element_sections, chunk_sections = _ab_section_lookups(
        database_url, gold, anchors,
    )
    row = project_ab_run(
        steps, arm=arm[0], effort=unit["effort"], repeat=int(unit["repeat"]),
        optimization=arm[1],
        question_key=unit["question_key"], corpus_cell=unit["corpus_cell"],
        answer=str(getattr(response, "answer", "") or ""),
        citations=list(getattr(response, "citations", None) or ()),
        anchors=anchors,
        coverage_complete=_ab_coverage_complete(response),
        kg_in_scope=fact["kg_in_scope"], sources_count=fact["sources"],
        has_intent_contract=contract is not None,
        notebook_id=fact["notebook"], gold=gold,
        element_sections=element_sections, chunk_sections=chunk_sections,
        source_titles=fact["source_titles"],
        # 题目声明了范围 ⇒ 允许集合就是解析出来的那几个;没声明 ⇒ 全库
        # (`citations_out_of_scope` 的正确值在两种情况下都恒为 0,§9-1)。
        allowed_source_ids=(
            frozenset(scope_ids) if scope_ids
            else frozenset(fact["source_titles"])
        ),
        usage=_ab_usage_for_window(
            log_dir, started_at, ended_at, concurrency=concurrency,
            offsets=offsets, llm_log_enabled=llm_log_enabled,
        ),
        latency_ms_total=latency_ms,
        model_contract=model_contract,
        corpus_signature=fact["corpus_signature"],
    )
    stamp_run_wall_ms(row, latency_ms)
    _write_ab_raw(out_dir, arm, item, response, raw_steps, contract)
    return row, "", _calls()


def _write_ab_raw(
    out_dir: Path, arm: tuple[str, str], item: dict, response: Any,
    raw_steps: Sequence[dict], contract: dict | None,
) -> None:
    """`.local` 的一份 run 存档。**不进数据集/不进仓库**(§7.3)。

    路径按 `arm_label` 分目录(两条 v2 臂不能撞进同一个 `raw/v2/`),存档正文里
    的 `arm` 仍是**命令行写法**(`format_arm`),人工抽样时读起来与 `--arms`
    里写的那一串逐字相同。
    """
    path = _ab_raw_path(out_dir, arm_label(*arm), item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            ab_raw_payload(item, format_arm(*arm), response, raw_steps,
                           contract),
            ensure_ascii=False, indent=2, sort_keys=True, default=str,
        ),
        encoding="utf-8",
    )


def cmd_export(args: argparse.Namespace, runner: Runner) -> int:
    out = runner.out_dir / "t0-dataset.jsonl"
    export_out = runner.out_dir / "ask-runs.jsonl"
    runner.say(
        "export ask traces",
        f"scripts/export_reasoning_traces.py --database-url <test-db> "
        f"--reports --out {export_out}",
    )
    if not runner.dry_run:
        from export_reasoning_traces import main as export_main

        export_main([
            "--database-url", args.database_url,
            "--reports", "--out", str(export_out),
        ])
    parts = [export_out] + sorted(runner.out_dir.glob("report-trace-*.jsonl"))
    runner.say("merge", " + ".join(str(part) for part in parts) + f" -> {out}")
    if runner.dry_run:
        return 0
    _merge_export_parts(parts, out)
    return 0


def _merge_export_parts(parts: Sequence[Path], out: Path) -> None:
    """把 `parts` 按 `merge_key` 去重后写成一份 JSONL(§README「每节一行」)。

    `--reports` 从库里导出的结果级行(`export_reasoning_traces.py --reports`,
    `project_report_section` 直出,没有 `trace_steps` 键)与 rig 自己写的
    `report-trace-*.jsonl`(同一份 `report_id`+`section_index` 已经按
    `merge_key` 把轨迹合成过,带 `trace_steps` 键)会撞同一个 `merge_key`——
    原来逐字节拼接文件会把它们各写一行,一份 3 节的报告就报成 6 个 run
    (codex #700 R2 P2,同 README 479–481 的不变量)。没有 `merge_key` 的行
    (纯 Ask run)不会跟别的行撞号,原样按原有顺序透传。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    passthrough: list[dict] = []
    for part in parts:
        if not part.exists():
            continue
        for line in part.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = row.get("merge_key")
            if not key:
                passthrough.append(row)
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = row
                order.append(key)
                continue
            # 撞号:带轨迹的那行(project_run 出的 `trace_steps` 键)赢——
            # 结果级独行永远输给已经合并过轨迹的版本,而不是各写一行。
            if "trace_steps" not in existing and "trace_steps" in row:
                merged[key] = row
    with out.open("w", encoding="utf-8") as handle:
        for row in passthrough:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        for key in order:
            handle.write(
                json.dumps(merged[key], ensure_ascii=False, sort_keys=True) + "\n"
            )


def _process_identity(pid: int) -> str | None:
    """`ps` 报的「启动时刻 + 命令行」——PID 之外再认一次人(codex #700 R11 P2)。
    进程不存在返回 None。macOS/Linux 的 `ps -o lstart=,command=` 同形。"""
    if not pid:
        return None
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=,command=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _backend_process_is_ours(
    pid: int, expected_identity: str | None,
) -> tuple[bool, str]:
    """state 里的 PID 现在指向的还是我们起的那台后端吗?

    后端已退出而 PID 被别的进程复用时,照 PID 发 SIGTERM/SIGKILL 会打到用户的
    无关进程上(codex #700 R11 P2)。有 seed/restart 记下的身份就逐字比;老 state
    没记身份时至少要求命令行是 uvicorn 起的 `app.main:app`。
    """
    identity = _process_identity(pid)
    if identity is None:
        return False, f"pid={pid} 已不存在"
    if expected_identity:
        if identity == expected_identity:
            return True, ""
        return False, f"pid={pid} 已被别的进程复用,不发信号"
    if "uvicorn" in identity and "app.main:app" in identity:
        return True, ""
    return False, f"pid={pid} 不是 rig 起的 uvicorn 后端,不发信号"


def _stop_backend_gracefully(
    runner: Runner, pid: int, *, timeout: float = 15.0,
    expected_identity: str | None = None,
) -> bool:
    """SIGTERM,给它 `timeout` 秒退出;超时了才 SIGKILL(§2.3「优雅停止」)。
    发信号之前先核对进程身份(`_backend_process_is_ours`);不是我们的进程就
    什么都不发,返回 False。

    轮询用 `os.kill(pid, 0)`(不发信号,只探测进程还在不在)——PID 不存在时
    这一探测本身就抛 `OSError`,拿来当"已经退出"的判据,不用额外 waitpid
    (uvicorn 子进程可能已经被系统回收,不一定还是本进程的直接子进程)。
    """
    ours, why = _backend_process_is_ours(pid, expected_identity)
    if not ours:
        runner.say("stop backend skipped", why)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        runner.say("stop backend failed", str(exc))
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
        runner.say("stop backend", f"pid={pid} 超时 {timeout}s 未退出,已 SIGKILL")
    except OSError:
        pass
    return True


def cmd_restart(args: argparse.Namespace, runner: Runner) -> int:
    """优雅停掉 state 里记的后端,按 `--policy` 重起同一份配置(§2.3)。

    端口 / DB / storage-dir / env-file 从 state 取(seed 落的那份),不是这次
    调用的命令行默认值——这样 `restart --policy v2` 不需要重复 `seed` 用过的
    全部参数就能起同一台后端。重启复用 `_start_backend`,因此复用它的就绪
    判据(`/api/ready` 的 `{"ready": true}`),不是「端口能连上」就算数。
    """
    state = _read_raw_state(runner.state_path())
    pid = int(state.get("backend_pid") or 0)
    port = int(state.get("port") or args.port)
    database_url = str(state.get("database_url") or args.database_url)
    storage_dir = str(state.get("storage_dir") or args.storage_dir)
    env_file = state.get("env_file", args.env_file)
    new_flag = backend_env(args, args.policy)["REASONING_REFLECT_V2_ENABLED"]

    runner.say(
        "stop backend",
        f"pid={pid or '<unknown>'} policy {state.get('policy', '<unknown>')} "
        f"-> {args.policy}",
    )
    runner.say(
        "start backend",
        f"port={port} database_url={_redact_url(database_url)} "
        f"REASONING_REFLECT_V2_ENABLED={new_flag}",
    )
    if runner.dry_run:
        return 0

    if pid:
        _stop_backend_gracefully(
            runner, pid, expected_identity=state.get("backend_identity"),
        )

    restart_args = argparse.Namespace(**vars(args))
    restart_args.port = port
    restart_args.database_url = database_url
    restart_args.storage_dir = storage_dir
    restart_args.env_file = env_file
    restart_args.base_url = f"http://127.0.0.1:{port}"
    backend = _start_backend(restart_args, args.policy)

    state.update({
        "backend_pid": backend.pid,
        "backend_identity": _process_identity(backend.pid),
        "policy": args.policy, "port": port,
        "database_url": database_url, "storage_dir": storage_dir,
        "env_file": env_file,
    })
    runner.save_state(state)
    runner.say("restarted", f"pid={backend.pid} policy={args.policy}")
    return 0


def _teardown_targets_this_run(
    args: argparse.Namespace, state: dict | None = None,
) -> bool:
    """`--db-name` 说的那个库,真的是这次 rig 写进去的那个吗?(codex #700 R1 P1)

    真源是 `rig-state.json` 里 `seed`/`restart` 落的 `database_url`——**不是**
    这次调用自己的 `--database-url`:它有默认值(`postgresql://127.0.0.1:5432/
    {DEFAULT_TEST_DB}`),一次跑在别的后端上的 rig(比如 SQLite 冒烟,或者
    `--out-dir` 指向一份从没 seed 过、state 里根本没有这个键的目录)照样会让
    `--database-url` 落到那个"看起来像 PG"的默认值上,而 teardown 原先只看
    这个默认值,不核对它是不是这次真写过的那个库。

    只有 state 记的 URL 与这次 `--database-url` **逐字相等**、而且是 PG、
    库名等于 `--db-name` 时才继续(到 `cmd_teardown` 里再做一次针对
    `--admin-url` 的服务器身份核对)。state 缺这个键、或者跟这次的
    `--database-url` 对不上号(哪怕只是因为都退回了各自的默认值,一个是
    SQLite 一个是 PG),一律拒绝 DROP——不拿命令行默认值去猜"大概率是同一个
    库"。
    """
    state = state or {}
    state_url = str(state.get("database_url") or "")
    if not state_url or state_url != str(args.database_url or ""):
        return False
    if not state_url.startswith(("postgres://", "postgresql://")):
        return False
    return state_url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] == args.db_name


def _pg_endpoint(url: str) -> tuple[str | None, int]:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return parts.hostname, parts.port or 5432


def _same_endpoint_literal(admin_url: str, database_url: str) -> tuple[bool, str]:
    """`_admin_targets_same_server` 不用连库就能判定的那一半:两个 URL 从字面上
    是不是同一个 host:port。seed 在建库之前也用它(codex #700 R6 P2):库名一致
    但服务器不同,CREATE DATABASE 会落在 A 机、扩展/迁移/播种落在 B 机——B 机若
    恰好有同名库,写进去的是别人的数据。"""
    admin_endpoint = _pg_endpoint(admin_url)
    db_endpoint = _pg_endpoint(database_url)
    if admin_endpoint != db_endpoint:
        return False, (
            f"--admin-url 指向 {admin_endpoint[0]}:{admin_endpoint[1]},"
            f"--database-url 指向 {db_endpoint[0]}:{db_endpoint[1]} —— 不是"
            "同一台服务器"
        )
    return True, ""


def _admin_targets_same_server(
    admin_url: str, database_url: str, *, identity: Any = None,
) -> tuple[bool, str]:
    """`--admin-url`(维护连接,真正执行 DROP 的那一个)与 `--database-url`
    是不是同一台 PG 服务器?(codex #700 R1 P1)

    `_teardown_targets_this_run` 只核对了 `--database-url` 与 state 记的是否
    一致,从没检查过用来执行 DROP 的 `--admin-url` 是不是同一台服务器——两者
    是两个独立的命令行参数,一个打错端口/host 就会让 teardown 在**另一台**
    服务器上删一个同名的库。先比 URL 本身的 host:port,再各连一次比服务器身份
    (`_live_server_identity`)——host:port 相同但两条连接其实落到不同服务器时
    这条能拦下来;同一台服务器经端口映射进来不会被误拒。`identity` 只给用例
    注入替身。
    """
    ok, reason = _same_endpoint_literal(admin_url, database_url)
    if not ok:
        return False, reason + ",拒绝 DROP"
    probe = identity or _live_server_identity
    admin_identity, db_identity = _comparable_identity(
        probe(admin_url), probe(database_url)
    )
    if admin_identity != db_identity:
        return False, (
            f"--admin-url 连到的服务器身份是 {admin_identity},--database-url "
            f"连到的是 {db_identity} —— 不是同一台服务器,拒绝 DROP"
        )
    return True, ""


def _comparable_identity(a: Mapping, b: Mapping) -> tuple[tuple, tuple]:
    """两条连接的身份按**同一把尺**比(codex #700 R9 P2):admin 是超级用户、
    database 角色读不到 `pg_control_system()` 时,一边有 system_identifier、
    一边没有,直接比会把同一台服务器判成两台。任一边缺 system_identifier 就两边
    都退回服务端 addr:port 比。"""
    if a.get("system_identifier") is not None and b.get("system_identifier") is not None:
        return (("system_identifier", a["system_identifier"]),
                ("system_identifier", b["system_identifier"]))
    return (("server_endpoint", *a["server_endpoint"]),
            ("server_endpoint", *b["server_endpoint"]))


def _live_server_identity(url: str) -> dict:
    """一条连接落在哪台 PG 上:`pg_control_system().system_identifier` 是集群
    初始化时生成的 64 位标识,与客户端从哪个端口/隧道进来无关(codex #700 R8
    P2:此前拿 `inet_server_port()` 与 URL 端口比,Docker 端口映射或 SSH 隧道
    `localhost:15432 → server:5432` 会被误判成两台机器)。角色读不到控制文件
    时退回服务端视角的 `(inet_server_addr, inet_server_port)`——同样是服务端
    自报的值,两条连接可比。两样都取回来,由 `_comparable_identity` 决定用哪把
    尺(两边都有 system_identifier 才用它)。"""
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        endpoint = conn.execute(
            "SELECT inet_server_addr()::text, inet_server_port()"
        ).fetchone()
        try:
            row = conn.execute(
                "SELECT system_identifier FROM pg_control_system()"
            ).fetchone()
            system_identifier: int | None = int(row[0])
        except Exception:  # noqa: BLE001 — 权限不够等:只剩服务端地址视角
            system_identifier = None
    return {"system_identifier": system_identifier,
            "server_endpoint": (endpoint[0], endpoint[1])}


def cmd_teardown(args: argparse.Namespace, runner: Runner) -> int:
    state = runner.load_state()
    pid = int(state.get("backend_pid") or 0)
    runner.say("stop backend", f"port {args.port} pid={pid or '<unknown>'}")
    drop = _teardown_targets_this_run(args, state)
    runner.say(
        "drop database" if drop else "skip drop database",
        f"{_redact_url(args.admin_url)} :: DROP DATABASE {args.db_name}" if drop
        else "state 记的库跟这次 --database-url 对不上号(或不是 PG),不碰任何库",
    )
    if runner.dry_run:
        return 0
    if pid:
        # 先收后端再删库:还连着的会话会让 DROP 走 FORCE 去踢连接,而被踢掉的
        # uvicorn 之后每个请求都在报连接错——留一个这样的进程占着端口比留一个
        # 库更烦人。发信号前核对进程身份,停完就把 PID 从 state 里清掉,重复
        # teardown 不会再对一个可能被复用的 PID 发信号(codex #700 R11 P2)。
        _stop_backend_gracefully(
            runner, pid, expected_identity=state.get("backend_identity"),
        )
        state["backend_pid"] = 0
        state["backend_identity"] = None
        runner.save_state(state)
    if not drop:
        return 0
    ok, reason = _admin_targets_same_server(args.admin_url, args.database_url)
    if not ok:
        runner.say("skip drop database", reason)
        print(f"ERROR: {reason}", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(args.admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{args.db_name}" WITH (FORCE)')
    return 0


# --- `prefix-probe`(E1 前缀敏感性探针;计划 §3 T-EX4)------------------------

#: 预热调用的正文与标记(design Q4:「用不同正文与不同前缀」)。`tier="__warmup__"`
#: 与 `block_index=-1` 不会与 `probe_plan` 产出的任何真实序列的
#: `(tier, block_index)` 撞上,`build_marker_pair` 的输入因此天然与主批不相交。
_PREFIX_PROBE_WARMUP_TIER = "__warmup__"

#: 整批开头的预热调用次数(design Q4 原文就是**一次**)。写成常量而不是字面
#: `+ 1`:dry-run 的调用数估计、`is_warmup` 单列的行数、以及请求上界三处读的
#: 必须是同一个数(评审 P3-14)。
PREFIX_PROBE_WARMUP_CALLS = 1

#: 预热行落哪一份 jsonl(`probe-warmup.jsonl`)。**不是一条臂**:它与
#: `ARM_STABLE`/`ARM_DISTURBED` 并列做 `rows_by_label` 的键,只是为了让「预热
#: 单列、不进任何统计」这条 design §9.1 的要求在落盘这一层也成立。
_PREFIX_PROBE_WARMUP_LABEL = "warmup"


def _prefix_probe_warmup_messages() -> list[dict]:
    from app.eval.reflect_prefix_probe import PROBE_OUTPUT_INSTRUCTION

    return [{
        "role": "user",
        "content": (
            "[warmup] 连接预热,正文与后续探针格无关,不参与任何统计。\n\n"
            + PROBE_OUTPUT_INSTRUCTION
        ),
    }]


def _prefix_probe_process_env(args: argparse.Namespace) -> dict[str, str]:
    """E1 的进程环境:只圈日志隔离,**不碰 `DATABASE_URL`**。

    这条命令结构上不连库(见 `cmd_prefix_probe` 对显式 `--database-url` 的响亮
    拒绝),往 `DATABASE_URL` 里写任何占位值都只是徒增一次 `Settings()` 校验
    失败的风险,没有任何收益(`normalize_database_url("")` 直接抛)。与
    `_rig_process_env` 共用同一对日志隔离助手(`rig_llm_log_path`/
    `rig_event_log_dir`),不复用它本身——那个函数的 `database_url` 是必填
    参数。
    """
    env = {
        "LLM_LOG_PATH": rig_llm_log_path(args.out_dir),
        "EVENT_LOG_DIR": rig_event_log_dir(args.out_dir),
    }
    if args.env_file:
        env["SILICON_NOTEBOOK_ENV_FILE"] = str(Path(args.env_file).expanduser())
    return env


def _prefix_probe_set_env(env: Mapping[str, str]) -> None:
    """把 E1 的进程环境写进 `os.environ`。

    单独一个函数**只为可注入**(评审 P2-8):`os.environ.update` 写下去的值在
    进程里是永久的,而 pytest 的 worker 就是那个进程——四条非 dry-run 用例跑完
    之后 `LLM_LOG_PATH` / `EVENT_LOG_DIR` 仍指向已被清掉的 `tmp_path`,同 worker
    里后续任何构造 `Settings()` 并写日志的用例都被牵着走,`-n 4` 下受影响的是
    哪几条还随分片漂。用例把这一步换成 `monkeypatch.setenv`,自带还原。
    """
    os.environ.update(env)


def _prefix_probe_finish_reason(raw: object) -> str | None:
    """`call_stats["finish_reason"]` → 行上那一列,收成短码或 `None`。

    `None` 的语义是「**provider 没说**」,与 `app/core/llm.py` ok 出口传的
    `finish_reason or ""`(那句注释明写 “servers may omit finish_reason
    entirely”)同口径。

    两条都必须折(评审 F1 / P1-1):

    * **空串**——`_is_short_code("")` 为 False,原样写进行会让
      `assert_probe_row_closed` 抛 `ValueError`。这不是操作失误,是一类真实
      OpenAI 兼容端点的**正常返回**:一整批 96 格会在第一格(预热格)全灭,
      连一行都产不出来;
    * **非短码形状**——厂商自定义的 `finish_reason` 带空格、中文或超过 64
      字符时同样抛。一次跑批不该因为一列诊断性的自由文本而报废,而把自由文本
      原样落进 JSONL 又正是这份闭集要挡的事,所以折 `None`。
    """
    if not isinstance(raw, str):
        # `None` 与任何非字符串(provider 回了个 dict/int)一律 unknown。
        return None
    from app.domain.reasoning_trace_stats import _is_short_code

    return raw if _is_short_code(raw) else None


def _prefix_probe_sample_path(args: argparse.Namespace) -> Path:
    if args.sample_file:
        return Path(args.sample_file).expanduser()
    return ROOT / "backend" / "app" / "eval" / "reflect_t0" / "prefix_probe_sample.json"


def _prefix_probe_plan(args: argparse.Namespace) -> list[dict]:
    """这次要跑的**全部**探针调用,dry-run 与真跑共读同一份(与 `ab_plan`/
    `search_plan` 同一条纪律)。

    `--marker-variant` **不是** `probe_plan` 自己的参数(它只接受 `seed`)——
    非零 variant 时,在这里用 `build_marker_pair` 按同一组
    `(seed, tier, block_index, arm, call_index)` 重算一遍 head/tail 并原地
    替换。这是一个可以商榷的取舍:更干净的做法是让 `probe_plan` 自己接一个
    `marker_variant` 参数,把这段重算逻辑收回模块内部——本任务的红线是不改
    `backend/app/**`,所以重算逻辑留在这一层,见任务报告「上游接口不够用」。
    """
    from app.eval.reflect_prefix_probe import (
        DEFAULT_BLOCKS, SMOKE_BLOCKS, build_marker_pair, probe_plan,
    )

    blocks = SMOKE_BLOCKS if args.smoke else DEFAULT_BLOCKS
    plan = probe_plan(seed=args.seed, blocks=blocks)
    if args.marker_variant:
        for row in plan:
            head, tail = build_marker_pair(
                args.seed, row["tier"], row["block_index"], row["arm"],
                row["call_index"], marker_variant=args.marker_variant,
            )
            row["head"], row["tail"] = head, tail
    return plan


def _prefix_probe_call_estimate(plan_calls: int) -> str:
    """与 `_search_call_estimate`/`_ab_call_estimate` 同一口径的那一句:
    逻辑调用数与**请求上界**是两个数,不要混用(见两者各自的说明)。

    **预热那一格算进这两个数**(评审 P3-14):它是一次真实的、要付钱的
    `reasoning_agent` 调用,只是不进统计。少算它的后果是按配额规划的人拿到的
    上界比真实请求数小 `1 × REASONING_ATTEMPT_BUDGET`——全量批实发 97 次而不是
    96 次,上界 ≤194 而不是 ≤192。
    """
    total_calls = plan_calls + PREFIX_PROBE_WARMUP_CALLS
    return (
        f"{total_calls} 次逻辑调用({plan_calls} 格计划 + "
        f"{PREFIX_PROBE_WARMUP_CALLS} 预热);请求上界 ≤ "
        f"{total_calls * REASONING_ATTEMPT_BUDGET}"
        f"(重试预算 ×{REASONING_ATTEMPT_BUDGET};静默 fallback 另计,不留日志行)"
    )


def _prefix_probe_say_sequence(runner: Runner, plan: Sequence[Mapping]) -> None:
    """dry-run 的「序列顺序与区组」:计划前几行 + 每个 `(tier, block)` 的臂序。

    design §9.1「各序列内部保持连续,不插入并发负载」——这里打的顺序就是真跑
    会打的顺序,不是另一份摘要。

    臂序那几行按 `plan` 里 `(tier, block)` 的**首次出现顺序**打,不排序(评审
    P3-13):`sorted(...)` 会按 tier 的字典序打成 `long / medium / short`,而真跑
    按 `DEFAULT_TIERS` 走 `short / medium / long`——操作者拿这几行去对
    `probe-*.jsonl` 的前几行会对不上,而紧邻上方的 `plan row` 预览又是真顺序,
    同一段输出自相矛盾。首次出现顺序**就是**派发顺序,不是它的一份近似。
    """
    preview = list(plan[:4])
    for row in preview:
        runner.say(
            "plan row",
            f"tier={row['tier']} block={row['block_index']} arm={row['arm']} "
            f"call_index={row['call_index']} series={row['series_index']}",
        )
    if len(plan) > len(preview):
        runner.say("plan row", f"...(其余 {len(plan) - len(preview)} 行同形状)")
    order: dict[tuple[str, int], list[str]] = {}
    for row in plan:
        key = (row["tier"], row["block_index"])
        if row["call_index"] == 0:
            order.setdefault(key, []).append(row["arm"])
    for key in order:
        tier, block_index = key
        runner.say(
            "block arm order",
            f"tier={tier} block={block_index} order={'/'.join(order[key])}",
        )


def _prefix_probe_client(settings: Any) -> tuple[Any, Any]:
    """`(provider, client)`。拆成单独一个函数只是为了让用例能整体替身它
    (T-EX4 用例 (d)/(e)/(f)/(g))——真实构造照抄 `mrl_truncation.run_gold_eval`
    (计划 §1 M2):`RuntimeModelProvider(settings, EventLogger(settings,
    channel="events", per_user=True))`,取 `.chat("reasoning_agent")`(与
    reflect 同一个 workload/端点/thinking 配置)。
    """
    from app.core.event_logging import EventLogger
    from app.services.model_provider import RuntimeModelProvider

    provider = RuntimeModelProvider(
        settings, EventLogger(settings, channel="events", per_user=True))
    return provider, provider.chat("reasoning_agent")


def _prefix_probe_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_probe_summary_markdown(
    summary: Mapping, *, seed: int, smoke: bool, sample_path: Path,
    marker_variant: int,
) -> str:
    lines = [
        "# E1 前缀敏感性探针 · 摘要",
        "",
        "**这批只能支持『稳定前缀的时间收益』,不得报告命中率、不得报告省了多少 "
        "token、不得报告 provider 是否关闭了缓存**(design §0/§9.1)。",
        "",
        f"- seed: {seed}",
        f"- scale: {'smoke(48)' if smoke else 'full(96)'}",
        f"- sample: {sample_path}",
        f"- marker_variant: {marker_variant}",
        f"- verdict: **{summary['verdict']}**",
        f"- row_count_total: {summary['row_count_total']}",
        f"- warmup_row_count: {summary['warmup_row_count']}",
        f"- local_cache_exit_rows: {summary['local_cache_exit_rows']}",
        f"- failed_row_count: {summary['failed_row_count']}",
        f"- cached_tokens_observed: {summary['cached_tokens_observed']}",
        "",
        "## overall",
        f"- n_regions_paired: {summary['overall']['n_regions_paired']}",
        f"- median_wall_ms_delta: {summary['overall']['median_wall_ms_delta']}",
        f"- median_wall_ms_ratio: {summary['overall']['median_wall_ms_ratio']}",
        f"- consistency_ratio: {summary['overall']['consistency_ratio']}",
        f"- first_observation: {summary['overall']['first_observation']}",
        f"- repeat_observation: {summary['overall']['repeat_observation']}",
        "",
        "## by_tier",
    ]
    for tier in sorted(summary["by_tier"]):
        scope = summary["by_tier"][tier]
        lines.append(f"### {tier}")
        lines.append(f"- n_regions_paired: {scope['n_regions_paired']}")
        lines.append(f"- median_wall_ms_delta: {scope['median_wall_ms_delta']}")
        lines.append(f"- median_wall_ms_ratio: {scope['median_wall_ms_ratio']}")
        lines.append(f"- consistency_ratio: {scope['consistency_ratio']}")
    lines.append("")
    return "\n".join(lines)


def cmd_prefix_probe(args: argparse.Namespace, runner: Runner) -> int:
    """E1(前缀敏感性探针):**零数据库、零检索、零 reflect**,只调
    `reasoning_agent` workload 本身,量两臂标记稳定性的墙钟差(design §9.1)。

    与 `search`/`ab` 的分工:那两条命令的全部意义建在检索/合成上;E1 剥掉这
    一切,只留「同一个 workload、同一个端点、同一个 thinking 配置」上的一段
    固定正文,两臂只在头/尾标记的稳定性上不同(计划 M1/M2)。**新子命令**而
    不是 `search`/`ab` 上的开关:E1 不跑检索、不连库、不建 repo,挂进任何一条
    既有路都会让那条路多一条与语料/范围/意图全部无关的死分支(计划 M2)。

    **per-call 表(`calls-e1.jsonl`)与探针行的对齐规则**(评审 F13):这张表
    是**无标签**的(`tags=None`)——`CALL_TAG_KEYS` 是 A/B 的维度
    (`question_key`/`corpus_cell`/`effort`),E1 一个都没有,硬塞会写出三列
    恒 `None`。于是它与 `probe-*.jsonl` 没有共享键,只能**按顺序**对齐,而这条
    对齐今天是成立的:E1 全程串行(一次一格,不并发),`join_calls` 给出的
    `call_index` 稠密有序,所以第 0 行是**预热格**,其后第 i 行对应
    `_prefix_probe_plan(args)[i - PREFIX_PROBE_WARMUP_CALLS]`,也就是
    `probe-stable.jsonl` 与 `probe-disturbed.jsonl` 两份按 `series_index` /
    `call_index` 归并回计划序之后的第 i 格。预算到点提前停批时,per-call 表的
    行数按实发调用数收窄,前缀仍然对齐(停的是尾部)。**一旦 E1 改成并发派发,
    这条规则立刻失效**——那时要么给这张表加维度,要么把对齐写进行里。
    """
    if args.database_url_explicit:
        print(
            "ERROR: prefix-probe 不连数据库,不接受 --database-url"
            "(它照抄的多半是 ab/search 的命令行)",
            file=sys.stderr,
        )
        return 2
    if args.seed is None:
        print(
            "ERROR: prefix-probe 需要 --seed"
            "(design M4:E1 的区组随机必须有种子)",
            file=sys.stderr,
        )
        return 2
    problem = _wall_budget_problem(args.max_wall_minutes)
    if problem:
        # **dry-run 也拦**(与 `_ab_preflight` 同一条纪律):一份 `nan` 预算的
        # 计划预演出来看起来完全像一次正常的预演。
        print("ERROR: " + problem, file=sys.stderr)
        return 2

    plan = _prefix_probe_plan(args)
    sample_path = _prefix_probe_sample_path(args)

    runner.say(
        "target",
        "E1 前缀敏感性探针:零数据库、零检索、零 reflect,只调 reasoning_agent "
        "workload 本身(计划 M1/M2)",
    )
    runner.say("seed", str(args.seed))
    runner.say("scale", "smoke(48)" if args.smoke else "full(96)")
    runner.say("model calls (estimate)", _prefix_probe_call_estimate(len(plan)))
    runner.say(
        "timeout (per call)",
        f"{REASONING_TIMEOUT_SECONDS_DEFAULT}s(镜像 Settings()."
        "reasoning_timeout_seconds 的默认值,与 `ab` 那一行读同一个常量;"
        "真跑读的是构造出来的那一份,可能被 --env-file 覆盖)",
    )
    runner.say(
        "batch wall-clock budget",
        f"{args.max_wall_minutes} 分钟(到点停止派发,不补跑到矩阵齐全)"
        if args.max_wall_minutes is not None
        else "未设(默认行为:跑到计划结束)",
    )
    runner.say(
        "sample",
        f"{sample_path}"
        + ("(--sample-file)" if args.sample_file else "(仓库内默认 fixture)"),
    )
    runner.say("marker variant", str(args.marker_variant))
    runner.say(
        "warmup",
        "整批开头一次连接预热,正文与标记均与主批不同,单列 is_warmup=True 行,"
        "不进任何统计(design §9.1)",
    )
    _prefix_probe_say_sequence(runner, plan)
    runner.say(
        "out",
        f"{runner.out_dir}/probe-stable.jsonl + probe-disturbed.jsonl + "
        "probe-warmup.jsonl + probe-summary.{md,json} + manifest.json"
        "(+ manifests.jsonl 历史)+ calls-e1.jsonl(per-call 表)",
    )
    runner.say(
        "isolated logs",
        f"LLM_LOG_PATH={runner.out_dir}/llm/llm.jsonl、"
        f"EVENT_LOG_DIR={runner.out_dir}/events"
        "——不往机器共用的 .local/logs 写一个字节",
    )
    runner.say(
        "conclusion scope",
        "这批只能支持『稳定前缀的时间收益』,不得报告命中率、不得报告省了多少 "
        "token、不得报告 provider 是否关闭了缓存(design §0/§9.1)",
    )
    if runner.dry_run:
        return 0
    return _run_prefix_probe(args, runner, plan, sample_path)


def _prefix_probe_preflight(
    plan: Sequence[Mapping], sample: Mapping, args: argparse.Namespace,
) -> dict:
    """**首格之前**把这批要用到的纯计算全跑一遍,返回 manifest 事实。

    存在的理由是钱(评审 P1-1 第 3 条):`render_sample` 与
    `probe_manifest_facts` 各有一组响亮拒绝路径(样本地板不足、`tier_chars`
    非严格递增、样本档位缺失、`--sample-file` 指了一份形状不对的 JSON),它们
    全部只依赖参数与样本,与模型无关。不先算的话第一个抛出点在**预热格之后**
    ——那一格已经付过钱了,而一整批 96 格连一行都产不出来。

    三档正文按 `plan` 里 `(tier)` 的首次出现顺序各渲染一次(不是样本声明的
    全部档位:`plan` 没跑的档位不该让这批跑不起来)。返回值就是收尾要写的
    `facts`,不重算第二遍。

    `matrix` 补两个**额外 int 子键**(`reflect_manifest` 的 `matrix` 允许额外
    子键,`MANIFEST_KEYS` 闭集不用改):

    * `planned_runs`(评审 F3;三条通道同规则)—— 这批**计划**的格数。读
      manifest 的人不必自己把四个基数乘一遍,而一旦 `probe_plan` 的枚举维度
      变化(比如某档不满跑),乘法就对不上账,这一列正是为对账设的;
    * `marker_variant`(评审 F11)—— 不写的话,`--marker-variant 0` 与 `1` 跑
      出来的两批 manifest 逐字节相同(seed / `sample_digest` / 四个基数全一致),
      事后无法分辨哪批是哪个 variant,而 design §9.1「更换标记值重复验证」的
      全部意义就是把这两批对起来看。
    """
    from app.eval.reflect_prefix_probe import probe_manifest_facts, render_sample

    for tier in dict.fromkeys(row["tier"] for row in plan):
        render_sample(tier, sample)
    facts = dict(probe_manifest_facts(plan, sample, args.seed))
    matrix = dict(facts["matrix"])
    matrix["planned_runs"] = len(plan)
    matrix["marker_variant"] = int(args.marker_variant)
    facts["matrix"] = matrix
    return facts


def _prefix_probe_local_cache_exits(
    summary: Mapping, rows_by_label: Mapping[str, Sequence[Mapping]],
) -> tuple[int, int]:
    """`(主批, 预热)` 两个本地缓存出口计数。

    主批那个数**直接读 `summary["local_cache_exit_rows"]`**,不再由派发循环自己
    数一遍(评审 P3-16):两个独立算出来的数没有任何守卫钉它们相等,而 stderr
    上那句话恰恰宣称「该计数已单列进 probe-summary」。

    预热那一格要单独数:`summarize_probe` 的分桶第一步就把 `is_warmup` 行整块
    摘出去(design §9.1「预热成本单列」),所以它**不在**那一列里。而预热同样
    走 `bypass_cache=True`,它命中本地缓存出口一样是「结构上不应出现」的事实,
    一样要让整批以非零退出码收尾——不然一次预热就命中的批会静静地退 0。
    """
    from app.eval.reflect_prefix_probe import _LOCAL_CACHE_EXIT_STATUS

    batch_exits = int(summary["local_cache_exit_rows"])
    warmup_exits = sum(
        1 for row in rows_by_label.get(_PREFIX_PROBE_WARMUP_LABEL, ())
        if row.get("status") == _LOCAL_CACHE_EXIT_STATUS
    )
    return batch_exits, warmup_exits


def _run_prefix_probe(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    sample_path: Path,
) -> int:
    """E1 的真跑半边。**产物落盘与 `provider.close()` 都在 `finally` 上**。

    那条 `try/finally` 不是防御性编程,是对已经花掉的钱负责(评审 F2 / P1-1):
    循环体里 `render_sample` 的拒绝路径、行闭集自检、以及任何一次 `Ctrl-C`,都
    会从循环里逃出去。裸语句版本下 `close()` 与全部落盘(三份 jsonl / 摘要 /
    manifest / per-call 表)一个都不执行——真跑时等于「打完 60 个真实模型调用,
    第 61 格一个 `ValueError` 把这 60 格的数据全部作废」,而 out-dir 连目录都
    没建。`except BaseException` 也走这条 salvage(`KeyboardInterrupt` 不是
    `Exception`,96 格 × 数十秒在第 80 分钟被 Ctrl-C 时最需要的正是这一条)。
    """
    _prefix_probe_set_env(_prefix_probe_process_env(args))
    from app.core.config import Settings
    from app.core.llm import (
        CALL_STATS_KWARG, experiment_message_markers, provider_messages,
        serialize_provider_messages,
    )
    from app.eval.reflect_manifest import build_manifest
    from app.eval.reflect_prefix_probe import (
        ARM_DISTURBED, ARM_STABLE, PROBE_SCHEMA_HINT, assert_probe_row_closed,
        build_marker_pair, load_prefix_probe_sample, render_sample,
        summarize_probe,
    )

    sample = load_prefix_probe_sample(sample_path)
    settings = Settings()
    # 首格之前:三档正文 + manifest 事实各算一遍,坏样本不烧真调用。
    try:
        facts = _prefix_probe_preflight(plan, sample, args)
    except (ValueError, KeyError, TypeError) as exc:
        print(
            "ERROR: prefix-probe preflight 不通过(样本或计划的纯计算就已经"
            f"不成立,一次模型调用都没打):{exc}",
            file=sys.stderr,
        )
        return 2

    provider, client = _prefix_probe_client(settings)
    try:
        if not provider.configured("reasoning_agent"):
            print(
                "ERROR: reasoning_agent workload 没有配置模型服务;prefix-probe "
                "需要一份真实模型配置(--env-file 或 .env)才能真跑",
                file=sys.stderr,
            )
            return 2

        contract_short, contract_readable = _ab_model_contract(settings)
        runner.say("model contract", f"{contract_short}  ({contract_readable})")

        log_dir = _ab_llm_log_dir(settings)
        event_dir = Path(rig_event_log_dir(args.out_dir))
        start_llm_offsets = _ab_llm_offsets(log_dir)
        start_event_offsets = _ab_llm_offsets(event_dir, glob=RIG_EVENT_LOG_GLOB)

        started_at = _prefix_probe_now()
        deadline = (
            time.monotonic() + args.max_wall_minutes * 60.0
            if args.max_wall_minutes is not None else None
        )

        def _budget_expired() -> bool:
            return deadline is not None and time.monotonic() >= deadline

        rows_by_label: dict[str, list[dict]] = {
            ARM_STABLE: [], ARM_DISTURBED: [], _PREFIX_PROBE_WARMUP_LABEL: [],
        }
        stopped_by_budget = False
        # `series_index` → 上一格**返回**的单调时刻(不是发起时刻)。
        prev_return_monotonic: dict[int, float] = {}

        def _record_row(label: str, row: dict) -> None:
            assert_probe_row_closed(row)
            rows_by_label.setdefault(label, []).append(row)

        def _call_row(
            *, head: str, tail: str, messages: list[dict], timeout: float,
            max_retries: int,
        ) -> dict:
            sink: dict[str, Any] = {}
            try:
                with experiment_message_markers(head, tail):
                    client.chat_json(
                        messages, PROBE_SCHEMA_HINT, timeout=timeout,
                        max_retries=max_retries, bypass_cache=True,
                        **{CALL_STATS_KWARG: sink},
                    )
            except Exception:
                sink.setdefault("status", "error")
            full_messages = provider_messages(
                messages, PROBE_SCHEMA_HINT, markers=(head, tail))
            usage = sink.get("usage") or {}
            return {
                "status": sink.get("status"),
                "call_wall_ms": sink.get("call_wall_ms"),
                "attempts": sink.get("attempts"),
                "finish_reason": _prefix_probe_finish_reason(
                    sink.get("finish_reason")),
                "response_chars": sink.get("response_chars"),
                "head_chars": len(head),
                "tail_chars": len(tail),
                "message_bytes_total": len(
                    serialize_provider_messages(full_messages)),
                "prompt_tokens": usage.get("prompt_tokens"),
                "cached_tokens": usage.get("cached_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            }

        def _finish() -> int:
            """落盘 + 摘要 + manifest + per-call 表 + 退出码。

            正常路径与 salvage 路径共用这一个函数——两条路各写一份的话,异常
            那条(正是最需要产物的那条)会长期与正常那条分叉。
            """
            finished_at = _prefix_probe_now()
            all_rows = [row for rows in rows_by_label.values() for row in rows]
            summary = summarize_probe(all_rows)

            # 三份 jsonl **一律出表**,零派发时是三个空文件(评审 P3-15):
            # dry-run 的 `out` 行承诺了这三个落点,按固定名读的下游拿到
            # `FileNotFoundError` 分不清「这批没跑起来」与「路径写错了」。
            for label, rows in rows_by_label.items():
                text = "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in rows
                )
                runner.write(runner.out_dir / f"probe-{label}.jsonl", text)

            summary_md = _render_probe_summary_markdown(
                summary, seed=args.seed, smoke=args.smoke,
                sample_path=sample_path, marker_variant=args.marker_variant,
            )
            runner.write(runner.out_dir / "probe-summary.md", summary_md)
            runner.write(
                runner.out_dir / "probe-summary.json",
                json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2),
            )

            manifest = build_manifest(
                channel="e1",
                code_sha=_ab_git_sha(),
                started_at=started_at,
                finished_at=finished_at,
                stopped_by_budget=stopped_by_budget,
                model_contract=contract_short,
                budgets={
                    "call_timeout_seconds": int(
                        settings.reasoning_timeout_seconds),
                    "attempt_budget": REASONING_ATTEMPT_BUDGET,
                    "max_wall_minutes": args.max_wall_minutes,
                },
                **facts,
            )
            _write_manifest(manifest, runner=runner)

            calls_rows = _rig_call_rows(
                log_dir, event_dir, llm_offsets=start_llm_offsets,
                event_offsets=start_event_offsets, tags=None,
            )
            _write_call_rows(runner.out_dir, "e1", calls_rows)

            # 退出码的**三个**来源,stderr 上各写清一句(评审 P2-4):混成一句
            # 「非零」会让 `prefix-probe && analyze …` 的操作者分不清这批是被
            # 预算截断、命中了不该命中的缓存出口,还是整批全灭。
            exit_status = 0
            if stopped_by_budget:
                print(
                    "ERROR: 整批墙钟预算到点,提前停止派发(未完成/不成对标记见 "
                    "manifest.stopped_by_budget)",
                    file=sys.stderr,
                )
                exit_status = 1
            batch_exits, warmup_exits = _prefix_probe_local_cache_exits(
                summary, rows_by_label)
            if batch_exits or warmup_exits:
                print(
                    f"ERROR: {batch_exits} 格(另有 {warmup_exits} 个预热格)"
                    "命中本地响应缓存出口(status=cache_hit),bypass_cache=True "
                    "下不应出现;主批那个数就是 probe-summary 的 "
                    "local_cache_exit_rows(预热行按 design §9.1 不进任何统计,"
                    "单独数)",
                    file=sys.stderr,
                )
                exit_status = 1
            ok_rows = (summary["first_observation_row_count"]
                       + summary["repeat_observation_row_count"])
            if ok_rows == 0:
                print(
                    "ERROR: 一格成功观测都没有(status=ok 的非预热行为 0;"
                    f"failed_row_count={summary['failed_row_count']})。"
                    "这批数据出不了任何结论,不要往 analyze 传",
                    file=sys.stderr,
                )
                exit_status = 1
            return exit_status

        try:
            if not _budget_expired():
                warmup_head, warmup_tail = build_marker_pair(
                    args.seed, _PREFIX_PROBE_WARMUP_TIER, -1, ARM_STABLE, 0,
                    marker_variant=args.marker_variant,
                )
                warmup_row = _call_row(
                    head=warmup_head, tail=warmup_tail,
                    messages=_prefix_probe_warmup_messages(),
                    timeout=settings.reasoning_timeout_seconds,
                    max_retries=settings.reasoning_max_retries,
                )
                warmup_row.update({
                    "tier": None, "block_index": None, "arm": None,
                    "call_index": None, "series_index": None,
                    "is_warmup": True, "gap_ms": None,
                })
                _record_row(_PREFIX_PROBE_WARMUP_LABEL, warmup_row)
                runner.say(
                    "warmup done",
                    f"status={warmup_row['status']} "
                    f"wall_ms={warmup_row['call_wall_ms']}",
                )

                dispatched = 0
                for entry in plan:
                    if _budget_expired():
                        stopped_by_budget = True
                        break
                    tier = entry["tier"]
                    block_index, arm = entry["block_index"], entry["arm"]
                    call_index = entry["call_index"]
                    series_index = entry["series_index"]

                    # `gap_ms` = 本格**发起** − 同序列上一格**返回**(评审 F6:
                    # 「与上一次调用的间隔」是真实空闲,不是周期)。start-to-start
                    # 的差值恰好等于上一格的整段墙钟,而在 E1 里那是数量级最大的
                    # 一项:连续派发时它会记成 8000ms,而真实空闲接近 0——拿这个数
                    # 去和 provider 前缀缓存的 TTL 比较,结论方向可以完全反过来。
                    prev_return = prev_return_monotonic.get(series_index)
                    call_started = time.monotonic()
                    gap_ms = (
                        None if prev_return is None
                        else round((call_started - prev_return) * 1000)
                    )

                    row = _call_row(
                        head=entry["head"], tail=entry["tail"],
                        messages=render_sample(tier, sample),
                        timeout=settings.reasoning_timeout_seconds,
                        max_retries=settings.reasoning_max_retries,
                    )
                    # 返回时刻才是下一格 `gap_ms` 的起点。写在 `_record_row`
                    # 之前:行闭集自检抛出去时这一格的返回时刻已经是事实。
                    prev_return_monotonic[series_index] = time.monotonic()
                    row.update({
                        "tier": tier, "block_index": block_index, "arm": arm,
                        "call_index": call_index, "series_index": series_index,
                        "is_warmup": False, "gap_ms": gap_ms,
                    })
                    _record_row(arm, row)
                    dispatched += 1
                runner.say(
                    "dispatched",
                    f"{dispatched}/{len(plan)} 次调用"
                    + ("(整批墙钟预算到点,提前停止派发)"
                       if stopped_by_budget else ""),
                )
            else:
                stopped_by_budget = True
                runner.say("dispatched", "0(整批墙钟预算在开跑前就已到点)")
        except BaseException:
            # 已经打过的格是花了真钱的观测,不该跟着异常一起消失。salvage 自己
            # 再抛的话会把原始异常盖掉(读的人拿到的是「写盘失败」而不是「样本
            # 地板不足」),所以它单独兜一层,只打一句。
            try:
                _finish()
            except BaseException as salvage_exc:  # noqa: BLE001
                print(
                    f"ERROR: 异常收尾时抢救产物也失败了:{salvage_exc!r}",
                    file=sys.stderr,
                )
            else:
                print(
                    "ERROR: 派发中断,已把此前打完的格落盘到 "
                    f"{runner.out_dir}(产物不完整,manifest 如实记录)",
                    file=sys.stderr,
                )
            raise
        return _finish()
    finally:
        provider.close()

# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印将要做的每一步与 client_request_id 编码;不连库、不起后端",
    )
    parser.add_argument("--db-name", default=DEFAULT_TEST_DB)
    parser.add_argument(
        # 默认值刻意留成 `None`,由 `main()` 填:`search` 要求这一项**显式给出**
        # (它连的是主库),而「有没有显式给」只能靠一个哨兵分辨。其余子命令拿到
        # 的默认值与从前逐字相同。
        "--database-url",
        help=f"**测试库**连接,所有写入都落在这里(默认 "
             f"postgresql://127.0.0.1:5432/{DEFAULT_TEST_DB})。"
             "`search` 例外:它连的是**主库**,必须显式给出,且全程只读",
    )
    parser.add_argument(
        "--admin-url", default="postgresql://127.0.0.1:5432/postgres",
        help="建库/删库用的维护连接",
    )
    parser.add_argument(
        "--source-db-url",
        help="**主库**连接,全程只读。两处消费:`seed` 重建 A 语料时读原文;"
             "`ab` 必填,用它在跑前跑后各点一次主库行数,兑现 §5.5-2 的"
             "「主库零接触」硬断言(不能与 --database-url 指同一个库)",
    )
    parser.add_argument(
        "--source-notebook",
        help="主库里 A 语料所在的 notebook id(刻意不写进仓库)",
    )
    parser.add_argument(
        "--source-notebook-a",
        help="`search` 的 A_nokg 格:主库里那个**无图**单篇笔记本的 id"
             "(刻意不写进仓库)",
    )
    parser.add_argument(
        "--source-notebook-b",
        help="`search` 的 B_kg 格:主库里那个**有图**多篇笔记本的 id"
             "(刻意不写进仓库)",
    )
    parser.add_argument(
        "--owner",
        help="`search` 用哪个主库用户的身份跑(用户名,大小写不敏感)。"
             "默认 = users 表里最早的那个 admin",
    )
    parser.add_argument(
        "--no-intent", action="store_true",
        help="`search` 跳过意图契约(省一次/题的模型调用)。代价是 v2 只剩"
             "整题一个方面,方面账那几列因此不可比",
    )
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="`search` 用几个线程并发跑 run(默认 1,行为与不加这个参数逐字"
             "一致)。两阶段:先按同一并发度并发算意图契约,再把全部 run 提交"
             "给一个线程池。SQLite 主库真跑时强制 clamp 到 1;PG 主库按"
             "POSTGRES_POOL_MAX_SIZE clamp——跑之前先确认模型服务的并发限流"
             "与连接池都吃得下这个数,否则要么被限流打回、要么在拿连接时死等",
    )
    parser.add_argument(
        "--keep-raw-trace", action="store_true",
        help="`search` 额外把每个 run 的原始 TraceStep 列表(含 summary)与 "
             "termination DTO 写到 <out-dir>/raw/<policy>/<question_key>_"
             "<corpus_cell>_<effort>.json。**这份文件含标题与模型 reason,"
             "不进数据集/不进仓库**,只用来人工核对反推口径",
    )
    parser.add_argument(
        "--only-question", action="append", default=[],
        help="`search` 只跑这些题号(可多次,如 B-q03);筛选发生在 --limit "
             "切片之前(先筛后限)。默认不筛,全量参与 --limit",
    )
    parser.add_argument(
        "--only-cell", action="append", default=[],
        choices=list(SEARCH_CELLS),
        help="`search` 只跑这些语料格(可多次,取值同 SEARCH_CELLS:"
             f"{', '.join(SEARCH_CELLS)})。在 --cell 选中的格子里再收窄一层,"
             "与 --only-question 一样先筛后限",
    )
    parser.add_argument("--corpus-dir", help="B 语料 markdown 所在目录")
    parser.add_argument(
        "--base-url", default="",
        help=f"默认 http://127.0.0.1:<--port>(缺省 {DEFAULT_PORT})",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--storage-dir", default=".local/storage-t0")
    parser.add_argument("--out-dir", default=".local/t0")
    parser.add_argument(
        "--env-file",
        help="临时后端与进程内 report 读哪份 .env(模型服务配置)。"
             "默认不设 ⇒ 用当前 checkout 的 .env;worktree 里通常要指向主 checkout",
    )
    parser.add_argument(
        "--user", default=DEFAULT_USER,
        help="fixture 用户名。必须是「单个小写字母+八位数字」(注册端点的正则)",
    )
    parser.add_argument(
        "--skip-kg", action="store_true",
        help="seed 不调 kg/build(有图格也留成无图)。给「没有模型配置」的冒烟跑用",
    )
    parser.add_argument(
        "--skip-embed", action="store_true",
        help="seed 只等解析终态,不等 index-status 就绪",
    )
    parser.add_argument(
        "--skip-create-db", action="store_true",
        help="库已存在(或不是 PG)时跳过建库/装扩展",
    )
    parser.add_argument("--ready-timeout", type=float, default=600.0)
    parser.add_argument("--parse-timeout", type=float, default=3600.0)
    parser.add_argument("--index-timeout", type=float, default=7200.0)
    parser.add_argument(
        "--policy", choices=POLICIES, default="legacy",
        help="`ask` / `report` / `restart` 用它选策略。**`search` 不读它**:"
             "那条路一次进程内跑完两侧,配对因此天然干净",
    )
    parser.add_argument(
        "--only-policy", action="append", default=[], choices=list(POLICIES),
        help="`search` 只跑这一侧策略(可多次;默认两侧都跑)。用于一侧被网络故障"
             "整批打废后的重跑;配对靠 question_key+corpus_cell+effort,不受影响",
    )
    parser.add_argument(
        "--cell", action="append", default=[], choices=list(CORPUS_CELLS),
        help="只跑这些语料格(可多次);默认四格全跑",
    )
    parser.add_argument(
        "--limit", type=int,
        help="每个语料格只取前 N 题(先验证用);默认全量",
    )
    parser.add_argument("--lang", choices=("zh", "en", "both"), default="zh")
    # --- `ab` 专属(A/B 设计规格 §5.4) ---
    parser.add_argument(
        "--efforts", action="append", default=[], choices=list(EFFORTS),
        help="`ab` 跑哪些档位(可多次;默认两档全跑)。`search`/`ask` 不读它"
             "——它们的档位维度由 EFFORTS 定死",
    )
    parser.add_argument(
        "--arms", default=None,
        help="`ab` 跑哪几条臂,逗号分隔。**一次一对**(配对差值表按对出,三条臂"
             "的批次会整批 paired=False,所以超过两条当场拒绝)。一维写法 "
             "`legacy,v2`(= 不给时的默认);二维写法 "
             "`v2:off,v2:prefix_snapshot` 把 REASONING_REFLECT_OPTIMIZATION 当"
             "第二维(前缀复用最终设计 §11)。省略 `:优化` 一律补 `off`;"
             "`legacy:prefix_snapshot` 这类合法值的非法组合、重复臂与空写法"
             "(`--arms \"\"`)都当场拒绝,不退化成默认两臂。"
             "与 --only-policy 互斥。`search`/`ask` 不读它",
    )
    parser.add_argument(
        "--repeats", type=int, default=3,
        help="`ab` 的重复轮数(§4.1 默认 3)。重复轮是**最外层**循环,所以任何"
             "被预算或故障截断的前缀都是一份配对完整、平衡的数据集",
    )
    parser.add_argument(
        "--round", type=int,
        help="`ab` 只跑第 N 轮(与 --repeats 配合:先跑第 1 轮看结论,再补"
             "第 2/3 轮)。不给就是 1..--repeats 全跑",
    )
    parser.add_argument(
        "--max-wall-minutes", type=float, default=None,
        help="`ab` 与 `prefix-probe` 共用的整批墙钟预算(分钟,T-EX8/T-EX4)。"
             "**不给 = 今天的行为逐字相同**:不设上限、不早停。`ab` 到点后停止"
             "派发新单元、cancel_event 唤醒在途、shutdown(cancel_futures=True) "
             "撤掉队列里的;在途未完成单元落 status=cancelled 行(删失观察),不"
             "偷偷补跑到矩阵齐全。`prefix-probe` 是串行的,到点停止派发**未开始"
             "**的调用,同样不补跑到矩阵齐全。两者都以非零退出码收尾,并把"
             " stopped_by_budget 如实写进 manifest.json。≤0 与 NaN 两条命令"
             "共用同一处判据当场拒绝(`_wall_budget_problem`)",
    )
    parser.add_argument(
        "--mode", choices=("reasoning", "chunk", "auto"), default="reasoning",
    )
    # --- `prefix-probe` 专属(E1,计划 §3 T-EX4)---
    parser.add_argument(
        "--seed", type=int, default=None,
        help="`prefix-probe` 必填:区组内的臂序平衡随机与可复现都靠它"
             "(design M4)。别的子命令不读它",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="`prefix-probe` 走 48 次冒烟规模(区组数砍半),而不是默认的 96 次"
             "全量。U4 拍板:可先冒烟,但不凭它直接定论",
    )
    parser.add_argument(
        "--sample-file",
        help="`prefix-probe` 用哪一份上下文样本 JSON。默认仓库内 fixture "
             "(backend/app/eval/reflect_t0/prefix_probe_sample.json);"
             "指向 --out-dir 之外、操作者 .local 下的真实样本时用这个",
    )
    parser.add_argument(
        "--marker-variant", type=int, default=0,
        help="`prefix-probe` 的标记版本号(design §9.1「更换标记值重复验证」);"
             "同一组 (seed, tier, block, arm, call_index) 换一个 variant 就拿到"
             "一组全新的标记值,计划形状不变",
    )
    parser.add_argument(
        "command",
        choices=("seed", "ask", "report", "search", "ab", "restart", "export",
                 "teardown", "prefix-probe"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # `--port` 与 `--base-url` 曾是两个各自独立的默认值:只改端口的人会拿到一台
    # 起在 8011、却被对着 8001 轮询的后端。端口是真源,base-url 只在显式给出时
    # 才覆盖(比如后端起在容器里)。
    args.base_url = args.base_url or f"http://127.0.0.1:{args.port}"
    # `--database-url` 的默认值在这里填,而不是在 argparse 里:`search` 连的是
    # **主库**,拿一个指向测试库的默认值去跑它是最坏的一种"能跑起来"。哨兵让
    # 「有没有显式给」变成一个可判定的事实。
    args.database_url_explicit = args.database_url is not None
    # 默认库名跟着 `--db-name` 走,不是常量:`--db-name another_t0 seed` 建的是
    # another_t0,扩展与迁移却打到常量默认库上——那个库若恰好存在就被写了,不存在
    # 就报一句和 `--db-name` 无关的连接错误(codex #700 R5 P2)。
    args.database_url = (
        args.database_url or f"postgresql://127.0.0.1:5432/{args.db_name}"
    )
    # 负数/零没有意义;`_resolve_concurrency` 的 clamp 是"往下调",这里只挡住
    # 明显打错的值,不在这里做 SQLite/连接池那两条(它们要跑到真库连上才判断
    # 得出来)。
    args.concurrency = max(1, int(args.concurrency))
    # `ab` 的重复轮数同理:0 或负数会让整批枚举变成空的,而那看起来完全像一次
    # 「这批题都被过滤掉了」。`--round` 不 clamp——它是一个下标,越界该报错。
    args.repeats = max(1, int(args.repeats))
    runner = Runner(dry_run=args.dry_run, out_dir=Path(args.out_dir))
    handlers = {
        "seed": cmd_seed, "ask": cmd_ask, "report": cmd_report,
        "search": cmd_search, "ab": cmd_ab, "restart": cmd_restart,
        "export": cmd_export, "teardown": cmd_teardown,
        "prefix-probe": cmd_prefix_probe,
    }
    return handlers[args.command](args, runner)


if __name__ == "__main__":
    raise SystemExit(main())
