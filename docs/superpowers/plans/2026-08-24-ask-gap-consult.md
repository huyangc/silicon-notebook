# 实现计划：`ask.gap_consult` 核心扩展点（PR-A）

状态：执行中（2026-08-24）。基线 origin/master `02c3b139`。本文是实现子代理的唯一规格来源。
方案页（用户已批准全部决策）：缺口外扩检索——非证据建议、逐步推理收尾恰好一次、fail-open。

## 主 agent 裁决（未决三项 + 风险处置，优先于下文任何相反表述）

1. **U1 轨迹步：出**。`step_type="gap_consult"`，前端标签「外扩」；零插件时一步不产生。
2. **U2 区块默认折叠**（`<details>` 无 `open`），与 `.answer-retrieval-scope` 一致。
3. **U3 建议只有 4 个字段**（title/url/summary/source_label），不带日期。
4. **R1 接受**：三处 `function_length_ceiling` 零余量棘轮按守卫实测值上调（`RepositoryFacade.__init__`、
   `RepositoryRuntime.__init__`、`AskService._run_reasoning_stage`），同一 diff 内改 baseline +
   `RUNTIME_ATTRIBUTES` 85→86 + AGENTS/CLAUDE 的「85 项」改 86；PR 正文明写论证；禁止折行骗计数。
5. **R7 守卫升级为必做**：T4 新增测试从 `app.extension_sdk` 反射全部 `*_POINT` 常量，断言 SOP 扩展点表逐个在场。
6. **T4 的文档-数值守卫必做**：新增 `backend/tests/test_gap_consult_docs_contract.py`，反射 `app.domain.gap_consult`
   的全部 `GAP_*` 常量值，断言逐个出现在两份 product-and-api 文档里。
7. **v2 预留**（用户方向）：后续把外扩做成 reflect 循环里模型可选动作；本 PR 的 `GapConsultHostPort.consult`
   已无收尾语义，类型在 domain 层——不实现动作，但不得引入任何「只能收尾调用」的耦合。

## 全局约定

- worktree `R=/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/plugin-x9a`；`frontend/node_modules`
  是真目录（不要 npm install）。`PY="${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python}"`；
  pytest `-p no:cacheprovider`。变异验证前先 `git commit --only` 打检查点；先 `grep -c` 确认变异改到了再跑。
- 任务串行：T1 → T2 → T3 → T4，每任务收尾 `bash scripts/check.sh` 可绿（T1/T2 至少 backend+contracts 两泳道绿）。

---

（以下为规划者产出全文，行号均已对着本 worktree 核实。）

## 0. 现状核实（要点，行号已对本 worktree 核实）

- 观察者宿主样板：`backend/app/extension_sdk/ask.py:24,31-53`；宿主 `backend/app/extensions/ask.py:72-275`，
  工具函数 `_connection_clear:393`、`_safe_clock:404`、`_valid_deadline:417`、`_deadline_open:421`、
  `_elapsed_ms:427`、`_emit:443`。`observe_application` 被钉 160——新宿主**不进**那张表。
- 休眠短路：`backend/app/domain/extensions.py:76-128`（`lane_is_dormant` 的防御性读法：probe 缺失/抛错/非字面
  `False` ⇒ 进宿主）。
- 注入链：`app/bootstrap.py:62-70` → `repositories/factory.py:20-54` → sqlite/postgres repository →
  `repository_facade.py:283-325` → `repository_runtime.py`（`_ProcessFoundation:153-169`、
  `_build_process_foundation:171-220`、host 挂载 `:936-939`、`ask_service():1992-2080`，`AskService(...)`
  构造在 `:2018`，`retrieval_contributors=` 在 `:2066`）。
- `run()` 收尾：`reasoning_retrieval.py` `run:3374`；reflect 结束 `:4930`；终态方向披露 `:4946-4972`
  （`_still_uncovered_directions` `:4950`；skip 步 detail `{"reason":"intent_coverage_incomplete",
  "pending":…, "directions": 简称列表}` `:4967-4971`）；`return ReasoningResult` `:5068-5081`。
  **`run` 被零余量钉 1708、`_new_run_state` 钉 201：本 PR 对该文件只允许加模块级常量。**
- `uncovered_intent_queries` 元素是「方向+整份问题契约」复合串（`:1540-1560`）——**绝不能外发原文**；
  终态 skip 步的 `directions` 已是注册表简称（≤60 字符、≤8 条）。
