import os, pathlib, pytest, yaml
REPO = pathlib.Path(__file__).resolve().parents[3]
GOLD = REPO / "fangan" / "testcases"
SRC = pathlib.Path(os.environ.get("QIEFEN_SOURCE_ROOT", "/Users/hzf/workspace/pdf_parser"))
SRC_PATHS = {
    "engram_paper_mineru.md": SRC / "engram_paper_mineru.md",
    "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md": SRC
    / "notebook_papers_mineru_skill_results"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg"
    / "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md",
}

@pytest.fixture
def source_text():
    def _load(name):
        p = SRC_PATHS[name]
        if not p.exists():
            pytest.skip(f"source missing: {p}")
        return p.read_text(encoding="utf-8")
    return _load
