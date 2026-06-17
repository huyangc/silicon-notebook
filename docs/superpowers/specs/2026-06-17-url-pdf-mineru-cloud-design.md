# 在线 URL → PDF → MinerU 云端解析 设计

- 日期：2026-06-17
- 状态：设计已确认，待写实现计划
- 关联：`backend/app/services/mineru_client.py`（现有 http/cli 通道）、`backend/app/services/parsers.py`（`mineru_content_list_to_elements`）、`backend/app/services/sqlite_repository.py`（`process_source` 流水线）、前端 `frontend/app/page.tsx`（Source Stack）

## 1. 目标与范围

允许用户粘贴**一个或多个在线 URL**作为来源；每个 URL **只支持指向一个 PDF 文件**。非 PDF（或不可达/超限）→ **直接判失败**。PDF → 调用 **mineru.net 云端 v4 解析服务**（`https://mineru.net/apiManage/docs`）解析，解析结果接入**现有**来源流水线（elements → chunks → embed → KG）。

### 已确认的产品决策

1. **URL 可达性**：仅面向**公开可直链**的 PDF（arXiv、官网、公开报告等）。因此把 URL **直接交给 mineru.net 云端**去拉取解析，后端不下载、不二次上传。
2. **失败呈现（折中）**：添加时做**轻量初筛**（非 PDF 立即拒绝、**不创建来源**）；通过初筛的进入**异步**云解析，云端失败则落 `failed` 来源条目（可见/可重试/可删）。
3. **入口**：Source Stack 面板"添加来源（文件）"旁，新增**并列独立**的「添加链接」入口；弹窗内每行一个 URL，可一次多个。
4. **`model_version=vlm`**（精度更好，官方推荐）。
5. 端点返回**结构体** `{ created, rejected }`（而非纯 `SourceSummary[]`），以便前端展示被拒 URL 及原因。

### 不在本期范围（YAGNI）

- 内网/需鉴权 URL（方案 B：后端先下载再以文件上传）——留作扩展点。
- 把现有"上传文件"来源改走云端（现有 http/cli 通道保持不变）。
- 云端**批量** URL 任务（`/extract/task/batch`）合并提交——首期逐 URL 单任务，失败隔离更简单。

## 2. 云端 API 契约（已核对 mineru.net v4 文档）

- **提交（单任务）**：`POST {MINERU_API_BASE}/api/v4/extract/task`
  - 鉴权头：`Authorization: Bearer {token}`
  - Body（JSON）：`url`（必填）、`model_version="vlm"`、`is_ocr=false`、`enable_formula=true`、`enable_table=true`、`language="ch"`、`data_id={source_id}`（便于排障）。
  - 响应：`{ "code": 0, "data": { "task_id": "..." }, "msg": "ok", "trace_id": "..." }`。
- **轮询**：`GET {MINERU_API_BASE}/api/v4/extract/task/{task_id}`（带 `Authorization`）
  - `state` 取值：`pending` / `running` / `converting` / `done` / `failed`。
  - `done` → `full_zip_url`（ZIP 内含 Markdown 与 JSON）；`failed` → `err_msg`；`running` → `extract_progress.{extracted_pages,total_pages}`。
- **限制**：单文件 ≤200MB / ≤200 页；批量 ≤200 文件；日 1000 页高优先，超出降优先。
- **支持格式**：PDF、图片、Doc/Docx、Ppt/Pptx、Xls/Xlsx、HTML——**比"仅 PDF"宽**，所以"仅 PDF"必须由我方初筛把关。
- **已知限制**：mineru.net 在境内，**境外 URL（GitHub/AWS 等）可能超时**；交由"轮询超时 → failed"优雅兜底，并在 `err_msg`/提示里点明。

## 3. 架构与数据流

```
前端「添加链接」弹窗（粘贴 1~N 个 URL，每行一个）
   │  POST /notebooks/{id}/sources/url   { urls: [...] }
   ▼
routes.add_url_sources
   └─ repository.add_url_sources(notebook_id, urls)
        ├─ token 未配置 → 400「未配置 MinerU 云端凭证」
        └─ 逐 URL：probe_pdf(url)
             ├─ 非 PDF / 不可达 / >200MB → rejected[] += {url, reason}（不建来源）
             └─ 通过 → 建 source(status=queued, source_url=…, source_type=pdf) → scheduler(process_source)
   ▼ 返回 { created: SourceSummary[], rejected: [{url, reason}] }

后台 process_source(source_id)（复用现有状态机）
   │  parsing → 检测 source.source_url 非空 → 云端分支
   ▼  content_list = mineru_cloud_client.parse_url(url, data_id=source_id)
   │     submit → poll(间隔轮询至终态/超时) → 下载 full_zip_url → 取 *_content_list.json（回退 full.md）
   ▼  elements = mineru_content_list_to_elements(source_id, content_list)   ← 复用
   ▼  parsed → chunks → embed(bg) → KG extract → extracted                  ← 全部复用
   └─ 云端 state=failed / 轮询超时 / 空结果 → 抛错 → source 落 failed(error_message=err_msg)
```

