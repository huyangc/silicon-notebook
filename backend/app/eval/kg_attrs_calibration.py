"""KG node attrs Gate 0 calibration (spec 2026-10-28 §13.1; plan §2).

Manual measurement tool, never run in CI and never imported by ``app``
(``tests/test_kg_attrs_isolation_guard.py``). It imports the product's own
derivation and verification (``app.domain.kg_attrs``) so the measured rules
are the shipped rules. The pure functions below are unit-tested in
``tests/kg/test_kg_attrs_calibration.py``.

Subcommands (run from ``backend/`` with ``PYTHONPATH=.``)::

    python -m app.eval.kg_attrs_calibration extract-en [--dry-run]
    python -m app.eval.kg_attrs_calibration prepare --database-url URL --dry-run
    python -m app.eval.kg_attrs_calibration prepare --database-url URL --confirm-budget \\
        [--allow-local-content-egress]
    python -m app.eval.kg_attrs_calibration render
    python -m app.eval.kg_attrs_calibration ingest --labels ~/Downloads/kg_attrs_labels.json
    python -m app.eval.kg_attrs_calibration report

Local outputs (items, review page, labels, report) go to the MAIN checkout's
``.local/kg_attrs_gate0/`` (git-ignored; refused otherwise). Only
``en_slice.json`` (pool A, built from text already in the repository) lives in
the repository.

Judging rules (plan §2.5): 95% Wilson lower bound; "unsure" counts as wrong;
fewer than ``N_TARGET`` judged items is ``insufficient`` unless the block has
already failed early.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Hashable, Iterable, Mapping, Sequence

from app.domain import kg_attrs
from app.domain.kg_attrs_contracts import FORMULA_NEXT_ELEMENT_ORDINAL_WINDOW

# --- judging lines (plan §2.5) ----------------------------------------------

Z_95 = 1.959963984540054
N_TARGET = 100
ALIAS_LINES = {"R1": 0.90, "R2": 0.90, "R3": 0.90, "R4": 0.85}
FORMULA_PRECISION_LINE = 0.85
FORMULA_RECALL_LINE = 0.70
FORMULA_MIN_DENOMINATOR = 100
FORMULA_MIN_KEPT_PAIRS = 100
VERDICTS = ("correct", "incorrect", "unsure")

# --- sampling (plan G0-T2 / G0-T4) -----------------------------------------

STRATUM_CAPS: tuple[int | None, ...] = (10, 20, 40, None)
BLIND_FRACTION = 0.10
EMPTY_POOL_SPOT_CHECKS = 30
LANGUAGES = ("en", "zh")

# --- outputs ----------------------------------------------------------------

LOCAL_SUBDIR = Path(".local") / "kg_attrs_gate0"
LABELS_FILE_NAME = "kg_attrs_labels.json"
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src 'none'; form-action 'none'"
)


# ===========================================================================
# Statistics
# ===========================================================================

def wilson_lower(correct: int, n: int, z: float = Z_95) -> float:
    """Lower bound of the Wilson score interval; 0.0 for n == 0."""
    if n <= 0:
        return 0.0
    p = correct / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def k_min(n: int, line: float) -> int:
    """Fewest correct out of ``n`` whose Wilson lower bound reaches ``line``
    (``n + 1`` when even ``n``/``n`` does not)."""
    for k in range(n + 1):
        if wilson_lower(k, n) >= line:
            return k
    return n + 1


def failed_early(wrong: int, line: float, n_target: int = N_TARGET) -> bool:
    """True once ``wrong`` (unsure included) exceeds what ``n_target`` items
    can absorb: no later run of correct answers can reach the line."""
    return wrong > n_target - k_min(n_target, line)


def precision_verdict(correct: int, wrong: int, line: float, n_target: int = N_TARGET) -> str:
    """``pass`` / ``fail`` / ``insufficient`` for one precision row."""
    n = correct + wrong
    if n >= n_target:
        return "pass" if wilson_lower(correct, n) >= line else "fail"
    return "fail" if failed_early(wrong, line, n_target) else "insufficient"


def count_verdicts(verdicts: Iterable[str | None]) -> tuple[int, int]:
    """(correct, wrong) over judged verdicts; ``unsure`` is wrong, None is unjudged."""
    correct = wrong = 0
    for verdict in verdicts:
        if verdict == "correct":
            correct += 1
        elif verdict in ("incorrect", "unsure"):
            wrong += 1
    return correct, wrong


# ===========================================================================
# Keys
# ===========================================================================

def _sha(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text or "").encode("utf-8")).hexdigest()


def align_key(text: str, name: str, symbol: str | None = None) -> str:
    """Gold alignment key (spec §13.1): ``sha256(NFC(element text))`` plus the
    name; a formula-variable entry also carries ``N(symbol)``. No object ids,
    so it survives a re-parse."""
    parts = [_sha(text), name]
    if symbol is not None:
        parts.append(kg_attrs.normalize_latex(symbol))
    return json.dumps(parts, ensure_ascii=False)


def context_hash(segments: Sequence[Sequence[str]]) -> str:
    return _sha("\n\n".join(text for _eid, text in segments))


def pair_group_key(segments: Sequence[Sequence[str]], symbol: str, meaning: str) -> str:
    """Cross-object dedup group of one candidate pair: (context text hash,
    candidate). The same candidate in the same context is judged once."""
    return json.dumps(
        ["pair", context_hash(segments), kg_attrs.normalize_latex(symbol),
         kg_attrs.normalize_latex(meaning)],
        ensure_ascii=False,
    )


def card_key(segments: Sequence[Sequence[str]], formula: str) -> str:
    return json.dumps(
        ["card", context_hash(segments), kg_attrs.normalize_latex(formula)], ensure_ascii=False
    )


# ===========================================================================
# Sampling
# ===========================================================================

def stratified_sample(
    candidates: Sequence[Any],
    n: int,
    *,
    stratum: Callable[[Any], Hashable],
    seed: int,
    caps: Sequence[int | None] = STRATUM_CAPS,
) -> list[Any]:
    """Seeded sample of up to ``n`` candidates, at most ``cap`` per stratum
    (notebook, source); the cap relaxes 10 -> 20 -> 40 -> unlimited until
    ``n`` is reached or the candidates run out. Output keeps the shuffled
    order, so a later top-up continues the same sequence."""
    order = list(range(len(candidates)))
    random.Random(seed).shuffle(order)
    chosen: list[int] = []
    taken: set[int] = set()
    per_stratum: Counter = Counter()
    for cap in caps:
        for index in order:
            if len(chosen) >= n:
                break
            if index in taken:
                continue
            key = stratum(candidates[index])
            if cap is not None and per_stratum[key] >= cap:
                continue
            per_stratum[key] += 1
            taken.add(index)
            chosen.append(index)
        if len(chosen) >= n:
            break
    return [candidates[i] for i in chosen]


def blind_keys(keys: Sequence[str], *, seed: int, fraction: float = BLIND_FRACTION) -> set[str]:
    """Seeded ``fraction`` of ``keys`` (rounded up) reviewed blind: no pre-label,
    no product keep/reject mark, no reject reason."""
    if not keys:
        return set()
    ordered = sorted(keys)
    random.Random(seed).shuffle(ordered)
    return set(ordered[: math.ceil(len(ordered) * fraction)])


# ===========================================================================
# Labels
# ===========================================================================

def parse_labels(payload: Any) -> dict[str, dict]:
    """Validate an exported label file: ``{"labels": [{key, verdict?, missing?}]}``.

    Anything else (unknown fields, text, a bad verdict, a negative or
    non-integer missing count) is refused, so a label file can never carry
    source text back into the tool."""
    if not isinstance(payload, dict) or set(payload) != {"labels"} or not isinstance(payload["labels"], list):
        raise ValueError("label file must be {\"labels\": [...]}")
    out: dict[str, dict] = {}
    for entry in payload["labels"]:
        if not isinstance(entry, dict) or not set(entry) <= {"key", "verdict", "missing"}:
            raise ValueError("label entries may only carry key, verdict, missing")
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("label entry without a key")
        label: dict = {}
        if "verdict" in entry:
            if entry["verdict"] not in VERDICTS:
                raise ValueError(f"bad verdict for {key}")
            label["verdict"] = entry["verdict"]
        if "missing" in entry:
            missing = entry["missing"]
            if type(missing) is not int or missing < 0:
                raise ValueError(f"bad missing count for {key}")
            label["missing"] = missing
        out[key] = label
    return out


def merge_labels(old: Mapping[str, dict], new: Mapping[str, dict]) -> dict[str, dict]:
    merged = {key: dict(value) for key, value in old.items()}
    for key, value in new.items():
        merged.setdefault(key, {}).update(value)
    return merged


# ===========================================================================
# Formula recall (plan §2.5)
# ===========================================================================

def formula_recall(
    cards: Sequence[Mapping[str, Any]],
    empty_checks: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    hint_empty_scanned: int,
) -> dict:
    """Pooled recall ``R = H / (G + M + M_hat)`` for one language.

    * a card counts only when its missing count is filled and every pair in
      its pool is judged (an unfinished card is left out entirely);
    * ``H``: product-kept pairs judged correct; ``G``: pool pairs judged
      correct; ``M``: missing counts on the counted cards;
    * ``M_hat``: mean missing count over the filled empty-pool spot checks,
      times the number of scanned formulas whose context had a hint word but
      whose pool was empty;
    * denominator formulas: counted cards with ``G >= 1`` or ``M >= 1``.
    """
    h = g = m = denominator = with_hit = 0
    for card in cards:
        missing = labels.get(card["key"], {}).get("missing")
        verdicts = [labels.get(p["group"], {}).get("verdict") for p in card["pairs"]]
        if missing is None or any(v is None for v in verdicts):
            continue
        card_g = sum(v == "correct" for v in verdicts)
        card_h = sum(
            v == "correct" and p["origin"] == "kept" for p, v in zip(card["pairs"], verdicts)
        )
        h, g, m = h + card_h, g + card_g, m + missing
        if card_g or missing:
            denominator += 1
            with_hit += card_h > 0
    filled = [labels[c["key"]]["missing"] for c in empty_checks if "missing" in labels.get(c["key"], {})]
    m_hat = (sum(filled) / len(filled)) * hint_empty_scanned if filled else 0.0
    total = g + m + m_hat
    raw_total = g + m
    return {
        "denominator_formulas": denominator,
        "H": h, "G": g, "M": m, "M_hat": round(m_hat, 3),
        "empty_checks_filled": len(filled),
        "recall": round(h / total, 4) if total else None,
        "recall_without_m_hat": round(h / raw_total, 4) if raw_total else None,
        "formula_hit_rate": round(with_hit / denominator, 4) if denominator else None,
    }


def recall_verdict(recall: Mapping[str, Any], kept_pairs_judged: int) -> str:
    if (
        recall["denominator_formulas"] < FORMULA_MIN_DENOMINATOR
        or kept_pairs_judged < FORMULA_MIN_KEPT_PAIRS
        or recall["recall"] is None
    ):
        return "insufficient"
    return "pass" if recall["recall"] >= FORMULA_RECALL_LINE else "fail"


def combine_verdicts(*verdicts: str) -> str:
    """One row's overall result: any fail fails; then untested; then insufficient."""
    for outcome in ("fail", "untested", "insufficient"):
        if outcome in verdicts:
            return outcome
    return "pass"


