from types import SimpleNamespace

from app.services.source_element_selection import (
    deduplicate_source_chunks_in_order,
    deduplicate_repeated_page_boundaries,
    rank_source_chunks,
    rank_source_elements,
)


def _element(eid, source, page, text, score=0.0, element_type="paragraph"):
    return SimpleNamespace(
        element_id=eid,
        source_id=source,
        element_type=element_type,
        text=text,
        score=score,
        metadata={"page_number": page},
    )


def test_ingestion_deduplicates_only_repeated_page_boundary_artifacts():
    elements = []
    for page in range(1, 7):
        # The repeated title is a running footer; every body remains distinct.
        elements.extend([
            _element(f"body-{page}", "s", page, f"Body paragraph {page}."),
            _element(f"footer-{page}", "s", page,
                     "Cosmos 3: Omnimodal World Models for Physical AI"),
        ])
    # Same text away from a page boundary is authored content, not layout noise.
    elements.insert(1, _element("body-repeat", "s", 1, "Body paragraph 2."))
    elements.append(_element("formula-a", "s", 6, "E = mc^2", element_type="formula"))
    elements.append(_element("formula-b", "s", 6, "E = mc^2", element_type="formula"))

    kept, suppressed = deduplicate_repeated_page_boundaries(elements)

    assert suppressed == 5
    assert sum("Cosmos 3:" in item.text for item in kept) == 1
    assert sum(item.text == "Body paragraph 2." for item in kept) == 2
    assert sum(item.text == "E = mc^2" for item in kept) == 2


def test_ingestion_keeps_low_coverage_boundary_repetitions():
    elements = []
    for page in range(1, 7):
        elements.append(_element(f"body-{page}", "s", page, f"Body {page}"))
        if page <= 2:
            elements.append(_element(f"note-{page}", "s", page, "Repeated note"))

    kept, suppressed = deduplicate_repeated_page_boundaries(elements)

    assert suppressed == 0
    assert [item.element_id for item in kept] == [item.element_id for item in elements]


def test_ingestion_cleans_builtin_pdf_page_text_boundaries():
    elements = []
    for page in range(1, 5):
        elements.extend([
            _element(
                f"body-{page}", "s", page, f"Built-in PDF body {page}",
                element_type="page_text",
            ),
            _element(
                f"footer-{page}", "s", page, "Repeated PDF footer",
                element_type="page_text",
            ),
        ])

    kept, suppressed = deduplicate_repeated_page_boundaries(elements)

    assert suppressed == 3
    assert sum(item.text == "Repeated PDF footer" for item in kept) == 1
    assert sum(item.text.startswith("Built-in PDF body") for item in kept) == 4


def test_ranked_elements_deduplicate_before_cap_but_preserve_cross_source_provenance():
    candidates = [
        _element("h2", "paper-a", 2, "  PAPER\nTITLE ", 0.94),
        _element("h1", "paper-a", 1, "Paper title", 0.95),
        _element("abstract", "paper-a", 1, "We introduce the model.", 0.70),
        _element("other-source", "paper-b", 1, "Paper title", 0.60),
    ]

    ranked = rank_source_elements(candidates, 3)

    assert [item.element_id for item in ranked] == [
        "h1", "abstract", "other-source",
    ]


def test_ranked_chunks_deduplicate_before_cap():
    chunks = [
        SimpleNamespace(chunk_id="h1", source_id="s", text="Paper title",
                        relevance=0.95),
        SimpleNamespace(chunk_id="h2", source_id="s", text=" paper  title ",
                        relevance=0.94),
        SimpleNamespace(chunk_id="abstract", source_id="s",
                        text="We introduce the model.", relevance=0.70),
    ]

    ranked = rank_source_chunks(chunks, 2)

    assert [item.chunk_id for item in ranked] == ["h1", "abstract"]


def test_ordered_chunk_dedup_keeps_exact_section_order():
    chunks = [
        SimpleNamespace(chunk_id="first", source_id="s", text="Header"),
        SimpleNamespace(chunk_id="body", source_id="s", text="Body"),
        SimpleNamespace(chunk_id="repeat", source_id="s", text=" header "),
    ]

    distinct = deduplicate_source_chunks_in_order(chunks)

    assert [item.chunk_id for item in distinct] == ["first", "body"]
