from app.services.parsers import mineru_content_list_to_elements


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
