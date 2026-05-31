#!/usr/bin/env python3
"""Cross-validated evaluation of the LLM atom selector.

Reads two scored runs (selector OFF and ON), treats a fixed validation set as
the only chapters the prompt was developed on, and reports performance on the
held-out test chapters (textbook vs article, OFF vs ON). Also reports a 10-fold
2-validation:8-test rotation to show the held-out result is stable (not overfit
to any particular split).
"""
import json
import pathlib
import random
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

# Chapters used to DEVELOP the selector prompt (their gold was inspected).
VALIDATION = {"cmos/ch02_cmos_technology", "engram/ch02_architecture"}
STAGES = ["evidence_atoms", "semantic_chunks", "objects", "object_payload",
          "object_evidence", "relations", "context_packages", "structure"]


def _load(out_dir):
    agg = json.load(open(REPO / out_dir / "_report" / "aggregate.json"))
    rows = {}
    for r in agg["rows"]:
        if r.get("missing_candidate"):
            continue
        rows[f"{r['doc']}/{r['chapter']}"] = dict(r["stage_scores"],
                                                  _total=r["weighted_score"], _doc=r["doc"])
    return rows


def _mean(rows, keys, stage):
    vals = [rows[k].get(stage, 0) for k in keys if k in rows]
    return statistics.mean(vals) if vals else 0.0


def main():
    off = _load(sys.argv[1] if len(sys.argv) > 1 else "harness_out/cv_off")
    on = _load(sys.argv[2] if len(sys.argv) > 2 else "harness_out/cv_on")
    chapters = sorted(off)
    held = [c for c in chapters if c not in VALIDATION]
    tb = [c for c in held if c.startswith("cmos/")]
    art = [c for c in held if c.startswith("engram/")]

    print("=== HELD-OUT TEST (prompt never developed on these) ===")
    print(f"validation/dev (excluded): {sorted(VALIDATION)}\n")
    hdr = f"{'group':22s}{'atoms':>14s}{'chunks':>10s}{'objects':>10s}{'TOTAL':>12s}"
    print(hdr)
    for name, ks in [("TEXTBOOK held-out (%d)" % len(tb), tb),
                     ("ARTICLE held-out (%d)" % len(art), art)]:
        for label, R in [("OFF", off), ("ON ", on)]:
            print(f"{name if label=='OFF' else '':22s}"
                  f"{label} {_mean(R,ks,'evidence_atoms'):.3f}    "
                  f"{_mean(R,ks,'semantic_chunks'):.3f}     "
                  f"{_mean(R,ks,'objects'):.3f}     "
                  f"{_mean(R,ks,'_total'):.1f}")
        print()

    print("=== 10-fold (2 validation : 8 test), selector ON, held-out test composite ===")
    rng = random.Random(20260531)
    pool = list(chapters)
    on_means, off_means = [], []
    for _ in range(10):
        test = rng.sample(pool, 8)  # 8 test chapters
        on_means.append(statistics.mean(on[c]["_total"] for c in test))
        off_means.append(statistics.mean(off[c]["_total"] for c in test))
    print(f"  OFF test-composite: {statistics.mean(off_means):.1f} "
          f"+- {statistics.pstdev(off_means):.1f}")
    print(f"  ON  test-composite: {statistics.mean(on_means):.1f} "
          f"+- {statistics.pstdev(on_means):.1f}")

    print("\n=== per held-out textbook chapter (atoms / total), OFF -> ON ===")
    for c in tb:
        print(f"  {c[:30]:30s} atoms {off[c].get('evidence_atoms',0):.2f}->"
              f"{on[c].get('evidence_atoms',0):.2f}   total "
              f"{off[c]['_total']:.1f}->{on[c]['_total']:.1f}")


if __name__ == "__main__":
    main()
