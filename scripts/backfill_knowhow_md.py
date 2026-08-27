#!/usr/bin/env python3
"""knowhow-md-normalize Task 6: one-time backfill for existing (存量) knowhow
cells' Markdown -- cleans up Excel copy-paste idioms (Tab-indented `•`
bullets, `A.`/`a.` section/sub markers, soft line breaks) that Task 5 already
normalizes for brand-new imports/appends, but that were never applied to
cells created before that change landed.

**Dry-run by default**: prints a per-cell before/after/source report plus a
summary, ALWAYS serializes the plan to a JSON file (whose path it prints
prominently), and writes NOTHING to the database. EVERY ``--apply`` REQUIRES
``--plan PATH`` and re-applies THAT reviewed plan file verbatim, so what lands is
exactly what a human reviewed -- never a fresh re-plan. A plan-less ``--apply``
would re-plan from the CURRENT database at apply time, so a cell edited after the
reviewed dry-run would enter the fresh plan with its current ``before``, pass the
guarded write, and get written despite never being reviewed (and for ``--use-llm``
the stochastic rewrite model would produce different candidates entirely) -- so it
is a hard error.

**The DEFAULT dry-run is fully read-only**: it opens the database DIRECTLY
through a ``mode=ro`` sqlite3 connection and NEVER constructs the write-capable
``SQLiteRepository`` -- whose ``__init__`` runs migrations/seed/crash-recovery
(``_recover_interrupted_jobs`` flips any lingering ``'pending'``/``'syncing'``
row to ``'failed'``), so even a "writes nothing" dry-run could otherwise mutate
schema/state. The deterministic rule normalizer is a pure function of the cell
content, so no repository (and no model resolution) is needed to plan it. Only
``--use-llm`` (needs the system ``knowhow_reformat`` workload) and ``--apply`` (writes)
construct the repository, and both print a one-line notice first: opening the
database read-write may run pending migrations/recovery, so run it when the
backend is idle.

``--apply`` is atomic (one store-level write transaction for the whole
reviewed plan -- a mid-run failure leaves zero cells modified, not an
arbitrarily partial backfill) and, on success, SYNCHRONOUSLY reprojects every
touched table in-process before this process exits (never a background
schedule -- a still-'pending' row would otherwise be flipped to 'failed' the
next time any process constructs the repository; see
``reproject_changed_tables``'s own docstring). A cell whose CURRENT stored
content no longer matches what the plan recorded as its ``before`` (someone
edited it after the review) is SKIPPED and reported -- never written on top of
a moved target.

用法（须在主 checkout 根目录跑 —— 需要真实的 .env / DB 配置，worktree 里没有）：
  cd /path/to/silicon_notebook
  PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx
      # dry-run（默认）：打印计划 + 写出 plan 文件（.local/backfill_plans/...），不写库

  PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --apply --plan <plan.json>
      # 按评审过的 plan 文件逐条写入（规则规整，零 LLM）

  PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --use-llm
      # dry-run + 走 LLM 重排（生成 plan 文件供评审）
  PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --use-llm --apply --plan <plan.json>
      # 评审无误后，按该 plan 文件写入

任何 ``--apply`` 都【必须】带 ``--plan``（不带即【硬错误】）：apply 时不带 plan 就地
重新规划会从【当前】库重新读取，dry-run 评审之后被改过的格子会带着当前内容进入新计划、
通过守卫写入却从未被评审（``--use-llm`` 更甚——改写模型随机，重新规划连候选都会不同）。
dry-run 永远会写出 plan 文件，供 ``--apply --plan`` 指向。见 main() 的守卫。

默认（不加 --use-llm）直接调 rule_normalize（Task 1，确定性规则、零 LLM）——批量
存量回填的默认路径必须可预测、零调用成本，绝不触发任何模型请求。加 --use-llm
才改走 reformat_cell；若该次运行改写模型实际未配置（source == "rule/no-llm"）或
LLM 结果未过校验已退回规则（source == "rule/llm-failed"），本脚本据此打印醒目
WARNING，不悄悄假装 LLM 生效了（见 docs/operations*.md 的运维失败边界）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote

from app.core.config import Settings
from app.services.knowhow import md_normalize
from app.services.knowhow.api import build_projector, reformat_cell
from app.services.sqlite_repository import SQLiteRepository


def _build_plan(tables, use_llm: bool, reformat_fn) -> list[dict]:
    """Shared plan core over an iterable of ``get_knowhow_table``-shaped table
    dicts (each carrying ``id`` + ``columns:[{id,name,role}]`` +
    ``rows:[{id, cells:{column_id: content_md}}]``). Produces one plan entry per
    NON-EMPTY cell: ``{table_id, row_id, column_id, before, after, source,
    changed}``. Pure/read-only -- never writes anything.

    Parameterizing the data source (rather than the repository) is what lets the
    live repo path (``plan_backfill``) and the read-only path
    (``plan_backfill_readonly``) produce BYTE-IDENTICAL plans: both feed the same
    per-table shape here. ``reformat_fn(before, column_name, column_role)`` is
    invoked ONLY when ``use_llm=True`` (the read-only path passes ``None`` and
    only ever uses ``use_llm=False``).

    ``use_llm=False`` (the default) goes through ``md_normalize.rule_normalize``
    -- it never invokes the LLM at all, not even indirectly, so the default
    bulk-backfill path stays free and predictable. ``use_llm=True`` instead calls
    ``reformat_fn`` -> ``reformat_cell`` (LLM reformat -> content-invariance
    check -> rule fallback), whose own ``source`` distinguishes ``"llm"`` /
    ``"rule/llm-failed"`` / ``"rule/no-llm"``.

    ``use_llm=True`` memoizes on ``(column_id, before)`` -- anchor-grouped
    tables forward-fill one shared value across sibling rows, so the exact
    same cell content routinely shows up in multiple rows of the same
    column. ``reformat_cell`` is a pure function of ``(content_md,
    column_name, kind)`` (no row identity), so re-invoking it per duplicate
    row wastes an LLM call every time AND -- since that LLM runs at
    temperature=1.0 with no caching of its own -- would hand back a
    DIFFERENT, individually-valid rewrite for each identical input,
    gratuitously making previously-identical sibling cells diverge after
    backfill. Reusing the first result for every later occurrence of the
    same (column, content) pair fixes both. The ``use_llm=False`` path
    (``rule_normalize``) is already a deterministic pure function of
    ``before`` alone, so it needs no memoization to produce identical
    output for identical input -- left untouched.

    final-review fix (Critical 1, layer 2 -- defense in depth): the
    ``use_llm=False`` path now goes through ``md_normalize.safe_rule_normalize``
    instead of calling ``rule_normalize`` directly -- it gates the rule
    candidate through ``content_invariant`` before ever surfacing it. This is
    belt-and-suspenders against the root fix (verbatim block passthrough in
    ``_normalize`` itself): if ``rule_normalize`` ever mis-normalizes a cell
    again, the corrupted candidate must not silently become ``after`` and
    later get written to the database. A gated cell reports
    ``source="rule/invariant-failed"`` (distinct from plain ``"rule"``) with
    ``after == before`` (unchanged) so it surfaces separately in
    ``_print_plan``'s by-source summary as needing manual attention, rather
    than either silently "fixing" it wrong or silently doing nothing
    indistinguishable from an already-clean cell.
    """
    plan: list[dict] = []
    reformat_cache: dict[tuple[str, str], dict] = {}
    for table in tables:
        for row in table["rows"]:
            for column in table["columns"]:
                before = row["cells"].get(column["id"])
                if not before or not before.strip():
                    continue  # empty cell: nothing to normalize
                # P1-c: the anchor column is EXEMPT from normalization (both the
                # rule and the LLM path) — anchor = 分组键，必须字节稳定；规整它
                # 会让新行与旧行的键失配、组被劈开 (the same skip import_table/
                # commit_append apply in api.py). Surface it as a distinct,
                # byte-stable ``source="anchor"`` changed=False entry so the
                # by-source summary shows it was deliberately left untouched
                # (not silently dropped, not falsely reported as rule-normalized).
                if column["role"] == "anchor":
                    plan.append({
                        "table_id": table["id"],
                        "row_id": row["id"],
                        "column_id": column["id"],
                        "before": before,
                        "after": before,
                        "source": "anchor",
                        "changed": False,
                    })
                    continue
                if use_llm:
                    cache_key = (column["id"], before)
                    result = reformat_cache.get(cache_key)
                    if result is None:
                        result = reformat_fn(before, column["name"], column["role"])
                        reformat_cache[cache_key] = result
                    after = result["candidate_md"]
                    source = result["source"]
                    changed = result["changed"]
                else:
                    after, used_rule = md_normalize.safe_rule_normalize(before)
                    source = "rule" if used_rule else "rule/invariant-failed"
                    changed = after != before
                plan.append({
                    "table_id": table["id"],
                    "row_id": row["id"],
                    "column_id": column["id"],
                    "before": before,
                    "after": after,
                    "source": source,
                    "changed": changed,
                })
    return plan


def plan_backfill(repo, notebook_id: str, use_llm: bool = False) -> list[dict]:
    """Plan the backfill by reading every knowhow table in ``notebook_id`` from
    the LIVE repository (``list_knowhow_tables``/``get_knowhow_table``) and
    delegating to ``_build_plan``. Read-only -- never writes (even
    ``reformat_cell`` under ``use_llm=True`` is suggestion-only). ``use_llm=True``
    needs the repository (its system workload provider), so it stays on this
    path; the DEFAULT rules-only dry-run instead uses the repository-free
    ``plan_backfill_readonly`` (see ``main``)."""
    tables = (
        repo.get_knowhow_table(summary["id"])
        for summary in repo.list_knowhow_tables(notebook_id)
    )
    reformat_fn = (
        (lambda before, name, role: reformat_cell(repo, before, name, role))
        if use_llm
        else None
    )
    return _build_plan(tables, use_llm, reformat_fn)


def _read_knowhow_tables_ro(conn: sqlite3.Connection, notebook_id: str) -> list[dict]:
    """Read every knowhow table (columns + rows + cells) for ``notebook_id`` from
    a read-only connection, returning the SAME per-table shape
    ``KnowhowStore.get_knowhow_table`` produces for the fields ``_build_plan``
    consumes (``id`` + ``columns:[{id,name,role,position}]`` +
    ``rows:[{id, cells:{column_id: content_md}}]``). Mirrors
    ``list_knowhow_tables``'s ``ORDER BY created_at, id`` and that store's own
    per-table column/row/cell queries (the store is the reference) so the plan is
    byte-identical to the live-repo path -- but never constructs the write-capable
    repository (P2). Cells are sparse exactly like the store: a never-edited cell
    is simply absent from the row's ``cells`` map."""
    table_rows = conn.execute(
        "SELECT id FROM knowhow_tables WHERE notebook_id = ? ORDER BY created_at, id",
        (notebook_id,),
    ).fetchall()
    tables: list[dict] = []
    for table_row in table_rows:
        table_id = table_row["id"]
        column_rows = conn.execute(
            "SELECT id, name, role, position FROM knowhow_columns "
            "WHERE table_id = ? ORDER BY position, id",
            (table_id,),
        ).fetchall()
        row_rows = conn.execute(
            "SELECT id, position FROM knowhow_rows WHERE table_id = ? ORDER BY position, id",
            (table_id,),
        ).fetchall()
        row_ids = [row["id"] for row in row_rows]
        cells_by_row: dict[str, dict[str, str]] = {rid: {} for rid in row_ids}
        if row_ids:
            placeholders = ",".join("?" for _ in row_ids)
            cell_rows = conn.execute(
                "SELECT row_id, column_id, content_md FROM knowhow_cells "
                f"WHERE row_id IN ({placeholders})",
                row_ids,
            ).fetchall()
            for cell_row in cell_rows:
                cells_by_row[cell_row["row_id"]][cell_row["column_id"]] = (
                    cell_row["content_md"]
                )
        tables.append({
            "id": table_id,
            "columns": [
                {
                    "id": column["id"],
                    "name": column["name"],
                    "role": column["role"],
                    "position": column["position"],
                }
                for column in column_rows
            ],
            "rows": [
                {"id": row["id"], "cells": cells_by_row[row["id"]]}
                for row in row_rows
            ],
        })
    return tables


