# 设计：`source.element_enricher` 扩展点复活 + 电路图样板插件

状态：已批准（2026-09-13，用户裁决「同意走这条路」）。基线 master `df14096cb`。
本文是实现计划 `docs/superpowers/plans/2026-09-13-source-element-enricher.md` 的规格来源。

## 0. 需求与结论

用户需求：以部署插件的形式接入「给一张图片 → 判断是否电路图 → 若是则返回网表与功能描述」，
并在解析（parse）阶段按内容自动触发。样板只需打通链路，不追求识别准确性；模型用
DeepSeek `deepseek-flash`（OpenAI 兼容 `chat/completions`，图片走 `image_url` data URI）。

现状核实（行号对本 worktree）：

- `source.parser_chain` 对插件内容不可见：`ParserExtensionContext` 只有 `(kind, suffix)` 与回调
  核心的 `probe`（`backend/app/extension_sdk/parser.py:83`）；核心侧 `route()`/`probe()` 只认三个内置
  id（`backend/app/services/parser_chain_execution.py:207,242`）。第三方链节注册后一律
  `unknown_parser_provider`。
- 「解析器不进用户可选范围」是已登记裁决（索引管线规格 §一.2）。
- `indexing.pipeline` 的 `IndexingSourceElement` 无 `asset_id`、产出只能是 chunk/KG 提案。
- 为这类需求设计的 `source.element_enricher`（#551，commit `3f8413a8f`）在 B2 #569 因零消费者删除；
  其 `ElementView` 也没有图片字节。
- 图片元素形状：`element_type="image"`，`metadata.asset_id`/`caption`/`description`（
  `backend/app/services/parsers.py:1583-1612`）；资产由 `persist_image` 在 materialize 阶段已落库落盘
  （`notebook_assets` 行已提交，文件在 `AssetService.path_for(asset)`）。
- chunk 入库对 image/figure 元素的门是 `metadata.caption` 或 `metadata.description` 非空
  （`backend/app/services/chunking.py:53`，metadata 由 `repositories/sqlite/chunk_store.py:182` 展开），
  **检索文本只取元素 `text`**。
- 前端来源详情 image 分支已渲染 `metadata.description`（`frontend/app/page.tsx:7780`），逐行 `<p>`。
- 单独 png/jpg 不是受支持来源类型；图片以 PDF/docx/md/zip 内嵌形式进入。
- 核心模型端口是纯文本 chat（`_WORKLOAD_LABELS` 无视觉工作负载）；插件自带 HTTP 客户端。

结论：复活 `source.element_enricher`，补图片读取端口，挂在解析产物落库前；插件自行判定
「是否电路图」；产出落进既有元素 metadata + `text`，不开新表。

## 一、用户已拍板的边界

1. **不改解析路由**：判定按元素、在解析之后做；核心不按内容路由解析器。
2. **插件拥有策略，核心拥有 schema**：网表/功能描述写进 `elements.metadata`，描述同时进 `text`
   以便检索；不新增表、不新增前端插槽。
3. **样板出厂关闭、零补丁接入**：`examples/extensions/circuit-diagram/`，启用 = 部署方
   `EXTENSIONS_CONFIG` 点名 + 环境变量给 key。
4. **样板不关心准确性**：只要求端到端链路可验证（零网络 e2e）；真实模型的效果由用户手动试。

## 二、扩展点契约（SDK：`backend/app/extension_sdk/element_enrichment.py`）

```python
SOURCE_ELEMENT_ENRICHER_POINT = "source.element_enricher"

@dataclass(frozen=True)
class ElementRef:                 # 请求内不透明句柄；候选必须原样返回同一对象
    token: object

@dataclass(frozen=True)
class ElementView:                # 核心拥有的只读投影；解析器 metadata 不跨 SDK
    ref: ElementRef
    element_type: str
    location_label: str
    text: str
    caption: str = ""
    description: str = ""
    asset_id: str = ""            # 非空 = 该元素带一张已落盘图片
    asset_mime: str = ""          # 与 asset_id 成对；无图为空

@runtime_checkable
class ElementAssetReader(Protocol):
    def read(self, ref: ElementRef) -> bytes | None: ...
    # 返回该元素图片字节；无图/超上限/读失败/请求已结束 → None，绝不抛。
    # 只在本次 enrich 调用期间有效；调用返回后再读恒 None。

@dataclass(frozen=True)
class ElementEnrichmentBudget:
    max_proposals: int
    max_metadata_bytes: int       # 本 contribution 本次落库 extensions 子树总字节
    max_description_chars: int
    max_asset_bytes: int          # reader 单图上限（= MINERU_MAX_IMAGE_BYTES）
    deadline_monotonic: float

@dataclass(frozen=True)
class ElementEnrichmentAvailabilityContext:
    plugin_id: str
    contribution_id: str
    element_count: int
    image_count: int              # 带 asset 的元素数
    deadline_monotonic: float

@dataclass(frozen=True)
class ElementEnrichmentContext:
    elements: tuple[ElementView, ...]
    assets: ElementAssetReader
    cancellation: CancellationToken
    budget: ElementEnrichmentBudget

@dataclass(frozen=True)
class ElementEnrichmentCandidate:
    element: ElementRef
    metadata: Mapping[str, Any]   # 键 ^[a-z][a-z0-9_]{0,63}$，JSON 标量/列表/映射，深度 ≤ 12
    description: str = ""         # 可检索的说明文本；可含 ``` 围栏代码块

