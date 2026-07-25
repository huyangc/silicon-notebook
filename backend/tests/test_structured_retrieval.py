import json

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.services.structured_retrieval import (
    enumerate_knowhow,
    is_knowhow_enumeration_query,
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
