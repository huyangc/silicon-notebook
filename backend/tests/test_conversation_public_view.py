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
    MAX_REFERENCED_ASSETS,
    MAX_REFERENCE_TITLE_CHARS,
    MAX_SNIPPET_CHARS,
    MAX_TURNS,
    conversation_asset_alias,
    referenced_asset_ids,
    resolve_conversation_asset_alias,
    public_conversation_payload as _project_conversation,
    public_turn as _project_turn,
)
from app.services.evidence_context import CITATION_IMAGES_PER_ANSWER


def test_endpoint_scan_cap_covers_every_alias_the_projection_can_emit():
    """Invariant (codex T4 review P2): the endpoint's alias reverse-lookup cap
    must be >= the MOST distinct image aliases the projection can ever emit, or a
    well-formed long conversation would show an image whose alias the endpoint
    stops scanning before reaching. The projection's ceiling is ``MAX_TURNS``
    turns x the upstream per-answer image cap. Pinned across both modules so a
    future bump to either constant can't silently reopen the gap."""
    assert MAX_REFERENCED_ASSETS >= MAX_TURNS * CITATION_IMAGES_PER_ANSWER


# T4 made the projection require the share token (to derive each image's opaque
# alias) and the deployment image switch. These same-named thin wrappers thread
# a fixed token + images-on default so every T3 assertion below stays
# byte-identical with zero call-site churn; the T4 tests pass the switch
# explicitly, and the real endpoint (test_public_conversation_asset_api.py)
# exercises the un-defaulted underlying function end to end.
_SHARE_TOKEN = "conversation-share-token"


def public_turn(turn, *, share_token=_SHARE_TOKEN, images_enabled=True):
    return _project_turn(turn, share_token=share_token, images_enabled=images_enabled)


def public_conversation_payload(row, *, share_token=_SHARE_TOKEN, images_enabled=True):
    return _project_conversation(
        row, share_token=share_token, images_enabled=images_enabled
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
        "key", "title", "file_name", "location", "snippet",
        "title_truncated", "snippet_truncated", "file_name_truncated",
        "is_image_reference",
    }
    # And the turn itself exposes no reasoning/id surface. ``images`` is the
    # only T4 addition; it carries aliases + captions, never addressable ids.
    assert set(turn) == {
        "question", "answer_md", "asked_at", "answered_at", "evidence_level",
        "references", "reference_count", "truncated_references",
        "omitted_result_sets", "images",
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


# ---- 承重 ③ (T4):附图作为 token 别名外发,绝不泄露 asset_id/element_id -----


def test_answer_images_are_projected_as_token_aliases_not_raw_ids():
    """T4: an answer-attached image crosses as an opaque, token-derived alias,
    caption and visible reference key — never its raw ``asset_id``/``element_id``. Emitting the raw
    ``asset_id`` (or the ``element_id``) into ``PublicImage`` turns this red."""
    payload = {
        "answer": "带图证据 [k1]。",
        "anchors": [_anchor("k1")],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))

    assert turn["images"] == [{
        "alias": conversation_asset_alias(_SHARE_TOKEN, "ASSET-k1"),
        "caption": "图注",
        "reference_keys": ["k1"],
    }]
    # The raw handles never appear anywhere in the turn.
    haystack = _all_strings(turn)
    assert "ASSET-k1" not in haystack   # raw asset_id
    assert "IMGELE-k1" not in haystack  # element_id dropped entirely
    # References themselves never gain an image field.
    assert "images" not in turn["references"][0]


def test_image_alias_is_derived_from_the_share_token():
    """The alias is HMAC(asset_id) under the share token, so a DIFFERENT token
    produces a different alias — that is what makes revocation total and
    cross-link correlation impossible. Load-bearing for the endpoint round-trip:
    if the alias ignored the token, the two channels would still agree but the
    security properties would be gone."""
    a = conversation_asset_alias("token-A", "ASSET-x")
    b = conversation_asset_alias("token-B", "ASSET-x")
    assert a != b
    assert a == conversation_asset_alias("token-A", "ASSET-x")  # deterministic
    assert len(a) == 32


