# Office 文档（Word/PPT）经 MinerU 解析 + 上传类型锁定

日期：2026-06-25
状态：设计已确认，待写实现计划

> **2026-08-10 更新**：本文里「xlsx 仍用 openpyxl / 不纳入 MinerU」这条范围决策已被用户拍板**反转**。
> xlsx/xlsm 现已接入 MinerU 优先路径 + 行/格覆盖对账 + openpyxl 兜底；降级质量警告只覆盖 pdf/docx/pptx。
> 真源是 `backend/app/services/parsers.py` 的 `MINERU_CAPABLE_SUFFIXES` 与 `MINERU_FALLBACK_WARNING_SUFFIXES`
> 两个常量。
>
> 同批：docx/pptx 的**本机兜底链本身**也已升级，不再是下表里的「python-docx 仅抽段落」「原始 XML 直读」。
> DOCX 现为 mammoth 语义化 HTML（保住标题层级/列表/表格结构）→ python-docx 最后兜底；
> PPTX 现为 python-pptx（形状/表格/图表标题/备注，组合形状完全递归）→ 原始幻灯片 XML 最后兜底。
> 兜底路径的表格按 `_FALLBACK_TABLE_ROWS_PER_ELEMENT` 行切成多个 `table` 元素——`chunking.py`
> 从不切分单个元素，整张大表折成一个元素就是一个几十万字符的 chunk。
>
> 同批：旧版二进制 `.xls`（BIFF/OLE2）已接入本地 xlrd 解析（不经 MinerU——该格式 MinerU
> 不支持），入口是 `parse_xls`/`_xls_cell_text`；`.doc/.ppt` 仍维持拒绝并提示另存为。
>
> 下文正文保留原样作为历史记录，不重写。

## 背景与目标

当前来源解析现状（代码实际行为，非印象）：

| 格式 | 现在的解析路径 | 保真度 |
|---|---|---|
| PDF（上传） | `MinerUClient`（http/cli）→ 失败回退 pypdf | 高（公式/表格/版面/阅读顺序） |
| PDF（链接） | `MinerUCloudClient`（云端 v4，URL 提交） | 高 |
| **docx** | `parse_docx`：python-docx，仅抽段落 + 表格文本 | 低，无版面/公式/图 |
| **pptx** | `parse_pptx`：原始 XML 直读，幻灯片文字 + 备注 | 低 |
| xlsx/xlsm | `parse_xlsx`：openpyxl，逐行 | 表格行 |
| csv | `parse_csv` | 表格行 |
| md/markdown | `parse_markdown`（structural） | 结构化 |

后端 `SUPPORTED_SOURCE_SUFFIXES`（`backend/app/api/routes.py:71`）与前端 `accept` 其实**已包含** docx/pptx/csv/xlsx——它们已能上传与解析，只是 office 走的是简陋解析器。

**本次目标：**
1. 让 **docx / pptx** 走 MinerU 高保真解析（MinerU 3.0 起原生支持 PPTX/XLSX、3.1 起原生支持 DOCX，无需 LibreOffice），MinerU 未配置/失败时回退到现有轻量解析器。
2. 上传时只允许我们支持的文件类型，对不支持的类型（含遗留 `.doc/.ppt/.xls`）**明确拒绝并提示**，不再静默丢弃。

**已确认的范围决策：**
- 纳入 MinerU 的格式：**仅 docx + pptx**。xlsx 继续用 openpyxl（纯表格数据，MinerU 收益有限）。
  （**2026-08-10 更新**：此条已反转——xlsx/xlsm 也走 MinerU 优先，非空产出须先过**行 + 格**覆盖对账才采信
  （只数行看不出宽表丢列），任一维度覆盖不足或 MinerU 失败时整份丢弃回落 openpyxl；因 openpyxl 兜底对单元格
  值全保真，工作簿不打降级警告。云端上传分支走同一道对账，且图片只在采信后才持久化。）
- 回退策略：MinerU 未配置或解析失败时 → **回退到现有轻量解析器**（镜像 PDF→pypdf），永不阻断入库。
- 遗留二进制 `.doc/.ppt/.xls`（非 OOXML）：**不支持、明确拒绝**（MinerU 不支持这些；不引入转换依赖）。

## 方案选择

**采用方案 A：在 `parsers.py` 中复刻 PDF 的 MinerU-优先 + 回退模式。**

- 方案 A（选中）：`parse_docx`/`parse_pptx` 先试 `MinerUClient.parse()`，用通用化 mapper 映射 content_list，失败/未配置回退现有实现。复用现成 MinerU 客户端与 mapper，改动面最小，回退是已验证模式，可用假 client 单测。
- 方案 B（弃）：office→PDF 转换后复用 `parse_pdf`。需给部署加 LibreOffice，有转换损耗；MinerU 原生支持 office，转换多余。
- 方案 C（弃）：新建独立 office MinerU 模块。office 走同样的端点/CLI，独立模块与 `MinerUClient` 逻辑重复。

## 设计详情

### ① 后端解析流程 — `backend/app/services/parsers.py`

- `parse_source_file`：把 `mineru_client` 也传给 `parse_docx`/`parse_pptx`（当前仅传给 `parse_pdf`）。
- `parse_docx(source_id, path, file_name, mineru_client)` 与 `parse_pptx(...)`：新增 MinerU 优先路径，**完全镜像 `parse_pdf`**：
  - `mineru_client is not None and getattr(mineru_client, "configured", False)` 为真 → `content_list = mineru_client.parse(str(path), file_name)` → `mineru_content_list_to_elements(..., label_prefix=...)`；
  - 返回非空则用；空结果或抛异常 → 记 `last_error`，**回退**到现有轻量实现（保留当前函数体作为回退分支）。