- floor：`backend/app/core/ask_retrieval_policy.py:106-140`，`ranked_final_floor` 依次 8/20/24/32/40。
- trace 惯例：`_TraceRecorder:1688-1721`；`TraceStep` 模型 `backend/app/models/ask.py:167-172`。
- 首轮合同由 `test_reasoning_retrieval.py` 的 intent/PPR/精查/空证据兜底/补种预算聚焦用例覆盖；本改动不触碰这些路径。
- `AskResponse:582-647`；`exclude_if` 空缺省先例 `Citation.images:110-112`、`result_sets:622-627`。
- 持久化：`sqlite/ask_state_store.py:733-761` `save_answer` 全量 `model_dump()`；`sanitize_answer_payload`
  （`core/internal_observability.py:46-55`）是黑名单——新字段自动持久化，**零 store 改动、零 migration**。
- 公开分享：`conversation_public_view.py:389-431` `public_turn` 构造性字面 dict（10 键）——天然不带新字段。
- 前端回答卡：`frontend/app/answer-panel.tsx:1179` `AnswerView`；渲染序 `:1348-1370`
  （AnswerMarkdown → KnowhowResultSets → ReasoningTracePanel）；调用点 `page.tsx:5686-5716`；
  只读视图 `frontend/app/dev/logs/activity/ActivityDetail.tsx:92`。
- URL 导入：`frontend/app/source-api.ts:33-41` `importUrlSources`；页面成功半在 `page.tsx:3506-3553`
  `submitUrlSources`（commitUrlSources → loadNotebookCollection → revalidateAskAvailability → reloadCheckup）；
  `use-source-library.ts:363-374` `commitUrlSources`、`:578-678` hasPending 轮询接手解析。
  后端只收 PDF（`remote_sources.probe_pdf:91-117`），拒绝原因在 `rejected[].reason`。
- 长任务按钮守卫 `long-task-button-guard.test.mjs:33-70` **只解析 page.tsx**——answer-panel 侧要另加 test block。
- `api_contract`：`serialization.ask_response` 不含 `exclude_if` 缺省字段 ⇒ 只要照惯例写，serialization 半零
  diff；openapi 半必变 ⇒ 跑 `scripts/generate_repository_contract_fixtures.py`。
- registry 对新 point 零改动（`_STABLE_METADATA_ID` 已允许 `.`；`availability:469-483` 先跑 manifest.requires
  的 capability probe）。插件用 `provides` 自供 probe ⇒ **core_decisions 零改动**。
- 三处零余量棘轮将变化：`RepositoryFacade.__init__` 458→460、`RepositoryRuntime.__init__` 118→119、
  `AskService._run_reasoning_stage` 530→实测。数字必须从守卫输出读。

## 1. 设计（总览）

新增文件：
`backend/app/domain/gap_consult.py`、`backend/app/extension_sdk/gap_consult.py`、
`backend/app/extensions/gap_consult.py`、`backend/tests/test_gap_consult_host.py`、
`backend/tests/test_gap_consult_ask_wiring.py`、`backend/tests/test_gap_consult_docs_contract.py`、
`frontend/app/answer-gap-suggestions.tsx`、`frontend/tests/component/answer-gap-suggestions.component.test.tsx`。

### domain（`app/domain/gap_consult.py`，零 `app.*`、零第三方）
常量：`GAP_CONSULT_MAX_GAP_PHRASES=2`、`GAP_CONSULT_MAX_SUGGESTIONS=5`、`GAP_CONSULT_QUESTION_MAX_CHARS=300`、
`GAP_CONSULT_PHRASE_MAX_CHARS=60`、`GAP_SUGGESTION_TITLE_MAX_CHARS=200`、`GAP_SUGGESTION_SUMMARY_MAX_CHARS=400`、
`GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS=40`、`GAP_SUGGESTION_URL_MAX_CHARS=2048`。
类型（全部 `@dataclass(frozen=True, slots=True)`）：
- `GapConsultQuery{question: str, gaps: tuple[str, ...], max_suggestions: int}` ——「外发了什么」的全集，一个对象即审计面。
- `GapSuggestion{title: str, url: str, summary: str = "", source_label: str = ""}` ——刻意只 4 字段。
- `GapConsultCallContext{query, cancellation, connection_probe, deadline_monotonic: float}` ——core-only 调用态，
  **不含 notebook/actor/source/证据**（隐私由字段集合结构性保证）。
