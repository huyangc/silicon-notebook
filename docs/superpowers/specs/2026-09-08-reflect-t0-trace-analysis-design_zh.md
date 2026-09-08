# reflect v2 开闸前测试摸排 · T0：离线轨迹统计与影子轨迹（设计规格 v1）

> **状态**：v1，2026-09-08 用户拍板三点后进入实施：本地 PG 测试库灌真实提问；报告轨迹分开聚合；测试环境开闸跑一批 v2 影子轨迹。
> 上游：`2026-09-07-retrieval-reflect-final-design_zh.md` §9.1（离线轨迹统计）与 §9.2（真实决策 A/B）。本文只覆盖 T0；A/B 题集与判分另立规格，但 T0 的产出要能直接回答 A/B 题集怎么配。
> **基线**：`origin/master = 8ffd147e8`（PR #698 合入后）。

## 0. 结论先行

- 两个脚本、读写分离：`scripts/export_reasoning_traces.py`（只读导出 → JSONL，每行一个 run 的闭集投影）与
  `scripts/analyze_reasoning_trace.py`（只吃 JSONL，零 DB / 零模型 / 零网络，输出聚合表）。
- 一个 rig：`scripts/reflect_shadow_rig.py`，在**本地 PG 测试库**上灌真实语料与真实提问，同一批问题在 legacy 与 v2 两种策略下各跑一遍，
  Ask 与 Report 都跑；Ask 的轨迹从库里导出，Report 的逐节轨迹**在进程内捕获**（它不落库，见 §2.3）。
- 隐私口径复用 `app.domain.retrieval_experience.project_run_step`：不输出问题原文、来源标题、证据文本、模型 reason、trace summary、任何 id。
- `unknown` 是一等值：旧记录缺字段就是 unknown，不当 0 / false；每个指标带「可观察样本数 / 缺失数」两个分母；一个格子样本数 < 5 只报计数、不出分位数。

## 1. 现状事实（2026-09-08 实测）

| 事实 | 出处 |
| --- | --- |
| Ask 轨迹落库两处：`ask_jobs`（mode/question/status/trace_json/answer_id/created_at）+ `ask_trace_steps`（job_id/seq/step_json，append-only，是权威）；会话 `answers.payload`（AskResponse JSON，含 `reasoning_trace`/`mode`/`retrieval_effort`/`intent`/`citations`） | `backend/app/repositories/postgres/ask_state_store.py` |
| 本机主库（PG）已有 58 个 ask job（reasoning 44 done / 2 failed / 1 cancelled，chunk 11 done）、570 条轨迹步、13 份报告、12 个笔记本 111 个来源 | psql 实测 |
| Report 的 `sections_json` 只存 attempted/evidence_level/top_relevance/claims 等结果字段，**逐节轨迹不落库**；`_deep_dive(on_step)` 的步只驱动进度 | `report_engine.py:2556-2596`、`reports` 表 |
| 已有闭集投影：`project_trace_step`（理解块）/ `project_run_step`（全局经验库，丢 summary，留 step_type、一个计数、duration、有界 result/anchor id） | `backend/app/domain/retrieval_experience.py:129,218` |
| 探针题集：24 题中英双语 + gold section_path，目标笔记本 `nb-1cf2533914374d198d45e91188607709`（arXiv 2502.05171，递归深度 LM）；PDF 不在磁盘，解析后的元素在库里 | `~/.claude/projects/.../memory/probe-floor-2026-09-07/questions.py` |
| `source_elements` 的列是 `id, source_id, element_type, location_label, text, metadata, created_at, ordinal`——**没有 `section_path` 列**（`section_path` 在 `chunks` 表与 `metadata` 里）。标题层级只在 `metadata.heading_level`；`element_type` 实测取值：paragraph / heading / table / list_item / code_block / page_text / formula / knowhow_cell / image / image_caption | 主库 `\d source_elements` 与 `GROUP BY element_type` 实测（2026-09-08） |
| 多篇论文语料的 MinerU markdown 在磁盘：`.local/storage/notebooks/nb-b37185f4ae/*_mineru.md`（DeepSeek-V2、Qwen-VL、ForesightKV、Hymba …） | ls 实测 |
| 真实模型已配置（主 checkout `.env` 的 `MODEL_SERVICES_CONFIG` + `SYSTEM_*_API_KEY`）；本机 PG 可建 `silicon_notebook_*_test` 一次性库（需 `pg_trgm`/`btree_gin`，URL 带用户名） | `.env`、memory `local-pg-test-db` |
| 总闸默认关：线上只有 legacy 轨迹；v2 轨迹只能靠开闸的影子 run 产生 | PR #698 |

