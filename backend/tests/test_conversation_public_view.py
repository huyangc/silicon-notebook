"""问答会话公开分享 T3:白名单投影服务(纯函数层,无端点、无数据库)。

这里钉的是 `services/conversation_public_view.py` 的**披露边界**:它是白名单
不是脱敏,所以每一条测试都从一个「什么都塞进 payload」的最坏输入出发,断言只有
被点名的字段跨得出去。三条承重项各有独立用例 + 变异证据:

  ① 无 id 跨界:source_id/element_id/object_id/notebook_id/memory_id/
     provenance/knowhow/images 的值一律不出现在投影里;
  ② Memory 引用**保留** title/snippet,但**剥掉** memory_id(自我发布,§五);
  ③ 附图(images)v1 不外发,且不泄露 asset_id/element_id(T4 才接图片通道)。

anchors vs citations 的选择镜像 `frontend/app/answer-formatting.ts` 的
`buildAnswerReferences`(reasoning 锚点权威、否则 citations 兜底),这里用与前端
同构的用例把两份实现钉在一起。

见 docs/superpowers/specs/2026-08-18-conversation-sharing-design_zh.md §五/§七。
"""
from __future__ import annotations

import json
from typing import Any

from app.models.ask import PublicConversation
from app.services.conversation_public_view import (
    MAX_REFERENCES,
    public_conversation_payload,
    public_turn,
)


# ---- helpers -------------------------------------------------------------


def _anchor(key: str, **overrides: Any) -> dict:
    """An AnswerAnchor dict carrying EVERY addressable field non-empty, so a
    projection that leaks one is caught. Callers override the readable half."""
    row = {
        "key": key,
        "object_id": f"OBJ-{key}",
        "object_type": "concept",
        "label": f"标签-{key}",
        "name": f"名称-{key}",
        "definition": "定义",
        "snippet": f"锚点摘录-{key}",
        "source_title": f"论文标题-{key}",
        "source_file_name": f"file-{key}.pdf",
        "location_label": f"第 {key} 节",
        "source_id": f"SRC-{key}",
        "element_id": f"ELE-{key}",
        "tier": "personal",
        "notebook_id": f"NB-{key}",
        "provenance": {"trail": f"PROV-{key}"},
        "knowhow": {"table_id": f"TBL-{key}", "row_id": f"ROW-{key}"},
        "images": [{"element_id": f"IMGELE-{key}", "asset_id": f"ASSET-{key}",
                    "caption": "图注"}],
    }
    row.update(overrides)
    return row


def _citation(index: int, **overrides: Any) -> dict:
    """A Citation dict carrying every addressable field non-empty."""
    row = {
        "label": f"引用标签-{index}",
        "source_id": f"CSRC-{index}",
        "element_id": f"CELE-{index}",
        "location_label": f"位置-{index}",
        "quoted_span": f"引用摘录-{index}",
        "source_file_name": f"cfile-{index}.pdf",
        "tier": "personal",
        "notebook_id": f"CNB-{index}",
        "memory_id": f"MEM-{index}",
        "provenance": {"trail": f"CPROV-{index}"},
        "knowhow": {"table_id": f"CTBL-{index}", "row_id": f"CROW-{index}"},
        "images": [{"element_id": f"CIMGELE-{index}", "asset_id": f"CASSET-{index}",
                    "caption": "图注"}],
    }
    row.update(overrides)
    return row


def _turn(question: str, payload: dict) -> dict:
    return {"answer_id": "ans-1", "question": question, "payload": payload,
            "created_at": "2026-01-01T00:00:01"}


