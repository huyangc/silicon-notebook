"""Render a scorer result as JSON (machine) and Markdown (human/agent feedback)."""
import json


def to_json(result):
    return json.dumps(result, ensure_ascii=False, indent=2, default=list)


def _fmt_pct(x):
    return f"{100.0 * x:.1f}%"


def to_markdown(result, title="(fixture)"):
    lines = []
    lines.append(f"# Score report — {title}")
    lines.append("")
    lines.append(f"**Weighted score: {result['weighted_score']} / 100**  "
                 f"(profile: `{result.get('profile')}`, schema: `{result.get('schema_version')}`)")
    lines.append("")
    lines.append("## Stage scores")
    lines.append("")
    lines.append("| bucket | score |")
    lines.append("| --- | --: |")
    for k, v in result["stage_scores"].items():
        lines.append(f"| {k} | {_fmt_pct(v)} |")
    lines.append("")

    st = result["stages"]

    # Per-stage P/R/F1
    lines.append("## Precision / Recall / F1")
    lines.append("")
    lines.append("| stage | P | R | F1 | extra |")
    lines.append("| --- | --: | --: | --: | --- |")
    for name in ("evidence_atoms", "semantic_chunks", "objects", "relations"):
        s = st.get(name)
        if not s or "prf" not in s:
            continue
        pr = s["prf"]
        extra = ""
        if name == "evidence_atoms":
            extra = f"type_acc={_fmt_pct(s['type_accuracy'])}, mean_iou={s['mean_iou']}"
        elif name == "objects":
            extra = (f"type_acc={_fmt_pct(s['type_accuracy'])}, "
                     f"payload_f1={_fmt_pct(s['payload']['f1'])}, "
                     f"ev_jaccard={s['evidence']['mean_jaccard']}")
        lines.append(f"| {name} | {_fmt_pct(pr['precision'])} | {_fmt_pct(pr['recall'])} "
                     f"| {_fmt_pct(pr['f1'])} | {extra} |")
    lines.append("")

    # Actionable diffs
    def section(header, rows):
        if not rows:
            return
        lines.append(f"## {header}")
        lines.append("")
        for r in rows:
            lines.append(f"- {r}")
        lines.append("")

    for name in ("evidence_atoms", "semantic_chunks", "objects", "relations"):
        s = st.get(name)
        if not s:
            continue
        section(f"Missed in {name} (false negatives)", s.get("missed"))
        section(f"Spurious in {name} (false positives)", s.get("spurious"))
        tms = s.get("type_mismatches") or []
        section(f"Type mismatches in {name}",
                [f"{t['gold_id']} (gold `{t['gold_type']}`) vs {t['pred_id']} (pred `{t['pred_type']}`)"
                 for t in tms])

    # payload gaps
    obj = st.get("objects", {})
    gaps = (obj.get("payload") or {}).get("gaps") or []
    section("Payload-field gaps",
            [f"{g['gold_id']}: missing {g['missing_values']}" for g in gaps])

    # packages
    pkg = st.get("context_packages", {})
    section("Package object-recall misses",
            [f"{d['package']}: {d['missed_expected_objects']}" for d in (pkg.get("details") or [])])

    # do_not_extract
    dne = st.get("do_not_extract", {})
    section("do_not_extract violations (over-extraction)",
            [f"`{h['forbidden']}` ({h.get('kind')})" for h in (dne.get("hits") or [])])

    return "\n".join(lines)