## 2. 交付

### 2.1 `scripts/export_reasoning_traces.py`（只读导出）

- 输入：`--database-url`（**必填**，不读 `.env` 的 `DATABASE_URL`，不隐式连库）、`--since/--until`、`--notebook`（可多值，导出时哈希）、`--out <jsonl>`。
- 走 repository 只读端口读 `ask_jobs` + `ask_trace_steps`（权威）与 `answers.payload`（补 `retrieval_effort`/`intent`/`citations`/`mode`）；`trace_json` 只作 `ask_trace_steps` 缺失时的回退并打 `trace_source=legacy_column` 标。
- 每行一个 run 的**闭集投影**（§3），投影函数放 `backend/app/domain/reasoning_trace_stats.py`（纯函数、零 I/O），导出脚本与分析脚本共用，SQLite/PG 同一份（`step_json` 一边是 TEXT 一边是 jsonb，与 `project_trace_step` 同一条理由）。
- 报告：`--reports` 时从 `reports.sections_json` 导出**结果级**字段（attempted 数、evidence_level、grounded、failed），标 `consumer=report_section`；逐节轨迹不在库里，由 rig 侧的 JSONL 直接产出（§2.3），两者按 `report_id+section_index` 合并。

### 2.2 `scripts/analyze_reasoning_trace.py`（离线聚合）

- 输入：一个或多个 JSONL；`--group-by` 默认 `consumer,mode,effort,kg_in_scope,policy_version`；`--min-samples 5`；`--out-md`、`--out-json`。
- 输出：markdown 聚合表 + JSON 明细（只含闭集维度与数值）。指标见 §4。
- 零依赖：不 import `app.services`，只 import `app.domain.reasoning_trace_stats`（或纯标准库）。

### 2.3 `scripts/reflect_shadow_rig.py`（影子轨迹 rig）

子命令：

- `seed`：建库（`silicon_notebook_t0_test`，装扩展）→ 起临时后端（worktree 内 `uvicorn`，`.env` 从主 checkout 复制并覆盖 `DATABASE_URL`/`SILICON_NOTEBOOK_STORAGE_DIR`/端口）→ 注册 fixture 用户 → 建两个笔记本：
  - **A 单篇**：从主库 `source_elements` 重建 arXiv 2502.05171 的 markdown（按 `ordinal` 拼，`element_type == "heading"` 的元素按 `metadata.heading_level` 输出 markdown 标题，缺该键退到 `##`）后上传；
  - **B 多篇**：直接上传 `.local/storage/notebooks/nb-b37185f4ae/*_mineru.md`（取 4–6 篇）。
  → 触发解析与 `kg/build`，等 `index-status` 就绪。A/B 各留一个**无图副本**（不建图），得到有图/无图 × 单篇/多篇四个语料格。
- `ask`：读题集（§5），对每题 × 每语料格 × `{legacy, v2}` × `{standard, deep}` 走 `/ask`（reasoning，高级界面路径：先 `/ask/intent` 拿契约再确认提交，让方面账有真实 `mandatory_topics`）。策略切换靠重启后端时的 `REASONING_REFLECT_V2_ENABLED`；每个 run 的 `client_request_id` 编码 `题号/语料格/策略/档位`，导出时据此打标（不写进问题文本）。
- `report`：对 §5 的 4 道综述题 × `{legacy, v2}` × 语料格 B（有图），**进程内**调用 `ReportEngine`（复用 `replay_retrieval.py --report-run` 的构造方式），`on_step` 直接写 JSONL（经 §3 投影），并把 `sections_json` 结果级字段并入。
- `export`：调 §2.1 导出 Ask 轨迹，与 `report` 的 JSONL 合并成一份数据集。
- 全程零接触主库写路径：主库只做**只读**的元素重建；所有写入都在测试库。结束 `--teardown` 删库。

#### `search`：不建测试库的首跑路径

上面 `seed → ask/report → export` 那条要先复制一份语料、建图、起后端，成本以小时计。
T0 首跑改走 `search`：**不建测试库、不建图、不合成答案**，直接对**主库（只读）**里两个
既有笔记本跑检索过程（plan + reflect 循环），轨迹在进程内投影成 JSONL。

