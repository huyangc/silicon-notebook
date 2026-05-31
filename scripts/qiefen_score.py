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

from app.services.qiefen.pipeline import run, default_client  # noqa: E402
from app.services.qiefen.emit import to_yaml  # noqa: E402

# Make the testcase harness importable (it lives under GOLD).
sys.path.insert(0, str(GOLD))


def _score_parallel(pred_root, judge, workers):
    """Score every gold chapter against pred_root, in parallel across chapters,
    sharing one cached judge. Writes aggregate.json + leaderboard.md like
    run_all, but parallelizes the (slow, judge-bearing) per-chapter scoring."""
    import glob
    from concurrent.futures import ThreadPoolExecutor
    from harness import scorer, report, run_all as _run_all
    golds = sorted(glob.glob(str(GOLD / "*" / "ch*" / "gold.yaml")))

    def _one(gp):
        chapter = os.path.basename(os.path.dirname(gp))
        doc = os.path.basename(os.path.dirname(os.path.dirname(gp)))
        cand = os.path.join(str(pred_root), doc, chapter, "pred.yaml")
        gold = yaml.safe_load(open(gp, encoding="utf-8"))
        if not os.path.exists(cand):
            return {"doc": doc, "chapter": chapter, "weighted_score": 0.0,
                    "missing_candidate": True, "stage_scores": {}}
        pred = yaml.safe_load(open(cand, encoding="utf-8"))
        res = scorer.score_fixture(gold, pred, judge=judge)
        return {"doc": doc, "chapter": chapter,
                "weighted_score": res["weighted_score"],
                "stage_scores": res["stage_scores"]}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(_one, golds))
    scored = [r for r in rows if not r.get("missing_candidate")]
    mean = round(sum(r["weighted_score"] for r in scored) / len(scored), 2) if scored else 0.0
    agg = {"chapters_scored": len(scored), "chapters_total": len(rows),
           "mean_weighted_score": mean, "rows": rows}
    report_dir = pathlib.Path(pred_root) / "_report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "aggregate.json").write_text(report.to_json(agg), encoding="utf-8")
    (report_dir / "leaderboard.md").write_text(_run_all._leaderboard_md(agg), encoding="utf-8")
    return agg


def _make_judge_backend(client):
    """A semantic-equivalence backend (gold_text, pred_text)->bool using the LLM.
    Only consulted when deterministic substring matching already failed."""
    def backend(gold_text, pred_text):
        prompt = (
            "Two field values are extracted from the same technical document. "
            "Do they state the SAME fact/value (allowing paraphrase, abbreviation, "
            "reordering)? Answer with a JSON object {\"same\": true|false} only.\n"
            f"GOLD: {str(gold_text)[:400]}\nPRED: {str(pred_text)[:400]}"
        )
        try:
            raw = client.chat_json([{"role": "user", "content": prompt}],
                                   '{"same": true}')
            import json as _json
            return bool(_json.loads(raw.strip().strip("`")).get("same"))
        except Exception:
            return False
    return backend

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
    ap.add_argument("--chapters", default="",
                    help="comma-separated doc/chapter filter, e.g. "
                         "engram/ch00_abstract,cmos/ch01_introduction")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the LLM stages (deterministic S1-S5 only)")
    ap.add_argument("--chapter-workers", type=int, default=14,
                    help="how many chapters to process concurrently (each chapter "
                         "also runs its packages concurrently via QIEFEN_LLM_WORKERS)")
    ap.add_argument("--llm-judge", action="store_true",
                    help="score payload values with an LLM semantic-equivalence "
                         "judge (credits correct paraphrases; costs LLM calls)")
    args = ap.parse_args()
    # Resolve to absolute: the harness runs with cwd=GOLD, so a relative
    # pred-root would otherwise be looked up relative to the wrong directory.
    out_root = pathlib.Path(args.out).resolve()
    only = {c.strip() for c in args.chapters.split(",") if c.strip()}

    client = None if args.no_llm else default_client()
    if client is not None and getattr(client, "configured", False):
        print(f"LLM: {client.settings.openai_compat_model} @ {client.settings.openai_compat_base_url}")
        client.client()  # single-threaded pre-warm before fan-out (avoid init race)
    else:
        print("LLM: not configured -> deterministic only")

    jobs = []
    for gp in sorted(GOLD.glob("*/ch*/gold.yaml")):
        rel = gp.parent.relative_to(GOLD)
        if only and str(rel) not in only:
            continue
        meta = yaml.safe_load(gp.read_text(encoding="utf-8"))["source_meta"]
        src_path = SOURCE_PATHS.get(meta["source_file"])
        if not src_path or not src_path.exists():
            print(f"skip {rel}: source missing")
            continue
        jobs.append((rel, meta, src_path.read_text(encoding="utf-8")))

    def _process(job):
        rel, meta, src = job
        doc = run(src, source_file=meta["source_file"], profile=meta["profile"],
                  line_range=meta.get("source_line_range"),
                  source_id=meta.get("source_id", ""), title=meta.get("title", ""),
                  scope=meta.get("scope", ""), client=client)
        dst = out_root / rel
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "pred.yaml").write_text(to_yaml(doc), encoding="utf-8")
        return (f"wrote {rel}/pred.yaml ({len(doc.evidence_atoms)} atoms, "
                f"{len(doc.objects)} objects, {len(doc.relations)} relations)")

    # Chapters are independent -> process them concurrently. Combined with the
    # per-chapter package pool, this fans out to chapter_workers x QIEFEN_LLM_WORKERS
    # concurrent calls (the flash endpoint allows thousands).
    workers = max(1, min(args.chapter_workers, len(jobs))) if (client and jobs) else 1
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(_process, jobs):
            print(line)

    # Score. With --llm-judge, run the harness in-process so we can inject an
    # LLM semantic-equivalence backend (run_all.run accepts judge=...).
    if args.llm_judge and client is not None and getattr(client, "configured", False):
        from harness.judge import make_judge
        judge = make_judge(enabled=True, backend=_make_judge_backend(client))
        print("scoring with LLM judge (semantic payload equivalence, parallel)...")
        agg = _score_parallel(out_root, judge, workers)
        print(f"mean {agg['mean_weighted_score']} / 100 over "
              f"{agg['chapters_scored']} chapters (LLM-judged)")
    else:
        cmd = [sys.executable, "-m", "harness.run_all", "--gold-root", ".",
               "--pred-root", str(out_root), "--out-dir", str(out_root / "_report")]
        print("running harness:", " ".join(cmd))
        subprocess.run(cmd, cwd=str(GOLD), check=False)
    print(f"leaderboard: {out_root / '_report' / 'leaderboard.md'}")


if __name__ == "__main__":
    main()
