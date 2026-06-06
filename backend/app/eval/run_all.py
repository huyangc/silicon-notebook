"""一键评测。用法:
PYTHONPATH=backend python -m app.eval.run_all --notebook nb-012fb94249 --only quality,speed,inference
"""
from __future__ import annotations
import argparse, glob, pathlib
from datetime import datetime

DEFAULT_DB = ".local/silicon_notebook.db"
DEFAULT_NB = "nb-012fb94249"


def _find_source_md(notebook_id: str, pattern: str = "*Design_of_Analog_CMOS_IC*.md") -> str:
    matches = sorted(glob.glob(f".local/storage/notebooks/{notebook_id}/{pattern}"))
    return matches[0] if matches else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook", default=DEFAULT_NB)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--only", default="quality,speed,inference")
    ap.add_argument("--source-md", default="")
    ap.add_argument("--target-seconds", type=int, default=120)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = pathlib.Path(a.out or f".local/eval_runs/{ts}")
    out.mkdir(parents=True, exist_ok=True)
    print(f"[eval] -> {out}")

    if "quality" in only:
        from app.eval.probes import run_quality
        from app.eval.report import render_quality_report
        pb = run_quality(a.db, a.notebook)
        (out / "quality_report.md").write_text(render_quality_report(pb), encoding="utf-8")
        print("[eval] quality_report.md done")

    if "speed" in only:
        from app.core.config import Settings
        from app.eval.speed import measure_speed, extrapolate
        from app.eval.report import render_speed_report
        s = Settings()
        source_md = a.source_md or _find_source_md(a.notebook)
        assert source_md, "找不到 speed 源文件,请用 --source-md 指定"
        measured = measure_speed(source_md)
        extra = extrapolate(measured, [100000, 200000, 500000, 1000000],
                            s.kg_extract_workers, s.kg_window_min_chars, s.kg_window_max_chars)
        within = [m["chars"] for m in measured if m["wall_s"] <= a.target_seconds]
        rec = max(within) if within else (measured[0]["chars"] if measured else 0)
        (out / "speed_report.md").write_text(
            render_speed_report(measured, extra, rec, a.target_seconds), encoding="utf-8")
        print("[eval] speed_report.md done")

    if "inference" in only:
        from app.eval.inference import run_inference
        from app.eval.report import render_inference_report
        rows = run_inference(a.notebook)
        (out / "inference_report.md").write_text(render_inference_report(rows), encoding="utf-8")
        print("[eval] inference_report.md done")

    print(f"[eval] all done -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