- **语料格**：`A_nokg`（`--source-notebook-a`，主库里那个本来就没有 `knowledge_objects`
  的单篇笔记本）与 `B_kg`（`--source-notebook-b`，有图多篇）。另外两格在主库上不存在，
  不在枚举里。notebook id 刻意不落仓库。题集按 §5 的 `corpus` 字段分流，`--limit 5`
  各取前 5 题 ⇒ `2 格 × 5 题 × {legacy, v2} × {standard, deep}` = 40 个 run。
- **策略切换不重启**：`ReasoningRetriever.from_repository(repo, settings)` 用的是**传入
  的**那份 Settings（`reflect_v2_active()` 只读 `self.settings` 与 `allow_reflect_v2`，
  两者都不经 repo），所以为两个策略各构造一份 `Settings()`（按
  `REASONING_REFLECT_V2_ENABLED` 走构造器，不是 `model_copy` 绕过校验），在同一个进程、
  同一个 repo 上交替跑。每个 run 开跑前核对 `retriever.reflect_v2_active()` 与该 run
  声明的策略；每个策略的**第一个** run 之后再核对一次投影出来的 `policy_version`
  （v2 的判据是这次 run 产出了 `termination` 事实）——对不上直接停，不跑完整批再发现。
- **意图契约**：走高级界面那条路的进程内等价物（`plan_query_intent` +
  `finalize_query_intent(answers=[])`，即「问题清晰⇒自动确认」），每题算一次并缓存进
  `--out-dir/intents.jsonl`。**那个文件不进数据集、不进仓库**（契约含问题原文）。
  由它派生 `research_question` / `intent_queries` / `intent_detail` 三个入参，与
  `AskService._prepare_reasoning_ask` 逐字同式；带契约的 run 因此**不调 plan 的 LLM**。
  `--no-intent` 跳过，代价是 v2 只剩整题一个方面。
- **投影**：`app.domain.reasoning_trace_stats.project_search_run`，复用 `project_run`
  的全部内部逻辑，只补三件调用方手上有确凿事实的东西：结束事实读 `result.termination`
  这个 DTO（v2-only；`None` ⇒ legacy 走既有反推，`termination_inferred=True`）并据它补
  `aspects_total` / `aspects_pending` / `unrecovered_channels_count`；`kg_in_scope` 取
  `kg_in_scope_for` 的直接判定；**没跑合成的那几列**（`anchors` / `included_*` /
  `citation_contribution`）一律 unknown。`consumer=ask_single`、`status=done`、
  `trace_source=in_process`（`TRACE_SOURCES` 的既有成员）由 rig 声明但仍过闭集。
- **只读断言**：跑前跑后各点一次 `ask_jobs` / `answers` / `conversations` /
  `knowledge_objects` / `retrieval_experiences` 的行数，任何一张对不上就标红报错。
  `ReasoningRetriever.run` 唯一的写路径是收尾那次 `note_adopted` UPDATE（仅注入开启时
  可达），rig 另外把注入三闸强制关。`retrieval_run` 的 `event_log` 不接。
- **输出**：`--out-dir/search-<policy>.jsonl`（每行一个 run，闭集投影）+
  `search-runs.log`（run 序号/题号/格/策略/档位/耗时/reflect 轮数/终态，无原文）。
  两份 JSONL 直接进 §2.2 的聚合器，与 `ask` 那边按 `question_key + corpus_cell +
  effort` 成对（§4.2），聚合脚本零改动。

### 2.4 守卫与测试

- `backend/tests/test_reasoning_trace_stats.py`：合成 fixture 覆盖 legacy 缺字段（无 `result_ids`、无 termination 步）、空数组、`result_ids_truncated`、seed/action 分离（`phase` 键与首轮位置回退）、同一证据被多步命中的归因口径（§4.4）、v2 `retrieval_termination` 步与 legacy 反推同一 run 不重复计数、`unknown` 分母、`min_samples` 门、SQLite TEXT / PG jsonb 两种 `step_json` 类型同一投影。
- `scripts/analyze_reasoning_trace.py` 的 CLI 用例：对 fixture JSONL 出表，断言不含任何自由文本键。
- 隐私守卫：投影输出键集是闭集常量 `RUN_PROJECTION_KEYS`，用例断言 `set(row) ⊆ RUN_PROJECTION_KEYS` 且不含 `question`/`summary`/`reason`/`title`/`*_id`（哈希后的 `notebook_bucket` 除外）。
- rig 本身不进标准门（需要网络与模型）；只有 `--dry-run` 的参数/编码用例进门。

