import os
import pathlib
import pytest
import yaml

# Repo root = .../silicon_notebook (or worktree root). tests live at backend/tests/qiefen.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GOLD_ROOT = REPO_ROOT / "fangan" / "testcases"

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


def _require(path: pathlib.Path) -> pathlib.Path:
    if not path.exists():
        pytest.skip(f"raw source not available: {path}")
    return path


@pytest.fixture
def gold_root():
    return GOLD_ROOT


@pytest.fixture
def load_gold():
    def _load(doc, chapter):
        return yaml.safe_load(
            (GOLD_ROOT / doc / chapter / "gold.yaml").read_text(encoding="utf-8")
        )
    return _load


@pytest.fixture
def source_text():
    def _load(basename):
        return _require(SOURCE_PATHS[basename]).read_text(encoding="utf-8")
    return _load