- `GapConsultHostPort`（Protocol）：`has_contributions() -> bool`、
  `consult(call_context, *, event_sink=None) -> tuple[GapSuggestion, ...]` ——签名无收尾语义（v2 复用）。
- `gap_consult_host_is_dormant(host) -> bool` ——镜像 `lane_is_dormant` 的防御性读法（不复用它：无 invocation 维度）。

为何单开模块不塞进 `domain/extensions.py`：`app.models.ask` 要 import 数值上限做 `max_length` 单一真源，
不该把 wire 层拉进纯 host-port 协议模块的 import 图。

### SDK（`app/extension_sdk/gap_consult.py`，零第三方）
`ASK_GAP_CONSULT_POINT = "ask.gap_consult"`；
`GapConsultAvailabilityContext{contribution_id, deadline_monotonic}`（镜像 `AskCompletedAvailabilityContext`）；
`GapConsultExtensionContext{query, cancellation, max_suggestions, deadline_monotonic}`（**无任何 core 端口**）；
`GapConsultContributor`（Protocol）：`consult(context) -> ContributorResult[GapSuggestion]`（复用既有
`ContributorResult`）。re-export domain 的值类型与上限。`__init__.py` 照既有分组追加 import 与 `__all__`。
ContributionKind 用既有 `CONTRIBUTOR`（`registrar.add_contributor`）。

### 宿主（`app/extensions/gap_consult.py`）
`GapConsultHost(registry, *, event_sink=None, clock=time.monotonic)`：构造期镜像 `extensions/ask.py:75-114`
（要求 frozen、校验 kind 与 `callable(impl.consult)`、冻结 tuple）。`has_contributions()` 零 I/O 零时钟。
`consult` 顺序（逐条有用例）：
1. 零 contribution ⇒ `()`（严格 no-op，先于一切校验）；
2. `type(call_context) is not GapConsultCallContext` ⇒ `()`；
3. `_valid_query`（类型/非空 question/gaps 是 tuple 且 ≤2/max_suggestions 1..5）⇒ 否则 `()`（宿主不信调用方）；
4. `_valid_deadline`/`_connection_clear`（复制 `extensions/ask.py:393-425` 的语义到本模块，不跨 host import 私有）；
5. 逐 contribution：deadline 未过 → `registry.availability(cid, GapConsultAvailabilityContext(...))` → 再查连接
   探针 → 构造 `GapConsultExtensionContext` → **worker 线程执行** → 校验 `ContributorResult` 形状 → 净化 →
   累积 → `_emit`；
6. 满 `max_suggestions` 或 deadline 用尽即停。

**超时机制（已定）**：每次调用一个 `daemon=True` 新线程；target 只调插件 `consult`，`except BaseException` 写进
本次调用私有 cell（`abandoned` 置位后迟到写入无人读）；**不 copy_context()**——新线程空 Context 是承重的
（插件拿不到冻结 scope/run/扇出槽），docstring 写明勿加；主线程 50ms 分片 `join`，每片查
`cancellation.is_set()`——取消 ⇒ 放弃线程并**上抛 `AskCancelled`**；超时/异常/畸形 ⇒ `()` +
`_emit(status="unavailable", reason_code=gap_consult_timeout|gap_consult_failed|invalid_gap_consult_result)`。
不用线程池（挂死插件会耗尽 worker、把单插件故障放大成全站）；不用观察者的协作式 deadline（约束不了永不返回
的插件）。登记接受：真挂死时每个受影响请求泄漏一个 daemon 线程。

**净化（core 独占）**：title 非空；url `urlparse().scheme in {http,https}` 且 netloc 非空且 ≤2048、无控制字符；
逐字段 strip+截断（200/400/40/2048）；按 url 保序去重；截到 max_suggestions。不探测 URL（零网络零 DB）；
「是不是 PDF」由导入端点的 `probe_pdf` 回答。

`_emit` 键集合与 `extensions/ask.py:443-470` 同款：`{kind="ask_extension", point, plugin_id, contribution_id,
status, duration_ms, count}∪{reason_code}` ——无 query/url/title。

### 接线（AskService，不动 run()）
- `ExtensionRuntime` 加 `gap_consult: GapConsultHost`（`report_exporter` 之后）；`build_extension_runtime`
  构造；`default_extension_runtime` 不加 decision 不加 builtin（docstring 说明零内建消费者）。
