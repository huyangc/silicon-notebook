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

_REPLY_SHAPES = {
    "single_object": lambda target: target,
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
