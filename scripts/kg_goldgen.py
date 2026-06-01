#!/usr/bin/env python3
"""Generate DRAFT gold KGs for the testcase chapters using the gold model
(GOLDGEN_ config = deepseek-v4-flash). Output:
fangan/testcases_kg/<doc>/<chapter>/gold_kg.yaml  (for human curation).

Usage: PYTHONPATH=backend python scripts/kg_goldgen.py --chapters engram/ch00_abstract
"""
import argparse
import concurrent.futures as cf
import os
import pathlib
import sys

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


def gen_chapter(rel, client, n, m, workers):
    meta = yaml.safe_load((GOLD / rel / "gold.yaml").read_text())["source_meta"]
    src = SRC_PATHS[meta["source_file"]].read_text(encoding="utf-8")
    dt = DOC_TYPE.get(meta["profile"], "academic")
    wins = make_windows(src, meta["source_file"], meta.get("source_line_range"), n, m)
    nodes, edges = [], []
    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(wins) or 1))) as pool:
        for ns, es in pool.map(lambda w: extract_window(client, src, w.char_start,
                                                        w.char_end, w.section_path, dt), wins):
            nodes += ns
            edges += es
    for item in nodes + edges:
        for ev in item.evidence:
            ev.file = meta["source_file"]
    nodes, edges = canonicalize(nodes, edges, doc_id=meta.get("source_id", "doc"))
    g = KnowledgeGraph(doc_id=meta.get("source_id", "doc"), doc_type=dt, nodes=nodes, edges=edges)
    dst = OUT / rel
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "gold_kg.yaml").write_text(to_yaml(g), encoding="utf-8")
    return rel, len(nodes), len(edges)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", default="")
    ap.add_argument("--n", type=int, default=9000)
    ap.add_argument("--m", type=int, default=450)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    client = make_client("GOLDGEN_")
    assert client.configured, "GOLDGEN_OPENAI_COMPAT_* not set in .env"
    print(f"gold model: {client.model}")
    only = {c.strip() for c in a.chapters.split(",") if c.strip()}
    chapters = [str(p.parent.relative_to(GOLD)) for p in sorted(GOLD.glob("*/ch*/gold.yaml"))]
    for rel in chapters:
        if only and rel not in only:
            continue
        r, nn, ne = gen_chapter(rel, client, a.n, a.m, a.workers)
        print(f"{r}: {nn} nodes, {ne} edges -> fangan/testcases_kg/{r}/gold_kg.yaml")


if __name__ == "__main__":
    main()
