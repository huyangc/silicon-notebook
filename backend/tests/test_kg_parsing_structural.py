from app.services.kg.parsing import parse_elements

MD = """# A

para one here.

```tcl
set_db x y
```

<a id="z"></a>
## B

| c1 | c2 |
| --- | --- |
| v1 | v2 |
"""


def test_code_block_is_prose_element_for_windowing():
    els = parse_elements(MD, "doc.md", None)
    code = [e for e in els if "set_db x y" in e.text]
    assert code and code[0].type == "paragraph"


def test_no_anchor_prose_elements():
    els = parse_elements(MD, "doc.md", None)
    assert all("<a id=" not in e.text for e in els)


def test_table_element_present():
    els = parse_elements(MD, "doc.md", None)
    assert any(e.type == "table" and "v1" in e.text for e in els)


def test_char_spans_round_trip():
    els = parse_elements(MD, "doc.md", None)
    for e in els:
        assert e.char_start <= e.char_end <= len(MD)
