# 检索结果带图 + Markdown 图片打包上传 设计方案

- 日期：2026-08-18
- 状态：已交付；其中 §3 的「ZIP 仅前端解包」裁决已于 2026-08-24 被后端原生 ZIP 摄取取代（见文末变更记录）
- 范围：两个互相独立的特性。特性一让「图注被检索命中」的图片出现在问答引用里；特性二让带本地图片链接的 markdown 能连图片一起上传。二者合起来闭环：图片进得来、也出得去。

---

## 0. 产品裁决（已确认，本方案的前提）

1. **图注是图片检索的唯一入口**：有图注的图片，图注命中即带出图片；无图注的图片不可检索，属正常行为，不做任何补救。
2. **模型不看图**：本期不引入任何多模态调用。附图是「证据片段附近的图」，展示层必须与证据归因区分，不冒充模型引用过的证据。
3. **VLM 生成图注、按周边文字打标签**：明确留到后续阶段，本方案不设计。
4. **md 携带资源的形态以 zip 为主**（文件夹拖拽为同构增强），前端解包归一为自包含 md，服务端不收 zip。

> 2026-08-24 更新：第 4 条保留为历史决策，不再是当前合同。用户反馈证明浏览器专属路径让 MCP/CLI 与网页能力分叉，也使后台拿不到包内图片。当前合同是：原始 ZIP 作为一个来源上传并由 backend builtin `markdown_bundle` 解析、相对图片落资产；拖入文件夹仍走本文原浏览器内联兼容路径。

---

## 1. 现状事实（实现依据，均已核实）

### 图片管线

- 图片二进制落磁盘 `assets/<notebook_id>/<asset_id>.<ext>`，元数据在 `notebook_assets` 表；mime 白名单仅 png/jpeg/gif/webp，SVG 因存储型 XSS 刻意排除（`backend/app/services/knowhow/assets.py`）。
- 读取走带鉴权的 `GET /api/notebooks/{id}/assets/{asset_id}`，含跨库（挂载参考库）代理与 `no-store` 规则（`backend/app/api/source_routes.py`）。
- image 元素：`text` = 图注（无图注但落了资产时为「Markdown 图 N」占位），`metadata.asset_id` 指向资产。既无图注又未落资产的图整条丢弃（`backend/app/services/parsers.py` 的 `if not caption and not asset_id: continue`）。
- **带图注的图片元素以图注文本进检索 chunk，且其 element id 记入 chunk 的 `element_ids`**（`backend/app/services/chunking.py:53`、`backend/app/repositories/sqlite/chunk_store.py::source_elements_for_chunking`）。无图注的图片不进 chunk。

### 检索与引用装配

- `RetrievedChunk` 带 `element_ids`（`backend/app/services/retrieval.py`），chunk 模式与 agentic search（`reasoning`）共用这一形状——`ReasoningResult.chunks` 就是 `List[RetrievedChunk]`，PPR / 精确查找 / 图扩展带回的 chunk 同形。
- KG 命中的证据绑定带 `element_id`（`backend/app/services/evidence_context.py::citations_from`）。
- 三个 Ask 模式的引用装配都在 `ask_service.py`：`ask_chunk`、`ask_reasoning`、`ask_graph`。
- **reasoning 模式的权威显示路径是 `AnswerAnchor`（`[k]` 锚点），`Citation` 只是回退列表**；chunk 锚点目前连 `source_id`/`element_id` 都可为空（`backend/app/models/ask.py`）。两个模型都不带 `asset_id`。
- 前端真的渲染图片的只有两处：来源详情弹窗（`ElementBody` + `AuthedImage`）、问答集合枚举清单卡（`TypedCollectionItem.asset_id`，默认折叠、展开才挂载，`AuthedImage` 内部再按视口懒加载）。**因此 agentic search 今天已经能经枚举工具显示图片**；断的只是常规相关性命中这条路。

### markdown 摄取与上传

