import sys
from types import SimpleNamespace

from app.models.sources import SourceElement
from app.services import parsers


def test_pymupdf4llm_fallback_preserves_pages_headings_and_html_tables(
    tmp_path, monkeypatch
):
    calls = []

    def to_markdown(path, **kwargs):
        calls.append((path, kwargs))
        return [
            {
                "text": (
                    "# Section title\n\n"
                    "First column then second column.\n\n"
                    "<table><tr><th>A</th><th>B</th></tr>"
                    "<tr><td>1</td><td>2</td></tr></table>"
                )
            },
            {"text": "## Next page\n\nMore text."},
        ]

    monkeypatch.setitem(
        sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=to_markdown)
    )
    path = tmp_path / "paper.pdf"
    path.touch()

    elements = parsers.parse_pdf_python("src-1", path)

    assert calls[0][0] == str(path)
    assert calls[0][1] == {
        "page_chunks": True,
        "show_progress": False,
        "write_images": False,
        "embed_images": False,
        "table_output": "html",
    }
    assert [element.metadata["page_number"] for element in elements] == [1, 1, 1, 2, 2]
    assert all(element.metadata["parser"] == "pymupdf4llm" for element in elements)
    assert any(element.element_type == "heading" and element.text == "Section title" for element in elements)
    table = next(element for element in elements if element.element_type == "table")
    assert table.metadata["table_html"].startswith("<table>")
    assert "A | B" in table.text


def test_pymupdf4llm_error_uses_last_resort_pypdf(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("layout engine failed")

    monkeypatch.setitem(
        sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=boom)
    )
    expected = [
        SourceElement(
            id="",
            source_id="src-1",
            element_type="page_text",
            location_label="PDF p.1 paragraph 1",
            text="last resort",
            metadata={"parser": "pdf", "page_number": 1},
        )
    ]
    monkeypatch.setattr(parsers, "parse_pdf_pypdf", lambda source_id, path: expected)

    assert parsers.parse_pdf_python("src-1", tmp_path / "paper.pdf") == expected
