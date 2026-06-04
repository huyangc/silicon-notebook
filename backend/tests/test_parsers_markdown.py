from pathlib import Path
from app.services.parsers import parse_markdown

MD = """# Title

<a id="a1"></a>
## Cmd

Use the command:

```tcl
set_message -severity info
```

| Opt | Desc |
| --- | --- |
| -x | do x |
"""


def _write(tmp_path, text):
    p = tmp_path / "doc.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_no_anchor_noise_elements(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    assert all("<a id=" not in e.text for e in els)


def test_code_block_is_one_element_verbatim(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    code = [e for e in els if e.element_type == "code_block"]
    assert len(code) == 1
    assert "set_message -severity info" in code[0].text
    assert code[0].metadata.get("lang") == "tcl"


def test_table_is_one_element(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    tables = [e for e in els if e.element_type == "table"]
    assert len(tables) == 1 and "-x" in tables[0].text


def test_section_path_in_metadata(tmp_path):
    els = parse_markdown("s1", _write(tmp_path, MD))
    para = [e for e in els if e.text.startswith("Use the command")][0]
    assert para.metadata.get("section_path") == "Title > Cmd"