# ===========================================================================
# Report
# ===========================================================================

def build_report(items: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]) -> dict:
    """Text-free judgement summary (``report.json``) from items plus labels."""
    alias_rows = []
    for rule in kg_attrs.ALIAS_RULES:
        block = [a for a in items.get("alias_items", []) if a["rule"] == rule]
        correct, wrong = count_verdicts(labels.get(a["key"], {}).get("verdict") for a in block)
        line = ALIAS_LINES[rule]
        alias_rows.append(_precision_row("alias", rule, correct, wrong, line, len(block)))
    formula_rows, recall_rows = [], []
    for language in LANGUAGES:
        scan = items.get("formula_scan", {}).get(language, {})
        cards = [c for c in items.get("formula_cards", []) if c["language"] == language]
        checks = [c for c in items.get("empty_checks", []) if c["language"] == language]
        groups = {p["group"] for c in cards for p in c["pairs"] if p["origin"] == "kept"}
        correct, wrong = count_verdicts(labels.get(gk, {}).get("verdict") for gk in groups)
        row = _precision_row("formula_precision", language, correct, wrong,
                             FORMULA_PRECISION_LINE, len(groups))
        recall = formula_recall(cards, checks, labels,
                                hint_empty_scanned=int(scan.get("hint_empty", 0)))
        recall_row = {"item": "formula_recall", "rule": language, **recall,
                      "line": FORMULA_RECALL_LINE,
                      "verdict": recall_verdict(recall, correct + wrong)}
        if scan.get("untested"):
            row["verdict"] = recall_row["verdict"] = "untested"
        formula_rows.append(row)
        recall_rows.append(recall_row)
    formula_overall = {
        lang: combine_verdicts(p["verdict"], r["verdict"])
        for lang, p, r in zip(LANGUAGES, formula_rows, recall_rows)
    }
    return {
        "meta": {k: items.get(k) for k in ("created", "seed", "rules_signature", "fixture_sha",
                                          "params", "models", "egress")},
        "alias": alias_rows,
        "formula_precision": formula_rows,
        "formula_recall": recall_rows,
        "formula_overall": formula_overall,
        "formula_ships": all(v == "pass" for v in formula_overall.values()),
        "alias_rules_passed": [r["rule"] for r in alias_rows if r["verdict"] == "pass"],
        "yield": items.get("yield", {}),
        "reject_reasons": _reject_reason_rows(items, labels),
        "prelabel_agreement": _prelabel_agreement(items, labels),
        "egress_log": items.get("egress_log", {}),
        "alias_recall_reference": items.get("alias_recall_reference", {}),
        "top_up_needed": _top_up_needed(alias_rows, formula_rows, recall_rows),
    }


def _precision_row(item: str, rule: str, correct: int, wrong: int, line: float, size: int) -> dict:
    n = correct + wrong
    return {
        "item": item, "rule": rule, "n": n, "sampled": size,
        "correct": correct, "wrong": wrong,
        "precision": round(correct / n, 4) if n else None,
        "wilson_lower": round(wilson_lower(correct, n), 4) if n else None,
        "line": line,
        "verdict": precision_verdict(correct, wrong, line),
    }


def _reject_reason_rows(items: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    counts: Counter = Counter()
    judged: Counter = Counter()
    right: Counter = Counter()
    for card in items.get("formula_cards", []):
        for pair in card["pairs"]:
            if pair["origin"] != "rejected":
                continue
            counts[pair["reason"]] += 1
            verdict = labels.get(pair["group"], {}).get("verdict")
            if verdict is not None:
                judged[pair["reason"]] += 1
                right[pair["reason"]] += verdict == "correct"
    total = sum(counts.values())
    return [
        {
            "reason": reason, "count": count, "share": round(count / total, 4),
            "judged": judged[reason],
            "judged_correct_rate": round(right[reason] / judged[reason], 4) if judged[reason] else None,
            "flag": count / total > 0.5,
        }
        for reason, count in counts.most_common()
    ]


def _prelabel_agreement(items: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]) -> dict:
    rows: list[tuple[bool, bool]] = []  # (agree, blind)
    for alias in items.get("alias_items", []):
        verdict = labels.get(alias["key"], {}).get("verdict")
        if verdict and alias.get("prelabel"):
            rows.append((alias["prelabel"]["verdict"] == verdict, bool(alias.get("blind"))))
    for card in items.get("formula_cards", []):
        for pair in card["pairs"]:
            verdict = labels.get(pair["group"], {}).get("verdict")
            if verdict and pair.get("prelabel"):
                rows.append((pair["prelabel"]["verdict"] == verdict, bool(card.get("blind"))))

    def rate(selected: list[bool]) -> float | None:
        return round(sum(selected) / len(selected), 4) if selected else None

    blind = rate([agree for agree, is_blind in rows if is_blind])
    return {
        "all": rate([agree for agree, _ in rows]), "all_n": len(rows),
        "blind": blind, "blind_n": sum(1 for _, b in rows if b),
        "blind_flag": blind is not None and blind < 0.8,
    }


def _top_up_needed(alias_rows, formula_rows, recall_rows) -> list[dict]:
    """Blocks still ``insufficient`` although every sampled item was judged:
    the sample itself is short, so ``prepare --top-up`` should draw more."""
    out = []
    for row in alias_rows + formula_rows:
        if row["verdict"] == "insufficient" and row["n"] >= row["sampled"]:
            out.append({"item": row["item"], "rule": row["rule"], "short_by": N_TARGET - row["n"]})
    for row in recall_rows:
        if row["verdict"] == "insufficient":
            out.append({"item": row["item"], "rule": row["rule"],
                        "short_by": max(0, FORMULA_MIN_DENOMINATOR - row["denominator_formulas"])})
    return out


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)


