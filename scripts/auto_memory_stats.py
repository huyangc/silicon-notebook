#!/usr/bin/env python3
"""Read-only, content-free statistics for the automatic user-memory suggestions.

Design: docs/superpowers/specs/2026-10-28-auto-user-memory-design_zh.md section 7.1.
Prints ONE JSON document of counts, categories, statuses and codes. It never
selects a title, content, tag, question or answer column (the only payload it
touches is ``answers.payload``, and only inside the database, to count which
Memory ids the citation cards name); no memory id is printed either.

Works on both backends (SQLite ``--db PATH`` opened ``mode=ro`` + ``query_only``;
PostgreSQL ``--database-url URL`` opened ``read_only``), writes nothing, calls
no model. Run it on a production snapshot or the live database alike.

Usage:
  python scripts/auto_memory_stats.py --db .local/silicon_notebook.db
  python scripts/auto_memory_stats.py --database-url postgresql://... \\
      [--notebook-id nb-xxxx] [--since 2026-10-01]

What it reports (every key is a count, a rate or a code):

``suggestions.by_signal_status``
    suggestion rows (``memory_items.suggested_signal IS NOT NULL``) grouped by
    signal class x status (zero-filled; a signal value outside the contract
    folds into ``other``).

``suggestions.funnel`` (per signal class and ``all``)
    one bucket per suggestion, decided by the row's current status and its
    ``memory_revisions`` (the status partition is exhaustive):

    * ``pending``      status ``candidate`` (still waiting for a decision);
    * ``expired``      status ``rejected`` AND a revision with
                       ``change_reason = 'expired'`` (the 30-day sweep);
    * ``ignored``      status ``rejected`` without such a revision (the user
                       dismissed it);
    * ``adopted_as_is`` / ``adopted_edited``  status ``confirmed`` or
                       ``deprecated`` (a deprecated row was adopted first).
                       The *adoption revision* is the lowest-numbered
                       revision whose status is ``confirmed``; the *suggested
                       revision* is the row's first revision (written by the
                       system, ``change_reason = 'suggested'``). Adopted as is
                       = adoption revision and suggested revision carry the
                       same ``title`` + ``content_md`` + ``tags_json``; edited
                       = any of the three differs (an edit made while still a
                       candidate counts: the adoption revision then differs
                       from the first one);
    * ``adopted_unclassified`` adopted rows with no adoption revision to
                       compare (history lost) -- reported, never guessed.

    ``adoption_rate`` = adopted / (adopted + ignored + expired); ``None``
    while that denominator is 0.

``suggestions.deprecation_30d`` (per signal class and ``all``)
    cohort = adopted suggestions whose adoption revision is at least 30 days
    old at ``now`` (``matured``; younger ones cannot have had their 30 days).
    ``deprecated_within_30d`` = matured rows with a revision whose status is
    ``deprecated`` created within 30 days of the adoption revision.
    ``rate`` = deprecated_within_30d / matured (``None`` for an empty cohort).
    Deleted suggestions leave no row, so this under-counts "deprecated or
    deleted" (the design text's wording) by the deleted ones -- a deletion is
    not observable after the fact.

``jobs``
    ``memory_suggest_jobs`` rows: ``by_outcome`` (``last_outcome`` counts;
    the empty string of a row that never ran reads ``never_run``),
    ``failure_reasons`` (outcome -> code -> count; the column holds a reason
    code or an exception class name, anything else prints as ``other``),
    ``running`` rows, and the ``runs`` / ``candidates_written`` totals.

``citations`` (G22: the "cited N times" source)
    The persisted trace of an answer citing a Memory is the citation card:
    ``answers.payload -> 'citations'[*].'memory_id'`` (``Citation.memory_id``,
    set by ``AskService._memory_citations`` for each Memory anchor the answer
    cites; absent/empty on every other card). The script joins those ids to
    ``memory_items`` and counts, per group (``manual`` = a memory with no
    ``suggested_signal``; ``clarified`` / ``repeated`` / ``corrected`` = suggestions), the
    ``cards`` and the distinct ``answers`` holding them. Retrieval hits that
    never reached a card are not persisted anywhere, so "hit counts" are not
    reported. Reports (deep reports) cite through their own tables and are
    out of scope here.

Filters: ``--notebook-id`` restricts every table to that notebook;
``--since`` (ISO date or datetime, naive = UTC) keeps suggestions created, jobs
last updated and answers created at or after it. Suggestions and jobs are
filtered in Python on parsed instants (a naive stored time reads as UTC); the
answers filter runs in SQL, as a timestamptz comparison on PostgreSQL and as a
text comparison of ISO strings on SQLite (whose stored forms mix naive and
aware spellings, so the boundary can be off by the writer's UTC offset). All
values reach SQL as bound parameters.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.domain.memory_suggest_contracts import (  # noqa: E402
    JOB_OUTCOMES,
    SUGGESTED_SIGNALS,
)

#: ``memory_items.status`` values, in the order they are reported.
STATUSES = ("candidate", "confirmed", "rejected", "deprecated")
#: The deprecation window of the design's "采纳后 30 天内弃用率".
DEPRECATION_WINDOW = timedelta(days=30)
#: Funnel buckets, in report order.
FUNNEL_KEYS = (
    "proposed",
    "pending",
    "adopted_as_is",
    "adopted_edited",
    "adopted_unclassified",
    "ignored",
    "expired",
)
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
OTHER = "other"
MANUAL = "manual"


# ---------------------------------------------------------------- connection
def _open_sqlite(path: str) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(f"database file does not exist: {resolved}")
    db = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True, timeout=30.0
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _open_postgres(url: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise SystemExit("psycopg is required for PostgreSQL") from exc
    db = psycopg.connect(url, row_factory=dict_row)
    # the server refuses any write on this connection
    db.read_only = True
    return db


def _query(connection: Any, dialect: str, sql: str, params: tuple = ()) -> list[Any]:
    if dialect == "postgresql":
        sql = sql.replace("?", "%s")
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


# ------------------------------------------------------------------- helpers
def _instant(value: Any) -> Optional[datetime]:
    """A stored timestamp (SQLite text, PostgreSQL datetime) as a UTC instant;
    a naive one is read as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_since(text: str) -> datetime:
    moment = _instant(text)
    if moment is None:
        raise SystemExit(f"--since is not an ISO date or datetime: {text!r}")
    return moment


