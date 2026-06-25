import zipfile
from pathlib import Path

from docx import Document

from app.services.parsers import mineru_content_list_to_elements, parse_docx, parse_pptx, parse_source_file


def _content_list():
    return [
        {"type": "title", "text": "Heading", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "Body text.", "page_idx": 0},
    ]


def test_mapper_default_prefix_is_pdf():
    els = mineru_content_list_to_elements("s1", _content_list())
    assert els[0].location_label.startswith("PDF p.1")
    assert all(e.metadata.get("parser") == "mineru" for e in els)
    assert els[0].metadata.get("source_format") == "pdf"


def test_mapper_custom_prefix_for_office():
    els = mineru_content_list_to_elements("s1", _content_list(), label_prefix="DOCX")
    assert els[0].location_label.startswith("DOCX p.1")
    assert els[0].metadata.get("source_format") == "docx"
    assert els[0].element_type == "heading"
    assert els[1].element_type == "paragraph"


class FakeMineru:
    """模式无关的假 MinerU 客户端：按构造参数返回 content_list / 抛错 / 报未配置。"""

    def __init__(self, configured=True, content_list=None, raises=None, mode="http"):
        self.configured = configured
        self._content_list = content_list if content_list is not None else []
        self._raises = raises
        self.mode = mode
        self.last_error = ""

    def parse(self, file_path, file_name):
        # 故意不在抛出前预设 last_error：让生产代码的 "not last_error → 设置" 分支
        # 真正被执行（否则该分支被测试 fake 短路，删了也察觉不到）。
        self.last_error = ""
        if self._raises is not None:
            raise self._raises
        return self._content_list


def _make_docx(path: Path) -> Path:
    doc = Document()
    doc.add_paragraph("Hello from docx.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "A"
    table.rows[0].cells[1].text = "B"
    doc.save(str(path))
    return path


def test_docx_uses_mineru_when_configured(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_docx("s1", path, "a.docx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)
    assert els[0].location_label.startswith("DOCX p.1")


def test_docx_falls_back_when_not_configured(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(configured=False)
    els = parse_docx("s1", path, "a.docx", client)
    assert all(e.metadata.get("parser") == "docx" for e in els)
    assert any("Hello from docx." in e.text for e in els)


def test_docx_falls_back_on_mineru_error(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(raises=RuntimeError("mineru boom"))
    els = parse_docx("s1", path, "a.docx", client)  # 不应冒泡异常
    assert all(e.metadata.get("parser") == "docx" for e in els)
    assert client.last_error == "mineru boom"


def test_docx_falls_back_when_mineru_empty(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(content_list=[])
    els = parse_docx("s1", path, "a.docx", client)
    assert all(e.metadata.get("parser") == "docx" for e in els)


def test_parse_source_file_forwards_client_to_docx(tmp_path):
    path = _make_docx(tmp_path / "a.docx")
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_source_file("s1", str(path), "a.docx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)


_SLIDE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    "<p:cSld><p:spTree><p:sp><p:txBody>"
    "<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)


def _make_pptx(path: Path, slide_texts) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index, text in enumerate(slide_texts, start=1):
            archive.writestr(f"ppt/slides/slide{index}.xml", _SLIDE_XML.format(text=text))
    return path


def test_pptx_uses_mineru_when_configured(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_pptx("s1", path, "a.pptx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)
    assert els[0].location_label.startswith("PPTX p.1")


def test_pptx_falls_back_when_not_configured(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(configured=False)
    els = parse_pptx("s1", path, "a.pptx", client)
    assert all(e.metadata.get("parser") == "pptx" for e in els)
    assert any("Slide one body" in e.text for e in els)


def test_pptx_falls_back_on_mineru_error(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(raises=RuntimeError("mineru boom"))
    els = parse_pptx("s1", path, "a.pptx", client)
    assert all(e.metadata.get("parser") == "pptx" for e in els)
    assert client.last_error == "mineru boom"


def test_pptx_falls_back_when_mineru_empty(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(content_list=[])
    els = parse_pptx("s1", path, "a.pptx", client)
    assert all(e.metadata.get("parser") == "pptx" for e in els)


def test_parse_source_file_forwards_client_to_pptx(tmp_path):
    path = _make_pptx(tmp_path / "a.pptx", ["Slide one body"])
    client = FakeMineru(content_list=[{"type": "text", "text": "From MinerU.", "page_idx": 0}])
    els = parse_source_file("s1", str(path), "a.pptx", client)
    assert any(e.metadata.get("parser") == "mineru" for e in els)
