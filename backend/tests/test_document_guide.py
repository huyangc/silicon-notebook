import json
import html
from dataclasses import replace

import pytest
from app.services.collection_enumeration import SourceItem
from app.services.document_catalog_overview import CatalogOverview
from app.services.document_guide import GUIDE_SCHEMA_HINT, guide_style_instruction, render_document_guide


def catalog(*, preview=True, second_summary="乙文说明图索引", title="甲文"):
    items = [
        SourceItem("a", title, "PDF", "甲文说明稀疏计算", "nb", "personal"),
        SourceItem("b", "乙文", "PDF", second_summary, "nb", "personal"),
    ]
    refs = {"k5001": {"object_type": "source", "object_id": "a", "source_id": "a"}}
    if preview:
        refs["k5002"] = {"object_type": "source", "object_id": "b", "source_id": "b"}
    return CatalogOverview("", refs, {}, [], "", items)


def test_omitted_document_gets_explicit_summary_without_repeating_generated_prose():
    data = {"documents": [{"reference": "k5001", "purpose": "优化稀疏计算", "method": "分块", "contribution": "减少计算"}]}
    answer = render_document_guide(data, catalog())
    assert answer.count("优化稀疏计算") == 1
    assert "### 1. 甲文" in answer and "### 2. 乙文" in answer
    assert "已存摘要摘录：乙文说明图索引 [k5002]" in answer
    assert "模型分项介绍 1 篇，摘要回退 1 篇" in answer


def test_foreign_reference_cannot_be_reassigned_to_another_title():
    answer = render_document_guide({"documents": [{"reference": "k5001", "title": "伪造标题", "purpose": "错误归属 [k5002]"}]}, catalog())
    assert "错误归属" not in answer and "伪造标题" not in answer
    assert "甲文说明稀疏计算 [k5001]" in answer


def test_unsupported_document_cannot_be_introduced_from_title_only():
    answer = render_document_guide({"documents": [{"reference": "k5002", "purpose": "标题猜想"}]}, catalog(second_summary=""))
    assert "标题猜想" not in answer
    assert "不能仅凭标题判断正文内容" in answer
    assert "证据不足 1 篇" in answer


def test_supplemental_original_evidence_can_support_missing_summary():
    prepared = catalog(second_summary="")
    prepared.id_map["k7001"] = {"object_type": "element", "object_id": "el-b", "source_id": "b", "element_id": "el-b"}
    prepared.id_map["k7002"] = {"object_type": "element", "object_id": "el-c", "source_id": "b", "element_id": "el-c"}
    answer = render_document_guide({
        "documents": [{"reference": "k5002", "purpose": "原文说明图索引 [k7001]"}],
        "relationships": [{"description": "优化主题相通", "references": ["k5001", "k5002"]}],
        "reading_order": [{"reference": "k5002", "reason": "先了解索引"}],
    }, prepared)
    assert "原文说明图索引 [k7001][k7002]" in answer
    assert "[k5002]" not in answer
    assert "模型分项介绍 1 篇" in answer
    assert "优化主题相通 [k5001][k7001][k7002]" in answer
    assert "先了解索引 [k7001][k7002]" in answer


def test_titles_and_summary_cannot_inject_markdown_or_citation_handles():
    prepared = catalog(title="[k9999]\n<script>*标题*</script>", second_summary="[k7777](javascript:evil) **摘要**")
    answer = render_document_guide({}, prepared)
    assert "[k9999]" not in answer and "[k7777]" not in answer
    assert "<script>" not in answer
    assert "［k9999］" in answer
    assert r"\*\*摘要\*\*" in answer


def test_unshown_preview_has_identity_but_no_summary_or_generated_claim():
    prepared = catalog(preview=False)
    answer = render_document_guide({"documents": [{"reference": "k5002", "purpose": "不应接受"}]}, prepared)
    assert "### 2. 乙文" in answer
    assert "乙文说明图索引" not in answer and "不应接受" not in answer
    assert "未进入合成预览 1 篇" in answer
    assert prepared.result_sets == []


def test_headings_distinguish_current_notebook_and_reference_libraries():
    prepared = catalog()
    prepared.active_notebook_id = "nb"
    prepared.items[1] = replace(prepared.items[1], notebook_id="base", tier="personal")
    answer = render_document_guide({}, prepared)
    assert "### 1. 甲文（本库）" in answer
    assert "### 2. 乙文（参考库）" in answer


