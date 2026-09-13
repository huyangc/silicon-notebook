# 实现计划：`source.element_enricher` 复活 + 电路图样板插件

状态：执行中（2026-09-13）。规格：`docs/superpowers/specs/2026-09-13-source-element-enricher-design.md`
（规格优先于本文任何相反表述）。分支 `claude/circuit-diagram-parser-plugin-807a60`，worktree
`R=/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/source-page-display-optimization-d94601`。

## 全局约定

- `frontend/node_modules` 是指向主 checkout 的软链，**绝不**在 worktree 跑 `npm install`。
- `PY=/opt/homebrew/Caskroom/miniconda/base/bin/python`；后端测试在 `$R/backend` 下
  `$PY -m pytest -p no:cacheprovider -n 4 <files>`；G1 全门 `bash scripts/check.sh`。
- 旧实现参考（已删除，`git show 3f8413a8f:<path>`）：`backend/app/extension_sdk/element_enrichment.py`、
  `backend/app/extensions/element_enrichment.py`、`backend/app/services/source_element_enrichment.py`、
  `backend/app/domain/element_enrichment.py`、`backend/tests/test_source_element_enricher.py`。可抄形状，
  但契约按规格 §二（新增 asset reader/description，删 caption）。
- 现行宿主范本：`backend/app/extensions/gap_consult.py`（worker 线程 + 硬截止 + 事件）。
- 任务串行 T1 → T2 → T3 → T4 → T5；每任务收尾后端相关测试绿；T5 后整条 `scripts/check.sh` 绿。
- 每任务完成后各跑一次 `spec-review` 与 `code-quality-review`，修完再推进。

## T1 — SDK 契约 + 域端口 + 宿主 + 运行时装配（opus）

改动：

1. `backend/app/extension_sdk/element_enrichment.py`（规格 §二全部类型 + 点常量）；
   `backend/app/extension_sdk/__init__.py` 导出（照 gap_consult 的导出块与 `__all__`）。
2. `backend/app/domain/element_enrichment.py`：`valid_element_enrichment_owner`、
   `persisted_element_enrichment_size`、常量 `EXTENSION_OWNER_ID_MAX_CHARS=128`、
   `EXTENSION_OWNER_VERSION_MAX_CHARS=64`、`ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH=12`。
3. `backend/app/domain/extensions.py`：规格 §三的域类型与 `ElementEnricherHostPort`。
4. `backend/app/extensions/element_enrichment.py::SourceElementEnricherHost`（规格 §三）：
   - `__init__(registry, *, event_sink=None, clock=time.monotonic)`；
   - `has_contributions()`；
   - `enrich_application(call_context, *, event_sink=None) -> tuple[ElementEnrichmentPatch, ...]`：
     构造 `ElementRef`/`ElementView`（ref→ordinal 私有映射）、`_AssetReader`（读 `asset_locations`，
     `max_asset_bytes` 上限，调用结束后关闭）、每 contributor 一条 daemon 线程（照抄 gap_consult
     `_execute` 的 join 分片与 deadline 语义；超时结束整点）、结果形状校验与整批准入、事件。
5. `backend/app/extensions/bootstrap.py`：`ExtensionRuntime.element_enrichers: SourceElementEnricherHost`，
   `build_extension_runtime` 里构造（`event_sink` 同其它宿主）。
6. `docs/deployment-extensions-sop.md`/`_zh.md` §3.5 表加行 + 计数词 eight→nine / 八→九（否则
   `test_gap_consult_docs_contract.py` 全集对账红）。
7. 测试 `backend/tests/test_source_element_enricher_host.py`：空拓扑零成本；kind/实现校验抛
   `ExtensionRegistryError`；正常一批准入并回传 patch；ref 伪造/重复/越界拒绝整批；metadata 键/深度/
   非有限浮点/字节上限；description 控制字符/长度；`UNAVAILABLE` 整批丢弃、`PARTIAL` 准入；超时
   abandoned + 后续 contributor 不启动；插件抛异常 fail-open 且下一个继续；reader 超上限 None、调用后 None、
   未知 ref None；调用方持租约整点跳过；事件字段稳定码。

验收：上述测试 + `test_gap_consult_docs_contract.py` + `test_extension_registry*.py` 绿；
`$PY scripts/check_architecture_boundaries.py` 绿（`ports_protocol_method_count` 变动则同 diff 改基线并说明）。