def _signal_key(value: Any) -> str:
    text = str(value) if value is not None else ""
    return text if text in SUGGESTED_SIGNALS else OTHER


def _code(value: Any) -> str:
    text = str(value) if value is not None else ""
    return text if _CODE.match(text) else OTHER


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _groups() -> tuple[str, ...]:
    return (*SUGGESTED_SIGNALS, OTHER)


# ----------------------------------------------------------------- suggestions
_SUGGESTION_SQL = """
SELECT m.suggested_signal AS signal,
       m.status AS status,
       m.created_at AS created_at,
       CASE WHEN EXISTS (
              SELECT 1 FROM memory_revisions r
              WHERE r.memory_id = m.id AND r.change_reason = 'expired'
            ) THEN 1 ELSE 0 END AS expired,
       adopt.created_at AS adopted_at,
       dep.created_at AS deprecated_at,
       CASE WHEN adopt.revision IS NULL OR base.revision IS NULL THEN NULL
            WHEN adopt.title = base.title
             AND adopt.content_md = base.content_md
             AND adopt.tags_json = base.tags_json THEN 0
            ELSE 1 END AS edited
FROM memory_items m
LEFT JOIN memory_revisions adopt
  ON adopt.memory_id = m.id AND adopt.revision = (
       SELECT MIN(x.revision) FROM memory_revisions x
       WHERE x.memory_id = m.id AND x.status = 'confirmed')
LEFT JOIN memory_revisions dep
  ON dep.memory_id = m.id AND dep.revision = (
       SELECT MIN(x.revision) FROM memory_revisions x
       WHERE x.memory_id = m.id AND x.status = 'deprecated')
LEFT JOIN memory_revisions base
  ON base.memory_id = m.id AND base.revision = (
       SELECT MIN(x.revision) FROM memory_revisions x WHERE x.memory_id = m.id)
WHERE m.suggested_signal IS NOT NULL{scope}
"""


