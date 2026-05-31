"""Score a candidate tree against all gold chapters; emit aggregate + leaderboard.

A candidate tree mirrors the gold layout: <pred_root>/<doc>/<chapter>/pred.yaml
(when pred_root IS the gold tree, gold.yaml is used as the candidate too).
"""
import argparse
import glob
import os

import yaml

from . import scorer, report


def _candidate_path(pred_root, doc, chapter):
    for name in ("pred.yaml", "gold.yaml"):
        cand = os.path.join(pred_root, doc, chapter, name)
        if os.path.exists(cand):
            return cand
    return None


def run(gold_root, pred_root, out_dir, judge=None):
    golds = sorted(glob.glob(os.path.join(gold_root, "*", "ch*", "gold.yaml")))
    rows = []
    for gp in golds:
        chapter = os.path.basename(os.path.dirname(gp))
        doc = os.path.basename(os.path.dirname(os.path.dirname(gp)))
        cand = _candidate_path(pred_root, doc, chapter)
        gold = yaml.safe_load(open(gp, encoding="utf-8"))
        if cand is None:
            rows.append({"doc": doc, "chapter": chapter, "weighted_score": 0.0,
                         "missing_candidate": True, "stage_scores": {}})
            continue
        pred = yaml.safe_load(open(cand, encoding="utf-8"))
        result = scorer.score_fixture(gold, pred, judge=judge)
        rows.append({"doc": doc, "chapter": chapter,
                     "weighted_score": result["weighted_score"],
                     "stage_scores": result["stage_scores"]})

    scored = [r for r in rows if not r.get("missing_candidate")]
    mean = round(sum(r["weighted_score"] for r in scored) / len(scored), 2) if scored else 0.0
    agg = {"chapters_scored": len(scored), "chapters_total": len(rows),
           "mean_weighted_score": mean, "rows": rows}

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "aggregate.json"), "w", encoding="utf-8") as f:
        f.write(report.to_json(agg))
    with open(os.path.join(out_dir, "leaderboard.md"), "w", encoding="utf-8") as f:
        f.write(_leaderboard_md(agg))
    return agg


def _leaderboard_md(agg):
    lines = [f"# Candidate leaderboard", "",
             f"**Mean weighted score: {agg['mean_weighted_score']} / 100** "
             f"over {agg['chapters_scored']}/{agg['chapters_total']} chapters", "",
             "| doc | chapter | score | atoms | chunks | objects | relations |",
             "| --- | --- | --: | --: | --: | --: | --: |"]
    for r in sorted(agg["rows"], key=lambda x: (x["doc"], x["chapter"])):
        ss = r.get("stage_scores", {})

        def pct(k):
            return f"{100.0 * ss[k]:.0f}%" if k in ss else "-"

        flag = " (missing)" if r.get("missing_candidate") else ""
        lines.append(f"| {r['doc']} | {r['chapter']}{flag} | {r['weighted_score']} "
                     f"| {pct('evidence_atoms')} | {pct('semantic_chunks')} "
                     f"| {pct('objects')} | {pct('relations')} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score a candidate tree against all gold chapters.")
    ap.add_argument("--gold-root", default="fangan/testcases")
    ap.add_argument("--pred-root", required=True)
    ap.add_argument("--out-dir", default="harness_out")
    args = ap.parse_args(argv)
    agg = run(args.gold_root, args.pred_root, args.out_dir)
    print(f"mean {agg['mean_weighted_score']} / 100 over {agg['chapters_scored']} chapters "
          f"-> {args.out_dir}/leaderboard.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