## T2 — 服务适配器 + 设置 + 全链接线 + 摄取挂点（opus）

1. `backend/app/core/config.py`：四个设置（规格 §四）。
2. `backend/app/services/source_element_enrichment.py::enrich_source_elements(elements, *, host,
   resolve_assets, notebook_id, connection_probe, settings 四值, timeout, event_sink, clock)`：短路、
   envelope、资产定位、调宿主、合成（规格 §四合成规则，整批 fail-open）。
3. 接线：`backend/app/bootstrap.py::application_repository_hosts` 加 `element_enricher_host`；
   `backend/app/repositories/factory.py`、`postgres/repository.py`、`services/sqlite_repository.py`、
   `services/repository_facade.py`、`services/repository_runtime.py`（foundation 字段 + 属性
   `element_enrichers` + `SourceIngestionService(...)` 传参）；facade 模块级
   `_resolve_element_assets(repo, notebook_id, asset_ids)`（`get_notebook_asset` + `AssetService.path_for`，
   过滤 notebook 不匹配/文件不存在）。
4. `backend/app/services/source_ingestion.py`：构造参数 `element_enrichers=None`、
   `resolve_element_assets=None`；`_process_source_scoped` 在去重后、`with self.write()` 前**一条语句**
   `elements = self._enrich_parsed_elements(source, elements)`；新方法内做 stage 记录与调用。
5. 守卫：`backend/tests/test_repository_runtime_composition.py::RUNTIME_ATTRIBUTES` 加
   `element_enrichers`；`scripts/architecture_boundary_baseline.json` 只在必要时同 diff 改；
   `backend/tests/repository_contract/transaction_phases.json` 若被合同测试点名则同步。
6. 测试：`backend/tests/test_source_element_enrichment_service.py`（合成规则：extensions 子树、
   description 追加与 text 拼接、重复 contribution 拒绝、字节上限、无宿主/空元素短路、宿主抛错 fail-open）；
   `test_source_ingestion_service.py` 加用例：假宿主返回 patch → 落库元素带 `extensions` 与 description，
   且 chunk 含该 image 元素；宿主为 None 行为逐字不变。

验收：后端 lane 相关文件绿；`check_architecture_boundaries.py` 绿；
`$PY -m pytest backend/tests/test_repository_runtime_composition.py` 绿。

## T3 — 前端描述围栏代码渲染（impl-task / sonnet）

1. `frontend/app/source-element-display.ts`：`descriptionBlocks()`（规格 §五）。
2. `frontend/app/page.tsx` image 分支 `descriptionNode` 改用它；`globals.css` 加
   `.element-image-description pre` 与 `code` 样式（等宽、`overflow-x:auto`、`white-space:pre`）。
3. `frontend/tests/unit/source-element-display.test.mjs` 加用例。

验收：`cd frontend && npm run test:node` 与 `npm run lint` 绿。

## T4 — 样板插件 + 单测 + e2e + 契约泳道（opus）

按规格 §六实现 `examples/extensions/circuit-diagram/`（`README.md`/`README_zh.md`/
`extensions.example.toml`/`pyproject.toml`/`src/silicon_notebook_circuit_diagram/{__init__,bundle,
settings,client,enricher}.py`）；`backend/tests/test_circuit_diagram_sample_plugin.py`、
`backend/tests/test_circuit_diagram_sample_plugin_e2e.py`（照 `test_arxiv_sample_plugin_e2e.py` 的隔离
fixture、`_configure`、`_clear_caches`；上传 markdown 内嵌 data-URI PNG；假 `_post` 返回固定 JSON）；
`scripts/check_contracts.sh` 加 `--extra-root`。

验收：两测试文件绿；`bash scripts/check_contracts.sh` 绿（含词汇守卫）。

## T5 — 文档 + 反射契约测试（impl-task / sonnet）

规格 §七：`docs/product-and-api*.md` 新节与限值表；`docs/deployment-and-configuration*.md` 与
`.env.example` 四个 env；SOP §3.6 红线 + 该点说明段；`architecture.md` 段落；
`backend/tests/test_element_enricher_docs_contract.py`（反射 `app.domain.element_enrichment` 常量与
`Settings` 四字段的默认值，必须在 EN/ZH 表行同现）。

验收：`test_architecture_documentation.py`、新契约测试绿；整条 `bash scripts/check.sh` 绿。
