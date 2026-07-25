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
    StructuredBatchCoverage,
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
_UNSUPPORTED_SET_OPERATION = re.compile(
    r"(?:分组|分类汇总|去重|不同(?:的)?|满足|符合|限定|仅限|只(?:要|列出)|筛选|过滤|"
    r"大于|小于|不少于|不超过|至少|至多|其中(?:的)?|多少(?:种|类)|"
    r"适用(?:于)?|适合|用于|可用于|针对|面向|"
    r"group\s+by|distinct|where|filter(?:ed)?|matching|only)",
    re.IGNORECASE,
)
_SAFE_COMPLETE_COLLECTION = re.compile(
    r"^\s*(?:请(?:帮我)?|麻烦(?:帮我)?)?\s*"
    r"(?:(?:列出|罗列|枚举|读取|展示|给出)\s*)?"
    r"(?:(?:全部|所有|全量|每(?:一)?种|逐一|逐项)\s*(?:的\s*)?"
    r"(?:方法|方案|做法|流程|步骤|技巧|技术|行|记录)"
    r"|(?:方法|方案|做法|流程|步骤|技巧|技术|行|记录)\s*"
    r"(?:有哪些|清单|列表|全部|所有))"
    r"\s*(?:有哪些|是什么|分别是什么|清单|列表)?"
    r"\s*(?:[，,]\s*)?(?:(?:并|以及|同时)\s*"
    r"(?:分析|比较|对比)\s*(?:各自|它们|这些方法)?\s*(?:的\s*)?"
    r"(?:优缺点|差异|特点|性能|趋势)?)?\s*[？?。.!！]?\s*$"
    r"|^\s*(?:please\s+)?(?:list|show|enumerate)\s+"
    r"(?:all|every|each)\s+"
    r"(?:methods?|procedures?|techniques?|approaches?|rows?|records?)"
    r"(?:\s+and\s+(?:compare|analy[sz]e)\s+(?:their\s+)?"
    r"(?:trade-?offs?|differences?|properties|performance))?\s*[?.!]?\s*$",
    re.IGNORECASE,
)
_SAFE_PHYSICAL_COUNT = re.compile(
    r"^\s*(?:请问|请统计|统计)?\s*(?:"
    r"(?:这些?|上述)?(?:方法|方案|做法|流程|步骤|技巧|技术|记录|行)"
    r"\s*(?:一共|总共|合计)?(?:有)?多少(?:个|项|条|行)?"
    r"|(?:一共|总共|合计)?(?:有)?多少(?:个|项|条|行)\s*"
    r"(?:方法|方案|做法|流程|步骤|技巧|技术|记录|行)?"
    r"|(?:物理)?(?:总行数|行数|记录数)(?:是|为)?多少)\s*[？?。.!！]?\s*$"
    r"|^\s*(?:please\s+)?(?:how\s+many|count(?:\s+all)?|"
    r"total\s+(?:number|count)\s+of)\s+"
    r"(?:methods?|procedures?|techniques?|approaches?|rows?|records?)\s*[?.!]?\s*$",
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
    synthesis_rows: int = 0
    synthesis_complete: bool | None = None

    def coverage(self) -> StructuredBatchCoverage:
        return StructuredBatchCoverage(
            known_tables=self.known_tables,
            selected_tables=self.selected_tables,
            known_total_rows=self.known_total_rows,
            scanned_rows=self.scanned_rows,
            returned_rows=self.returned_rows,
            complete=self.complete,
            truncated_reason=self.truncated_reason,
            overflow_semantics=(
                "" if self.complete else EXPLICIT_PARTIAL_OVERFLOW
            ),
            synthesis_rows=self.synthesis_rows,
            synthesis_complete=self.synthesis_complete,
        )


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
    # Reserve enough room for the authoritative coverage header, then include
    # only complete row lines that fit.  The batch records exactly what the
    # synthesis model saw; enumeration completeness remains a separate fact.
    lines: list[str] = []
    remaining_rows = max(0, int(inline_rows))
    used = 0
    header_reserve = min(max(0, int(budget_chars)), 480)
    row_budget = max(0, int(budget_chars) - header_reserve)
    for result in batch.result_sets:
        names = {column.id: column.name for column in result.columns}
        for row in result.rows:
            if remaining_rows <= 0 or used >= row_budget:
                break
            values = " | ".join(
                f"{names.get(column_id, column_id)}={content[:cell_excerpt_chars]}"
                for column_id, content in row.cells.items()
                if content
            )
            line = f"{result.title} row={row.row_id}: {values or '(empty)'}"
            if used + len(line) + (1 if lines else 0) > row_budget:
                break
            lines.append(line)
            used += len(line) + (1 if len(lines) > 1 else 0)
            remaining_rows -= 1
        if remaining_rows <= 0 or used >= row_budget:
            break
    batch.synthesis_rows = len(lines)
    batch.synthesis_complete = bool(
        batch.complete and batch.synthesis_rows == batch.known_total_rows
    )
    header = (
        "[Structured Knowhow coverage: "
        f"enumeration_complete={str(batch.complete).lower()}, "
        f"enumerated={batch.returned_rows}/{batch.known_total_rows}, "
        f"synthesis_complete={str(batch.synthesis_complete).lower()}, "
        f"synthesis_rows={batch.synthesis_rows}/{batch.known_total_rows}. "
        "The result cards carry the authoritative enumerated rows. Base every "
        "analysis only on synthesis_rows and explicitly disclose partial analysis.]"
    )
    rendered = "\n".join([header, *lines])
    while len(rendered) > budget_chars and lines:
        lines.pop()
        batch.synthesis_rows = len(lines)
        batch.synthesis_complete = bool(
            batch.complete and batch.synthesis_rows == batch.known_total_rows
        )
        header = re.sub(
            r"synthesis_complete=(?:true|false), synthesis_rows=\d+",
            f"synthesis_complete={str(batch.synthesis_complete).lower()}, "
            f"synthesis_rows={batch.synthesis_rows}",
            header,
        )
        rendered = "\n".join([header, *lines])
    return rendered[:max(0, int(budget_chars))]


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


def _without_explicit_table_scope(
    question: str, tables: Sequence[dict]
) -> str:
    """Remove only a proven table locator before applying the sentence grammar."""
    text = question
    for row in sorted(
        tables,
        key=lambda item: len(str(item.get("title") or "")),
        reverse=True,
    ):
        title = str(row.get("title") or "").strip()
        if len(_text(title)) < 2:
            continue
        escaped = re.escape(title)
        text = re.sub(
            rf"(?:[《「『\"']\s*{escaped}\s*[》」』\"']|{escaped}(?:表|表格)?)"
            rf"\s*(?:中|内|里)(?:的)?",
            "",
            text,
            flags=re.IGNORECASE,
        )
    # Generic, non-title locators are safe to remove.  Arbitrary phrases such
    # as “低功耗设计中” are deliberately not accepted as table scope.
    text = re.sub(
        r"(?:这个|该|当前)?\s*(?:knowhow\s*)?(?:数据表|表格|表)"
        r"\s*(?:(?:中|内|里)(?:的)?)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def supports_structured_knowhow_query(
    question: str,
    result_scope: str,
    tables: Sequence[dict] = (),
) -> bool:
    """Admit only whole sentences the physical-row executor can prove exactly."""
    if _UNSUPPORTED_SET_OPERATION.search(question):
        return False
    scoped_question = _without_explicit_table_scope(question, tables)
    if result_scope == "aggregate":
        return bool(_SAFE_PHYSICAL_COUNT.fullmatch(scoped_question))
    if result_scope in {"complete", "hybrid"}:
        return bool(_SAFE_COMPLETE_COLLECTION.fullmatch(scoped_question))
    return False


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


def _load_catalog(store, notebook_id: str, limit: int, *, query: str = "") -> dict:
    loader = getattr(store, "knowhow_enumeration_catalog", None)
    if callable(loader):
        return dict(loader(notebook_id, limit=limit, query=query) or {})
    # Compatibility fallback for narrow test doubles and older repository
    # implementations; production adapters implement the bounded metadata port.
    tables = list(store.list_knowhow_tables(notebook_id) or [])[:limit]
    return {
        "tables": tables,
        "known_tables": len(tables),
        "known_total_rows": sum(int(row.get("row_count") or 0) for row in tables),
        "fingerprint": list(_table_scope_fingerprint(tables)),
    }


def enumerate_knowhow(
    store,
    notebook_id: str,
    question: str,
    limits: AskRetrievalLimits,
    *,
    catalog_snapshot: dict | None = None,
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
    initial_catalog = dict(catalog_snapshot or _load_catalog(
        store, notebook_id, limits.structured_max_tables, query=question
    ))
    all_tables = list(initial_catalog.get("tables") or [])
    selected, scope_tables, listed_table_limited = _select_tables(
        all_tables, question, limits.structured_max_tables
    )
    explicit_scope = len(scope_tables) < len(all_tables)
    known_tables = (
        len(scope_tables) if explicit_scope
        else int(initial_catalog.get("known_tables") or len(scope_tables))
    )
    known_total_rows = (
        sum(int(row.get("row_count") or 0) for row in scope_tables)
        if explicit_scope else
        int(initial_catalog.get("known_total_rows") or 0)
    )
    table_limited = listed_table_limited or (
        not explicit_scope and known_tables > len(selected)
    )
    initial_scope_fingerprint = _table_scope_fingerprint(scope_tables)
    initial_global_fingerprint = tuple(initial_catalog.get("fingerprint") or ())
    batch = StructuredEnumeration(
        known_total_rows=known_total_rows,
        selected_tables=len(selected),
        known_tables=known_tables,
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
        table_complete = not column_limited
        table_reason = (
            "column_limit" if column_limited else ""
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
            if _catalog_fingerprint(current_catalog) != initial_catalog_fingerprint:
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
    final_catalog = _load_catalog(
        store, notebook_id, limits.structured_max_tables, query=question
    )
    final_tables = list(final_catalog.get("tables") or [])
    _, final_scope_tables, _ = _select_tables(final_tables, question, limits.structured_max_tables)
    scope_changed = (
        _table_scope_fingerprint(final_scope_tables) != initial_scope_fingerprint
    )
    if not explicit_scope:
        final_global_fingerprint = tuple(final_catalog.get("fingerprint") or ())
        scope_changed = scope_changed or bool(
            initial_global_fingerprint
            and final_global_fingerprint != initial_global_fingerprint
        )
    if scope_changed:
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
