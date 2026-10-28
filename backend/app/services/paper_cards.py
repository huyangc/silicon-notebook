"""论文卡片:prompt / schema hint / 输入块渲染与零 LLM 接地校验(纯函数)。

设计见 ``docs/superpowers/specs/2026-10-28-paper-cards-find-sources-design_zh.md``
§4.1–§4.3。卡片是由原文确定性派生的缓存:模型只能引用本次输入采样里出现过的
元素,指标数值归一化后必须以完整数字 token 出现在它所引用的元素**送给模型的那段
文本**里(表格截断后的文本,不是全文元素),否则丢弃并记审计。无 DB / 网络依赖。

输入块里每个元素用短句柄 ``e1``、``e2`` … 标注(而不是 35 字符的元素 id):句柄是
本次采样的局部名字,校验时一一映射回真实元素 id,卡片里存的永远是元素 id。模型
回填的若是元素 id 本身(而非句柄),同样接受——合法性只看它是否在本次输入里。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.core.model_values import as_text

# prompt / schema / 校验规则 / 采样口径任何一项变化都要加一:旧卡片在读取时
# 因版本不等自动失效(规格 §4.4「有效性在读取时判定」),不写清理代码。
# 2:指标只收本文方法的成绩,基线数值进 compared_to,setting 写测量条件(T7 验收)。
# 3:指标值先按数字安全的上限截短再接地(截短会切到数字就丢弃,value_too_long),
#    name/setting/compared_to 也不再切在数字中间。v2 卡片可能存着与接地数不同的
#    截短值,升版让它们在读取时失效、按需重生成。
PAPER_CARD_VERSION = 3

# 示例里不写 null、空列表只给字符串列表字段(本 hint 一个都没有,所以每个列表都
# 给一个示例项):否则严格 schema 投影整体回落 JSON 模式(json-hint-strict-alignment)。
# 根字段在严格模式下不可为 null,所以「原文没有」由 prose 约定为 text 写 null / 列表写 []。
PAPER_CARD_SCHEMA_HINT = (
    '{"tldr":{"text":"","anchors":["e1"]},'
    '"problem":{"text":"","anchors":["e1"]},'
    '"method":{"text":"","anchors":["e1"]},'
    '"contributions":[{"text":"","anchors":["e1"]}],'
    '"metrics":[{"name":"","value":"","unit":"","setting":"","compared_to":"","anchor":"e1"}],'
    '"datasets":[{"name":"","anchors":["e1"]}]}'
)

# §4.1 长度与条数上限。文字超长由校验截断(不丢字段);条数超出的尾部丢弃并记审计。
TLDR_MAX_CHARS = 300
PROBLEM_MAX_CHARS = 400
METHOD_MAX_CHARS = 400
CONTRIBUTION_MAX_CHARS = 200
MAX_CONTRIBUTIONS = 5
MAX_METRICS = 12
MAX_DATASETS = 8
MAX_CARD_ANCHORS = 24
# 规格只给出 name/setting/compared_to「只截断长度」,没给数;取单元格能放下的量级。
METRIC_NAME_MAX_CHARS = 80
METRIC_VALUE_MAX_CHARS = 40
METRIC_UNIT_MAX_CHARS = 16
METRIC_SETTING_MAX_CHARS = 120
METRIC_COMPARED_MAX_CHARS = 120
DATASET_NAME_MAX_CHARS = 120

# §4.1 anchor 裁剪的保留顺序。
ANCHOR_FIELD_ORDER = ("tldr", "metrics", "contributions", "method", "problem", "datasets")

_TEXT_FIELD_CAPS = {
    "tldr": TLDR_MAX_CHARS,
    "problem": PROBLEM_MAX_CHARS,
    "method": METHOD_MAX_CHARS,
}


@dataclass(frozen=True)
class CardInputElement:
    """一个送进模型的采样元素。``text`` 是**送给模型的那段文本**(已截断),
    指标接地只在它上面判定。"""

    handle: str
    element_id: str
    element_type: str
    location_label: str
    text: str


def card_element_header(handle: str, element_type: str, location_label: str) -> str:
    label = " ".join(str(location_label or "").split())[:80]
    kind = str(element_type or "text")
    return f"[{handle}] {kind}" + (f" · {label}" if label else "") + "\n"


def card_element_cost(handle: str, element_type: str, location_label: str, text: str) -> int:
    """一个元素在输入块里占的字符数(含头行与段间空行),采样按它记预算。"""
    return len(card_element_header(handle, element_type, location_label)) + len(text) + 2


def render_card_input(elements: Iterable[CardInputElement]) -> str:
    return "\n\n".join(
        card_element_header(e.handle, e.element_type, e.location_label) + e.text
        for e in elements
    )


def paper_card_prompt(input_block: str) -> str:
    return (
        "Summarise an academic paper into a structured card, using ONLY the "
        "excerpts below.\n"
        "Each excerpt starts with a handle in brackets, e.g. [e3]. Every "
        "\"anchors\" / \"anchor\" value must be such a handle (e.g. \"e3\") of an "
        "excerpt that actually states the content.\n"
        "Rules:\n"
        "- Use ONLY the excerpts. Even if you recognise the paper, do NOT fill "
        "in anything from memory. When the excerpts do not show something, "
        "write null for that text and [] for that list.\n"
        f"- tldr: one or two sentences (<= {TLDR_MAX_CHARS} characters). "
        f"problem / method: <= {PROBLEM_MAX_CHARS} characters each.\n"
        f"- contributions: at most {MAX_CONTRIBUTIONS} key innovations, each "
        f"<= {CONTRIBUTION_MAX_CHARS} characters.\n"
        f"- metrics: at most {MAX_METRICS} main quantitative results of THIS "
        "paper's own proposed method ONLY. \"value\" is the number EXACTLY as "
        "printed in the cited excerpt (e.g. \"85.2\"); put the unit (%, x, ms, "
        "...) in \"unit\". \"setting\" is the dataset / benchmark / split / "
        "evaluation condition that number was measured on (e.g. \"ImageNet "
        "256, 250 steps\") -- never the header of another column. A number "
        "of a baseline, a prior method or an ablation row is NOT a metric: put "
        "it in \"compared_to\" of the metric it is compared with, naming the "
        "row (e.g. \"BigGAN-deep 6.95\"); leave compared_to empty when the "
        "excerpt states no comparison. In a results table, take values from "
        "the row of the paper's own method. \"anchor\" is the ONE excerpt "
        "(often a table) where that number appears.\n"
        f"- datasets: at most {MAX_DATASETS} datasets or benchmarks used.\n"
        "- Write the text in the language of the paper.\n"
        "- Return JSON only.\n\n"
        f"Excerpts:\n{input_block}"
    )


# ---------------------------------------------------------------- numbers
#
# 指标值接地(规格 §4.2):值与元素文本各自切成「数字 token 序列」,值的序列必须作为
# **连续子序列**出现在元素文本的序列里。
#
# - 全角转半角(NFKC),U+2212 减号与 U+2013/U+2014 连接号都视同 ``-``,去千分位逗号;
# - token 是完整数字串:前后都不是数字,点号连接的数字串整体算一个 token(185.23、
#   1.2.3),所以 85.2 不会命中 185.23 或 85.23;字母、%、×、x、^ 等都是边界;
# - 单个小数点的 token 去小数尾零与整数前导零(85.20 == 85.2);
# - 符号保留:``-`` 紧跟在 ASCII 字母数字之后只是分隔(top-1、ResNet-50、10-20、
#   1.5–2.0),其它情况(开头、空白、括号、Δ 这类非 ASCII 字母之后)都是负号,所以 -0.5
#   不命中 0.5、Δ-0.5 保留符号;科学计数法 1.2e-3 的指数符号也保留;
# - 多 token 的值(科学计数法 1.2e-3、85.2±0.3、3×10^5)要求这些 token 在元素文本里
#   连续出现、彼此之间只隔着连接符(e、×、^、±、/ 等),不能东一个西一个凑齐;
# - 只有一个 token、且是一位整数的值(``3``、``3x``、``-1``)太含糊,一律拒绝
#   (``ambiguous_value``);连续匹配的多 token 值(3e-4、1e-5)不算含糊。
#
# 已知限制:token 不看上下文,值 ``2.1`` 会命中元素里的「Section 2.1」这类章节号 /
# 版本号。引用的元素是模型自己选的、又必须含这个数,残余误判可接受。

# 千分位:逗号两侧都是数字,且右侧恰好三位后不再接数字。
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# 省略整数位的小数(``.95``、``±.9``)也是数:规范成 ``0.95``,与写全的 ``0.95`` 同形。
_NUMBER_RE = re.compile(r"(?<![\d.])(-?)(\d+(?:\.\d+)*|\.\d+)(?!\d)")


def _canonical_number(token: str) -> str:
    """单个小数点的 token 去小数尾零与整数前导零(85.20 == 85.2,07 == 7);
    多个点号的(版本号形状)原样保留。"""
    if token.count(".") > 1:
        return token
    whole, _, frac = token.partition(".")
    whole = whole.lstrip("0") or "0"
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _normalize_numeric_text(text: str) -> str:
    """全角转半角(NFKC)、统一减号并去千分位逗号。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    for dash in ("\u2212", "\u2013", "\u2014"):
        normalized = normalized.replace(dash, "-")
    return _THOUSANDS_RE.sub("", normalized)