def test_images_are_deduped_by_asset_id_across_selected_references():
    """The same image cited by two selected anchors shows once (first-seen)."""
    payload = {
        "answer": "首 [k1] 再 [k2]。",
        "anchors": [
            _anchor("k1", images=[{"element_id": "E1", "asset_id": "SHARED",
                                   "caption": "甲"}]),
            _anchor("k2", images=[{"element_id": "E2", "asset_id": "SHARED",
                                   "caption": "乙"}]),
        ],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    assert turn["images"] == [{
        "alias": conversation_asset_alias(_SHARE_TOKEN, "SHARED"),
        "caption": "甲",  # first-seen caption wins
        "reference_keys": ["k1", "k2"],
    }]


def test_images_come_from_selected_references_only():
    """Images ride the SELECTED references. When anchors win, a citation's
    images do not appear (mirrors the reference selection)."""
    payload = {
        "answer": "锚点 [k1]。",
        "anchors": [_anchor("k1", images=[{"element_id": "AE", "asset_id": "A-ASSET",
                                            "caption": "锚图"}])],
        "citations": [_citation(1, images=[{"element_id": "CE", "asset_id": "C-ASSET",
                                            "caption": "引图"}])],
    }
    turn = public_turn(_turn("q", payload))
    aliases = [image["alias"] for image in turn["images"]]
    assert aliases == [conversation_asset_alias(_SHARE_TOKEN, "A-ASSET")]
    assert conversation_asset_alias(_SHARE_TOKEN, "C-ASSET") not in aliases


def test_images_are_empty_when_the_deployment_stores_no_images():
    """MINERU_RETURN_IMAGES off -> the projection emits no aliases, so the page
    never hands out a handle to bytes the deployment declined to serve."""
    payload = {
        "answer": "带图证据 [k1]。",
        "anchors": [_anchor("k1")],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload), images_enabled=False)
    assert turn["images"] == []


