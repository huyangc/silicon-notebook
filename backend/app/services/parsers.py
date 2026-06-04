from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree

from app.models.schemas import SourceElement


def parse_source_file(
    source_id: str,
    file_path: str,
    file_name: str,
    mineru_client: Any = None,
) -> List[SourceElement]:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(source_id, Path(file_path))
    if suffix == ".docx":
        return parse_docx(source_id, Path(file_path))
    if suffix == ".pptx":
        return parse_pptx(source_id, Path(file_path))
    if suffix == ".pdf":
        return parse_pdf(source_id, Path(file_path), file_name, mineru_client)
    if suffix == ".csv":
        return parse_csv(source_id, Path(file_path))
    if suffix in {".xlsx", ".xlsm"}:
        return parse_xlsx(source_id, Path(file_path))
    return parse_plain_text(source_id, Path(file_path), "text")


def parse_csv(source_id: str, path: Path) -> List[SourceElement]:
    import csv

    elements: List[SourceElement] = []
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        for index, row in enumerate(csv.reader(handle), start=1):
            cells = [cell.strip() for cell in row if cell and cell.strip()]
            if not cells:
                continue
            elements.append(
                _element(
                    source_id,
                    "table_row",
                    f"CSV row {index}",
                    " | ".join(cells),
                    {"parser": "csv", "row_index": index},
                )
            )
    return elements


def parse_xlsx(source_id: str, path: Path) -> List[SourceElement]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("XLSX parser dependency openpyxl is not installed") from exc

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    elements: List[SourceElement] = []
    for sheet in workbook.worksheets:
        for index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
            if not cells:
                continue
            elements.append(
                _element(
                    source_id,
                    "table_row",
                    f"XLSX {sheet.title} row {index}",
                    " | ".join(cells),
                    {"parser": "xlsx", "sheet": sheet.title, "row_index": index},
                )
            )
    workbook.close()
    return elements


def parse_markdown(source_id: str, path: Path) -> List[SourceElement]:
    from app.services.structural_markdown import parse_blocks

    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = parse_blocks(text)
    elements: List[SourceElement] = []
    counters: Dict[str, int] = {}
    for block in blocks:
        counters[block.type] = counters.get(block.type, 0) + 1
        ordinal = counters[block.type]
        metadata: Dict[str, Any] = {
            "parser": "markdown",
            "section_path": block.section_path,
            "char_start": block.char_start,
            "char_end": block.char_end,
            "line_start": block.line_start,
            "line_end": block.line_end,
        }
        if block.type == "heading":
            metadata["heading_level"] = block.level
            if block.anchor_id:
                metadata["anchor_id"] = block.anchor_id
        if block.type == "code_block":
            metadata["lang"] = block.lang
        if block.type == "image":
            metadata.update(block.metadata)
        elements.append(
            _element(
                source_id,
                block.type,
                f"Markdown {block.type} {ordinal}",
                block.text,
                metadata,
            )
        )
    return elements or parse_plain_text(source_id, path, "markdown")


