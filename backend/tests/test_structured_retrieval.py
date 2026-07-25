import json

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.services.structured_retrieval import (
    enumerate_knowhow,
    is_knowhow_enumeration_query,
    structured_prompt_block,
    supports_structured_knowhow_query,
)


class FakeKnowhowStore:
    def __init__(self, tables):
        self.tables = tables

    def list_knowhow_tables(self, notebook_id):
        return [
            {
                "id": table["id"],
                "title": table["title"],
                "description": "",
                "row_count": len(table["rows"]),
            }
            for table in self.tables
        ]

    def knowhow_enumeration_catalog(self, notebook_id, *, limit=8, query=""):
        tables = self.list_knowhow_tables(notebook_id)
        normalized = query.casefold()
        tables.sort(key=lambda row: (
            0 if row["title"].casefold() in normalized else 1,
            row["title"].casefold(),
            row["id"],
        ))
        return {
            "tables": tables[:limit],
            "known_tables": len(tables),
            "known_total_rows": sum(row["row_count"] for row in tables),
            "fingerprint": [
                len(tables),
                sum(row["row_count"] for row in tables),
                sum(int(table.get("mutation_seq", 1)) for table in self.tables),
                sum(int(table.get("enumeration_seq", 1)) for table in self.tables),
            ],
        }

    def enumerate_knowhow_rows(
        self, notebook_id, *, table_ids, cursor=None, page_size=25, column_ids=None
    ):
        table = next(row for row in self.tables if row["id"] == table_ids[0])
        start = int((cursor or {}).get("position", -1)) + 1
        rows = table["rows"][start:start + page_size]
        selected = set(column_ids) if column_ids is not None else None
        projected = []
        for row in rows:
            cells = row["cells"]
            if selected is not None:
                cells = {key: value for key, value in cells.items() if key in selected}
            projected.append({**row, "table_id": table["id"], "cells": cells})
        next_cursor = None
        has_more = start + len(rows) < len(table["rows"])
        if has_more and rows:
            next_cursor = {
                "table_id": table["id"],
                "position": rows[-1]["position"],
                "id": rows[-1]["id"],
            }
        return {
            "tables": [{
                "id": table["id"],
                "title": table["title"],
                "description": "",
                "mutation_seq": table.get("mutation_seq", 1),
                "enumeration_seq": table.get("enumeration_seq", 1),
                "row_count": len(table["rows"]),
                "columns": table["columns"],
            }],
            "rows": projected,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "counts": {
                "scope_total_rows": len(table["rows"]),
                "scope_nonempty_rows": len(table["rows"]),
                "page_rows": len(projected),
            },
        }


def _table(table_id="methods", title="方法表", count=100, content_size=0):
    columns = [
        {"id": "method", "name": "方法", "role": "anchor", "position": 0},
        {"id": "note", "name": "备注", "role": "attribute", "position": 1},
    ]
    rows = [
        {
            "id": f"row-{index:04d}",
            "position": index,
            "cells": {
                "method": f"方法 {index}" + ("x" * content_size),
                "note": f"说明 {index}",
            },
        }
        for index in range(count)
    ]
    return {"id": table_id, "title": title, "columns": columns, "rows": rows}


