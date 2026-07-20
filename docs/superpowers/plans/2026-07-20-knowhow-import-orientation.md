# Knowhow Import Orientation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users importing a new Knowhow table choose whether source attributes are arranged by column or by row, while normalizing both forms to the existing internal column-attribute grid.

**Architecture:** Add a request-scoped `orientation=columns|rows` protocol to the existing preview and commit endpoints. `grid_parser.py` will normalize the raw extracted matrix before the existing grid validation; every downstream component will continue to consume the same `ParsedGrid`. The frontend wizard will own the explicit user choice and send it unchanged to both requests.

**Tech Stack:** Python 3.11+, FastAPI multipart forms, pytest, TypeScript 5.7, React 19, Next.js 15, Node's built-in test runner.

## Global Constraints

- Change only the “import and create a new Knowhow table” flow; append import remains column-oriented and unchanged.
- Protocol values are exactly `columns` and `rows`; Chinese labels are UI copy, not API values.
- `orientation` defaults to `columns` on the backend so existing clients remain compatible.
- Preview and commit must reparse the same original file with the same orientation; the client never submits a transformed matrix.
- `rows` normalization happens after raw xlsx/csv/Markdown extraction and before `_build_grid`.
- The normalized first column is the default row-title suggestion for `rows`, but the user may change or clear it.
- No database field, schema migration, or persisted orientation is added.
- Follow TDD: add one focused failing behavior test, run it red, implement the minimum, then run it green before adding the next behavior.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together, plus `architecture.md` and `fangan_done.md`.
- Do not mark the feature complete until `scripts/check.sh` and `npm --prefix frontend run build` pass.

---

## File Structure

- `backend/app/services/knowhow/grid_parser.py`: validate the orientation and normalize raw matrices before building `ParsedGrid`.
- `backend/app/services/knowhow/api.py`: thread orientation through preview/import orchestration and override the row-title suggestion for row-oriented input.
- `backend/app/api/routes.py`: expose the optional multipart field on both new-table import endpoints.
- `backend/tests/test_knowhow_grid_parser.py`: pin format-independent transposition, ragged rows, validation, and compatibility.
- `backend/tests/test_knowhow_api.py`: pin HTTP preview/commit behavior and default compatibility.
- `frontend/app/knowhow-model.ts`: define the frontend protocol type and attach orientation to both multipart requests.
- `frontend/app/knowhow-model.test.mjs`: pin the actual `FormData` contract.
- `frontend/app/knowhow-import-logic.ts`: own the two UI choices, descriptions, and default.
- `frontend/app/knowhow-import.test.mjs`: pin option copy/default and the back-navigation retention contract.
- `frontend/app/knowhow-import.tsx`: render the direction selector, preserve it on back navigation, and pass it to preview/commit.
- `README.md`, `README_zh.md`, `AGENTS.md`, `architecture.md`, `fangan_done.md`: synchronize shipped product behavior and architecture.

---

### Task 1: Normalize Raw Knowhow Grids by Import Orientation

**Files:**

- Modify: `backend/app/services/knowhow/grid_parser.py:17-123`
- Test: `backend/tests/test_knowhow_grid_parser.py:1-453`

**Interfaces:**

- Consumes: raw `list[list[str]]` from `_extract_xlsx_rows`, `_extract_csv_rows`, or `_extract_markdown_rows`.
- Produces: `parse_grid(filename: str, data: bytes, orientation: str = "columns") -> ParsedGrid`.
- Produces: `IMPORT_ORIENTATIONS = frozenset({"columns", "rows"})`.
- Invariant: `_build_grid` receives only the normalized column-attribute matrix.

- [ ] **Step 1: Add a failing invalid-orientation test**

Add this validation test in `backend/tests/test_knowhow_grid_parser.py`:

```python
def test_parse_grid_rejects_unknown_orientation():
    with pytest.raises(GridParseError) as excinfo:
        parse_grid(
            "rules.csv",
            b"name,value\nA,1\n",
            orientation="diagonal",
        )
    assert str(excinfo.value) == "非法的属性排列方式"
```

- [ ] **Step 2: Run the invalid-orientation test and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py::test_parse_grid_rejects_unknown_orientation -q
```

Expected: FAIL because `parse_grid()` does not accept `orientation`.

- [ ] **Step 3: Add the protocol value validation without transposing yet**

Add above `parse_grid` and change its signature:

```python
IMPORT_ORIENTATIONS = frozenset({"columns", "rows"})


def parse_grid(
    filename: str, data: bytes, orientation: str = "columns"
) -> ParsedGrid:
    if orientation not in IMPORT_ORIENTATIONS:
        raise GridParseError("非法的属性排列方式")
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        raw_rows = _extract_xlsx_rows(data)
    elif suffix == ".csv":
        raw_rows = _extract_csv_rows(data)
    elif suffix in (".md", ".markdown"):
        raw_rows = _extract_markdown_rows(data)
    else:
        raise GridParseError(f"不支持的表格文件类型：{suffix or filename}")
    return _build_grid(raw_rows)