def test_malformed_scalar_image_is_skipped_not_crashed():
    """A non-dict image entry (hand-edited/ancient row) is skipped, matching the
    ``_as_list``/``isinstance`` discipline the rest of the module uses."""
    payload = {
        "answer": "锚点 [k1]。",
        "anchors": [_anchor("k1", images=[7, {"asset_id": "OK", "caption": "c"},
                                          "junk"])],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    assert turn["images"] == [{
        "alias": conversation_asset_alias(_SHARE_TOKEN, "OK"),
        "caption": "c",
        "reference_keys": ["k1"],
    }]


def test_citation_fallback_images_carry_the_positional_body_key():
    payload = {
        "answer": "引用图见 [1]。",
        "anchors": [],
        "citations": [_citation(1, images=[{
            "element_id": "E", "asset_id": "C-ASSET", "caption": "图",
        }])],
    }
    turn = public_turn(_turn("q", payload))
    assert turn["images"][0]["reference_keys"] == ["1"]


def test_image_keeps_its_body_key_when_the_empty_reference_card_is_filtered():
    payload = {
        "answer": "图片位置 [k4]。",
        "anchors": [_anchor(
            "k4",
            source_title="",
            label="",
            name="",
            source_file_name="",
            snippet="",
            images=[{"element_id": "E", "asset_id": "IMAGE-ONLY", "caption": "图"}],
        )],
        "citations": [],
    }
    turn = public_turn(_turn("q", payload))
    assert turn["references"] == []
    assert turn["images"][0]["reference_keys"] == ["k4"]


# ---- T4:别名反查(端点侧)与投影共用同一份派生 --------------------------


def test_alias_round_trips_against_the_referenced_assets():
    """The alias the projection emits resolves back to its asset_id via the
    endpoint helper — the two share ``conversation_asset_alias``. And the token
    is load-bearing: the SAME alias resolves to nothing under another token
    (the endpoint would 404)."""
    row = {"turns": [_turn("q", {
        "answer": "带图 [k1]。",
        "anchors": [_anchor("k1", images=[{"element_id": "E", "asset_id": "REF-ASSET",
                                           "caption": "c"}])],
        "citations": [],
    })]}
    alias = conversation_asset_alias(_SHARE_TOKEN, "REF-ASSET")
    assert resolve_conversation_asset_alias(row, _SHARE_TOKEN, alias) == "REF-ASSET"
    # Wrong token -> no match (revocation / cross-link isolation).
    assert resolve_conversation_asset_alias(row, "other-token", alias) is None


def test_unreferenced_asset_alias_does_not_resolve():
    """An alias for an asset that is NOT referenced anywhere in the snapshot
    resolves to nothing — the endpoint serves only referenced assets."""
    row = {"turns": [_turn("q", {
        "answer": "带图 [k1]。",
        "anchors": [_anchor("k1", images=[{"element_id": "E", "asset_id": "IN-SNAP",
                                           "caption": "c"}])],
        "citations": [],
    })]}
    stranger = conversation_asset_alias(_SHARE_TOKEN, "NOT-IN-SNAPSHOT")
    assert resolve_conversation_asset_alias(row, _SHARE_TOKEN, stranger) is None


def test_referenced_asset_ids_match_the_projected_selection_exactly():
    """The endpoint enumeration is EXACTLY the projected selection, NOT the old
    anchors ∪ citations superset (codex #522 R5): an image on an UNSELECTED
    reference is never enumerated, so its alias 404s. The share token is public
    (it is in the image URL), so a collaborator who knows a raw ``asset_id`` can
    compute its alias themselves — enumerating an un-projected asset would let
    them fetch an image the page never showed.

    Here anchors win (body has ``[k1]``), so the citation's image ``B`` is
    unselected; only the selected anchor's image ``A`` is served.

    Mutation guard: reverting ``referenced_asset_ids`` to the old superset walk
    (all anchors ∪ citations) makes ``B`` resolve and reds this."""
    row = {"turns": [_turn("q", {
        "answer": "锚点 [k1]。",
        "anchors": [_anchor("k1", images=[{"element_id": "AE", "asset_id": "A",
                                           "caption": ""}])],
        "citations": [_citation(1, images=[{"element_id": "CE", "asset_id": "B",
                                            "caption": ""}])],
    })]}
    # Only the selected anchor's image is enumerated — the endpoint can serve
    # exactly the aliases the page emitted, no more.
    assert referenced_asset_ids(row) == ["A"]
    # The selected image's alias resolves; the unselected one 404s (None).
    assert resolve_conversation_asset_alias(
        row, _SHARE_TOKEN, conversation_asset_alias(_SHARE_TOKEN, "A")
    ) == "A"
    assert resolve_conversation_asset_alias(
        row, _SHARE_TOKEN, conversation_asset_alias(_SHARE_TOKEN, "B")
    ) is None


def test_referenced_asset_ids_dedupe_selected_images_across_turns():
    """Selected images are deduped across turns in first-seen order. With no
    anchor markers the fallback selects citations, so each turn's cited image is
    enumerated; the repeated asset is deduped to one entry."""
    row = {"turns": [
        _turn("q1", {"answer": "见清单一。", "anchors": [], "citations": [
            _citation(1, images=[{"element_id": "E1", "asset_id": "C1", "caption": ""}])]}),
        _turn("q2", {"answer": "见清单二。", "anchors": [], "citations": [
            _citation(2, images=[{"element_id": "E2", "asset_id": "C1", "caption": ""}])]}),
    ]}
    assert referenced_asset_ids(row) == ["C1"]  # repeat deduped


def test_empty_or_blank_alias_never_resolves():
    row = {"turns": [_turn("q", {
        "answer": "带图 [k1]。",
        "anchors": [_anchor("k1", images=[{"element_id": "E", "asset_id": "X",
                                           "caption": "c"}])],
        "citations": [],
    })]}
    assert resolve_conversation_asset_alias(row, _SHARE_TOKEN, "") is None
    assert resolve_conversation_asset_alias(row, _SHARE_TOKEN, "   ") is None


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


def test_reference_truncation_is_disclosed_not_silent():
    """A title past ``MAX_REFERENCE_TITLE_CHARS`` / an excerpt past
    ``MAX_SNIPPET_CHARS`` is clipped to its bounded prefix, but the clip is
    DISCLOSED via ``title_truncated``/``snippet_truncated`` — not dropped
    silently (codex #522 R3; AGENTS.md 用户编辑的数据不得静默截断). Mutation guard:
    hardcoding either flag to ``False`` reds this."""
    long_title = "标" * (MAX_REFERENCE_TITLE_CHARS + 100)
    long_snippet = "摘" * (MAX_SNIPPET_CHARS + 100)
    long_file = "档" * (MAX_REFERENCE_TITLE_CHARS + 100)
    payload = {
        "answer": "见 [k1]。",
        "anchors": [_anchor("k1", source_title=long_title, snippet=long_snippet,
                            source_file_name=long_file)],
        "citations": [],
    }
    ref = public_turn(_turn("q", payload))["references"][0]

    assert ref["title"] == "标" * MAX_REFERENCE_TITLE_CHARS   # bounded prefix
    assert ref["title_truncated"] is True
    assert ref["snippet"] == "摘" * MAX_SNIPPET_CHARS
    assert ref["snippet_truncated"] is True
    # The original uploaded filename is client-supplied user data too
    # (codex #522 R4).
    assert ref["file_name"] == "档" * MAX_REFERENCE_TITLE_CHARS
    assert ref["file_name_truncated"] is True


def test_reference_within_caps_is_not_flagged_truncated():
    """A title/excerpt at or under the cap must not raise the disclosure flag —
    otherwise every normal reference would falsely claim it was clipped."""
    ref = public_turn(_turn("q", {
        "answer": "见 [k1]。",
        "anchors": [_anchor("k1", source_title="短标题", snippet="短摘录")],
        "citations": [],
    }))["references"][0]
    assert ref["title_truncated"] is False
    assert ref["snippet_truncated"] is False
    assert ref["file_name_truncated"] is False


def test_image_reference_flag_compares_internal_ids_but_exposes_only_a_boolean():
    """Direct image evidence lets the public UI suppress duplicated parser
    description; nearby images on a text reference must not suppress the real
    excerpt. Neither compared element id crosses the boundary."""
    direct = public_turn(_turn("q", {
        "answer": "图 [k1]。",
        "anchors": [_anchor(
            "k1",
            element_id="DIRECT-IMAGE-ELEMENT",
            images=[{
                "element_id": "DIRECT-IMAGE-ELEMENT",
                "asset_id": "DIRECT-ASSET",
                "caption": "图注",
            }],
        )],
        "citations": [],
    }))["references"][0]
    nearby = public_turn(_turn("q", {
        "answer": "文 [k1]。",
        "anchors": [_anchor(
            "k1",
            element_id="TEXT-ELEMENT",
            images=[{
                "element_id": "NEARBY-IMAGE-ELEMENT",
                "asset_id": "NEARBY-ASSET",
                "caption": "图注",
            }],
        )],
        "citations": [],
    }))["references"][0]

    assert direct["is_image_reference"] is True
    assert nearby["is_image_reference"] is False
    assert "DIRECT-IMAGE-ELEMENT" not in _all_strings(direct)
    assert "NEARBY-IMAGE-ELEMENT" not in _all_strings(nearby)


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


def test_a_long_question_is_served_whole_not_truncated():
    """The projection must serve the question WHOLE (like ``answer_md``), never
    silently truncate the very text that produced the answer (codex #522 R1).
    Mutation guard: restoring ``_text(..., 2000)`` here drops it to 2,000.

    5,000 chars is past today's write-side rail (``ASK_QUESTION_MAX_CHARS``), so
    this input now stands for a turn written BEFORE that rail — exactly the row
    the projection still has to render faithfully rather than clip."""
    long_q = "问" * 5000
    turn = public_turn(_turn(long_q, {"answer": "答。", "anchors": [], "citations": []}))
    assert turn["question"] == long_q
    assert len(turn["question"]) == 5000  # no 2,000 cap


def test_public_question_is_bounded_by_the_write_side_rail():
    """"Served whole" is only a BOUNDED promise because the write side refuses an
    over-length question — the two halves are one guardrail and this pins them
    together (codex #525 R1 P2, raised against the report projection and equally
    true here).

    Without the rail an anonymous response would be unbounded by client input;
    without "serve whole" the page would silently clip a user's own artifact.
    Relaxing either half alone fails here: drop ``AskRequest.question``'s
    ``max_length`` and the model stops refusing; re-cap ``_question_text`` and
    the sibling test above goes red."""
    from pydantic import ValidationError

    from app.models.ask import ASK_QUESTION_MAX_CHARS, AskRequest

    at_cap = "问" * ASK_QUESTION_MAX_CHARS
    assert len(AskRequest(question=at_cap).question) == ASK_QUESTION_MAX_CHARS
    try:
        AskRequest(question=at_cap + "问")
    except ValidationError:
        pass
    else:  # pragma: no cover - the whole point of the test
        raise AssertionError(
            "AskRequest accepted an over-length question; the public projection "
            "serves it verbatim, so an anonymous response is now unbounded by "
            "client input"
        )

    # And what the write side does admit, the projection still emits whole — no
    # second, quieter cap hiding inside the disclosure boundary.
    turn = public_turn(_turn(at_cap, {"answer": "答。", "anchors": [], "citations": []}))
    assert turn["question"] == at_cap


def test_a_long_title_is_served_whole_not_truncated():
    """The authenticated UI shows the title whole; the projection must not
    silently truncate it at the retired 400-char cap (codex #522 R2, same red
    line as the question). Mutation guard: restoring
    ``_text(row.get("title"), 400)`` drops it to 400.

    1,000 chars is past today's write-side rail
    (``CONVERSATION_TITLE_MAX_CHARS``), so this input now stands for a title
    renamed BEFORE that rail — exactly the row the projection still has to
    render faithfully rather than clip."""
    long_title = "标" * 1000
    payload = public_conversation_payload({
        "title": long_title, "created_at": "2026-01-01T00:00:00",
        "shared_through_at": "2026-01-01T00:00:05",
        "turns": [],
    })
    assert payload["title"] == long_title
    assert len(payload["title"]) == 1000  # no 400 cap


def test_public_title_is_bounded_by_the_write_side_rail():
    """The title half of the same two-halves guardrail the question already has.

    "Served whole" is a BOUNDED promise only because the write side refuses an
    over-length title (codex #525 R1 P2, raised against the report projection and
    equally true here). Renaming is the only way a title grows past the 60
    characters ``ensure_conversation`` slices off the first question, so
    ``ConversationRenameRequest`` is the whole write side of this field.

    Relaxing either half alone fails here: drop the ``max_length`` and the model
    stops refusing; re-cap ``_title_text`` and the sibling test above goes red."""
    from pydantic import ValidationError

    from app.models.ask import CONVERSATION_TITLE_MAX_CHARS, ConversationRenameRequest

    at_cap = "标" * CONVERSATION_TITLE_MAX_CHARS
    assert len(ConversationRenameRequest(title=at_cap).title) == CONVERSATION_TITLE_MAX_CHARS
    try:
        ConversationRenameRequest(title=at_cap + "标")
    except ValidationError:
        pass
    else:  # pragma: no cover - the whole point of the test
        raise AssertionError(
            "ConversationRenameRequest accepted an over-length title; the public "
            "projection serves it verbatim, so an anonymous response is now "
            "unbounded by client input"
        )

    # And what the write side does admit, the projection still emits whole — no
    # second, quieter cap hiding inside the disclosure boundary.
    payload = public_conversation_payload({
        "title": at_cap, "created_at": "2026-01-01T00:00:00",
        "shared_through_at": "2026-01-01T00:00:05",
        "turns": [],
    })
    assert payload["title"] == at_cap


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


def test_gap_suggestions_never_reach_the_public_projection():
    """Gap suggestions (``ask.gap_consult``) stay inside the authenticated app.

    They are not evidence and not part of the answer, so a shared link has no
    business carrying them: the URLs record what a *deployment plugin* was
    asked about this reader's question, which is a signal about the run rather
    than about the material the page exists to show.

    The projection is a whitelist, so this holds constructively — but only for
    as long as the emitted key set is the frozen one below.  Both halves matter:
    the substring assertions catch a field added under a different name, and
    the frozen key set catches a field added under any name at all.
    """
    turn = public_turn(_turn("引力波探测的最新进展？", {
        "answer": "见 [k1]。",
        "anchors": [_anchor("k1")],
        "gap_suggestions": [{
            "title": "LIGO O4 run summary",
            "url": "https://example.org/ligo-o4.pdf",
            "summary": "Detector sensitivity and event rate for the O4 run.",
            "source_label": "arXiv",
        }],
    }))

    rendered = json.dumps(turn, ensure_ascii=False)
    for secret in (
        "LIGO O4 run summary", "ligo-o4.pdf", "example.org",
        "Detector sensitivity", "arXiv", "gap_suggestion",
    ):
        assert secret not in rendered, secret
    assert set(turn) == {
        "question", "answer_md", "asked_at", "answered_at", "evidence_level",
        "references", "reference_count", "truncated_references",
        "omitted_result_sets", "images",
    }
    # The turn is otherwise projected normally — this is an exclusion, not a
    # payload that silently fails to render.
    assert turn["references"][0]["key"] == "k1"