- 注入链五文件各 +1 kwarg/字段/转发（见 §0 行号）；`AskService.__init__` 加 `gap_consult_host=None`。
- `Settings` 加 `ask_gap_consult_timeout_seconds: float = Field(4.0, gt=0, le=30,
  validation_alias="ASK_GAP_CONSULT_TIMEOUT_SECONDS")`（answer-latency 预算，非 post-terminal deadline，注释写明）。
- `AskService._consult_gap_sources(prepared, limits, trace, top_hits, chunks, elements, on_step)`（新私有方法，
  未被钉行数）：host None/休眠 ⇒ `()`；`gaps = _uncovered_directions_from_trace(trace)`（模块级纯函数：找终态
  skip 步 `detail["reason"] == INTENT_COVERAGE_INCOMPLETE_REASON` 的 `directions`，逐条 LOOSE_MARKER_RE 剥标记、
  折叠空白、[:60]、去重、[:2]）；`thin = limits is not None and len(top_hits)+len(chunks)+len(elements) <
  limits.ranked_final_floor`；两者皆否 ⇒ `()`；`query = GapConsultQuery(_egress_question(prepared), gaps[:2], 5)`
  （`_egress_question`：`resolved_question or research_question or question` → LOOSE_MARKER_RE 剥 → 折叠空白 →
  [:300]；LOOSE_MARKER_RE 在 `app/services/citation_markers.py:16-19`）；调 `host.consult(GapConsultCallContext(
  query, cancel_event, self.retrieval_connection_probe, monotonic()+settings.ask_gap_consult_timeout_seconds),
  event_sink=self.event_log.emit)`；`except AskCancelled: raise`；`except Exception: raw=()`（纵深防御）；
  追加 `TraceStep(step_type="gap_consult", summary=…, detail={reason, count, gaps})` 且 `duration_ms` 为本段墙钟，
  `trace.append(step); on_step(step)`；返回 `tuple(AskGapSuggestion(...))`。
- 触发点：`_run_reasoning_stage` 在 `:3007` `raise_if_cancelled` 之后、`:3014` `_assert_reasoning_runtime` 之前；
  core 填充在 `:3058-3061` 的 `if type(draft.response) is AskResponse:` 块内加
  `draft.response.gap_suggestions = list(gap_suggestions)`（镜像 model_errors 先例；不进 `ResponseDraftInput`）。
- `reasoning_retrieval.py` 只加模块级 `INTENT_COVERAGE_INCOMPLETE_REASON = "intent_coverage_incomplete"`
  （紧邻 `_INTENT_PENDING_DISCLOSE`，注释说明与 run() 收尾 skip 步同值、由 wiring 测试对账；**不改 run() 函数体**）。
- 「仅 reasoning Ask、报告/knowhow 不接」结构性成立：报告走 `retriever.run(...)`（`report_engine.py:1850`）、
  knowhow 走 `_construct_reasoning_retriever`，都不经 `_run_reasoning_stage`。

### wire 与前端
- `models/ask.py`：`AskGapSuggestion{title,url,summary,source_label}`（`max_length` 从 domain 常量取）；
  `AskResponse.gap_suggestions: List[AskGapSuggestion] = Field(default_factory=list, exclude_if=lambda v: not v)`
  （放 `model_errors` 之前；注释写明非证据/不进 anchors/citations/公开分享）。
- `workspace-model.ts` 加 `GapSuggestion` 类型与 `AskResponse.gap_suggestions?`。
- 新组件 `answer-gap-suggestions.tsx` `GapSuggestionsPanel({suggestions, onImport})`：空则 null；
  `<details class="answer-gap-consult">`（视觉复用 `.answer-retrieval-scope`，默认折叠）；summary
  `站外来源建议 · {n} 条` + title 提示；展开首行免责句「以下结果来自笔记本之外，没有参与本次回答，也不会被
  引用。导入后才会进入这个笔记本。」；每条：新窗口链接（`rel="noopener noreferrer"`）+ source_label 小徽标 +
  summary 小字 + 「导入」按钮；本地 busy/done/failed 状态（key=url）；长任务红线：点击立即 disabled +
  「导入中…」，成功固化「已导入」，失败把 message 持久渲染在该条下方（`.answer-gap-consult-error`，不用 toast）；
  `onImport` 缺省时整颗按钮不渲染（onSaveMemory 惯例）。