```

Run the Step 2 command again.

Expected: PASS.

- [ ] **Step 4: Add a failing canonical transposition test for all supported format families**

Add this after the Markdown parser tests in `backend/tests/test_knowhow_grid_parser.py`:

```python
@pytest.mark.parametrize(
    ("filename", "data"),
    [
        (
            "rules.xlsx",
            _xlsx_bytes(
                [[
                    ["属性", "记录 A", "记录 B"],
                    ["现象", "A 现象", "B 现象"],
                    ["根因", "A 根因", "B 根因"],
                ]]
            ),
        ),
        (
            "rules.xlsm",
            _xlsx_bytes(
                [[
                    ["属性", "记录 A", "记录 B"],
                    ["现象", "A 现象", "B 现象"],
                    ["根因", "A 根因", "B 根因"],
                ]]
            ),
        ),
        (
            "rules.csv",
            "属性,记录 A,记录 B\n现象,A 现象,B 现象\n根因,A 根因,B 根因\n".encode("utf-8"),
        ),
        (
            "rules.md",
            (
                "| 属性 | 记录 A | 记录 B |\n"
                "| --- | --- | --- |\n"
                "| 现象 | A 现象 | B 现象 |\n"
                "| 根因 | A 根因 | B 根因 |\n"
            ).encode("utf-8"),
        ),
    ],
)
def test_parse_grid_rows_orientation_transposes_to_column_attributes(filename, data):
    grid = parse_grid(filename, data, orientation="rows")

    assert grid.columns == ["属性", "现象", "根因"]
    assert grid.rows == [
        ["记录 A", "A 现象", "A 根因"],
        ["记录 B", "B 现象", "B 根因"],
    ]
```

- [ ] **Step 5: Run the canonical test and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py::test_parse_grid_rows_orientation_transposes_to_column_attributes -q
```

Expected: FAIL because `orientation="rows"` still returns the original layout.

- [ ] **Step 6: Implement the minimal rectangular transposition**

Add the helper above `parse_grid`:

```python
def _normalize_orientation(
    raw_rows: list[list[str]], orientation: str
) -> list[list[str]]:
    if orientation == "columns" or not raw_rows:
        return raw_rows
    return [list(column) for column in zip(*raw_rows)]
```

At the end of `parse_grid`, normalize before `_build_grid`:

```python
    normalized_rows = _normalize_orientation(raw_rows, orientation)
    return _build_grid(normalized_rows)
```

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py::test_parse_grid_rows_orientation_transposes_to_column_attributes -q
```

Expected: `4 passed`.

- [ ] **Step 7: Add a failing ragged-row transposition test**

```python
def test_parse_grid_rows_orientation_pads_ragged_rows_before_transpose():
    data = (
        "属性,记录 A,记录 B\n"
        "现象,A 现象\n"
        "根因,A 根因,B 根因\n"
    ).encode("utf-8")

    grid = parse_grid("rules.csv", data, orientation="rows")

    assert grid.columns == ["属性", "现象", "根因"]
    assert grid.rows == [
        ["记录 A", "A 现象", "A 根因"],
        ["记录 B", "", "B 根因"],
    ]
```

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py::test_parse_grid_rows_orientation_pads_ragged_rows_before_transpose -q
```

Expected: FAIL because plain `zip(*raw_rows)` truncates at the shortest raw row.

- [ ] **Step 8: Pad raw rows before transposition**

Replace `_normalize_orientation` with:

```python
def _normalize_orientation(
    raw_rows: list[list[str]], orientation: str
) -> list[list[str]]:
    if orientation == "columns" or not raw_rows:
        return raw_rows
    width = max((len(row) for row in raw_rows), default=0)
    rectangular = [
        row + [""] * (width - len(row))
        for row in raw_rows
    ]
    return [list(column) for column in zip(*rectangular)]
```

Run the Step 7 command again.

Expected: PASS.

- [ ] **Step 9: Add failing normalized-header and actionable-hint tests**

Append:

```python
@pytest.mark.parametrize(
    ("text", "message_fragment"),
    [
        ("属性,记录 A\n,A 现象\n", "空列名"),
        ("属性,记录 A\n现象,A\n现象,B\n", "重复列名"),
        ("属性\n现象\n根因\n", "没有数据行"),
    ],
)
def test_parse_grid_rows_orientation_reuses_normalized_header_validation(
    text, message_fragment
):
    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.csv", text.encode("utf-8"), orientation="rows")
    assert message_fragment in str(excinfo.value)


def test_parse_grid_column_orientation_points_transposed_shape_to_ui_choice():
    data = "属性,,\n现象,A,B\n根因,C,D\n".encode("utf-8")

    with pytest.raises(GridParseError) as excinfo:
        parse_grid("rules.csv", data, orientation="columns")

    assert str(excinfo.value) == (
        "这张表看起来是属性按行排列，"
        "请返回并选择“属性按行”后重新导入。"
    )


def test_parse_grid_default_orientation_remains_columns():
    data = b"name,value\nA,1\n"
    assert parse_grid("rules.csv", data) == parse_grid(
        "rules.csv", data, orientation="columns"
    )
```