def _number_spans(text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """规范化后的文本,以及其中每个数字 token 的 ``(规范形, 起点, 终点)``。"""
    normalized = _normalize_numeric_text(text)
    spans = []
    for match in _NUMBER_RE.finditer(normalized):
        sign, digits = match.group(1), match.group(2)
        start = match.start()
        # ``-`` 前面是 ASCII 字母数字(top-1、10-20)就只是分隔符,不是负号;科学计数法
        # 的指数(1.2e-3:``e`` 前是数字)例外,符号保留。
        before = normalized[start - 1] if sign and start > 0 else ""
        exponent = before in ("e", "E") and start > 1 and normalized[start - 2].isdigit()
        if before.isascii() and before.isalnum() and not exponent:
            sign = ""
            start += 1
        spans.append((sign + _canonical_number(digits), start, match.end()))
    return normalized, spans


def number_tokens(text: str) -> list[str]:
    """文本里全部完整数字 token 的规范形(按出现顺序,负数带 ``-``)。"""
    return [token for token, _start, _end in _number_spans(text)[1]]


# 多 token 值里相邻两个数之间允许的连接符(去空白后):科学计数法、误差、乘幂、
# 比值与区间。夹着文字(and、steps、%)的两个数不算同一个值。
_JOINER_CHARS = frozenset("eE×xX*^±+-/()·.:")
_MAX_JOINER = 4


def _joined(text: str, end: int, start: int) -> bool:
    gap = "".join(text[end:start].split())
    return len(gap) <= _MAX_JOINER and all(ch in _JOINER_CHARS for ch in gap)


def _is_ambiguous(tokens: list[str]) -> bool:
    return len(tokens) == 1 and len(tokens[0].lstrip("-")) == 1


def _run_in(text: str, spans: list[tuple[str, int, int]], wanted: list[str]) -> bool:
    """``wanted`` 作为一段连续、只隔连接符的数字 token 出现在 ``spans`` 里。"""
    width = len(wanted)
    for i in range(len(spans) - width + 1):
        window = spans[i:i + width]
        if [token for token, _s, _e in window] == wanted and all(
            _joined(text, window[k][2], window[k + 1][1]) for k in range(width - 1)
        ):
            return True
    return False


def ground_value(value: str, element_text: str) -> str:
    """指标值接地,返回 ``""`` 表示通过,否则是丢弃原因码:``no_number``、
    ``ambiguous_value`` 或 ``value_not_in_anchor``。"""
    wanted = number_tokens(value)
    if not wanted:
        return "no_number"
    if _is_ambiguous(wanted):
        return "ambiguous_value"
    text, spans = _number_spans(element_text)
    return "" if _run_in(text, spans, wanted) else "value_not_in_anchor"


# 只由一个一位整数(加上最多这么多字的单位,如 ``3x``、``5 %``)构成的值太含糊。
_BARE_NUMBER_MAX_CHARS = 4
# 文字值里把相邻两个数连成「一个数」的只有紧贴的数学连接符(科学计数法、误差、乘幂);
# 指数的符号已随 token 保留(1.2e-3 的 gap 只是 ``e``)。``:``、``/``、括号、``-``、
# 纯空白与任何文字都把两侧分成各自接地的两段(「COCO 2017: 42.1 AP」是两段)。
_TIGHT_JOINER_CHARS = frozenset("±eE×xX*^+")
_MAX_TIGHT_JOINER = 3


def _tightly_joined(text: str, end: int, start: int) -> bool:
    gap = "".join(text[end:start].split())
    return 0 < len(gap) <= _MAX_TIGHT_JOINER and all(ch in _TIGHT_JOINER_CHARS for ch in gap)


def _tight_run_in(text: str, spans: list[tuple[str, int, int]], wanted: list[str]) -> bool:
    width = len(wanted)
    for i in range(len(spans) - width + 1):
        window = spans[i:i + width]
        if [token for token, _s, _e in window] == wanted and all(
            _tightly_joined(text, window[k][2], window[k + 1][1]) for k in range(width - 1)
        ):
            return True
    return False


def ground_prose_numbers(value: str, element_text: str) -> str:
    """一句可能夹着数字的文字值(逐篇抽取的临时字段)的数值接地,同 ``ground_value``
    的归一化与 token 规则,返回 ``""`` 表示通过,否则是原因码。

    抽取值常是带设定的短句(「COCO 2017: 42.1 AP」「ResNet-50: 76.1% top-1」),所以
    值里的数字按**紧贴的数学连接符**(``±``、``e``、``×``、``^``)切成若干组
    (``85.2±0.3``、``1.2e-3``、``3×10^5`` 各是一组),``:``、``/``、括号、``-``、空格
    与文字都把两侧分开;**每一组**都要以同样紧贴的形式连续出现在引用文本里。没有
    数字的值直接通过。整个值只是一个一位整数(可带短单位)时按 ``ambiguous_value``
    拒绝;夹在文字里的一位整数只要求在引用文本里出现。"""
    normalized, spans = _number_spans(value)
    if not spans:
        return ""
    if len(spans) == 1 and _is_ambiguous([spans[0][0]]) and len(
        "".join(normalized.split())
    ) <= _BARE_NUMBER_MAX_CHARS:
        return "ambiguous_value"
    groups: list[list[str]] = [[spans[0][0]]]
    for previous, current in zip(spans, spans[1:]):
        if _tightly_joined(normalized, previous[2], current[1]):
            groups[-1].append(current[0])
        else:
            groups.append([current[0]])
    text, text_spans = _number_spans(element_text)
    for group in groups:
        if not _tight_run_in(text, text_spans, group):
            return "value_not_in_anchor"
    return ""


def value_grounded(value: str, element_text: str) -> bool:
    return ground_value(value, element_text) == ""


# ---------------------------------------------------------------- verify

def _scalar_text(value: Any) -> str:
    """字符串取 strip;数字(JSON 模式下模型可能把数值写成 number)转字符串。"""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return as_text(value)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip()


_SPAN_NUMBER = r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
# 一个完整的数值表达式:普通数、科学计数法(1.2e-3)、乘幂(3×10^5、10^-5)、
# 正负误差(85.2±0.3)。截断时整段视为一个数,切点落在里面就退到整段之前。
_NUMBER_SPAN_RE = re.compile(
    rf"[-+−]?{_SPAN_NUMBER}"
    rf"(?:\s*(?:[eE][-+−]?\d+"
    rf"|[×xX*·]\s*10\s*(?:\^|\*\*)\s*[-+−]?\d+"
    rf"|\^\s*[-+−]?\d+"
    rf"|±\s*{_SPAN_NUMBER}))*"
)


def cap_number_safe(value: str, limit: int) -> str:
    """截到 ``limit`` 字;切点落在一个数值表达式里面时退到整个表达式之前,绝不留下半个数
    (``36.2`` 截成 ``36.``、``1.2e-3`` 截成 ``1.2``、``3×10^5`` 截成 ``3×10`` 都会变成
    另一个数)。卡片输入、卡片指标、逐篇抽取与表格渲染共用。"""
    if len(value) <= limit:
        return value
    cut = limit
    for match in _NUMBER_SPAN_RE.finditer(value, 0, limit + 64):
        if match.start() >= cut:
            break
        if match.start() < cut < match.end():
            cut = match.start()
            break
    return value[:cut].rstrip()


class _AnchorResolver:
    """句柄或元素 id → 元素 id;不在本次输入里的返回 ``""``。"""

    def __init__(self, elements: Iterable[CardInputElement]) -> None:
        self.by_handle: dict[str, CardInputElement] = {}
        self.by_id: dict[str, CardInputElement] = {}
        for element in elements:
            self.by_handle[element.handle.lower()] = element
            self.by_id[element.element_id] = element

    def resolve(self, raw: Any) -> CardInputElement | None:
        token = as_text(raw)
        if not token:
            return None
        if token in self.by_id:
            return self.by_id[token]
        return self.by_handle.get(token.strip("[]【】 ").lower())


def _anchor_list(raw: Any, resolver: _AnchorResolver, invalid: list[str]) -> list[str]:
    values = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    kept: list[str] = []
    for value in values:
        element = resolver.resolve(value)
        if element is None:
            text = _scalar_text(value)
            if text:
                invalid.append(text[:80])
            continue
        if element.element_id not in kept:
            kept.append(element.element_id)
    return kept


def _note(dropped: dict, field: str, reason: str, value: Any = "") -> None:
    entry = {"reason": reason}
    text = _scalar_text(value) if not isinstance(value, dict) else _scalar_text(
        value.get("text") or value.get("name") or value.get("value"))
    if text:
        entry["value"] = text[:120]
    dropped.setdefault(field, []).append(entry)


def _text_item(raw: Any, cap: int, resolver: _AnchorResolver, field: str, dropped: dict) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        # 没有 anchors 的裸字符串:形状降级,但没有合法引用,按规则丢弃。
        if raw.strip():
            _note(dropped, field, "no_valid_anchor", raw)
        return None
    if not isinstance(raw, Mapping):
        return None
    text = _scalar_text(raw.get("text"))
    invalid: list[str] = []
    anchors = _anchor_list(raw.get("anchors"), resolver, invalid)
    if invalid:
        dropped.setdefault("invalid_anchors", []).extend(invalid)
    if not text:
        return None
    if not anchors:
        _note(dropped, field, "no_valid_anchor", text)
        return None
    return {"text": _cap(text, cap), "anchors": anchors}


def _metric_item(raw: Any, resolver: _AnchorResolver, dropped: dict) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    value = _scalar_text(raw.get("value"))
    name = _scalar_text(raw.get("name"))
    if not value:
        if name:
            _note(dropped, "metrics", "no_value", name)
        return None
    anchor_raw = raw.get("anchor")
    if anchor_raw is None and isinstance(raw.get("anchors"), list) and raw.get("anchors"):
        anchor_raw = raw["anchors"][0]  # 形状降级:anchors 列表取第一个
    element = resolver.resolve(anchor_raw)
    if element is None:
        if _scalar_text(anchor_raw):
            dropped.setdefault("invalid_anchors", []).append(_scalar_text(anchor_raw)[:80])
        _note(dropped, "metrics", "no_valid_anchor", value)
        return None
    # Bound first, then ground what is stored: grounding the raw value and
    # cutting it afterwards could store a different number than the verified
    # one.  A value that cannot be bounded without losing a number is dropped.
    bounded = cap_number_safe(value, METRIC_VALUE_MAX_CHARS)
    if number_tokens(bounded) != number_tokens(value):
        _note(dropped, "metrics", "value_too_long", value)
        return None
    verdict = ground_value(bounded, element.text)
    if verdict:
        _note(dropped, "metrics", verdict, bounded)
        return None
    return {
        "name": cap_number_safe(name, METRIC_NAME_MAX_CHARS),
        "value": bounded,
        "unit": _cap(_scalar_text(raw.get("unit")), METRIC_UNIT_MAX_CHARS),
        "setting": cap_number_safe(_scalar_text(raw.get("setting")), METRIC_SETTING_MAX_CHARS),
        "compared_to": cap_number_safe(
            _scalar_text(raw.get("compared_to")), METRIC_COMPARED_MAX_CHARS),
        "anchor": element.element_id,
    }


def _dataset_item(raw: Any, resolver: _AnchorResolver, dropped: dict) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    name = _scalar_text(raw.get("name"))
    invalid: list[str] = []
    anchors = _anchor_list(raw.get("anchors"), resolver, invalid)
    if invalid:
        dropped.setdefault("invalid_anchors", []).extend(invalid)
    if not name:
        return None
    if not anchors:
        _note(dropped, "datasets", "no_valid_anchor", name)
        return None
    return {"name": _cap(name, DATASET_NAME_MAX_CHARS), "anchors": anchors}


def _bounded_list(raw: Any, limit: int, build, field: str, dropped: dict) -> list[dict]:
    items = [item for item in (build(value) for value in (raw if isinstance(raw, list) else [])) if item]
    for extra in items[limit:]:
        _note(dropped, field, "over_limit", extra)
    return items[:limit]


def _cap_anchors(card: dict, dropped: dict) -> None:
    """去重后全卡最多 ``MAX_CARD_ANCHORS`` 个 anchor,按 ``ANCHOR_FIELD_ORDER`` 保留;
    已保留的 anchor 被后面的条目复用不另计。裁掉 anchor 后变空的条目整条丢弃。"""
    kept: set[str] = set()

    def admit(anchor: str) -> bool:
        if anchor in kept:
            return True
        if len(kept) >= MAX_CARD_ANCHORS:
            return False
        kept.add(anchor)
        return True

    for field in ANCHOR_FIELD_ORDER:
        value = card[field]
        if field == "metrics":
            metrics = []
            for metric in value:
                if admit(metric["anchor"]):
                    metrics.append(metric)
                else:
                    _note(dropped, field, "anchor_cap", metric)
            card[field] = metrics
            continue
        items = [value] if isinstance(value, dict) else (value if isinstance(value, list) else [])
        survivors = []
        for item in items:
            item["anchors"] = [a for a in item["anchors"] if admit(a)]
            if item["anchors"]:
                survivors.append(item)
            else:
                _note(dropped, field, "anchor_cap", item)
        if isinstance(value, dict) or value is None:
            card[field] = survivors[0] if survivors else None
        else:
            card[field] = survivors


def card_is_empty(card: Mapping[str, Any]) -> bool:
    return not any(card.get(field) for field in ANCHOR_FIELD_ORDER)


def verify_paper_card(
    parsed: Mapping[str, Any], input_elements: Iterable[CardInputElement]
) -> tuple[dict, dict]:
    """接地校验,返回 ``(card, dropped)``。

    - 每个 ``anchors`` / ``anchor`` 必须是本次输入里的句柄或元素 id,非法的删掉;
      清掉后一个都不剩的文字字段 / 条目整条丢弃。
    - 指标还要求 ``value`` 的数字 token 以完整 token 出现在该元素送给模型的文本里。
    - 长度超限截断,条数超限丢尾部,全卡 anchor 去重后最多 24 个(按字段顺序保留)。
    - 返回的 ``card`` 字段齐全:``tldr/problem/method`` 为 ``{"text","anchors"}`` 或
      ``None``,其余为列表;整张卡全空时由调用方按生成失败处理(``card_is_empty``)。
    - ``dropped`` 按字段记被丢条目与原因码,另有 ``invalid_anchors``;与 ``paper_meta``
      同一审计信封口径,由调用方写进 ``raw_json["dropped"]``。
    """
    resolver = _AnchorResolver(input_elements)
    dropped: dict = {}
    data = parsed if isinstance(parsed, Mapping) else {}
    card: dict[str, Any] = {
        field: _text_item(data.get(field), cap, resolver, field, dropped)
        for field, cap in _TEXT_FIELD_CAPS.items()
    }
    card["contributions"] = _bounded_list(
        data.get("contributions"), MAX_CONTRIBUTIONS,
        lambda raw: _text_item(raw, CONTRIBUTION_MAX_CHARS, resolver, "contributions", dropped),
        "contributions", dropped,
    )
    card["metrics"] = _bounded_list(
        data.get("metrics"), MAX_METRICS,
        lambda raw: _metric_item(raw, resolver, dropped), "metrics", dropped,
    )
    card["datasets"] = _bounded_list(
        data.get("datasets"), MAX_DATASETS,
        lambda raw: _dataset_item(raw, resolver, dropped), "datasets", dropped,
    )
    _cap_anchors(card, dropped)
    ordered = {field: card[field] for field in ("tldr", "problem", "method", "contributions", "metrics", "datasets")}
    return ordered, dropped


def dropped_count(dropped: Mapping[str, Any]) -> int:
    return sum(len(v) if isinstance(v, list) else 1 for v in dropped.values())