- `answer-panel.tsx`：`AnswerView` 加可选 `onImportGapSuggestion?: (url) => Promise<{ok, message}>`；渲染插在
  KnowhowResultSets 之后、reasoning_trace 之前。
- `page.tsx`：从 `submitUrlSources` 成功半抽 `applyImportedUrlSources(owner, created)`（行为逐字不变）；新增
  `importGapSuggestion(url)`：`sourceLibrary.captureOwner()` 缺 ⇒ 失败文案；`importUrlSources(nb,[url])` →
  created 非空 ⇒ `applyImportedUrlSources` + `{ok:true}`；否则 `{ok:false, message: rejected[0]?.reason ||
  "未能添加这个链接"}`；`catch` 用 `errors.ts` 翻译入口（**不读 error.message**）。传给 `<AnswerView>`。
- `globals.css`：`.answer-gap-consult` 系列，零颜色字面量；`.answer-gap-consult-error` 用 `--danger`。
- `reasoning-trace.ts`：`TRACE_STEP_LABELS` 加 `gap_consult: "外扩"`；`getTraceStepDetail` 加
  `${count} 条建议` 一支（count 非 number 回空串）。

## 2. 任务拆分（串行 T1→T2→T3→T4）

### T1（opus）— domain/SDK 合同 + GapConsultHost
文件：新建三个后端模块 + `extension_sdk/__init__.py`、`extensions/__init__.py`、`extensions/bootstrap.py`、
`core/config.py`；测试 `backend/tests/test_gap_consult_host.py`（脚手架照
`test_ask_post_completion_extensions.py:1-80` 的 `_Bundle`/`_ConnectionProbe`）。
用例（逐条）：`test_empty_topology_is_a_strict_no_op`（clock/probe 零调用、零事件）、
`test_dormant_probe_reads_defensively`、`test_query_is_the_whole_egress_surface`（三个 dataclass 的
`__dataclass_fields__` 集合逐一冻结）、`test_plugin_receives_no_core_port`、
`test_budget_caps_suggestions_and_phrases`、`test_malformed_items_are_dropped_not_fatal`、
`test_fields_are_truncated`、`test_plugin_exception_is_fail_open`、
`test_hung_plugin_is_abandoned_within_the_deadline`（真实墙钟 <1.0s 返回）、
`test_late_result_from_an_abandoned_plugin_is_inert`、`test_cancellation_propagates`（抛 AskCancelled）、
`test_connection_lease_blocks_the_call`、`test_unavailable_contribution_is_skipped_but_others_run`、
`test_events_are_content_free`（键集合 + 子串双断言）、`test_no_contextvars_leak_into_the_plugin_thread`。
变异：删 no-op 短路 → 红；`join()` 去掉超时参数 → 挂死用例红；连接检查挪到插件返回之后 → 红；净化挪出宿主 → 红。
验证：`pytest test_gap_consult_host.py test_extension_registry.py test_extension_discovery.py
test_admin_extensions_routes.py`；`check_architecture_boundaries.py`；`check_contracts.sh`；`check_backend.sh`。

### T2（opus）— 注入链 + 触发/查询构造 + AskResponse + 持久化 + 公开分享排除
文件：注入链五文件、`ask_service.py`、`reasoning_retrieval.py`（仅模块级常量）、`models/ask.py`、
baseline 三项（从守卫输出读数）、`test_repository_runtime_composition.py`（`RUNTIME_ATTRIBUTES` +"gap_consult"、
85→86）、`api_contract.json` 重生成（只许 openapi 半变）、`test_conversation_public_view.py` 加冻结键集合用例。
测试 `test_gap_consult_ask_wiring.py`（真跑 reasoning Ask，fixture 装配参照 golden 测试）：
`test_zero_plugin_answer_is_byte_identical`（None 与零 contribution host 双基线、JSON 无 gap_suggestions 键）、
`test_trigger_on_uncovered_directions`（gaps == skip 步 directions[:2]）、
`test_trigger_on_thin_evidence_below_the_tier_floor`（在 overview 与 standard 两档各验一次 floor 真按档取）、
`test_no_trigger_when_covered_and_above_floor`、`test_egress_payload_carries_nothing_else`（±断言已知来源名/
用户名/nb id 不在外发串里）、`test_marker_stripping`、`test_terminal_disclosure_reason_is_the_shared_constant`、
`test_timeout_leaves_the_answer_verbatim`（0.2s 预算、答案逐字、墙钟多出 <1s）、
`test_plugin_raises_leaves_the_answer_verbatim`、`test_malformed_result_leaves_the_answer_verbatim`、
`test_cancellation_during_consult_propagates`、`test_report_and_knowhow_paths_never_consult`（真跑报告深挖与
knowhow 补全、插件零调用）、`test_suggestions_survive_persistence_and_reopen`（含旧 payload 缺键回退）、
`test_suggestions_are_not_evidence`、`test_trace_step_detail_is_content_free`、
`test_an_injected_draft_stage_cannot_drop_the_suggestions`（注入裸 AskResponse 的 stage）。
变异：删触发短路 → 红；public_turn 加键 + 删冻结断言 → 红；core 填充挪进 draft stage → 红；consult 调用挪到
图激活之前 → thin 支红。
验证：上述 pytest 集 + `test_ask_modes_api.py` + `test_reasoning_retrieval.py` +
`test_public_conversation_api.py`；守卫；`generate_repository_contract_fixtures.py`；`bash scripts/check.sh`。