def plan_backfill_readonly(db_path: str, notebook_id: str) -> list[dict]:
    """Rules-only plan built by reading the database DIRECTLY through a
    ``mode=ro`` connection -- the DEFAULT dry-run path, which must NEVER
    construct the write-capable ``SQLiteRepository`` (whose ``__init__`` runs
    migrations/seed/crash-recovery; ``_recover_interrupted_jobs`` flips lingering
    ``'pending'`` rows to ``'failed'``, an incident actually seen in production).
    ``rule_normalize`` is a pure function of the cell content, so no repository
    and no model resolution are needed here, and the resulting plan is
    byte-identical to ``plan_backfill(repo, notebook_id, use_llm=False)`` (they
    share ``_build_plan``). ``--use-llm`` keeps going through the repository path
    (it needs the system ``knowhow_reformat`` workload provider)."""
    # Percent-encode the filesystem path (safe="/" keeps path separators) so a
    # path containing ``?`` or ``%`` cannot truncate the URI or drop ``mode=ro``
    # (which would open a DIFFERENT file, or open THIS file WRITE-capable —
    # violating the dry-run read-only guarantee). ``quote`` mirrors
    # app/api/routes.py's Content-Disposition encoding.
    conn = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = _read_knowhow_tables_ro(conn, notebook_id)
    finally:
        conn.close()
    return _build_plan(tables, use_llm=False, reformat_fn=None)


