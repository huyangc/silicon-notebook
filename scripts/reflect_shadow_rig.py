#!/usr/bin/env python3
"""reflect v2 开闸前 T0 · 影子轨迹 rig(设计规格 2026-09-08 §2.3)。

在**本地一次性 PG 测试库**上灌真实语料与真实提问,同一批问题在 legacy 与 v2
两种策略下各跑一遍,Ask 与 Report 都跑。Ask 的轨迹落库、由
`scripts/export_reasoning_traces.py` 导出;Report 的逐节轨迹不落库,由本脚本
**进程内**捕获成 JSONL(§2.3)。

另有一条**不建测试库**的路:`search` 直接对**主库(只读)**里两个既有笔记本
跑检索过程(plan + reflect 循环),轨迹在进程内投影成 JSONL。它不建库、不建图、
不合成答案,也因此不写 ask_jobs / answers / conversations 任何一行。

    python scripts/reflect_shadow_rig.py --dry-run seed
    python scripts/reflect_shadow_rig.py --dry-run ask
    python scripts/reflect_shadow_rig.py --dry-run report
    python scripts/reflect_shadow_rig.py --dry-run --limit 5 search

子命令:`seed` / `ask` / `report` / `search` / `restart` / `export` / `teardown`。
**`--dry-run` 打印将要做的每一步与每个 `client_request_id` 的编码,不连库、不
起后端、不发任何请求**——它是唯一进标准门的路径(rig 本体要网络与真实模型)。

三条红线,代码层面的落点在下面各处注释里:

* **主库只读**:`seed` 碰主库时只用 `--source-db-url` 显式给出的连接、只跑一条
  SELECT(重建 A 语料的 markdown),写入全在测试库;`search` 整条路对
  `--database-url`(必须显式给出的主库连接)只读,跑前跑后各点一次
  `READONLY_TABLES` 的行数,不等就报错。
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
import secrets
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

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
) -> list[dict]:
    """`ask` 要发的全部 run,一条一行。**dry-run 与真跑读的是同一个函数**。

    dry-run 若另起一份枚举,它验证的就不是将要跑的那件事了。
    """
    plan: list[dict] = []
    langs = ("zh", "en") if lang == "both" else (lang,)
    for cell in cells:
        corpus = cell.split("_", 1)[0]
        rows = [row for row in questions["ask"] if row["corpus"] == corpus]
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
                        "scope_sources": row.get("scope_sources"),
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
    """
    plan: list[dict] = []
    for policy in policies:
        for item in ask_plan(
            questions, cells=cells, policy=policy, limit=limit, lang=lang,
            mode="reasoning", efforts=efforts,
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
            # 那会让一次跑了半小时的 seed 死在一句「HTTP Error 400」上。
            detail = exc.read().decode("utf-8", "replace")[:500]
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
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc
        self.say("stream done",
                 f"{events} event(s), last type={last.get('type', '<none>')}")
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
        page = runner.http(
            "GET", f"{base}/api/notebooks/{notebook_id}/sources?offset=0&limit=200",
            token=token, timeout=60, quiet=True,
        ) or {}
        items = list(page.get("items") or [])
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
    runner.say("create database", f"{args.admin_url} :: CREATE DATABASE {args.db_name}")
    runner.say("install extensions", ", ".join(PG_EXTENSIONS))
    if not runner.dry_run and not args.skip_create_db:
        _create_test_database(args)

    cells = corpus_cells(args.cell)
    corpus = _stage_corpus(args, runner, questions, cells)
    if corpus is None:
        return 2

    runner.say("start backend",
               shlex.join(_backend_command(args))
               + "  env=" + ",".join(f"{k}={v}" for k, v in
                                     backend_env(args, "legacy").items()))
    backend = _start_backend(args, "legacy") if not runner.dry_run else None
    try:
        return _seed_notebooks(args, runner, corpus, cells,
                               backend_pid=backend.pid if backend else 0)
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


def _create_test_database(args: argparse.Namespace) -> None:
    import psycopg

    with psycopg.connect(args.admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{args.db_name}"')
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
    for item in plan:
        notebook = state["notebooks"].get(item["corpus_cell"], "<unknown>")
        base = f"{args.base_url}/api/notebooks/{notebook}"
        runner.say(
            "ask",
            f"{item['question_key']} cell={item['corpus_cell']} "
            f"effort={item['effort']} mode={item['requested_mode']} "
            f"crid={item['client_request_id']}",
        )
        # 高级界面路径:先 /ask/intent 拿契约,再带着确认过的契约提交。
        # 只有这条路径才让方面账拿到真实的 mandatory_topics(§2.3)。
        contract = runner.http(
            "POST", f"{base}/ask/intent",
            json_body={"question": item["question"]}, token=token,
        )
        body: dict[str, Any] = {
            "question": item["question"],
            "mode": item["requested_mode"],
            "retrieval_effort": item["effort"],
            "client_request_id": item["client_request_id"],
        }
        if contract is not None:
            body["intent"] = {
                "contract": contract,
                "resolved_question": contract.get("resolved_question")
                or item["question"],
                "answers": [],
            }
        # **必须走 /ask/stream,不是 /ask**:同步端点经 `begin_durable_job` 建
        # 行,而那条路 `client_request_id=None` 是写死的
        # (`repositories/postgres/ask_state_store.py:231`,SQLite 侧同形);
        # 只有 `begin_or_attach_durable_job`(即 stream 端点)会把幂等键落库。
        # 走同步端点的话,整批 run 的 `question_key`/`corpus_cell` 全是 unknown
        # ——§4.2 的对照表恒空,而且要跑完几百个 run 才看得出来。
        runner.stream("POST", f"{base}/ask/stream", json_body=body, token=token)
        if not verified:
            _assert_tag_landed(args, runner, item["client_request_id"])
            verified = True
    return 0


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
    try:
        with out.open("a", encoding="utf-8") as handle:
            for item in plan:
                notebook = state["notebooks"][item["corpus_cell"]]
                report_id = repo.create_report(
                    notebook, item["question"], depth=item["depth"]
                )
                captured = _generate_report(
                    repo, engine_class, profile, notebook, report_id, item,
                    model_work_scope=model_work_scope,
                    model_priority=ModelPriority.REPORT,
                    source_scope_context=source_scope_context,
                )
                for row in _report_rows(repo, notebook, report_id, item, captured):
                    assert_closed(row)
                    assert_projection_values(row)
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                runner.say("report done", item["question_key"])
    finally:
        reset_request_user(ctx)
    runner.say("wrote report trace", str(out))
    return 0


def _generate_report(
    repo: Any, engine_class: type, profile: Any, notebook: str, report_id: str,
    item: dict, *, model_work_scope: Any, model_priority: Any,
    source_scope_context: Any,
) -> dict[int, list[dict]]:
    """跑一份报告,返回 `{节号: [步, ...]}`。

    两层 scope 与 `report_execution.ReportExecutionCoordinator.start_plan` 逐字
    同形:`model_work_scope`(模型调用的归属与优先级)+ `source_scope_context`
    (范围冻结)。少哪一层都不是「少记一点日志」——模型调度会按另一份归属排队,
    检索会在另一份范围上跑,那就不是生产接线下的行为了。

    入口选 `run(..., auto_generate=True, require_intent_review=True)` 而不是
    `generate(...)`:`generate` 要一份已确认的大纲,报告行是空的时候直接调它没有
    任何东西可生成。这一条正是协调器 `start_plan` 在 `intent_contract is None`
    时走的那条(理解 → 自动确认契约 → 规划大纲 → 生成)。
    """
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
    return captured


def _report_rows(
    repo: Any, notebook: str, report_id: str, item: dict,
    captured: dict[int, list[dict]],
) -> Iterable[dict]:
    """逐节**一行**:轨迹投影与结果级投影按 `merge_key` 当场合成。

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
    report = repo.get_report(notebook, report_id)
    sections = list(report.get("sections") or [])
    tags = {
        "corpus_cell": item["corpus_cell"],
        "question_key": item["question_key"],
        "consumer": "report_section",
        "trace_source": "in_process",
        "policy_version": item["policy"],
    }
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
            {"mode": "reasoning", "status": "done"}, steps, None,
            sources_count=None, rig_tags=tags,
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
    notebooks = {
        "A_nokg": args.source_notebook_a, "B_kg": args.source_notebook_b,
    }
    if not cells:
        # 这一条在 `--dry-run` 下也拦:选空了的 `--cell` 会让 dry-run 打印一份
        # 「零个 run」的计划,而那看起来完全像一次正常的预演。
        print("ERROR: " + _search_preflight(args, cells, notebooks),
              file=sys.stderr)
        return 2
    plan = search_plan(
        questions, cells=cells, policies=POLICIES, limit=args.limit,
        lang=args.lang, efforts=EFFORTS,
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
               ", ".join(POLICIES) + "(同进程换 Settings,不重启后端)")
    runner.say("efforts", ", ".join(EFFORTS))
    runner.say("planned runs",
               f"{len(plan)}({len(unique_questions)} 题 × {len(POLICIES)} 策略 "
               f"× {len(EFFORTS)} 档)")
    runner.say("model calls (estimate)",
               _search_call_estimate(plan, len(unique_questions),
                                     no_intent=args.no_intent))
    runner.say("out",
               f"{runner.out_dir}/search-<policy>.jsonl + search-runs.log"
               + ("" if args.no_intent else
                  f" + {runner.out_dir}/intents.jsonl(**不进数据集/仓库**)"))
    if runner.dry_run:
        for item in plan:
            runner.say(
                "search",
                f"{item['question_key']} cell={item['corpus_cell']} "
                f"policy={item['policy']} effort={item['effort']}",
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
    """
    intent_calls = 0 if no_intent else question_count
    plan_calls = len(plan) if no_intent else 0
    reflect_ceiling = sum(
        MAX_REFLECT_STEPS.get(item["effort"], 0) for item in plan
    )
    return (
        f"intent {intent_calls} + plan {plan_calls} + reflect ≤ "
        f"{reflect_ceiling}  ⇒  ≤ {intent_calls + plan_calls + reflect_ceiling}"
    )


def _search_process_env(args: argparse.Namespace) -> dict[str, str]:
    """`search` 装进**本进程**的环境变量。必须在 import `app.core.config` 之前设。

    `DATABASE_URL` 显式覆盖:主库连接由 `--database-url` 给出,不从 `.env` 猜
    ——那个文件里写的是这台机器平时连的库,而这条路会真的对它跑检索。

    另外三把**注入闸强制关**。它们本来就默认关、而且 `from_repository` 压根不
    接 `agent_profile` / `retrieval_experiences` / `identity_store` 三个端口
    (所以注入在结构上就不可达),这里再关一次是为了让「主库只读」不依赖于
    「那三个端口恰好没接线」这个会随重构漂移的前提:注入闸一关,
    `experience_wiring_active` 恒假,收尾那条 `note_adopted` UPDATE 就连分支
    都进不去。
    """
    env = {
        "DATABASE_URL": args.database_url,
        "RETRIEVAL_EXPERIENCE_INJECT_ENABLED": "false",
        "REASONING_CONSULT_MEMORY_ENABLED": "false",
        "AGENT_PROFILE_ENABLED": "false",
    }
    if args.env_file:
        env["SILICON_NOTEBOOK_ENV_FILE"] = str(Path(args.env_file).expanduser())
    return env


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


def _readonly_counts(database_url: str) -> dict[str, int | None]:
    """`READONLY_TABLES` 的行数快照。**每张表一条独立的只读连接**。

    一条连接跑五次 count 更省,但 PG 上任何一条失败(表不存在)会把整个事务打
    成 aborted、后面四张跟着报错——那会让「有一张表改名了」看起来像「五张表都
    没了」。一张表一条连接,一次失败只影响它自己。连接由 `_Reader` 开
    `read_only`,所以这几条 count 在服务端就写不动任何东西。
    """
    from export_reasoning_traces import _Reader

    counts: dict[str, int | None] = {}
    for table in READONLY_TABLES:
        try:
            with _Reader(database_url) as reader:
                rows = reader.query(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(rows[0]["n"]) if rows else 0
        except Exception:  # noqa: BLE001 — 表不存在 ⇒ 这次不看它(记 None)
            counts[table] = None
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


def _format_counts(counts: dict[str, int | None]) -> str:
    return ", ".join(
        f"{table}={counts.get(table) if counts.get(table) is not None else 'n/a'}"
        for table in READONLY_TABLES
    )


def _assert_readonly(
    runner: Runner, before: dict[str, int | None], after: dict[str, int | None],
) -> None:
    """跑前跑后逐张表的行数必须相等。不等就标红并报错。"""
    drifted = {
        table: (before.get(table), after.get(table))
        for table in READONLY_TABLES
        if before.get(table) != after.get(table)
    }
    if not drifted:
        runner.say("readonly ok", _format_counts(after))
        return
    detail = ", ".join(
        f"{table}: {old} -> {new}" for table, (old, new) in sorted(drifted.items())
    )
    print(f"\033[31m[READ-ONLY VIOLATION]\033[0m {detail}", file=sys.stderr)
    raise RuntimeError("主库行数在这次 search 之后变了: " + detail)


class _IntentCache:
    """每题只算一次意图契约,落 `intents.jsonl` 复用。

    **这个文件不进数据集、不进仓库**:契约里带着问题原文与模型改写过的
    `resolved_question`,是自由文本,而 rig 写进 JSONL 的每一行都受闭集约束
    (§6)。它落在 `--out-dir`(默认在 `.local/` 下)只为一件事:一次中断的
    rig 重跑时不用再付一遍 intent 的模型调用。

    键取 `question_key`,不带语料格:契约是 **corpus-blind** 的
    (`plan_query_intent` 一个字的语料都不读),而 A/B 两格的题号本来就不同。
    """

    def __init__(self, path: Path, *, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self._rows: dict[str, dict] = {}
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
        if key not in self._rows:
            self._rows[key] = _plan_intent(
                repo, settings, item["question"], actor_id=actor_id,
                notebook=notebook,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"question_key": key, "contract": self._rows[key]},
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
        answers=[],
    )


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
    # 提交了契约、没有待澄清项、没有澄清回答 ⇒ 用户原文仍是首要权威。
    authoritative = not frozen.needs_clarification
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
) -> Any:
    """一个 run:三层 scope + `ReasoningRetriever.run`。生产接线的逐字复刻。

    三层 scope 与 Ask 生产路径同形,少哪一层都不是「少记一点日志」:

    * `model_work_scope(INTERACTIVE)` —— 模型调用的归属与优先级(Ask 是交互档,
      不是报告档;两者的 deadline 差一个数量级);
    * `retrieval_run(run_kind="ask_reasoning")` —— 请求级的查询向量 memo 与扇出
      预算,`kg_in_scope_for` 的 memo 也挂在它上面。**`event_log=None`**:那个
      事件汇是这条路上唯一会往库里写的旁路,主库只读,所以刻意不接;
    * `source_scope_context(notebook, None, None)` —— 不收窄范围时它是个 no-op,
      留着是为了与 `_generate_report` 那半程同形。

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
    with model_work_scope(
        priority=ModelPriority.INTERACTIVE, actor_id=actor_id,
        notebook_id=notebook, question=question,
    ):
        with retrieval_run(
            run_kind="ask_reasoning", event_log=None, actor_id=actor_id,
            cancel_event=cancel_event,
        ):
            with source_scope_context(notebook, None, None):
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


def _search_preflight(
    args: argparse.Namespace, cells: Sequence[str], notebooks: dict[str, str],
) -> str:
    """真跑前的硬前提。返回空串 = 通过,否则是要打给用户的那句话。"""
    if not cells:
        return (f"--cell 选中的格子不在 search 的范围里(它只跑 "
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

    import threading

    from app.core.request_context import reset_request_user, set_request_user
    from app.repositories.factory import create_repository
    from app.services.reasoning_retrieval import kg_in_scope_for

    settings_by_policy = _settings_by_policy()
    # `create_repository` 而不是 `create_application_repository`:后者还会
    # 装插件宿主并 prime 一次扩展准入表。这条路只跑检索、而且对主库只读,
    # 接一层可能带写路径的扩展面没有收益。
    repo = create_repository(settings_by_policy["legacy"])
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
        _search_loop(
            args, runner, plan, facts, repo, settings_by_policy,
            actor_id=actor_id, cancel_event=threading.Event(),
        )
    finally:
        reset_request_user(context)
    _assert_readonly(runner, before, _readonly_counts(args.database_url))
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


def _search_loop(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    facts: dict[str, dict], repo: Any, settings_by_policy: dict[str, Any],
    *, actor_id: str, cancel_event: Any,
) -> None:
    """整批 run。每个 run 一行 JSONL + 一行日志,**都不含任何问题原文**。"""
    intents = _IntentCache(
        runner.out_dir / "intents.jsonl", enabled=not args.no_intent
    )
    runner.out_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, Any] = {}
    verified: set[str] = set()
    log = (runner.out_dir / "search-runs.log").open("a", encoding="utf-8")
    try:
        for index, item in enumerate(plan, 1):
            fact = facts[item["corpus_cell"]]
            policy = item["policy"]
            settings = settings_by_policy[policy]
            prepared = _prepare_search_intent(
                intents.get(repo, settings, item, actor_id=actor_id,
                            notebook=fact["notebook"]),
                item["question"], item["effort"],
            )
            steps: list[dict] = []
            started = time.monotonic()
            result = run_search_once(
                repo, settings, notebook=fact["notebook"], item=item,
                prepared=prepared,
                on_step=lambda step: steps.append(trace_step_row(step)),
                cancel_event=cancel_event, actor_id=actor_id,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000)
            row = project_search_run(
                steps, result=result, effort=item["effort"], policy=policy,
                question_key=item["question_key"],
                corpus_cell=item["corpus_cell"],
                kg_in_scope=fact["kg_in_scope"],
                has_intent_contract=prepared is not None,
                sources_count=fact["sources"],
            )
            assert_closed(row)
            assert_projection_values(row)
            handle = handles.get(policy)
            if handle is None:
                handle = handles[policy] = (
                    runner.out_dir / f"search-{policy}.jsonl"
                ).open("a", encoding="utf-8")
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            line = (
                f"{index:03d}/{len(plan)} {item['question_key']} "
                f"{item['corpus_cell']} {policy} {item['effort']} "
                f"{elapsed_ms}ms reflect_turns={row['reflect_turns']} "
                f"termination={row['termination_reason']}"
            )
            log.write(line + "\n")
            log.flush()
            runner.say("search done", line)
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
    with out.open("w", encoding="utf-8") as handle:
        for part in parts:
            if part.exists():
                handle.write(part.read_text(encoding="utf-8"))
    return 0


def _stop_backend_gracefully(
    runner: Runner, pid: int, *, timeout: float = 15.0,
) -> None:
    """SIGTERM,给它 `timeout` 秒退出;超时了才 SIGKILL(§2.3「优雅停止」)。

    轮询用 `os.kill(pid, 0)`(不发信号,只探测进程还在不在)——PID 不存在时
    这一探测本身就抛 `OSError`,拿来当"已经退出"的判据,不用额外 waitpid
    (uvicorn 子进程可能已经被系统回收,不一定还是本进程的直接子进程)。
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        runner.say("stop backend failed", str(exc))
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
        runner.say("stop backend", f"pid={pid} 超时 {timeout}s 未退出,已 SIGKILL")
    except OSError:
        pass


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
        f"port={port} database_url={database_url} "
        f"REASONING_REFLECT_V2_ENABLED={new_flag}",
    )
    if runner.dry_run:
        return 0

    if pid:
        _stop_backend_gracefully(runner, pid)

    restart_args = argparse.Namespace(**vars(args))
    restart_args.port = port
    restart_args.database_url = database_url
    restart_args.storage_dir = storage_dir
    restart_args.env_file = env_file
    restart_args.base_url = f"http://127.0.0.1:{port}"
    backend = _start_backend(restart_args, args.policy)

    state.update({
        "backend_pid": backend.pid, "policy": args.policy, "port": port,
        "database_url": database_url, "storage_dir": storage_dir,
        "env_file": env_file,
    })
    runner.save_state(state)
    runner.say("restarted", f"pid={backend.pid} policy={args.policy}")
    return 0


def _teardown_targets_this_run(args: argparse.Namespace) -> bool:
    """`--db-name` 说的那个库,真的是这次 rig 写进去的那个吗?

    `teardown` 的 DROP 走的是 `--admin-url`(PG 维护连接),而 `--db-name` 有个
    默认值。一次跑在别的后端上的 rig(比如 SQLite 冒烟)照样会让 teardown 去
    PG 上 DROP 那个默认名字的库——它可能是别人的。判据取 `--database-url`:只有
    当这次真的写在 PG 的 `--db-name` 上时才删。
    """
    url = str(args.database_url or "")
    if not url.startswith(("postgres://", "postgresql://")):
        return False
    return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] == args.db_name