### T3（sonnet）— 前端披露区块 + 导入按钮
文件与做法见 §1「wire 与前端」。测试：`answer-gap-suggestions.component.test.tsx` 八条（空不渲染/折叠+免责句/
不进来源分布与引用/点击立即禁用换文案（resolve 前断言）/成功固化/失败文案持久 2.5s 后仍在/无 onImport 不出
按钮/持久 payload 重开渲染）；`answer-panel-readonly.component.test.tsx` 加只读支（区块在场、按钮为 0）；
`long-task-button-guard.test.mjs` 新增解析 `answer-gap-suggestions.tsx` 的 test block（`disabled={false}` 也红）；
`answer-panel-callbacks-guard.test.mjs` 的 `KNOWN_OPTIONAL_CALLBACKS` 加 `onImportGapSuggestion`。
变异：删 disabled → 守卫红；page.tsx 不传回调 → callbacks 守卫红；组件挪进 page.tsx 内联 → 只读用例红；
导入换成插件路由 → api/extension 守卫红（不红则补一条 import 面断言）。
验证：`npm run test`、`npm run build`、`npx tsc --noEmit`、`check_ui_vocabulary.py`。

### T4（sonnet）— 文档 + 守卫 + api_contract 校对
- `docs/product-and-api.md`/`_zh.md` 新节 `### Gap consultation (ask.gap_consult)` / `### 缺口外扩检索`：
  非证据；触发（仅 reasoning、全档、收尾恰好一次、两条件、报告/knowhow 结构性不接）；外发面（≤300 问题 +
  ≤2×≤60 短语，绝无 notebook 内容/证据/来源名/用户身份）；预算与 fail-open（含 daemon 线程泄漏的登记）；
  可用性（插件 provides probe，核心零开关）；导入（核心 URL 端点、只收 PDF、拒因逐字上屏）；
  数值上限唯一登记处（2/5/300/60/200/400/40/2048 + 4.0s 默认与 0<x≤30）；Ask 条目补 `gap_suggestions`。
- SOP 对（§3.5 扩展点表加一行、"four"→"five"；§3.6 红线加「独立线程、硬超时、勿依赖 ContextVar、勿假设被等到」）。
- README 对、AGENTS.md（Ask stages 条尾 + 85→86）、CLAUDE.md（同两处中文）、architecture.md 一句。
- 新守卫：`test_gap_consult_docs_contract.py`（反射 GAP_* 常量 → 两份 product-and-api 逐个在场）；
  SOP 扩展点表与 `app.extension_sdk` 的 `*_POINT` 反射对账（可并入同文件）。
- 校对 `api_contract.json` diff 只有 openapi 半。
变异：删数值段 → docs 守卫红；把上限挪进 SOP → 红；删 SOP 表新行 → point 反射守卫红。
验证：`test_architecture_documentation.py`、`check_ui_vocabulary.py`、`bash scripts/check.sh`。

## 3. 风险登记
R1 三处棘轮上调（接受，见裁决 4）；R2 daemon 线程泄漏（接受；熔断留待后续）；R3 导入只收 PDF（SOP 契约：
插件必须给 PDF 直链；arXiv 插件负责 `/pdf/`）；R4 检索降级路径会触发外扩（刻意，用例钉住）；R5 trace 键耦合
（常量 + 对账测试 + 删除变异）；R6 directions ≤8 的上游截断（docstring 登记）；R7 SOP 计数守卫（必做）。
