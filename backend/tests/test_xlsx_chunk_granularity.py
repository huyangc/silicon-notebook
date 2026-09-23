"""用户反馈：400 行 × 十几列的 Excel，里面的文字检索不到。

复现假设：MinerU 把整个 sheet 出成**一个** table 元素，而 build_chunks 从不切分
单个元素，于是整张表变成一个十万字级的 chunk：向量只嵌入前 embed_truncate_chars 字；即便
ILIKE 把它捞回来，回答上下文也只截取前 chunk_answer_budget_chars 字，靠后的行永远
到不了模型；PG trgm similarity 对它也偏低（实测 0.0098，同内容小 chunk 0.073）。
openpyxl 兜底路径逐行出元素，作为对照组。
"""
from pathlib import Path

from app.core.config import Settings
from app.services.chunking import build_chunks

from tests.test_parsers_office import FakeMineru, _parse_via_chain

ROWS = 400
COLS = 15
NEEDLE_ROW = 350  # 表格靠后的一行，用户要找的文字在这里
NEEDLE = "苏州工业园区星湖街328号"


def _cell(row: int, col: int) -> str:
    if row == NEEDLE_ROW and col == 7:
        return NEEDLE
    return f"第{row}行第{col}列的业务数据内容"


def _make_workbook(path: Path) -> Path:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "明细"
    sheet.append([f"字段{col}" for col in range(COLS)])
    for row in range(1, ROWS + 1):
        sheet.append([_cell(row, col) for col in range(COLS)])
    book.save(str(path))
    return path


def _mineru_one_table_per_sheet() -> list[dict]:
    header = "<tr>" + "".join(f"<td>字段{c}</td>" for c in range(COLS)) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(r, c)}</td>" for c in range(COLS)) + "</tr>"
        for r in range(1, ROWS + 1)
    )
    return [{
        "type": "table",
        "table_body": f"<table>{header}{body}</table>",
        "table_caption": ["明细"],
        "page_idx": 0,
    }]


def _chunks(elements):
    settings = Settings()
    rows = [
        {"id": f"e{i}", "element_type": e.element_type, "text": e.text}
        for i, e in enumerate(elements)
    ]
    return build_chunks(rows, target_chars=settings.chunk_target_chars), settings


def _needle_is_retrievable(chunks, settings) -> tuple[bool, dict]:
    hit = [c for c in chunks if NEEDLE in c["text"]]
    assert len(hit) == 1
    chunk = hit[0]
    offset = chunk["text"].index(NEEDLE)
    receipt = {
        "chunks": len(chunks),
        "max_chunk_chars": max(len(c["text"]) for c in chunks),
        "needle_chunk_chars": len(chunk["text"]),
        "needle_offset": offset,
        "embed_truncate_chars": settings.embed_truncate_chars,
        "chunk_answer_budget_chars": settings.chunk_answer_budget_chars,
    }
    # 三道关都要过：向量只嵌入前 embed_truncate_chars 字；回答上下文把 chunk 截到
    # chunk_answer_budget_chars（整批共享的预算，这里只算它独占的最好情况）；chunk
    # 本身不能大到让 trgm similarity 排序把它压到底。
    ok = (
        offset + len(NEEDLE) <= settings.embed_truncate_chars
        and offset + len(NEEDLE) <= settings.chunk_answer_budget_chars
        and len(chunk["text"]) <= settings.embed_truncate_chars
    )
    return ok, receipt


def test_openpyxl_path_keeps_late_rows_retrievable(tmp_path):
    path = _make_workbook(tmp_path / "wide.xlsx")
    elements = _parse_via_chain("s1", path, "wide.xlsx", FakeMineru(configured=False))
    assert all(e.metadata.get("parser") == "xlsx" for e in elements)
    ok, receipt = _needle_is_retrievable(*_chunks(elements))
    print("openpyxl:", receipt)
    assert ok, receipt


def test_mineru_path_keeps_late_rows_retrievable(tmp_path):
    path = _make_workbook(tmp_path / "wide.xlsx")
    client = FakeMineru(content_list=_mineru_one_table_per_sheet())
    elements = _parse_via_chain("s1", path, "wide.xlsx", client)
    assert all(e.metadata.get("parser") == "mineru" for e in elements)  # 对账通过、被采信

    tables = [e for e in elements if e.element_type == "table"]
    assert len(tables) > 1  # 解析层已把超长表切成多段
    groups = {e.metadata.get("table_group") for e in tables}
    assert len(groups) == 1 and next(iter(groups))  # 同一张表各段 table_group 一致
    assert tables[0].metadata["table_html"].count("字段0") == 1  # 表头只在第 1 段 html 里
    header_text = "明细 字段0 | 字段1"  # caption（明细）拼在最前，其后紧跟表头
    for part in tables[1:]:
        assert "字段0" not in part.metadata["table_html"]           # 后段 html 不重复表头
        assert part.text.startswith(header_text)                    # 后段检索文本以表头开头

    ok, receipt = _needle_is_retrievable(*_chunks(elements))
    print("mineru:", receipt)
    assert ok, receipt