def reproject_changed_tables(repo, plan: list[dict]) -> list[str]:
    """P1-3 code review fix (most important): after a successful
    ``--apply``, run ``KnowhowProjector.project_table`` SYNCHRONOUSLY,
    IN-PROCESS, once per DISTINCT ``table_id`` that owns at least one
    ``changed=True`` entry -- ``apply_reviewed_plan`` only marks each written
    row's ``projection_status`` ``'pending'``; nothing else in this
    one-shot CLI process ever schedules or RUNS the projector the way the
    HTTP PATCH-cell route does (that route's handler calls
    ``knowhow_api.get_scheduler(repo).schedule(table_id)`` after every
    write, then the process keeps running so the debounced background job
    eventually fires). Left at ``'pending'``, a row is not just stale: the
    NEXT time ANY process constructs ``SQLiteRepository``,
    ``migrations.py::_recover_interrupted_jobs`` treats a still-``'pending'``
    (or ``'syncing'``) row as an abandoned crash artifact and flips it to
    ``'failed'`` -- this is a confirmed production incident, not a
    theoretical one (10 rows in a real notebook went to ``'failed'`` after
    running this script).

    Scheduling via ``knowhow_api.get_scheduler(repo).schedule(table_id)``
    instead would NOT fix this: that scheduler debounces ``0.5s`` then hands
    the actual run to ``app.services.background_jobs`` -- a fire-and-forget
    background thread this short-lived CLI process would exit right past,
    before the run ever started (exactly the bug the task calls out).
    ``project_table`` itself is the SAME full deterministic pass regardless
    of caller (scheduler or direct) -- calling it directly here simply skips
    the part of the HTTP path that exists only to debounce/coalesce rapid
    successive edits, which this CLI does not need (it already applies the
    whole reviewed plan first, then reprojects once per table, after the
    fact).

    Returns the table_ids actually reprojected (in first-seen order, each
    exactly once), for ``main()`` to report."""
    table_ids = list(dict.fromkeys(
        entry["table_id"] for entry in plan if entry["changed"]
    ))
    projector = build_projector(repo)
    for table_id in table_ids:
        projector.project_table(table_id)
    return table_ids