- [ ] **Step 10: Run the new validation tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py::test_parse_grid_rows_orientation_reuses_normalized_header_validation backend/tests/test_knowhow_grid_parser.py::test_parse_grid_column_orientation_points_transposed_shape_to_ui_choice -q
```

Expected: FAIL because a single empty normalized header is not rejected and the old hint still asks for manual Excel transposition.

- [ ] **Step 11: Tighten normalized-header validation and update the hint**

Change `_blank_header_error` and `_build_grid`:

```python
def _blank_header_error(
    raw_rows: list[list[str]], *, suggest_rows_orientation: bool
) -> str:
    if suggest_rows_orientation and _looks_transposed(raw_rows):
        return (
            "这张表看起来是属性按行排列，"
            "请返回并选择“属性按行”后重新导入。"
        )
    return "表头存在空列名，请补齐缺失的列名后再导入。"


def _build_grid(
    raw_rows: list[list[str]], *, suggest_rows_orientation: bool = False
) -> ParsedGrid:
    if not raw_rows or not raw_rows[0]:
        raise GridParseError("表格缺少表头：文件为空，或首行没有任何列名")

    columns = raw_rows[0]
    seen: set[str] = set()
    for name in columns:
        if not name.strip():
            raise GridParseError(
                _blank_header_error(
                    raw_rows,
                    suggest_rows_orientation=suggest_rows_orientation,
                )
            )
        if name in seen:
            raise GridParseError(f"表头存在重复列名：{name!r}")
        seen.add(name)

    width = len(columns)
    normalized_rows: list[list[str]] = []
    for row in raw_rows[1:]:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        if not any(cell.strip() for cell in row):
            continue
        normalized_rows.append(row)

    if not normalized_rows:
        raise GridParseError("表格没有数据行：表头之后未找到任何数据")

    return ParsedGrid(columns=columns, rows=normalized_rows)
```

Pass the hint flag at the end of `parse_grid`:

```python
    normalized_rows = _normalize_orientation(raw_rows, orientation)
    return _build_grid(
        normalized_rows,
        suggest_rows_orientation=orientation == "columns",
    )
```

Update the affected docstrings to describe explicit in-product orientation selection instead of manual Excel transposition.

- [ ] **Step 12: Run the complete parser suite and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py -q
```

Expected: all tests in `test_knowhow_grid_parser.py` pass.

- [ ] **Step 13: Commit the parser slice**

```bash
git add backend/app/services/knowhow/grid_parser.py backend/tests/test_knowhow_grid_parser.py
git commit -m "feat: normalize knowhow import orientation"
```

---

### Task 2: Thread Orientation Through Preview and Commit APIs

**Files:**

- Modify: `backend/app/services/knowhow/api.py:57-73,220-252`
- Modify: `backend/app/api/routes.py:477-522`
- Test: `backend/tests/test_knowhow_api.py:83-330`

**Interfaces:**

- Consumes: `parse_grid(filename, data, orientation)`.
- Produces: `preview_import(filename, data, orientation="columns") -> dict`.
- Produces: `import_table(..., anchor_index=None, orientation="columns") -> str`.
- HTTP: multipart `orientation` on preview and commit, defaulting to `columns`.

- [ ] **Step 1: Add failing HTTP preview tests**

Add:

```python
def test_preview_rows_orientation_returns_normalized_grid_and_first_anchor(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000527")
    nb = _mk_notebook(client, owner_h)
    data = _xlsx_bytes(
        ["属性", "过冲问题", "欠冲问题"],
        [
            ["现象识别", "上升沿过冲", "下降沿欠冲"],
            ["根因分析", "电源阻抗高", "寄生电感大"],
        ],
    )

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview",
        headers=owner_h,
        files={
            "file": (
                "row-attributes.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"orientation": "rows"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [column["name"] for column in body["columns"]] == [
        "属性",
        "现象识别",
        "根因分析",
    ]
    assert body["rows_preview"] == [
        ["过冲问题", "上升沿过冲", "电源阻抗高"],
        ["欠冲问题", "下降沿欠冲", "寄生电感大"],
    ]
    assert body["total_rows"] == 2
    assert body["anchor_suggestion"] == 0


def test_preview_rejects_unknown_orientation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000528")
    nb = _mk_notebook(client, owner_h)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview",
        headers=owner_h,
        files={"file": ("rules.csv", b"name,value\nA,1\n", "text/csv")},
        data={"orientation": "diagonal"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "非法的属性排列方式"
```