@runtime_checkable
class ElementEnricher(Protocol):
    def enrich(self, context: ElementEnrichmentContext) -> ContributorResult[ElementEnrichmentCandidate]: ...
```

规则：

- Contribution kind = `CONTRIBUTOR`，经 `registrar.add_contributor` 注册；`availability` 走
  `registry.availability(contribution_id, ElementEnrichmentAvailabilityContext)`（与 gap_consult 同口）。
- 视图给**全部**解析元素（插件按 `element_type`/`asset_id` 自筛）；图片字节按需经 `assets.read`。
- 一个 contribution 对同一元素最多一条候选；`ContributorResult.status` 为 `UNAVAILABLE` 时整批丢弃，
  `PARTIAL` 照常准入。
- 插件返回 `description` 时，核心把它作为检索文本；不允许改 `caption`（旧契约的 caption 规则删除）。

## 三、宿主（`backend/app/extensions/element_enrichment.py`，`SourceElementEnricherHost`）

- 启动冻结：构造时校验每条 contribution `kind is CONTRIBUTOR` 且实现有 `enrich`，否则
  `ExtensionRegistryError`。`has_contributions()` 零成本读拓扑。
- 执行模型照抄 `GapConsultHost._execute`：每个 contributor 一条**私有 daemon 线程**，**不**
  `copy_context()`；availability 探针与 `enrich` 同在 worker 上、同受 `deadline_monotonic` 硬截止；
  `_JOIN_SLICE_SECONDS` 分片 join；超时 → `abandoned`、事件 `element_enricher_timeout`，并**结束整个点**
  （后续 contributor 不再启动）；插件异常 → fail-open、事件 `element_enricher_failed`，下一个继续。
- 调用方连接租约在**调用线程**检查一次（`connection_probe.is_connection_held()`），持有则整点跳过
  （`connection_lease_held`）。worker 线程**零数据库访问**：`ElementAssetReader` 只读调用线程预先解析好的
  `(path, mime)` 文件，且带 `max_asset_bytes` 上限与「调用已结束」闸（`enrich` 返回或被放弃后 `read` 恒 None）。
- 准入（宿主内做形状校验，服务层做落库合成）：候选 `element` 必须是本次发出的 `ElementRef` 同一对象；
  同一 contribution 内元素不重复；`metadata` 经 `_thaw` 规则（键正则、JSON 标量、深度 ≤ 12、非有限浮点拒绝）；
  `description` 只允许可打印字符 + `\n`/`\t`，长度 ≤ `max_description_chars`；单 contribution 落库字节
  （`persisted_element_enrichment_size`）≤ `max_metadata_bytes`；候选数 ≤ `max_proposals`。**任一违规 →
  该 contribution 整批丢弃**、事件 `invalid`，其它 contribution 不受影响。
- 事件（`event_sink`，与 `event_log.emit` 同形）：`{"kind": "source_element_enricher_attempt",
  "plugin_id", "contribution_id", "status", "reason_code", "duration_ms", "count"}`；一律稳定码，绝不带
  设置值/异常文本。

域端口（`backend/app/domain/extensions.py`，服务层只 import 这里）：

```python
@dataclass(frozen=True)
class ParsedElementEnvelope: ordinal:int; element_type:str; location_label:str; text:str; caption:str; description:str; asset_id:str; asset_mime:str
@dataclass(frozen=True)
class ElementAssetLocation: path: str; mime: str
@dataclass(frozen=True)
class ElementEnrichmentPatch: ordinal:int; plugin_id:str; plugin_version:str; contribution_id:str; metadata:Mapping[str,Any]; description:str = ""
@dataclass(frozen=True)
class ElementEnrichmentCallContext: elements; asset_locations: Mapping[str, ElementAssetLocation]; cancellation; connection_probe; max_proposals; max_metadata_bytes; max_description_chars; max_asset_bytes; deadline_monotonic
class ElementEnricherHostPort(Protocol):
    def has_contributions(self) -> bool: ...
    def enrich_application(self, call_context, *, event_sink=None) -> tuple[ElementEnrichmentPatch, ...]: ...
