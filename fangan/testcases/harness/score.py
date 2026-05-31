"""CLI: score one chapter's pred.yaml against its gold.yaml."""
import argparse
import os
import sys

import yaml

from . import scorer, report, judge


def _load_gold(path):
    if os.path.isdir(path):
        path = os.path.join(path, "gold.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f), path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score a qiefen extraction against gold.")
    ap.add_argument("--gold", required=True, help="chapter dir or path to gold.yaml")
    ap.add_argument("--pred", required=True, help="path to candidate pred.yaml")
    ap.add_argument("--out", help="write report.json here")
    ap.add_argument("--md", help="write report.md here")
    ap.add_argument("--title", help="title shown in the markdown report")
    ap.add_argument("--llm-judge", action="store_true", help="enable LLM judge (no backend wired by default)")
    args = ap.parse_args(argv)

    gold, gold_path = _load_gold(args.gold)
    with open(args.pred, "r", encoding="utf-8") as f:
        pred = yaml.safe_load(f)

    j = judge.make_judge(enabled=args.llm_judge, backend=None)
    if args.llm_judge and j is None:
        print("warning: --llm-judge set but no backend wired; using deterministic equivalence",
              file=sys.stderr)

    result = scorer.score_fixture(gold, pred, judge=j)
    title = args.title or os.path.basename(os.path.dirname(gold_path)) or "fixture"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report.to_json(result))
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(report.to_markdown(result, title=title))

    print(f"{title}: {result['weighted_score']} / 100")
    for k, v in result["stage_scores"].items():
        print(f"  {k:18s} {100.0 * v:6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
