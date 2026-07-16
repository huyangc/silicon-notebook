# 保留 pdf/word/ppt 内嵌图片（MinerU 抽图落盘 + 前端展示）—— 设计

- 日期：2026-07-16
- 状态：设计已确认（范围已收窄），待落实现计划
- 分支：`claude/cool-goodall-5c83b7`
- 交付：单个 PR（前后端同 PR）

## 1. 目标与范围

让 MinerU 从 **pdf / docx(word) / pptx(ppt)** 文档中抽出的**内嵌图片真正保留下来**：落盘成可见资产、在前端来源视图里按阅读顺序渲染出来；图注（caption）/已 OCR 的文字继续进检索。

**明确不做（用户 2026-07-16 收窄）**：
- **不支持裸图片文件上传处理**——不把 `.png/.jpg/...` 加进可上传源类型，不写 `parse_image()`，`parse_source_file` 的类型分派**不动**。
- 不新增旧版 `.doc/.ppt`、不扩 `batch_ingest` 后缀（原「顺带一致性」项全撤）。
- 不做图片语义理解 / VLM 图生文描述——只保留 MinerU 给的原图 + 其 caption。

用户原话脉络：「把能用 mineru 处理的都走 mineru + 图片两者都要」→ 收窄为「**我们仅支持 ppt/word/pdf 中的图片，不支持裸图片的处理**」。

## 2. 现状：图片为什么现在丢了

pdf/docx/pptx 已经走 MinerU 优先解析（`parsers.py:341/156/228`）。但 MinerU 抽出的内嵌图有两道关卡被丢弃：

1. **HTTP 模式压根不让 MinerU 回传图片**：请求字段写死 `"return_images": "false"`（`mineru_client.py:71`），内网 mineru-api 连图片字节都不传回。
   - CLI 模式：图片 dump 到临时目录，读完 `*_content_list.json` 就 `TemporaryDirectory` 销毁，图片没留下。
   - 云端模式：`_content_list_from_zip`（`mineru_cloud_client.py:95`）只从 zip 取 content_list，zip 里本含 `images/` 却没抽。
2. **content_list→元素映射丢图**：`mineru_content_list_to_elements` 的 image 分支（`parsers.py:529`）只把 caption 当文本，`img_path` 被无视；**无 caption 的图整块 `continue` 丢弃**（大多数插图没被识别出 caption → 直接消失）。

结论：整条链路是「纯文本进检索」定位，图片要么退化成一句图注、要么彻底消失。

## 3. 改动概览（前后端同一 PR）

改动**全部集中在图片保留链路**，不碰文件类型分派、不新增可上传类型。

### A. 三种 MinerU 模式统一「把图带回来」 `mineru_client.py` / `mineru_cloud_client.py`
- `parse()` 返回从「只 content_list」扩展为「content_list + `images: {img_path: bytes}`」（返回 `(content_list, images)` 或小 dataclass，二选一在实现计划定；保持 `parsers.py` 单测无需装 MinerU）。
- **HTTP 模式**：`return_images` 由新配置驱动（默认开，见决策①），响应里的图片（base64 / data-uri，按 `img_path` 键）解码为 bytes。
- **CLI 模式**：临时目录销毁前，把 `images/` 目录按 `img_path` 读成 bytes 映射。
- **云端模式**：从已下载的 zip 里额外抽 `images/`（复用同一 zip，无额外网络）。

### B. content_list 映射保留图片 `parsers.py`
- 改 image 分支：不再丢弃无 caption 的图。产出 `image` 元素——metadata 带 `asset_id`（引用落盘图片）+ 页码/序号，text 保留 `caption`（有则留，无则用占位如 `p.N 图 M`），既可检索又不丢图。
- `mineru_content_list_to_elements` 需要能拿到 `images` 映射与「落盘写资产」的回调/服务——通过参数注入，保持纯函数可测（写盘副作用由调用方 `source_ingestion` 提供）。

### C. 图片资产存储（复用现成资产层） `knowhow/assets.py` + 迁移
- 复用 `AssetService` 落盘布局（`storage_dir/assets/<notebook_id>/<asset_id>.<ext>`）与 serving（`GET /api/notebooks/{id}/assets/{id}`，`routes.py:453`）。mime 白名单已含 png/jpeg/gif/webp；MinerU 抽图统一按其真实格式落盘（tiff/bmp 若出现则转 png，避免扩白名单）。
- 加 **source 关联**：资产需能反查 `source_id`（+ 页码/序号），供来源视图按序渲染、删源级联清理。走新 `_migration_N` + bump `SCHEMA_VERSION`（按 [[schema迁移约定]]：追加新迁移、不塞已封版迁移，否则已部署库版本闸短路漏建）。给 `notebook_assets` 加可空 `source_id` 列 vs 新 `source_assets` 表，在实现计划定（倾向前者，surface 最小）。

### D. serving
- 复用/扩展资产端点返回图片 `FileResponse`（正确 mime + `require_notebook_read` 守卫，经 source 所属 notebook 鉴权）。若沿用 `GET /notebooks/{id}/assets/{id}` 则无需新端点。

### E. 前端渲染 `frontend/app/page.tsx`

**显示语义（已确认）：图片「跟着 chunk 走」——不做独立检索/引用单元**（检索仍是 chunk-native，图片元素的 caption 随 chunk 被检索；图片本体不进 embedding、不单独成引用目标，避免稀释纯文本检索信噪比）。因此分两个显示场景：

