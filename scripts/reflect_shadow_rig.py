#!/usr/bin/env python3
"""reflect v2 开闸前 T0 · 影子轨迹 rig(设计规格 2026-09-08 §2.3)。

在**本地一次性 PG 测试库**上灌真实语料与真实提问,同一批问题在 legacy 与 v2
两种策略下各跑一遍,Ask 与 Report 都跑。Ask 的轨迹落库、由
`scripts/export_reasoning_traces.py` 导出;Report 的逐节轨迹不落库,由本脚本
**进程内**捕获成 JSONL(§2.3)。

    python scripts/reflect_shadow_rig.py --dry-run seed
    python scripts/reflect_shadow_rig.py --dry-run ask
    python scripts/reflect_shadow_rig.py --dry-run report

子命令:`seed` / `ask` / `report` / `export` / `teardown`。
**`--dry-run` 打印将要做的每一步与每个 `client_request_id` 的编码,不连库、不
起后端、不发任何请求**——它是唯一进标准门的路径(rig 本体要网络与真实模型)。

三条红线,代码层面的落点在下面各处注释里:

* **主库只读**:只有 `seed` 会碰主库,而且只用 `--source-db-url` 显式给出的
  连接、只跑一条 SELECT(重建 A 语料的 markdown)。所有写入都在测试库。
* **策略靠重启**:`REASONING_REFLECT_V2_ENABLED` 是进程级 Settings,rig 不去
  热改它;`ask` / `report` 每次只跑**一个** `--policy`,换策略 = 换一次后端。
* **编号不进问题文本**:题号/语料格/策略/档位只编进 `client_request_id`(见
  `encode_client_request_id`),导出时据它打标。模型永远看不到这些编号。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import shlex
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
    assert_closed,
    merge_key,
    project_report_section,
    project_run,
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


def cmd_ask(args: argparse.Namespace, runner: Runner) -> int:
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
        "--database-url",
        default=f"postgresql://127.0.0.1:5432/{DEFAULT_TEST_DB}",
        help="**测试库**连接。所有写入都落在这里",
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
    parser.add_argument("--policy", choices=POLICIES, default="legacy")
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
        choices=("seed", "ask", "report", "export", "teardown"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # `--port` 与 `--base-url` 曾是两个各自独立的默认值:只改端口的人会拿到一台
    # 起在 8011、却被对着 8001 轮询的后端。端口是真源,base-url 只在显式给出时
    # 才覆盖(比如后端起在容器里)。
    args.base_url = args.base_url or f"http://127.0.0.1:{args.port}"
    runner = Runner(dry_run=args.dry_run, out_dir=Path(args.out_dir))
    handlers = {
        "seed": cmd_seed, "ask": cmd_ask, "report": cmd_report,
        "export": cmd_export, "teardown": cmd_teardown,
    }
    return handlers[args.command](args, runner)


if __name__ == "__main__":
    raise SystemExit(main())
