import os
import pathlib
import pytest

from app.services.structural_markdown import parse_blocks
from app.services.kg.windowing import make_windows

SAMPLE = pathlib.Path(
    os.environ.get("INNOVUS_SAMPLE", "/Users/hzf/Downloads/doc/innovusUG/innovusUG_complete.md")
)


@pytest.fixture
def text():
    if not SAMPLE.exists():
        pytest.skip(f"Innovus 样本缺失: {SAMPLE}")
    return SAMPLE.read_text(encoding="utf-8", errors="replace")


def test_no_anchor_noise_blocks(text):
    blocks = parse_blocks(text)
    anchor_blocks = [b for b in blocks if "<a id=" in b.text]
    assert anchor_blocks == [], f"不应有锚点噪声块, got {len(anchor_blocks)}"


def test_has_intact_code_blocks(text):
    blocks = parse_blocks(text)
    code = [b for b in blocks if b.type == "code_block"]
    assert len(code) >= 100
    assert any("\n" in b.text for b in code)


def test_window_count_is_hundreds_not_thousands(text):
    wins = make_windows(text, "innovus.md", None, n=9000, m=450)
    assert len(wins) < 1200, f"窗口数应为百级(<warn阈值), got {len(wins)}"