def parse_plain_text(source_id: str, path: Path, parser_name: str) -> List[SourceElement]:
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
    return [
        _element(
            source_id,
            "paragraph",
            f"Text paragraph {index}",
            " ".join(chunk.split()),
            {"parser": parser_name, "paragraph_index": index},
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def parse_docx(source_id: str, path: Path) -> List[SourceElement]:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("DOCX parser dependency python-docx is not installed") from exc

    document = Document(str(path))
    elements: List[SourceElement] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if not text:
            continue
        elements.append(
            _element(
                source_id,
                "paragraph",
                f"DOCX paragraph {index}",
                text,
                {"parser": "docx", "paragraph_index": index},
            )
        )

    table_index = 1
    for table in document.tables:
        for row_index, row in enumerate(table.rows, start=1):
            values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if not values:
                continue
            elements.append(
                _element(
                    source_id,
                    "table_row",
                    f"DOCX table {table_index} row {row_index}",
                    " | ".join(values),
                    {"parser": "docx", "table_index": table_index, "row_index": row_index},
                )
            )
        table_index += 1
    return elements


def parse_pptx(source_id: str, path: Path) -> List[SourceElement]:
    elements: List[SourceElement] = []
    namespace = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }

    def shape_text(node: ElementTree.Element) -> str:
        values = [
            run.text.strip()
            for run in node.findall(".//a:t", namespace)
            if run.text and run.text.strip()
        ]
        return " ".join(values)

    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for slide_index, slide_name in enumerate(slide_names, start=1):
            root = ElementTree.fromstring(archive.read(slide_name))
            shapes = root.findall(".//p:sp", namespace)
            shape_index = 0
            for shape in shapes:
                text = shape_text(shape)
                if not text:
                    continue
                shape_index += 1
                elements.append(
                    _element(
                        source_id,
                        "slide_text",
                        f"PPTX slide {slide_index} shape {shape_index}",
                        text,
                        {
                            "parser": "pptx",
                            "slide_number": slide_index,
                            "shape_index": shape_index,
                        },
                    )
                )
            if shape_index == 0:
                # No shape-level text (e.g. minimal XML); fall back to whole slide.
                text = shape_text(root)
                if text:
                    elements.append(
                        _element(
                            source_id,
                            "slide_text",
                            f"PPTX slide {slide_index}",
                            text,
                            {"parser": "pptx", "slide_number": slide_index},
                        )
                    )

        notes_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
        )
        for notes_name in notes_names:
            match = re.search(r"notesSlide(\d+)\.xml", notes_name)
            notes_number = int(match.group(1)) if match else 0
            root = ElementTree.fromstring(archive.read(notes_name))
            text = shape_text(root).strip()
            # Notes placeholders often echo the bare slide number; skip those.
            if not text or text == str(notes_number):
                continue
            elements.append(
                _element(
                    source_id,
                    "speaker_notes",
                    f"PPTX slide {notes_number} notes",
                    text,
                    {"parser": "pptx", "slide_number": notes_number, "kind": "notes"},
                )
            )
    return elements


def parse_pdf(
    source_id: str,
    path: Path,
    file_name: str = "",
    mineru_client: Any = None,
) -> List[SourceElement]:
    """Parse a PDF via MinerU when configured, else fall back to pypdf text.

    MinerU (run on the GPU deployment host) recovers formulas (as LaTeX),
    tables (as HTML), and reading order. When it is not configured or fails,
    we degrade to pypdf's flat text extraction so local/no-GPU dev still works.
    """
    if mineru_client is not None and getattr(mineru_client, "configured", False):
        try:
            content_list = mineru_client.parse(str(path), file_name or path.name)
            elements = mineru_content_list_to_elements(source_id, content_list)
            if elements:
                return elements
            if hasattr(mineru_client, "last_error"):
                mineru_client.last_error = "MinerU content_list mapped to zero source elements"
        except Exception as exc:
            if hasattr(mineru_client, "last_error") and not getattr(
                mineru_client, "last_error", ""
            ):
                mineru_client.last_error = str(exc)
            # Fall through to pypdf so a MinerU outage never blocks ingestion.
            pass
    return parse_pdf_pypdf(source_id, path)