### 2.5 文档

- `scripts/README.md`（若存在；否则 `docs/operations*.md` 的脚本段）：三个脚本的用法与隐私口径。
- `docs/development*.md`：只在「评测夹具不进标准门」这条真的新增时补一句。
- 台账：`fangan_todo.md`「reflect v2 开闸前待办」(a) 改为「T0 已交付；A/B 待立规格」，并把 T0 首份基线报告的**摘要数字**（不是明细）记入 `fangan_done.md`。

## 3. 每 run 一行的投影（`RUN_PROJECTION_KEYS`）

维度（闭集）：

| 键 | 取值 | 来源 |
| --- | --- | --- |
| `consumer` | `ask_single` / `ask_sectioned` / `report_section` | Ask：synthesis 步 detail 有 `section_total` 即 sectioned；Report：rig 标 |
| `mode` | `chunk` / `reasoning` / `auto→chunk` / `auto→reasoning` | `ask_jobs.mode` + payload `mode` |
| `effort` | 五档 | payload `retrieval_effort`；缺则 `unknown` |
| `kg_in_scope` | `true` / `false` / `unknown` | 只认正面证据（2026-09-08 订正）：(a) 轨迹里有无图披露步（`kg_unavailable` 类 reason）→ `false`；(b) 否则 `mode` 是 reasoning 且轨迹里出现过图形状的步（`expand`/`ppr`/`follow_chain`/`expand_community`，或 `retrieve` 步 detail 的 `new`/`found` > 0 且不带无图原文半的 `chunks_found` 印记）→ `true`；(c) 其余 → `unknown`。`kg_required=True` 仍叠加进 (a) 的判据，但 `kg_required=False`（默认值，早退/chunk 轨迹总是带着它）不再单独判成 `true`——它曾经把从没碰过图的 run 误报成有图 |
| `policy_version` | `legacy` / `v2` | trace 有 `reason=retrieval_termination` 的 skip 步或 `termination_reason` 键 → v2；否则 legacy |
| `has_intent_contract` | bool | payload `intent` 非空 |
| `corpus_cell` | `A_kg` / `A_nokg` / `B_kg` / `B_nokg` / `unknown` | rig 编码；线上导出恒 unknown |
| `notebook_bucket` | 来源数分桶 `1` / `2-5` / `6-20` / `21+` / `unknown` | 导出时按 `sources` 计数，不记 id |
| `status` | `done` / `failed` / `cancelled` | `ask_jobs.status` |

指标（每个都允许 `null` = unknown）：

- `reflect_turns`、`action_seq`（step_type 闭集序列，seed 前缀标 `seed:`）、`actions_by_type`、`seed_actions_by_type`
- `skip_reasons`（reason 码 → 次数）、`fallback_count`/`fallback_reasons`（2026-09-08 订正：只数 reflect 步 detail 里带 `fallback_reason` 键的**模型兜底**——反思调用失败/响应畸形后的 fail-open 收尾；`search_elements` 那个同名的 `fallback` step_type 是路由决策，不是模型兜底，只在 `actions_by_type` 里以 `fallback` 计，不进这两个键）、`stale_breaker`（bool）、`stale_max`
- `termination_reason`（v2 读 termination 步；legacy 反推：有 `stale_circuit_breaker` → `stale`；末尾 reflect 带 `fallback_reason` → `model_degraded`（须先判，fail-open 兜底会把 `next_action` 写成 `answer`，与「模型自己说够了」同形）；否则末尾 reflect 的 `next_action=answer`/`sufficient` → `model_end`；reflect 数 == 档位上限且无 answer → `step_budget`；其余 unknown）
- `aspects_total` / `aspects_pending` / `aspects_undelivered` / `unrecovered_channels_count`（v2 only）
- `candidates_kg` / `candidates_chunks` / `candidates_elements`（answer 步）、`included_kg` / `included_chunks` / `included_elements`（synthesis 步）、`anchors`（synthesis 步）
- `citation_contribution`（§4.4）：`{action_type: {steps, steps_with_ids, cited_hits, unknown_steps}}`
- `durations_ms`：按 step_type 的步间耗时和（只报为「步间耗时」）、`total_ms`
- `trace_steps`、`trace_truncated`（bool，任一步 `result_ids_truncated` 或 `anchor_evidence_ids_truncated`）