- md 走内置解析器（刻意不经 MinerU）。`![alt](src)` 只有 data URI（四种 mime）落资产；http(s) 与相对路径原样存 `metadata.src`，不下载、前端不渲染。
- 上传：9 后缀白名单（zip、独立图片文件都不收）；multipart 合同紧（20 文件/批、40 标量 part、字段白名单 422）；前端原样字节直传，唯一读内容先例是上传前读 8KB 做类型检测。

---

## 2. 特性一：图注命中 → 引用带图

### 2.1 产品行为

- Ask 三模式（chunk / reasoning / graph）中，答案的 `[k]` 锚点弹层与引用卡在其绑定证据包含带图注图片时，显示「本段附图」缩略图；点击可放大或跳转来源详情。
- 附图区与引证内容视觉区分，标注为「本段附图」——不是模型引用的证据。
- 缩略图用既有 `AuthedImage`（鉴权 fetch + 视口懒加载），弹层不打开不发请求。
- 跨库证据的附图经既有 active-notebook 资产代理端点获取，无新增权限面。
- 旧持久化答案缺新字段时回退纯文本，零 migration。
- 公开报告分享页 v1 **不带图**：白名单投影红线，`asset_id` 与 `element_id` 同类，不跨出匿名边界。

### 2.2 后端改动

1. **响应模型**：`AnswerAnchor` 与 `Citation` 各加可空字段 `images: List[CitationImage]`，`CitationImage = {element_id, asset_id, caption}`；沿用 `exclude_if` 惯例，空列表整体从 JSON 缺席。字段必须加在 `AnswerAnchor` 上——否则 reasoning 主路径（锚点）看不到图，只加 `Citation` 只覆盖回退列表。
2. **共享 enrichment**（建议放 `evidence_context`）：输入一批 element_id → 一次有界批量 PK 读 `source_elements` → 滤 `element_type='image'` 且 `metadata.asset_id` 非空 → 返回 `{element_id: CitationImage}` 映射。零新增模型/embedding 调用；knowhow/memory 投影行没有这个形状，天然不命中，无需特判。
3. **装配点接线**（四处，全部在引用/锚点装配已持有完整证据对象的位置）：
   - `ask_chunk` 的 chunk 引用：按 `RetrievedChunk.element_ids` 反查；
   - `ask_reasoning` 的锚点装配：chunk 锚点经 `chunk_by_id` 拿 `element_ids`；element 锚点直接判元素类型；KG 锚点/`citations_from` 用 `evidence.element_id`；
   - `ask_graph` 的两处 chunk 引用：同 chunk 模式。
4. **上限**：每锚点附图 ≤ N 张、每答案合计 ≤ M 张，具名常量（协议边界，非部署预算），超限按元素 id 序截断。精确数值只登记在 `docs/product-and-api*.md`（建议 N=3、M=12，review 时定）。

### 2.3 前端改动

- `answer-panel.tsx` 的锚点弹层（`CitationPopover`/`SelectedReferenceDetail`）与引用卡新增「本段附图」区：`AuthedImage` 渲染、点击放大/跳来源详情；缺字段（旧答案）不渲染该区。
- 复用集合枚举卡的懒挂载纪律：弹层未打开不挂载图片条目。

### 2.4 测试与守卫

- 后端：enrichment 单测（有图注 / 无图注不出现 / 无资产回退 / 跨库 / 上限截断）；`AnswerAnchor`/`Citation` 新字段跑架构守卫 `--rebaseline-surface` 刷 `api_contract`。
- 前端：component 测试——引用卡带图渲染、缺字段回退、弹层关闭不发图片请求（对齐枚举卡既有测试形态）。

### 2.5 明确不做（本期）

- VLM 图注、周边文字打标签（远期，接入后自动进入同一管线）。
- 无图注图片的「邻近带出」。
- 公开分享页附图。
- 深度报告引用卡附图 → 开放问题 §4.1。

---

## 3. 特性二：Markdown + 图片 zip 打包上传

### 3.1 产品行为（用户故事)