def test_alias_source_reference_is_not_original_content_evidence():
    prepared = catalog(second_summary="")
    prepared.id_map["k6002"] = {"object_type": "source", "object_id": "b", "source_id": "b"}
    answer = render_document_guide({"documents": [{"reference": "k6002", "purpose": "标题猜想"}]}, prepared)
    assert "标题猜想" not in answer


def test_duplicate_unknown_and_invalid_fields_use_fallback():
    data = {"documents": [
        {"reference": "k5001", "purpose": "第一版"},
        {"reference": "k5001", "purpose": "重复版"},
        {"reference": "k5002", "method": ["无效"]},
        {"reference": "k9999", "purpose": "越权内容"},
    ]}
    answer = render_document_guide(data, catalog())
    assert "第一版" not in answer and "重复版" not in answer and "越权内容" not in answer
    assert "摘要回退 2 篇" in answer


def test_relationships_and_read_order_use_bound_titles_and_discard_unknown_sources():
    data = {
        "relationships": [{"description": "都是优化研究", "references": ["k5001", "k5002"]},
                          {"description": "未提供关系", "references": ["k7777"]}],
        "reading_order": [{"reference": "k5002", "reason": "先理解图索引"},
                          {"reference": "k7777", "reason": "无效建议"}],
    }
    answer = render_document_guide(data, catalog())
    assert "都是优化研究 [k5001][k5002]" in answer
    assert "1. 乙文：先理解图索引 [k5002]" in answer
    assert "未提供关系" not in answer and "无效建议" not in answer


def test_schema_and_instruction_match_bound_document_references():
    assert json.loads(GUIDE_SCHEMA_HINT)["documents"][0]["reference"] == "k5001"
    assert "k5001, k5002" in guide_style_instruction(catalog())


def test_reference_groups_reject_unknown_keys_and_accept_own_evidence():
    data = {"documents": [{"reference": "k5001", "purpose": "错误 [k5001, k9999]"},
                          {"reference": "k5002", "purpose": "图索引 [k5002]"}]}
    answer = render_document_guide(data, catalog())
    assert "错误" not in answer
    assert "图索引 [k5002]" in answer


def test_malformed_reference_identity_cannot_create_a_link_or_synthesis_slot():
    prepared = catalog(preview=False)
    prepared.id_map["k5002](javascript:evil)"] = {"object_type": "source", "object_id": "b"}
    answer = render_document_guide({"documents": [{"reference": "k5002](javascript:evil)", "purpose": "伪造"}]}, prepared)
    assert "javascript:" not in answer and "伪造" not in answer
    assert "未进入合成预览 1 篇" in answer


@pytest.mark.parametrize("marker", ["【k5002】", "【 k5001，k5002 】", "[2]", "【2】", "[k5001, 2]"])
def test_foreign_localized_markers_and_display_aliases_cannot_reassign_content(marker):
    answer = render_document_guide({"documents": [{"reference": "k5001", "purpose": "错误归属 " + marker}]}, catalog())
    assert "错误归属" not in answer
    assert "甲文说明稀疏计算 [k5001]" in answer


def test_own_localized_markers_bind_only_to_the_rendered_source():
    answer = render_document_guide({"documents": [{"reference": "k5001", "purpose": "稀疏计算【k5001】"}]}, catalog())
    assert "稀疏计算 [k5001]" in answer
    assert "【k5001】" not in answer


@pytest.mark.parametrize("marker", ["[k5002]", "【k5002】", "[2]", "【2】", "[k5001，2]", "&#91;k5002&#93;", "&#x3010;k5002&#x3011;"])
def test_authored_marker_text_stays_literal_after_markdown_entity_decode(marker):
    from app.services.document_guide import _DISPLAY_MARKERS, _plain

    literal = html.unescape(_plain(marker))
    assert not _DISPLAY_MARKERS.search(literal)
    answer = render_document_guide({}, catalog(title=marker, second_summary=marker))
    # Only server-appended markers survive the Markdown text decoding pass.
    decoded = html.unescape(answer)
    assert [match.group() for match in _DISPLAY_MARKERS.finditer(decoded)] == ["[k5001]", "[k5002]"]
