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
import os
import shlex
import subprocess
import sys
import time
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
             files: object = None, token: str = "") -> Any:
        self.say("http", f"{method} {url}")
        if self.dry_run:
            return None
        import urllib.error
        import urllib.request

        data = None
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        with urllib.request.urlopen(request, timeout=1800) as response:
            body = response.read().decode("utf-8")
        return json.loads(body) if body else None

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


# --- 子命令 -----------------------------------------------------------------


def backend_env(args: argparse.Namespace, policy: str) -> dict[str, str]:
    """临时后端的环境变量。策略开关在这里,而不是在运行时改 Settings。"""
    return {
        "DATABASE_URL": args.database_url,
        "SILICON_NOTEBOOK_STORAGE_DIR": args.storage_dir,
        "SILICON_NOTEBOOK_AUTH_OPTIONAL": "true",
        "REASONING_REFLECT_V2_ENABLED": "true" if policy == "v2" else "false",
        "PORT": str(args.port),
    }


def _backend_command(args: argparse.Namespace) -> list[str]:
    return [
        "uvicorn", "app.main:app", "--port", str(args.port),
        "--app-dir", str(ROOT / "backend"),
    ]


def cmd_seed(args: argparse.Namespace, runner: Runner) -> int:
    questions = load_questions()
    runner.say("create database", f"{args.admin_url} :: CREATE DATABASE {args.db_name}")
    runner.say("install extensions", ", ".join(PG_EXTENSIONS))
    if not runner.dry_run:
        _create_test_database(args)

    work = runner.out_dir / "corpus"
    runner.say("rebuild corpus A (read-only on the source database)",
               f"--source-db-url=<given> notebook={args.source_notebook or '<required>'}")
    if runner.dry_run:
        runner.say("write", str(work / "A" / "<source_id>.md"))
    else:
        if not (args.source_db_url and args.source_notebook):
            print("ERROR: seed 需要 --source-db-url 与 --source-notebook",
                  file=sys.stderr)
            return 2
        for source_id, text in rebuild_source_markdown(
            args.source_db_url, args.source_notebook
        ).items():
            runner.write(work / "A" / f"{source_id}.md", text)

    corpus_dir = Path(args.corpus_dir or questions["corpus"]["B"]["dir"])
    for name in questions["corpus"]["B"]["files"]:
        runner.say("stage corpus B file", str(corpus_dir / name))

    runner.say("start backend",
               shlex.join(_backend_command(args))
               + "  env=" + ",".join(f"{k}={v}" for k, v in
                                     backend_env(args, "legacy").items()))
    if not runner.dry_run:
        _start_backend(args, "legacy")

    base = args.base_url
    runner.http("POST", f"{base}/api/auth/register",
                json_body={"username": args.user, "password": "<redacted>"})
    state = {"notebooks": {}}
    for cell in CORPUS_CELLS:
        runner.http("POST", f"{base}/api/notebooks", json_body={"name": cell})
        state["notebooks"][cell] = f"<{cell}>"
        runner.say("upload sources", f"{cell} <- corpus {cell[0]}")
        runner.http("POST", f"{base}/api/notebooks/<{cell}>/sources")
        if cell.endswith("_kg"):
            # 无图格刻意**不**建图:有图/无图 × 单篇/多篇 = 四个语料格,§2.3。
            runner.http("POST", f"{base}/api/notebooks/<{cell}>/kg/build")
            runner.say("await index-status ready",
                       f"GET {base}/api/notebooks/<{cell}>/index-status")
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
    env = dict(os.environ)
    env.update(backend_env(args, policy))
    process = subprocess.Popen(_backend_command(args), env=env, cwd=str(ROOT))
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        try:
            import urllib.request

            with urllib.request.urlopen(f"{args.base_url}/api/ready", timeout=5):
                return process
        except Exception:  # noqa: BLE001 — readiness poll, any failure = not ready
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
    for item in plan:
        notebook = state["notebooks"].get(item["corpus_cell"], "<unknown>")
        base = f"{args.base_url}/api/notebooks/{notebook}"
        runner.say(
            "ask",
            f"{item['question_key']} cell={item['corpus_cell']} "
            f"effort={item['effort']} mode={item['requested_mode']} "
            f"crid={item['client_request_id']}",
        )
        # 高级界面路径:先 /ask/intent 拿契约,再带着确认过的契约提交 /ask。
        # 只有这条路径才让方面账拿到真实的 mandatory_topics(§2.3)。
        contract = runner.http(
            "POST", f"{base}/ask/intent",
            json_body={"question": item["question"]},
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
        runner.http("POST", f"{base}/ask", json_body=body)
    return 0


def cmd_report(args: argparse.Namespace, runner: Runner) -> int:
    """4 道综述题 × 当前策略 × B 有图格,**进程内**调 `ReportEngine`。

    逐节轨迹不落库(`reports.sections_json` 只存结果级字段),所以这里不走
    HTTP,而是按 `scripts/replay_retrieval.py` 的构造方式在进程内拿到引擎,
    用一个只加 tee 的 `ReportEngine` 子类把 `_deep_dive` 的 `on_step` 复制一份
    进 JSONL。**子类住在 rig 里,生产代码一行不动。**
    """
    questions = load_questions()
    state = runner.load_state()
    plan = report_plan(
        questions, cell=args.cell[0] if args.cell else "B_kg",
        policy=args.policy, limit=args.limit,
    )
    out = runner.out_dir / f"report-trace-{args.policy}.jsonl"
    runner.say("policy", f"{args.policy}(进程内 Settings 由环境变量决定,"
                         "REASONING_REFLECT_V2_ENABLED 必须在启动前设好)")
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


def _run_reports(
    args: argparse.Namespace, runner: Runner, plan: Sequence[dict],
    state: dict, out: Path,
) -> int:
    from app.bootstrap import create_application_repository
    from app.core.config import Settings
    from app.core.request_context import reset_request_user, set_request_user
    from app.services.report_engine import ReportEngine

    class _TracingReportEngine(ReportEngine):
        """只做一件事:把每一步 `on_step` 复制一份给 rig,再原样转发。

        覆盖点选 `_deep_dive` 而不是 `generate`,因为 `on_step` 是
        `_run_sections` 内部构造的闭包,外面拿不到。转发保持逐字不变——进度文案、
        大纲计数、节流落库都还是原来那个回调在做,rig 只是在旁边接了根线。

        **按节归属靠 `section["title"]`**:`_run_sections` 可能并发跑各节
        (它自己拿着锁),所以「第几次调用 `_deep_dive`」不等于「第几节」;而
        `_deep_dive` 拿到的 `section` 就是大纲里那一节,标题正是它与
        `sections_json` 里那一行的对应关系。**标题只在进程内用来对号**,一个字
        都不写进 JSONL(写出去的只有 `section_index`)。
        """

        sink: Any = None

        def _deep_dive(self, notebook_id, section, question, depth=None,
                       on_step=None):
            title = str((section or {}).get("title") or "")

            def tee(step):
                self.sink(title, step)
                if on_step is not None:
                    on_step(step)

            return super()._deep_dive(notebook_id, section, question, depth, tee)

    repo = create_application_repository(Settings())
    profile = repo.maintenance.resolve_owner_profile(args.user)
    if profile is None:
        print(f"ERROR: 找不到 fixture 用户: {args.user}", file=sys.stderr)
        return 2
    token = set_request_user(profile)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out.open("a", encoding="utf-8") as handle:
            for item in plan:
                notebook = state["notebooks"][item["corpus_cell"]]
                report_id = repo.create_report(
                    notebook, item["question"], depth=item["depth"]
                )
                captured: dict[str, list[dict]] = {}

                def sink(title, step, _captured=captured):
                    _captured.setdefault(title, []).append({
                        "step_type": step.step_type,
                        "detail": dict(getattr(step, "detail", None) or {}),
                        "duration_ms": getattr(step, "duration_ms", None),
                    })

                engine = _TracingReportEngine(
                    _report_dependencies(repo), user_id=str(profile.id)
                )
                engine.sink = sink
                engine.generate(
                    notebook, report_id, item["question"], item["depth"]
                )
                for row in _report_rows(repo, notebook, report_id, item, captured):
                    assert_closed(row)
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                runner.say("report done", item["question_key"])
    finally:
        reset_request_user(token)
    runner.say("wrote report trace", str(out))
    return 0


def _report_dependencies(repo: Any) -> Any:
    """复用 runtime 已经接好的那份引擎依赖(端口、模型、事件日志)。

    刻意不在 rig 里重新装配一套 `ReportEngineDependencies`:那会变成第二份与
    生产分叉的接线,而 T0 要测的正是生产接线下的行为。
    """
    coordinator = repo._runtime.report_execution  # type: ignore[attr-defined]
    return coordinator.engine_factory(
        user_id="", cancel_event=None
    ).dependencies


def _report_rows(
    repo: Any, notebook: str, report_id: str, item: dict,
    captured: dict[str, list[dict]],
) -> Iterable[dict]:
    """逐节:一行**轨迹**投影 + 一行**结果级**投影,两者带同一个 `merge_key`。

    轨迹那一半走的是与 Ask 完全相同的 `project_run`——报告的逐节深挖跑的就是
    `ReasoningRetriever.run`,换一份投影只会让两边的口径分叉。结果级那一半走
    `project_report_section`,与 `export_reasoning_traces.py --reports` 是同一个
    函数,所以「rig 出的」和「从库里导的」不可能长出不同的键。

    `merge_key(report_id, section_index)` 是它们对上的唯一凭据:单向哈希,读不
    回 report id。
    """
    report = repo.get_report(notebook, report_id)
    sections = list(report.get("sections") or [])
    tags = {
        "corpus_cell": item["corpus_cell"],
        "question_key": item["question_key"],
        "consumer": "report_section",
        "trace_source": "in_process",
        # 轨迹那一行的 policy_version 由轨迹自己判(有没有 v2 终态步);结果级
        # 那一行没有轨迹可读,只能认 rig 声明的这一份。
        "policy_version": item["policy"],
    }
    for index, section in enumerate(sections):
        title = str((section or {}).get("title") or "")
        key = merge_key(report_id, index)
        steps = captured.get(title, [])
        trace_row = project_run(
            {"mode": "reasoning", "status": "done"}, steps, None,
            sources_count=None, rig_tags=tags,
        )
        trace_row["merge_key"] = key
        trace_row["section_index"] = index
        trace_row["section_total"] = len(sections)
        trace_row["report_depth"] = item["depth"]
        yield trace_row
        result_row = project_report_section(
            section, section_index=index, section_total=len(sections),
            report_depth=item["depth"], report_id=report_id, rig_tags=tags,
        )
        yield result_row


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


def cmd_teardown(args: argparse.Namespace, runner: Runner) -> int:
    runner.say("stop backend", f"port {args.port}")
    runner.say("drop database", f"{args.admin_url} :: DROP DATABASE {args.db_name}")
    if runner.dry_run:
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
    parser.add_argument("--base-url", default=f"http://127.0.0.1:{DEFAULT_PORT}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--storage-dir", default=".local/storage-t0")
    parser.add_argument("--out-dir", default=".local/t0")
    parser.add_argument("--user", default="t0fixture")
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
    runner = Runner(dry_run=args.dry_run, out_dir=Path(args.out_dir))
    handlers = {
        "seed": cmd_seed, "ask": cmd_ask, "report": cmd_report,
        "export": cmd_export, "teardown": cmd_teardown,
    }
    return handlers[args.command](args, runner)


if __name__ == "__main__":
    raise SystemExit(main())
