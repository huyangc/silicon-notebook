from app.services.chunking import build_chunks


def _el(eid, typ, text):
    return {"id": eid, "element_type": typ, "text": text}


def test_merges_small_elements_to_target():
    # 5 个 ~150 字段落, target 600 → 合并成 ~2 个 chunk
    els = [_el(f"e{i}", "paragraph", "x" * 150) for i in range(5)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert 2 <= len(chunks) <= 3
    # 每个 chunk 记录其 element_ids
    assert all(c["element_ids"] for c in chunks)
    # 所有 element 都被覆盖
    covered = [eid for c in chunks for eid in c["element_ids"]]
    assert covered == [f"e{i}" for i in range(5)]


def test_heading_becomes_section_label_not_body():
    els = [_el("h1", "heading", "3 Architecture"),
           _el("p1", "paragraph", "y" * 300)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == "3 Architecture"
    # heading 文本作为标签拼进 chunk 文本, heading 自身不在 element_ids
    assert "3 Architecture" in chunks[0]["text"]
    assert chunks[0]["element_ids"] == ["p1"]


def test_heading_cuts_chunk_boundary():
    # heading 切断: 前后 paragraph 属不同 section → 不同 chunk
    els = [_el("p1", "paragraph", "a" * 200), _el("h1", "heading", "Sec B"),
           _el("p2", "paragraph", "b" * 200)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 2
    assert chunks[0]["element_ids"] == ["p1"]
    assert chunks[1]["element_ids"] == ["p2"] and chunks[1]["section_path"] == "Sec B"


def test_skips_image_and_empty():
    els = [_el("img", "image", "fig.png"), _el("e", "figure", ""),
           _el("p1", "paragraph", "real content here")]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["element_ids"] == ["p1"]


def test_oversize_element_becomes_own_chunk():
    els = [_el("big", "paragraph", "z" * 2000)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1 and chunks[0]["element_ids"] == ["big"]
