from app.services.chunking import build_chunks


def _el(eid, typ, text):
    return {"id": eid, "element_type": typ, "text": text}


def _el_sp(eid, typ, text, section_path):
    return {"id": eid, "element_type": typ, "text": text, "section_path": section_path}


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


def test_image_with_caption_enters_chunk():
    els = [
        {"id": "e1", "element_type": "image", "text": "Figure 1: the layout", "caption": "Figure 1: the layout"},
        {"id": "e2", "element_type": "paragraph", "text": "Body."},
    ]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    joined = " ".join(c["text"] for c in chunks)
    assert "Figure 1: the layout" in joined          # 带图注的图进了检索
    assert "e1" in [i for c in chunks for i in c["element_ids"]]


def test_image_without_caption_is_skipped():
    els = [
        {"id": "e1", "element_type": "image", "text": "PDF p.3 图 2"},   # 占位文本, 无 caption 键
        {"id": "e2", "element_type": "paragraph", "text": "Body."},
    ]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    ids = [i for c in chunks for i in c["element_ids"]]
    assert "e1" not in ids                              # 无图注的图仍被跳过
    assert "e2" in ids


def test_heading_section_path_breadcrumb_used_when_present():
    # 子标题 Arguments 带完整面包屑 → section_path 列不再被子标题自身文本覆盖,
    # 命令名(set_db)保留在第二个 chunk 的 section_path 里。打分文本的前缀刻意
    # 只取尾部两段(父级 > 叶子):完整面包屑会把文档/章级 token 白送给整棵子树
    # (keyword_score 是集合覆盖率,零稀释代价),父级+叶子已让命令名进入子块的
    # 可检索文本,更深的定位归 section_path 列。
    els = [
        _el_sp("h1", "heading", "set_db", "Manual > Commands > set_db"),
        _el("p1", "paragraph", "x" * 200),
        _el_sp("h2", "heading", "Arguments", "Manual > Commands > set_db > Arguments"),
        _el("t1", "table_row", "name | type | desc"),
    ]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 2
    assert chunks[0]["section_path"] == "Manual > Commands > set_db"
    assert chunks[0]["text"].startswith("[Commands > set_db] ")
    assert chunks[1]["section_path"] == "Manual > Commands > set_db > Arguments"
    assert chunks[1]["text"].startswith("[set_db > Arguments] ")
    assert "Manual" not in chunks[1]["text"]   # 文档级 token 不进打分文本
    assert chunks[1]["element_ids"] == ["t1"]


def test_heading_breadcrumb_empty_leaf_segments_are_dropped():
    # 空标题/纯图片标题会产生 "Commands > " 这类尾段为空的面包屑;空段剔除,
    # 不产生悬挂分隔符,标签与前缀都落到剩余非空段。
    els = [_el_sp("h1", "heading", "", "Commands > "),
           _el("p1", "paragraph", "y" * 300)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == "Commands"
    assert chunks[0]["text"].startswith("[Commands] ")


def test_heading_breadcrumb_all_empty_falls_back_to_heading_text():
    # 面包屑全空段 → 回退标题自身文本;标题也为空 → 无前缀(与现状一致)。
    els = [_el_sp("h1", "heading", "", " > "),
           _el("p1", "paragraph", "y" * 300)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == ""
    assert not chunks[0]["text"].startswith("[")


def test_heading_section_path_empty_string_falls_back_to_heading_text():
    # section_path 键存在但为空串 → 与现状(section=标题自身)逐字节相同
    els = [_el_sp("h1", "heading", "3 Architecture", ""),
           _el("p1", "paragraph", "y" * 300)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == "3 Architecture"
    assert chunks[0]["text"].startswith("[3 Architecture]")


def test_heading_without_section_path_key_falls_back_to_heading_text():
    # 无 section_path 键(MinerU heading / 旧库存量行)→ 行为与现状逐字节相同
    els = [_el("h1", "heading", "3 Architecture"),
           _el("p1", "paragraph", "y" * 300)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == "3 Architecture"
    assert chunks[0]["text"].startswith("[3 Architecture]")