**核心不变量**：URL 来源在 `process_source` 之后与上传文件来源**走完全相同的下游**；唯一分叉是"解析这一步用云端 URL 任务而非本地文件"。

## 4. 组件与边界（每个单元：做什么 / 怎么用 / 依赖谁）

### 4.1 `backend/app/services/mineru_cloud_client.py`（新建，自包含）

- **做什么**：把一个公开 PDF URL 解析为 MinerU `content_list`。
- **接口**：`MinerUCloudClient(settings)`；`parse_url(url: str, *, data_id: str = "") -> list[dict]`（失败抛异常）；属性 `configured -> bool`（`bool(token)`）、`last_error: str`。
- **内部**：`_submit(url, data_id) -> task_id`；`_poll(task_id) -> result`（按 `MINERU_CLOUD_POLL_INTERVAL_SECONDS` 轮询，至终态或 `MINERU_CLOUD_TIMEOUT_SECONDS` 超时）；`_download_zip(full_zip_url) -> bytes`；`_content_list_from_zip(zip_bytes) -> list[dict]`（优先 `*_content_list.json`；缺失则用 `full.md`/`*.md` 走现有 markdown 解析回退）。
- **依赖**：stdlib `urllib.request` / `zipfile` / `json`（仿 `mineru_client.py` 的零重依赖风格）；`Settings`。**不**在模块加载期引入任何重依赖。
- **保密**：token 仅用于请求头，**绝不**写日志/异常文本。

### 4.2 `backend/app/services/remote_sources.py`（新建，小工具）

- **做什么**：判定一个 URL 是否"可解析的 PDF"，并给出展示名。
- **接口**：`probe_pdf(url, *, settings) -> PdfProbe`，`PdfProbe = {ok: bool, reason: str, content_length: int, display_name: str}`。
- **判定**：要求 `http/https` scheme → 先 `HEAD`（跟随重定向，~10s 超时）读 `Content-Type`/`Content-Length`；歧义或不支持 HEAD 时发 `Range: bytes=0-1023` 的 `GET` 读首字节。**PASS** 当且仅当 `Content-Type` 以 `application/pdf` 开头，**或**响应首字节为 `%PDF-`。`Content-Length > 200MB` 直接拒。`display_name` 取 URL 末段（无则用 host），保证带 `.pdf` 后缀便于下游类型识别。
- **依赖**：stdlib urllib；`Settings`（超时配置）。

### 4.3 `backend/app/api/routes.py`

- 新增 `POST /notebooks/{notebook_id}/sources/url`，Body `AddUrlSourcesRequest{ urls: list[str] }`，返回 `AddUrlSourcesResult{ created, rejected }`。
- 委托 `repository().add_url_sources(notebook_id, urls, scheduler=...)`，**沿用** `upload_sources` 的后台调度：route 传入 `scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id)`。notebook 不存在 → 404；token 未配置 → 400。

### 4.4 `backend/app/services/sqlite_repository.py`

- 新增字段 `self.mineru_cloud_client = MinerUCloudClient(settings)`（与现有 `self.mineru_client` 并存）。
- 新增 `add_url_sources(notebook_id, urls, scheduler=None) -> AddUrlSourcesResult`（`scheduler` 由 route 传入，同 `upload_sources`：有则后台调度、来源回 `queued`；无则同步跑完）：token 校验 → 逐 URL `probe_pdf` → 通过的建 `sources` 行（`source_url`、`source_type=pdf`、`file_path=""`、`file_size=content_length`、`status/parse_status=queued`、`summary="链接已添加，解析排队中。"`）→ `scheduler(source_id)` 或同步 `process_source(source_id)`。
- `process_source`：`parsing` 后增 URL 分支——`source.source_url` 非空时调 `mineru_cloud_client.parse_url(...)` 得 `content_list`，再 `mineru_content_list_to_elements`，其余流水线不变。空结果/异常 → 现有 `except` 落 `failed`（写入 `last_error`/`err_msg`）。
- `add_url_sources` 与 `process_source` 的 Protocol 声明同步加到 `repository.py`。