1. 用户把 md 与其引用的图片（保持相对路径结构）打成 zip——Notion/语雀/HackMD 的导出本来就是这个形态——或**直接拖整个文件夹**，拖入添加来源弹窗。
2. 前端解包/读目录：
   - 恰好一个 `.md` → 直接用；多个 → 列出让用户勾选，每个勾选的 md 成为一个独立来源；零个 → 明确报错「压缩包里没有 markdown 文件」。
3. 解析每个 md 的 `![alt](src)`，`src` 为相对路径时**相对于该 md 在包内的所在目录**配对包内文件（处理 `./`、`../`、`%20` 等 URL 编码；精确匹配失败时提示近似候选，不静默丢）。
4. 配上的图片按**魔数**（不按后缀）嗅探 mime：png/jpeg/gif/webp 直接 base64 内联；其他类型（含 SVG）列入「不支持」清单，保留原链接文字。
5. 预检：内联后的 md ≤ 单文件上限（`source_upload_max_bytes`）；超限即拦、给出体积明细。
6. 改写后的**自包含 md 走现有上传通道**——服务端零改动，`persist_image` 护栏、mime 白名单、张数/单图上限原样生效。
7. 弹窗内**持久**列出配对回执：「N 张已内联 / M 张未找到（列路径）/ K 张不支持（列原因）」——对齐「被跳过文件逐条持久列出、不能只发即逝 toast」的既有红线。无 alt 的图片在回执中提示「无图注，上传后无法被检索」。
8. 云端 `http(s)` 图片链接：v1 **不拉取**，保持现状（有 alt 留文字）。

### 3.2 技术要点

- **zip 解析零新依赖**：central directory 解析是纯 JS 小函数；deflate 解压用浏览器原生 `DecompressionStream('deflate-raw')`（主流浏览器均支持）。不引入 jszip/fflate（依赖政策）。加密条目、zip64 超大条目按不支持处理，明确报出。
- **解压护栏（防浏览器端 zip bomb）**：解出总字节上限（`source_upload_max_bytes` × 固定系数）、条目数上限、不递归嵌套 zip；超限即停，报「压缩包过大/条目过多」。
- **文件夹拖拽**：`webkitGetAsEntry` 递归读目录、保留相对路径，与 zip 归一为同一「虚拟文件集」结构，共用配对/内联管线。
- **入列分支**：`classifyStagedFiles` 增加 zip 分支——`.zip` 只是前端识别的交换格式，**不进后端上传白名单**、不进 `supportedSourceExtensions`；解包产出的 md 以正常文件身份入列（计入 20 文件/批与文档配额）。
- **代码位置**：纯函数管线（zip 解析、路径配对、内联改写）放 `frontend/app` 新模块（如 `md-bundle.ts`），弹窗接线在 `page.tsx`；生产/测试目录红线照常。
- hash 去重按改写后字节：同一 md 配不同图集是不同来源，语义自然。

### 3.3 后端改动

- 主管线**零改动**。
- 唯一建议项（→ 开放问题 §4.3）：`GET /system/config` 增发 `image_max_bytes` / `max_images_per_source`（取自既有 Settings），供前端在配对阶段预检提示「第 X 张图超过单图上限，上传后将被跳过」；不发则前端只能按文件总大小盲检，服务端护栏兜底（图会被静默跳过、仅保留图注）。

### 3.4 测试

- 前端 unit（用例表驱动）：central directory 解析、路径解析/配对（`./`、`../`、`%20`、大小写、未找到、撞名）、内联改写、mime 嗅探、护栏（总量/条目数超限）、多 md 勾选。
- 前端 component：弹窗配对回执持久显示、超限拦截文案。
- 后端不动：现有 data URI 摄取测试即覆盖管线终点。

### 3.5 明确不做（本期）

- 后端原生收 zip（zip 成为一等来源类型）：留到出现 CLI/MCP/批量导入场景再立项，届时前端管线退化为校验预览器，不浪费。
- 云端 URL 拉取（服务端拉取的 SSRF 护栏方案、前端 best-effort CORS 拉取均留后续）。
- bmp/avif canvas 转码 → 开放问题 §4.4。
- SVG、加密 zip、zip64。