def cmd_teardown(args: argparse.Namespace, runner: Runner) -> int:
    state = runner.load_state()
    pid = int(state.get("backend_pid") or 0)
    runner.say("stop backend", f"port {args.port} pid={pid or '<unknown>'}")
    drop = _teardown_targets_this_run(args)
    runner.say(
        "drop database" if drop else "skip drop database",
        f"{args.admin_url} :: DROP DATABASE {args.db_name}" if drop else
        f"--database-url 不是 PG 的 {args.db_name},不碰任何库",
    )
    if runner.dry_run:
        return 0
    if pid:
        # 先收后端再删库:还连着的会话会让 DROP 走 FORCE 去踢连接,而被踢掉的
        # uvicorn 之后每个请求都在报连接错——留一个这样的进程占着端口比留一个
        # 库更烦人。
        try:
            os.kill(pid, 15)
        except OSError as exc:
            runner.say("stop backend failed", str(exc))
    if not drop:
        return 0
    import psycopg

    with psycopg.connect(args.admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{args.db_name}" WITH (FORCE)')
    return 0


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
        help="**主库**连接,只在 seed 重建 A 语料时只读使用。必须显式给出",
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
        "--cell", action="append", default=[], choices=list(CORPUS_CELLS),
        help="只跑这些语料格(可多次);默认四格全跑",
    )
    parser.add_argument(
        "--limit", type=int,
        help="每个语料格只取前 N 题(先验证用);默认全量",
    )
    parser.add_argument("--lang", choices=("zh", "en", "both"), default="zh")
    parser.add_argument(
        "--mode", choices=("reasoning", "chunk", "auto"), default="reasoning",
    )
    parser.add_argument(
        "command",
        choices=("seed", "ask", "report", "search", "restart", "export",
                 "teardown"),
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
    args.database_url = (
        args.database_url or f"postgresql://127.0.0.1:5432/{DEFAULT_TEST_DB}"
    )
    runner = Runner(dry_run=args.dry_run, out_dir=Path(args.out_dir))
    handlers = {
        "seed": cmd_seed, "ask": cmd_ask, "report": cmd_report,
        "search": cmd_search, "restart": cmd_restart, "export": cmd_export,
        "teardown": cmd_teardown,
    }
    return handlers[args.command](args, runner)


if __name__ == "__main__":
    raise SystemExit(main())