- **场景一 · 来源正文（核心）**：`ElementBody`（`page.tsx:5366`）加 `image` 分支：`<img src={assetUrl} alt={caption}>` + caption 文本，用元素 metadata 里的 `asset_id` 拼 serving URL，按 content_list 阅读顺序内联在来源正文（顺序天然保留）。元素列表渲染点见 `page.tsx:4501`。
- **场景二 · 引用/证据（方案 a：文字 + 跳转，不出图字节）**：引用/证据卡（`EvidenceLine` `page.tsx:5384`、`KgEvidenceCard` `page.tsx:5407`、报告证据 `page.tsx:5136`）**沿用现有文字渲染**（`quoted_span`/`element_text` = 含 caption 的 chunk 文本），**不改 evidence/检索数据结构**、不把图片字节塞进答案流。用户点击引用**跳转到来源正文对应元素**，在场景一里看到完整图片。可选轻量润色：cited chunk 含图片元素时给一个「🖼 含图」标记（判断成本低则做，否则略）。

前后端同一 PR 交付（按 [[frontend-backend-co-design]]）。

## 4. 已确认的决策

**决策①：`return_images` 默认开 + 护栏。** 新配置 `MINERU_RETURN_IMAGES`（默认 `true`）。护栏：单图大小上限（`MINERU_MAX_IMAGE_BYTES`）、每源最多张数（`MINERU_MAX_IMAGES_PER_SOURCE`），超限丢弃并记事件；`MINERU_RETURN_IMAGES=0` 可整体关停回到「只文字/caption」。让内网 mineru-api 回传载荷与磁盘占用有界、可关。（按 [[efficiency-first-mandate]]：新增载荷/存储必须可 gate。）

**决策②：单个 PR。** 范围收窄后覆盖线已无，图片保留是一件内聚的事；且前后端同 PR。之前「拆两 PR」的前提（覆盖 vs 保留两条线）不复存在。

## 5. 回退 / 降级（「MinerU 出问题回退默认」）

图片保留是叠加在既有解析之上的增强，**任何环节失败都不能影响已跑通的文本解析**：

| 情形 | 行为 |
|------|------|
| `MINERU_RETURN_IMAGES=0` 或该模式拿不到图 | 退化为「只 caption 文本」（等于现状，但不再丢无 caption 的图——给占位文本） |
| 抽图/写盘单张失败 | 跳过该图、记事件，其余图与全文文本照常 |
| MinerU 整体 off/失败 | 沿用现有回退（pypdf / python-docx / XML 提取），无图但文本不受影响 |

不变量：pdf/docx/pptx 的既有「MinerU 优先 + 回退」路径与文本产出**行为不变**，图片保留是纯增量。

## 6. 测试策略
- `parsers.py` 单测（无需装 MinerU）：喂造好的 content_list（含 image 块）+ images 映射 + 假写盘回调，断言 image 元素带 `asset_id`、有/无 caption 都不丢、占位文本正确。
- MinerU client 三模式：HTTP（mock urlopen 返回带 images 的 payload，验 `return_images` 配置驱动）、CLI（mock 子进程 + 临时 images 目录读出）、云端（造含 `images/` 的 zip）。
- 护栏：超大图/超张数被丢弃并记事件；`MINERU_RETURN_IMAGES=0` 时零图、文本照常。
- 降级：某模式无图 → caption-only、不丢图；MinerU off → 原回退路径文本不变。
- 端到端：上传含图 PDF → 图片资产可 `GET` 到 + 前端元素渲染 `<img>` + 删源级联清理。
- schema 迁移：新库全量建 + 已部署库 ALTER 补列/表（按迁移约定覆盖已部署补建路径）。

## 7. 不在本次范围
- 裸图片文件上传 / 处理（`.png/.jpg/...` 作为源）。
- 图片作为**独立检索/引用单元**（图片本体进 embedding、单独成证据）——图片跟着 chunk 走，引用走「文字 + 跳转」（方案 a）。证据卡直接出缩略图（方案 b）为可选快速跟进，非本次范围。
- 旧版 `.doc/.ppt`、`batch_ingest` 后缀扩展、`SUPPORTED_SOURCE_SUFFIXES` 变更。
- `.xlsx/.csv` 走 MinerU（保持结构化解析）。
- 图片语义理解 / VLM 图生文。
- knowhow 表格单元格资产（已有独立机制）不动。

## 8. 关键文件锚点
- `backend/app/services/mineru_client.py`（HTTP/CLI，`return_images`、CLI 拷图，`parse()` 返回扩展）
- `backend/app/services/mineru_cloud_client.py:95`（zip 抽 `images/`）
- `backend/app/services/parsers.py:453/529`（`mineru_content_list_to_elements` image 分支保图）
- `backend/app/services/source_ingestion.py:454-500`（parse 阶段：把 images 落盘为资产、注入写盘回调）
- `backend/app/services/knowhow/assets.py` + `backend/app/api/routes.py:429-463`（资产存储/serving 复用）
- `backend/app/core/config.py:425-449`（MinerU 配置，新增 `MINERU_RETURN_IMAGES` + 护栏）
- 迁移文件（新 `_migration_N` + bump `SCHEMA_VERSION`，source↔asset 关联）
- `frontend/app/page.tsx:5366`（`ElementBody` 加 image 分支）