def render_report_md(report: Mapping[str, Any], *, local_examples: Sequence[str] = ()) -> str:
    """``report.md`` (local only). ``local_examples`` are English-pool error
    examples; nothing else in the report carries source text."""
    meta = report["meta"]
    models = meta.get("models") or {}
    formula_model = models.get("formula") or {}
    params = meta.get("params") or {}
    lines = [
        f"# KG attrs Gate 0 报告（{meta.get('created')}，抽样种子 {meta.get('seed')}，"
        f"rules_signature {meta.get('rules_signature')}，夹具 sha {meta.get('fixture_sha')}）",
        f"模型：抽取 {models.get('extract')}；公式 service={formula_model.get('service')} "
        f"model={formula_model.get('model')} fingerprint={formula_model.get('fingerprint')}；"
        f"预标/草稿 {models.get('judge')}",
        f"参数：max_chars={params.get('max_chars')} pair_window={params.get('pair_window')} "
        f"窗口 n={params.get('window_n')} m={params.get('window_m')}；"
        f"出站开关：{'开' if meta.get('egress') else '关'}",
        "## 判定",
        "| 项 | 规则/语言 | n | 对 | 错(含不确定) | 精度 | Wilson 下界 | 线 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["alias"] + report["formula_precision"]:
        lines.append(
            f"| {row['item']} | {row['rule']} | {row['n']} | {row['correct']} | {row['wrong']} | "
            f"{_fmt(row['precision'])} | {_fmt(row['wilson_lower'])} | {row['line']} | {row['verdict']} |"
        )
    lines += [
        "",
        "| 公式召回 | 语言 | 分母公式 | H | G | M | M̂ | 召回 | 不含 M̂ | 至少命中一条 | 线 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["formula_recall"]:
        lines.append(
            f"| 公式召回 | {row['rule']} | {row['denominator_formulas']} | {row['H']} | {row['G']} | "
            f"{row['M']} | {row['M_hat']} | {_fmt(row['recall'])} | {_fmt(row['recall_without_m_hat'])} | "
            f"{_fmt(row['formula_hit_rate'])} | {row['line']} | {row['verdict']} |"
        )
    lines.append("")
    lines.append(f"公式逐语言结论：{report['formula_overall']}；公式上线：{report['formula_ships']}")
    lines.append(f"通过的别名规则：{report['alias_rules_passed'] or '无'}")
    lines += ["## 产出率（按池）", "```", json.dumps(report["yield"], ensure_ascii=False, indent=1), "```"]
    lines += ["## 拒绝原因分布", "| 原因 | 次数 | 占比 | 已判 | 用户判对的比例（误杀率） |", "|---|---|---|---|---|"]
    for row in report["reject_reasons"]:
        reason = f"**{row['reason']}（>50%）**" if row["flag"] else row["reason"]
        lines.append(f"| {reason} | {row['count']} | {_fmt(row['share'])} | {row['judged']} | "
                     f"{_fmt(row['judged_correct_rate'])} |")
    agreement = report["prelabel_agreement"]
    blind = _fmt(agreement["blind"]) + ("（**< 0.8**）" if agreement["blind_flag"] else "")
    lines += [
        "## 预标一致性",
        f"全部 {_fmt(agreement['all'])}（n={agreement['all_n']}）；盲审 {blind}（n={agreement['blind_n']}）",
        "## 提前停止、补抽、样本不足、出站记录",
        f"需要补抽：{report['top_up_needed'] or '无'}",
        "```", json.dumps(report["egress_log"], ensure_ascii=False, indent=1), "```",
        "## 别名召回参照（只报告）",
        "```", json.dumps(report["alias_recall_reference"], ensure_ascii=False, indent=1), "```",
        "## 错例（只放英文池，每类最多 5 条）",
        *[f"- {example}" for example in local_examples],
    ]
    return "\n".join(lines) + "\n"


# ===========================================================================
# Review page (plan §2.4). Template and script are constants so the safety
# tests scan exactly what ships; only the embedded JSON varies.
# ===========================================================================

_PAGE_HEAD = (
    '<!doctype html>\n<html lang="zh-CN">\n<head>\n'
    '<meta http-equiv="Content-Security-Policy" content="__CSP__">\n'
    '<meta charset="utf-8">\n<title>KG attrs Gate 0 复核</title>\n'
)

_PAGE_STYLE = """<style>
body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;margin:0;background:#fafafa;color:#222}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:8px 16px;z-index:1}
main{max-width:960px;margin:0 auto;padding:16px}
.blocks{display:flex;flex-wrap:wrap;gap:8px}
.block{border:1px solid #ccc;border-radius:6px;padding:4px 8px;cursor:pointer;background:#fff}
.block.active{border-color:#2563eb}
.block .hint{color:#b91c1c;font-weight:600}
.block .ok{color:#15803d;font-weight:600}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;margin-top:12px}
.meta{color:#666;font-size:12px}
.src{white-space:pre-wrap;background:#f4f4f5;border-radius:6px;padding:8px;margin:8px 0}
mark{background:#fde68a}
.pair{display:flex;gap:8px;align-items:center;padding:4px 0;border-top:1px dashed #eee}
.pair.focus{background:#eff6ff}
button{font:inherit;padding:2px 10px;border:1px solid #bbb;border-radius:4px;background:#fff;cursor:pointer}
button:active{transform:translateY(1px);background:#e5e7eb}
button.on{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
.status{margin-left:8px;color:#15803d}
code{font-family:ui-monospace,Menlo,monospace}
</style>
"""

_PAGE_BODY = """</head>
<body>
<header>
  <div class="blocks" id="blocks"></div>
  <div class="meta">1 对 · 2 错 · 3 不确定（计为错） · N 备注 · J/K 翻页 · 漏标数必须显式填写（可以填 0）</div>
  <button id="export">导出标注</button><span class="status" id="export-status"></span>
  <label>导入标注 <input type="file" id="import" accept=".json"></label><span class="status" id="import-status"></span>
</header>
<main id="main"></main>
"""

_PAGE_SCRIPT = r"""<script>
"use strict";
const DATA = JSON.parse(document.getElementById("kg-data").textContent);
const STORE = "kg_attrs_gate0:" + DATA.run;
let labels = {};
try { labels = JSON.parse(localStorage.getItem(STORE) || "{}") || {}; } catch (e) { labels = {}; }
let blockIndex = 0, cardIndex = 0, pairIndex = 0;

function save() { try { localStorage.setItem(STORE, JSON.stringify(labels)); } catch (e) {} }
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function label(key) { if (!labels[key]) labels[key] = {}; return labels[key]; }
function units(block) {
  const out = [];
  for (const ci of block.order) {
    const card = DATA.cards[ci];
    if (card.kind === "pairs") { for (const p of card.pairs) if (p.first) out.push(p.group); }
    else if (card.kind === "alias") out.push(card.key);
  }
  return out;
}
function progress(block) {
  let correct = 0, wrong = 0, judged = 0;
  for (const key of units(block)) {
    const v = labels[key] && labels[key].verdict;
    if (!v) continue;
    judged += 1;
    if (v === "correct") correct += 1; else wrong += 1;
  }
  let missing = 0;
  for (const ci of block.order) {
    const card = DATA.cards[ci];
    if (card.needs_missing && labels[card.key] && Number.isInteger(labels[card.key].missing)) missing += 1;
  }
  return {correct, wrong, judged, missing};
}
function drawBlocks() {
  const box = document.getElementById("blocks");
  box.replaceChildren();
  DATA.blocks.forEach((block, i) => {
    const p = progress(block);
    const node = el("div", "block" + (i === blockIndex ? " active" : ""));
    node.append(el("span", "", block.title + " "));
    if (block.unit_total) node.append(el("span", "meta", p.judged + "/" + block.unit_total + " "));
    if (block.missing_total) node.append(el("span", "meta", "漏标 " + p.missing + "/" + block.missing_total + " "));
    if (block.max_wrong !== null && p.wrong > block.max_wrong) node.append(el("span", "hint", "本块已判定失败，可以停"));
    else if (block.k_min !== null && p.correct >= block.k_min && p.judged >= block.n_target) node.append(el("span", "ok", "已达标"));
    node.addEventListener("click", () => { blockIndex = i; cardIndex = 0; pairIndex = 0; draw(); });
    box.append(node);
  });
}
function sourceBox(segments, highlight) {
  const box = el("div", "src");
  for (const seg of segments) {
    const text = seg[1];
    const at = highlight ? text.indexOf(highlight) : -1;
    if (at < 0) { box.append(el("span", "", text)); }
    else {
      box.append(el("span", "", text.slice(0, at)));
      box.append(el("mark", "", highlight));
      box.append(el("span", "", text.slice(at + highlight.length)));
    }
    box.append(el("br"));
  }
  return box;
}
function verdictButtons(key, onSet) {
  const row = el("span");
  [["correct", "对"], ["incorrect", "错"], ["unsure", "不确定"]].forEach(([value, text]) => {
    const b = el("button", labels[key] && labels[key].verdict === value ? "on" : "", text);
    b.addEventListener("click", () => { label(key).verdict = value; save(); onSet(); });
    row.append(b);
  });
  return row;
}
function missingInput(card) {
  const wrap = el("label", "", "漏标数（原文写明含义、但上面没有列出的符号个数）：");
  const input = el("input");
  input.type = "number"; input.min = "0"; input.step = "1";
  const cur = labels[card.key] && labels[card.key].missing;
  input.value = Number.isInteger(cur) ? String(cur) : "";
  input.addEventListener("change", () => {
    const n = Number(input.value);
    if (input.value === "" || !Number.isInteger(n) || n < 0) { if (labels[card.key]) delete labels[card.key].missing; }
    else label(card.key).missing = n;
    save(); drawBlocks();
  });
  wrap.append(input);
  return wrap;
}
function drawCard(card) {
  const box = el("div", "card");
  box.append(el("div", "meta", DATA.blocks[blockIndex].title + " · " + (cardIndex + 1) + "/" + DATA.blocks[blockIndex].order.length + (card.blind ? " · 盲审" : "")));
  if (card.kind === "alias") {
    box.append(el("div", "", "规则 " + card.rule + " · 概念：" + card.name));
    box.append(el("div", "", "候选别名：" + card.alias));
    box.append(sourceBox(card.segments, card.alias));
    if (card.prelabel) box.append(el("div", "meta", "预标：" + card.prelabel.verdict + (card.prelabel.reason ? "（" + card.prelabel.reason + "）" : "")));
    box.append(verdictButtons(card.key, () => { next(); }));
  } else {
    box.append(el("div", "", "公式："));
    box.append(el("code", "", card.formula));
    box.append(sourceBox(card.segments, null));
    card.pairs.forEach((pair, i) => {
      if (!pair.first) return;
      const row = el("div", "pair" + (i === pairIndex ? " focus" : ""));
      row.append(el("code", "", pair.symbol));
      row.append(el("span", "", "＝ " + pair.meaning));
      if (pair.origin_label) row.append(el("span", "meta", pair.origin_label));
      if (pair.prelabel) row.append(el("span", "meta", "预标：" + pair.prelabel.verdict));
      row.append(verdictButtons(pair.group, () => { drawBlocks(); draw(); }));
      box.append(row);
    });
    const shown = card.pairs.filter(p => p.first).length;
    if (shown < card.pairs.length) box.append(el("div", "meta", "另有 " + (card.pairs.length - shown) + " 个候选已在别的卡上判定"));
    if (card.needs_missing) box.append(missingInput(card));
  }
  if (labels[card.key] && labels[card.key].note) box.append(el("div", "meta", "备注：" + labels[card.key].note));
  return box;
}
function current() { const b = DATA.blocks[blockIndex]; return b && b.order.length ? DATA.cards[b.order[cardIndex]] : null; }
function draw() {
  drawBlocks();
  const main = document.getElementById("main");
  main.replaceChildren();
  const card = current();
  if (card) main.append(drawCard(card));
  else main.append(el("div", "card", "本块没有条目"));
}
function next() { const b = DATA.blocks[blockIndex]; if (cardIndex < b.order.length - 1) { cardIndex += 1; pairIndex = 0; } draw(); }
function prev() { if (cardIndex > 0) { cardIndex -= 1; pairIndex = 0; } draw(); }
function setVerdict(value) {
  const card = current();
  if (!card) return;
  if (card.kind === "alias") { label(card.key).verdict = value; save(); next(); return; }
  const shown = card.pairs.map((p, i) => [p, i]).filter(x => x[0].first);
  const hit = shown.find(x => x[1] === pairIndex) || shown[0];
  if (!hit) return;
  label(hit[0].group).verdict = value; save();
  const later = shown.find(x => x[1] > hit[1]);
  if (later) pairIndex = later[1];
  draw();
}
function exportRecords() {
  return Object.keys(labels).sort().map(key => {
    const record = {key: key};
    if (labels[key].verdict) record.verdict = labels[key].verdict;
    if (Number.isInteger(labels[key].missing)) record.missing = labels[key].missing;
    return record;
  }).filter(r => r.verdict || Number.isInteger(r.missing));
}
document.getElementById("export").addEventListener("click", () => {
  const status = document.getElementById("export-status");
  status.textContent = "导出中…";
  const blob = new Blob([JSON.stringify({labels: exportRecords()})], {type: "application/json"});
  const link = el("a");
  link.href = URL.createObjectURL(blob);
  link.download = "__LABELS__";
  document.body.append(link); link.click(); link.remove();
  status.textContent = "已导出 " + exportRecords().length + " 条（浏览器「下载」目录）";
});
document.getElementById("import").addEventListener("change", (event) => {
  const status = document.getElementById("import-status");
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  status.textContent = "读取中…";
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(String(reader.result));
      let n = 0;
      for (const r of (data.labels || [])) {
        if (typeof r.key !== "string") continue;
        const target = label(r.key);
        if (["correct", "incorrect", "unsure"].includes(r.verdict)) target.verdict = r.verdict;
        if (Number.isInteger(r.missing) && r.missing >= 0) target.missing = r.missing;
        n += 1;
      }
      save(); draw();
      status.textContent = "已导入 " + n + " 条";
    } catch (e) { status.textContent = "文件无法读取"; }
  };
  reader.readAsText(file);
});
document.addEventListener("keydown", (event) => {
  if (event.target && event.target.tagName === "INPUT") return;
  if (event.key === "1") setVerdict("correct");
  else if (event.key === "2") setVerdict("incorrect");
  else if (event.key === "3") setVerdict("unsure");
  else if (event.key === "j" || event.key === "J") next();
  else if (event.key === "k" || event.key === "K") prev();
  else if (event.key === "n" || event.key === "N") {
    const card = current();
    if (!card) return;
    const text = window.prompt("备注（只存在本机浏览器，不会导出）", (labels[card.key] && labels[card.key].note) || "");
    if (text !== null) { label(card.key).note = text; save(); draw(); }
  }
});
draw();
</script>
"""

_PAGE_TAIL = "</body>\n</html>\n"


def page_template_parts() -> tuple[str, ...]:
    """Every non-data part of the review page, for the safety scan."""
    head = _PAGE_HEAD.replace("__CSP__", CSP)
    script = _PAGE_SCRIPT.replace("__LABELS__", LABELS_FILE_NAME)
    return (head, _PAGE_STYLE, _PAGE_BODY, script, _PAGE_TAIL)


def embed_json(data: Any) -> str:
    """JSON for an inline ``application/json`` script: every ``<`` escaped, so
    no data value can close the element or open a tag."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")


def render_review_html(page_data: Mapping[str, Any]) -> str:
    head, style, body, script, tail = page_template_parts()
    data = (
        '<script type="application/json" id="kg-data">'
        + embed_json(page_data)
        + "</script>\n"
    )
    return head + style + body + data + script + tail


def review_page_data(items: Mapping[str, Any], *, seed: int) -> dict:
    """Blocks in the suggested order (aliases, English formulas, Chinese
    formulas, spot checks), cards in a seeded random order per block, blind
    cards stripped of pre-label, product keep/reject mark and reason code."""
    cards: list[dict] = []
    blocks: list[dict] = []
    rng = random.Random(seed)

    def add_block(block_id: str, title: str, block_cards: list[dict], line: float | None,
                  unit_total: int, missing_total: int) -> None:
        order = list(range(len(cards), len(cards) + len(block_cards)))
        cards.extend(block_cards)
        rng.shuffle(order)
        blocks.append({
            "id": block_id, "title": title, "order": order,
            "n_target": N_TARGET,
            "k_min": k_min(N_TARGET, line) if line is not None else None,
            "max_wrong": N_TARGET - k_min(N_TARGET, line) if line is not None else None,
            "unit_total": unit_total, "missing_total": missing_total,
        })

    for rule in kg_attrs.ALIAS_RULES:
        block_cards = []
        for alias in items.get("alias_items", []):
            if alias["rule"] != rule:
                continue
            block_cards.append({
                "kind": "alias", "key": alias["key"], "rule": rule, "name": alias["name"],
                "alias": alias["alias"], "segments": alias["segments"],
                "blind": bool(alias.get("blind")),
                "prelabel": None if alias.get("blind") else alias.get("prelabel"),
            })
        add_block(f"alias-{rule}", f"别名 {rule}", block_cards, ALIAS_LINES[rule],
                  len(block_cards), 0)
    seen_groups: set[str] = set()
    for language, title in (("en", "英文公式"), ("zh", "中文公式")):
        block_cards = []
        for card in items.get("formula_cards", []):
            if card["language"] != language:
                continue
            blind = bool(card.get("blind"))
            pairs = []
            for pair in card["pairs"]:
                first = pair["group"] not in seen_groups
                seen_groups.add(pair["group"])
                pairs.append({
                    "group": pair["group"], "symbol": pair["symbol"], "meaning": pair["meaning"],
                    "first": first,
                    "origin_label": None if blind else _origin_label(pair),
                    "prelabel": None if blind else pair.get("prelabel"),
                })
            block_cards.append({
                "kind": "pairs", "key": card["key"], "formula": card["formula"],
                "segments": card["segments"], "pairs": pairs, "blind": blind,
                "needs_missing": True,
            })
        units = sum(p["first"] for c in block_cards for p in c["pairs"])
        add_block(f"formula-{language}", title, block_cards, FORMULA_PRECISION_LINE,
                  units, len(block_cards))
    for language, title in (("en", "英文空池抽查"), ("zh", "中文空池抽查")):
        block_cards = [
            {"kind": "empty", "key": c["key"], "formula": c["formula"], "segments": c["segments"],
             "pairs": [], "blind": False, "needs_missing": True}
            for c in items.get("empty_checks", []) if c["language"] == language
        ]
        add_block(f"empty-{language}", title, block_cards, None, 0, len(block_cards))
    return {"run": items.get("run", ""), "blocks": blocks, "cards": cards}


def _origin_label(pair: Mapping[str, Any]) -> str:
    if pair["origin"] == "kept":
        return "产品保留"
    if pair["origin"] == "rejected":
        return f"产品拒绝（{pair.get('reason')}）"
    return "草稿"


# ===========================================================================
# Local output location
# ===========================================================================

def main_checkout_root(start: Path | None = None) -> Path:
    """The main checkout (parent of the shared ``.git``), never a worktree."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(start or Path(__file__).resolve().parent),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return Path(common).parent


def local_output_dir(root: Path | None = None) -> Path:
    """``<main checkout>/.local/kg_attrs_gate0``; refuses a path git does not ignore."""
    root = root or main_checkout_root()
    target = root / LOCAL_SUBDIR
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(target / "items.json")], cwd=str(root),
    ).returncode == 0
    if not ignored:
        raise SystemExit(f"refusing to write Gate 0 outputs to a path git does not ignore: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# Model plumbing shared by extract-en and prepare
# ===========================================================================

EN_SLICE_PATH = Path(__file__).resolve().parent / "kg_attrs_gate0" / "en_slice.json"
# Pool A chapters: the repository's own test-case sources (spec §13.1).
EN_BOOKS = (("cmos", "textbook"), ("engram", "academic_paper"))
# Extraction windows are pinned (not adaptive) so the slice is reproducible.
EXTRACT_WINDOW_N = 8000
EXTRACT_WINDOW_M = 450
# Rough fixed prompt size of one extraction pass, for the up-front estimate.
EXTRACT_PROMPT_CHARS = 6000
DEFAULT_MAX_CALLS = 4000
DEFAULT_MAX_INPUT_CHARS = 20_000_000
FINGERPRINT_PREFIX_CHARS = 16


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Hard caps on calls actually sent, judged on the running total BEFORE a
    send; a call answered from the response cache is not counted."""

    def __init__(self, max_calls: int, max_chars: int) -> None:
        import threading

        self.max_calls, self.max_chars = max_calls, max_chars
        self.calls = self.chars = 0
        self.cache_hits = 0
        self._lock = threading.Lock()

    def admit(self, chars: int) -> None:
        with self._lock:
            if self.calls + 1 > self.max_calls or self.chars + chars > self.max_chars:
                raise BudgetExceeded(
                    f"budget reached: {self.calls} calls / {self.chars} chars sent "
                    f"(caps {self.max_calls} / {self.max_chars})"
                )

    def charge(self, chars: int, *, cache_hit: bool) -> None:
        with self._lock:
            if cache_hit:
                self.cache_hits += 1
            else:
                self.calls += 1
                self.chars += chars


def messages_chars(messages: Sequence[Mapping[str, str]]) -> int:
    return sum(len(m.get("content", "")) for m in messages)


def _tool_environment(root: Path) -> None:
    """Point a tool process at the main checkout's configuration WITHOUT
    letting it touch any database: DATABASE_URL goes to a throwaway SQLite
    path nothing opens, events/LLM logs are off, and the relative model
    config / response-cache paths of the main checkout are made absolute so a
    worktree run reuses the same files. Must run before app.core.config is
    imported."""
    import os
    import tempfile

    os.environ.setdefault("SILICON_NOTEBOOK_ENV_FILE", str(root / ".env"))
    scratch = tempfile.mkdtemp(prefix="kg_attrs_gate0_")
    os.environ["DATABASE_URL"] = f"sqlite:///{scratch}/unused.db"
    os.environ["SILICON_NOTEBOOK_STORAGE_DIR"] = f"{scratch}/storage"
    os.environ["EVENT_LOG_ENABLED"] = "false"
    os.environ["LLM_LOG_ENABLED"] = "false"
    os.environ.setdefault("MODEL_BINDINGS_STRICT", "false")
    dotenv: dict[str, str] = {}
    env_file = Path(os.environ["SILICON_NOTEBOOK_ENV_FILE"])
    if env_file.is_file():
        from dotenv import dotenv_values

        dotenv = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    for key, default in (("MODEL_SERVICES_CONFIG", ""), ("LLM_CACHE_PATH", ".local/llm_cache_v2.db")):
        value = os.environ.get(key) or dotenv.get(key) or default
        if value and not Path(value).is_absolute():
            os.environ[key] = str(root / value)


def _open_models():
    """(settings, provider) for a tool process; caller closes the provider."""
    from app.core.config import Settings
    from app.core.event_logging import EventLogger
    from app.services.model_provider import RuntimeModelProvider

    settings = Settings()
    provider = RuntimeModelProvider(settings, EventLogger(settings, channel="events", per_user=True))
    return settings, provider


def _service_record(service: Any) -> dict:
    from urllib.parse import urlparse

    return {
        "service": service.id,
        "model": service.model,
        "host": urlparse(service.base_url).hostname or "",
        "fingerprint": service.fingerprint[:FINGERPRINT_PREFIX_CHARS],
    }


def formula_workload(registry: Any, service_id: str) -> str:
    """A chat workload bound to ``service_id`` (first by id), asserted to route
    there: the Gate 0 formula call must hit the service ``kg_attr_enrich``
    will be bound to."""
    candidates = sorted(w.id for w in registry.workloads_for(service_id) if w.kind == "chat")
    if not candidates:
        raise SystemExit(f"no chat workload is bound to model service {service_id!r}")
    workload = candidates[0]
    bound = registry.service_for(workload)
    assert bound is not None and bound.id == service_id, (workload, bound)
    return workload


def call_formula(client: Any, formula: str, segments: Sequence[Sequence[str]],
                 budget: Budget) -> tuple[str | None, str | None]:
    """The product formula call (no thinking, shape validator => cached).
    Returns (raw reply, None) or (None, exception class name)."""
    messages = kg_attrs.formula_variables_messages(formula, [tuple(s) for s in segments])
    chars = messages_chars(messages)
    budget.admit(chars)
    stats: dict = {}
    try:
        raw = client.chat_json(
            messages, kg_attrs.FORMULA_VARIABLES_HINT, thinking_mode="disabled",
            response_validator=kg_attrs.formula_variables_reply_ok, call_stats=stats,
        )
    except BudgetExceeded:
        raise
    except Exception as exc:  # recorded as status "failed", never retried here
        budget.charge(chars, cache_hit=False)
        return None, type(exc).__name__
    budget.charge(chars, cache_hit=stats.get("status") == "cache_hit")
    return raw, None


def verification_record(formula: str, segments: Sequence[Sequence[str]], reply: str | None,
                        *, pair_window: int) -> dict:
    if reply is None:
        return {"status": kg_attrs.FORMULA_STATUS_FAILED, "kept": [], "rejected": []}
    result = kg_attrs.verify_formula_variables(
        formula, [tuple(s) for s in segments], reply, pair_window=pair_window)
    return {"status": result.status, "kept": list(result.kept), "rejected": list(result.rejected)}


# ===========================================================================
# extract-en: pool A through the product extraction chain (plan G0-T3)
# ===========================================================================

def en_chapters(repo_root: Path) -> list[tuple[str, str, str, Path]]:
    """(book, chapter, doc_type, source.md) for the 14 English test-case chapters."""
    out = []
    for book, doc_type in EN_BOOKS:
        for path in sorted((repo_root / "fangan" / "testcases" / book).glob("*/source.md")):
            out.append((book, path.parent.name, doc_type, path))
    return out


def element_id_for(book: str, chapter: str, index: int) -> str:
    return f"el-en-{book}-{chapter}-{index:05d}"


def chapter_records(
    book: str, chapter: str, elements: Sequence[Any], objects: Sequence[Mapping[str, Any]],
    *, max_chars: int,
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Concept entries (with every-rule alias hits) and formula entries (with
    their context, before the model call) for one chapter, plus the texts of
    every element they reference. Pure: ``elements`` already carry ids."""
    by_id = {el.id: (i, el.text) for i, el in enumerate(elements)}
    used: dict[str, str] = {}
    concepts, formulas = [], []
    for obj in objects:
        name = str((obj.get("payload") or {}).get("name") or "")
        evidence = [e["element_id"] for e in obj.get("evidence", []) if e.get("element_id") in by_id]
        if not name or not evidence:
            continue
        if obj["object_type"] == "concept":
            items = [{"element_id": eid, "element_text": by_id[eid][1], "element_read": True}
                     for eid in evidence]
            hits = kg_attrs.trace_concept_aliases(name, items, exclude_names=(),
                                                  rules=kg_attrs.ALIAS_RULES)
            used.update({eid: by_id[eid][1] for eid in evidence})
            concepts.append({"book": book, "chapter": chapter, "name": name,
                             "evidence": evidence, "hits": hits})
        elif obj["object_type"] == "formula":
            first = evidence[0]
            index = by_id[first][0]
            following = [(elements[index + 1].id, elements[index + 1].text)] if index + 1 < len(elements) else []
            segments = kg_attrs.build_formula_context(
                (first, by_id[first][1]), following, max_chars=max_chars)
            used[first] = by_id[first][1]
            used.update(dict(following))
            formulas.append({
                "book": book, "chapter": chapter, "name": name, "element_id": first,
                "next_element_id": following[0][0] if following else None,
                "context": [[eid, len(text)] for eid, text in segments],
                "segments": [list(s) for s in segments],
            })
    return concepts, formulas, used


def slice_segments(entry: Mapping[str, Any], elements: Mapping[str, str], *, max_chars: int) -> list:
    """Rebuild a pool-A formula's context from the fixture (drift guard)."""
    first = (entry["element_id"], elements[entry["element_id"]])
    nxt = entry.get("next_element_id")
    following = [(nxt, elements[nxt])] if nxt else []
    return [list(s) for s in kg_attrs.build_formula_context(first, following, max_chars=max_chars)]


def _estimate_extraction(chapters, settings) -> tuple[int, int, list[dict]]:
    from app.services import kg_ingest
    from app.services.kg.filters import should_extract_window
    from app.services.kg.windowing import windows_with_elements

    passes = 1 + int(settings.kg_refine_enabled) + (
        settings.kg_gleaning_rounds if settings.kg_gleaning_enabled else 0)
    calls = chars = 0
    rows = []
    for book, chapter, doc_type, path in chapters:
        raw = path.read_text(encoding="utf-8")
        kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type, "academic")
        kept = [
            sum(len(e.text) for e in els)
            for w, els in windows_with_elements(raw, f"{chapter}.md", None,
                                                EXTRACT_WINDOW_N, EXTRACT_WINDOW_M)
            if els and should_extract_window(w.section_path, els, kg_doc_type)[0]
        ]
        calls += len(kept) * passes
        chars += sum(size + EXTRACT_PROMPT_CHARS for size in kept) * passes
        rows.append({"book": book, "chapter": chapter, "windows": len(kept), "chars": len(raw)})
    return calls, chars, rows


