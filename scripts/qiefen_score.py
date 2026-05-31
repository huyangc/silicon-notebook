#!/usr/bin/env python3
"""Run the qiefen pipeline over every gold chapter, write pred.yaml into a
candidate tree mirroring the gold layout, then run the testcase harness.

Usage:
  PYTHONPATH=backend python scripts/qiefen_score.py --out /tmp/qiefen_pred
"""
import argparse
import os
import pathlib
import subprocess
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
GOLD = REPO / "fangan" / "testcases"
sys.path.insert(0, str(REPO / "backend"))

from app.services.qiefen.pipeline import run  # noqa: E402
from app.services.qiefen.emit import to_yaml  # noqa: E402

SOURCE_ROOT = pathlib.Path(
    os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser")
)
SOURCE_PATHS = {
    "engram_paper_mineru.md": SOURCE_ROOT / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SOURCE_ROOT
    / "notebook_papers_mineru_skill_results"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "harness_out" / "qiefen_pred"))
    args = ap.parse_args()
    out_root = pathlib.Path(args.out)

    for gp in sorted(GOLD.glob("*/ch*/gold.yaml")):
        chapter_dir = gp.parent
        rel = chapter_dir.relative_to(GOLD)
        meta = yaml.safe_load(gp.read_text(encoding="utf-8"))["source_meta"]
        src_path = SOURCE_PATHS.get(meta["source_file"])
        if not src_path or not src_path.exists():
            print(f"skip {rel}: source missing")
            continue
        src = src_path.read_text(encoding="utf-8")
        doc = run(src, source_file=meta["source_file"], profile=meta["profile"],
                  line_range=meta.get("source_line_range"),
                  source_id=meta.get("source_id", ""), title=meta.get("title", ""),
                  scope=meta.get("scope", ""))
        dst = out_root / rel
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "pred.yaml").write_text(to_yaml(doc), encoding="utf-8")
        print(f"wrote {rel}/pred.yaml ({len(doc.evidence_atoms)} atoms)")

    # Run the harness.
    cmd = [sys.executable, "-m", "harness.run_all", "--gold-root", ".",
           "--pred-root", str(out_root), "--out-dir", str(out_root / "_report")]
    print("running harness:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(GOLD), check=False)
    print(f"leaderboard: {out_root / '_report' / 'leaderboard.md'}")


if __name__ == "__main__":
    main()