```

## 四、核心接线与落库

- `ExtensionRuntime.element_enrichers`（`backend/app/extensions/bootstrap.py`）→
  `application_repository_hosts()["element_enricher_host"]`（`backend/app/bootstrap.py`）→
  `create_repository` host kwargs → `SQLiteRepository`/`PostgresRepository` → `RepositoryFacade` →
  `RepositoryRuntime` foundation（属性 `element_enrichers`，`RUNTIME_ATTRIBUTES` 同步）→
  `SourceIngestionService(element_enrichers=..., resolve_element_assets=...)`。无宿主时（库/测试直构）
  `None`，服务层短路。
- `resolve_element_assets(notebook_id, asset_ids) -> Mapping[str, ElementAssetLocation]`：facade 侧**模块级
  函数**（同 `_make_persist_image` 的理由：one-hop-delegate 合同），用 `repo.get_notebook_asset(id)` +
  `AssetService(repo).path_for(asset)`；只返回 `notebook_id` 匹配且文件存在的项。
- 挂点：`backend/app/services/source_ingestion.py::_process_source_scoped`，在
  `deduplicate_repeated_page_boundaries(parsed.elements)` 之后、`with self.write()` 之前，
  **一条语句** `elements = self._enrich_parsed_elements(source, elements)`（热函数天花板 364 零松弛；
  若仍超一行，同 diff 改基线并在 PR 说明）。`stage("enrich", "start"/"done"/"skipped", ...)` 记
  `patches=` 数量。服务层适配器 `backend/app/services/source_element_enrichment.py::enrich_source_elements`
  做：宿主短路、封装 envelope、调用宿主、把 patch 合成回 `SourceElement`。
- 合成规则（服务层）：
  - `metadata["extensions"][contribution_id] = {"plugin_id", "plugin_version", "metadata"}`；同一
    contribution 已存在 → 整批拒绝。
  - `description` 非空时：`metadata["description"]` = 既有描述 + `"\n\n"` + 新描述（既有为空则直接置）；
    `text` = 既有 `text` + `" "` + 压平空白后的描述（`" ".join(description.split())`），因 image 类型
    `text` 落库是单行（`parsers._element`）。这样 chunk 门（caption/description）与检索文本（`text`）同时满足。
  - 任何一处不合规 → 返回原 `elements`（整批 fail-open），不抛。
- 取消：解析作业无取消令牌，传 `_NeverCancelled`；SDK 面仍提供 `raise_if_cancelled`。
- Settings（`backend/app/core/config.py`，登记于部署文档对）：
  - `SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS` 默认 120.0，`0 < x ≤ 900`（整点硬截止；解析作业非交互延迟）
  - `SOURCE_ELEMENT_ENRICHER_MAX_PROPOSALS` 默认 2048，`1..25000`
  - `SOURCE_ELEMENT_ENRICHER_MAX_METADATA_BYTES` 默认 1048576，`1024..16777216`
  - `SOURCE_ELEMENT_ENRICHER_MAX_DESCRIPTION_CHARS` 默认 8192，`1..65536`
  - 单图字节上限复用 `MINERU_MAX_IMAGE_BYTES`，不新增。
- 结构常量放 `backend/app/domain/element_enrichment.py`（`EXTENSION_OWNER_ID_MAX_CHARS=128`、
  `EXTENSION_OWNER_VERSION_MAX_CHARS=64`、`ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH=12`），并由反射式文档
  契约测试钉进 `docs/product-and-api*.md` 的表行（照 `test_gap_consult_docs_contract.py`）。

## 五、前端

- `frontend/app/source-element-display.ts` 新增纯函数 `descriptionBlocks(description) ->
  Array<{kind:"paragraph", text} | {kind:"code", text, lang}>`：按 ``` 围栏切分；围栏内保留原文
  （含空行）；围栏外按行去空。node 单测覆盖：无围栏、单围栏、未闭合围栏（按代码块到末尾）、语言标记。
- `frontend/app/page.tsx` image 分支的 `descriptionNode` 改用它：paragraph → `<p>`，code → `<pre><code>`。
  `globals.css` 加 `.element-image-description pre` 样式（等宽、可横向滚动、不撑破卡片）。
- 不新增插槽、不新增插件 UI 包。

## 六、样板插件 `examples/extensions/circuit-diagram/`

- 包 `silicon_notebook_circuit_diagram`（`src/` 布局，`pip install -e` 或 `PYTHONPATH`），plugin id
  `examples.circuit_diagram`，contribution id `examples.circuit_diagram.enricher`，`trust="deployment"`，
  `requires=()`，`provides=()`，无 UI 包、无 HTTP 路由。
- Settings（pydantic，`extra="forbid"`）：`base_url="https://api.deepseek.com"`、`model="deepseek-flash"`、
  `api_key_env="DEEPSEEK_API_KEY"`（变量名，不是 key）、`timeout_seconds=30.0 (0<x≤120)`、
  `max_images_per_source=8 (1..64)`、`max_image_bytes=4194304`、`prompt_language="zh"`。