def _print_plan(plan: list[dict]) -> None:
    for entry in plan:
        marker = "CHANGED" if entry["changed"] else "same   "
        print(
            f"[{marker}] table={entry['table_id']} row={entry['row_id']} "
            f"column={entry['column_id']} source={entry['source']}"
        )
        if entry["changed"]:
            print(f"    before: {entry['before']!r}")
            print(f"    after:  {entry['after']!r}")
    changed = [entry for entry in plan if entry["changed"]]
    by_source = Counter(entry["source"] for entry in plan)
    print("---")
    print(f"总格子数: {len(plan)}  将改变: {len(changed)}")
    print(f"来源计数: {dict(by_source)}")


# ---------------------------------------------------------------------------
# P1-a: the dry-run -> review -> apply plan-file handshake. Dry-run ALWAYS
# writes the plan to a JSON file; ``--apply --plan`` re-applies THAT file
# verbatim, so what lands is exactly what a human reviewed (never a fresh,
# possibly-different re-plan -- see main()'s --use-llm guard).
# ---------------------------------------------------------------------------


# F3 (review): bound the collision-suffix retry so a pathological run can't spin
# forever -- a plausibly-tiny cap, since microseconds+pid already make a genuine
# tie astronomically rare; exceeding it is a hard error, never a silent clobber.
_MAX_PLAN_PATH_ATTEMPTS = 100


def _default_plan_path(notebook_id: str, created_at: datetime) -> pathlib.Path:
    """``.local/backfill_plans/knowhow_md_<notebook_id>_<UTC ts>_<pid>.json``
    relative to the process CWD (this script is documented to run from the main
    checkout root).

    F3 (review): the timestamp now carries MICROSECONDS (``%f``) and the filename
    the PID, so two dry-runs within the same wall-clock SECOND -- or two
    concurrent processes -- no longer resolve to one path and silently clobber a
    reviewed plan (the old ``%Y%m%dT%H%M%SZ``-only name did). ``save_plan``
    additionally opens O_EXCL and disambiguates on the vanishingly rare
    exact-microsecond+pid tie."""
    # 局部 import：本文件 1-354 行的行号被架构守卫（SQLITE_CONNECT_SITES /
    # INDEPENDENT_SQL_SITES / FACADE_CLASS_IMPORT_SITES）逐行冻结，顶层新增
    # import 会整体下移它们、逼出无谓的守卫 re-pin；os 仅此处需要（getpid），
    # 局部引入把行号扰动限制在本函数之后（那里没有被冻结的站点）。
    import os
    ts = created_at.strftime("%Y%m%dT%H%M%S_%fZ")
    return pathlib.Path(".local") / "backfill_plans" / f"knowhow_md_{notebook_id}_{ts}_{os.getpid()}.json"


