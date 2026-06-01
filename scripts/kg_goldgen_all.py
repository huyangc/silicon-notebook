#!/usr/bin/env python3
"""Generate DRAFT gold KGs for ALL testcase chapters using ONE global thread pool,
so every window across every chapter runs concurrently. With deepseek-v4-pro
(500-concurrent headroom) the whole batch finishes in ~one window-time instead of
summing per-chapter. Output: fangan/testcases_kg/<doc>/<chapter>/gold_kg.yaml.

Usage: PYTHONPATH=backend python scripts/kg_goldgen_all.py [--chapters a,b] [--max-workers 200]
"""
import argparse
import concurrent.futures as cf
import os
import pathlib
import sys
import threading
import traceback

import yaml
from dotenv import load_dotenv

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
load_dotenv(REPO / ".env")

from app.services.kg.client import make_client  # noqa: E402
from app.services.kg.windowing import make_windows  # noqa: E402
from app.services.kg.extract import extract_window  # noqa: E402
from app.services.kg.canonicalize import canonicalize  # noqa: E402
from app.services.kg.models import KnowledgeGraph  # noqa: E402
from app.services.kg.emit import to_yaml  # noqa: E402

GOLD = REPO / "fangan" / "testcases"
OUT = REPO / "fangan" / "testcases_kg"
SRC = pathlib.Path(os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser"))
SRC_PATHS = {
    "engram_paper_mineru.md": SRC / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SRC
    / "notebook_papers_mineru_skill_results" / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}
DOC_TYPE = {"article_research": "academic", "textbook": "textbook"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", default="")
    ap.add_argument("--n", type=int, default=9000)
    ap.add_argument("--m", type=int, default=450)
    ap.add_argument("--max-workers", type=int, default=200)
    a = ap.parse_args()
    client = make_client("GOLDGEN_")
    assert client.configured, "GOLDGEN_OPENAI_COMPAT_* not set in .env"
    print(f"gold model: {client.model}", flush=True)

    only = {c.strip() for c in a.chapters.split(",") if c.strip()}
    meta_by_rel = {}
    tasks = []  # (rel, src, doc_type, window)
    for p in sorted(GOLD.glob("*/ch*/gold.yaml")):
        rel = str(p.parent.relative_to(GOLD))
        if only and rel not in only:
            continue
        meta = yaml.safe_load(p.read_text())["source_meta"]
        src = SRC_PATHS[meta["source_file"]].read_text(encoding="utf-8")
        dt = DOC_TYPE.get(meta["profile"], "academic")
        wins = make_windows(src, meta["source_file"], meta.get("source_line_range"), a.n, a.m)
        meta_by_rel[rel] = (meta, src, dt)
        for w in wins:
            tasks.append((rel, src, dt, w))
    print(f"{len(tasks)} windows across {len(meta_by_rel)} chapters "
          f"(max_workers={a.max_workers})", flush=True)

    results = {rel: ([], []) for rel in meta_by_rel}
    lock = threading.Lock()
    done = [0]
    failed = []

    def run(t):
        rel, src, dt, w = t
        try:
            ns, es = extract_window(client, src, w.char_start, w.char_end, w.section_path, dt)
        except Exception as exc:  # noqa: BLE001 — keep batch alive; log + skip window
            with lock:
                failed.append((rel, str(exc)))
                done[0] += 1
                print(f"  [{done[0]}/{len(tasks)}] {rel} FAILED: {exc}", flush=True)
            return
        with lock:
            results[rel][0].extend(ns)
            results[rel][1].extend(es)
            done[0] += 1
            print(f"  [{done[0]}/{len(tasks)}] {rel}  +{len(ns)}n +{len(es)}e", flush=True)

    with cf.ThreadPoolExecutor(max_workers=a.max_workers) as pool:
        list(pool.map(run, tasks))

    print("--- canonicalize + write ---", flush=True)
    for rel, (nodes, edges) in results.items():
        meta, src, dt = meta_by_rel[rel]
        for item in nodes + edges:
            for ev in item.evidence:
                ev.file = meta["source_file"]
        nodes, edges = canonicalize(nodes, edges, doc_id=meta.get("source_id", "doc"))
        g = KnowledgeGraph(doc_id=meta.get("source_id", "doc"), doc_type=dt,
                           nodes=nodes, edges=edges)
        dst = OUT / rel
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "gold_kg.yaml").write_text(to_yaml(g), encoding="utf-8")
        print(f"{rel}: {len(nodes)} nodes, {len(edges)} edges "
              f"-> fangan/testcases_kg/{rel}/gold_kg.yaml", flush=True)
    if failed:
        print(f"\n{len(failed)} window(s) failed:", flush=True)
        for rel, msg in failed:
            print(f"  {rel}: {msg}", flush=True)


if __name__ == "__main__":
    main()
