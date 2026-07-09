"""Mention-bridge 匹配核:别名表构建 + 命中校验。纯函数、零 IO。

trigram FTS 召回候选后,必须过统一 alnum-lookaround 边界后校验(trigram 是子串
语义,rope 会命中 europe)。

调用方约定:claim 文本必须先 NFKC 折叠 + lower 再传入 boundary_hit——别名侧在
build_alias_table 内已做同样的 NFKC+lower,两侧折叠一致才能对上(全角ＧＱＡ/
（）等折到半角;Task 3 的摄取路径负责对文本做同样折叠)。"""
from __future__ import annotations
import re
import unicodedata
from typing import Dict, List, Set, Tuple

_PAREN_ACRONYM_RE = re.compile(r"^(.*\S)\s*\(([^)]+)\)\s*$")
# 缩写 token 必须含 ≥1 字母:纯数字 token(如年份 "2018")不是缩写。否则
# "BERT (2018)" 会把 "2018" 当缩写别名、把无关的年份提及桥进共提图。
_ACR_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9]{3,8}$")
_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def is_latin(alias: str) -> bool:
    return bool(_ASCII_RE.match(alias))


def _long_enough(alias: str, latin_min: int, cjk_min: int) -> bool:
    return len(alias) >= (latin_min if is_latin(alias) else cjk_min)


def build_alias_table(clusters: List[Tuple[str, str]], *, latin_min: int = 4,
                      cjk_min: int = 3) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for cid, name in clusters:
        # NFKC 先折(全角字母/括号→半角),再 strip/lower;与 boundary_hit 的
        # 调用方约定(文本同样 NFKC+lower)配对,否则全角缩写永不命中。
        nm = unicodedata.normalize("NFKC", name or "").strip()
        gated, exempt = set(), set()
        if nm:
            m = _PAREN_ACRONYM_RE.match(nm)
            if m:
                # 括号模式命中时,整串原名不入别名表:整串出现处其组件必然
                # 同时以合法边界命中(头名是整串前缀、后随空格/括号,非 alnum),
                # 整串别名纯冗余、徒增 FTS 查询;整串仅在无括号模式时作全名入表。
                head, paren = m.group(1).strip(), m.group(2).strip()
                head_acr = bool(_ACR_RE.match(head))
                paren_acr = bool(_ACR_RE.match(paren))
                if head_acr and not paren_acr:
                    # 逆序惯例 "ACR (Full Name)":头=缩写绕过长度门,
                    # 括号内=全名走长度门(与正序对称)。
                    exempt.add(head.lower())
                    gated.add(paren.lower())
                else:
                    gated.add(head.lower())
                    if paren_acr:
                        # 正序惯例 "Full Name (ACR)"。括号缩写绕过 latin_min:
                        # 显式 "(ACR)" 模式 precision 高,GQA/MQA/SFT 等 3 位
                        # 缩写是共提桥最有价值的别名;长度下限由 _ACR_RE 的
                        # {3,8} 承担(trigram 最短查询=3)。
                        exempt.add(paren.lower())
            else:
                gated.add(nm.lower())
        kept = {a for a in gated if _long_enough(a, latin_min, cjk_min)} | exempt
        # 纯数字 token 绝非概念别名(年份/计数)。逆序惯例 "ACR (Full)" 下,
        # "BERT (2018)" 会把 "2018" 当"全名"塞进 gated 并过长度门 —— 在此统一
        # 滤除,不论它经 gated/exempt 哪条路径进来(缩写侧已由 _ACR_RE 要求字母)。
        kept = {a for a in kept if not a.isdigit()}
        if kept:
            out[cid] = kept
    return out


def boundary_hit(alias: str, text_lower: str) -> bool:
    # 统一 alnum-lookaround 边界:匹配两端不得紧邻 [a-zA-Z0-9]。
    # - Latin 别名:等效词边界(rope 不命中 europe);
    # - 尾括号别名:")后跟空格/标点/行尾"能命中(\b 在 ')' 后永假,旧写法判死此类别名);
    # - 混排别名(bert模型):Latin 侧不被粘连(superbert模型 不命中);
    # - 纯 CJK:邻字符是 CJK/标点/行首尾等非 alnum → 子串语义保持;但紧邻字母/数字时
    #   同样被 lookaround 拦截而不命中(如"图3铸币平价"中的"铸币平价"贴着数字 3),
    #   与混排别名(superbert模型)拦截 bert 同一机制、对称的召回代价,非纯 CJK 例外。
    pat = r"(?<![a-zA-Z0-9])" + re.escape(alias) + r"(?![a-zA-Z0-9])"
    return re.search(pat, text_lower) is not None
