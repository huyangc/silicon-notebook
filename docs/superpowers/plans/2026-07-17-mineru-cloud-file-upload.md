# 上传文件走云端 MinerU 解析 —— 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让只配置了云端 MinerU（`MINERU_MODE=cloud` + `MINERU_API_TOKEN`）的部署，在**上传本地 PDF/Word/PPT 文件**时也能走 mineru.net 云端 v4 解析、拿到图片/公式/表格，与"添加 URL 链接"路径对称。

**Architecture:** 两处改动。(A) `MinerUCloudClient` 新增 `parse_file_with_images`，走 v4 `file-urls/batch` 上传流程（申请上传 URL → PUT 上传 → 轮询 batch 结果 → 下载结果 ZIP），复用现有 `_content_list_from_zip`/`_images_from_zip`。(B) `source_ingestion.process_source` 的文件上传分支加云端兜底：本地 MinerU 未配置 && 云端已配置 → 走云端；云端失败 → 回落 pypdf 纯文本。

**Tech Stack:** Python 3.13、`urllib.request`（不引 requests）、pytest、pydantic-settings。

## Global Constraints

- 触发方式 = 对称兜底：**不新增配置开关，不改 `mineru_enabled` 语义**。仅"本地 http/cli 未配置 && 云端 token 已配置"才走云端。
- **零新增配置项**：复用 `mineru_cloud_model_version`（默认 `vlm`）/`mineru_cloud_formula_enable`/`mineru_cloud_table_enable`/`mineru_cloud_language`（默认 `ch`）/`mineru_cloud_timeout_seconds`（600）/`mineru_cloud_poll_interval_seconds`（5）/`mineru_return_images`/`mineru_max_image_bytes`/`mineru_max_images_per_source`。
- Bearer token 绝不写入日志/异常消息。
- 云端失败/超时/结果不可用 → **回落 `parse_pdf_pypdf` 纯文本，摄取不中断**（沿用 `parse_pdf` 既有 "MinerU outage never blocks ingestion" 原则）。
- 单文件直传：本路径每次 `file-urls/batch` 只含 1 个文件。
- 所有网络 I/O 经可覆写接缝 `_http_json`/`_http_put_file`/`_http_bytes`/`_sleep`，编排逻辑可在不打网络下单测。
- v4 官方约束沿用：单文件 ≤200MB / ≤600 页。
- MinerU batch API 形状（verbatim）：
  - `POST /api/v4/file-urls/batch`，body `{files:[{name,data_id,is_ocr}], model_version, enable_formula, enable_table, language}` → `{code, data:{batch_id, file_urls:[signed_url]}}`（`file_urls` 顺序对应 `files`）。
  - 签名 URL `PUT` 文件字节，**Header `Content-Type: ""`**，成功状态 200/201/204。
  - `GET /api/v4/extract-results/batch/{batch_id}` → `{data:{extract_result:[{data_id, state, full_zip_url, err_msg}]}}`；终态 `state ∈ {done, failed}`。

---

### Task 1: 云端客户端新增文件上传解析

**Files:**
- Modify: `backend/app/services/mineru_cloud_client.py`（在现有 `parse_url_with_images` / `_submit` / `_poll` / `_http_bytes` 各自之后插入新方法）
- Test: `backend/tests/test_mineru_cloud_client.py`

**Interfaces:**
- Consumes: 现有 `self._http_json`、`self._http_bytes`、`self._sleep`、`self._api`、`self._content_list_from_zip`、模块级 `_images_from_zip`、`self.configured`、`self.settings.*`。
- Produces（供 Task 2）：
  - `parse_file_with_images(self, path: str, *, data_id: str = "") -> tuple[list[dict], dict[str, bytes]]`
  - 未配置 token → `MinerUCloudNotConfigured`；失败/超时/空结果 → `RuntimeError`（并置 `self.last_error`）。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_mineru_cloud_client.py` 末尾）

```python
def _fake_pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake bytes")
    return str(p)