def save_plan(
    plan: list[dict], notebook_id: str, use_llm: bool, created_at: datetime,
    path: "str | None" = None,
) -> pathlib.Path:
    """Serialize the plan (header + entries) to JSON and return the path it was
    written to. ``path`` overrides the default (``--save-plan``); its parent
    directory is created if missing.

    F3 (review): for the DEFAULT path, open with ``x`` mode (O_EXCL) and, on
    ``FileExistsError``, retry with a bounded ``-1``/``-2``... suffix -- so two
    plans that map to the identical default path get DISTINCT files and a
    reviewed plan is never silently overwritten. An explicit ``--save-plan PATH``
    is the caller's deliberate choice and keeps overwrite semantics."""
    payload = json.dumps(
        {
            "notebook_id": notebook_id,
            "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "use_llm": use_llm,
            "entries": plan,
        },
        ensure_ascii=False,
        indent=2,
    )
    if path is not None:                              # explicit path: caller owns it
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        return target
    base = _default_plan_path(notebook_id, created_at)
    base.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_MAX_PLAN_PATH_ATTEMPTS):
        candidate = base if attempt == 0 else base.with_name(f"{base.stem}-{attempt}{base.suffix}")
        try:
            with open(candidate, "x", encoding="utf-8") as fh:   # O_EXCL: fail if exists
                fh.write(payload)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(
        f"无法为回填计划分配唯一文件名（已尝试 {_MAX_PLAN_PATH_ATTEMPTS} 次）：{base}"
    )


def load_plan(path: str) -> dict:
    """Read a plan file back into its ``{notebook_id, created_at, use_llm,
    entries}`` document."""
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def apply_reviewed_plan(
    repo, notebook_id: str, entries: list[dict], actor: str = "",
) -> "tuple[list[dict], list[dict], list[dict]]":
    """Apply a plan's entries VERBATIM (P1-a), with the moved-target check AND
    the membership check done ATOMICALLY inside the write transaction (P1 TOCTOU
    fix + F2 membership fix). For each ``changed=True`` entry, the reviewed
    ``after`` is staged EXACTLY as reviewed (no re-planning, no fresh
    normalization) and handed to ``update_knowhow_cells_bulk_guarded`` together
    with ``notebook_id``, which -- under the write lock -- (a) verifies the
    entry's row/column both belong to the claimed table and that table belongs
    to ``notebook_id`` (else REJECTED), then (b) re-reads each cell and writes it
    ONLY if its CURRENT content still equals the plan's recorded ``before`` (else
    SKIPPED); neither a reject nor a skip overwrites anything.

    Every ``--apply`` reaches here through ``--plan`` (a reviewed plan file) --
    ``main`` rejects a plan-less ``--apply`` up front, so what is staged is always
    exactly the reviewed ``after``, never a fresh re-plan. (Tests also drive this
    guarded writer directly with an in-process plan to exercise the moved-target /
    membership paths without the CLI handshake.)

    The compare-and-write MUST be atomic. The earlier implementation read the
    current cells, compared against ``before``, THEN called the plain bulk write
    in a separate step -- a cell a live backend user edited BETWEEN that read and
    the write passed the stale comparison and got overwritten anyway. The guarded
    store method moves the re-read into the same transaction as the write, so the
    "post-review edits are skipped" guarantee actually holds. The skip/reject
    report below therefore comes from the transaction's RETURN VALUE, not a CLI
    pre-read.

    The content_invariant defense rail still runs against every staged write
    (belt-and-suspenders, same as ``md_normalize.safe_rule_normalize`` on the
    live paths): a reviewed ``after`` that somehow fails ``content_invariant``
    against its ``before`` -- e.g. a ``rule/llm-failed`` fallback candidate a
    rule_normalize bug corrupted -- is skipped rather than written. That check is
    a PURE function of the plan's own ``(before, after)`` (it reads no DB state),
    so it stays a CLI-level pre-filter -- only the moved-target and membership
    checks, which depend on live DB state, run inside the transaction.

    Returns ``(applied, already_applied, skipped, rejected)`` where ``applied`` is
    the entries actually written and ``already_applied`` (F4) is the entries whose
    cell ALREADY held their reviewed ``after`` (a prior committed apply whose
    reprojection never finished) -- BOTH are ``changed=True`` entries whose TABLES
    must be reprojected (``reproject_changed_tables`` accepts either directly), so
    a rerun after a partial apply still settles rows the old code left stuck (it
    reprojected only ``applied``, which is EMPTY on such a rerun). ``skipped``
    carries a ``reason`` per skipped cell (``"moved"`` from the transaction -- a
    GENUINE moved target, current != before AND != after; ``"invariant"`` from the
    pre-filter) and ``rejected`` carries ``reason="membership"`` per cell that
    failed the notebook/table ownership check. Idempotent on re-run: a re-applied
    cell's current content equals ``after`` -> ``already_applied`` (not written,
    not a moved skip).

    ``actor`` (knowhow 表版本管理 Task 13, spec §7.6) is threaded to
    ``update_knowhow_cells_bulk_guarded`` with ``origin="backfill"`` so the
    history timeline can show this write apart from a manual edit --
    otherwise the one deliverable this whole version-management feature
    promises for this exact CLI (spec §7.6: "看到 LLM 改了什么") never
    materializes for it."""
    skipped: list[dict] = []
    candidates: list[dict] = []
    for entry in entries:
        if not entry.get("changed"):
            continue
        if not md_normalize.content_invariant(entry["before"], entry["after"]):
            skipped.append({**entry, "reason": "invariant"})
            continue
        candidates.append(entry)
    by_cell = {(e["row_id"], e["column_id"]): e for e in candidates}
    result = repo.update_knowhow_cells_bulk_guarded(
        notebook_id,
        [
            (e["table_id"], e["row_id"], e["column_id"], e["before"], e["after"])
            for e in candidates
        ],
        actor=actor, origin="backfill",
    )
    applied = [by_cell[key] for key in result["written"]]
    already_applied = [by_cell[key] for key in result.get("already_applied", [])]
    skipped.extend({**by_cell[key], "reason": "moved"} for key in result["skipped"])
    rejected = [
        {**by_cell[key], "reason": "membership"} for key in result.get("rejected", [])
    ]
    return applied, already_applied, skipped, rejected


