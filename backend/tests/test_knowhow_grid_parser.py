import io

import pytest
from openpyxl import Workbook

from app.services.knowhow.grid_parser import GridParseError, ParsedGrid, guess_roles, parse_grid


def _xlsx_bytes(sheets: list[list[list[str]]]) -> bytes:
    """Build real xlsx bytes with openpyxl. First list is the first sheet."""
    wb = Workbook()
    ws = wb.active
    for row in sheets[0]:
        ws.append(row)
    for extra_rows in sheets[1:]:
        extra = wb.create_sheet()
        for row in extra_rows:
            extra.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _assert_chinese_message(err: Exception) -> None:
    message = str(err)
    assert message
    assert any("一" <= ch <= "鿿" for ch in message)


# --- parse_grid: xlsx ---------------------------------------------------


def test_parse_grid_xlsx_basic():
    header = ["违例概念", "现象识别方法", "根因分析动作", "修复方法", "依赖工具"]
    data_row = ["天线效应", "金属密度超标告警", "多晶硅未及时接地释放电荷", "插入跳线二极管", "Calibre DRC"]
    data = _xlsx_bytes([[header, data_row]])

    grid = parse_grid("rules.xlsx", data)

    assert isinstance(grid, ParsedGrid)
    assert grid.columns == header
    assert grid.rows == [data_row]


def test_parse_grid_xlsm_suffix_uses_same_parser():
    header = ["a", "b"]
    data_row = ["1", "2"]
    data = _xlsx_bytes([[header, data_row]])

    grid = parse_grid("rules.xlsm", data)

    assert grid.columns == header
    assert grid.rows == [data_row]


def test_parse_grid_xlsx_reads_first_sheet_only():
    first_sheet = [["a", "b"], ["1", "2"]]
    second_sheet = [["x", "y"], ["9", "9"]]
    data = _xlsx_bytes([first_sheet, second_sheet])

    grid = parse_grid("rules.xlsx", data)

    assert grid.columns == ["a", "b"]
    assert grid.rows == [["1", "2"]]


def test_parse_grid_xlsx_none_cells_become_empty_string():
    wb = Workbook()
    ws = wb.active
    ws.append(["h1", "h2", "h3"])
    ws.append(["a1", None, "a3"])
    buf = io.BytesIO()
    wb.save(buf)

    grid = parse_grid("rules.xlsx", buf.getvalue())

    assert grid.rows == [["a1", "", "a3"]]


def test_parse_grid_xlsx_non_string_cells_are_stringified():
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "count", "ratio"])
    ws.append(["widget", 42, 3.5])
    buf = io.BytesIO()
    wb.save(buf)

    grid = parse_grid("rules.xlsx", buf.getvalue())

    assert grid.rows == [["widget", "42", "3.5"]]


# --- parse_grid: csv ------------------------------------------------------


def test_parse_grid_csv_basic_with_bom():
    text = "h1,h2\na,b\n"
    data = text.encode("utf-8-sig")  # BOM-prefixed, must decode cleanly

    grid = parse_grid("rules.csv", data)

    assert grid.columns == ["h1", "h2"]
    assert grid.rows == [["a", "b"]]


def test_parse_grid_csv_row_length_mismatch_pads_and_truncates():
    text = "h1,h2,h3\na1,a2\nb1,b2,b3,b4\n"
    data = text.encode("utf-8")

    grid = parse_grid("rules.csv", data)

    assert grid.columns == ["h1", "h2", "h3"]
    assert grid.rows == [["a1", "a2", ""], ["b1", "b2", "b3"]]


# --- parse_grid: markdown --------------------------------------------------


def test_parse_grid_md_basic():
    text = (
        "| 违例概念 | 现象识别方法 |\n"
        "| --- | --- |\n"
        "| 天线效应 | 金属密度超标 |\n"
    )

    grid = parse_grid("rules.md", text.encode("utf-8"))

    assert grid.columns == ["违例概念", "现象识别方法"]
    assert grid.rows == [["天线效应", "金属密度超标"]]


def test_parse_grid_markdown_suffix_alias():
    text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"

    grid = parse_grid("rules.markdown", text.encode("utf-8"))

    assert grid.columns == ["a", "b"]
    assert grid.rows == [["1", "2"]]


