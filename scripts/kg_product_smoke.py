#!/usr/bin/env python3
"""Task 10 smoke: drive the REAL product KG extraction (_run_extraction with the
deepseek-v4-flash OpenAICompatibleClient) on one academic + one textbook source,
asserting non-zero grounded KG nodes/edges. Main-session only (needs network)."""
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from app.core.config import Settings  # noqa: E402
from app.services.sqlite_repository import SQLiteRepository  # noqa: E402
from app.services.kg.parsing import parse_elements  # noqa: E402
from app.models.schemas import NotebookCreate  # noqa: E402

SRC = pathlib.Path("/Users/hzf/workspace/pdf_parser")
SOURCES = {
    "engram": (SRC / "engram_paper_mineru.md", "academic_paper", None),
    "cmos": (SRC / "notebook_papers_mineru_skill_results"
             / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
             / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md", "textbook", 40000),
}


def insert_source(repo, nb_id, name, md_path, doc_type, char_limit, tmpdir):
    text = pathlib.Path(md_path).read_text(encoding="utf-8")
    if char_limit:
        text = text[:char_limit]
    f = pathlib.Path(tmpdir) / f"{name}.md"
    f.write_text(text, encoding="utf-8")
    els = parse_elements(text, source_file=str(f))
    sid = repo.maintenance.seed_parsed_source(
        nb_id, title=name, doc_type=doc_type,
        file_name=f"{name}.md", file_path=str(f),
        elements=[{"element_type": el.type,
                   "location_label": f"L{el.line_start}-{el.line_end}",
                   "text": el.text} for el in els])
    return sid, len(els)


def grounding_ok(repo, nb_id):
    objs = repo.maintenance.sample_knowledge_objects(nb_id, limit=5)
    bad = 0
    import json
    for o in objs:
        for ev in json.loads(o["evidence"] or "[]"):
            text = repo.maintenance.element_text(ev["element_id"])
            if text is None or " ".join(ev["quoted_span"].split()) not in " ".join(text.split()):
                bad += 1
    return bad


def main():
    settings = Settings()
    repo = SQLiteRepository(settings)
    print("llm configured:", repo.llm_client.configured, "| model:",
          getattr(settings, "openai_compat_model", "?"), flush=True)
    assert repo.llm_client.configured, "product LLM not configured in .env"
    tmpdir = tempfile.mkdtemp()
    results = {}
    for name, (md, dt, lim) in SOURCES.items():
        nb = repo.create_notebook(NotebookCreate(name=f"smoke-{name}"))
        sid, n_el = insert_source(repo, nb.id, name, md, dt, lim, tmpdir)
        print(f"\n[{name}] {n_el} elements, doc_type={dt} -> extracting...", flush=True)
        repo.maintenance.run_extraction(sid)
        g = repo.knowledge_graph(nb.id)
        types = sorted({n.object_type for n in g.nodes})
        empty_headlines = sum(1 for n in g.nodes if not n.headline.strip())
        bad = grounding_ok(repo, nb.id)
        results[name] = (len(g.nodes), len(g.edges), types, empty_headlines, bad)
        print(f"[{name}] nodes={len(g.nodes)} edges={len(g.edges)} types={types} "
              f"empty_headlines={empty_headlines} ungrounded_in_sample={bad}", flush=True)
        sample = [n.headline for n in g.nodes[:6]]
        print(f"[{name}] sample headlines: {sample}", flush=True)
    print("\n=== SUMMARY ===", flush=True)
    ok = True
    for name, (nn, ne, types, eh, bad) in results.items():
        good = nn > 0 and set(types) <= {"concept", "claim", "formula", "procedure"} and eh == 0 and bad == 0
        ok = ok and good
        print(f"{name}: nodes={nn} edges={ne} types={types} empty_headlines={eh} "
              f"ungrounded={bad} -> {'PASS' if good else 'FAIL'}", flush=True)
    print("OVERALL:", "PASS" if ok else "FAIL", flush=True)


if __name__ == "__main__":
    main()