def _all_strings(value: Any) -> list[str]:
    """Every string value anywhere in a nested projection dict/list."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_all_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_all_strings(item))
    return out


# ---- anchors vs citations selection (mirrors buildAnswerReferences) ------


def test_anchor_markers_win_and_carry_their_own_key():
    """Reasoning-mode turn: answer body has `[k1] [k2]`, anchors resolve, so the
    references are the anchors — in marker-appearance order, keyed `k1`/`k2`."""
    payload = {
        "answer": "第一点 [k2]，第二点 [k1]。",
        "anchors": [_anchor("k1"), _anchor("k2")],
        "citations": [_citation(9)],  # present but must be ignored
        "evidence_level": "grounded",
    }
    turn = public_turn(_turn("问题?", payload))

    assert [ref["key"] for ref in turn["references"]] == ["k2", "k1"]
    assert turn["references"][0]["title"] == "论文标题-k2"
    assert turn["references"][0]["snippet"] == "锚点摘录-k2"
    assert turn["references"][0]["file_name"] == "file-k2.pdf"
    assert turn["evidence_level"] == "grounded"
    # The citation must not have leaked in when anchors won.
    assert all("引用" not in ref["title"] for ref in turn["references"])


def test_a_grouped_marker_binds_all_or_nothing():
    """`[k1, k2]` where only k1 resolves leaves the WHOLE group unbound — same
    all-or-nothing rule as buildAnswerReferences (a grouped marker is one
    claim)."""
    payload = {
        "answer": "复合 [k1，k2] 与单独 [k3]。",
        "anchors": [_anchor("k1"), _anchor("k3")],  # k2 absent
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    # k1 dropped (its group had an unknown k2); only the resolvable k3 survives.
    assert [ref["key"] for ref in turn["references"]] == ["k3"]


def test_citations_are_the_fallback_when_no_anchor_marker_resolves():
    """Chunk-mode turn: no `[k]` anchor markers, so references fall back to the
    flat citation list, positionally keyed `1`/`2` (what the body `[1]` binds)."""
    payload = {
        "answer": "结论见 [1] 与 [2]。",
        "anchors": [],
        "citations": [_citation(1), _citation(2)],
    }
    turn = public_turn(_turn("q", payload))
    assert [ref["key"] for ref in turn["references"]] == ["1", "2"]
    assert turn["references"][0]["title"] == "引用标签-1"
    assert turn["references"][0]["snippet"] == "引用摘录-1"


def test_answer_md_prefers_answer_then_conclusion():
    """`answer_md` = answer || conclusion, mirroring the authenticated view
    (`answer.answer || answer.conclusion`)."""
    assert public_turn(_turn("q", {"answer": "正文", "conclusion": "结论"}))[
        "answer_md"] == "正文"
    assert public_turn(_turn("q", {"answer": "", "conclusion": "只有结论"}))[
        "answer_md"] == "只有结论"


# ---- 承重 ①:无 id 跨界 -------------------------------------------------


def test_no_addressable_id_crosses_the_boundary():
    """A turn whose anchors AND citations carry every addressable field must
    project NONE of those values. This is the load-bearing whitelist check:
    injecting `source_id` (or any id) into `public_reference` turns it red."""
    payload = {
        "answer": "锚点 [k1]。",
        "anchors": [_anchor("k1")],
        # citations ignored here (anchors won) but still must not leak via any
        # code path.
        "citations": [_citation(1)],
        # The whole reasoning surface — none of it may appear.
        "reasoning_trace": [{"step_type": "plan", "summary": "内部轨迹泄露词"}],
        "intent": {"objective": "内部意图泄露词"},
        "retrieval_query": "内部检索词",
        "retrieval_scope": {"local": {"selected": 1}},
        "mode": "reasoning",
        "llm_mode": "step",
        "top_relevance": 0.9,
    }
    conv = public_conversation_payload({
        "title": "会话", "created_at": "2026-01-01T00:00:00",
        "shared_through_at": "2026-01-01T00:00:05",
        "turns": [_turn("q", payload)],
    })
    haystack = _all_strings(conv)

    forbidden = [
        "OBJ-k1", "SRC-k1", "ELE-k1", "NB-k1", "PROV-k1", "TBL-k1", "ROW-k1",
        "IMGELE-k1", "ASSET-k1",
        "CSRC-1", "CELE-1", "CNB-1", "MEM-1", "CTBL-1", "CROW-1",
        "CIMGELE-1", "CASSET-1",
        "内部轨迹泄露词", "内部意图泄露词", "内部检索词",
        "reasoning", "step",
    ]
    for token in forbidden:
        assert token not in haystack, f"{token} leaked into the public view"

    # Sanity: the readable half IS present, so the test isn't vacuously green.
    assert "论文标题-k1" in haystack
    assert "锚点摘录-k1" in haystack


def test_projection_keys_are_exactly_the_allowlist():
    """The projected reference dict has EXACTLY the five allowlisted keys — a
    stronger form of "no id crosses" that also catches a leak whose value
    happened not to collide with the forbidden list."""
    turn = public_turn(_turn("q", {
        "answer": "见 [k1]。", "anchors": [_anchor("k1")], "citations": [],
    }))
    assert set(turn["references"][0]) == {
        "key", "title", "file_name", "location", "snippet"
    }
    # And the turn itself exposes no reasoning/id surface.
    assert set(turn) == {
        "question", "answer_md", "asked_at", "answered_at", "evidence_level",
        "references", "reference_count", "truncated_references",
        "omitted_result_sets",
    }


# ---- 承重 ②:Memory 引用保留摘录但剥掉 memory_id ------------------------


def test_memory_citation_keeps_excerpt_but_strips_memory_id():
    """A Memory citation is self-publishing (a user's answer can only cite that
    user's own private Memory), so its title/snippet ARE public — but its
    `memory_id` is an addressable handle and must be stripped. NOT dropped
    wholesale (the excerpt is the point). Leaving `memory_id` in the projection
    turns this red."""
    payload = {
        "answer": "根据我的记忆 [1]。",
        "anchors": [],
        "citations": [_citation(7, label="我的记忆标题", quoted_span="记忆摘录内容",
                                 memory_id="MEM-SECRET-7")],
    }
    turn = public_turn(_turn("q", payload))

    # Kept, not dropped:
    assert len(turn["references"]) == 1
    assert turn["references"][0]["title"] == "我的记忆标题"
    assert turn["references"][0]["snippet"] == "记忆摘录内容"
    # Stripped:
    assert "MEM-SECRET-7" not in _all_strings(turn)
    assert "memory_id" not in turn["references"][0]


# ---- 承重 ③:附图 v1 不外发,不泄露 asset_id/element_id -----------------


def test_answer_images_are_not_exposed_in_v1():
    """Images are T4's anonymous channel; v1's public turn has no image field
    and must not leak `asset_id`/`element_id` from a turn's images."""
    payload = {
        "answer": "带图证据 [k1]。",
        "anchors": [_anchor("k1")],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    assert "images" not in turn
    assert "images" not in turn["references"][0]
    haystack = _all_strings(turn)
    assert "ASSET-k1" not in haystack
    assert "IMGELE-k1" not in haystack


# ---- C-1:清单卡不外发,但留计数信号 ------------------------------------


def test_result_sets_are_counted_not_dropped_silently():
    """`result_sets` are out of v1, but the count crosses so the page can
    disclose the omission (design C-1). Content of the cards never crosses."""
    payload = {
        "answer": "见清单。",
        "anchors": [],
        "citations": [],
        "result_sets": [
            {"kind": "collection", "collection": "sources",
             "items": [{"item_id": "x", "text": "清单内容泄露词"}],
             "coverage": {"returned_total": 1}},
            {"kind": "knowhow", "table_id": "T", "title": "表",
             "coverage": {"total_rows": 1}},
        ],
    }
    turn = public_turn(_turn("q", payload))
    assert turn["omitted_result_sets"] == 2
    assert "清单内容泄露词" not in _all_strings(turn)


def test_no_result_sets_reports_zero():
    turn = public_turn(_turn("q", {"answer": "无清单", "anchors": [], "citations": []}))
    assert turn["omitted_result_sets"] == 0


# ---- 引用编号在过滤后仍与正文对齐(§七 item 4)-------------------------


def test_reference_filter_drops_empty_entries_but_key_keeps_alignment():
    """A reference with neither title nor snippet is dropped, but surviving
    references keep their `key`, so a body `[k3]` still points at the right row
    even though the k2 reference was filtered out. `reference_count` counts the
    pre-filter total (mirrors report `reference_count`)."""
    payload = {
        "answer": "首 [k1] 中 [k2] 末 [k3]。",
        "anchors": [
            _anchor("k1"),
            _anchor("k2", source_title="", label="", name="", snippet=""),
            _anchor("k3"),
        ],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    assert [ref["key"] for ref in turn["references"]] == ["k1", "k3"]
    assert turn["reference_count"] == 3            # pre-filter
    assert turn["truncated_references"] is False


def test_reference_list_is_bounded():
    anchors = [_anchor(f"k{i}") for i in range(1, MAX_REFERENCES + 5)]
    body = " ".join(f"[k{i}]" for i in range(1, MAX_REFERENCES + 5))
    turn = public_turn(_turn("q", {"answer": body, "anchors": anchors, "citations": []}))
    assert len(turn["references"]) == MAX_REFERENCES
    assert turn["truncated_references"] is True


# ---- 顶层装配 + pydantic 模型对齐 --------------------------------------


def test_public_conversation_payload_validates_against_the_model():
    """The assembled dict must round-trip through the response model unchanged —
    proves the projection keys and the `PublicConversation`/`PublicTurn` schema
    agree (a rename on one side would raise here)."""
    payload = {
        "answer": "答案 [k1]。",
        "anchors": [_anchor("k1")],
        "citations": [],
        "asked_at": "2026-01-01T00:00:00+08:00",
        "answered_at": "2026-01-01T00:00:02+08:00",
        "evidence_level": "overview",
    }
    row = {
        "id": "conv-1", "notebook_id": "nb-1", "created_by": "user-1",
        "title": "共享会话", "created_at": "2026-01-01T00:00:00",
        "shared_through_at": "2026-01-01T00:00:05",
        "shared_through_id": "ans-1",
        "turns": [_turn("我的问题?", payload)],
    }
    model = PublicConversation(**public_conversation_payload(row))
    assert model.title == "共享会话"
    assert model.shared_at == "2026-01-01T00:00:05"
    assert len(model.turns) == 1
    assert model.turns[0].question == "我的问题?"
    assert model.turns[0].asked_at == "2026-01-01T00:00:00+08:00"
    assert model.turns[0].evidence_level == "overview"
    assert model.turns[0].references[0].key == "k1"
    # Serialized model carries none of the gate/id surface.
    dumped = json.dumps(model.model_dump(), ensure_ascii=False)
    for token in ("nb-1", "user-1", "OBJ-k1", "SRC-k1", "notebook_id",
                  "created_by", "reasoning_trace"):
        assert token not in dumped


def test_legacy_payload_without_evidence_level_defaults_to_inferred():
    turn = public_turn(_turn("q", {"conclusion": "旧答案"}))
    assert turn["evidence_level"] == "inferred"
    assert turn["references"] == []


def test_duplicate_anchor_key_resolves_last_write_wins_like_the_frontend():
    """A malformed payload with two anchors sharing key ``k1`` must resolve the
    SAME one the author sees — the frontend's ``new Map(...)`` is last-wins
    (codex T3 review, P2). Anchor keys are unique per answer in practice; this
    pins the direction so a future edit can't silently diverge."""
    payload = {
        "answer": "见 [k1]。",
        "anchors": [
            _anchor("k1", source_title="第一个"),
            _anchor("k1", source_title="第二个"),
        ],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    assert [ref["key"] for ref in turn["references"]] == ["k1"]
    assert turn["references"][0]["title"] == "第二个"  # last wins, not "第一个"


def test_truthy_scalar_evidence_fields_degrade_instead_of_500():
    """Stored ``AskResponse`` always serializes anchors/citations/result_sets as
    lists; a truthy SCALAR can only come from a hand-edited/ancient row. The
    anonymous surface must degrade (empty refs, zero omitted), never crash on
    ``for x in 5`` / ``len(5)`` (codex T3 review, P1)."""
    for bad in (5, 1.5, True):
        turn = public_turn(_turn("q", {
            "answer": "见 [k1]。",
            "anchors": bad,
            "citations": bad,
            "result_sets": bad,
        }))
        assert turn["references"] == []
        assert turn["omitted_result_sets"] == 0


def test_one_bad_turn_does_not_topple_the_whole_page():
    """A batch with a scalar-anchors turn beside a normal turn must return BOTH
    turns (the bad one degraded), not 500 the entire conversation page."""
    good = _turn("好问题", {"answer": "见 [k1]。", "anchors": [_anchor("k1")]})
    bad = _turn("坏问题", {"answer": "x", "anchors": 7, "result_sets": 3})
    payload = public_conversation_payload({
        "title": "混合", "created_at": "2026-01-01T00:00:00",
        "shared_through_at": "2026-01-01T00:00:05",
        "turns": [good, bad],
    })
    assert [t["question"] for t in payload["turns"]] == ["好问题", "坏问题"]
    assert payload["turns"][0]["references"][0]["key"] == "k1"
    assert payload["turns"][1]["references"] == []