def run_extract_en(args: Any) -> int:
    from datetime import date

    root = main_checkout_root()
    _tool_environment(root)
    repo_root = Path(__file__).resolve().parents[3]
    chapters = en_chapters(repo_root)
    settings, provider = _open_models()
    try:
        est_calls, est_chars, rows = _estimate_extraction(chapters, settings)
        print(f"[extract-en] {len(chapters)} chapters, extraction <= {est_calls} calls / "
              f"{est_chars} chars before cache (windows n={EXTRACT_WINDOW_N} m={EXTRACT_WINDOW_M})")
        for row in rows:
            print(f"  {row['book']}/{row['chapter']}: {row['windows']} windows, {row['chars']} chars")
        if args.dry_run:
            return 0
        budget = Budget(args.max_calls, args.max_input_chars)
        if est_calls > args.max_calls or est_chars > args.max_input_chars:
            raise SystemExit("extraction estimate exceeds --max-calls / --max-input-chars")
        budget.calls, budget.chars = est_calls, est_chars
        workload = formula_workload(provider.registry, args.formula_service)
        extract_service = provider.registry.service_for("kg_extract")
        formula_service = provider.registry.service_for(workload)
        if extract_service is None or formula_service is None:
            raise SystemExit("kg_extract or the formula service is not configured")
        print(f"[extract-en] extraction -> {_service_record(extract_service)['host']}; "
              f"formula ({workload}) -> {_service_record(formula_service)['host']}")
        fixture = _extract_slice(chapters, settings, provider, workload, budget)
        fixture.update({
            "generated": date.today().isoformat(),
            "models": {"extract": _service_record(extract_service),
                       "formula": {**_service_record(formula_service), "workload": workload}},
        })
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fixture, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"[extract-en] wrote {out} ({len(fixture['concepts'])} concepts, "
              f"{len(fixture['formulas'])} formulas); formula calls sent "
              f"{budget.calls - est_calls}, cache hits {budget.cache_hits}")
        return 0
    finally:
        provider.close()


