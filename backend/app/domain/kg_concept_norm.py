"""Concept-name normalizer shared by same-document canonicalize and KG attrs.

Moved verbatim from ``app.services.kg.canonicalize._norm`` so the domain layer
(``app.domain.kg_attrs``) can compare alias candidates with the exact key
canonicalize merges on, without importing a service module. canonicalize
imports it back under its old private name; behaviour is byte-identical.

Deliberately NOT ``kg_merge_seed._norm``: that one folds through ``_ALIASES``
("vco" -> "voltage controlled oscillator"), so comparing an alias against the
name with it would discard real aliases.
"""
from __future__ import annotations

import re
import unicodedata


def norm_concept_name(name: str) -> str:
    # NFKC + Unicode \w:CJK/希腊/带音标名参与同文档窗口间合并(旧类清成空名,
    # 中文概念每窗口留一份重复节点)。删除语义保持:非字母数字直接删除(不是替换
    # 空格),下划线显式删除 —— 纯 ASCII 输出与旧 [^a-z0-9 ] 类逐字节相同。
    folded = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]|_", "", folded)).strip()
