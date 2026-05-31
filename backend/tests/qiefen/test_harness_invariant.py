import pathlib
import pytest
import yaml
from app.services.qiefen.pipeline import run

REPO = pathlib.Path(__file__).resolve().parents[3]
GOLD = REPO / "fangan" / "testcases"
CHAPTERS = sorted(str(p.parent.relative_to(GOLD))
                  for p in GOLD.glob("*/ch*/gold.yaml"))


@pytest.mark.parametrize("chapter", CHAPTERS)
def test_every_emitted_atom_span_is_verbatim(chapter, source_text):
    gold = yaml.safe_load((GOLD / chapter / "gold.yaml").read_text(encoding="utf-8"))
    meta = gold["source_meta"]
    src = source_text(meta["source_file"])
    doc = run(src, source_file=meta["source_file"], profile=meta["profile"],
              line_range=meta.get("source_line_range"),
              source_id=meta.get("source_id", ""), title=meta.get("title", ""))
    for a in doc.evidence_atoms:
        s = a.source_span
        assert src[s.char_start:s.char_end] == a.raw_text, f"{chapter}:{a.id}"
