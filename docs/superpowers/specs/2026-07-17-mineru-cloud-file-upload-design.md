# 上传的本地文件走云端 MinerU 解析（补齐文件上传→云端通道）—— 设计

状态：已确认，待实现
关联：承接 `2026-07-16-mineru-embedded-image-retention-design.md`（图片保留）；本设计补上"上传文件也能用云端 MinerU"这一段缺失通道。

## 1. 目标与范围

让**上传的本地 PDF/Word/PPT 文件**在只配置了云端 MinerU（`MINERU_MODE=cloud` + `MINERU_API_TOKEN`）时，也能走 mineru.net 云端 v4 精准解析——从而拿到图片/公式/表格，与"添加 URL 链接"路径对称。

**范围内**：
- 云端客户端新增"本地文件批量上传解析"能力（v4 file-urls/batch 流程）。
- 摄取路径的"文件上传"分支新增云端兜底（本地 MinerU 未配置 && 云端已配置 → 云端）。
- 复用既有图片保留、护栏、结果 ZIP 解析逻辑。

**范围外**：
- 不改"添加 URL 链接"路径（已支持云端）。
- 不改本地 http/cli 模式行为。
- 不新增用户可见 UI（复用现有"重新解析"按钮）。
- 不做裸图片文件（.png/.jpg）作为一等 source 类型。

## 2. 现状：上传文件为什么没走云端

`source_ingestion.py` 的解析分支对两种来源**不对称**（`process_source`，约 :477–:502）：

- **URL 来源**（`source.source_url` 非空）：本地 MinerU 已配置 → 本地优先（数据不出网）；**否则回落云端** `cloud_client.parse_url_with_images(...)` → 能出图。
- **文件上传来源**（`else` 分支）：只把 `self.mineru_client()`（本地 http/cli）传给 `parse_file`。云端客户端**根本没接进来**。

而 `mineru_enabled`（`config.py`，决定本地 `mineru_client.configured`）只认 `http`/`cli`，`cloud` 落到 `return False`（`cloud` 的 token 走的是独立的 `mineru_cloud_enabled`，此前仅服务 URL 来源）。

于是"本机只配云端 token + 上传本地文件"时：`mineru_client.configured == False` → `parse_pdf` 退回 `parse_pdf_pypdf` 纯文本兜底（无图片、无公式、无表格）。**云端一次都没被调用。**

现场证据：notebook DeepSeek-V4（`nb-a73f16940c`）新上传的 `Scaling Agents via Continual Pre-training.pdf`（`src-25ee921b…`）解析后仅有 `heading`/`page_text` 元素、`notebook_assets` 中该源图片资产为 0。

## 3. 改动概览（单 PR，后端为主）

### A. 云端客户端新增文件上传解析 `mineru_cloud_client.py`

新增 `parse_file_with_images(path, *, data_id="") -> (content_list, {basename: bytes})`，与现有 `parse_url_with_images` 并列，走 v4 **本地文件批量上传**流程：

1. `POST /api/v4/file-urls/batch` — body 复用现有解析参数（`model_version` / `enable_formula` / `enable_table` / `language` / `is_ocr=false`）+ `files: [{name: <文件名>, is_ocr: false, data_id?}]`。响应给出 `batch_id` 与一条签名上传 URL。
2. **签名 URL `PUT` 上传文件字节**——按官方示例，不带 `Content-Type` 头。经新接缝 `_http_put_file(url, data: bytes)` 完成（对齐 `_http_json`/`_http_bytes`/`_sleep`，便于不打网络单测）。
3. `GET /api/v4/extract-results/batch/{batch_id}` —— 新增 `_poll_batch(batch_id)`，沿用现有 `_poll`（task 版）的间隔/超时/终态处理模式，但解析 batch 结果响应中**本文件条目**的 `state`（`done` 取该条目 `full_zip_url`；`failed` 抛其 `err_msg`；超时抛错）。
4. 下载结果 ZIP（`_http_bytes`）→ **复用** `_content_list_from_zip` 与 `_images_from_zip`（图片仅在 `mineru_return_images` 开启时抽取）。

ingestion 直接调用 `parse_file_with_images`；**不额外加 `parse_file` 兼容 wrapper**（无历史调用方，且与 ingestion 里已有的 `self.parse_file` 概念撞名 —— YAGNI）。

Bearer token 仍不入日志；错误统一经 `last_error` + 抛出，编排逻辑保持可覆写接缝单测。

### B. 摄取"文件上传"分支加云端兜底 `source_ingestion.py`

`process_source` 的 `else`（文件上传）分支改为三段式，对称 URL 分支：