def parse_pdf_pypdf(source_id: str, path: Path) -> List[SourceElement]:
    """Offline PDF fallback (no MinerU).

    Uses pypdf's layout-aware extraction (better reading order and column/row
    spacing than the default mode) and segments each page into paragraph and
    heading elements instead of one flattened blob. No formula/table fidelity —
    that requires MinerU — but gives a usable structured baseline offline.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF parser dependency pypdf is not installed") from exc

    reader = PdfReader(str(path))
    elements: List[SourceElement] = []
    for page_index, page in enumerate(reader.pages, start=1):
        text = _extract_pdf_page_text(page)
        if not text.strip():
            continue
        para_index = 0
        heading_index = 0
        for is_heading, body in _segment_pdf_blocks(text):
            if is_heading:
                heading_index += 1
                elements.append(
                    _element(
                        source_id,
                        "heading",
                        f"PDF p.{page_index} heading {heading_index}",
                        body,
                        {"parser": "pdf", "page_number": page_index, "heading_index": heading_index},
                    )
                )
            else:
                para_index += 1
                elements.append(
                    _element(
                        source_id,
                        "page_text",
                        f"PDF p.{page_index} paragraph {para_index}",
                        body,
                        {"parser": "pdf", "page_number": page_index, "paragraph_index": para_index},
                    )
                )
    return elements


def _extract_pdf_page_text(page: Any) -> str:
    """Prefer pypdf layout mode; fall back to default extraction."""
    try:
        layout = page.extract_text(extraction_mode="layout") or ""
        if layout.strip():
            return layout
    except Exception:
        pass
    return page.extract_text() or ""


def _segment_pdf_blocks(text: str) -> List[tuple[bool, str]]:
    """Split page text into (is_heading, body) blocks on blank lines.

    Lines within a block are joined (wrapped text), intra-line runs of spaces
    are collapsed. A single short line without terminal punctuation is treated
    as a heading so document structure survives into extraction/citations.
    """
    blocks: List[tuple[bool, str]] = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [" ".join(line.split()) for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        body = " ".join(lines).strip()
        if not body:
            continue
        is_heading = (
            len(lines) == 1
            and len(body) <= 80
            and not body.endswith((".", "。", ";", "；", ":", "：", ",", "，"))
        )
        blocks.append((is_heading, body))
    return blocks


def mineru_content_list_to_elements(
    source_id: str,
    content_list: List[dict],
) -> List[SourceElement]:
    """Map MinerU's content_list blocks into structured SourceElements.

    Formulas are kept as LaTeX (element_type "formula"), tables keep their HTML
    in metadata while exposing a flattened text body, and headings keep their
    level. page_idx is 0-based in MinerU; we display 1-based page numbers.
    """
    elements: List[SourceElement] = []
    counters: Dict[int, int] = {}
    for block in content_list:
        if not isinstance(block, dict):
            continue
        page = int(block.get("page_idx", 0)) + 1
        counters[page] = counters.get(page, 0) + 1
        ordinal = counters[page]
        block_type = str(block.get("type", "")).lower()

        if block_type in {"text", "title"}:
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            level = int(block.get("text_level", 0) or 0)
            element_type = "heading" if level >= 1 else "paragraph"
            elements.append(
                _element(
                    source_id,
                    element_type,
                    f"PDF p.{page} block {ordinal}",
                    text,
                    {"parser": "mineru", "page_number": page, "text_level": level},
                )
            )
        elif block_type == "equation":
            latex = _strip_math_delimiters(str(block.get("text", "")))
            if not latex:
                continue
            elements.append(
                _element(
                    source_id,
                    "formula",
                    f"PDF p.{page} formula {ordinal}",
                    latex,
                    {
                        "parser": "mineru",
                        "page_number": page,
                        "text_format": str(block.get("text_format", "latex")),
                    },
                )
            )
        elif block_type == "table":
            html = str(block.get("table_body", ""))
            caption = " ".join(_as_list(block.get("table_caption")))
            body_text = _html_table_to_text(html)
            text = " ".join(part for part in (caption, body_text) if part).strip()
            if not text:
                continue
            elements.append(
                _element(
                    source_id,
                    "table",
                    f"PDF p.{page} table {ordinal}",
                    text,
                    {
                        "parser": "mineru",
                        "page_number": page,
                        "table_html": html,
                        "caption": caption,
                    },
                )
            )
        elif block_type == "image":
            caption = " ".join(
                _as_list(block.get("image_caption")) + _as_list(block.get("image_footnote"))
            ).strip()
            if not caption:
                continue
            elements.append(
                _element(
                    source_id,
                    "image_caption",
                    f"PDF p.{page} image {ordinal}",
                    caption,
                    {"parser": "mineru", "page_number": page},
                )
            )
        else:
            # Lists and any other block types: keep whatever text is present.
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            elements.append(
                _element(
                    source_id,
                    "paragraph",
                    f"PDF p.{page} block {ordinal}",
                    text,
                    {"parser": "mineru", "page_number": page, "block_type": block_type},
                )
            )
    return elements


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [str(value).strip()]
    return []


def _strip_math_delimiters(text: str) -> str:
    cleaned = text.strip()
    for marker in ("$$", "\\[", "\\]", "$"):
        cleaned = cleaned.replace(marker, " ")
    return " ".join(cleaned.split())


def _html_table_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?i)</t[dh]>", " | ", html)
    text = re.sub(r"(?i)</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    rows = [" ".join(row.split()).strip(" |") for row in text.splitlines()]
    return " ; ".join(row for row in rows if row)


def _element(
    source_id: str,
    element_type: str,
    location_label: str,
    text: str,
    metadata: Dict[str, Any],
) -> SourceElement:
    # 代码块/表格保真（保留换行/结构）；其余压平空白。
    if element_type in ("code_block", "table"):
        clean_text = text.strip("\n")
    else:
        clean_text = " ".join(text.split())
    return SourceElement(
        id="",
        source_id=source_id,
        element_type=element_type,
        location_label=location_label,
        text=clean_text,
        metadata=metadata,
    )
