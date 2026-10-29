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
# CLI
# ===========================================================================

def _not_yet(name: str) -> Callable[[Any], int]:
    def run(_args: Any) -> int:
        raise SystemExit(f"{name}: not implemented in this revision")
    return run


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="kg_attrs_calibration", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("extract-en", "prepare", "render", "ingest", "report"):
        sub.add_parser(name).set_defaults(run=_not_yet(name))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