def _print_skipped(skipped: list[dict]) -> None:
    for entry in skipped:
        reason = {
            "moved": "内容在评审后已变化，跳过",
            "invariant": "候选未通过内容不变式校验，跳过",
            "membership": "不属于该笔记本/表，已拒绝",
        }.get(entry["reason"], "跳过")
        tag = "reject" if entry["reason"] == "membership" else "skip"
        print(
            f"[{tag}] {reason} table={entry['table_id']} row={entry['row_id']} "
            f"column={entry['column_id']}",
            file=sys.stderr,
        )


def _degradation_warning(plan: list[dict]) -> "str | None":
    """The --use-llm no-silent-degradation warning (仓库「拒绝静默降级」约定），
    distinguishing the two degraded shapes:
      - ``rule/no-llm``: the rewrite model was never invoked (未配置/不可用) --
        surfaced whenever any such cell exists, since the user asked for LLM and
        got none.
      - ``rule/llm-failed`` WITH ``changed=True``: the model WAS invoked, its
        output failed the content-invariant check, and the rules fallback then
        actually changed the cell (校验未过已退回规则) -- the user got a rules
        result where they asked for an LLM one. A ``rule/llm-failed`` that left
        the cell UNCHANGED is not counted: the cell was fine either way, so
        there is no material degradation to warn about."""
    no_llm = [e for e in plan if e["source"] == "rule/no-llm"]
    llm_failed = [e for e in plan if e["source"] == "rule/llm-failed" and e["changed"]]
    if not (no_llm or llm_failed):
        return None
    parts: list[str] = []
    if no_llm:
        parts.append(
            f"{len(no_llm)} 个格子因改写模型未配置/不可用，已回退为纯规则规整"
            "（source=rule/no-llm）"
        )
    if llm_failed:
        parts.append(
            f"{len(llm_failed)} 个格子的 LLM 重排结果未通过内容不变式校验、已退回"
            "规则规整（source=rule/llm-failed）"
        )
    return (
        "WARNING: 已指定 --use-llm，但 " + "；".join(parts)
        + "。上述格子不是真正的 LLM 重排结果。"
    )