- `mineru_content_list_to_elements`：新增 `label_prefix` 参数（默认 `"PDF"` 保持现有行为），docx/pptx 传 `"DOCX"`/`"PPTX"`，使元素 `location_label` 不再硬编码 `PDF p.X`；metadata 增加 `source_format`。其余 block 映射（text/title/equation/table/image/list）格式无关，原样复用。
  - 备注：MinerU 对 pptx 的 `page_idx` 对应幻灯片序号、docx 对应页/段序号，沿用 `+1` 的 1-based 展示即可，无需特殊处理。

### ② MinerU 客户端 — `backend/app/services/mineru_client.py`

- `parse(file_path, file_name)` 语义由「PDF」推广为「受支持文档」（函数已足够通用）。
- **http 模式**（`_parse_http`，部署默认路径，GPU 主机跑 `mineru-api`）：已是把任意文件字节 POST 到 `/file_parse` 的 `files` 字段，office 直接可用（`/file_parse` 已确认接受 docx/pptx）。现有字段（`parse_method`/`formula_enable`/`table_enable`/`backend` 等）对 office 无害，无需改。
- **cli 模式**（`_parse_cli`）：现有 `_DO_PARSE_SCRIPT` 是 PDF 语义的 `do_parse(pdf_bytes_list=...)`。office 改走已确认支持的 `mineru -p <file> -o <out_dir>` CLI 命令，复用现有 `*_content_list.json` 发现逻辑（`rglob`）。
  - 实现时先验证当前安装的 MinerU 版本里 `do_parse` 是否也接受 office 字节；若可，则统一用 do_parse，省去 CLI 分支。以 CLI 命令作为稳妥默认。
- 云端 `MinerUCloudClient`（URL 来源）**不改**——office 是本地上传，不走 URL 路径。

### ③ 前端上传锁定 — `frontend/app/page.tsx`

- 引入**单一常量** `SUPPORTED_SOURCE_EXTENSIONS`（`["pdf","md","markdown","docx","pptx","csv","xlsx","xlsm"]`），由它派生：
  - 两处 `<input accept=...>`（`page.tsx:2366`、`page.tsx:2739`）的 accept 串；
  - `stageFiles`（`page.tsx:1424`）的校验。
  - 消除当前 3 处（两 accept + 一正则）各写一份的漂移风险。
- `stageFiles`：**静默丢弃 → 明确拒绝**：
  - 选中文件按扩展名分为「合法 / 不支持」两组；合法的照常入列。
  - 有不支持项时弹 toast，列出被跳过的文件名 + 支持类型清单（PDF / Word .docx / PPT .pptx / Excel .xlsx,.xlsm / Markdown / CSV）。
  - 对遗留 `.doc/.ppt/.xls` 给**专门提示**：「不支持旧版 Office 格式，请另存为 .docx/.pptx/.xlsx」。
- `accept` 仅作软提示（用户切「所有文件」或拖拽可绕过），真正的门槛是 `stageFiles` 校验 + 后端 400。

### ④ 错误与回退

- office 失败/未配置 MinerU → 回退轻量解析器，永不阻断入库（镜像 PDF→pypdf）。
- `.doc/.ppt/.xls`：后端 `SUPPORTED_SOURCE_SUFFIXES` 本就不含 → 已 400 拒绝；前端新增专门提示。双重防御。
- office 与 PDF 共用 `mineru_timeout_seconds` 与同样的同步解析流程，无新基础设施。
- 失败可观测：复用现有 `last_error` 机制（与 PDF 一致），便于排查「office 没走 MinerU」的原因。

### ⑤ 测试

- 新增 `backend/tests/test_parsers_office.py`，仿 `test_parsers_markdown.py` 风格，用假 `mineru_client` 覆盖三态：
  - configured 且 `parse()` 返回 content_list → 得到 mineru 元素（`parser=="mineru"`、`location_label` 前缀为 DOCX/PPTX）；
  - 未配置（`configured=False`）→ 走轻量解析器（python-docx / XML）；
  - `parse()` 抛异常 → 回退轻量解析器且不冒泡异常。
- `mineru_content_list_to_elements` 的 `label_prefix` 泛化单测（默认仍为 PDF）。
- 前端拒绝提示：手动 + preview 验证（含选入 .doc 的专门提示、混合合法/非法批次的部分入列）。
- 全量 `pytest` 与前端 `tsc` 绿。

### ⑥ 明确不做（YAGNI）

- xlsx 仍用 openpyxl（**2026-08-10 更新：已反转，见文首**）；不加图片（png/jpg）上传；不支持 `.doc/.ppt/.xls`；office-URL 不走云端；不把同步解析改异步；不加 `MINERU_OFFICE_ENABLED` 开关（回退已覆盖旧版 MinerU；旧服务器对 office 会快速报错而非长挂，无需额外开关）。

## 影响面

- 改动文件：`backend/app/services/parsers.py`、`backend/app/services/mineru_client.py`、`frontend/app/page.tsx`、新增 `backend/tests/test_parsers_office.py`。
- 配置：无新增 env（沿用现有 `MINERU_*`）。
- 行为变化：部署已配置 MinerU 时，docx/pptx 自动升级为高保真解析；未配置时与现状一致。对既有 PDF 路径无行为改变（mapper 默认 `label_prefix="PDF"`）。
