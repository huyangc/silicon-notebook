"""T6（深度报告引用卡附图）公开分享白名单守卫：`report_public_view.
public_reference()` 是显式白名单投影（不是脱敏管线），本文件钉死 `images`/
`asset_id` 永不跨出匿名边界——这条红线的完整背景见模块自身的 docstring。

`public_reference()` 逐字构造一个键集固定的新 dict，天然不会转发调用方塞进
输入 dict 的任何额外键；但「天然安全」不是「经过验证」，所以本文件仍显式钉住它
——即使未来有人把这份函数改写成"复制输入再删几个键"的形状，这条测试也应该
报红。见 T6 任务简报要求的删除变异验证（在本次实现的验证记录里，非本文件内容）。

本文件还钉住第二条红线：这份投影的截断必须**披露**而不是静默丢尾（AGENTS.md
「用户编辑的数据不得静默截断」）。姊妹面 `conversation_public_view` 由 codex #522
R1-R4 收口，本面同批平移——问题原样返回，引用的标题/摘录/原始文件名仍有界但超限
会置 `*_truncated`。
"""
from __future__ import annotations

from app.models.reports import REPORT_QUESTION_MAX_CHARS
from app.services.report_public_view import (
    MAX_REFERENCE_TITLE_CHARS,
    MAX_SNIPPET_CHARS,
    public_reference,
    public_report_payload,
)

ALLOWLIST_KEYS = {
    "key",
    "title",
    "file_name",
    "location",
    "snippet",
    "title_truncated",
    "snippet_truncated",
    "file_name_truncated",
}


def test_public_reference_never_carries_images_even_when_the_input_has_them():
    reference = {
        "key": "k1",
        "source_title": "时序手册",
        "source_file_name": "timing.md",
        "location_label": "§1",
        "snippet": "被引用的原文片段",
        # 这些字段一旦跨出，就是把内部 handle 或图片资产 id 发给匿名访客。
        "images": [
            {"element_id": "el-secret-fig", "asset_id": "asset-secret-1",
             "caption": "机密图注"},
        ],
        "source_id": "src-secret",
        "element_id": "el-secret",
        "object_id": "ko-secret",
    }

    projected = public_reference(reference)

    assert "images" not in projected
    assert "asset_id" not in projected
    assert set(projected) == ALLOWLIST_KEYS
    serialized = str(projected)
    for secret in (
        "el-secret-fig", "asset-secret-1", "机密图注",
        "src-secret", "el-secret", "ko-secret",
    ):
        assert secret not in serialized


def test_public_reference_allowlist_shape_is_exactly_the_named_keys():
    """回归门：新增字段必须显式加进 `public_reference()` 才会出现在这里——
    这条断言本身就是"新增字段默认不过白名单"的存在性证明。"""
    projected = public_reference({"key": "k1", "label": "x"})
    assert set(projected) == ALLOWLIST_KEYS


def test_a_short_reference_reports_no_truncation():
    """空转保护：没超上限时三个披露位必须都是 False——否则下面几条「超限置真」
    的断言可以被一个恒真的实现骗过去。"""
    projected = public_reference(
        {"key": "k1", "source_title": "短标题", "source_file_name": "a.md",
         "snippet": "短摘录"}
    )

    assert projected["title_truncated"] is False
    assert projected["snippet_truncated"] is False
    assert projected["file_name_truncated"] is False


def test_an_over_length_title_filename_and_snippet_are_disclosed_not_dropped():
    projected = public_reference(
        {
            "key": "k1",
            "source_title": "标" * (MAX_REFERENCE_TITLE_CHARS + 1),
            "source_file_name": "f" * (MAX_REFERENCE_TITLE_CHARS + 1),
            "snippet": "摘" * (MAX_SNIPPET_CHARS + 1),
        }
    )

    # 仍然有界（证据元数据不是用户自撰 artifact），但截断这件事被说出来了。
    assert len(projected["title"]) == MAX_REFERENCE_TITLE_CHARS
    assert len(projected["file_name"]) == MAX_REFERENCE_TITLE_CHARS
    assert len(projected["snippet"]) == MAX_SNIPPET_CHARS
    assert projected["title_truncated"] is True
    assert projected["file_name_truncated"] is True
    assert projected["snippet_truncated"] is True


def test_a_value_exactly_at_the_cap_is_not_reported_as_truncated():
    """边界：恰好等于上限没有丢任何字符，报「已截断」是在撒谎（也会让每条贴边
    引用都挂上一个假提示）。"""
    projected = public_reference(
        {
            "key": "k1",
            "source_title": "标" * MAX_REFERENCE_TITLE_CHARS,
            "source_file_name": "f" * MAX_REFERENCE_TITLE_CHARS,
            "snippet": "摘" * MAX_SNIPPET_CHARS,
        }
    )

    assert projected["title_truncated"] is False
    assert projected["file_name_truncated"] is False
    assert projected["snippet_truncated"] is False


def test_the_research_question_is_served_whole():
    """问题是用户自撰的 artifact，旧的 2,000 字符上限静默吃掉的正是产生这份报告的
    那段文字。凡是**今天能创建出来的**问题都原样返回、披露位为假——创建端点已经
    拒收更长的输入。"""
    question = "问" * REPORT_QUESTION_MAX_CHARS

    payload = public_report_payload({"question": question, "content_md": ""}, [])

    assert payload["question"] == question
    assert payload["question_truncated"] is False


def test_a_legacy_over_length_question_is_bounded_and_disclosed():
    """创建期护栏上线**之前**建的报告可以带超长问题，而它的分享链接已经发出去了。

    投影必须自己有界（否则匿名响应被客户端输入撑到无界，codex #525 R2 P2），同时
    把这件事说出来——既不静默丢尾，也不改写用户存下来的数据。"""
    question = "问" * (REPORT_QUESTION_MAX_CHARS + 1)

    payload = public_report_payload({"question": question, "content_md": ""}, [])

    assert len(payload["question"]) == REPORT_QUESTION_MAX_CHARS
    assert payload["question_truncated"] is True


def test_the_report_body_is_still_served_whole():
    """空转保护：正文原样返回这条既有行为不能被本次改动带偏。"""
    body = "正文" * 5000

    payload = public_report_payload({"question": "q", "content_md": body}, [])

    assert payload["content_md"] == body