## 4. 指标与口径

### 4.1 分母

每个聚合格子输出 `n_runs`；每个指标另带 `n_observed`（该指标非 unknown 的 run 数）。分位数（P50/P95）只在 `n_observed ≥ min_samples` 时输出。

### 4.2 legacy 与 v2 的可比性

同一格子里 legacy 与 v2 分开成行；跨策略对比只在 `corpus_cell`、`effort`、题号（rig 编码的 `question_key`，是编号不是原文）三者相同时成对。线上导出的 legacy 轨迹没有 `question_key`，只进「基线」表不进「对照」表。

### 4.3 耗时

`duration_ms` 是相邻记录的墙钟差，含排队与模型往返，报为 `inter_step_ms`；不宣称单次 provider 或工具执行时长。

### 4.4 每动作引用贡献

`cited_hits(action)` = 该动作各步 `result_ids` 与 synthesis 步 `anchor_evidence_ids` 的交集大小之和。规则：

- 某步无 `result_ids` 或带 `result_ids_truncated`，或 synthesis 步无 `anchor_evidence_ids` 或带截断标 → 该步计入 `unknown_steps`，**不**用整轮 `anchors` 顶替；
- 同一证据 id 被多步命中：按**首次**命中的动作归因（首轮 seed 优先于循环动作），并另报 `shared_hits` 计数，不均摊；
- seed 与 action 分开：`seed:ppr` 与 `ppr` 是两行。

### 4.5 结束原因（legacy 反推的置信度）

反推结果带 `termination_inferred=true`；v2 读到 termination 步则 `false`。聚合表分两列。

`model_degraded`（反思调用失败后的 fail-open 兜底）与 `model_end`（模型自己说够了）在
`next_action`/`sufficient` 两个字段上完全同形——`_reflect_fallback`
（`reasoning_retrieval.py`）兜底时同样把 `next_action` 写成 `answer`。legacy 轨迹里唯一
能把两者分开的信号是末尾 reflect 步 detail 里带不带 `fallback_reason` 键，所以反推**必须**
先判这个键，再退到 `next_action=answer`/`sufficient`，否则会把兜底收尾误判成模型主动判定。

## 5. 题集（rig 用；A/B 题集另立）

- **A 单篇**：memory 里的 24 题（中英各一），保留 gold section_path 供后续 A/B 用；T0 只用题号。
- **B 多篇**：新写 10 题，覆盖 §9.2 点名的形态：来源目录与类型清单（2）、两个对象比较（2）、参数默认值与条件（2）、同标题/同内容（1）、首轮空手的措辞（1，英文术语打中文语料）、范围缩小（1，勾选 2 篇）、超预算的综述（1）。
- **Report**：4 道综述题（B 语料），深度取 standard 与 deep 各 2。
- 题集文件放 `backend/app/eval/reflect_t0/questions.json`（无敏感数据；语料是公开论文），随仓库。

## 6. 隐私与安全

- 投影键闭集；`notebook_bucket`/`question_key` 都是编号或桶；rig 的 `client_request_id` 编码只进导出侧的打标，不进问题文本、不进模型 prompt。
- 导出脚本对主库只读，且只在 `seed` 重建元素时读主库；影子 run 全部写测试库。
- 测试库用后删除；基线报告只含聚合数字。

## 7. 验收

1. `bash scripts/check.sh` 全绿；新增用例含 §2.4 全部形态；隐私守卫变异（往投影里加一个 `question` 键）必红。
2. 对主库导出 legacy 基线：58 个 job 全部产出投影行，failed/cancelled 行 `status` 正确、指标为 unknown 而不是 0。
3. rig 跑通四个语料格 × 34 题 × 2 策略 × 2 档位 的 Ask（约 500 run；允许按 `--limit` 缩到每格 10 题先验证）与 4 × 2 的 Report；导出 + 分析产出基线表与对照表。
4. 首份报告能回答三个问题：哪类动作最常空手；stale 熔断与 fallback 集中在哪个档位与图状态；legacy 下「查到了但没被引用」的动作占比。这三条决定 A/B 题集的分层。

## 8. 刻意不做

- 不做 A/B 判分与 gold 对照（§9.2 另立）；不引入判分模型。
- 不改任何生产代码路径；不给 Report 加逐节轨迹落库（rig 进程内捕获已够，落库是产品决定）。
- 不把 rig 进标准门；不在 CI 里跑真实模型。