---

## 4. 开放问题（请 review 时拍板）

1. **深度报告引用卡要不要同批带附图？** 报告逐字复用 `ReasoningRetriever`，证据形状相同，但 references 是 `report_engine` 另一个装配点，需单独接线（估计为特性一的 ~1/3 增量）。建议：本期做，一次把「图注命中带图」做齐三个消费面（Ask 锚点、Ask 回退引用、报告引用）。
2. **无 alt 图片要不要在上传弹窗提供图注补全编辑器？** v1 建议只在回执中提示、不做编辑（避免弹窗复杂化；用户可改 md 后重传）。
3. **`/system/config` 增发图片护栏值（§3.3）做不做？** 建议做，改动极小，换来预检提示的准确性。
4. **bmp/avif 前端 canvas 转码？** 建议 v1 不做，列入「不支持」清单。
5. **附图上限数值**：建议每锚点 3、每答案 12（登记入 `docs/product-and-api*.md`）。

---

## 5. 任务拆分与顺序

两个特性互相独立，可并行：

| 任务 | 内容 | 依赖 |
| --- | --- | --- |
| T1 | 后端：`CitationImage` 模型 + enrichment + 四处装配点 + 单测 + `api_contract` 重基线 | — |
| T2 | 前端：锚点弹层/引用卡附图渲染 + component 测试 | T1 |
| T3 | 前端：zip/文件夹「虚拟文件集」解析 + 配对/内联纯函数管线 + unit 用例表 | — |
| T4 | 前端：添加来源弹窗接线（zip 入列分支、配对回执 UI）+ component 测试 | T3 |
| T5 | （视 §4.3）后端 `/system/config` 增发护栏值 + 前端预检 | T3 |
| T6 | （视 §4.1）报告引用卡附图（`report_engine` 装配 + `report-view.tsx` 渲染） | T1 |
| T7 | 文档同步：`README.md`×2、`AGENTS.md`、`CLAUDE.md`、`docs/product-and-api*.md`（附图上限、zip 交互与护栏数值登记） | 全部 |

### 涉及红线自查清单

- 全栈对等：两特性均前后端同批交付（特性二本就以前端为主体）。
- 数值上限：附图 N/M、zip 解压护栏系数均用具名常量，精确值只登记 `docs/product-and-api*.md`。
- 公开分享白名单：`asset_id` 不跨匿名边界。
- 依赖政策：zip 解析零新依赖；如实现中确需引库，先停下来问。
- 界面词汇：「图注」「本段附图」「压缩包」均为界面词，不出现内部黑话。

---

## 6. 2026-08-24 后端原生 ZIP 变更记录

- `.zip` 进入 backend parser capability registry 与 `supportedSourceExtensions`，浏览器原样上传，一个压缩包对应一个来源；包内所有 `.md`/`.markdown` 按稳定路径顺序解析，元素记录 `bundle_path`。
- 图片路径按各自 Markdown 成员目录解析，支持 `./`、`../`、包根路径、URL 编码与 query/fragment；png/jpeg/gif/webp 按魔数准入并经既有 `persist_image` 端口落资产。缺失/远程/不支持图片逐图 fail-open，整包结构或总量错误原子拒绝，归档从不解到宿主文件系统。
- MCP 新增 `add_source_file`，以严格标准 base64 上传解析注册表支持的 PDF、DOCX、PPTX、工作簿、Markdown 与 ZIP 等文件，复用同一去重、文档上限、Agent 来源出处与后台调度路径；默认 core 工具面由 22 增至 23。
- 本文 §3.1–§3.5 对 ZIP 的前端多 Markdown 勾选、前端解压护栏、服务端零改动与「后端原生收 ZIP 不做」均为历史实现记录；只对拖入文件夹仍有效。当前权威行为与数值护栏见 `docs/product-and-api.md` / `_zh.md`。
- 上传弹窗回执：跳过项逐条持久列出。
- 守卫有效性：新增守卫测试按「删除 + 移动」双变异验证。