def test_parse_grid_md_drops_alignment_separator_variants():
    text = (
        "| A | B | C |\n"
        "| :--- | ---: | :---: |\n"
        "| 1 | 2 | 3 |\n"
    )

    grid = parse_grid("rules.md", text.encode("utf-8"))

    assert grid.columns == ["A", "B", "C"]
    assert grid.rows == [["1", "2", "3"]]


def test_parse_grid_md_escaped_pipe_and_br():
    text = (
        "| 概念 | 说明 |\n"
        "| --- | --- |\n"
        "| A\\|B | line1<br>line2<br/>line3 |\n"
    )

    grid = parse_grid("rules.md", text.encode("utf-8"))

    assert grid.columns == ["概念", "说明"]
    assert grid.rows == [["A|B", "line1\nline2\nline3"]]


def test_parse_grid_md_ignores_non_pipe_lines():
    text = (
        "# Title\n"
        "Some prose before the table.\n"
        "| h1 | h2 |\n"
        "| --- | --- |\n"
        "| a | b |\n"
        "Trailing prose.\n"
    )

    grid = parse_grid("rules.md", text.encode("utf-8"))

    assert grid.columns == ["h1", "h2"]
    assert grid.rows == [["a", "b"]]


def test_parse_grid_md_row_length_mismatch_pads_and_truncates():
    text = (
        "| h1 | h2 | h3 |\n"
        "| --- | --- | --- |\n"
        "| a1 | a2 |\n"
        "| b1 | b2 | b3 | b4 |\n"
    )

    grid = parse_grid("rules.md", text.encode("utf-8"))

    assert grid.columns == ["h1", "h2", "h3"]
    assert grid.rows == [["a1", "a2", ""], ["b1", "b2", "b3"]]


# --- validation errors ------------------------------------------------------


def test_parse_grid_empty_csv_raises_grid_parse_error():
    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.csv", b"")
    assert isinstance(excinfo.value, ValueError)
    _assert_chinese_message(excinfo.value)


def test_parse_grid_md_without_any_table_raises_grid_parse_error():
    text = "just some prose, no pipe table here.\n"
    with pytest.raises(GridParseError):
        parse_grid("rules.md", text.encode("utf-8"))


def test_parse_grid_duplicate_column_names_raise():
    text = "h1,h1,h3\na,b,c\n"
    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.csv", text.encode("utf-8"))
    _assert_chinese_message(excinfo.value)


def test_parse_grid_zero_data_rows_raise():
    text = "h1,h2,h3\n"
    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.csv", text.encode("utf-8"))
    _assert_chinese_message(excinfo.value)


def test_parse_grid_unsupported_suffix_raises():
    with pytest.raises(GridParseError):
        parse_grid("rules.txt", b"whatever")


def test_parse_grid_csv_gbk_encoded_chinese_falls_back_and_decodes():
    # Chinese Excel exports CSV as GBK/ANSI by default, not UTF-8.
    text = "概念,说明\n违例,超标\n"
    data = text.encode("gbk")

    grid = parse_grid("rules.csv", data)

    assert grid.columns == ["概念", "说明"]
    assert grid.rows == [["违例", "超标"]]


def test_parse_grid_csv_undecodable_bytes_raise_friendly_error():
    # Fails both utf-8-sig and gbk decoding.
    data = b"\xff\xff\xff\xff"

    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.csv", data)

    assert str(excinfo.value) == "无法识别文件编码，请将文件另存为 UTF-8 编码后重试"


def test_parse_grid_xlsx_corrupted_bytes_raise_friendly_error():
    data = b"this is not a real xlsx file, just garbage bytes"

    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.xlsx", data)

    assert str(excinfo.value) == "无法读取 Excel 文件，请确认文件未损坏且为 .xlsx 格式"


# --- guess_roles -------------------------------------------------------------


def test_guess_roles_full_role_set():
    columns = ["违例概念", "现象识别方法", "根因分析动作", "修复方法", "依赖工具"]
    assert guess_roles(columns) == ["concept", "identify", "root_cause", "fix", "tool"]


def test_guess_roles_no_match_promotes_first_column_to_concept():
    columns = ["列一", "列二", "列三"]
    assert guess_roles(columns) == ["concept", "plain", "plain"]


def test_guess_roles_keeps_only_first_concept_candidate():
    columns = ["违例类型", "根因分析", "概念说明"]
    # 1st and 3rd both hit "concept" keywords; only the first should stay "concept".
    assert guess_roles(columns) == ["concept", "root_cause", "plain"]
