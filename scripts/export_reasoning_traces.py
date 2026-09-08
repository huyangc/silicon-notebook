#!/usr/bin/env python3
"""reflect v2 开闸前 T0 · 只读导出:Ask 轨迹 / 报告段 → 闭集投影 JSONL。

设计真源 §2.1:
`docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md`

用法(`--database-url` **必填**,脚本不读 `.env`、不隐式连库):

    python scripts/export_reasoning_traces.py \
        --database-url postgresql://127.0.0.1:5432/silicon_notebook_local_pg16_live \
        --out /tmp/t0/legacy-baseline.jsonl
    python scripts/export_reasoning_traces.py \
        --database-url .local/silicon_notebook.db --reports --out /tmp/t0/sqlite.jsonl

**读法**:最小只读 SQL,不经 repository 端口。理由有三条,都是这个脚本特有的:
(1) 端口构造要 `Settings()`,而 `Settings()` 按 CWD 读 `.env` —— 那正是本脚本
被要求**不许**做的事(`--database-url` 必填就是为了不让连错库这种事发生在别人
的生产库上);(2) 端口是读写门面,而这里必须只读:PG 侧连接开
`read_only`,SQLite 侧用 `mode=ro` URI,这两道闸在端口后面挂不上;(3) 要读的
只有五张表的几列,SQL 比把整个 app 拉起来窄得多。**收窄规则一行都不在这里**
——它全在 `app.domain.reasoning_trace_stats`,与 rig 和分析脚本共用同一份。

双后端:两侧跑同一段 SQL,只有占位符(`%s` / `?`)不同;`jsonb` 与 TEXT 的差异
在 SQL 里用 `CAST(... AS TEXT)` 抹平,解码统一由投影侧的 `_mapping` 做。

**输出里没有**:问题原文、答案正文、来源标题、任何 id。`--notebook` 只做过滤,
不落到输出里(输出只有来源数分桶 `notebook_bucket`)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain.reasoning_trace_stats import (  # noqa: E402
    assert_closed,
    assert_projection_values,
    project_report_section,
    project_run,
)

#: rig 把「题号/语料格/策略/档位」编进 `ask_jobs.client_request_id`。格式与
#: `scripts/reflect_shadow_rig.py::encode_client_request_id` 是同一份合同,由
#: `backend/tests/test_reflect_t0_scripts.py` 双向钉住。
RIG_PREFIX = "t0:"
RIG_FIELDS = ("question_key", "corpus_cell", "policy", "effort", "requested_mode")


def decode_client_request_id(raw: object) -> dict:
    """`t0:<题号>:<语料格>:<策略>:<档位>:<请求 mode>` → 标签字典。

    不是 rig 发的(线上真实提问、或别的客户端的幂等键)就返回空字典 —— 那些 run
    的 `corpus_cell` / `question_key` 恒为 unknown,只进基线表不进对照表(§4.2)。
    解析失败同样返回空字典:一个畸形的幂等键不该让整次导出失败。
    """
    text = str(raw or "")
    if not text.startswith(RIG_PREFIX):
        return {}
    parts = text[len(RIG_PREFIX):].split(":")
    if len(parts) != len(RIG_FIELDS):
        return {}
    # 每一段都要形如短码(≤64 个 `[A-Za-z0-9_.+-]`):API 允许 128 字的幂等键,
    # 一段 65 个字符的「题号」能过 API 校验却会让 `project_run` 的值形状守卫抛
    # ValueError,一条这样的落库 job 就打死整次导出(codex #700 R20 P2)。畸形
    # 标签按「不是 rig 发的」处理:进基线表,不进对照表。
    if not all(_RIG_SEGMENT.match(part) for part in parts):
        return {}
    return dict(zip(RIG_FIELDS, parts))


_RIG_SEGMENT = re.compile(r"^[A-Za-z0-9_.+\-]{1,64}$")


class _Reader:
    """两个后端共用的最小只读查询面。"""

    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.is_postgres = database_url.startswith(("postgres://", "postgresql://"))
        self.placeholder = "%s" if self.is_postgres else "?"
        self._conn: Any = None

    def __enter__(self) -> "_Reader":
        if self.is_postgres:
            import psycopg

            self._conn = psycopg.connect(self.url)
            # 只读闸:任何 INSERT/UPDATE/DDL 在服务端直接被拒,而不是靠这个脚本
            # 自觉不写。导出跑在别人的主库上,自觉不算保证。
            self._conn.read_only = True
        else:
            import sqlite3

            path = self.url
            for prefix in ("sqlite:///", "sqlite://", "file:"):
                if path.startswith(prefix):
                    path = path[len(prefix):]
                    break
            # 文件名先百分号编码再拼 URI(codex #700 R17 P2):路径里的 `?`/`#`/`%`
            # 直接内插会改变 URI 含义——`#` 会把后面的 `mode=ro` 当片段丢掉,
            # 以默认可写方式打开(甚至新建)另一个文件。
            self._conn = sqlite3.connect(
                f"{Path(path).resolve().as_uri()}?mode=ro", uri=True
            )
            self._conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        sql = sql.replace("?", self.placeholder)
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, tuple(params))
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()


def _in_clause(reader: _Reader, values: Sequence[str]) -> str:
    return ", ".join([reader.placeholder] * len(values))


def _batched(values: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _fetch_jobs(reader: _Reader, notebooks: Sequence[str]) -> list[dict]:
    sql = (
        "SELECT id, notebook_id, mode, status, answer_id, client_request_id, "
        "CAST(created_at AS TEXT) AS created_at, "
        "CAST(trace_json AS TEXT) AS trace_json FROM ask_jobs"
    )
    params: list[Any] = []
    if notebooks:
        sql += f" WHERE notebook_id IN ({_in_clause(reader, notebooks)})"
        params = list(notebooks)
    return reader.query(sql + " ORDER BY id", params)


def _fetch_traces(reader: _Reader, job_ids: Sequence[str]) -> dict[str, list]:
    traces: dict[str, list] = {}
    for batch in _batched(job_ids):
        rows = reader.query(
            "SELECT job_id, CAST(step_json AS TEXT) AS step_json "
            f"FROM ask_trace_steps WHERE job_id IN ({_in_clause(reader, batch)}) "
            "ORDER BY job_id, seq",
            batch,
        )
        for row in rows:
            traces.setdefault(row["job_id"], []).append(row["step_json"])
    return traces


def _fetch_answers(reader: _Reader, answer_ids: Sequence[str]) -> dict[str, str]:
    payloads: dict[str, str] = {}
    for batch in _batched(answer_ids):
        rows = reader.query(
            "SELECT id, CAST(payload AS TEXT) AS payload FROM answers "
            f"WHERE id IN ({_in_clause(reader, batch)})",
            batch,
        )
        payloads.update({row["id"]: row["payload"] for row in rows})
    return payloads


def _fetch_source_counts(reader: _Reader) -> dict[str, int]:
    rows = reader.query(
        "SELECT notebook_id, COUNT(*) AS n FROM sources GROUP BY notebook_id"
    )
    return {row["notebook_id"]: int(row["n"]) for row in rows}


def _in_window(created_at: object, since: str | None, until: str | None) -> bool:
    """`--since/--until` 的窗口判定,在 Python 侧做而不是在 SQL 里。

    `created_at` 在 PG 是 timestamptz、在 SQLite 是 TEXT,而且 PG 侧允许 NULL;
    两边各写一条日期比较就是两条会分叉的规则。统一取 `CAST(... AS TEXT)` 之后
    按 ISO 前缀比较:两个后端的文本形态都以 `YYYY-MM-DD` 开头,而窗口参数本身
    就是日期。没有时间戳的行(NULL)在给了窗口时被排除——不是「一定在窗口内」。
    """
    if since is None and until is None:
        return True
    text = str(created_at or "")
    if not text:
        return False
    if since is not None and text[:len(since)] < since:
        return False
    if until is not None and text[:len(until)] > until:
        return False
    return True


def export_ask_runs(
    reader: _Reader, *, notebooks: Sequence[str],
    since: str | None, until: str | None,
) -> list[dict]:
    jobs = [
        job for job in _fetch_jobs(reader, notebooks)
        if _in_window(job.get("created_at"), since, until)
    ]
    traces = _fetch_traces(reader, [job["id"] for job in jobs])
    answer_ids = [job["answer_id"] for job in jobs if job.get("answer_id")]
    payloads = _fetch_answers(reader, answer_ids)
    source_counts = _fetch_source_counts(reader)

    rows: list[dict] = []
    for job in jobs:
        steps = traces.get(job["id"])
        source = "trace_steps"
        if not steps:
            # 回退到退役的 `ask_jobs.trace_json` 单列(append_ask_trace 起停止
            # 写入,只有子表出现之前的旧行还带内容),并如实打标,以便聚合侧知道
            # 这一行的轨迹来自哪里。
            legacy = job.get("trace_json")
            decoded = json.loads(legacy) if isinstance(legacy, str) and legacy else []
            steps = decoded if isinstance(decoded, list) else []
            if steps:
                source = "legacy_column"
        tags = decode_client_request_id(job.get("client_request_id"))
        tags["trace_source"] = source
        row = project_run(
            job,
            steps,
            payloads.get(job.get("answer_id") or ""),
            sources_count=source_counts.get(job.get("notebook_id")),
            rig_tags=tags,
        )
        assert_closed(row)
        assert_projection_values(row)
        rows.append(row)
    return rows


def export_report_sections(
    reader: _Reader, *, notebooks: Sequence[str],
    since: str | None, until: str | None,
) -> list[dict]:
    sql = (
        "SELECT id, notebook_id, depth, "
        "CAST(created_at AS TEXT) AS created_at, "
        "CAST(sections_json AS TEXT) AS sections_json FROM reports"
    )
    params: list[Any] = []
    if notebooks:
        sql += f" WHERE notebook_id IN ({_in_clause(reader, notebooks)})"
        params = list(notebooks)
    rows: list[dict] = []
    for report in reader.query(sql + " ORDER BY id", params):
        if not _in_window(report.get("created_at"), since, until):
            continue
        raw = report.get("sections_json")
        sections = json.loads(raw) if isinstance(raw, str) and raw else []
        if not isinstance(sections, list):
            continue
        for index, section in enumerate(sections):
            row = project_report_section(
                section,
                section_index=index,
                section_total=len(sections),
                report_depth=report.get("depth"),
                report_id=report.get("id"),
            )
            assert_closed(row)
            assert_projection_values(row)
            rows.append(row)
    return rows


def write_jsonl(rows: Iterable[dict], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            assert_closed(row)
            assert_projection_values(row)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url", required=True,
        help="PostgreSQL URL 或 SQLite 文件路径。必填:本脚本不读 .env",
    )
    parser.add_argument("--out", required=True, help="输出 JSONL 路径")
    parser.add_argument("--since", help="created_at 下界(ISO 日期前缀,含)")
    parser.add_argument("--until", help="created_at 上界(ISO 日期前缀,含)")
    parser.add_argument(
        "--notebook", action="append", default=[],
        help="只导出这些笔记本(可多次)。只做过滤,id 不进输出",
    )
    parser.add_argument(
        "--reports", action="store_true",
        help="同时导出 reports.sections_json 的结果级行(consumer=report_section)",
    )
    args = parser.parse_args(argv)

    with _Reader(args.database_url) as reader:
        rows = export_ask_runs(
            reader, notebooks=args.notebook, since=args.since, until=args.until,
        )
        if args.reports:
            rows += export_report_sections(
                reader, notebooks=args.notebook,
                since=args.since, until=args.until,
            )
    count = write_jsonl(rows, Path(args.out))
    print(f"exported {count} projection rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