def _funnel_bucket(row: Any) -> str:
    status = row["status"]
    if status == "candidate":
        return "pending"
    if status == "rejected":
        return "expired" if row["expired"] else "ignored"
    if row["edited"] is None:
        return "adopted_unclassified"
    return "adopted_edited" if row["edited"] else "adopted_as_is"


def _suggestion_stats(
    connection: Any, dialect: str, notebook_id: str, since: Optional[datetime],
    now: datetime,
) -> dict[str, Any]:
    scope, params = "", ()
    if notebook_id:
        scope, params = " AND m.notebook_id = ?", (notebook_id,)
    rows = _query(connection, dialect, _SUGGESTION_SQL.format(scope=scope), params)
    by_status = {g: {s: 0 for s in STATUSES} for g in _groups()}
    funnel = {g: {k: 0 for k in FUNNEL_KEYS} for g in (*_groups(), "all")}
    window = {
        g: {"adopted": 0, "matured": 0, "deprecated_within_30d": 0}
        for g in (*_groups(), "all")
    }
    for row in rows:
        created = _instant(row["created_at"])
        if since is not None and (created is None or created < since):
            continue
        group = _signal_key(row["signal"])
        status = row["status"] if row["status"] in STATUSES else None
        if status is not None:
            by_status[group][status] += 1
        if status is None:
            continue
        bucket = _funnel_bucket(row)
        for key in (group, "all"):
            funnel[key]["proposed"] += 1
            funnel[key][bucket] += 1
        if status not in ("confirmed", "deprecated"):
            continue
        adopted_at = _instant(row["adopted_at"])
        for key in (group, "all"):
            window[key]["adopted"] += 1
        if adopted_at is None or now - adopted_at < DEPRECATION_WINDOW:
            continue
        deprecated_at = _instant(row["deprecated_at"])
        for key in (group, "all"):
            window[key]["matured"] += 1
            if (
                deprecated_at is not None
                and deprecated_at - adopted_at <= DEPRECATION_WINDOW
            ):
                window[key]["deprecated_within_30d"] += 1
    for counts in funnel.values():
        decided = (
            counts["adopted_as_is"] + counts["adopted_edited"]
            + counts["adopted_unclassified"]
        )
        counts["adoption_rate"] = _rate(
            decided, decided + counts["ignored"] + counts["expired"]
        )
    for counts in window.values():
        counts["rate"] = _rate(counts["deprecated_within_30d"], counts["matured"])
    return {
        "by_signal_status": by_status,
        "funnel": funnel,
        "deprecation_30d": window,
    }


# ------------------------------------------------------------------------ jobs
_JOB_SQL = (
    "SELECT status, last_outcome, failure_reason, runs, candidates_written, "
    "updated_at FROM memory_suggest_jobs{scope}"
)


def _job_stats(
    connection: Any, dialect: str, notebook_id: str, since: Optional[datetime]
) -> dict[str, Any]:
    scope, params = "", ()
    if notebook_id:
        scope, params = " WHERE notebook_id = ?", (notebook_id,)
    rows = _query(connection, dialect, _JOB_SQL.format(scope=scope), params)
    outcomes = {code: 0 for code in JOB_OUTCOMES}
    outcomes["never_run"] = 0
    reasons: dict[str, dict[str, int]] = {}
    totals = {"jobs": 0, "running": 0, "runs": 0, "candidates_written": 0}
    for row in rows:
        updated = _instant(row["updated_at"])
        if since is not None and (updated is None or updated < since):
            continue
        totals["jobs"] += 1
        totals["running"] += 1 if row["status"] == "running" else 0
        totals["runs"] += int(row["runs"] or 0)
        totals["candidates_written"] += int(row["candidates_written"] or 0)
        raw = str(row["last_outcome"] or "")
        outcome = "never_run" if raw == "" else (raw if raw in JOB_OUTCOMES else OTHER)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        reason = str(row["failure_reason"] or "")
        if reason:
            bucket = reasons.setdefault(outcome, {})
            code = _code(reason)
            bucket[code] = bucket.get(code, 0) + 1
    return {
        **totals,
        "by_outcome": outcomes,
        "failure_reasons": {k: dict(sorted(v.items())) for k, v in sorted(reasons.items())},
    }