- [ ] **Step 2: Run preview tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_api.py::test_preview_rows_orientation_returns_normalized_grid_and_first_anchor backend/tests/test_knowhow_api.py::test_preview_rejects_unknown_orientation -q
```

Expected: FAIL because the route ignores `orientation`.

- [ ] **Step 3: Implement preview threading and row-oriented anchor suggestion**

Replace `preview_import` in `backend/app/services/knowhow/api.py` with:

```python
def preview_import(
    filename: str, data: bytes, orientation: str = "columns"
) -> dict:
    grid = parse_grid(filename, data, orientation)
    kinds, guessed_anchor_index = guess_kinds(grid.columns)
    anchor_index = 0 if orientation == "rows" else guessed_anchor_index
    return {
        "columns": [
            {"name": name, "guessed_kind": kind}
            for name, kind in zip(grid.columns, kinds)
        ],
        "anchor_suggestion": anchor_index,
        "rows_preview": grid.rows[:5],
        "total_rows": len(grid.rows),
    }
```

Update the preview route:

```python
async def preview_knowhow_import(
    notebook_id: str,
    file: UploadFile = File(...),
    orientation: str = Form("columns"),
) -> KnowhowImportPreview:
    data = await file.read()
    try:
        return knowhow_api.preview_import(
            file.filename or "import",
            data,
            orientation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

- [ ] **Step 4: Run preview tests and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_api.py::test_preview_rows_orientation_returns_normalized_grid_and_first_anchor backend/tests/test_knowhow_api.py::test_preview_rejects_unknown_orientation -q
```

Expected: `2 passed`.

- [ ] **Step 5: Add a failing commit test and explicit legacy-default assertion**

Extend `_import_xlsx` with an optional orientation that is omitted when `None`:

```python
def _import_xlsx(
    client,
    headers,
    nb,
    *,
    header=HEADER,
    rows=None,
    title="时序修复表",
    columns_json=None,
    anchor_index=ANCHOR_INDEX,
    orientation=None,
):
    if rows is None:
        rows = DATA_ROWS
    data = _xlsx_bytes(header, rows)
    form = {"title": title, "columns_json": columns_json or _columns_json(header)}
    if anchor_index is not None:
        form["anchor_index"] = str(anchor_index)
    if orientation is not None:
        form["orientation"] = orientation
    return client.post(
        f"/api/notebooks/{nb}/knowhow/import",
        headers=headers,
        files={
            "file": (
                "rules.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data=form,
    )
```

Add:

```python
def test_import_rows_orientation_persists_the_normalized_preview_shape(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000529")
    nb = _mk_notebook(client, owner_h)
    normalized_header = ["属性", "现象识别", "根因分析"]
    normalized_kinds = ["attribute", "procedure", "procedure"]

    resp = _import_xlsx(
        client,
        owner_h,
        nb,
        header=["属性", "过冲问题", "欠冲问题"],
        rows=[
            ["现象识别", "上升沿过冲", "下降沿欠冲"],
            ["根因分析", "电源阻抗高", "寄生电感大"],
        ],
        columns_json=_columns_json(normalized_header, normalized_kinds),
        anchor_index=0,
        orientation="rows",
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [column["name"] for column in body["columns"]] == normalized_header
    assert [column["kind"] for column in body["columns"]] == [
        "anchor",
        "procedure",
        "procedure",
    ]
    column_ids = {
        column["name"]: column["id"]
        for column in body["columns"]
    }
    assert [
        [
            row["cells"][column_ids[name]]
            for name in normalized_header
        ]
        for row in body["rows"]
    ] == [
        ["过冲问题", "上升沿过冲", "电源阻抗高"],
        ["欠冲问题", "下降沿欠冲", "寄生电感大"],
    ]


def test_import_without_orientation_remains_column_oriented(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000530")
    nb = _mk_notebook(client, owner_h)

    resp = _import_xlsx(client, owner_h, nb, orientation=None)

    assert resp.status_code == 200, resp.text
    assert [column["name"] for column in resp.json()["columns"]] == HEADER
```

- [ ] **Step 6: Run commit tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_api.py::test_import_rows_orientation_persists_the_normalized_preview_shape backend/tests/test_knowhow_api.py::test_import_without_orientation_remains_column_oriented -q
```

Expected: the row-oriented test FAILS with a normalized column-count mismatch; the legacy-default test passes.

- [ ] **Step 7: Implement commit threading**

Append `orientation` after `anchor_index` to preserve existing positional callers:

```python
def import_table(
    repo: Any,
    notebook_id: str,
    filename: str,
    data: bytes,
    title: str,
    columns_json: str,
    anchor_index: "int | None" = None,
    orientation: str = "columns",
) -> str:
    grid = parse_grid(filename, data, orientation)
    columns = parse_import_columns(columns_json, grid, anchor_index)
    table_id = repo.create_knowhow_table(notebook_id, title, "", columns)
    column_ids = [
        column["id"]
        for column in repo.get_knowhow_table(table_id)["columns"]
    ]
    rows = (
        forward_fill_column(grid.rows, anchor_index)
        if anchor_index is not None
        else grid.rows
    )
    for row in rows:
        cells = {
            column_ids[index]: value
            for index, value in enumerate(row)
            if value
        }
        repo.add_knowhow_row(table_id, cells)
    repo.bump_knowhow_mutation_seq(table_id)
    return table_id
```

Update the route signature and call:

```python
async def import_knowhow_table(
    notebook_id: str,
    file: UploadFile = File(...),
    title: str = Form(...),
    columns_json: str = Form(...),
    anchor_index: Optional[int] = Form(None),
    orientation: str = Form("columns"),
) -> KnowhowTableDetail:
    repo = repository()
    data = await file.read()
    try:
        table_id = knowhow_api.import_table(
            repo,
            notebook_id,
            file.filename or "import",
            data,
            title,
            columns_json,
            anchor_index,
            orientation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return knowhow_api.to_wire_table(repo.get_knowhow_table(table_id))
```

Update route/service docstrings to state that `orientation` is request-scoped and both endpoints normalize before column validation.

- [ ] **Step 8: Run backend Knowhow import tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py backend/tests/test_knowhow_api.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit the API slice**

```bash
git add backend/app/services/knowhow/api.py backend/app/api/routes.py backend/tests/test_knowhow_api.py
git commit -m "feat: expose knowhow import orientation"
```

---

### Task 3: Pin the Frontend Multipart Orientation Contract

**Files:**

- Modify: `frontend/app/knowhow-model.ts:96-114,358-396`
- Test: `frontend/app/knowhow-model.test.mjs:1-31,721-763`

**Interfaces:**

- Produces: `type KnowhowImportOrientation = "columns" | "rows"`.
- Produces: `importKnowhowPreview(notebookId, file, orientation="columns")`.
- Produces: `importKnowhow(notebookId, file, title, columns, anchorIndex, orientation="columns")`.
- Invariant: both requests append `orientation` to `FormData`.

- [ ] **Step 1: Add failing FormData tests**

Import `importKnowhowPreview` at the top of `knowhow-model.test.mjs`, then add:

```javascript
test("importKnowhowPreview: multipart 携带用户选择的属性排列方式", () => {
  return withFetchStub(
    {
      columns: [],
      rows_preview: [],
      total_rows: 0,
      anchor_suggestion: null,
    },
    async (calls) => {
      await importKnowhowPreview(
        "nb-1",
        new Blob(["x"]),
        "rows",
      );
      const form = calls[0].init.body;
      assert.ok(form instanceof FormData);
      assert.strictEqual(form.get("orientation"), "rows");
    },
  );
});
```

Update the existing successful commit contract test call to pass `"rows"` after `anchorIndex`, then add:

```javascript
assert.strictEqual(form.get("orientation"), "rows");
```

Update the `anchorIndex=null` test call to pass `"columns"` and assert:

```javascript
assert.strictEqual(calls[0].init.body.get("orientation"), "columns");
```

- [ ] **Step 2: Run the model contract tests and verify RED**

Run:

```bash
node --test frontend/app/knowhow-model.test.mjs
```

Expected: FAIL because neither function accepts or appends orientation.

- [ ] **Step 3: Implement the frontend protocol type and multipart fields**

Add near `KnowhowImportPreview`:

```typescript
export type KnowhowImportOrientation = "columns" | "rows";
```

Update the preview client:

```typescript
export const importKnowhowPreview = (
  notebookId: string,
  file: File | Blob,
  orientation: KnowhowImportOrientation = "columns",
): Promise<KnowhowImportPreview> => {
  const form = new FormData();
  form.append("file", file);
  form.append("orientation", orientation);
  return apiFetch<WireImportPreview>(
    `/notebooks/${notebookId}/knowhow/import/preview`,
    {
      method: "POST",
      body: form,
    },
  ).then(mapPreview);
};
```

Update the commit client:

```typescript
export const importKnowhow = (
  notebookId: string,
  file: File | Blob,
  title: string,
  columns: { name: string; kind: ColumnKind }[],
  anchorIndex: number | null,
  orientation: KnowhowImportOrientation = "columns",
): Promise<KnowhowTableDetail> => {
  const form = new FormData();
  form.append("file", file);
  form.append("title", title);
  form.append("columns_json", JSON.stringify(columns));
  form.append("orientation", orientation);
  if (anchorIndex !== null) {
    form.append("anchor_index", String(anchorIndex));
  }
  return apiFetch<WireKnowhowTableDetail>(
    `/notebooks/${notebookId}/knowhow/import`,
    {
      method: "POST",
      body: form,
    },
  ).then(mapDetail);
};
```

- [ ] **Step 4: Run the model tests and TypeScript compiler**

Run:

```bash
node --test frontend/app/knowhow-model.test.mjs
npm --prefix frontend run lint
```

Expected: the Node tests pass and TypeScript reports no errors. Existing call sites remain column-oriented through the frontend default until Task 4 makes the choice explicit.

- [ ] **Step 5: Commit the frontend protocol slice**

```bash
git add frontend/app/knowhow-model.ts frontend/app/knowhow-model.test.mjs
git commit -m "feat: send knowhow import orientation"
```

---

### Task 4: Add the Direction Selector to the New-Table Import Wizard

**Files:**

- Modify: `frontend/app/knowhow-import-logic.ts:9-40`
- Modify: `frontend/app/knowhow-import.tsx:1-525,826-1060`
- Test: `frontend/app/knowhow-import.test.mjs:1-80`

**Interfaces:**

- Consumes: `KnowhowImportOrientation`.
- Produces: `DEFAULT_IMPORT_ORIENTATION = "columns"`.
- Produces: `IMPORT_ORIENTATION_OPTIONS` with the exact two protocol values and UI descriptions.
- Invariant: `backToSelect()` clears file-derived state but never resets orientation.

- [ ] **Step 1: Add failing option/default tests**

Import `readFileSync` and the new logic exports in `knowhow-import.test.mjs`:

```javascript
import { readFileSync } from "node:fs";

import {
  DEFAULT_IMPORT_ORIENTATION,
  IMPORT_ORIENTATION_OPTIONS,
} from "./knowhow-import-logic.ts";
```

Add:

```javascript
test("导入方向：默认属性按列，协议值与说明固定", () => {
  assert.strictEqual(DEFAULT_IMPORT_ORIENTATION, "columns");
  assert.deepStrictEqual(IMPORT_ORIENTATION_OPTIONS, [
    {
      value: "columns",
      label: "属性按列",
      description: "第一行是属性名，每一行是一条记录",
    },
    {
      value: "rows",
      label: "属性按行",
      description: "第一列是属性名，每一列是一条记录",
    },
  ]);
});


test("返回选文件步骤时保留导入方向", () => {
  const source = readFileSync(
    new URL("./knowhow-import.tsx", import.meta.url),
    "utf8",
  );
  const backToSelect = source.match(
    /function backToSelect\(\) \{[\s\S]*?\n  \}/,
  )?.[0];
  assert.ok(backToSelect, "找不到 backToSelect");
  assert.doesNotMatch(backToSelect, /setOrientation/);
});
```

- [ ] **Step 2: Run the wizard logic tests and verify RED**

Run:

```bash
node --test frontend/app/knowhow-import.test.mjs
```

Expected: FAIL because the option registry and default do not exist.

- [ ] **Step 3: Implement the orientation option registry**

Update the model type import in `knowhow-import-logic.ts` and add:

```typescript
import {
  ROLE_LABELS,
  type KnowhowColumnInput,
  type KnowhowImportOrientation,
  type KnowhowPreviewColumn,
  type Role,
} from "./knowhow-model.ts";

export const DEFAULT_IMPORT_ORIENTATION: KnowhowImportOrientation = "columns";

export const IMPORT_ORIENTATION_OPTIONS: {
  value: KnowhowImportOrientation;
  label: string;
  description: string;
}[] = [
  {
    value: "columns",
    label: "属性按列",
    description: "第一行是属性名，每一行是一条记录",
  },
  {
    value: "rows",
    label: "属性按行",
    description: "第一列是属性名，每一列是一条记录",
  },
];
```

- [ ] **Step 4: Wire orientation state through preview and commit**

In `knowhow-import.tsx`:

1. Import `type KnowhowImportOrientation` from `knowhow-model.ts`.
2. Import `DEFAULT_IMPORT_ORIENTATION` and `IMPORT_ORIENTATION_OPTIONS` from `knowhow-import-logic.ts`.
3. Add state:

```typescript
const [orientation, setOrientation] =
  useState<KnowhowImportOrientation>(DEFAULT_IMPORT_ORIENTATION);
```

4. Change the preview call:

```typescript
const result = await importKnowhowPreview(
  notebookId,
  selected,
  orientation,
);
```

5. Change the commit call:

```typescript
await importKnowhow(
  notebookId,
  file,
  title.trim(),
  columns,
  anchorIndex,
  orientation,
);
```

6. Pass orientation props to `SelectFileStep`:

```tsx
<SelectFileStep
  loading={previewLoading}
  error={previewError}
  orientation={orientation}
  onOrientationChange={setOrientation}
  onFileInputChange={handleFileInputChange}
/>
```

7. Keep `backToSelect()` exactly free of `setOrientation(...)`.

- [ ] **Step 5: Render the accessible direction selector**

Replace the `SelectFileStep` signature/body with:

```tsx
function SelectFileStep({
  loading,
  error,
  orientation,
  onOrientationChange,
  onFileInputChange,
}: {
  loading: boolean;
  error: string | null;
  orientation: KnowhowImportOrientation;
  onOrientationChange: (orientation: KnowhowImportOrientation) => void;
  onFileInputChange: (event: ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <div className="knowhow-import-select-step">
      <fieldset
        className="knowhow-import-orientation"
        disabled={loading}
      >
        <legend>属性排列方式</legend>
        <div className="knowhow-import-orientation-options">
          {IMPORT_ORIENTATION_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={
                orientation === option.value
                  ? "is-selected"
                  : undefined
              }
            >
              <input
                type="radio"
                name="knowhow-import-orientation"
                value={option.value}
                checked={orientation === option.value}
                onChange={() => onOrientationChange(option.value)}
              />
              <span>
                <strong>{option.label}</strong>
                <small>{option.description}</small>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <label
        className={`knowhow-import-dropzone${
          loading ? " is-loading" : ""
        }`}
      >
        <input
          type="file"
          accept={IMPORT_ACCEPT}
          onChange={onFileInputChange}
          disabled={loading}
        />
        <span className="knowhow-import-drop-icon">
          {loading ? (
            <Loader2 size={28} className="knowhow-import-spin" />
          ) : (
            <Upload size={28} />
          )}
        </span>
        <strong>{loading ? "解析中…" : "点击或拖拽文件到此处"}</strong>
        <small>支持 {IMPORT_ACCEPT_EXTENSIONS.join(" / ")}</small>
      </label>
      {error && <p className="knowhow-import-error">{error}</p>}
    </div>
  );
}
```

Change the first step label:

```typescript
const STEP_LABELS: [1 | 2 | 3, string][] = [
  [1, "选格式与文件"],
  [2, "预览与列设置"],
  [3, "提交"],
];
```

- [ ] **Step 6: Add focused selector styling**

Add before `.knowhow-import-dropzone` in `ImportWizardStyles`:

```css
.knowhow-import-orientation {
  margin: 0;
  padding: 0;
  border: 0;
}

.knowhow-import-orientation legend {
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 700;
}

.knowhow-import-orientation-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.knowhow-import-orientation-options label {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
  cursor: pointer;
}

.knowhow-import-orientation-options label.is-selected {
  border-color: var(--blue);
  background: #eef2ff;
}

.knowhow-import-orientation-options input {
  margin-top: 3px;
}

.knowhow-import-orientation-options span {
  display: grid;
  gap: 3px;
}

.knowhow-import-orientation-options strong {
  font-size: 13px;
}

.knowhow-import-orientation-options small {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

@media (max-width: 620px) {
  .knowhow-import-orientation-options {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 7: Run frontend tests, type checking, and build**

Run:

```bash
node --test frontend/app/knowhow-import.test.mjs frontend/app/knowhow-model.test.mjs frontend/app/knowhow-manage.test.mjs
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: all Node tests pass, TypeScript reports no errors, and Next.js production build succeeds.

- [ ] **Step 8: Commit the complete frontend slice**

```bash
git add frontend/app/knowhow-import-logic.ts frontend/app/knowhow-import.test.mjs frontend/app/knowhow-import.tsx
git commit -m "feat: choose knowhow import attribute direction"
```

---

### Task 5: Synchronize Product and Architecture Documentation

**Files:**

- Modify: `README.md:284`
- Modify: `README_zh.md:259`
- Modify: `AGENTS.md:135`
- Modify: `architecture.md:160-174`

**Interfaces:**

- Documents the shipped contract; no runtime interface.
- Invariant: English and Chinese README descriptions carry the same behavior.

- [ ] **Step 1: Update the English README with the exact behavior**

In the first paragraph under `## Knowhow tables`, replace the import sentence with:

```markdown
A table starts either from an import (xlsx/csv/Markdown, with a column-to-kind mapping preview) or from a **create-table wizard** (define the column headers first, then fill in rows). New-table import asks whether attributes are arranged by column (the default: first row is the header) or by row (first column contains attribute names); row-oriented input is transposed on the backend before preview and commit, so the internal grid, append-import contract, and projection pipeline remain column-oriented.
```

- [ ] **Step 2: Update the Chinese README with the equivalent contract**

In the first paragraph under `## Knowhow 表`, replace the import sentence with:

```markdown
建表可以从**导入**开始（xlsx/csv/Markdown，预览时给出列→内容类型的映射建议）：新表导入会让用户选择“属性按列”（默认，首行为表头）或“属性按行”（首列为属性名），后端在预览与确认导入前自动把属性行表转置成内部统一的属性列表；追加导入与投影管线仍只处理属性列表。也可以用**建表向导**从零搭建（先定列名表头，再填行）。
```

- [ ] **Step 3: Update the repository working contract**

Add this sentence to the Knowhow paragraph in `AGENTS.md` after the content-kind sentence:

```markdown
New-table import explicitly accepts `orientation=columns|rows` (`columns` default); `rows` input is transposed after raw xlsx/csv/Markdown extraction and before preview/validation, while persisted grids, append import, retrieval, and projection stay column-oriented. Preview and commit must use the same request orientation, and row-oriented input defaults the normalized first column as the row-title suggestion without making that choice mandatory.
```

- [ ] **Step 4: Update the architecture**

Add this paragraph to `architecture.md` §3.7 after the column-kind paragraph:

```markdown
新表导入在请求层接受 `orientation=columns|rows`（默认 `columns`）。`grid_parser.py`
先提取 xlsx/csv/Markdown 原始矩阵；`rows` 模式将不等长行右侧补空后转置，再统一进入
表头校验、预览、建表和投影。方向不持久化；追加导入、存储网格、检索和 KG 投影始终保持
“列是属性”的内部契约。属性行预览默认建议规范化后的首列为行标题，用户仍可改选或不设置。
```

- [ ] **Step 5: Check documentation consistency and vocabulary**

Run:

```bash
rg -n "orientation=columns\\|rows|属性按列|属性按行|row-oriented" README.md README_zh.md AGENTS.md architecture.md
git diff --check
```

Expected: the four documentation surfaces mention the same scope; `git diff --check` produces no output.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md README_zh.md AGENTS.md architecture.md
git commit -m "docs: explain knowhow import orientation"
```

---

### Task 6: Run Final Verification and Review the Delivered Scope

**Files:**

- Verify only; modify implementation/tests/docs only if a failing check exposes a scoped defect.

**Interfaces:**

- Consumes all outputs from Tasks 1-5.
- Produces evidence that the feature is complete and the repository contract is green.

- [ ] **Step 1: Run focused backend verification**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_grid_parser.py backend/tests/test_knowhow_api.py -q
```

Expected: all focused backend tests pass.

- [ ] **Step 2: Run focused frontend verification**

```bash
node --test frontend/app/knowhow-import.test.mjs frontend/app/knowhow-manage.test.mjs frontend/app/knowhow-model.test.mjs
```

Expected: all focused frontend tests pass.

- [ ] **Step 3: Run the repository-required full check**

```bash
scripts/check.sh
```

Expected: exit code 0, including backend suite, frontend Node tests, TypeScript check, and Next.js build.

- [ ] **Step 4: Re-run the explicit frontend build required by `AGENTS.md`**

```bash
npm --prefix frontend run build
```

Expected: exit code 0 and a successful production build.

- [ ] **Step 5: Record the feature only after all required verification is green**

Insert this completed section before `## 20. 当前边界` in `fangan_done.md`:

```markdown
## 28. Knowhow 新表双方向导入（2026-07-20）

- **新表导入方向**：`POST /api/notebooks/{id}/knowhow/import/preview` 与 `POST /api/notebooks/{id}/knowhow/import` 支持请求级 `orientation=columns|rows`，默认 `columns` 兼容旧客户端。
- **服务端统一规范化**：xlsx/csv/Markdown 原始矩阵在校验前按用户选择转置；属性行输入先补齐不等长行，再转成内部“列是属性”的网格。预览和正式导入使用同一方向，追加导入、存储、检索与投影契约不变。
- **前端闭环**：导入向导第一步可选“属性按列 / 属性按行”，预览显示最终规范化形态；属性按行默认建议首列为行标题，用户可改选或取消。
- **验证**：相关解析/API/前端契约测试、`scripts/check.sh` 与前端生产构建均通过。
```

This entry is now factual because Steps 1-4 passed. Do not add it if either required command is red.

- [ ] **Step 6: Check the final documentation change**

```bash
rg -n "Knowhow 新表双方向导入|orientation=columns\\|rows|属性按列|属性按行" fangan_done.md README.md README_zh.md AGENTS.md architecture.md
git diff --check
```

Expected: all five surfaces describe the same shipped behavior; `git diff --check` produces no output.

- [ ] **Step 7: Commit the verified completion record**

```bash
git add fangan_done.md
git commit -m "docs: record verified knowhow import orientation"
```

- [ ] **Step 8: Inspect the final diff and scope**

```bash
git status --short
git diff --stat HEAD~3..HEAD
git log -n 6 --oneline
```

Confirm:

- no database migration was added;
- append-import files and endpoints did not change;
- both new-table endpoints default to `columns`;
- preview and commit both carry the same explicit frontend state;
- row-oriented preview defaults `anchor_suggestion` to `0`;
- documentation and `fangan_done.md` describe only verified behavior.

- [ ] **Step 9: Apply the verification-before-completion gate**

Invoke `superpowers:verification-before-completion`, cite the fresh command outputs in the handoff, and do not claim success if any required command remains red.