def _extract_slice(chapters, settings, provider, workload: str, budget: Budget) -> dict:
    import concurrent.futures as cf

    from app.services import kg_ingest, parsers

    concepts: list[dict] = []
    formulas: list[dict] = []
    elements: dict[str, str] = {}
    chapter_rows = []
    gleaning = settings.kg_gleaning_rounds if settings.kg_gleaning_enabled else 0
    for book, chapter, doc_type, path in chapters:
        raw = path.read_text(encoding="utf-8")
        source_id = f"src-en-{book}-{chapter}"
        parsed = parsers.parse_markdown_text(source_id, raw)
        for index, element in enumerate(parsed):
            element.id = element_id_for(book, chapter, index)
        kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type, "academic")
        graph = kg_ingest.extract_graph(
            provider, raw, f"{chapter}.md", kg_doc_type,
            n=EXTRACT_WINDOW_N, m=EXTRACT_WINDOW_M,
            refine=settings.kg_refine_enabled, gleaning_rounds=gleaning,
        )
        objects, _relations = kg_ingest.build_records(graph, source_id, chapter, parsed)
        chapter_concepts, chapter_formulas, used = chapter_records(
            book, chapter, parsed, objects, max_chars=settings.kg_attrs_context_chars)
        concepts += chapter_concepts
        formulas += chapter_formulas
        elements.update(used)
        chapter_rows.append({
            "book": book, "chapter": chapter, "doc_type": kg_doc_type,
            "windows_total": graph.total_windows, "windows_failed": graph.failed_windows,
            "concepts": len(chapter_concepts), "formulas": len(chapter_formulas),
        })
        print(f"  {book}/{chapter}: {graph.total_windows} windows "
              f"({graph.failed_windows} failed), {len(chapter_concepts)} concepts, "
              f"{len(chapter_formulas)} formulas", flush=True)
    client = provider.chat(workload)

    def one(entry: dict) -> None:
        reply, error = call_formula(client, entry["name"], entry["segments"], budget)
        entry["reply"], entry["error"] = reply, error
        entry["verification"] = verification_record(
            entry["name"], entry["segments"], reply, pair_window=settings.kg_attrs_pair_window)

    with cf.ThreadPoolExecutor(max_workers=max(1, provider.parallelism(workload))) as pool:
        for future in [pool.submit(one, entry) for entry in formulas]:
            future.result()
    for entry in formulas:
        del entry["segments"]
    return {
        "format": 1,
        "rules_signature": kg_attrs.rules_signature(),
        "params": {
            "max_chars": settings.kg_attrs_context_chars,
            "pair_window": settings.kg_attrs_pair_window,
            "window_n": EXTRACT_WINDOW_N, "window_m": EXTRACT_WINDOW_M,
            "refine": bool(settings.kg_refine_enabled), "gleaning_rounds": gleaning,
        },
        "chapters": chapter_rows,
        "elements": elements,
        "concepts": concepts,
        "formulas": formulas,
    }


# ===========================================================================
# prepare: pools A/B/C, model calls, items.json (plan G0-T4, §2.3)
# ===========================================================================

POOLS = ("A", "B", "C")
LOCAL_POOLS = ("B", "C")
CJK_SHARE_ZH = 0.3
FORMULA_SCAN_TARGET_POOLED = 140
FORMULA_SCAN_TARGET_KEPT_PAIRS = 120
DEFAULT_MAX_FORMULAS_PER_LANGUAGE = 900
DEFAULT_PER_RULE = 100
DEFAULT_SEED = 20261028
MAX_PAIR_PRELABEL_CARDS = 300
# Rough pairs per card for the dry-run estimate of the pair pre-label prompt.
ESTIMATED_PAIRS_PER_CARD = 4
_HINT_PATTERN = r"\bwhere\b|\bhere\b|其中|式中|为|表示"
CALL_KINDS = ("formula", "draft", "alias_prelabel", "pair_prelabel")
CALL_KIND_LABELS = {
    "formula": "产品公式调用", "draft": "gold 草稿",
    "alias_prelabel": "别名预标", "pair_prelabel": "公式配对预标",
}
DRAFT_HINT = '{"pairs":[{"symbol":"","meaning":""}]}'
ALIAS_PRELABEL_HINT = '{"verdict":"correct|incorrect|unsure","reason":""}'
PAIR_PRELABEL_HINT = '{"judgements":[{"i":0,"verdict":"correct|incorrect|unsure","reason":""}]}'
_JUDGE_ENV_PREFIX = "EVAL_JUDGE_"


def has_hint_word(segments: Sequence[Sequence[str]]) -> bool:
    import re

    text = "\n".join(t for _eid, t in segments)
    return re.search(_HINT_PATTERN, text, re.IGNORECASE) is not None


def cjk_share(cjk_chars: int, visible_chars: int) -> float:
    return cjk_chars / visible_chars if visible_chars else 0.0


def check_local_database_url(url: str) -> None:
    """Only localhost / 127.0.0.1 / ::1 / a unix socket may be read."""
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(url)
    for field in ("host", "hostaddr"):
        for host in filter(None, str(info.get(field) or "").split(",")):
            if host.startswith("/") or host in ("localhost", "127.0.0.1", "::1"):
                continue
            raise SystemExit(f"--database-url must point at this machine (got host {host!r})")


def read_only_connection(url: str):
    """psycopg connection whose every statement runs read-only."""
    import psycopg
    from psycopg.rows import dict_row

    check_local_database_url(url)
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    conn.execute("SET default_transaction_read_only = on")
    conn.execute("BEGIN READ ONLY")
    return conn


# --- pool A (fixture) -------------------------------------------------------

