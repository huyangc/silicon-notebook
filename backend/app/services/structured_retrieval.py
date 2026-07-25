"""Collection-aware retrieval for exhaustive Knowhow questions.

Ranked ANN/FTS retrieval can find the best rows but cannot prove that a finite
table has been exhausted.  This module deliberately uses the Knowhow store's
stable cursor instead.  Every safety stop is returned as explicit coverage;
callers must never turn a partial batch into an "all" answer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from app.core.ask_retrieval_policy import (
    AskRetrievalLimits,
    EXPLICIT_PARTIAL_OVERFLOW,
)
from app.models.ask import (
    StructuredKnowhowResult,
    StructuredResultColumn,
    StructuredResultCoverage,
    StructuredResultRow,
    TraceStep,
)
from app.services.cancellation import CancelEvent, raise_if_cancelled


_METHOD_WORDS = re.compile(
    r"方法|方案|做法|流程|步骤|技巧|技术|knowhow|methods?|procedures?|"
    r"techniques?|approaches?",
    re.IGNORECASE,
)
_TABLE_WORDS = re.compile(
    r"knowhow|表格|表中|数据表|(?:所有|每|各|多少)行|行记录|"
    r"tables?|rows?|columns?",
    re.IGNORECASE,
)


@dataclass
class StructuredEnumeration:
    result_sets: list[StructuredKnowhowResult] = field(default_factory=list)
    known_total_rows: int = 0
    scanned_rows: int = 0
    returned_rows: int = 0
    selected_tables: int = 0
    known_tables: int = 0
    complete: bool = True
    truncated_reason: str = ""


def render_structured_answer(
    batch: StructuredEnumeration,
    *,
    aggregate: bool,
    inline_rows: int,
    cell_excerpt_chars: int,
) -> tuple[str, str]:
    """Return honest deterministic prose while result sets stay authoritative."""
    if batch.complete:
        conclusion = (
            f"已完整统计 {batch.known_total_rows} 行 Knowhow 记录。"
            if aggregate else
            f"已完整读取 {batch.known_total_rows}/{batch.known_total_rows} 行 Knowhow 记录。"
        )
    else:
        conclusion = (
            f"当前仅返回部分结果：已读取 {batch.scanned_rows}/"
            f"{batch.known_total_rows} 行，原因 {batch.truncated_reason or 'unknown'}。"
        )
    lines = [conclusion]
    remaining = max(0, int(inline_rows))
    for result in batch.result_sets:
        if remaining <= 0:
            break
        lines.append(f"\n### {result.title or 'Knowhow'}")
        names = {column.id: column.name for column in result.columns}
        for row in result.rows[:remaining]:
            values = [
                f"{names.get(column_id, column_id)}：{content[:cell_excerpt_chars]}"
                for column_id, content in row.cells.items()
                if content
            ]
            lines.append(f"- {row.position + 1}. " + ("；".join(values) or "（空行）"))
            remaining -= 1
            if remaining <= 0:
                break
    if batch.returned_rows > inline_rows:
        lines.append(
            f"\n答案正文最多内联 {inline_rows} 行；其余已加载行请在下方结果卡中展开。"
        )
    return conclusion, "\n".join(lines)


def structured_prompt_block(
    batch: StructuredEnumeration,
    *,
    inline_rows: int,
    cell_excerpt_chars: int,
    budget_chars: int,
) -> str:
    """Bound the structured preview injected into hybrid answer synthesis."""
    header = (
        f"[Structured Knowhow coverage: complete={str(batch.complete).lower()}, "
        f"scanned={batch.scanned_rows}, total={batch.known_total_rows}. "
        "The structured row set and exact coverage are returned separately; "
        "do not claim coverage beyond these numbers.]"
    )
    lines = [header]
    remaining_rows = max(0, int(inline_rows))
    used = len(header)
    for result in batch.result_sets:
        names = {column.id: column.name for column in result.columns}
        for row in result.rows:
            if remaining_rows <= 0 or used >= budget_chars:
                return "\n".join(lines)
            values = " | ".join(
                f"{names.get(column_id, column_id)}={content[:cell_excerpt_chars]}"
                for column_id, content in row.cells.items()
                if content
            )
            line = f"{result.title} row={row.row_id}: {values or '(empty)'}"
            if used + len(line) > budget_chars:
                return "\n".join(lines)
            lines.append(line)
            used += len(line)
            remaining_rows -= 1
    return "\n".join(lines)


def _text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def is_knowhow_enumeration_query(tables: Sequence[dict], question: str) -> bool:
    """Conservative routing guard: never call a KG collection a Knowhow table."""
    if not tables:
        return False
    normalized = _text(question)
    if _METHOD_WORDS.search(question) or _TABLE_WORDS.search(question):
        return True
    return any(
        len(_text(row.get("title"))) >= 2 and _text(row.get("title")) in normalized
        for row in tables
    )


def _select_tables(
    tables: Sequence[dict], question: str, limit: int
) -> tuple[list[dict], list[dict], bool]:
    """Prefer an explicitly named table; otherwise enumerate the whole scope."""
    explicit = []
    for row in tables:
        title = str(row.get("title") or "").strip()
        if len(_text(title)) < 2:
            continue
        # A plain lexical overlap is not an explicit scope.  In particular, a
        # table named “方法” must not turn “所有方法有哪些” into a silently
        # single-table answer.  Require table/location syntax or a quoted exact
        # title before narrowing an otherwise global completeness request.
        escaped = re.escape(title)
        if re.search(
            rf"(?:[《「『\"']\s*{escaped}\s*[》」』\"']|"
            rf"{escaped}(?:表|表格)?(?:中|内|里|中的|内的|里的))",
            question,
            re.IGNORECASE,
        ):
            explicit.append(row)
    pool = explicit or list(tables)
    pool.sort(key=lambda row: (_text(row.get("title")), str(row.get("id") or "")))
    return pool[:limit], pool, len(pool) > limit


def _select_columns(catalog: dict, question: str, limit: int) -> tuple[list[dict], bool]:
    columns = list(catalog.get("columns") or [])
    normalized = _text(question)
    named = [
        column for column in columns
        if len(_text(column.get("name"))) >= 1
        and _text(column.get("name")) in normalized
    ]
    if named:
        selected = named
    elif _METHOD_WORDS.search(question):
        method_columns = [
            column for column in columns
            if column.get("role") == "anchor"
            or _METHOD_WORDS.search(str(column.get("name") or ""))
        ]
        selected = method_columns or columns
    else:
        selected = columns
    selected.sort(
        key=lambda column: (
            int(column.get("position") or 0), str(column.get("id") or "")
        )
    )
    return selected[:limit], len(selected) > limit


def _catalog_fingerprint(catalog: dict) -> tuple:
    """Cheap metadata identity used to reject a changing enumeration scope."""
    columns = tuple(sorted(
        (
            str(column.get("id") or ""),
            str(column.get("name") or ""),
            str(column.get("role") or ""),
            int(column.get("position") or 0),
        )
        for column in (catalog.get("columns") or [])
    ))
    return (
        int(catalog.get("mutation_seq") or 0),
        int(catalog.get("enumeration_seq") or 0),
        int(catalog.get("row_count") or 0),
        columns,
    )


def _table_scope_fingerprint(tables: Sequence[dict]) -> tuple:
    return tuple(sorted(
        (str(row.get("id") or ""), int(row.get("row_count") or 0))
        for row in tables
    ))


def enumerate_knowhow(
    store,
    notebook_id: str,
    question: str,
    limits: AskRetrievalLimits,
    *,
    cancel_event: CancelEvent = None,
    on_step: Callable[[TraceStep], None] | None = None,
) -> StructuredEnumeration:
    """Enumerate selected Knowhow tables under one request-wide hard budget.

    The metadata probe uses ``column_ids=[]`` so no cell payload is hydrated.
    Data pages are 25 physical rows, at most 50 pages / 1,250 rows across all
    selected tables.  A final mutation probe prevents a concurrent edit from
    being reported as a stable complete snapshot.
    """
    raise_if_cancelled(cancel_event)
    all_tables = list(store.list_knowhow_tables(notebook_id) or [])
    selected, scope_tables, table_limited = _select_tables(
        all_tables, question, limits.structured_max_tables
    )
    initial_scope_fingerprint = _table_scope_fingerprint(scope_tables)
    batch = StructuredEnumeration(
        known_total_rows=sum(int(row.get("row_count") or 0) for row in scope_tables),
        selected_tables=len(selected),
        known_tables=len(scope_tables),
        complete=not table_limited,
        truncated_reason="table_limit" if table_limited else "",
    )
    if not selected:
        return batch

    payload_chars = 0
    pages = 0
    stop_all = False
    for summary in selected:
        raise_if_cancelled(cancel_event)
        table_id = str(summary.get("id") or "")
        probe = store.enumerate_knowhow_rows(
            notebook_id,
            table_ids=[table_id],
            cursor=None,
            page_size=1,
            column_ids=[],
        )
        if not probe.get("tables"):
            batch.complete = False
            batch.truncated_reason = batch.truncated_reason or "concurrent_change"
            continue
        catalog = probe["tables"][0]
        initial_catalog_fingerprint = _catalog_fingerprint(catalog)
        initial_mutation = initial_catalog_fingerprint[0]
        columns, column_limited = _select_columns(
            catalog, question, limits.structured_max_columns
        )
        if column_limited:
            batch.complete = False
            batch.truncated_reason = batch.truncated_reason or "column_limit"
        column_ids = [str(column.get("id") or "") for column in columns]
        table_rows: list[StructuredResultRow] = []
        table_scanned = 0
        cursor = None
        table_total = int(catalog.get("row_count") or 0)
        table_complete = not table_limited and not column_limited
        table_reason = (
            "table_limit" if table_limited else "column_limit" if column_limited else ""
        )

        while True:
            raise_if_cancelled(cancel_event)
            if pages >= limits.structured_max_pages or batch.scanned_rows >= limits.structured_max_rows:
                batch.complete = table_complete = False
                batch.truncated_reason = batch.truncated_reason or "row_limit"
                table_reason = table_reason or "row_limit"
                stop_all = True
                break
            page = store.enumerate_knowhow_rows(
                notebook_id,
                table_ids=[table_id],
                cursor=cursor,
                page_size=min(
                    limits.structured_page_size,
                    limits.structured_max_rows - batch.scanned_rows,
                ),
                column_ids=column_ids,
            )
            pages += 1
            page_rows = list(page.get("rows") or [])
            scanned_now = len(page_rows)
            table_scanned += scanned_now
            batch.scanned_rows += scanned_now
            current_catalog = (page.get("tables") or [{}])[0]
            if int(current_catalog.get("mutation_seq") or 0) != initial_mutation:
                batch.complete = table_complete = False
                batch.truncated_reason = batch.truncated_reason or "concurrent_change"
                table_reason = table_reason or "concurrent_change"
                break

            for raw in page_rows:
                result_row = StructuredResultRow(
                    row_id=str(raw.get("id") or ""),
                    position=int(raw.get("position") or 0),
                    cells={
                        str(key): str(value)
                        for key, value in (raw.get("cells") or {}).items()
                    },
                )
                row_chars = len(json.dumps(
                    result_row.model_dump(), ensure_ascii=False, separators=(",", ":")
                ))
                if payload_chars + row_chars > limits.structured_payload_chars:
                    batch.complete = table_complete = False
                    batch.truncated_reason = batch.truncated_reason or "payload_limit"
                    table_reason = table_reason or "payload_limit"
                    stop_all = True
                    break
                table_rows.append(result_row)
                batch.returned_rows += 1
                payload_chars += row_chars
            if stop_all or table_reason in {"concurrent_change", "payload_limit"}:
                break

            if on_step:
                on_step(TraceStep(
                    step_type="enumerate",
                    summary=f"读取 {batch.scanned_rows}/{batch.known_total_rows} 行 Knowhow",
                    detail={
                        "table_id": table_id,
                        "page": pages,
                        "scanned_rows": batch.scanned_rows,
                        "known_total_rows": batch.known_total_rows,
                    },
                ))
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                batch.complete = table_complete = False
                batch.truncated_reason = batch.truncated_reason or "cursor_error"
                table_reason = table_reason or "cursor_error"
                break

        # No cross-page transaction is held.  Re-read only metadata and refuse
        # to claim completeness if a concurrent writer changed the table.
        final_probe = store.enumerate_knowhow_rows(
            notebook_id,
            table_ids=[table_id],
            cursor=None,
            page_size=1,
            column_ids=[],
        )
        final_catalog = (final_probe.get("tables") or [{}])[0]
        if _catalog_fingerprint(final_catalog) != initial_catalog_fingerprint:
            batch.complete = table_complete = False
            batch.truncated_reason = batch.truncated_reason or "concurrent_change"
            table_reason = table_reason or "concurrent_change"

        batch.result_sets.append(StructuredKnowhowResult(
            table_id=table_id,
            title=str(catalog.get("title") or summary.get("title") or ""),
            columns=[
                StructuredResultColumn(
                    id=str(column.get("id") or ""),
                    name=str(column.get("name") or ""),
                    role=str(column.get("role") or "attribute"),
                )
                for column in columns
            ],
            rows=table_rows,
            coverage=StructuredResultCoverage(
                total_rows=table_total,
                scanned_rows=table_scanned,
                returned_rows=len(table_rows),
                complete=(
                    table_complete
                    and table_scanned == table_total
                    and len(table_rows) == table_total
                ),
                truncated_reason=table_reason,
                overflow_semantics=(
                    "" if not table_reason else EXPLICIT_PARTIAL_OVERFLOW
                ),
            ),
        ))
        if stop_all:
            break

    # Known totals include omitted tables, which is what lets the answer say
    # "partial" honestly when the eight-table request-wide ceiling fires.
    final_tables = list(store.list_knowhow_tables(notebook_id) or [])
    _, final_scope_tables, _ = _select_tables(
        final_tables, question, limits.structured_max_tables
    )
    if _table_scope_fingerprint(final_scope_tables) != initial_scope_fingerprint:
        batch.complete = False
        batch.truncated_reason = batch.truncated_reason or "concurrent_change"
        for result in batch.result_sets:
            result.coverage.complete = False
            result.coverage.truncated_reason = (
                result.coverage.truncated_reason or "concurrent_change"
            )
            result.coverage.overflow_semantics = EXPLICIT_PARTIAL_OVERFLOW
    if (
        batch.scanned_rows != batch.known_total_rows
        or batch.returned_rows != batch.known_total_rows
    ):
        batch.complete = False
    # The incremental row check above avoids hydrating a predictably oversized
    # response.  This final exact serialization check also accounts for JSON
    # separators, table/column metadata, and coverage fields, so the documented
    # 256,000-character rail applies to the actual structured result payload.
    def result_payload_chars() -> int:
        return len(json.dumps(
            [item.model_dump() for item in batch.result_sets],
            ensure_ascii=False,
            separators=(",", ":"),
        ))

    payload_trimmed = False
    while result_payload_chars() > limits.structured_payload_chars:
        payload_trimmed = True
        result_with_row = next(
            (item for item in reversed(batch.result_sets) if item.rows), None
        )
        if result_with_row is None:
            batch.result_sets = []
            batch.returned_rows = 0
            break
        result_with_row.rows.pop()
        result_with_row.coverage.returned_rows -= 1
        result_with_row.coverage.complete = False
        result_with_row.coverage.truncated_reason = "payload_limit"
        result_with_row.coverage.overflow_semantics = EXPLICIT_PARTIAL_OVERFLOW
        batch.returned_rows -= 1
    if result_payload_chars() > limits.structured_payload_chars:
        # Defensive only: ``[]`` is two characters, so validated positive
        # limits cannot reach this branch.
        raise ValueError("structured payload limit is too small")
    if payload_trimmed or batch.returned_rows != batch.known_total_rows:
        batch.complete = False
        batch.truncated_reason = batch.truncated_reason or "payload_limit"
    return batch