# ------------------------------------------------------------------- citations
_CITATION_SQLITE = """
SELECT COALESCE(m.suggested_signal, '') AS signal,
       COUNT(*) AS cards,
       COUNT(DISTINCT a.id) AS answers
FROM answers a,
     json_each(CASE WHEN json_valid(a.payload) THEN a.payload ELSE '{{}}' END,
               '$.citations') c
JOIN memory_items m ON m.id = json_extract(c.value, '$.memory_id')
WHERE 1 = 1{scope}
GROUP BY COALESCE(m.suggested_signal, '')
"""

_CITATION_POSTGRES = """
SELECT COALESCE(m.suggested_signal, '') AS signal,
       COUNT(*) AS cards,
       COUNT(DISTINCT a.id) AS answers
FROM answers a
CROSS JOIN LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(a.payload -> 'citations') = 'array'
            THEN a.payload -> 'citations' ELSE '[]'::jsonb END) AS c(value)
JOIN memory_items m ON m.id = (c.value ->> 'memory_id')
WHERE 1 = 1{scope}
GROUP BY COALESCE(m.suggested_signal, '')
"""


def _citation_stats(
    connection: Any, dialect: str, notebook_id: str, since: Optional[datetime]
) -> dict[str, Any]:
    scope, params = "", []
    if notebook_id:
        scope += " AND a.notebook_id = ?"
        params.append(notebook_id)
    if since is not None:
        scope += " AND a.created_at >= ?"
        params.append(since if dialect == "postgresql" else since.isoformat())
    template = _CITATION_POSTGRES if dialect == "postgresql" else _CITATION_SQLITE
    rows = _query(connection, dialect, template.format(scope=scope), tuple(params))
    groups = {g: {"cards": 0, "answers": 0} for g in (MANUAL, *_groups())}
    for row in rows:
        key = MANUAL if row["signal"] == "" else _signal_key(row["signal"])
        groups[key]["cards"] += int(row["cards"])
        # per-group distinct answers; an answer citing two groups counts in each
        groups[key]["answers"] += int(row["answers"])
    return groups


# ---------------------------------------------------------------------- report
def collect(
    connection: Any,
    dialect: str,
    *,
    notebook_id: str = "",
    since: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The whole report for one open (read-only) connection."""
    moment = now or datetime.now(timezone.utc)
    return {
        "read_only": True,
        "dialect": dialect,
        "filters": {
            "notebook_id": notebook_id or None,
            "since": since.isoformat() if since else None,
        },
        "suggestions": _suggestion_stats(connection, dialect, notebook_id, since, moment),
        "jobs": _job_stats(connection, dialect, notebook_id, since),
        "citations": _citation_stats(connection, dialect, notebook_id, since),    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--db", help="SQLite database path (opened read-only)")
    target.add_argument("--database-url", help="PostgreSQL connection URL (read-only)")
    parser.add_argument("--notebook-id", default="", help="restrict to one notebook")
    parser.add_argument("--since", default="", help="ISO date/datetime lower bound")
    args = parser.parse_args(argv)
    since = _parse_since(args.since) if args.since else None
    if args.db:
        connection, dialect = _open_sqlite(args.db), "sqlite"
    else:
        connection, dialect = _open_postgres(args.database_url), "postgresql"
    try:
        report = collect(
            connection, dialect, notebook_id=args.notebook_id, since=since
        )
    finally:
        if dialect == "postgresql":
            connection.rollback()
        connection.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