- 可用性探针（I/O-free）：`os.environ.get(api_key_env)` 非空 → AVAILABLE，否则
  `DISABLED/"api_key_missing"`。
- `enrich`：筛 `element_type == "image" and asset_id`，按顺序最多 `max_images_per_source` 张；每张前检查
  剩余预算 ≥ `timeout_seconds + 0.25s`，不足则停止（`PARTIAL`）；`assets.read(ref)` 为 None 则跳过；
  只接受 mime ∈ {image/jpeg, image/png, image/gif, image/webp}（DeepSeek 支持集）。
- 传输：stdlib `urllib.request` POST `{base_url}/chat/completions`，OpenAI 兼容体：
  `messages=[{"role":"user","content":[{"type":"text","text":<prompt>},{"type":"image_url","image_url":{"url":"data:<mime>;base64,<...>"}}]}]`，
  `response_format={"type":"json_object"}` 若失败则退回纯文本解析（去 ``` 围栏后 `json.loads`）。
  可注入 `_post(url, body, headers, timeout) -> bytes` 供测试；`Authorization: Bearer <key>` 只在发出时读环境变量。
- Prompt 要求模型只输出 JSON：`{"is_circuit": bool, "netlist": str, "function": str}`；网表用 SPICE 风格文本。
- 产出：`is_circuit` 为真 → 候选 `metadata={"is_circuit": true, "netlist": ..., "function": ..., "model": <model>}`，
  `description = "电路功能：{function}\n\n```spice\n{netlist}\n```"`；为假 → 不出候选（不记「已检查」）。
- 任何网络/解析错误只影响该张图（记数，不抛）；整体返回 `AVAILABLE`/`PARTIAL`。
- 红线：settings 值与 key 不进日志/异常/事件；不读 contextvars；不在 `configure()` 里做 I/O。
- 单测（`backend/tests/test_circuit_diagram_sample_plugin.py`）：bundle 形状与 manifest 一致；探针；
  JSON 解析（含围栏/非 JSON/缺字段）；传输 spy 收到 data URI 与 Bearer；预算不足提前停止；
  非图元素被忽略。e2e（`backend/tests/test_circuit_diagram_sample_plugin_e2e.py`，零网络，
  `socket.getaddrinfo` 守卫）：真 TOML + `create_app()` + `TestClient`，上传含 data-URI PNG 的 markdown，
  等解析完成，`GET` 元素断言 `metadata.extensions["examples.circuit_diagram.enricher"].metadata.netlist`、
  `metadata.description` 含网表、`text` 含功能描述；并断言 chunk 已包含该元素（`element_ids`）。
- `scripts/check_contracts.sh` 加 `--extra-root examples/extensions/circuit-diagram/src`；README 对
  （`README.md`/`README_zh.md`）+ `extensions.example.toml` + `pyproject.toml`（零仓库依赖，`pydantic>=2`）。

## 七、文档与守卫

- `docs/deployment-extensions-sop.md` / `_zh.md`：§3.5 表加 `SOURCE_ELEMENT_ENRICHER_POINT` 行、
  计数词 eight→nine / 八→九；§3.6 加一条红线（worker 线程、无 contextvars、`assets.read` 只在调用期内有效）；
  §3.5 后加一段该点的说明（对照 `ask.engine`/`indexing.pipeline` 段落）。
- `docs/product-and-api.md` / `_zh.md`：`### Deployment extensions` 之后新增
  `### Source element enrichment (\`source.element_enricher\`)` / `### 来源元素补全`，含限值表
  （四个 env + 三个结构常量），供反射契约测试。
- `docs/deployment-and-configuration.md` / `_zh.md`：四个 env 行（放在 MinerU 图片设置附近）；`.env.example` 同步。
- `architecture.md`：`ask.gap_consult` 段后加一段（挂点位置、同代落库、fail-open）。
- 守卫：`RUNTIME_ATTRIBUTES` 加 `element_enrichers`；`ports_protocol_method_count`、函数天花板若变动同 diff 改
  `scripts/architecture_boundary_baseline.json`；`repository_contract/transaction_phases.json` 若阶段序变动同步；
  `test_gap_consult_docs_contract.py` 的全集对账必须继续绿。

## 八、刻意不做

- 不复活旧契约的 caption 改写；不给插件写 `caption`。
- 不新增前端插槽/插件 UI 包；不做「已检查非电路图」标记；不做离线回填阶段。
- 不把视觉模型接进核心模型端口（`_WORKLOAD_LABELS` 不动）。
- 不做热更；不做按用户/按笔记本的开关（沿用 `/admin/extensions` 运行时启停）。