def pool_a(fixture: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Concept candidates and formula entries of the English slice. Every
    element there was read from its own source (``element_read=True``)."""
    elements = fixture["elements"]
    concepts = []
    for c in fixture["concepts"]:
        stratum = ("en_slice", f"{c['book']}/{c['chapter']}")
        concepts.append({
            "pool": "A", "language": "en", "stratum": stratum, "name": c["name"],
            "hits": c["hits"],
            "texts": {eid: elements[eid] for eid in c["evidence"]},
        })
    formulas = []
    for f in fixture["formulas"]:
        segments = slice_segments(f, elements, max_chars=fixture["params"]["max_chars"])
        formulas.append({
            "pool": "A", "language": "en", "ref": f"A:{f['book']}/{f['chapter']}:{f['element_id']}:{f['name']}",
            "name": f["name"], "segments": segments, "reply": f.get("reply"),
            "reply_error": f.get("error"),
        })
    return concepts, formulas


# --- pools B / C (local database, read-only) -------------------------------

_CJK_CLASS = r"[^㐀-䶿一-鿿豈-﫿]"


def load_local_pools(conn) -> tuple[list[dict], list[dict]]:
    """Concept candidates (alias hits of every rule) and formula refs from the
    local database. Evidence is joined to elements of the object's OWN
    notebook (B-11): a foreign element keeps only its stored quote and its
    locator is cleared; a missing element keeps its quote. Either way the item
    is ``element_read=False`` and R2-R4 skip it."""
    languages = _source_languages(conn)
    rows = conn.execute(
        "SELECT id, notebook_id, object_type, payload->>'name' AS name, evidence "
        "FROM knowledge_objects WHERE object_type IN ('concept', 'formula') "
        "AND status <> 'deprecated' AND id NOT LIKE %s ORDER BY id",
        (kg_attrs.KNOWHOW_OBJECT_ID_PREFIX + "%",),
    ).fetchall()
    concepts: list[dict] = []
    formulas: list[dict] = []
    for start in range(0, len(rows), 2000):
        chunk = rows[start:start + 2000]
        element_ids = {
            e.get("element_id") for r in chunk for e in (r["evidence"] or [])
            if isinstance(e, dict) and e.get("element_id")
        }
        found = _elements(conn, element_ids)
        for row in chunk:
            items = _evidence_items(row, found)
            readable = [i for i in items if i["element_read"]]
            source_id = found[readable[0]["element_id"]]["source_id"] if readable else None
            language = languages.get(source_id) if source_id else None
            if row["object_type"] == "concept" and row["name"]:
                if language is None:
                    continue
                hits = kg_attrs.trace_concept_aliases(
                    row["name"], items, exclude_names=(), rules=kg_attrs.ALIAS_RULES)
                concepts.append({
                    "pool": "B" if language == "en" else "C", "language": language,
                    "stratum": (row["notebook_id"], source_id), "name": row["name"],
                    "object_id": row["id"], "hits": hits,
                    "texts": {i["element_id"]: i["element_text"] for i in items
                              if i["element_read"] and any(h.get("element_id") == i["element_id"] for h in hits)},
                })
            elif row["object_type"] == "formula" and row["name"] and readable:
                first = found[readable[0]["element_id"]]
                formulas.append({
                    "pool": "B" if language == "en" else "C", "language": language,
                    "ref": f"{row['notebook_id']}:{row['id']}", "name": row["name"],
                    "first": (first["id"], first["text"]), "source_id": first["source_id"],
                    "ordinal": first["ordinal"],
                })
    return concepts, formulas


def _source_languages(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT s.id, "
        "COALESCE(SUM(length(regexp_replace(se.text, %s, '', 'g'))), 0) AS cjk, "
        "COALESCE(SUM(length(regexp_replace(se.text, '\\s', '', 'g'))), 0) AS visible "
        "FROM sources s JOIN source_elements se ON se.source_id = s.id "
        "WHERE s.memory_id IS NULL GROUP BY s.id",
        (_CJK_CLASS,),
    ).fetchall()
    return {
        r["id"]: "zh" if cjk_share(int(r["cjk"]), int(r["visible"])) >= CJK_SHARE_ZH else "en"
        for r in rows
    }


def _elements(conn, element_ids: Iterable[str]) -> dict[str, dict]:
    ids = sorted(element_ids)
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 5000):
        for row in conn.execute(
            "SELECT se.id, se.source_id, se.text, se.ordinal, s.notebook_id "
            "FROM source_elements se JOIN sources s ON s.id = se.source_id "
            "WHERE se.id = ANY(%s)",
            (ids[start:start + 5000],),
        ).fetchall():
            found[row["id"]] = row
    return found


def _evidence_items(row: Mapping[str, Any], found: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    items = []
    for e in row["evidence"] or []:
        if not isinstance(e, dict):
            continue
        element_id = e.get("element_id") or ""
        element = found.get(element_id)
        if element is not None and element["notebook_id"] == row["notebook_id"]:
            items.append({"element_id": element_id, "element_text": element["text"], "element_read": True})
        else:
            foreign = element is not None
            items.append({"element_id": "" if foreign else element_id,
                          "element_text": e.get("quoted_span", ""), "element_read": False})
    return items


def next_element(conn, source_id: str, ordinal: int) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT n.id, n.text FROM source_elements n WHERE n.source_id = %s "
        "AND n.ordinal > %s AND n.ordinal <= %s ORDER BY n.ordinal LIMIT 1",
        (source_id, ordinal, ordinal + FORMULA_NEXT_ELEMENT_ORDINAL_WINDOW),
    ).fetchone()
    return (row["id"], row["text"]) if row else None


# --- alias sampling ---------------------------------------------------------

def alias_candidates(concepts: Sequence[Mapping[str, Any]], *, egress: bool) -> dict[str, list[dict]]:
    """Per rule: one candidate per (concept, alias) hit, with the text window
    the reviewer sees. Without egress, local pools still derive aliases (pure
    local rules) but are never pre-labeled."""
    out: dict[str, list[dict]] = {rule: [] for rule in kg_attrs.ALIAS_RULES}
    for concept in concepts:
        for hit in concept["hits"]:
            element_id = hit.get("element_id")
            text = concept["texts"].get(element_id, "") if element_id else ""
            segments = [[element_id or "", _window_around(text, hit["text"]) if text else concept["name"]]]
            out[hit["rule"]].append({
                "key": align_key(text or concept["name"], concept["name"]) + "|" + hit["rule"] + "|" + hit["text"],
                "rule": hit["rule"], "pool": concept["pool"], "language": concept["language"],
                "stratum": list(concept["stratum"]), "name": concept["name"], "alias": hit["text"],
                "element_id": element_id or "", "segments": segments,
                "prelabel_allowed": concept["pool"] == "A" or egress,
            })
    return out


def _window_around(text: str, needle: str, radius: int = 300) -> str:
    at = text.find(needle)
    if at < 0 or len(text) <= 2 * radius + len(needle):
        return text[: 2 * radius + len(needle)]
    lo = max(0, at - radius)
    return text[lo: at + len(needle) + radius]


def sample_aliases(candidates: Mapping[str, Sequence[dict]], *, per_rule: int, seed: int,
                   existing: Sequence[dict] = ()) -> list[dict]:
    """``per_rule`` per rule, stratified by (notebook, source); a top-up keeps
    ``existing`` items and draws only the shortfall from the rest."""
    out = list(existing)
    have = Counter(item["rule"] for item in existing)
    taken = {item["key"] for item in existing}
    for rule in kg_attrs.ALIAS_RULES:
        remaining = [c for c in candidates.get(rule, []) if c["key"] not in taken]
        out += stratified_sample(remaining, max(0, per_rule - have[rule]),
                                 stratum=lambda c: tuple(c["stratum"]), seed=seed)
    return out


# --- formula scanning -------------------------------------------------------

def scan_formulas(
    formulas: Sequence[dict],
    *,
    product: Callable[[dict], tuple[str | None, str | None]],
    draft: Callable[[dict], list[dict] | None],
    pair_window: int,
    max_formulas: int,
    target_pooled: int = FORMULA_SCAN_TARGET_POOLED,
    target_kept_pairs: int = FORMULA_SCAN_TARGET_KEPT_PAIRS,
    start: int = 0,
    state: dict | None = None,
) -> dict:
    """Scan ``formulas`` (already in seeded random order) from ``start`` until
    ``target_pooled`` formulas have a non-empty pool AND the product kept
    ``target_kept_pairs`` pairs, or ``max_formulas`` are scanned, or the list
    ends. ``product`` returns (raw reply, error); ``draft`` returns extra
    candidate pairs (only asked when the pool is non-empty or a hint word is
    present). Returns the cards, the hint-but-empty formulas, counters and the
    resume position."""
    state = state or {"cards": [], "hint_empty": [], "status": Counter(), "scanned": 0,
                      "kept_pairs": 0, "pooled": 0}
    position = start
    while position < len(formulas) and state["scanned"] < max_formulas:
        if state["pooled"] >= target_pooled and state["kept_pairs"] >= target_kept_pairs:
            break
        formula = formulas[position]
        position += 1
        state["scanned"] += 1
        reply, error = product(formula)
        record = verification_record(formula["name"], formula["segments"], reply, pair_window=pair_window)
        state["status"][record["status"]] += 1
        pairs = [{"symbol": k["symbol"], "meaning": k["meaning"], "origin": "kept"} for k in record["kept"]]
        pairs += [{"symbol": r["symbol"], "meaning": r["meaning"], "origin": "rejected", "reason": r["reason"]}
                  for r in record["rejected"]]
        hint = has_hint_word(formula["segments"])
        if pairs or hint:
            for extra in draft(formula) or []:
                pairs.append({"symbol": extra["symbol"], "meaning": extra["meaning"], "origin": "draft"})
        pairs = _dedup_pool(formula["segments"], pairs)
        if pairs:
            state["pooled"] += 1
            state["kept_pairs"] += sum(p["origin"] == "kept" for p in pairs)
            state["cards"].append({
                "key": card_key(formula["segments"], formula["name"]),
                "language": formula["language"], "pool": formula["pool"], "ref": formula["ref"],
                "formula": formula["name"], "segments": formula["segments"], "pairs": pairs,
                "status": record["status"], "error": error,
            })
        elif hint:
            state["hint_empty"].append({
                "key": card_key(formula["segments"], formula["name"]) + "|empty",
                "language": formula["language"], "pool": formula["pool"], "ref": formula["ref"],
                "formula": formula["name"], "segments": formula["segments"],
            })
    state["position"] = position
    return state


def _dedup_pool(segments, pairs: list[dict]) -> list[dict]:
    """One pool entry per candidate (product kept/rejected wins over draft)."""
    seen: dict[str, dict] = {}
    for pair in pairs:
        group = pair_group_key(segments, pair["symbol"], pair["meaning"])
        if group not in seen:
            seen[group] = {**pair, "group": group}
    return list(seen.values())


def parse_draft_reply(raw: str | None) -> list[dict]:
    try:
        payload = json.loads(raw or "")
    except ValueError:
        return []
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        return []
    return [
        {"symbol": p["symbol"].strip(), "meaning": p["meaning"].strip()} for p in pairs
        if isinstance(p, dict) and isinstance(p.get("symbol"), str) and isinstance(p.get("meaning"), str)
        and p["symbol"].strip() and p["meaning"].strip()
    ]


def parse_verdict(raw: str | None) -> dict | None:
    try:
        payload = json.loads(raw or "")
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("verdict") not in VERDICTS:
        return None
    return {"verdict": payload["verdict"], "reason": str(payload.get("reason") or "")[:200]}


def parse_judgements(raw: str | None, size: int) -> list[dict | None]:
    out: list[dict | None] = [None] * size
    try:
        payload = json.loads(raw or "")
    except ValueError:
        return out
    for item in (payload.get("judgements") if isinstance(payload, dict) else None) or []:
        if isinstance(item, dict) and type(item.get("i")) is int and 0 <= item["i"] < size \
                and item.get("verdict") in VERDICTS:
            out[item["i"]] = {"verdict": item["verdict"], "reason": str(item.get("reason") or "")[:200]}
    return out


# --- judge prompts (calibration only) --------------------------------------

def draft_messages(formula: str, segments: Sequence[Sequence[str]]) -> list[dict]:
    context = "\n\n".join(t for _eid, t in segments)
    return [{"role": "user", "content": (
        f"Formula (LaTeX):\n{formula}\n\nSource text:\n<<<\n{context}\n>>>\n\n"
        "List EVERY symbol of the formula whose meaning this source text states, each with "
        "the meaning copied verbatim from the text (original language, no translation, no "
        "paraphrase). Include only meanings the text states. Return JSON only."
    )}]


def alias_prelabel_messages(item: Mapping[str, Any]) -> list[dict]:
    context = "\n\n".join(t for _eid, t in item["segments"])
    return [{"role": "user", "content": (
        f"Concept name: {item['name']}\nCandidate alias: {item['alias']}\n\n"
        f"Source text:\n<<<\n{context}\n>>>\n\n"
        "Is the candidate another name (abbreviation, acronym or expansion) for exactly this "
        "concept, according to the source text? verdict: correct, incorrect, or unsure; "
        "reason: one short sentence. Return JSON only."
    )}]


def pair_prelabel_messages(card: Mapping[str, Any]) -> list[dict]:
    context = "\n\n".join(t for _eid, t in card["segments"])
    listing = "\n".join(f"{i}. {p['symbol']} = {p['meaning']}" for i, p in enumerate(card["pairs"]))
    return [{"role": "user", "content": (
        f"Formula (LaTeX):\n{card['formula']}\n\nSource text:\n<<<\n{context}\n>>>\n\n"
        f"Candidate symbol meanings (index. symbol = meaning):\n{listing}\n\n"
        "For each index, judge whether the source text states that this symbol of the formula "
        "means exactly that. verdict: correct, incorrect, or unsure; reason: one short "
        "sentence. Return JSON only."
    )}]


# --- budget table -----------------------------------------------------------

def budget_table(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Sum planned calls into "endpoint host x pool x call kind" rows."""
    table: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row["host"], row["pool"], row["kind"])
        entry = table.setdefault(key, {"host": row["host"], "pool": row["pool"], "kind": row["kind"],
                                       "calls": 0, "chars": 0,
                                       "needs_egress": row["pool"] in LOCAL_POOLS})
        entry["calls"] += 1
        entry["chars"] += row["chars"]
    return [table[k] for k in sorted(table)]


