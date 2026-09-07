from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.models.ask import Citation
from app.services.cancellation import AskCancelled
from app.services.collection_enumeration import EnumerationCoverage, SourceItem
from app.services.document_catalog_overview import prepare_catalog_overview, supplement_missing_summaries


class Directory:
    def __init__(self, *, total=2, complete=True):
        self.calls = []
        self.items = (
            SourceItem("a", "解析方法", "文档", "介绍解析步骤", "active", "personal"),
            SourceItem("b", "参考材料", "文档", "", "base", "base"),
        )
        self.coverage = EnumerationCoverage(
            returned=2, returned_total=2, scanned=2, total=total,
            has_more=not complete, complete=complete,
            truncated_reason="" if complete else "budget",
            overflow_semantics="explicit_partial",
        )

    def enumerate_sources(self, notebook_id, **kwargs):
        self.calls.append((notebook_id, kwargs))
        return SimpleNamespace(items=self.items, coverage=self.coverage)


class Evidence:
    def collection_item_citations(self, items, *, active_notebook_id):
        assert active_notebook_id == "active"
        return {item.source_id: Citation(
            label=item.source_title, source_id=item.source_id,
            element_id="", location_label="", quoted_span=item.summary,
            notebook_id=item.notebook_id, tier=item.tier,
        ) for item in items}


def prepare(directory=None, **kwargs):
    return prepare_catalog_overview(
        directory or Directory(), Evidence(), "active",
        kwargs.pop("limits", ask_retrieval_limits("standard")),
        kwargs.pop("budget_chars", 10000), **kwargs,
    )


def test_summary_and_missing_summary_are_citable_without_claiming_full_text():
    directory = Directory()
    result = prepare(directory)
    assert "已存摘要摘录：介绍解析步骤" in result.context_block
    assert "暂无已存摘要" in result.context_block
    assert "not verified full text" in result.context_block
    assert result.id_map["k5001"]["object_id"] == "a"
    assert result.id_map["k5002"]["object_id"] == "b"
    assert result.citations["b"].notebook_id == "base"
    assert result.items == list(directory.items)
    assert result.result_sets[0].items[1].text == ""
    assert "2/2" in result.coverage_note
    assert "合成展示 2 篇" in result.coverage_note
    assert "目录列全不等于已阅读全文" in result.coverage_note
    budget = directory.calls[0][1]["budget"]
    limits = ask_retrieval_limits("standard")
    assert budget.max_rows == limits.enum_rows_per_run
    assert budget.max_pages == limits.enum_pages_per_run


@pytest.mark.parametrize("budget", [0, 100, 800, 1600])
def test_context_budget_and_actual_preview_counts(budget):
    result = prepare(budget_chars=budget)
    assert len(result.context_block) <= budget
    assert result.result_sets[0].synthesis_rows == len(result.id_map)
    assert result.result_sets[0].coverage.returned_total == 2
    assert f"合成展示 {len(result.id_map)} 篇" in result.coverage_note


def test_payload_projection_drives_directory_coverage_and_items():
    limits = replace(ask_retrieval_limits("standard"), structured_payload_chars=600)
    result = prepare(limits=limits)
    row = result.result_sets[0]
    assert len(row.items) < 2
    assert not row.coverage.complete
    assert row.coverage.truncated_reason == "payload"
    assert len(result.items) == row.coverage.returned_total
    assert f"{len(row.items)}/2" in result.coverage_note
    assert set(result.citations) == {item.item_id for item in row.items}


def test_unknown_total_is_not_presented_as_zero():
    result = prepare(Directory(total=None, complete=False))
    assert "2/未知" in result.coverage_note
    assert not result.result_sets[0].coverage.complete


def test_cancellation_prevents_directory_access():
    directory = Directory()
    event = Event()
    event.set()
    with pytest.raises(AskCancelled):
        prepare(directory, cancel_event=event)
    assert directory.calls == []


def test_missing_summaries_share_context_and_element_budget_without_changing_cards():
    from app.models.sources import PaginatedSourceElements, SourceElement

    class Pages:
        def __init__(self):
            self.calls = []

        def source_elements_page(self, source_id, offset=0, limit=1):
            self.calls.append((source_id, offset))
            return PaginatedSourceElements(items=[SourceElement(
                id=f"{source_id}-{offset}", source_id=source_id,
                element_type="paragraph", location_label="正文", text="真实内容" * 100,
            )], total_count=10, offset=offset, limit=limit)

    directory = Directory()
    directory.items = tuple(replace(item, summary="") for item in directory.items)
    catalog = prepare(directory)
    preview_keys = set(catalog.id_map)
    reader = Pages()
    supplement_missing_summaries(catalog, reader, budget_chars=2000, max_elements=4,
                                generation_reader=lambda _: "v1")
    assert len(reader.calls) == 4
    assert {source for source, _ in reader.calls} == {"a", "b"}
    assert len(catalog.context_block) <= 2000
    assert len(catalog.id_map) == len(preview_keys) + 4
    assert all(item.text == "" for item in catalog.result_sets[0].items)
    assert catalog.result_sets[0].synthesis_rows == 2
    assert "不代表覆盖所有章节" in catalog.coverage_note


def test_no_remaining_budget_does_not_read_originals():
    catalog = prepare()
    supplement_missing_summaries(catalog, object(), budget_chars=len(catalog.context_block),
                                max_elements=4, generation_reader=lambda _: "v1")
    assert len(catalog.id_map) == 2
