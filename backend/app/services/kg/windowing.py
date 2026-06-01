"""Chapter -> contiguous N-char windows (M overlap) over prose, tagged by section."""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel
from app.services.qiefen.source_elements import parse_elements
from app.services.qiefen.section_tree import build_section_tree

class Window(BaseModel):
    char_start: int
    char_end: int
    section_path: str
    file: str

def _section_of_line(line: int, sec_by_line):
    chosen = ""
    for hline, path in sec_by_line:
        if hline <= line:
            chosen = path
        else:
            break
    return chosen

def make_windows(text: str, source_file: str, line_range: Optional[List[int]],
                 n: int = 9000, m: int = 450) -> List[Window]:
    elements = parse_elements(text, source_file, line_range)
    sections = build_section_tree(elements)
    headings = [e for e in elements if e.type == "heading"]
    sec_by_line = sorted((h.line_start, s.path) for h, s in zip(headings, sections))
    prose = [e for e in elements if e.type in ("paragraph", "list_item", "formula",
                                               "table", "figure_caption")]
    # group prose elements by enclosing section, window each section's span
    windows: List[Window] = []
    by_sec = {}
    for e in prose:
        path = _section_of_line(e.line_start, sec_by_line)
        by_sec.setdefault(path, []).append(e)
    step = max(1, n - m)
    for path, els in by_sec.items():
        start = min(e.char_start for e in els)
        end = max(e.char_end for e in els)
        s = start
        while s < end:
            windows.append(Window(char_start=s, char_end=min(s + n, end),
                                  section_path=path, file=source_file))
            if s + n >= end:
                break
            s += step
    windows.sort(key=lambda w: w.char_start)
    return windows