def render_budget_table(table: Sequence[Mapping[str, Any]]) -> str:
    lines = ["| 端点主机 | 池 | 调用类别 | 调用数(上限) | 输入字符数(上限) | 需要出站开关 |",
             "|---|---|---|---|---|---|"]
    for row in table:
        lines.append(f"| {row['host']} | {row['pool']} | {CALL_KIND_LABELS[row['kind']]} | "
                     f"{row['calls']} | {row['chars']} | {'是' if row['needs_egress'] else '否'} |")
    lines.append(f"| 合计 | | | {sum(r['calls'] for r in table)} | {sum(r['chars'] for r in table)} | |")
    return "\n".join(lines)


def plan_calls(
    alias_items: Sequence[dict], scan_lists: Mapping[str, Sequence[dict]], *,
    formula_host: str, judge_host: str, max_formulas: int,
) -> list[dict]:
    """Upper-bound call plan for the dry run (before any reply is known):
    every formula among the first ``max_formulas`` of each language's scan
    order gets a product call (pool A replies come from the fixture) and a
    draft call; at most ``MAX_PAIR_PRELABEL_CARDS`` cards get a pair
    pre-label; every sampled alias gets one pre-label."""
    rows = []
    for item in alias_items:
        rows.append({"host": judge_host, "pool": item["pool"], "kind": "alias_prelabel",
                     "chars": messages_chars(alias_prelabel_messages(item)) + len(ALIAS_PRELABEL_HINT)})
    card_budget = MAX_PAIR_PRELABEL_CARDS
    for language in LANGUAGES:
        for formula in list(scan_lists.get(language, []))[:max_formulas]:
            segments = formula["segments"]
            if formula["pool"] != "A":
                rows.append({"host": formula_host, "pool": formula["pool"], "kind": "formula",
                             "chars": messages_chars(kg_attrs.formula_variables_messages(
                                 formula["name"], [tuple(s) for s in segments]))})
            rows.append({"host": judge_host, "pool": formula["pool"], "kind": "draft",
                         "chars": messages_chars(draft_messages(formula["name"], segments)) + len(DRAFT_HINT)})
            if card_budget > 0:
                card_budget -= 1
                fake = {"formula": formula["name"], "segments": segments,
                        "pairs": [{"symbol": "x", "meaning": "m" * 20}] * ESTIMATED_PAIRS_PER_CARD}
                rows.append({"host": judge_host, "pool": formula["pool"], "kind": "pair_prelabel",
                             "chars": messages_chars(pair_prelabel_messages(fake)) + len(PAIR_PRELABEL_HINT)})
    return rows


# --- prepare orchestration --------------------------------------------------

def _judge_client():
    from app.services.kg.client import make_client

    client = make_client("EVAL_JUDGE_")
    return client


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url or "").hostname or f"(未配置 {_JUDGE_ENV_PREFIX}OPENAI_COMPAT_BASE_URL)"


def _materialize(formula: dict, conn, max_chars: int) -> dict:
    """Pool B/C formula -> context segments (first element + next within 64)."""
    if "segments" in formula:
        return formula
    following = []
    nxt = next_element(conn, formula["source_id"], formula["ordinal"])
    if nxt is not None:
        following.append(nxt)
    segments = kg_attrs.build_formula_context(formula["first"], following, max_chars=max_chars)
    return {**formula, "segments": [list(s) for s in segments]}


def run_prepare(args: Any) -> int:
    import datetime

    root = main_checkout_root()
    _tool_environment(root)
    check_local_database_url(args.database_url)
    out_dir = local_output_dir(root)
    fixture = _read_json(Path(args.fixture))
    if fixture.get("rules_signature") != kg_attrs.rules_signature():
        raise SystemExit("en_slice.json is stale for the current rules: regenerate it with extract-en first")
    settings, provider = _open_models()
    conn = read_only_connection(args.database_url)
    try:
        max_chars, pair_window = settings.kg_attrs_context_chars, settings.kg_attrs_pair_window
        workload = formula_workload(provider.registry, args.formula_service)
        formula_service = provider.registry.service_for(workload)
        judge = _judge_client()
        formula_host = _service_record(formula_service)["host"]
        judge_host = _host_of(judge.base_url)
        a_concepts, a_formulas = pool_a(fixture)
        local_concepts, local_formulas = load_local_pools(conn)
        concepts = a_concepts + local_concepts
        candidates = alias_candidates(concepts, egress=args.allow_local_content_egress)
        previous = _read_json(out_dir / "items.json") if args.top_up and (out_dir / "items.json").is_file() else None
        alias_items = sample_aliases(candidates, per_rule=args.per_rule, seed=args.seed,
                                     existing=(previous or {}).get("alias_items", []))
        scan_lists = _scan_lists(a_formulas, local_formulas, seed=args.seed,
                                 egress=args.allow_local_content_egress)
        planned_lists = {
            lang: [_materialize(f, conn, max_chars) for f in formulas[: args.max_formulas_per_language]]
            for lang, formulas in scan_lists.items()
        }
        planned_aliases = [a for a in alias_items if a["prelabel_allowed"]]
        table = budget_table(plan_calls(planned_aliases, planned_lists, formula_host=formula_host,
                                        judge_host=judge_host, max_formulas=args.max_formulas_per_language))
        print(render_budget_table(table))
        print(f"[prepare] concepts A/B/C = {Counter(c['pool'] for c in concepts)}; "
              f"formulas en/zh = {Counter(f['language'] for f in a_formulas + local_formulas)}; "
              f"egress={'on' if args.allow_local_content_egress else 'off'}")
        _write_json(out_dir / "prepare-budget.json", {"table": table, "egress": args.allow_local_content_egress,
                                                      "generated": datetime.datetime.now().isoformat()})
        if args.dry_run:
            return 0
        if not args.confirm_budget:
            raise SystemExit("re-run with --confirm-budget after the budget above is approved")
        if not judge.configured:
            raise SystemExit(f"{_JUDGE_ENV_PREFIX}OPENAI_COMPAT_* is not configured: drafts and pre-labels need it")
        print(f"[prepare] sending to: formula {formula_host}; judge {judge_host}")
        items = _execute_prepare(
            args, fixture, provider.chat(workload), judge, planned_lists, alias_items,
            concepts=concepts, previous=previous, max_chars=max_chars, pair_window=pair_window,
            hosts={"formula": formula_host, "judge": judge_host},
        )
        items["models"] = {"extract": fixture.get("models", {}).get("extract"),
                           "formula": {**_service_record(formula_service), "workload": workload,
                                       "fingerprint_full": formula_service.fingerprint},
                           "judge": judge.model or None}
        _write_json(out_dir / "items.json", items)
        print(f"[prepare] wrote {out_dir / 'items.json'}")
        return 0
    finally:
        try:
            conn.execute("ROLLBACK")
        finally:
            conn.close()
            provider.close()


def _scan_lists(a_formulas, local_formulas, *, seed: int, egress: bool) -> dict[str, list[dict]]:
    """Seeded random scan order per language. Without egress the local pools
    are not scanned at all (no call may carry their text)."""
    lists: dict[str, list[dict]] = {"en": list(a_formulas), "zh": []}
    if egress:
        lists["en"] += [f for f in local_formulas if f["language"] == "en"]
        lists["zh"] += [f for f in local_formulas if f["language"] == "zh"]
    for language, formulas in lists.items():
        formulas.sort(key=lambda f: f["ref"])
        random.Random(f"{seed}:{language}").shuffle(formulas)
    return lists