def _apply_needs_plan_message(use_llm: bool) -> str:
    """Every ``--apply`` requires a reviewed ``--plan`` (F3: the rules-only
    plan-less exception was dropped). Re-planning at apply time reads the CURRENT
    database, so a cell edited after the reviewed dry-run would enter a fresh plan
    carrying its current content, pass the guarded write, and get written despite
    never being reviewed. The message shows the same two-step plan handshake the
    ``--use-llm`` path already printed, with the example commands adapted to
    whether ``--use-llm`` is in play (and, for ``--use-llm``, the additional
    stochastic-model reason)."""
    flag = "--use-llm " if use_llm else ""
    reason = (
        "拒绝执行：应用（--apply）必须基于一份评审过的 plan 文件（--plan）。"
        "apply 时若不带 plan 就地重新规划，会从【当前】数据库重新读取内容——"
        "在 dry-run 评审之后被改动过的格子会带着当前内容进入这份新计划、通过守卫"
        "写入，却从未被任何人评审过。"
    )
    if use_llm:
        reason += (
            "（--use-llm 更甚：改写模型随机（temperature 1.0、无缓存），"
            "重新规划连候选本身都会与评审时不同。）"
        )
    return (
        reason + "\n请改用两步式 plan 握手：\n"
        "  1) 先 dry-run 生成并评审 plan 文件：\n"
        f"     PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook <nb> {flag}\n"
        "  2) 评审无误后，用该 plan 文件写入：\n"
        "     PYTHONPATH=backend python scripts/backfill_knowhow_md.py "
        f"--notebook <nb> {flag}--apply --plan <plan.json>"
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="回填一个 notebook 存量 knowhow 格子的 Markdown 规整（默认 dry-run，不写库）"
    )
    parser.add_argument("--notebook", required=True, help="目标 notebook 的 id")
    parser.add_argument(
        "--apply", action="store_true",
        help="真正写库（默认不加即 dry-run，只打印计划 + 写出 plan 文件）；必须配 --plan（见下）",
    )
    parser.add_argument(
        "--use-llm", action="store_true",
        help="走 LLM 重排 + 内容不变式校验 + 规则兜底（默认不加，只用零 LLM 的确定性规则）",
    )
    parser.add_argument(
        "--save-plan", default=None, metavar="PATH",
        help="dry-run 时把 plan 写到该路径（默认 .local/backfill_plans/knowhow_md_<nb>_<ts>.json）",
    )
    parser.add_argument(
        "--plan", default=None, metavar="PATH",
        help="apply 时按该 plan 文件逐条应用（任何 --apply 都必须提供）",
    )
    return parser.parse_args(argv)


def _emit_dry_run(plan: list[dict], args) -> int:
    """Print + persist a dry-run plan (shared by the read-only default path and
    the ``--use-llm`` repository path). Only ``--use-llm`` can degrade
    (rule/no-llm / rule/llm-failed), so the no-silent-degradation warning is
    gated on ``args.use_llm``."""
    _print_plan(plan)
    if args.use_llm:
        warning = _degradation_warning(plan)
        if warning:
            print(warning, file=sys.stderr)
    created_at = datetime.now(timezone.utc)
    path = save_plan(plan, args.notebook, args.use_llm, created_at, args.save_plan)
    use_llm_flag = "--use-llm " if args.use_llm else ""
    print(f"[dry-run] 计划已保存到：{path}", file=sys.stderr)
    print("[dry-run] 未写入任何库内容。评审该文件后，用它执行：", file=sys.stderr)
    print(
        f"    PYTHONPATH=backend python scripts/backfill_knowhow_md.py "
        f"--notebook {args.notebook} {use_llm_flag}--apply --plan {path}",
        file=sys.stderr,
    )
    return 0


def _run_dry_run_readonly(args) -> int:
    """DEFAULT (rules-only) dry-run: plan from a ``mode=ro`` connection resolved
    from the SAME Settings the repository would use, WITHOUT ever constructing
    the write-capable repository (P2). ``Settings()`` is config-only (no DB
    touch)."""
    plan = plan_backfill_readonly(Settings().sqlite_path, args.notebook)
    return _emit_dry_run(plan, args)


def _run_dry_run(repo, args) -> int:
    if not args.use_llm:
        # (unreachable via main(): the rules-only dry-run takes the read-only
        # path; kept correct for direct callers.)
        return _emit_dry_run(plan_backfill(repo, args.notebook, use_llm=False), args)
    print("[use-llm] 使用系统 knowhow_reformat workload 执行。", file=sys.stderr)
    plan = plan_backfill(repo, args.notebook, use_llm=True)
    return _emit_dry_run(plan, args)


