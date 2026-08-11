"""生产热点整改批 E:orphan asset 扫描的后端中性判定原语。

单遍扫描把「逐资产 ``LIKE '%asset://<id>%'``」换成「一次取回 token 集合 +
集合差」。这些用例钉的是那个换算**逐位等价**——尤其是
``token.startswith(asset_id)`` 而不是 ``token == asset_id``:LIKE 是子串匹配,
一个 id 若恰好是另一个被引用 id 的前缀,旧实现会把它判为「仍被引用」而保留,
等值比较会把它判成孤儿并删掉——那是**扩大删除面**,方向错了。
"""
from __future__ import annotations

import json
import random
import re

import pytest

from app.repositories.knowhow_asset_refs import (
    asset_ref_tokens,
    collect_change_payload_tokens,
    content_strings_in_payload,
    keepers_among,
)


def _like_referenced(asset_id: str, texts) -> bool:
    """退役的判据,逐字复刻:``payload/content LIKE '%asset://<id>%'``。"""
    needle = f"asset://{asset_id}"
    return any(needle in str(text or "") for text in texts)


def _tokens(texts) -> set:
    out: set = set()
    for text in texts:
        out.update(asset_ref_tokens(text))
    return out


def test_tokens_pick_up_every_asset_ref_shape():
    text = (
        "![图](asset://asset-aaa) 正文 asset://asset-bbb) 结尾 "
        "`asset://asset-ccc` <asset://asset-ddd>"
    )
    assert asset_ref_tokens(text) == [
        "asset-aaa", "asset-bbb", "asset-ccc", "asset-ddd",
    ]
    assert asset_ref_tokens(None) == []
    assert asset_ref_tokens("") == []


def test_keepers_match_the_retired_like_predicate_exactly():
    """随机语料上逐个资产比对新旧判据。"""
    rng = random.Random(20260810)
    ids = [f"asset-{rng.randrange(16**8):08x}" for _ in range(60)]
    # 刻意混入前缀关系:短 id 是某个长 id 的严格前缀。
    ids += ["asset-dead", "asset-deadbeef", "asset-dead0000"]
    texts = []
    referenced = set()
    for _ in range(40):
        chosen = rng.choice(ids)
        referenced.add(chosen)
        texts.append(
            rng.choice([
                f"![x](asset://{chosen})",
                f"前面 asset://{chosen} 后面",
                f"asset://{chosen}.png",
            ])
        )
    texts.append("完全不提资产的一段正文")

    kept = keepers_among(ids, _tokens(texts))
    for asset_id in ids:
        assert (asset_id in kept) == _like_referenced(asset_id, texts), asset_id


def test_prefix_id_is_kept_exactly_like_the_old_substring_match():
    """``asset-dead`` 只出现在 ``asset://asset-deadbeef`` 里 —— 旧 LIKE 判它
    「仍被引用」而保留,新判据必须一样保留。等值比较会把它删掉(扩大删除面)。"""
    texts = ["![x](asset://asset-deadbeef)"]
    tokens = _tokens(texts)
    assert _like_referenced("asset-dead", texts) is True
    assert keepers_among(["asset-dead", "asset-deadbeef"], tokens) == {
        "asset-dead", "asset-deadbeef",
    }


def test_non_id_shaped_asset_id_is_conservatively_kept():
    """id 里含 token 字符集之外的字符时无法用 token 推理 —— 垃圾回收器的安全
    答案是「保留」。"""
    assert keepers_among(["a.b/c"], set()) == {"a.b/c"}
    assert keepers_among([""], set()) == {""}
    # 正常形状的 id 在零 token 下照常被判为孤儿。
    assert keepers_among(["asset-abc"], set()) == set()


def test_change_payload_tokens_never_read_code_attachments():
    """``row_delete`` 的 payload 把行的 code 附件与真正的 cells 并排放着 ——
    只有 cells 那半算引用。"""
    payload = {
        "rows": [
            {
                "row_id": "r1",
                "cells": {"c1": "![图](asset://asset-content)"},
                "code": [
                    {
                        "row_id": "r1", "column_id": "c1",
                        "code_text": "# asset://asset-codeonly",
                        "language": "python", "updated_by": "u",
                        "cell_content_hash": "h",
                    }
                ],
            }
        ]
    }
    tokens: set = set()
    collect_change_payload_tokens("row_delete", json.dumps(payload), tokens)
    assert tokens == {"asset-content"}
    assert keepers_among(["asset-content", "asset-codeonly"], tokens) == {
        "asset-content"
    }


def test_unregistered_change_kind_still_fails_loudly():
    with pytest.raises(ValueError):
        content_strings_in_payload("brand_new_kind", {})