def _zip_content_and_image(content, img_name="fig1.jpg", img_bytes=b"IMG"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("out/x_content_list.json", json.dumps(content).encode())
        z.writestr(f"images/{img_name}", img_bytes)
    return buf.getvalue()


def test_parse_file_happy_path_returns_content_and_images(monkeypatch, tmp_path):
    c = _client(monkeypatch)
    content = [{"type": "text", "text": "Hi", "page_idx": 0},
               {"type": "image", "img_path": "images/fig1.jpg", "page_idx": 0}]
    responses = iter([
        {"code": 0, "data": {"batch_id": "b1", "file_urls": ["https://up/f1"]}},   # submit
        {"data": {"extract_result": [{"data_id": "src-1", "state": "pending"}]}},   # poll 1
        {"data": {"extract_result": [                                               # poll 2
            {"data_id": "src-1", "state": "done", "full_zip_url": "https://z/r.zip"}]}},
    ])
    put_calls = []
    c._http_json = lambda method, url, payload=None: next(responses)
    c._http_put_file = lambda url, data: put_calls.append((url, data))
    c._http_bytes = lambda url: _zip_content_and_image(content)
    cl, images = c.parse_file_with_images(_fake_pdf(tmp_path), data_id="src-1")
    assert cl == content
    assert images == {"fig1.jpg": b"IMG"}
    assert put_calls and put_calls[0][0] == "https://up/f1"        # 上传到签名 URL
    assert put_calls[0][1] == b"%PDF-1.4 fake bytes"               # 上传的是文件字节


def test_parse_file_failed_state_raises_with_err_msg(monkeypatch, tmp_path):
    c = _client(monkeypatch)
    responses = iter([
        {"code": 0, "data": {"batch_id": "b1", "file_urls": ["https://up/f1"]}},
        {"data": {"extract_result": [{"data_id": "src-1", "state": "failed", "err_msg": "页数超限"}]}},
    ])
    c._http_json = lambda method, url, payload=None: next(responses)
    c._http_put_file = lambda url, data: None
    with pytest.raises(RuntimeError) as exc:
        c.parse_file_with_images(_fake_pdf(tmp_path), data_id="src-1")
    assert "页数超限" in str(exc.value)
    assert "页数超限" in c.last_error


def test_parse_file_poll_timeout_raises(monkeypatch, tmp_path):
    c = _client(monkeypatch, interval="1", timeout="2")
    seq = iter([{"code": 0, "data": {"batch_id": "b1", "file_urls": ["https://up/f1"]}}])
    running = {"data": {"extract_result": [{"data_id": "src-1", "state": "running"}]}}
    c._http_json = lambda method, url, payload=None: next(seq, running)
    c._http_put_file = lambda url, data: None
    with pytest.raises(RuntimeError) as exc:
        c.parse_file_with_images(_fake_pdf(tmp_path), data_id="src-1")
    assert "超时" in str(exc.value)


def test_parse_file_not_configured_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    c = MinerUCloudClient(Settings())
    with pytest.raises(MinerUCloudNotConfigured):
        c.parse_file_with_images(_fake_pdf(tmp_path))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_mineru_cloud_client.py -k parse_file -v`
Expected: FAIL —— `AttributeError: 'MinerUCloudClient' object has no attribute 'parse_file_with_images'`

- [ ] **Step 3: 实现新方法**

在 `parse_url_with_images` 方法（约第 66 行结束）之后插入：

```python
    def parse_file_with_images(
        self, path: str, *, data_id: str = ""
    ) -> tuple[List[dict], dict[str, bytes]]:
        """上传本地文件给云端(v4 file-urls/batch)，返回 (content_list, {basename: 图片字节})。

        与 parse_url_with_images 对称，但走"申请上传URL→PUT上传→轮询batch结果→下载ZIP"。
        未配置 token → MinerUCloudNotConfigured；失败/超时/结果不可用 → RuntimeError。
        """
        self.last_error = ""
        if not self.configured:
            raise MinerUCloudNotConfigured("未配置 MinerU 云端凭证 (MINERU_API_TOKEN)")
        try:
            file_path = Path(path)
            batch_id, item_data_id = self._submit_file(file_path, data_id)
            zip_url = self._poll_batch(batch_id, item_data_id)
            zip_bytes = self._http_bytes(zip_url)
            content_list = self._content_list_from_zip(zip_bytes)
            if not content_list:
                raise RuntimeError("MinerU 云端结果为空 content_list")
            images = _images_from_zip(zip_bytes) if self.settings.mineru_return_images else {}
            return content_list, images
        except Exception as exc:
            if not self.last_error:
                self.last_error = str(exc)
            raise
```

在 `_submit` 方法之后插入：

```python
    def _submit_file(self, path: Path, data_id: str) -> tuple[str, str]:
        """申请上传 URL 并 PUT 上传文件，返回 (batch_id, 本文件 data_id)。"""
        item_data_id = data_id or path.stem
        body = {
            "files": [{"name": path.name, "data_id": item_data_id, "is_ocr": False}],
            "model_version": self.settings.mineru_cloud_model_version,
            "enable_formula": self.settings.mineru_cloud_formula_enable,
            "enable_table": self.settings.mineru_cloud_table_enable,
            "language": self.settings.mineru_cloud_language,
        }
        payload = self._http_json("POST", self._api("/api/v4/file-urls/batch"), body)
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(f"MinerU 云端申请上传失败: {payload.get('msg') or payload}")
        data = payload.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise RuntimeError("MinerU 云端未返回 batch_id/file_urls")
        self._http_put_file(str(file_urls[0]), path.read_bytes())
        return str(batch_id), item_data_id
```

在 `_poll` 方法之后插入：

```python
    def _poll_batch(self, batch_id: str, data_id: str) -> str:
        """轮询 batch 结果，返回本文件 done 时的 full_zip_url。"""
        interval = max(1, int(self.settings.mineru_cloud_poll_interval_seconds))
        timeout = max(interval, int(self.settings.mineru_cloud_timeout_seconds))
        max_polls = max(1, math.ceil(timeout / interval))
        url = self._api(f"/api/v4/extract-results/batch/{batch_id}")
        for _ in range(max_polls):
            payload = self._http_json("GET", url)
            data = payload.get("data") or {}
            results = data.get("extract_result") or []
            item = next((r for r in results if str(r.get("data_id")) == str(data_id)), None)
            state = str((item or {}).get("state", "")).lower()
            if state == "done":
                zip_url = (item or {}).get("full_zip_url")
                if not zip_url:
                    raise RuntimeError("MinerU 云端完成但缺少 full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise RuntimeError(
                    f"MinerU 云端解析失败: {(item or {}).get('err_msg') or '未知错误'}"
                )
            self._sleep(interval)
        raise RuntimeError(f"MinerU 云端轮询超时 (>{timeout}s)")
```

在 `_http_bytes` 方法之后插入：

```python
    def _http_put_file(self, url: str, data: bytes) -> None:
        """PUT 上传文件字节到签名 URL(不带 Content-Type，按 MinerU 官方示例)。"""
        request = urllib.request.Request(url, data=data, method="PUT")
        request.add_header("Content-Type", "")
        with urllib.request.urlopen(
            request, timeout=self.settings.mineru_cloud_timeout_seconds
        ) as response:
            status = getattr(response, "status", 200) or 200
            if status not in (200, 201, 204):
                raise RuntimeError(f"MinerU 云端上传失败 HTTP {status}")
```

> `Path`、`math`、`urllib.request`、`List` 均已在文件顶部 import，无需新增。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_mineru_cloud_client.py -v`
Expected: PASS（新增 4 个 `parse_file*` 测试 + 原有测试全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/mineru_cloud_client.py backend/tests/test_mineru_cloud_client.py
git commit -m "feat(mineru): 云端客户端支持本地文件上传解析 (v4 file-urls/batch)"
```

---

### Task 2: 摄取文件上传分支加云端兜底 + 文档

**Files:**
- Modify: `backend/app/services/source_ingestion.py`（`process_source` 文件上传 `else` 分支，约 :495–:502）
- Modify: `README.md` / `README_zh.md`（MinerU 云端解析口径：补一句"上传文件在仅配云端时也走云端"）
- Test: `backend/tests/test_source_ingestion_service.py`

**Interfaces:**
- Consumes: Task 1 的 `cloud_client.parse_file_with_images(path, data_id=...)`；现有 `self.mineru_client()`（Callable→本地 client）、`self.mineru_cloud_client()`（Callable→云端 client）、`self.parse_file(...)`、模块级 `mineru_content_list_to_elements`（已 import）。
- Produces: 无新对外接口；`parser_mode` 新增取值 `"mineru_cloud"` / `"pypdf_fallback_after_cloud_error"`。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_source_ingestion_service.py` 末尾；沿用文件内既有 import：`SQLiteRepository`、`Settings`、`NotebookCreate`、`_now`、`uuid4`）

```python
def _seed_queued_pdf(repo, tmp_path):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "pdf", "queued", "queued",
             "doc.pdf", "/tmp/doc.pdf", 0, "", "", "academic_paper", now, now))
    return nb, sid


def test_upload_file_uses_cloud_when_only_cloud_configured(tmp_path, monkeypatch):
    """本地 MinerU 未配置 + 云端已配 → 上传文件走云端 parse_file_with_images，
    图片作为 notebook_assets 落地(与本地路径同一持久化闭包)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_MODE", raising=False)          # 本地 off → not configured
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")             # 云端 configured
    repo = SQLiteRepository(Settings())
    nb, sid = _seed_queued_pdf(repo, tmp_path)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    monkeypatch.setattr(
        repo.mineru_cloud_client, "parse_file_with_images",
        lambda path, data_id="": (
            [{"type": "image", "img_path": "fig1.png",
              "image_caption": ["Figure 1."], "page_idx": 0}],
            {"fig1.png": png},
        ),
    )
    repo.process_source(sid)

    assert repo.get_source(sid).parse_status == "extracted"
    asset_ids = repo.source_asset_ids(sid)
    assert len(asset_ids) == 1
    elements = repo.source_elements(sid)
    assert any(e.element_type == "image" for e in elements)


def test_upload_file_cloud_error_falls_back_to_pypdf(tmp_path, monkeypatch):
    """云端上传解析抛错 → 回落 pypdf 纯文本，摄取不中断(仍产出文本 elements)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_MODE", raising=False)
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")
    repo = SQLiteRepository(Settings())
    nb, sid = _seed_queued_pdf(repo, tmp_path)

    def _boom(path, data_id=""):
        raise RuntimeError("云端 500")
    monkeypatch.setattr(repo.mineru_cloud_client, "parse_file_with_images", _boom)
    # pypdf 回落读取真实文件：写一个最小 PDF 到 source.file_path
    import pypdf, io as _io
    writer = pypdf.PdfWriter(); writer.add_blank_page(width=200, height=200)
    real_pdf = tmp_path / "real.pdf"
    with real_pdf.open("wb") as fh: writer.write(fh)
    with repo._write() as db:
        db.execute("UPDATE sources SET file_path=? WHERE id=?", (str(real_pdf), sid))

    repo.process_source(sid)   # 不抛：云端错 → pypdf 兜底
    assert repo.get_source(sid).parse_status == "extracted"
    assert repo.source_asset_ids(sid) == []   # 空白页无图片资产
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py -k "cloud" -v`
Expected: FAIL —— `test_upload_file_uses_cloud_when_only_cloud_configured` 断言 `len(asset_ids) == 1` 失败（现状走 pypdf，无图片资产、无 image element）。

- [ ] **Step 3: 改 `process_source` 文件上传分支**

把 `source_ingestion.py` 现有 `else` 分支（本地文件来源，约 :495–:502）：

```python
            else:
                mineru_client = self.mineru_client()
                elements = self.parse_file(
                    source_id, source.file_path, source.file_name, mineru_client,
                    persist_image=persist_image,
                )
                mineru_error = str(getattr(mineru_client, "last_error", "") or "")
                parser_mode = str(getattr(mineru_client, "mode", ""))
```

替换为：

```python
            else:
                mineru_client = self.mineru_client()
                cloud_client = self.mineru_cloud_client()
                if not mineru_client.configured and cloud_client.configured:
                    # 本地 http/cli 未配置 + 云端已配 → 上传文件走云端(对称 URL 分支)；
                    # 云端任一步失败 → 回落 pypdf，摄取不中断。
                    try:
                        content_list, images = cloud_client.parse_file_with_images(
                            source.file_path, data_id=source_id
                        )
                        elements = mineru_content_list_to_elements(
                            source_id, content_list, images=images, persist_image=persist_image
                        )
                        mineru_error = str(getattr(cloud_client, "last_error", "") or "")
                        parser_mode = "mineru_cloud"
                    except Exception as exc:
                        mineru_error = str(getattr(cloud_client, "last_error", "") or exc)
                        elements = self.parse_file(
                            source_id, source.file_path, source.file_name, mineru_client,
                            persist_image=persist_image,
                        )
                        parser_mode = "pypdf_fallback_after_cloud_error"
                else:
                    # 本地已配(http/cli) 或 两者都没配 → 现状：本地 MinerU / pypdf。
                    elements = self.parse_file(
                        source_id, source.file_path, source.file_name, mineru_client,
                        persist_image=persist_image,
                    )
                    mineru_error = str(getattr(mineru_client, "last_error", "") or "")
                    parser_mode = str(getattr(mineru_client, "mode", ""))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py -v`
Expected: PASS（新增 2 个 cloud 测试 + 原有全绿，尤其 `test_url_local_parse_never_falls_back_to_cloud` 不受影响）

- [ ] **Step 5: 更新 README（两份）**

在 `README.md` 与 `README_zh.md` 描述 MinerU 云端解析的段落，补一句（中英各一，措辞保持通用、不含机器细节）：上传的本地文件在仅配置云端 MinerU（`MINERU_MODE=cloud`、无本地 http/cli）时，也会经云端 v4 上传解析（含图片/公式/表格），失败自动回落纯文本。先 `grep -n "MINERU\|MinerU\|云端" README.md README_zh.md` 定位现有段落，就近补充。

- [ ] **Step 6: 跑更广的回归 + 提交**

Run: `cd backend && python -m pytest tests/test_source_ingestion_service.py tests/test_source_ingestion_failure_boundaries.py tests/test_mineru_cloud_client.py tests/test_url_sources.py -q`
Expected: 全绿

```bash
git add backend/app/services/source_ingestion.py backend/tests/test_source_ingestion_service.py README.md README_zh.md
git commit -m "feat(mineru): 上传文件仅配云端时走云端解析并回落 pypdf"
```

---

## 验证与收尾（执行完 Task 1–2 后，非 TDD）

1. **真机重解析目标文章**（本机后端 8000 在跑，`MINERU_MODE=cloud` 已配）：
   - 前端点《Scaling Agents via Continual Pre-training.pdf》的"重新解析"按钮，或 `curl -X POST http://127.0.0.1:8000/api/sources/src-25ee921b821b4a7185248d2dc12307df/parse`（需带鉴权，实际以前端触发为准）。
   - **注意**：改的是后端 `.py`，需用户重启 8000 端后端才生效（不代劳重启，只告知）。
2. **验证 SQL**（主 checkout 根 `.local`）：
   - `sqlite3 -readonly .local/silicon_notebook.db "SELECT element_type,COUNT(*) FROM source_elements WHERE source_id='src-25ee921b821b4a7185248d2dc12307df' GROUP BY element_type;"` → 应出现 `image` 行。
   - `sqlite3 -readonly .local/silicon_notebook.db "SELECT COUNT(*) FROM notebook_assets WHERE source_id='src-25ee921b821b4a7185248d2dc12307df';"` → > 0。
3. **前端源视图**内联显示图片（`AuthedImage` 走鉴权 blob）。
4. **提 PR**：分支 rebase 到 master → push → `gh pr create --base master`（承接 PR#280 的图片保留栈）。

## Self-Review

- **Spec 覆盖**：spec §3.A（云端 `parse_file_with_images` + `_poll_batch` + `_http_put_file` + 复用 ZIP 抽取）→ Task 1；§3.B（文件上传分支三段式兜底）→ Task 2 Step 3；§5 回退 pypdf → Task 2 `test_upload_file_cloud_error_falls_back_to_pypdf` + 实现 `except` 分支；§6 测试全覆盖；§9 收尾 → 验证与收尾章节；README（spec §9）→ Task 2 Step 5。无遗漏。
- **Placeholder 扫描**：无 TBD/TODO；每个代码步给出完整代码；测试含真实断言。
- **类型一致**：`parse_file_with_images(path, *, data_id="")` 在 Task 1 定义、Task 2 调用签名一致（`cloud_client.parse_file_with_images(source.file_path, data_id=source_id)`）；`_submit_file`/`_poll_batch`/`_http_put_file` 仅在 Task 1 内部互相调用，签名自洽；`parser_mode` 新增取值不与既有冲突。