```
mineru_client = self.mineru_client()
if mineru_client.configured:                    # 本地 http/cli：现状不变
    elements = self.parse_file(..., mineru_client, persist_image=persist_image)
    parser_mode = mineru_client.mode
elif self.mineru_cloud_client().configured:     # 新增：本地没配 + 云端配了 → 云端
    cloud = self.mineru_cloud_client()
    content_list, images = cloud.parse_file_with_images(file_path, data_id=source_id)
    elements = mineru_content_list_to_elements(source_id, content_list, images=images,
                                               persist_image=persist_image)
    parser_mode = "mineru_cloud"
    # 云端失败 → 落 last_error 后回落 pypdf（见"回退"）
else:                                            # 都没配：现状不变
    elements = self.parse_file(..., mineru_client, persist_image=persist_image)
```

`persist_image`、`delete_source_images`（重解析前清旧图）等 Task 8/9 既有机制原样透传，无需改动。

## 4. 已确认的决策

- **触发方式 = 对称兜底，不新增开关、不改 `mineru_enabled` 语义**。内网部署配 `MINERU_MODE=http` 时文件走本地内网 server（不外发）；仅"显式配云端 token 且未配本地"才走云端——即用户本机意图。
- **数据出网**：文件上传走云端 = 本地文件发往 mineru.net。这是"仅在没有任何本地 MinerU、且用户主动配了云端 token 时"的兜底，与 URL 路径既有语义一致；不额外设 gate。
- **零新增配置项**：全部复用 `mineru_return_images` / `mineru_max_image_bytes` / `mineru_max_images_per_source` / `mineru_cloud_*` / `mineru_api_*`。
- v4 约束沿用官方：单文件 ≤200MB / ≤600 页；本路径每次只上传 1 个文件（batch size = 1）。

## 5. 回退 / 降级（"MinerU 出问题不阻塞摄取"）

- 云端上传/轮询/下载/解析任一步失败 → 记 `last_error`，**回落 `parse_pdf_pypdf` 纯文本**，摄取不中断（沿用 `parse_pdf` 既有"MinerU outage never blocks ingestion"原则）。降级后 `parser_mode`/`mineru_error` 照常落 stage 日志，便于事后诊断。
- `mineru_return_images=false` → 云端仍解析文本/公式/表格，只是不抽图（`_images_from_zip` 返回空）。
- 单张图片条目损坏被跳过而非拖垮正文（复用 `_images_from_zip` 既有语义）。

## 6. 测试策略

- **云端客户端单测**：覆写 `_http_json`/`_http_put_file`/`_http_bytes`/`_sleep` 四接缝，喂假的 batch 申请响应、假签名 URL、假轮询序列（pending→done）、假结果 ZIP（含 `_content_list.json` + `images/`）；断言 `parse_file_with_images` 返回正确 content_list + images，且未打真实网络。
- **失败路径单测**：轮询 `failed` / 超时 / ZIP 无 content_list → 抛错并置 `last_error`。
- **摄取分支测**：本地未配置 + 云端已配置（stub cloud client）→ 走云端支路、`parser_mode="mineru_cloud"`；云端 stub 抛错 → 回落 pypdf、摄取仍产出文本 elements。
- 现有图片保留/护栏测试保持通过（复用同一 `mineru_content_list_to_elements` + `persist_image`）。

## 7. 不在本次范围

- 前端改动（复用现有"重新解析"按钮与授权图片 endpoint）。
- 云端多文件并发 batch（本路径固定 1 文件）。
- 大文件自动分块/OCR 自适应（那是 `scripts/mineru_pdf_to_md.py` 批量工具的职责；backend 内联解析保持单文件直传）。

## 8. 关键文件锚点

- `backend/app/services/mineru_cloud_client.py` — 新增 `parse_file_with_images` / `parse_file` / `_http_put_file`；复用 `_poll` 语义、`_content_list_from_zip`、`_images_from_zip`。
- `backend/app/services/source_ingestion.py` — `process_source` 文件上传分支加云端兜底（约 :496）。
- `backend/app/services/parsers.py` — `parse_pdf_pypdf` 作为回退（不改）。
- `backend/app/core/config.py` — `mineru_cloud_*`/`mineru_api_*`/图片护栏（不改，仅复用）。
- 参考实现：`~/.claude/skills/mineru-pdf-to-md/scripts/mineru_pdf_to_md.py`（v4 file-urls/batch 上传 + 轮询的官方流程形状）。

## 9. 收尾

改完 → 重新解析 `Scaling Agents via Continual Pre-training.pdf`（前端"重新解析"按钮或 `POST /sources/{id}/parse`）→ 验证：该源出现 `image` 元素、`notebook_assets` 有对应资产、前端源视图内联显示图片。→ 分支 rebase 到 master，提 PR（README/README_zh 若涉及云端文件解析口径同步更新）。