def _run_apply_from_plan(repo, args) -> int:
    document = load_plan(args.plan)
    plan_notebook = document.get("notebook_id")
    if plan_notebook != args.notebook:
        print(
            f"拒绝执行：plan 文件的 notebook（{plan_notebook!r}）与 --notebook"
            f"（{args.notebook!r}）不一致；不会对错误的 notebook 应用计划。",
            file=sys.stderr,
        )
        return 2
    # knowhow 表版本管理 Task 13（spec §7.6）：actor 按 notebook 所有者解析——
    # 与 --use-llm 分支解析模型配置用的是同一个 resolve_notebook_owner_profile
    # helper，但这里的后果轻得多（写库的 actor 字段留空，只是历史时间线上少
    # 一个"是谁做的"标签），不像 --use-llm 那样是把私有格子文本发去错误模型
    # 端点的隐私风险——所以这里选择警告后继续写入，而不是像 --use-llm 那样
    # 硬拒绝：dry-run 阶段本就不做这个解析，一个已经跑通 dry-run、被人工评审
    # 通过的 plan 不该单单因为所有者解析失败就在 apply 这一步被拦下。
    owner = repo.maintenance.resolve_notebook_owner_profile(args.notebook)
    if owner is None:
        print(
            f"[apply] 警告：无法解析 notebook（{args.notebook!r}）的所有者，"
            "本次写入的 actor 将记为空字符串（历史时间线上看不出是谁做的这次回填）。",
            file=sys.stderr,
        )
    actor = owner.id if owner is not None else ""
    applied, already_applied, skipped, rejected = apply_reviewed_plan(
        repo, args.notebook, document.get("entries", []), actor=actor,
    )
    _print_skipped(skipped + rejected)
    print(
        f"[apply] 已写入 {len(applied)} 个格子"
        f"（已是目标值·重投影 {len(already_applied)} 个，跳过 {len(skipped)} 个，"
        f"拒绝 {len(rejected)} 个）",
        file=sys.stderr,
    )
    # P1-3 + F4: reproject synchronously in-process before returning -- a still-
    # 'pending' row would otherwise be flipped to 'failed' the next time any
    # process constructs SQLiteRepository. Reproject tables with an actual staged
    # write (``applied``) OR an ALREADY-APPLIED cell (``already_applied`` -- a
    # prior run committed the write but its reprojection never finished, leaving
    # the row stuck; on THIS rerun the cell is already at its AFTER so nothing is
    # written, yet the table still needs reprojecting to recover). Reprojection is
    # idempotent, so re-running it for an already-settled table is harmless. Both
    # bucket kinds are changed=True entries reproject_changed_tables accepts.
    reproject_targets = applied + already_applied
    if reproject_targets:
        reprojected = reproject_changed_tables(repo, reproject_targets)
        print(f"[apply] 已重投影 {len(reprojected)} 张表：{reprojected}", file=sys.stderr)
    return 0


_DB_OPEN_NOTICE = (
    "[notice] 即将以【可写】方式打开数据库并构造仓库：这会执行尚未完成的迁移与崩溃恢复"
    "（可能把仍处于 pending/syncing 的行判为 failed）——建议后端空闲时执行。"
    "（默认 dry-run 是只读打开，随时可安全执行。）"
)


def main(argv: "list[str] | None" = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # EVERY --apply WITHOUT --plan is a HARD ERROR (F3: the rules-only plan-less
    # exception was dropped). Re-planning at apply time reads the CURRENT db, so a
    # cell edited after the reviewed dry-run would enter a fresh plan carrying its
    # current content, pass the guarded write, and be written despite never being
    # reviewed. The only correct apply is the two-step plan handshake -- dry-run
    # already always saves a plan to point --apply --plan at. Checked before
    # opening the DB so it writes/touches nothing.
    if args.apply and not args.plan:
        print(_apply_needs_plan_message(args.use_llm), file=sys.stderr)
        return 2

    # DEFAULT dry-run (rules-only, no --apply) is FULLY READ-ONLY (P2): plan it
    # from a mode=ro connection and NEVER construct the write-capable repository,
    # whose __init__ runs migrations/seed/crash-recovery (a "writes nothing"
    # dry-run could otherwise mutate schema/state). Only --use-llm (model
    # resolution) and --apply (writes) need the repository below.
    if not args.apply and not args.use_llm:
        return _run_dry_run_readonly(args)

    # Opening the DB read-write here may run pending migrations/recovery.
    print(_DB_OPEN_NOTICE, file=sys.stderr)
    repo = SQLiteRepository(Settings())

    if not args.apply:
        return _run_dry_run(repo, args)
    # --apply always has --plan here (main()'s guard rejected plan-less apply above).
    return _run_apply_from_plan(repo, args)


if __name__ == "__main__":
    raise SystemExit(main())
