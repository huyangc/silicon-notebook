#!/usr/bin/env python3
"""Evaluate paired selected-source baseline/shadow observations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.eval.selected_source_graph import (  # noqa: E402
    evaluate_selected_source_rollout,
    load_golden_cases,
    observation_from_dict,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="paired observation JSON")
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    value = json.loads(args.input.read_text(encoding="utf-8"))
    cases = load_golden_cases(args.golden)
    baseline = tuple(observation_from_dict(row) for row in value["baseline"])
    shadow = tuple(observation_from_dict(row) for row in value["shadow"])
    result = evaluate_selected_source_rollout(cases, baseline, shadow)
    if not baseline:
        raise ValueError("paired observation input must not be empty")
    attestation = result.attestation(run_id=str(value["run_id"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(attestation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if result.approved:
        print(
            f"selected-source graph quality gate approved ({result.case_count} cases)"
        )
        return 0
    print("selected-source graph quality gate blocked", file=sys.stderr)
    for failure in (
        *result.hard_failures,
        *result.quality_failures,
        *result.cost_failures,
    ):
        print(f"- {failure}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
