"""The workbook lane executes up to three tables from ONE planner call.

User ruling 2026-10-19: one table-analysis plan may name several tables (at
most 2-3; the cap is ``SPREADSHEET_ANALYSIS_MAX_TARGETS``, default 3) and is
still a single planner call, in a notebook ask and in Global Ask alike.

The single-table lane is pinned byte for byte below: the result cards, the
trace step and the synthesis model's table block (text and citable map) are
digests captured on the commit before multi-table planning existed, with the
planner replying in that commit's single-object shape.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.services.spreadsheet_analysis import spreadsheet_prompt_block
from tests.test_spreadsheet_federation import (
    _COMPARE,
    _Planner,
    _Seat,
    _ask,
    _compile,
    _peer,
    _peer_ask,
    _run_lane,
    _service,
)


def _digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(results, trace) -> dict:
    """Every byte the lane hands downstream for one run."""
    block, evidence = spreadsheet_prompt_block(
        results, preview_rows=20, max_bytes=65_536,
    )
    tight_block, tight_evidence = spreadsheet_prompt_block(
        results, preview_rows=20, max_bytes=65_536, max_chars=120,
    )
    return {
        "results": _digest([result.model_dump(mode="json") for result in results]),
        "trace": _digest([
            {"summary": step.summary, "detail": step.detail} for step in trace
        ]),
        "block": _digest(block),
        "evidence": _digest(evidence),
        "tight_block": _digest(tight_block),
        "tight_evidence": _digest(tight_evidence),
    }


def _two_quote_tables(analysis, tmp_path, *, own="nb-1", other="nb-1"):
    _compile(analysis, tmp_path, own, "quote-a", title="A 报价表",
             header=("Item", "Price", "Vendor"),
             rows=(("bolt", 3, "x"), ("nut", 1, "y"), ("gear", 9, "x")))
    _compile(analysis, tmp_path, other, "quote-b", title="B 报价表",
             header=("Item", "Price"), rows=(("bolt", 5), ("nut", 2)))


# One target per scenario, in the reply shape the pre-list planner used.
_SINGLE_TARGETS = {
    "top": {"source_id": "quote-b", "sheet": "Sheet", "operation": "top",
            "sort_by": "Price", "order": "desc"},
    "aggregate": {"source_id": "quote-a", "sheet": "Sheet",
                  "operation": "aggregate", "aggregation": "sum",
                  "measure": "Price", "group_by": "Vendor"},
    "filter": {"source_id": "quote-a", "sheet": "Sheet", "operation": "filter",
               "filters": [{"column": "Price", "operator": "gt", "value": 2}],
               "columns": ["Item", "Price"]},
    # An unknown column: the whole plan falls back to the local profile.
    "invalid": {"source_id": "quote-b", "sheet": "Sheet", "operation": "top",
                "sort_by": "Missing"},
}

# Captured on the parent of the multi-table commit (see module docstring).
_SINGLE_LIBRARY_PINS = {
    "aggregate": {
        "block": "c10c42c98077971e7664f4737f28044cba3f91188349cb9d96ecc30788689bf7",
        "evidence": "e6236d43cb4a7a2ffd537a1d34c57ac75c161838017678f406f95ce2f4d7c166",
        "results": "0f13f8e1ca40e9cd5620f4b321bd41a9fe8e9d249339e77605ddd1a1390d5db2",
        "tight_block": "c10c42c98077971e7664f4737f28044cba3f91188349cb9d96ecc30788689bf7",
        "tight_evidence": "e6236d43cb4a7a2ffd537a1d34c57ac75c161838017678f406f95ce2f4d7c166",
        "trace": "c3086c554433a6db3e8dcdbf8d94701def5ca0c23c51fea5a1388ff78ba2959c",
    },
    "filter": {
        "block": "55ece58f7b75eff0746fc2a844fbe4657f744518d56e2d755104bd25a0ebcb70",
        "evidence": "f229a70372a4558ae71f5ce77bf07d0f52addbe2736ca4b9f727699c31c62760",
        "results": "8419ebc54f2ad104bd61104bd763c12d02a74be179b93713879d9dca697cfe37",
        "tight_block": "55ece58f7b75eff0746fc2a844fbe4657f744518d56e2d755104bd25a0ebcb70",
        "tight_evidence": "f229a70372a4558ae71f5ce77bf07d0f52addbe2736ca4b9f727699c31c62760",
        "trace": "13c420a1a0ddc9bc761eb1e6d55d09de18c3463de0c82c1036263dc98b467def",
    },
    "invalid": {
        "block": "9267e343f72246b78d6f2090adc2dbc5ba509256404bab52ffdda202b5fe6b5e",
        "evidence": "0bd7d57252090e768912b7e7f63ff22af54898de2771fd2e128a897c42930689",
        "results": "dd3a7c972227a6960fa3f1cff3d38be035dfa72de80bfc93462d4b2373a7e11d",
        "tight_block": "da17daf8c019e7402bd7e44c6ddee1829b8749160713fbd1987edd400988ab51",
        "tight_evidence": "989e22a76937f85c085ee6f8cde082d6871e23f692ec749e9effe52740b564e0",
        "trace": "120d4f7a8b97dd9da2b71ef25e7e3beca8b8868cb3e8ed206b47da5a5935aaec",
    },
    "top": {
        "block": "5e04c8e101e82eb416424695feb8a7bd967464354671181499d62842ec7865ca",
        "evidence": "b4b66f00b5cc3f7f1c0d1c31ed1e13eb2024ff570f9594f5b91b0ecdfa9b907d",
        "results": "bbac1216d866617d62512a0f7c98efd900016d4ed1832db1f5e09ac6432ed0fc",
        "tight_block": "5e04c8e101e82eb416424695feb8a7bd967464354671181499d62842ec7865ca",
        "tight_evidence": "b4b66f00b5cc3f7f1c0d1c31ed1e13eb2024ff570f9594f5b91b0ecdfa9b907d",
        "trace": "8c06a75f7409733f2e6d46bc700b8c83cc6f4d11c55affdb1516d59c37e0e2f8",
    },
}
_PEER_PINS = {
    "aggregate": {
        "block": "82b2fcbcc0d3bf1719925cea3db4edc3dc7115c74311b0b426d78e2841e8e1b3",
        "evidence": "aa63c5e43869faa16837c54cfb0494967fd693ed38d53cb02fc9336bcfa30354",
        "results": "7980b4097ac7df59a2ad17e1240948daf36dc9eaed1dd863930cdec282d4f837",
        "tight_block": "4cabb71020a7ff442b9c81e8925b625b99c471dfcae0e3b95a655b47aea39c62",
        "tight_evidence": "3b21b900544be676412b93358ba8abc18deb9e1bfda8a9068a8d1ac5d0f383e6",
        "trace": "01e13a24be43d5b43f1dd13b374f9ea8681af658bd736218d4e6f042037ae78f",
    },
    "filter": {
        "block": "db40de7fcff06d90d86b5815b0d97f57a228656ad9402498a3dfa827c7edd82e",
        "evidence": "83a037d64c6b20f755c7936185ca6eb6db91b53fad438266bc53cd2f0f345d5f",
        "results": "ef6040591135168779b5d8d50164c75d3c5c81c6fa9687b7967497d2c4226b5d",
        "tight_block": "dfc997295ece9f31fc0221d2a382ec7e4b15ca85b0a80dff6358f9b19409a4e7",
        "tight_evidence": "8d26f6ca2c7a2a7f4faaa8b87adebd8f537f52e96b38d85a784c26c22e564910",
        "trace": "9e1e707c765f587b8ae3419f81115b050549d36c3a911f4321d57653cdbedc9e",
    },
    "invalid": {
        "block": "f4f7982139d4ed3e56ef9d16a584ea8cdbbdf4a438c737066f04478f9e345934",
        "evidence": "b9c622e9c5b38f6d5a85b175f298ebe0769e7efe31965737bdbaba361b37784a",
        "results": "e35edd28f5ea32cc1ab48a33a52cbde5b48c4ae9db8483dcc3bc9d5aa5405188",
        "tight_block": "1ff2a24e0a820fdec0a52712eaabc64ced062b31edd25b278a1a15802d96b712",
        "tight_evidence": "8d4d063dd5f150699795318fe3a6a062417f8c7755bc1fbaa056bf56fb4c60bf",
        "trace": "25d0cd9c17359c8ee69872caf5d3773e7f7ad99674f2d14c950150478b4f158f",
    },
    "top": {
        "block": "d0fb4420a5cff4841bbdafead6bcea11f206f876a441aa7a4634159f5ed78817",
        "evidence": "93188637c6f37909cc937d64a3d8a7fed36565f9c091610f70d7b1dd3aef9464",
        "results": "d5389cb5ef31877e31d86bf9488db6ad5b3e39ba3e0fd1e23a1e681b0aaa9892",
        "tight_block": "81d48d365f19eef9215bf3bd62fd5425a26c0ac24933eb99a7735db569f476a5",
        "tight_evidence": "f4126a6c9e04c00160245280b4ea3d3767fc780213cc44e46eed49b77534f5bc",
        "trace": "776f79fe3d946cfe1a18c850d90e36b0c7d0683e0ceeaf2cb3a6c77805c2b9f5",
    },
}

# The pre-list single-object reply (still read as one target) and the target
# list the planner is now asked for: a one-item list must be byte for byte.
_REPLY_SHAPES = {
    "single_object": lambda target: target,
    "target_list": lambda target: {"targets": [target]},
}


def _single_library_run(tmp_path, reply):
    analysis, _artifacts = _service(tmp_path)
    _two_quote_tables(analysis, tmp_path)
    planner = _Planner(reply)
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": ("doc-1", "quote-a", "quote-b"),
    })
    results, trace = _run_lane(ask, "比较这两张 Excel 报价表的价格")
    return planner, results, trace


def _peer_run(tmp_path, reply):
    analysis, _artifacts = _service(tmp_path)
    _two_quote_tables(analysis, tmp_path, own="nb-a", other="nb-b")
    planner = _Planner(reply)
    seat = _Seat((("nb-a", ("quote-a",)), ("nb-b", ("quote-b",))))
    with _peer({"nb-a": {"quote-a"}, "nb-b": {"quote-b"}},
               names={"nb-a": "A 库", "nb-b": "B 库"}) as scope:
        results, trace = _run_lane(
            _peer_ask(analysis, planner, seat, {"nb-a": "personal", "nb-b": "base"}),
            _COMPARE, "nb-a", scope,
        )
    return planner, results, trace


@pytest.mark.parametrize("shape", sorted(_REPLY_SHAPES))
@pytest.mark.parametrize("scenario", sorted(_SINGLE_TARGETS))
def test_single_target_single_library_lane_is_byte_identical(tmp_path, scenario, shape):
    planner, results, trace = _single_library_run(
        tmp_path, _REPLY_SHAPES[shape](_SINGLE_TARGETS[scenario]),
    )
    assert len(planner.prompts) == 1
    assert len(results) == 1
    assert _fingerprint(results, trace) == _SINGLE_LIBRARY_PINS[scenario]


@pytest.mark.parametrize("shape", sorted(_REPLY_SHAPES))
@pytest.mark.parametrize("scenario", sorted(_SINGLE_TARGETS))
def test_single_target_peer_lane_is_byte_identical(tmp_path, scenario, shape):
    planner, results, trace = _peer_run(
        tmp_path, _REPLY_SHAPES[shape](_SINGLE_TARGETS[scenario]),
    )
    assert len(planner.prompts) == 1
    assert len(results) == 1
    assert _fingerprint(results, trace) == _PEER_PINS[scenario]


# ---------------------------------------------------------------------------
# Several tables from one plan
# ---------------------------------------------------------------------------

_TOP_A = {"source_id": "quote-a", "sheet": "Sheet", "operation": "top",
          "sort_by": "Price", "order": "desc"}
_TOP_B = {"source_id": "quote-b", "sheet": "Sheet", "operation": "top",
          "sort_by": "Price", "order": "desc"}


class _RecordingPlanner(_Planner):
    """``_Planner`` that also keeps the schema hint and call options."""

    def __init__(self, plan) -> None:
        super().__init__(plan)
        self.calls: list[tuple[str, dict]] = []

    def chat_json(self, messages, schema_hint, **kwargs) -> str:
        self.calls.append((schema_hint, kwargs))
        return super().chat_json(messages, schema_hint, **kwargs)


def test_one_plan_compares_two_tables_of_one_library(tmp_path):
    """「比较 A、B 两张报价表」: one planner call names both tables; each is
    executed and becomes its own card, in the planner's order, and the
    synthesis block carries both under consecutive keys."""
    for order in ((_TOP_A, _TOP_B), (_TOP_B, _TOP_A)):
        run_dir = tmp_path / order[0]["source_id"]
        run_dir.mkdir()
        planner, results, trace = _single_library_run(
            run_dir, {"targets": list(order)},
        )
        assert len(planner.prompts) == 1
        assert [result.source_id for result in results] == [
            target["source_id"] for target in order
        ]
        assert all(result.operation == "top" for result in results)
        # A notebook ask over its own workbooks: no library on either card.
        assert all(result.notebook_id == "" for result in results)
        [step] = trace
        titles = [f"{result.source_title} · Sheet" for result in results]
        assert step.summary == f"已分析 2 张表：{titles[0]}；{titles[1]}"
        assert [table["source_id"] for table in step.detail["tables"]] == [
            target["source_id"] for target in order
        ]
        assert step.detail["targets"] == {
            "planned": 2, "executed": 2, "invalid": 0, "duplicate": 0,
            "over_limit": 0, "limit": 3,
        }
        block, evidence = spreadsheet_prompt_block(
            results, preview_rows=20, max_bytes=65_536,
        )
        assert list(evidence) == ["k6001", "k6002"]
        assert [evidence[key]["source_id"] for key in evidence] == [
            target["source_id"] for target in order
        ]
        heads = [line for line in block.splitlines() if line.startswith("k600")]
        assert [head.split(":", 1)[0] for head in heads] == ["k6001", "k6002"]


def test_planner_is_asked_for_a_target_list_once(tmp_path):
    analysis, _artifacts = _service(tmp_path)
    _two_quote_tables(analysis, tmp_path)
    planner = _RecordingPlanner({"targets": [_TOP_A, _TOP_B]})
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": ("quote-a", "quote-b"),
    })
    _run_lane(ask, "比较这两张 Excel 报价表的价格")

    [(hint, options)] = planner.calls
    assert json.loads(hint)["targets"][0]["source_id"] == "id"
    # The single-target allowance, once per target the plan may name.
    assert options["max_tokens"] == 1_500
    assert "最多 3 项" in planner.prompts[0]


def test_global_ask_compares_one_quote_table_from_each_library(tmp_path):
    """Global Ask, library A and library B, one quote table each: one plan
    names both, and each card, receipt and block line names its own
    library and tier."""
    planner, results, trace = _peer_run(tmp_path, {"targets": [_TOP_A, _TOP_B]})

    assert len(planner.prompts) == 1
    assert [(r.source_id, r.notebook_id, r.tier, r.notebook_name) for r in results] == [
        ("quote-a", "nb-a", "personal", "A 库"),
        ("quote-b", "nb-b", "base", "B 库"),
    ]
    assert [
        (r.rows[0].citation.notebook_id, r.rows[0].citation.tier) for r in results
    ] == [("nb-a", "personal"), ("nb-b", "base")]
    [step] = trace
    assert step.summary == (
        "已分析 2 张表：「A 库」中的 A 报价表 · Sheet；「B 库」中的 B 报价表 · Sheet"
    )
    assert [(t["source_id"], t["notebook_id"]) for t in step.detail["tables"]] == [
        ("quote-a", "nb-a"), ("quote-b", "nb-b"),
    ]
    assert "notebook_id" not in step.detail
    assert (step.detail["libraries"], step.detail["contributing_libraries"],
            step.detail["workbooks"], step.detail["listed"]) == (2, 2, 2, 2)
    block, evidence = spreadsheet_prompt_block(
        results, preview_rows=20, max_bytes=65_536,
    )
    heads = [line for line in block.splitlines() if line.startswith("k600")]
    assert "library=A 库; " in heads[0] and "library=B 库; " in heads[1]
    assert [evidence[key]["notebook_id"] for key in ("k6001", "k6002")] == [
        "nb-a", "nb-b",
    ]


def _four_tables(analysis, tmp_path):
    for letter in "abcd":
        _compile(analysis, tmp_path, "nb-1", f"quote-{letter}",
                 title=f"{letter.upper()} 报价表", header=("Item", "Price"),
                 rows=(("bolt", 3), ("nut", 1)))


def _target(letter: str) -> dict:
    return {"source_id": f"quote-{letter}", "sheet": "Sheet", "operation": "profile"}


def test_targets_over_the_cap_are_cut_in_plan_order_and_disclosed(tmp_path):
    analysis, _artifacts = _service(tmp_path)
    _four_tables(analysis, tmp_path)
    planner = _Planner({"targets": [_target(letter) for letter in "dbca"]})
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": tuple(f"quote-{letter}" for letter in "abcd"),
    })
    results, trace = _run_lane(ask, "比较这四张 Excel 报价表")

    assert len(planner.prompts) == 1
    assert [result.source_id for result in results] == [
        "quote-d", "quote-b", "quote-c",
    ]
    disclosure = (
        "本次规划了 4 项表格分析，超过单次最多 3 项的上限，按规划顺序只做了前 3 项；"
        "未做：A 报价表 · Sheet（数据概况）。"
    )
    # Said once, on the last card -- where the missing table would follow.
    assert results[-1].warnings[-1] == disclosure
    assert all(disclosure not in result.warnings for result in results[:-1])
    [step] = trace
    assert step.detail["targets"]["over_limit"] == 1
    assert step.detail["targets"]["executed"] == 3


def test_the_cap_is_a_setting(tmp_path):
    analysis, _artifacts = _service(tmp_path, spreadsheet_analysis_max_targets=1)
    _two_quote_tables(analysis, tmp_path)
    planner = _RecordingPlanner({"targets": [_TOP_B, _TOP_A]})
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": ("quote-a", "quote-b"),
    })
    results, trace = _run_lane(ask, "比较这两张 Excel 报价表的价格")

    [result] = results
    assert result.source_id == "quote-b"
    assert result.warnings[-1] == (
        "本次规划了 2 项表格分析，超过单次最多 1 项的上限，按规划顺序只做了前 1 项；"
        "未做：A 报价表 · Sheet（排序分析：按 Price 降序）。"
    )
    [(_hint, options)] = planner.calls
    assert options["max_tokens"] == 500
    assert "最多 1 项" in planner.prompts[0]
    [step] = trace
    assert step.summary == "已分析 B 报价表 · Sheet"
    assert step.detail["targets"]["over_limit"] == 1


@pytest.mark.parametrize("value", [0, 4])
def test_the_cap_setting_is_bounded_to_one_through_three(tmp_path, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _service(tmp_path, spreadsheet_analysis_max_targets=value)


def test_invalid_and_duplicate_targets_are_dropped_on_their_own(tmp_path):
    analysis, _artifacts = _service(tmp_path)
    _four_tables(analysis, tmp_path)
    planner = _Planner({"targets": [
        # An unknown column.
        {"source_id": "quote-a", "sheet": "Sheet", "operation": "top",
         "sort_by": "Missing"},
        _target("b"),
        # An exact repeat of an earlier target (a stray sort_by changes
        # nothing a profile computes).
        {**_target("b"), "sort_by": "Price"},
        # Not an object.
        "quote-c",
        # An unknown source.
        {"source_id": "nope", "sheet": "Sheet"},
        _target("d"),
    ]})
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": tuple(f"quote-{letter}" for letter in "abcd"),
    })
    results, trace = _run_lane(ask, "比较这几张 Excel 报价表")

    # A dropped target never stands in for another table: no fallback to
    # the first sheet while a valid target survives.
    assert [(r.source_id, r.operation) for r in results] == [
        ("quote-b", "profile"), ("quote-d", "profile"),
    ]
    assert all(not result.warnings for result in results)
    [step] = trace
    assert step.detail["targets"] == {
        "planned": 6, "executed": 2, "invalid": 3, "duplicate": 1,
        "over_limit": 0, "limit": 3,
    }


def _same_sheet_run(tmp_path, targets, **overrides):
    analysis, _artifacts = _service(tmp_path, **overrides)
    _two_quote_tables(analysis, tmp_path)
    planner = _Planner({"targets": targets})
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": ("quote-a", "quote-b"),
    })
    results, trace = _run_lane(ask, "统计 A 报价表的价格合计并按价格排名")
    return planner, results, trace


_SUM_A = {"source_id": "quote-a", "sheet": "Sheet", "operation": "aggregate",
          "aggregation": "sum", "measure": "Price", "group_by": "Vendor"}


def test_one_sheet_may_be_summed_and_ranked_in_one_plan(tmp_path):
    """User ruling 2026-10-21: two different analyses of one table are two
    cards, each a seat under the cap; each names its parameters so the two
    cards (and the two synthesis lines) can be told apart."""
    planner, results, trace = _same_sheet_run(tmp_path, [_SUM_A, _TOP_A])

    assert len(planner.prompts) == 1
    assert [(r.source_id, r.operation) for r in results] == [
        ("quote-a", "aggregate"), ("quote-a", "top"),
    ]
    assert [r.analysis_label for r in results] == [
        "Price 求和；按 Vendor 分组", "按 Price 降序",
    ]
    [step] = trace
    assert step.summary == (
        "已对 1 张表做了 2 项分析：A 报价表 · Sheet（Price 求和；按 Vendor 分组）；"
        "A 报价表 · Sheet（按 Price 降序）"
    )
    assert step.detail["targets"]["duplicate"] == 0
    block, evidence = spreadsheet_prompt_block(
        results, preview_rows=20, max_bytes=65_536,
    )
    heads = [line for line in block.splitlines() if line.startswith("k600")]
    assert [head.split(":", 1)[0] for head in heads] == ["k6001", "k6002"]
    assert "operation=aggregate; plan=Price 求和；按 Vendor 分组; " in heads[0]
    assert "operation=top; plan=按 Price 降序; " in heads[1]
    assert [evidence[key]["source_id"] for key in ("k6001", "k6002")] == [
        "quote-a", "quote-a",
    ]
    # The wire carries the label only on the repeated sheet's cards.
    assert results[0].model_dump(mode="json")["analysis_label"]


def test_the_same_operation_on_different_columns_is_two_cards(tmp_path):
    by_vendor = {**_TOP_A, "sort_by": "Vendor", "order": "asc"}
    _planner, results, _trace = _same_sheet_run(tmp_path, [_TOP_A, by_vendor, _TOP_B])

    assert [(r.source_id, r.operation, r.analysis_label) for r in results] == [
        ("quote-a", "top", "按 Price 降序"),
        ("quote-a", "top", "按 Vendor 升序"),
        # B is analysed once: no label, its card and line as before.
        ("quote-b", "top", ""),
    ]
    assert "analysis_label" not in results[2].model_dump(mode="json")
    assert [row.cells["Item"] for row in results[0].rows] == ["gear", "bolt", "nut"]
    assert [row.cells["Item"] for row in results[1].rows][0] == "bolt"
    block, _evidence = spreadsheet_prompt_block(
        results, preview_rows=20, max_bytes=65_536,
    )
    heads = [line for line in block.splitlines() if line.startswith("k600")]
    assert "plan=" not in heads[2]


def test_only_an_exact_repeat_is_a_duplicate(tmp_path):
    count_a = {"source_id": "quote-a", "sheet": "Sheet", "operation": "aggregate",
               "aggregation": "count", "group_by": "Vendor"}
    _planner, results, trace = _same_sheet_run(tmp_path, [
        _TOP_A,
        dict(_TOP_A),                                    # exact repeat
        {**_TOP_A, "columns": ["Item"]},                 # columns: unread by top
        count_a,
        {**count_a, "measure": "Price"},                 # a count never reads it
        {**_TOP_A, "order": "asc"},                      # a different ranking
    ])
    assert [(r.operation, r.analysis_label) for r in results] == [
        ("top", "按 Price 降序"),
        ("aggregate", "计数；按 Vendor 分组"),
        ("top", "按 Price 升序"),
    ]
    [step] = trace
    assert step.detail["targets"] == {
        "planned": 6, "executed": 3, "invalid": 0, "duplicate": 3,
        "over_limit": 0, "limit": 3,
    }


def test_same_sheet_analyses_past_the_cap_are_named_in_the_disclosure(tmp_path):
    _planner, results, _trace = _same_sheet_run(tmp_path, [
        _TOP_A, _SUM_A, _TOP_B,
        {**_TOP_A, "filters": [{"column": "Vendor", "operator": "eq", "value": "x"}]},
    ])
    assert len(results) == 3
    assert results[-1].warnings[-1] == (
        "本次规划了 4 项表格分析，超过单次最多 3 项的上限，按规划顺序只做了前 3 项；"
        "未做：A 报价表 · Sheet（排序分析：按 Price 降序；筛选：Vendor = x）。"
    )


@pytest.mark.parametrize("reply", [
    {"targets": []},
    {"targets": {"source_id": "quote-b", "sheet": "Sheet"}},
    {"targets": "quote-b"},
    {"targets": [{"source_id": "nope", "sheet": "Sheet"}, "quote-b"]},
])
def test_a_reply_naming_no_usable_target_falls_back_to_the_local_profile(
    tmp_path, reply,
):
    """Exactly what an invalid single-object plan always did: the local
    profile of the first listed sheet, one card, no planning tally unless
    the reply named several targets."""
    (tmp_path / "reply").mkdir()
    (tmp_path / "invalid").mkdir()
    _planner, results, trace = _single_library_run(tmp_path / "reply", reply)
    _baseline, invalid_results, invalid_trace = _single_library_run(
        tmp_path / "invalid", _SINGLE_TARGETS["invalid"],
    )
    assert [r.model_dump(mode="json") for r in results] == [
        r.model_dump(mode="json") for r in invalid_results
    ]
    [step] = trace
    targets = step.detail.pop("targets", None)
    assert step.summary == invalid_trace[0].summary
    assert step.detail == invalid_trace[0].detail
    named = reply["targets"] if isinstance(reply["targets"], list) else []
    assert (targets is None) == (len(named) <= 1)


def test_two_sheets_of_one_workbook_keep_two_receipts(tmp_path):
    from types import SimpleNamespace

    from openpyxl import Workbook

    from app.services.ask_service import AskService

    analysis, _artifacts = _service(tmp_path)
    path = tmp_path / "book.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "2025"
    first.append(["Item", "Price"])
    first.append(["bolt", 3])
    second = workbook.create_sheet("2026")
    second.append(["Item", "Price"])
    second.append(["bolt", 4])
    workbook.save(path)
    assert analysis.compile_source(SimpleNamespace(
        id="book", notebook_id="nb-1", title="年度报价表", type="xlsx",
        file_name="book.xlsx", file_path=str(path), file_hash="h", source_url="",
    ), notebook_name="nb-1", owner_id="owner", row_element_ids={})
    planner = _Planner({"targets": [
        {"source_id": "book", "sheet": "2025", "operation": "profile"},
        {"source_id": "book", "sheet": "2026", "operation": "profile"},
    ]})
    ask = _ask(analysis, planner, participants=(), visible={"nb-1": ("book",)})
    results, _trace = _run_lane(ask, "比较 Excel 报价表 2025 和 2026 两个工作表")

    assert [result.sheet for result in results] == ["2025", "2026"]
    citations: list = []
    AskService._append_spreadsheet_citations(citations, results)
    # Both are source-level receipts of the same workbook (no row element):
    # still two tables, so still two receipts.
    assert [citation.element_id for citation in citations] == ["", ""]
    assert [citation.location_label for citation in citations] == [
        f"{result.sheet}!{result.range}" for result in results
    ]


# ---------------------------------------------------------------------------
# The synthesis table block: one budget for every table
# ---------------------------------------------------------------------------

def _wide_results(tmp_path, count: int):
    analysis, _artifacts = _service(tmp_path)
    for index in range(count):
        _compile(analysis, tmp_path, "nb-1", f"wide-{index}",
                 title=f"报价表 {index}", header=("Item", "Price"),
                 rows=tuple((f"item-{index}-{row:02d}", row) for row in range(30)))
    planner = _Planner({"targets": [
        {"source_id": f"wide-{index}", "sheet": "Sheet", "operation": "filter"}
        for index in range(count)
    ]})
    ask = _ask(analysis, planner, participants=(), visible={
        "nb-1": tuple(f"wide-{index}" for index in range(count)),
    })
    results, _trace = _run_lane(ask, "比较这些 Excel 报价表的明细")
    assert len(results) == count
    return results


def _rows_shown(block: str) -> list[int]:
    shown: list[int] = []
    for line in block.splitlines():
        if line.startswith("k600"):
            shown.append(0)
        elif line != "Item | Price":
            shown[-1] += 1
    return shown


@pytest.mark.parametrize(("max_bytes", "max_chars"), [(1_024, None), (65_536, 400)])
def test_tables_share_one_block_budget_and_none_is_starved(
    tmp_path, max_bytes, max_chars,
):
    results = _wide_results(tmp_path, 3)
    block, evidence = spreadsheet_prompt_block(
        results, preview_rows=20, max_bytes=max_bytes, max_chars=max_chars,
    )
    # One budget: three tables do not get three blocks' worth.
    assert len(block.encode("utf-8")) <= max_bytes
    assert max_chars is None or len(block) <= max_chars
    assert list(evidence) == ["k6001", "k6002", "k6003"]
    # Key and header lines first, then rows round robin: every table shows
    # rows, and no table shows more than one row more than another.
    shown = _rows_shown(block)
    assert min(shown) >= 1, shown
    assert max(shown) - min(shown) <= 1, shown
    assert sum(shown) < 3 * 20  # the cap actually bit
    # Each table shows a prefix of its own rows under its own key.
    for (key, entry), result, count in zip(evidence.items(), results, shown):
        assert entry["source_id"] == result.source_id
        last = result.rows[count - 1].cells
        assert entry["snippet"] == f"{last['Item']} | {last['Price']}"


def test_one_table_under_a_tight_budget_keeps_its_greedy_prefix(tmp_path):
    """With one table the round robin is the old greedy walk."""
    [result] = _wide_results(tmp_path, 1)
    block, _evidence = spreadsheet_prompt_block(
        [result], preview_rows=20, max_bytes=300,
    )
    lines = block.splitlines()
    expected = [lines[0], "Item | Price"]
    for row in result.rows[:20]:
        line = f"{row.cells['Item']} | {row.cells['Price']}"
        if len("\n".join([*expected, line]).encode("utf-8")) > 300:
            break
        expected.append(line)
    assert lines == expected
    assert 2 < len(lines) < 22


# ---------------------------------------------------------------------------
# Citation keys: workbook results and external evidence share the 6000 band
# ---------------------------------------------------------------------------

def _external(index: int):
    from app.domain.reflect_action import ExternalEvidence

    return ExternalEvidence(
        key=f"ext:acme:{index}", plugin_id="acme", action="web_search",
        source_label="IEEE Xplore", title=f"标题-{index}",
        excerpt=f"摘录-{index}", url=f"https://example.org/{index}",
        location_label=f"§{index}",
    )


def _external_ask():
    from types import SimpleNamespace

    from app.core.config import Settings
    from app.services.ask_service import AskService
    from app.services.evidence_context import EvidenceContextService

    ask = object.__new__(AskService)
    ask.evidence_context = EvidenceContextService(
        notebooks=SimpleNamespace(), sources=SimpleNamespace(),
        knowledge=SimpleNamespace(), settings=Settings(_env_file=None),
    )
    return ask


def _shifted(block: str, sheet_map: dict, offset: int) -> tuple[str, dict]:
    """The block and map as an outline section ``offset`` would number them."""
    def key(name: str) -> str:
        return f"k{offset + int(name[1:])}"

    lines = [
        key(line.split(":", 1)[0]) + ":" + line.split(":", 1)[1]
        if line.startswith("k600") else line
        for line in block.splitlines()
    ]
    return "\n".join(lines), {key(name): value for name, value in sheet_map.items()}


@pytest.mark.parametrize("key_offset", [0, 10_000])
def test_external_evidence_numbers_after_the_workbook_keys(tmp_path, key_offset):
    results = _wide_results(tmp_path, 2)
    block, sheet_map = _shifted(*spreadsheet_prompt_block(
        results, preview_rows=2, max_bytes=65_536,
    ), key_offset)
    context, merged, admitted, _counts = _external_ask()._append_external_context(
        block, dict(sheet_map), [_external(1), _external(2)], key_offset=key_offset,
    )
    keys = [f"k{key_offset + 6000 + n}" for n in (1, 2, 3, 4)]
    assert [merged[key]["source_id"] for key in keys[:2]] == [
        result.source_id for result in results
    ]
    assert [merged[key]["object_type"] for key in keys[2:]] == ["external", "external"]
    heads = [line.split(":", 1)[0] for line in context.splitlines()
             if line.startswith("k")]
    assert len(heads) == len(set(heads)) == 4, heads
    assert admitted == {"ext:acme:1", "ext:acme:2"}


def test_external_evidence_without_workbook_keys_still_starts_at_k6001():
    _context, merged, _admitted, _counts = _external_ask()._append_external_context(
        "k1: [chunk] text", {"k1": {"object_type": "chunk"}}, [_external(1)],
        key_offset=0,
    )
    assert sorted(merged) == ["k1", "k6001"]
    assert merged["k6001"]["object_type"] == "external"