def test_all_methods_enumerates_100_rows_without_top_n_truncation():
    steps = []
    result = enumerate_knowhow(
        FakeKnowhowStore([_table()]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["overview"],
        on_step=steps.append,
    )

    assert result.complete is True
    assert result.known_total_rows == result.scanned_rows == result.returned_rows == 100
    assert len(result.result_sets) == 1
    assert result.result_sets[0].coverage.complete is True
    assert [row.row_id for row in result.result_sets[0].rows] == [
        f"row-{index:04d}" for index in range(100)
    ]
    assert [column.id for column in result.result_sets[0].columns] == ["method"]
    assert [step.detail["scanned_rows"] for step in steps] == [25, 50, 75, 100]


def test_named_table_limits_complete_scope_to_that_table():
    result = enumerate_knowhow(
        FakeKnowhowStore([
            _table("a", "制造方法", 3),
            _table("b", "测试方法", 4),
        ]),
        "nb",
        "列出测试方法中的所有方法",
        ASK_RETRIEVAL_LIMITS["standard"],
    )

    assert result.complete is True
    assert result.known_tables == 1
    assert result.known_total_rows == 4
    assert [item.table_id for item in result.result_sets] == ["b"]


def test_generic_short_table_name_does_not_narrow_global_all_request():
    result = enumerate_knowhow(
        FakeKnowhowStore([
            _table("a", "方法", 3),
            _table("b", "其他方法", 4),
        ]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["standard"],
    )

    assert result.complete is True
    assert result.known_tables == 2
    assert result.known_total_rows == result.returned_rows == 7
    assert {item.table_id for item in result.result_sets} == {"a", "b"}


def test_final_catalog_change_is_explicit_partial_even_without_mutation_bump():
    class ConcurrentStore(FakeKnowhowStore):
        def __init__(self, tables):
            super().__init__(tables)
            self.enumerate_calls = 0

        def enumerate_knowhow_rows(self, *args, **kwargs):
            self.enumerate_calls += 1
            result = super().enumerate_knowhow_rows(*args, **kwargs)
            if self.enumerate_calls >= 3:
                result["tables"][0]["row_count"] += 1
            return result

    result = enumerate_knowhow(
        ConcurrentStore([_table(count=2)]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["standard"],
    )

    assert result.complete is False
    assert result.truncated_reason == "concurrent_change"
    assert result.result_sets[0].coverage.complete is False
    assert result.result_sets[0].coverage.truncated_reason == "concurrent_change"


def test_equal_count_row_replacement_changes_enumeration_sequence():
    class EqualReplacementStore(FakeKnowhowStore):
        def __init__(self, tables):
            super().__init__(tables)
            self.enumerate_calls = 0

        def enumerate_knowhow_rows(self, *args, **kwargs):
            self.enumerate_calls += 1
            result = super().enumerate_knowhow_rows(*args, **kwargs)
            if self.enumerate_calls >= 3:
                result["tables"][0]["enumeration_seq"] += 2
            return result

    result = enumerate_knowhow(
        EqualReplacementStore([_table(count=2)]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["standard"],
    )

    assert result.complete is False
    assert result.truncated_reason == "concurrent_change"
    assert result.result_sets[0].coverage.overflow_semantics == "explicit_partial"


def test_more_than_1250_rows_is_explicit_partial():
    result = enumerate_knowhow(
        FakeKnowhowStore([_table(count=1_251)]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["exhaustive"],
    )

    assert result.complete is False
    assert result.known_total_rows == 1_251
    assert result.scanned_rows == result.returned_rows == 1_250
    coverage = result.result_sets[0].coverage
    assert coverage.complete is False
    assert coverage.truncated_reason == "row_limit"
    assert coverage.overflow_semantics == "explicit_partial"


def test_payload_limit_reports_scanned_but_not_returned_rows():
    result = enumerate_knowhow(
        FakeKnowhowStore([_table(count=300, content_size=1_000)]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["standard"],
    )

    assert result.complete is False
    assert result.scanned_rows > result.returned_rows
    assert result.result_sets[0].coverage.truncated_reason == "payload_limit"
    payload_chars = len(json.dumps(
        [item.model_dump() for item in result.result_sets],
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    assert payload_chars <= ASK_RETRIEVAL_LIMITS["standard"].structured_payload_chars


def test_complete_kg_question_is_not_misrouted_to_knowhow():
    tables = [{"id": "methods", "title": "方法表", "row_count": 100}]
    assert is_knowhow_enumeration_query(tables, "所有方法有哪些？") is True
    assert is_knowhow_enumeration_query(tables, "所有概念有哪些？") is False
    assert is_knowhow_enumeration_query(tables, "所有执行条件有哪些？") is False


def test_structured_executor_accepts_only_semantics_it_can_prove():
    assert supports_structured_knowhow_query("所有方法有哪些？", "complete") is True
    assert supports_structured_knowhow_query("这个表一共有多少行记录？", "aggregate") is True
    assert supports_structured_knowhow_query("列出所有方法并比较优缺点", "hybrid") is True
    assert supports_structured_knowhow_query("列出所有低功耗方法", "complete") is False
    assert supports_structured_knowhow_query("列出所有方法用于低功耗设计", "complete") is False
    assert supports_structured_knowhow_query(
        "列出所有方法中适用于低功耗设计的项目", "complete"
    ) is False
    assert supports_structured_knowhow_query("这些方法一共有多少种？", "aggregate") is False
    assert supports_structured_knowhow_query("有多少种低功耗方法", "aggregate") is False
    assert supports_structured_knowhow_query("按方法类型分组统计", "aggregate") is False
    assert supports_structured_knowhow_query(
        "列出所有方法里功耗低于 1mW 的项目", "complete"
    ) is False
    assert supports_structured_knowhow_query(
        "列出所有方法，要求功耗低", "complete"
    ) is False
    assert supports_structured_knowhow_query(
        "列出测试方法中的所有方法",
        "complete",
        [{"id": "b", "title": "测试方法"}],
    ) is True


def test_hybrid_preview_separates_enumeration_from_synthesis_coverage():
    batch = enumerate_knowhow(
        FakeKnowhowStore([_table(count=200)]),
        "nb",
        "列出所有方法并比较优缺点",
        ASK_RETRIEVAL_LIMITS["standard"],
    )
    block = structured_prompt_block(
        batch,
        inline_rows=100,
        cell_excerpt_chars=1_000,
        budget_chars=30_000,
    )
    assert batch.complete is True
    assert batch.synthesis_rows == 100
    assert batch.synthesis_complete is False
    assert "enumeration_complete=true" in block
    assert "synthesis_complete=false" in block
    assert "synthesis_rows=100/200" in block


def test_table_limit_is_batch_partial_but_selected_tables_remain_complete():
    batch = enumerate_knowhow(
        FakeKnowhowStore([
            _table(f"table-{index}", f"方法表 {index}", 1)
            for index in range(10)
        ]),
        "nb",
        "所有方法有哪些？",
        ASK_RETRIEVAL_LIMITS["standard"],
    )
    assert batch.complete is False
    assert batch.truncated_reason == "table_limit"
    assert batch.known_tables == 10
    assert batch.selected_tables == 8
    assert batch.known_total_rows == 10
    assert batch.returned_rows == 8
    assert all(result.coverage.complete for result in batch.result_sets)
    coverage = batch.coverage()
    assert coverage.complete is False
    assert coverage.selected_tables == 8
    assert coverage.known_tables == 10


def test_named_table_beyond_global_catalog_window_is_selected_first():
    batch = enumerate_knowhow(
        FakeKnowhowStore([
            _table(f"table-{letter}", f"{letter}表", 1)
            for letter in "ABCDEFGHIJ"
        ]),
        "nb",
        "列出「I表」中的所有方法",
        ASK_RETRIEVAL_LIMITS["standard"],
    )

    assert batch.complete is True
    assert batch.known_tables == 1
    assert batch.known_total_rows == batch.returned_rows == 1
    assert [result.table_id for result in batch.result_sets] == ["table-I"]