def test_sqlite_history_store_reexports_the_shared_extractor():
    """搬到中性模块之后,历史 import 位仍然指向**同一个**函数对象——两个
    maintenance adapter 共用它的前提就是「只有一份实现」。"""
    from app.repositories.sqlite import knowhow_history_store

    assert knowhow_history_store.content_strings_in_payload is (
        content_strings_in_payload
    )


def test_both_maintenance_adapters_use_the_neutral_helper_not_each_other():
    """adapter 不得互相 import(架构红线):两侧都从中性模块拿判定原语。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "repositories"
    pg = (root / "postgres" / "maintenance.py").read_text(encoding="utf-8")
    lite = (root / "sqlite" / "maintenance.py").read_text(encoding="utf-8")
    for source in (pg, lite):
        assert "from app.repositories.knowhow_asset_refs import" in source
    assert not re.search(r"^from app\.repositories\.sqlite", pg, re.M)
    assert "app.repositories.postgres" not in lite


# ---------------------------------------------------------------------------
# 评审 P2-4 / P3:token 扫描与 keepers_among 的登记边界。
# ---------------------------------------------------------------------------


def test_adjacent_markers_without_a_separator_are_both_found():
    """``asset://X`` 紧挨着 ``asset://Y`` 时两个都要认出来。

    单一正则 ``asset://([A-Za-z0-9_-]+)`` 会让第一段的极大 run 把第二个 marker
    的 ``asset`` 吞掉、扫描点越过它,``Y`` 从此看不见 —— 那是**欠匹配**,是这条
    判据唯一不许走的方向。改成「先找 marker、再读 run」两步即可。"""
    text = "asset://asset-aaaasset://asset-bbb"
    tokens = _tokens([text])
    assert "asset-bbb" in tokens
    # 第一段的 run 依旧吞掉了后面的字面 ``asset``,但那只是**过**匹配:
    # ``asset-aaa`` 仍靠前缀比较保住,与旧 LIKE 一致。
    assert keepers_among(["asset-aaa", "asset-bbb"], tokens) == {
        "asset-aaa", "asset-bbb",
    }
    for asset_id in ("asset-aaa", "asset-bbb"):
        assert _like_referenced(asset_id, [text]) is True


def test_marker_with_nothing_id_shaped_after_it_yields_no_token():
    assert asset_ref_tokens("看这里 asset:// 就没了") == []
    assert asset_ref_tokens("![x](asset://)") == []


def test_keepers_among_is_case_sensitive_registered_departure():
    """登记的第二条偏离:实现是**大小写敏感**的,两后端就此收敛到 PG 口径。

    SQLite 的 LIKE 默认 ASCII 大小写不敏感,所以退役判据在 SQLite 上还会匹配
    ``ASSET://<ID>``;PG 的不会。生成的 id 是小写 hex、marker 由机器写出,所以
    真实引用不会改判 —— 但这是 SQLite 侧删除面唯一可能变宽的方向,明写在这里
    而不是让它悄悄发生。"""
    # ① id 的大小写:token 认出来了,但只匹配同样大小写的候选。
    tokens = _tokens(["![x](asset://ASSET-ABC)"])
    assert tokens == {"ASSET-ABC"}
    assert keepers_among(["ASSET-ABC"], tokens) == {"ASSET-ABC"}
    assert keepers_among(["asset-abc"], tokens) == set()
    # 而退役的 SQLite LIKE 会把这个 id 判成「仍被引用」——两者就此分歧。
    assert _like_referenced("asset-abc", ["![x](asset://ASSET-ABC)"]) is False  # 本函数模拟的是 PG 口径
    # ② marker 本身的大小写:``ASSET://`` 根本不产生 token。
    assert _tokens(["![x](ASSET://asset-abc)"]) == set()


def test_keepers_among_prefix_fallback_is_exact_under_bisect():
    """前缀回退改用 bisect 之后仍与线性 ``any(startswith)`` 逐位同解。"""
    rng = random.Random(20260811)
    alphabet = "ab-_09"
    tokens = {
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
        for _ in range(200)
    }
    candidates = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
        for _ in range(300)
    ] + sorted(tokens)[:20]

    kept = keepers_among(candidates, tokens)
    expected = {
        candidate for candidate in candidates
        if any(token.startswith(candidate) for token in tokens)
    }
    assert kept == expected


def test_registered_kinds_is_module_level_and_shared():
    from app.repositories import knowhow_asset_refs

    assert isinstance(knowhow_asset_refs.REGISTERED_KINDS, frozenset)
    assert "cell_update" in knowhow_asset_refs.REGISTERED_KINDS