def _execute_prepare(args, fixture, product_client, judge, planned_lists, alias_items, *,
                     concepts, previous, max_chars, pair_window, hosts) -> dict:
    import datetime

    budget = Budget(args.max_calls, args.max_input_chars)
    egress_log: Counter = Counter()
    egress = args.allow_local_content_egress

    def send(kind: str, pool: str, host_key: str, messages, call) -> Any:
        if pool in LOCAL_POOLS and not egress:
            raise AssertionError("local pool text would leave the machine without the egress switch")
        chars = messages_chars(messages)
        budget.admit(chars)
        result = call()
        budget.charge(chars, cache_hit=False)
        egress_log[f"{hosts[host_key]}|{pool}|{kind}"] += chars
        return result

    def product(formula: dict) -> tuple[str | None, str | None]:
        if formula["pool"] == "A":
            return formula.get("reply"), formula.get("reply_error")
        before = budget.chars
        reply, error = call_formula(product_client, formula["name"], formula["segments"], budget)
        egress_log[f"{hosts['formula']}|{formula['pool']}|formula"] += budget.chars - before
        return reply, error

    def draft(formula: dict) -> list[dict] | None:
        if not judge.configured:
            return None
        messages = draft_messages(formula["name"], formula["segments"])
        return parse_draft_reply(send("draft", formula["pool"], "judge", messages,
                                      lambda: judge.chat_json(messages, DRAFT_HINT)))

    scan_state = (previous or {}).get("scan_state", {})
    scans: dict[str, dict] = {}
    for language in LANGUAGES:
        formulas = planned_lists.get(language, [])
        prior = scan_state.get(language)
        state = None
        if prior:
            state = {"cards": [c for c in previous["formula_cards"] if c["language"] == language],
                     "hint_empty": prior["hint_empty"], "status": Counter(prior["status"]),
                     "scanned": prior["scanned"], "kept_pairs": prior["kept_pairs"], "pooled": prior["pooled"]}
        boost = args.top_up_formulas if args.top_up else 0
        scans[language] = scan_formulas(
            formulas, product=product, draft=draft, pair_window=pair_window,
            max_formulas=args.max_formulas_per_language,
            target_pooled=FORMULA_SCAN_TARGET_POOLED + boost,
            target_kept_pairs=FORMULA_SCAN_TARGET_KEPT_PAIRS + boost,
            start=prior["position"] if prior else 0, state=state,
        )
    cards = [c for lang in LANGUAGES for c in scans[lang]["cards"]]
    blind = blind_keys([c["key"] for c in cards], seed=args.seed) | blind_keys(
        [a["key"] for a in alias_items], seed=args.seed)
    for card in cards:
        card["blind"] = card["key"] in blind
    for item in alias_items:
        item["blind"] = item["key"] in blind
    if judge.configured:
        for item in alias_items:
            if item.get("prelabel") is None and item["prelabel_allowed"]:
                messages = alias_prelabel_messages(item)
                item["prelabel"] = parse_verdict(send(
                    "alias_prelabel", item["pool"], "judge", messages,
                    lambda m=messages: judge.chat_json(m, ALIAS_PRELABEL_HINT)))
        labelled = 0
        for card in cards:
            if labelled >= MAX_PAIR_PRELABEL_CARDS:
                break
            if any("prelabel" in p for p in card["pairs"]):
                labelled += 1
                continue
            messages = pair_prelabel_messages(card)
            judgements = parse_judgements(send(
                "pair_prelabel", card["pool"], "judge", messages,
                lambda m=messages: judge.chat_json(m, PAIR_PRELABEL_HINT)), len(card["pairs"]))
            for pair, verdict in zip(card["pairs"], judgements):
                pair["prelabel"] = verdict
            labelled += 1
    empty_checks = []
    for language in LANGUAGES:
        pool = scans[language]["hint_empty"]
        previous_checks = [c for c in (previous or {}).get("empty_checks", []) if c["language"] == language]
        chosen_keys = {c["key"] for c in previous_checks}
        fresh = [c for c in pool if c["key"] not in chosen_keys]
        random.Random(f"{args.seed}:empty:{language}").shuffle(fresh)
        empty_checks += previous_checks + fresh[: max(0, EMPTY_POOL_SPOT_CHECKS - len(previous_checks))]
    formula_scan = {}
    for language in LANGUAGES:
        scan = scans[language]
        formula_scan[language] = {
            "scanned": scan["scanned"], "pooled": scan["pooled"], "kept_pairs": scan["kept_pairs"],
            "hint_empty": len(scan["hint_empty"]), "status": dict(scan["status"]),
            "untested": language == "zh" and not egress,
        }
    return {
        "run": hashlib.sha256(f"{args.seed}:{kg_attrs.rules_signature()}".encode()).hexdigest()[:12],
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "rules_signature": kg_attrs.rules_signature(),
        "fixture_sha": _sha(json.dumps(fixture, sort_keys=True, ensure_ascii=False)),
        "params": {"max_chars": max_chars, "pair_window": pair_window,
                   "window_n": fixture["params"]["window_n"], "window_m": fixture["params"]["window_m"]},
        "egress": egress,
        "alias_items": alias_items,
        "formula_cards": cards,
        "empty_checks": empty_checks,
        "formula_scan": formula_scan,
        "scan_state": {lang: {"position": scans[lang]["position"], "scanned": scans[lang]["scanned"],
                              "pooled": scans[lang]["pooled"], "kept_pairs": scans[lang]["kept_pairs"],
                              "status": dict(scans[lang]["status"]),
                              "hint_empty": scans[lang]["hint_empty"]} for lang in LANGUAGES},
        "yield": alias_yield(concepts) | {"formula_status": {lang: dict(scans[lang]["status"]) for lang in LANGUAGES}},
        "egress_log": {"sent_chars": dict(egress_log), "calls": budget.calls, "chars": budget.chars,
                       "cache_hits": budget.cache_hits},
        "alias_recall_reference": alias_recall_reference(concepts),
    }


def alias_yield(concepts: Sequence[Mapping[str, Any]]) -> dict:
    """Aliases per 100 concepts, by rule and pool (spec §2.13 comparison)."""
    totals = Counter(c["pool"] for c in concepts)
    hits = Counter((h["rule"], c["pool"]) for c in concepts for h in c["hits"])
    return {"concepts": dict(totals), "aliases_per_100_concepts": {
        f"{rule}/{pool}": round(100 * hits[(rule, pool)] / totals[pool], 3)
        for rule in kg_attrs.ALIAS_RULES for pool in POOLS if totals[pool]
    }}


def alias_recall_reference(concepts: Sequence[Mapping[str, Any]], repo_root: Path | None = None) -> dict:
    """Report-only reference (plan §2.5): abbreviation-shaped aliases in the
    test-case gold ``canonicalization.aliases`` and how many any pool-A rule hit."""
    import re

    import yaml

    repo_root = repo_root or Path(__file__).resolve().parents[3]
    shape = re.compile(r"^[A-Z][A-Za-z0-9-]{1,7}$")
    gold: set[str] = set()
    total = 0
    for path in sorted((repo_root / "fangan" / "testcases").glob("*/*/gold.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in doc.get("canonicalization") or []:
            for alias in entry.get("aliases") or []:
                total += 1
                if isinstance(alias, str) and shape.match(alias.strip()):
                    gold.add(kg_attrs.norm_concept_name(alias))
    by_rule = {rule: set() for rule in kg_attrs.ALIAS_RULES}
    for concept in concepts:
        if concept["pool"] != "A":
            continue
        for hit in concept["hits"]:
            by_rule[hit["rule"]].add(kg_attrs.norm_concept_name(hit["text"]))
    return {"gold_aliases": total, "abbreviation_shaped": len(gold),
            "hit_by_rule": {rule: len(gold & found) for rule, found in by_rule.items()},
            "hit_any_rule": len(gold & set().union(*by_rule.values()))}


# ===========================================================================
# render / ingest / report (plan G0-T5, §2.4-§2.6)
# ===========================================================================

MAX_ERROR_EXAMPLES = 5


def write_review(out_dir: Path) -> Path:
    items = _read_json(out_dir / "items.json")
    page = render_review_html(review_page_data(items, seed=items["seed"]))
    target = out_dir / "review.html"
    target.write_text(page, encoding="utf-8")
    return target


def item_keys(items: Mapping[str, Any]) -> set[str]:
    """Every key a label may carry: alias items, formula cards (missing
    count), candidate-pair groups, empty-pool spot checks."""
    keys = {a["key"] for a in items.get("alias_items", [])}
    for card in items.get("formula_cards", []):
        keys.add(card["key"])
        keys |= {p["group"] for p in card["pairs"]}
    keys |= {c["key"] for c in items.get("empty_checks", [])}
    return keys


def ingest_labels(out_dir: Path, labels_path: Path) -> tuple[int, int]:
    """Merge an exported label file into ``labels.json``; returns (merged,
    ignored-unknown). Labels for keys this run does not have are dropped."""
    items = _read_json(out_dir / "items.json")
    incoming = parse_labels(_read_json(labels_path))
    known = item_keys(items)
    accepted = {k: v for k, v in incoming.items() if k in known}
    store = out_dir / "labels.json"
    old = _read_json(store) if store.is_file() else {}
    _write_json(store, merge_labels(old, accepted))
    return len(accepted), len(incoming) - len(accepted)


def error_examples(items: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """English-slice (pool A) error examples for the local report only, at
    most ``MAX_ERROR_EXAMPLES`` per class."""
    buckets: dict[str, list[str]] = {}
    for alias in items.get("alias_items", []):
        verdict = labels.get(alias["key"], {}).get("verdict")
        if alias["pool"] == "A" and verdict in ("incorrect", "unsure"):
            buckets.setdefault(f"别名 {alias['rule']} 判错", []).append(
                f"{alias['name']} → {alias['alias']}（{verdict}）")
    for card in items.get("formula_cards", []):
        if card["pool"] != "A":
            continue
        for pair in card["pairs"]:
            verdict = labels.get(pair["group"], {}).get("verdict")
            if pair["origin"] == "kept" and verdict in ("incorrect", "unsure"):
                buckets.setdefault("公式保留但判错", []).append(
                    f"{card['formula'][:80]} | {pair['symbol']} = {pair['meaning']}（{verdict}）")
            elif pair["origin"] == "rejected" and verdict == "correct":
                buckets.setdefault(f"公式误杀（{pair['reason']}）", []).append(
                    f"{card['formula'][:80]} | {pair['symbol']} = {pair['meaning']}")
    return [f"{kind}：{text}" for kind, rows in sorted(buckets.items()) for text in rows[:MAX_ERROR_EXAMPLES]]


def write_report(out_dir: Path) -> dict:
    items = _read_json(out_dir / "items.json")
    store = out_dir / "labels.json"
    labels = _read_json(store) if store.is_file() else {}
    report = build_report(items, labels)
    _write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(
        render_report_md(report, local_examples=error_examples(items, labels)), encoding="utf-8")
    return report


def run_render(_args: Any) -> int:
    target = write_review(local_output_dir())
    print(f"[render] {target} — 用浏览器以 file:// 打开；进度存在浏览器本机")
    return 0


def run_ingest(args: Any) -> int:
    labels_path = Path(args.labels).expanduser()
    merged, ignored = ingest_labels(local_output_dir(), labels_path)
    print(f"[ingest] merged {merged} labels, ignored {ignored} unknown keys")
    print(f"[ingest] 已读入，可以删除 {labels_path}")
    return 0


def run_report(_args: Any) -> int:
    out_dir = local_output_dir()
    report = write_report(out_dir)
    print(f"[report] {out_dir / 'report.md'}")
    print(f"[report] 别名通过：{report['alias_rules_passed'] or '无'}；"
          f"公式逐语言：{report['formula_overall']}；公式上线：{report['formula_ships']}")
    if report["top_up_needed"]:
        print(f"[report] 样本不足，需要补抽（prepare --top-up）：{report['top_up_needed']}")
    return 0


# ===========================================================================
# CLI
# ===========================================================================

def build_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="kg_attrs_calibration", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract-en", help="build en_slice.json (pool A) with the product chain")
    extract.add_argument("--dry-run", action="store_true", help="print the call/char estimate only")
    extract.add_argument("--out", default=str(EN_SLICE_PATH))
    extract.add_argument("--formula-service", default="general")
    _budget_args(extract)
    extract.set_defaults(run=run_extract_en)

    prepare = sub.add_parser("prepare", help="sample pools A/B/C, call models, write items.json")
    prepare.add_argument("--database-url", required=True, help="local PostgreSQL, opened read-only")
    prepare.add_argument("--fixture", default=str(EN_SLICE_PATH))
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument("--per-rule", type=int, default=DEFAULT_PER_RULE)
    prepare.add_argument("--max-formulas-per-language", type=int, default=DEFAULT_MAX_FORMULAS_PER_LANGUAGE)
    prepare.add_argument("--formula-service", default="general")
    prepare.add_argument("--dry-run", action="store_true", help="print the host x pool x call-kind budget only")
    prepare.add_argument("--confirm-budget", action="store_true", help="actually send (after the budget is approved)")
    prepare.add_argument("--allow-local-content-egress", action="store_true",
                         help="let pool B/C (local library) text reach the configured endpoints")
    prepare.add_argument("--top-up", action="store_true", help="keep items.json and draw only the shortfall")
    prepare.add_argument("--top-up-formulas", type=int, default=20,
                         help="with --top-up: raise both formula scan targets by this much")
    _budget_args(prepare)
    prepare.set_defaults(run=run_prepare)

    sub.add_parser("render", help="write review.html from items.json").set_defaults(run=run_render)
    ingest = sub.add_parser("ingest", help="merge an exported kg_attrs_labels.json")
    ingest.add_argument("--labels", required=True)
    ingest.set_defaults(run=run_ingest)
    sub.add_parser("report", help="write report.md / report.json").set_defaults(run=run_report)
    return parser


def _budget_args(parser: Any) -> None:
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