### 4.5 `backend/app/models/schemas.py`

- `SourceSummary` / `SourceDetail` 增 `source_url: str = ""`（前端据此显示外链）。
- 新增 `AddUrlSourcesRequest{ urls: list[str] }`、`RejectedUrl{ url: str, reason: str }`、`AddUrlSourcesResult{ created: list[SourceSummary], rejected: list[RejectedUrl] }`。

### 4.6 DB 迁移

- `sources` 加列 `source_url TEXT NOT NULL DEFAULT ''`，沿用现有列迁移方式（参照 `file_path TEXT NOT NULL DEFAULT ''` 的轻量 `ALTER`/建表默认）。`get_source` 等读取处带出该列。

### 4.7 配置 `backend/app/core/config.py` + `.env.example`

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINERU_API_TOKEN` | `""` | 云端 Bearer token（密钥）。`mineru_cloud_enabled = bool(token)`。|
| `MINERU_API_BASE` | `https://mineru.net` | 云端基址。|
| `MINERU_CLOUD_MODEL_VERSION` | `vlm` | 解析模型版本。|
| `MINERU_CLOUD_LANGUAGE` | `ch` | 识别语言。|
| `MINERU_CLOUD_FORMULA_ENABLE` | `true` | 公式识别。|
| `MINERU_CLOUD_TABLE_ENABLE` | `true` | 表格识别。|
| `MINERU_CLOUD_TIMEOUT_SECONDS` | `600` | 轮询总超时。|
| `MINERU_CLOUD_POLL_INTERVAL_SECONDS` | `5` | 轮询间隔。|

与现有 `MINERU_MODE`(off/http/cli) **完全独立**：云端是专供 URL 来源的独立通道，不改变现有上传文件的解析路径。

### 4.8 前端 `frontend/app/page.tsx`

- Source Stack 面板「添加来源」旁加 **「添加链接」** 按钮 → 轻量弹窗：多行 `<textarea>`（每行一个 URL）+ 提交。
- 提交 `POST /notebooks/{id}/sources/url`；成功后 toast「已添加 N 个，M 个被拒」，弹窗内列出 `rejected[]` 的 url+reason；`created[]` 追加进来源列表，沿用现有 pending 轮询自动推进到 `extracted`。
- URL 来源卡片显示外链图标/可点原 URL（读 `source.source_url`，最小改动）。

## 5. 错误处理一览

- **初筛（同步）**：非 PDF / 不可达(4xx/5xx/超时) / >200MB → `rejected[]`，不建来源；前端逐条展示原因。
- **云解析（异步）**：`state=failed`（带 `err_msg`，如超 200 页、境外超时、URL 失效）或本地轮询超时 或 ZIP 无可用内容 → 抛错 → source `failed`，`error_message` 写入原因；可经现有 `POST /sources/{id}/parse` 重试。
- **无本地回退**：token 缺失或云端不可用时，URL 来源**不**退化到 pypdf（没有字节），直接给明确错误（与上传文件来源的 pypdf 兜底不同）。

## 6. 测试策略（不打真网：注入假客户端 / monkeypatch urllib）

- `probe_pdf`：`application/pdf` 过；`octet-stream + %PDF` 过；`text/html` 拒；`404`/超时拒；`Content-Length` 超限拒。
- `MinerUCloudClient`：`submit → task_id`；`poll` 经 `pending→running→done`；用**内存 ZIP fixture**（含 `*_content_list.json`）→ `parse_url` 得 `content_list` → `mineru_content_list_to_elements` 映射出 elements；`state=failed` → 带 `err_msg` 抛错；超时 → 抛错；ZIP 仅含 `full.md` → 走 markdown 回退。
- 端点：`{urls:[pdf, html]}` → `created` 1 个、`rejected` 1 个；token 未配置 → 400；notebook 缺失 → 404。
- 仓储：URL 来源经（打桩的）云客户端 → elements 落库、chunks 建好、状态 `extracted`；云失败 → `failed` 带 `err_msg`。
- 复用既有 `mineru_content_list_to_elements` 的单测，不重复造。

## 7. 复用与解耦小结

- **复用**：`mineru_content_list_to_elements`、`process_source` 全下游、`SourceSummary`/pending 轮询/前端来源列表、`/sources/{id}/parse` 重解析。
- **新增且自包含**：`mineru_cloud_client.py`、`remote_sources.py`（均 stdlib，无重依赖，可独立单测）。
- **与现有解耦**：云端通道与 `MINERU_MODE`(http/cli) 互不影响；URL 分支只在 `process_source` 的"解析步"短路一次。
