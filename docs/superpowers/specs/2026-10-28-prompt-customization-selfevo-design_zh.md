# Prompt 按笔记本定制与 self-evo 设计规格（草案 v2）

- 状态：**草案 v3。已过 opus 两轮评审：首轮的 6 条 P1、9 条 P2、8 条 P3，复审的 1 条 P1 和 11 条改进项，
  均已处理，见文末附录。待用户拍板**
  （用户 2026-10-19 裁决 16「长期方向四项都启动设计：先写规格、评审后给用户看」）。
- **用户裁决（2026-10-28）：D1=(c)，P2 评测台先行，再据 7.3 的结论决定 P1；D17=(a)；D18=(a)。
  其余 D2–D16、D19–D21 在 P1 / P3 立项时再定（§10.2）。**
- 对应待办：`fangan_todo.md`「Prompt 三层化后的 per-notebook 定制与 self-evo」。
- 地基：PR #637（Prompt 三层化，`backend/app/services/prompt_layers.py`）。
- 本文只做设计，不含实现代码。所有「现状」断言都给出文件:行号或函数名；存储以 PostgreSQL 为规范定义，
  SQLite 作镜像。

---

## 0. 结论先行

1. **定制的单位不是「L1 片段」，而是片段里的「示例槽位」。** 逐个体检后发现，9 个 L1 片段中有几个
   夹带了实际上属于 L0 的条款（覆盖披露、数值不得跨条件转移、清单不得漏项）。如果允许整段覆盖，
   就等于允许改写这些条款。因此先把片段重切为「锁定的指令核心 + 槽位」：槽位连同连接词一起包住，
   核心在默认、定制、通用三态下逐字节同源；默认态的渲染与今天逐字节相同。
2. **按笔记本定制（子题 a）可以独立交付。** owner 按字段填写领域示例，只追加版本、可以恢复、对成员可见；
   不改指令、不改格式、不碰工具协议，也不改变任何权限。全局问答一律用默认。
3. **self-evo（子题 b）不建议现在实现。** 真正的价值在 P2 评测台，它同时能补上三条现有的人工 A/B 待办。
   A 类自优化相对 P2 新增的只是一个候选生成器，而且在公开语料上调出的默认值会全局改变生产（电路、EDA）
   行为，预期收益低。B 类只给 owner **提**示例建议，必须逐条人工采纳才会注入。
4. **推荐顺序：P2 先行**，在评测台上做示例敏感度实验（7.3）。它比较四组示例：默认、通用、贴合本库语料的定制、
   他领域。决策看两项：盲评的成对胜负，以及规划在环模式下的金标命中；合同判据只当护栏。实验结果决定是否做 P1。
5. **与检索经验库不重复**：经验库只告诉规划和 reflect「哪个检索通道对哪类问题有效」，从构造上进不了
   答案合成，也不改任何指令文本（第 4 节）。
6. 决策点分两部分（第 10 节）：**现在拍** 3 条（D1、D17、D18，只涉及 P2 范围）；另外 18 条等 P1 或 P3 立项时再定。

---

## 1. 现状

### 1.1 三层与唯一接缝

- 三层定义见 `prompt_layers.py:14-47`。L0 是系统骨架，不可被任何按库或按笔记本的定制覆盖（`:16-24`）；
  L1 是 9 个具名片段（`:126-275`）；L2 是数据注入块的元数据登记（`:318-419`）。
- 只有 L0、不得挂任何 L1 的 prompt：`reflect_prompt` 及其 schema hint、`report_synthesis_prompt`、
  `report_sufficiency_prompt`、`evidence_refine_prompt`、`followup_rewrite_prompt`，以及 Agentic Memory 全组
  （`prompt_layers.py:49-62`）。
- 唯一覆盖接缝是 `fragment_text(fragment_id)`（`prompt_layers.py:278-287`）。它今天直接返回注册表默认值，
  不知道当前是哪个笔记本。模块只依赖标准库（`:3-12`）；将来的解析器放在服务层，存储端口不得 import
  本模块（`:72-89`）。
- 调用点：`prompts.py:436`（answer.style_language）、`:448-450`（answer 规则 8/9/10）、`:1718`（expand_query）、
  `:1871`（intent）、`:2029`（report_section）、`:2171`、`:2196`（storm 大纲）。另有**第二个消费者**
  `document_guide.py:79`：`guide_style_instruction` 把 `answer.style_language` 嵌进文档导读 prompt 的句子中间。
- 守卫在 `backend/tests/test_prompt_layers.py`：闭集 id、形状契约（起始序号与结尾换行，`:103-142`）、接线
  （`:145-180`）、L2 双向对账，以及「`prompts.py` 只许 import `fragment_text`」（`:304-330`）。
  **缺口**：这条 AST 守卫只扫 `prompts.py`，没有覆盖 `document_guide.py`。

### 1.2 九个 L1 片段逐个体检

字符数是在当前 HEAD 实测的默认态 `len(fragment.text)`；9 段合计 5717 字符。

| 片段 | 所在 prompt | 字符 | 夹带的 L0 性质条款 | 纯示例部分 |
|---|---|---|---|---|
| `answer.style_language` | answer 规则 4；另被 `document_guide.py:79` 复用 | 1029 | 标题清单「每条都给出、同名不合并」、「覆盖局限说一次」、「不得为求简短删掉被要求的项」 | 无 |
| `answer.mechanism_organization` | answer 规则 8 | 746 | 断链处用（推断）桥接；区分共享与独立组件 | 括号里的层次阶梯 |
| `answer.domain_conventions` | answer 规则 9 | 309 | 量纲一致；记号不同时以（推断）注明换算 | 「电路可实现形式」子句与两个公式例子 |
| `answer.numeric_attribution` | answer 规则 10 | 1545 | 单来源数值归属、逐条件比较、绝不跨条件猜测或转移数值、冲突要报告 | 一个括号例子 |
| `expand_query.decomposition_guidance` | 规划 | 548 | 无 | 两个括号例子 |
| `intent.cross_tool_mapping` | 意图合同 | 843 | 无 | Innovus/ICC2 例子行 |
| `report.storm_lenses` | 报告 STORM 大纲 | 305 | 必须产出非空视角集合（`prompt_layers.py:243-248`） | 通用视角集合 |
| `report.frame_example` | 报告 STORM 大纲 | 149 | 尾随空格契约（`:258-260`） | 名词串 |
| `report_section.domain_conventions` | 报告分节 | 243 | 推导链完整、单来源数值归属 | 「prefer circuit-realizable forms」 |

结论：今天的 L1 边界是按「有没有服务端解析器依赖原文」划的，而不是按「改了会不会削弱纪律」划的。
如果直接开放整段覆盖，「Never guess or transfer a value between conditions」这类条款就可以被删掉，
而服务端没有任何校验能发现。

### 1.3 已有的「按人 / 按库」注入通道

| 通道 | 粒度 | 进入哪些 prompt | 形态 | 出处 |
|---|---|---|---|---|
| 用户风格档案 | 按用户 | answer、expand_query（L2 `style_block`） | 封闭枚举字段加领域术语；`origin=job` 的推断值在用户确认前不注入 | `backend/app/services/search_profile.py:186` `render_style_block`、`:153` `_user_field_value`、上限 `:103` |
| 笔记本理解底座 | 按笔记本 | 只进规划与 reflect，**永不进答案合成** | 五类自由文本块，每块 ≤400 字符、整块 ≤1200 | `backend/app/services/agent_profile_block.py:16-20`、`:62`、`:66`；写权限 `agent_profile:write`=admin（`deps.py:498`） |
| 检索经验库 | 本库分区，全局补齐 | 只进规划（L2 `experience_block`）与 reflect 的 `consult_memory` 动作 | 「情形 → 检索动作 → 好/坏」，≤600 字符，不含范围词 | `retrieval_experience_block.py:24-33`、`:61`；注入开关默认开（`config.py:1357`） |
| 插件 reflect 动作 | 按部署 | reflect | 插件只能写动作名与描述；四句固定句不可改；拒收控制字符 | `prompts.py:525`、`domain/reflect_action.py:214` |

可以复用的先例：

- 「默认态逐字节等于特性不存在」：`answer_prompt` docstring 对 `style_block`、`external_rules`、`report_block`
  的写法（`prompts.py:371-389`）。
- 「后台推断值经人接受才注入」：`_user_field_value`。
- 「ContextVar 用上下文管理器安装、按 token 复位」：`source_scope.py:808-812`。

### 1.4 评测资产逐项（能不能用）

| 资产 | 内容 | 能否直接用于本题 | 处置（P2） |
|---|---|---|---|
| `backend/app/eval/inference.py` | 对每题跑 `repo.ask`，再用带「期望要点」的 LLM 评委打 correctness / inference_quality / grounding_consistency / fabricated_citation | **部分可用**：评委有参考要点，比 reference-free 可靠；但走的是 `create_bare_schema_guest_repository(..., sqlite_only=True)`（只能跑 SQLite），评委打分不是门 | **复用**评委 prompt 与解析器（`judge_prompt` / `parse_judge`）作为去锚定的回归探测；runner 重写为可跑 PG |
| `backend/app/eval/questions.yaml` | 30 题，L1–L4（含 7 道应拒答题），模拟 CMOS 教材，期望要点 | **可用作种子**：题与要点不绑库 id；L4 拒答题正好用来测规则 2/3/13；语料是受版权保护的教材，**不能入库**，只能在本机或用户库上跑 | 迁入新套件格式，标为「需本地语料」 |
| `backend/app/eval/recall_gold.yaml` | 45 题，金标是 KG 对象/关系 id，绑定 `nb-b37185f4ae` 当前 KG | **不可直接用**：KG 一重建 id 就失效；而且只测 KG 检索，不测规划 | 题面可当开发集种子，金标需改成章节路径或来源别名才能复用 |
| `backend/app/eval/retrieval_metrics.py` | recall@k、MRR、`leakage_ratio`（题面复用原文的比例） | **可用**：纯函数 | **复用**；`leakage_ratio` 用作新题的泄漏体检 |
| `backend/app/eval/run_all.py` | 一键 quality/speed/inference/recall；默认库 `nb-012fb94249`、默认 `.local/silicon_notebook.db` | 绑定本机库 id 与 SQLite 路径 | 不扩展；新评测台独立入口 |
| `backend/app/eval/memory_agent_ab.py` | 三种记忆模式的确定性 Agent 夹具 | 与 prompt 无关 | 不用 |
| `backend/app/eval/reflect_context_bench.py` | `llm.jsonl` ⋈ `events.jsonl` 的逐调用表（token、缓存 token、重试） | **可用**：做成本与前缀缓存测量 | **复用** |
| `backend/app/eval/selected_source_graph.py` | 成对基线/影子、硬不变量、无内容 attestation（`:1-8`） | **可用作模式** | 照其形状写 attestation |
| `backend/app/eval/sa_calibration.py` | 同一批窗口跑新旧两版抽取 prompt，逐指标对照；指标函数是纯函数并有单测，`main()` 是人工测量工具（文件头 docstring） | **成对 prompt A/B 的先例**；但测的是 KG 抽取，`:121` 硬编码了一台旧机器的 `.env` 路径 | 照它的「纯指标函数 + 人工入口」结构写，不复用代码 |
| `backend/app/eval/ask_latency.py` | 读 `events.jsonl` 的 `ask_stage`，出各阶段 P50/P95/max | **可用** | 复用，作为冒烟门第 5 条（延迟）的数据源 |
| `backend/app/eval/reflect_t0/questions.json` | 公开论文题集：34 道问答题加 4 道报告题，带 `gold_section_path`。语料 A 是 arXiv 2502.05171（与 probe-floor 同一篇），语料 B 是 6 篇公开论文（含 DeepSeek-V2）；库 id 由 runner 显式传入，不入库 | **可用，而且已经在仓库里**；reflect T0 调优用过，**只能进开发集** | 作为 P2 第一套开发集（覆盖 probe-floor）。注意语料 B 含 DeepSeek-V2，正是 `decomposition_guidance` 默认对比例子里的实体：在 B 上，F4 的默认例子是「贴合」的 |
| `scripts/replay_retrieval.py` | 固定题集跑检索（不调答案 LLM），`--compare` 逐题 diff | **受限**：`sqlite_only=True`（`:53`）；`--full` 用固定子查询，reflect 是「立即 answer」的桩（`:126-134`）；不跑 `query_intent` | 加「规划在环」「意图在环」「真 reflect」三种模式和 PG 支持 |
| `scripts/export_reasoning_traces.py` | 只读导出闭集投影，无问题、答案、标题、id | 可用于护栏统计 | 加 `prompt_variant.digest` 字段 |
| `baseline_sink`（`ask_service.py:4378-4386`） | 捕获 `context_block`、`id_map` | **不够**：冻结合成还要 `answer_prompt` 的全部入参（见 7.2） | 扩成完整合成清单并落盘 |
| memory 中的 `probe-floor-2026-09-07/questions.py` | 24 题 + 金标章节路径，单篇公开论文 | 已被 `reflect_t0/questions.json` 的语料 A 覆盖（同一篇论文、同样的题面） | 不再单独入库 |
| 反馈 `feedback` 表（`postgres/migrations/0001_initial.sql:224-232`）、全局评分（`0076_global_ask_feedback_index.sql`） | `useful` / `not_useful` | 弱信号 | 只当护栏 |

结论：仓库里**已有**这些东西：LLM 评委、30 题分层题集、34+4 题带章节金标的公开论文题集、成对 prompt A/B 的
先例。它们的局限是只跑 SQLite、绑定本机库、评委不是门。仓库里**还没有**规划在环的检索对照、冻结全部入参的
合成对照、跨后端 runner 和无内容证明。P2 的工作量主要在后面这几项（7.2）。

### 1.5 运行上下文与多阶段流程

- 范围与运行形态装在 ContextVar 里：`source_scope.py:625` `_CURRENT_SOURCE_SCOPE`；安装方式是上下文管理器
  `set` 之后在 `finally` 里 `reset(token)`（`:808-812`）。`subjectless_run_active()`（`:969`）在全局问答时为真。
- 长寿线程池：`global_ask.py:711` `_retrieval_pool`、`model_scheduler.py:73` `_executor`。池线程跨任务复用，
  裸 `ContextVar.set` 会把值留到下一个任务。传播用 `contextvars.copy_context().run`
  （`source_scope.py:2568`、`reasoning_retrieval.py:6481`）。
- **问答是两个请求**：意图预览 `POST /notebooks/{id}/ask/intent`（`ask_routes.py:662`，流式 `:740`，用到
  `intent.cross_tool_mapping`），用户确认后再发 `POST .../ask/stream`（`:1009`）做检索与合成。另有同步
  `POST /notebooks/{id}/ask`（`:862`），给 MCP 和脚本用，一次请求跑完，不建 ask job 行。
- **报告是多阶段、多请求**：`POST .../reports`（`report_routes.py:223`，`report_execution.start_plan`
  `report_execution.py:219`）→ `POST .../intent`（`report_routes.py:286`）→ `POST .../generate`
  （`:660`，`start_generate` `report_execution.py:301`）。
- 合成调用点：`ask_service.py:4388`、`:4441`、`:5087`、`:5468`；规划 `query_rewrite.py:85`；意图
  `query_intent.py:460`；报告 `report_engine.py:1868`、`report_context_runtime.py:373`。

### 1.6 日志现状

- 仓库规则：密钥、令牌、私有路径、来源内容、**prompt**、证据和异常文本不得进入日志与无内容遥测
  （`AGENTS.md:77-78`）。
- 既有例外：`llm.jsonl` 是按用户分目录的调试通道，会记录截断后的 prompt 全文（`core/llm.py:976-981`，
  `LLM_LOG_MAX_CHARS` 默认 4000，`config.py:1733-1735`）。定制文本作为 prompt 的一部分会出现在这里，
  待遇与来源内容相同。

### 1.7 导出格式

`domain/notebook_archive.py:47-48`：`FORMAT_VERSION = 1`，`READABLE_FORMAT_VERSIONS = (1,)`。表集合一变就
必须升版本（`:27-31`）；读到比自己新的版本会拒绝，并提示先升级部署（`:300-305`）。

`tests/test_notebook_archive_format.py:36-60` 钉住两条：

- 两个后端的复制闭包（`_COPY_SNAPSHOT_QUERIES`）**必须等于** `ARCHIVE_TABLES`。
- 每个格式版本对应一个冻结的表集合。

理解底座 `agent_notebook_profile`、观测日志、检索经验都**刻意不在**复制闭包里
（`postgres/sharing_store.py:342-381` 的注释）。

### 1.8 发现的漂移（P1 顺手修）

- `prompt_layers.py:352-357` 对 `experience_block` 的描述、`prompts.py:1681-1685` 的注释，仍写着「部署级」
  「默认关」。#769/#773 之后它已经按笔记本分区，并且默认开启。
- memory 里记的「`decomposition_guidance` 在 plan_prompt 还有一份备用拼写」已经不存在：全仓只剩
  `prompts.py:1718` 一处。
- 单读取路径守卫没有覆盖 `document_guide.py`（1.1）。

---

## 2. 设计红线

1. **L0 永不可定制、永不自优化**；self-evo 机器自己的 prompt 也不许被自优化。
2. **不加新的读取路径**：一切定制文本都经 `fragment_text()`。`prompts.py` 只改**一次**：插入五个框定片段的
   `fragment_text(...)` 调用（3.4）。之后的定制不再改它，函数签名也不变。
3. **默认态零回退**：没有定制的笔记本、全局问答、所有 L0-only prompt，渲染结果与改动前逐字节相同。
4. **定制不改变任何权限，权限在检索层**（用户裁决）。定制文本里写了库名、来源名、工具名都无所谓：可读范围
   只由检索层按请求者权限与勾选决定，prompt 里的任何文字都扩大不了它。本规格不做「库名检测」类校验，
   因为它既查不全，也不是防线所在。
5. **示例不是证据**：槽位文本不可被 `[k]` 引用，不进 `context_block`。
6. **自动的只有「算」和「报」**：生成候选、跑评测、出证明、护栏报警可以自动；任何文本**生效**都必须经人。
7. **展示原则**：只在真影响结果时提示用户；原因码写进事件，界面不猜原因。

---

## 3. 子题 (a)：按笔记本定制

### 3.1 槽位：连接词一起包进去，核心三态同源

每个槽位登记：`slot_id`、所属片段、**三态文本**、字符上限、渲染方式（列表分隔符）。

- **默认态**：等于今天原文中对应的那一截，包含连接词，例如整个 ` (e.g. circuit principle -> … -> engineering practice)`。
  核心加默认槽位拼回去，与现在的片段逐字节相等。
- **定制态**：由字段值经固定模板渲染，模板也包含同样的连接词，例如 ` (e.g. ` + 字段列表按 ` -> ` 连接 + `)`。
- **通用态**（用户选「清空为通用」）：一段写在代码里的通用文本，可以是空串（仅限去掉后语法和契约都不受影响的槽位）。

**核心**（片段减去槽位后剩下的文本）在三态下**逐字节相同**，这是「只换例子」的硬保证。各片段的三态如下：

| 片段 | 槽位（连同连接词） | 定制态模板 | 通用态 | 备注 |
|---|---|---|---|---|
| `answer.mechanism_organization` | ` (e.g. circuit principle -> … -> engineering practice)` | ` (e.g. ` + F1 以 ` -> ` 连接 + `)` | 空串 | 去掉括号后句子完整 |
| `answer.domain_conventions` 槽 9a | ` and prefer the circuit-realizable form the sources use (e.g. $\Delta V_{BE}=V_T\ln N$ rather than an abstract $K\cdot V_T$)` | ` and ` + F2 | 空串 | 核心是 `9. Keep formulas dimensionally consistent` + 9a + `; when converting between energy and voltage, state the conversion` + 9b + ` — as a （推断） note if the items use a different notation.\n`。「换算必须写明、记号不同时标（推断）」这条 L0 留在核心里，三态都在 |
| `answer.domain_conventions` 槽 9b | ` (e.g. $E_g=qV_{G0}$)` | 空串（F2 定制时去掉这个半导体例子） | 空串 | 9b 不绑定独立字段；只有 F2 处于默认态时才保留它 |
| `answer.numeric_attribution` | ` (e.g. strict/flexible scoring, with/without a system prompt)` | ` (e.g. ` + F3 + `)` | 空串 | 核心 1500 多字符的纪律条款一字不动 |
| `expand_query.decomposition_guidance` 例一 | ` (e.g. 'DeepSeek-V2 …', 'DeepSeek-V3 improvements')` | ` (e.g. ` + F4 各项加引号、以 `, ` 连接 + `)` | 空串 | |
| `expand_query.decomposition_guidance` 例二 | ` (e.g. circuit principle / … & materials)` | ` (e.g. ` + F1 以 ` / ` 连接 + `)` | 空串 | 与 answer 规则 8 共用 F1 |
| `intent.cross_tool_mapping` | 整个 `  Example: … not "place_opt_design usage".\n` 行 | `  Example: for "` + F5.问题 + `", a good target-side query is "` + F5.好 + `", not "` + F5.坏 + `".\n` | 空串 | 核心以 `never the one dropped.\n` 结尾，形状契约（结尾 `\n`）三态都成立 |
| `report.storm_lenses` | **追加位**：紧接在 `risk-skeptic` 之后、`)` 之前，默认是空串 | ` / ` + F6 以 ` / ` 连接 | **没有通用态**：写入 `{"cleared": true}` 时拒收（422 `field_not_clearable`），因为通用态就等于默认，删除这个键即可 | F6 只能**追加**通用视角，不能替换。L0 规则 2 的例子「the skeptic asks about failure modes」（`prompts.py:2172-2174`）指向 risk-skeptic，替换会让它悬空 |
| `report.frame_example` | 句首名词串 `A capacity mechanism, sequence mixer, macro topology, and memory mechanism` | F7 以英文列表规则连接（`A, B, and C`） | `A component's mechanism, its structure, and its operating regime`（代码固定） | **不能为空**：核心 ` are different facets and must not be emitted as mutually exclusive peers. ` 需要主语；尾随空格契约三态都成立 |
| `report_section.domain_conventions` | ` and prefer circuit-realizable forms` | ` and ` + F2 | 空串 | 与 answer 规则 9 共用 F2 |
| `answer.style_language` | 无槽位 | — | — | **整段锁定** |

新增测试（长期保留）：

- 每个片段都登记一组「锁定核心子串」，覆盖 1.2 表里列出的全部 L0 性质条款。测试断言它们在默认、定制
  （用一组最长合法值和一组最短合法值）、通用三态下都逐字出现。
- 形状契约测试（`test_prompt_layers.py:103-142`）扩展为三态都要通过。
- 每个槽位登记它依附的**语法宿主**：嵌入的那句话，以及槽位前后紧邻的核心文本。例如 9a 的宿主是
  `Keep formulas dimensionally consistent` 与 `; when converting`。测试断言宿主在三态下都存在，而且槽位文本恰好
  夹在宿主前后两段之间。A 类候选也必须保留每个宿主（5.4）。

### 3.2 字段

面向用户的是 7 个字段。一个字段可以填多个槽位，这样同一个库前后说法一致。

| 字段（界面名待 `docs/ui-vocabulary.md` 定） | 形态 | 填哪些槽位 | 单项上限 | 字段上限 |
|---|---|---|---|---|
| F1 机制层次 | 2–6 个短语的有序列表 | answer 规则 8；expand_query 例二 | 每项 ≤40 | 240 |
| F2 公式与记号惯例 | 单行子句 | answer 规则 9；report_section 规则 3 | — | 300 |
| F3 比较条件示例 | 单行 | answer 规则 10 | — | 120 |
| F4 实体对比示例 | 1–3 个短语 | expand_query 例一 | 每项 ≤60 | 180 |
| F5 跨工具映射示例 | 三元组（问题 / 好的目标侧查询 / 坏的查询） | intent 例子行 | — | 300 |
| F6 追加的报告通用视角 | 1–4 个短语 | storm 视角追加位 | 每项 ≤40 | 160 |
| F7 报告维度区分示例 | 2–5 个短语 | storm frame 反例 | 每项 ≤40 | 200 |

**计数口径**（三条，各自独立成立）：

1. 字段上限合计 1500 字符；**整库存储总帽 1200**（按各字段存储原文的字符数之和计），比各字段上限之和更紧，
   写入时两条都要检查。
2. F1 和 F2 各渲染两处，因此单个 prompt 的渲染增量只按它用到的字段算：answer ≤ F1+F2+F3 = 660，
   expand_query ≤ F1+F4 = 420，intent ≤ 300，storm ≤ F6+F7 = 360，report_section ≤ 300；
   再加框定句（3.4，约 150 字符）。
3. 与已有 L2 块叠加后仍然全部有界：规划 prompt 已有的理解底座 ≤1200、经验块 ≤600、风格块 ≤600、
   集合地图有上限；答案 prompt 已有风格块 ≤600。本设计只在这些常量之上加上面的固定上限，不引入任何
   随数据增长的项。

### 3.3 三态的存储语义

`fields_json` 是一个对象：

- **键缺失**：该字段用默认态。
- **键的值为 `{"cleared": true}`**：通用态。
- **键的值为合法字段值**：定制态。
- **「全部恢复默认」**：写入 `{}`。
- **不可清空的字段**：只有 F6。F6 写入 `{"cleared": true}` 时拒收（3.1）。
- **「已定制」的判定**：`fields_json` 里至少有一个合法键（cleared 也算），这只决定设置卡上怎么显示。
  **框定句按 prompt 判定**：某个 prompt 用到的槽位里至少有一个不在默认态，才给这个 prompt 追加框定句（3.4）。
  `{}`、或者全部键都非法时，所有 prompt 的渲染都与默认逐字节相同。

### 3.4 框定片段（锁定）

- 现有守卫要求每个 L1 片段都有一个 `prompt_id`、id 前缀能映射到 prompt、文本非空，并且总数恰好 9
  （`test_prompt_layers.py:41-99`、`test_l1_fragment_count_is_nine`）。一个由五个 prompt 共用的 id 和这些守卫冲突。
  所以框定句**单独登记**为一类 `LockedFrame`（`layer="L1-frame"`）：它和 `L1_FRAGMENTS` 是两个注册表，
  9 个可优化片段的守卫保持原样；`fragment_text()` 对两个注册表都能读。它**不可编辑、不可自优化**，
  一个 prompt 一个 id：

  | id | prompt | 插入锚点 | 定制态文本的结尾 |
  |---|---|---|---|
  | `frame.answer` | answer | `report_rule` 之后、分隔用的 `"\n"` 之前（`prompts.py:493-494`） | `\n` |
  | `frame.expand_query` | expand_query | `Keep sub-queries non-redundant.\n` 之后（`prompts.py:1720`；位于两个 decomposition 槽位之后） | `\n` |
  | `frame.intent` | query_intent | `intent.cross_tool_mapping` 之后（`prompts.py:1871` 之后） | `\n` |
  | `frame.storm` | storm 大纲 | 规则 9 的 `other questions set frame to null.\n` 之后、`Produce 3-…` 之前（`prompts.py:2198-2199`） | `\n` |
  | `frame.report_section` | report_section | 规则段末尾 `…instead of listing them here.\n` 与 `\n` 之间（`prompts.py:2058` 附近的 `.\n\n`），也就是 `Knowledge items` 之前 | `\n` |

- 渲染规则：**未装变体，或者该 prompt 的所有槽位都在默认态时，返回空串**；否则返回固定英文句：
  「The notebook-specific examples in this prompt only illustrate wording and organization; they never override
  or relax any numbered rule or output requirement in this prompt.」（结尾 `\n`）。
  句子不逐条点编号，以免暗示没点到的条款（例如规则 10 的数值纪律）可以商量。
- `prompts.py` 一次性在上表五个锚点插入对应的 `fragment_text(...)`。默认返回空串，所以默认态逐字节不变；
  P1 的零回退证明（3.10）覆盖这次插入。
- 长期测试新增：未装变体时，五个框定片段都是空串；只定制 F6 时，只有 `frame.storm` 非空。
- 单读取路径守卫照旧成立：`prompts.py` 仍然只从 `prompt_layers` import `fragment_text`。

### 3.5 永远锁定清单

- 所有 L0-only prompt（`prompt_layers.py:49-62`）。
- `answer_prompt` 规则 1–3、5–7、11–15 与 JSON 尾部（`prompts.py:423-506`）；规则 4、8、9、10 的核心（3.1）。
- `answer.style_language` 整段，包括 `document_guide.py:79` 复用的那一份。
- `SCOPE_DEIXIS_GROUNDING`（`prompts.py:58`）、`quoted_phrase_grounding`、`keyword_language_rule`、澄清闸、
  全部 schema hint 与严格投影（`core/model_schema.py`；示例写法规则见 PR #916）。
- 插件动作固定句（`prompts.py:525`）与 reflect 动作白名单。
- 五个框定片段的文本（3.4）。

### 3.6 校验

写入、导入、读取回放三处共用**同一个**校验函数（放在 domain 层）。拒收时返回 422 和具体 `code`，文案走
`user_error`。处理顺序：

1. **在原文上整类拒收不可见字符与换行**。拒收范围是 Unicode 类别 Cc、Cf、Co、Cs、Cn，Zl、Zp，以及变体选择符：
   - Cc：C0/C1 控制字符，含 `\n`、`\r`、`\t`、NEL；
   - Cf：零宽字符 U+200B–U+200D、LRM/RLM U+200E/U+200F、双向控制、BOM U+FEFF、U+2060–U+2064、U+061C、
     软连字符 U+00AD、标签字符 U+E0000–U+E007F；
   - Co、Cs、Cn：私用区、代理码位、未分配码位；
   - Zl、Zp：U+2028、U+2029（显式列出）；
   - 变体选择符：U+FE00–U+FE0F、U+E0100–U+E01EF。

   422 的 `code` 是 `invalid_character`，detail 里写出是哪个码位（如 `U+200B`）和它在第几个字符，用户能直接定位。
   **已知代价**：带 ZWJ 的组合 emoji、带 FE0F 的 emoji、软连字符、从网页粘贴进来的零宽空格都会被拒收。
   这些字段是示例短语，用不到这些字符；被拒时报错里会点名是哪个字符。
2. **单行不变量**：第 1 步已经拒收了所有换行类字符，所以通过校验的文本一定是单行。之后每个短语再经
   `collapse_prompt_line`（`agent_profile_block.py:148`），它只负责把连续空白折成一个空格。**真正的防线是
   「不可能换行」**：不能换行，就伪造不出「新的一条规则」或「新的 prompt 段」。
3. **结构记号拒收**：把文本做 NFKC 并 casefold 后匹配。拒收模式本身也事先做同样的 NFKC 和 casefold。
   全角写法（`［k1］`、`１０. `）在匹配时已经变成半角，因此绕不过去。NFKC **只用于匹配，不改存储**（见第 4 条）。
   拒收的模式如下：
   - 引用记号 `[k` + 数字（即 `\[k\d`，与 `[k1]` 的形状一致；`[kT/q]` 这类物理记号放行）；
   - `[external`、`[base]`、`[memory]`、`（推断）`、`likely,`；
   - `"grounded"`、`grounded=`、`grounded:`（`grounded-gate`、`grounded capacitor` 放行）；
   - `{"`（JSON 对象开头）、`"answer"`；
   - `question:`、`knowledge items`；
   - **字段开头**的「数字 + 点 + 空格」序号。

   **刻意不拒收**：单独的 `{` `}`（EDA 领域的 Tcl 花括号是合法内容）、句中的「数字 + 点」（如 `v1.`、`3.3V`）。
   理由同第 2 条：文本只有一行，模型不会把句中的数字读成新规则编号。
4. **存储**：存用户提交的原文。第 1 步只做拒收、不做删除，所以存下的文本本来就没有不可见字符。不用 NFKC 后的
   文本覆盖原文，避免 `²` 被改成 `2`、全角标点被改掉。成员只读视图（D10）看到的就是模型收到的文本。
5. 字段名闭集，形态与上限见 3.2。未知字段写入时拒收、读取时丢弃（与 `backend/app/services/search_profile.py`
   「读宽写严」的做法一致）。
6. 读取时如果发现存量行已经不合法（例如校验规则日后收紧），该字段按默认渲染，并记一条
   `prompt_customization_invalid` 事件，不上横幅。

字段可用的语言见 D21（推荐中英文都允许）。

**残余风险（不再写「由作者承担」）**：确定性校验挡不住语义诱导，例如在例子里写「不必标注推断」。定制处在
指令位置，权重高于被框定成数据的来源，所以这条风险不能等同于「他本来就能上传来源」。缓解措施有五层：
示例只出现在例子位置；锁定规则在后面，并有框定句；引用由服务端按 `id_map` 解析（`ask_service.py:2779`
`_parse_answer_anchors`）；只有 owner 能改（D4）；对成员可见（D10），并在答案旁标注、首次保存前必须试答
（D11、D12）。是否还要加上成员变更通知，见 D11。

### 3.7 谁能改、多人库的归属

- 新增能力 `prompt_customization:write`，**推荐 owner 档（D4）**。理由：定制会影响所有成员的问答，也会影响
  owner 档的报告（`reports:write` 是 owner，见 `deps.py:492`）。如果给 admin，会出现「能改报告的写法、
  却不能写报告」的错位。按字段拆权限也拆不干净：F2 一个字段同时填答案槽位和报告槽位（3.2）。
- `POST …/preview`（试答对照）同样需要 `prompt_customization:write`：它会花模型费用，用户还能在里面提交草稿文本。
- 能力表与镜像围栏表的键集合必须同步加格（`deps.py` 两张表的守卫）。镜像围栏推荐 `False`（D8）。
- 定制属于**笔记本**，不属于编辑者。版本行记 `created_by`，并登记进 `repositories/user_references.py`
  （#876 账号合并的约束）。

### 3.8 版本化、快照与回滚

- PG 规范表 `notebook_prompt_custom_versions`（SQLite 镜像同形）：`notebook_id`、`version`（每库自增）、
  `fields_json`（jsonb，存用户提交的原文，见 3.6 第 4 条）、`digest`（规范化 JSON 的 sha256 前 16 位）、`origin`
  （`user` / `restore` / `import` / `accept`）、`restored_from`、`created_by`、`created_at`；主键 `(notebook_id, version)`。
- **只追加**：当前生效的是该库 `version` 最大的一行。「恢复到某版」= 复制旧字段新写一版；
  「全部恢复默认」= 新写一版 `{}`。
- 乐观并发：写入带 `expected_version`，过期返回 409（结构化 detail，与 `knowhow_history_stale` 同族）。
- **多阶段作业钉版本**：
  - 报告：`start_plan`（`report_execution.py:219`）解析一次，把 `(prompt_custom_version, prompt_custom_digest)`
    写进报告行；之后的意图、大纲、分节、追加阶段都按这个版本号读，不再看「当前最新」。
  - 问答：ask job 行建行时写入同样两列；同一个 job 的检索、合成、后台续跑（#661 的接回）都按它读。
  - 同步 `POST /ask`（`ask_routes.py:862`）不建 job：**在入口解析一次**，用 ContextVar 带到整个请求结束。
    这条路径在一次请求内完成，不存在跨请求钉版本的问题。
  - **意图预览与合成允许跨版本**：预览（`ask_routes.py:662`）用当时最新的版本渲染；用户确认后的意图合同作为
    数据传给合成请求，合成请求再解析一次当前最新版本。**轨迹只记合成用的那一版**。预览的版本不回传，因为回传
    需要客户端把它带回来，而合同是用户确认过的文本，不依赖预览时的示例，强行钉同一版收益很小。
  - 读到被钉的版本时，如果 digest 对不上或行已不存在（理论上不会发生），按默认渲染，记事件，不中断作业。
- **迁移清单**（PG 迁移 + SQLite 镜像，两后端都要）：
  1. 新表 `notebook_prompt_custom_versions`（见上），按 `(notebook_id, version)` 建主键。
  2. 新表 `notebook_prompt_proposals`（只在 P4 加）。
  3. `reports` 加 `prompt_custom_version bigint`（可空）和 `prompt_custom_digest text`（默认 `''`）两列。
  4. `ask_jobs` 加同样两列。
  5. 新表在 `app/migration/sync/manifest.py` 登记为 `LOCAL`（不随跨环境同步走，与 D8 推荐一致；若 D8 选 (b)
     则改登记为同步类）。
  6. `app/migration/shadow/manifest.py` 中 `ask_jobs`（`:252`，现在是 19 列）与 `reports`（`:389`，现在是 48 列）
     的列数各加 2，并为新表登记行。
  7. 新表登记进 `ownership_manifest.py`、删除闭包和 `user_references.py`（`created_by` 列）。
  8. 迁移号在开发时按 `origin/master` 的最新号再定（并行分支迁移号会撞）。
- **保留策略**：每库保留最近 20 版（D19）。超出时删除最旧的版本，但**跳过仍被未终态报告或 ask job 引用的
  版本**（同一事务里按引用查询；终态作业不再需要读它）。
- 轨迹归因：ask job 与报告的轨迹记 `prompt_variant = {"version": n, "digest": "…"}`；未定制时不记该字段，
  轨迹也零变化。`export_reasoning_traces.py` 的闭集投影只导出 digest。

### 3.9 运行时解析

- 服务层新增解析器 `prompt_customization.py`：在入口按 `(notebook_id, version)` 读取版本行，校验后构造不可变的
  `PromptVariant`（槽位 id → 三态之一的文本；五个框定片段按 3.4 的规则从变体推出）。
- **安装方式**：`prompt_layers` 内的 ContextVar 只能通过上下文管理器 `prompt_variant_context(variant)` 安装：
  `token = _VAR.set(v)`，在 `finally` 里 `_VAR.reset(token)`，照 `source_scope.py:808-812`。**禁止裸 `.set`**
  （加 AST 守卫：除这个上下文管理器之外，任何地方出现 `_VAR.set` 都判失败）。
  `prompt_layers.py` 新增的依赖仍然只有标准库 `contextvars`。
- `fragment_text(fragment_id)` 改为返回「核心 + 当前变体中该片段的槽位文本」；没有安装变体时用默认态。
  签名不变。
- 线程池：所有在池内渲染 prompt 的路径都必须走 `copy_context().run`。守卫新增两条：
  1. 在每个工作负载的合成点，以真实调用链断言：本次运行装了变体时，渲染结果包含变体文本。
  2. 在 `global_ask._retrieval_pool` 与 `model_scheduler` 的池上各提交「装了变体的任务」和「下一个普通任务」，
     断言下一个任务里变体是 `None`。
- 运行形态：
  - **单库问答、报告、报告追问、MCP `ask_notebook`**：用该库定制（D9）。
  - **全局问答**（`subjectless_run_active()` 为真）：一律默认（D6）。
  - **挂载参考库**：只用主体库的定制。
  - **文档导读**：只用到锁定的 `answer.style_language`，不受影响。
- **部署级总开关**（D20）：`PROMPT_CUSTOMIZATION_ENABLED`，形态与 `retrieval_experience_inject_enabled` 相同
  （`config.py:1357`）。关闭时不读表、不装变体，与特性不存在逐字节相同；已存的定制保留。
- 读取成本：每次运行多一次按主键的点读；没有定制的库读到空就走默认。可选优化：在 `notebooks` 行上加
  `prompt_custom_version` 列，借用已有的笔记本行读取，让未定制的库零额外读。但 `notebooks` 行在复制闭包和
  同步闭包里，这一列必须从两者排除。先测量点读成本，确有必要再做。

### 3.10 零回退证明与 P1 门禁

1. 重切与框定片段插入：以重切前的源码为基线，对每个 prompt 用多组参数重渲染，逐字节比较。这是一次性证明，
   与 #637 的做法相同，不留 refactor-only 快照。
2. 长期语义测试：未装变体时 `fragment_text` 等于默认拼接；锁定核心子串三态都在（3.1）；A 库变体不影响并发的
   B 库运行；全局问答即使主体库有定制也渲染默认；池复用后变体不残留（3.9）；校验拒收清单逐条有用例，
   包括零宽、双向、标签字符、全角绕过、Tcl 花括号放行；保留策略不删在途作业引用的版本。
3. 性能：解析器点读的耗时在 PG 和 SQLite 上各测一次，附在 PR 里。

### 3.11 与分享、导出、复制、同步、删除的关系

| 场景 | 推荐 | 说明 |
|---|---|---|
| 网页/CLI 导出（`notebook_transfer/export.py`） | **只带最新一版字段**，不带历史（D7） | **不是一张表**：归档里加一个独立条目 `prompt_customization.json`（内容是 `{"version": 1, "fields": …}`，里面的 `version` 是这一段自己的格式版本）。`ARCHIVE_TABLES` 与复制闭包都不变，`test_notebook_archive_format.py:36-60` 的「复制闭包 = `ARCHIVE_TABLES`」照旧成立。有这个条目时，归档 `FORMAT_VERSION` 写 2；**未定制时仍写 1**，所以未定制的库导出后旧部署照样能导入。有定制的文件导入旧部署会被拒，并提示先升级（`notebook_archive.py:300-305`）。读取侧 `READABLE_FORMAT_VERSIONS = (1, 2)`；版本冻结测试改为 `{1: T, 2: T}`，两个版本的表集合相同，差别只在这个可选条目。导入时过同一个校验，非法字段丢弃并列在导入结果里；写入时 `origin=import`，作者记为导入者 |
| 复制（含 `/shared/{token}/copy`，`notebook_routes.py:453`） | 与导出一致：只带最新一版 | **不进复制闭包**。理解底座 `agent_notebook_profile` 等表是直接排除在闭包之外（`postgres/sharing_store.py:342-381`），本设计也让定制表留在闭包之外，但在复制成功之后**单独跑一步**：把源库最新一版重新校验，再写进新库（`origin=import`，作者记为复制者）。这一步失败时不回滚复制，复制结果里标明「定制未带过来」，并记事件。闭包注释块里补一段说明，与 `agent_notebook_profile` 那段并列 |
| 链接只读加入（`/shared/{token}/join`） | 按成员对待 | 答案应用定制，成员可以只读查看 |
| 问答/报告分享页 | 不展示定制内容 | 轨迹里只有 digest |
| 跨环境同步镜像 | **目标端自有、不同步、镜像上可改**（D8） | 与 `agent_profile:write` 的围栏取值一致（`deps.py:574`） |
| 删除笔记本（含宽限期） | 随库删除 | 登记进删除闭包 |
| MCP | 不提供读写工具（D9） | Agent 令牌不能改定制 |

### 3.12 界面与 API 草图

- `GET /notebooks/{id}/prompt-customization`：返回当前字段（三态逐字段标明）、版本、最近修改人和时间、
  `can_edit`（由能力表算出）。有读权即可调用（D10）。
- `PUT …`：`{expected_version, fields}`。`POST …/restore`：`{version, expected_version}`。`GET …/versions`：版本列表。
- `POST …/preview`（试答对照，D13）：`{question, draft_fields, include_planning}`。需要 `prompt_customization:write`。
  **能覆盖哪些槽位**：
  - 默认（`include_planning=false`）：两版共用同一份检索结果，只换答案合成。覆盖 F1 的答案槽位、F2 的规则 9 槽位和 F3。
  - `include_planning=true`：两版各自跑一次规划和检索。额外覆盖 F1 的规划槽位和 F4；如果这个问题会走意图合同，也覆盖 F5。
  - **不覆盖** F6、F7 和 F2 的报告槽位。试一次报告太贵，这几个字段只能看护栏。
  边界：
  - **不写**问答记录、反馈、学习链、经验蒸馏、活动记录、提问分析，也不推进任何巡固计数。
  - 未保存的草稿走同一个校验。
  - 可以取消（复用现有取消机制）。
  - 限频：每用户每库每分钟 2 次、每日 30 次（数值待定）。
  - 费用：默认一次检索加两次合成；`include_planning=true` 时两次规划、两次检索、两次合成。
- **护栏（原 P5，已并入 P1）**：设置卡上按 digest 分组展示近 N 天的弱信号（`not_useful` 率、grounded 率、
  引用密度、空结果率、回答长度），与定制前对比。样本不足时只显示「样本不足」。越线时卡上提示，并给出
  「恢复上一版」按钮，不自动恢复（D14），不上横幅。
- 答案旁标注与变更通知见 D11；首次保存前必须试答见 D12。
- 按钮反馈遵守 `CLAUDE.md` 的交互反馈规则。文案写入 `docs/ui-vocabulary.md`；产品与 API 合同写入
  `docs/product-and-api*.md`（中英成对）。

---

## 4. 与检索经验库、理解底座、风格档案的边界

| | 检索经验库 | 理解底座 | 用户风格档案 | **本设计：定制字段** | **本设计：self-evo** |
|---|---|---|---|---|---|
| 作用对象 | 规划与 reflect 选**检索动作** | 规划与 reflect 对**语料的认识** | 答案与规划的**措辞偏好** | 答案、规划、意图、报告里的**领域示例** | A：规划指令文本（部署级）；B：示例字段的建议 |
| 层 | L2 | L2 | L2 | L1 槽位 | L1 |
| 进答案合成 | 永不 | 永不 | 进（只限措辞） | 进（只限例子） | A 不进；B 采纳后进 |
| 粒度 | 本库分区，全局补齐 | 按库 | 按用户 | 按库 | A 部署级；B 按库 |
| 谁写 | 蒸馏作业 | 作业 + 管理员 | 用户 + 作业（作业值需用户接受） | owner | 离线流程产候选，由人决定 |
| 证据 | `adopted` 计数 | 无 | 无 | 试答对照 + 护栏 | 评测台冒烟门 |

**self-evo 比经验库多的东西，直说只有两件**：(1) 能改**指令或示例文本本身**；(2) 用**带金标的离线评测**判断
有没有大的回退。第 (2) 件的价值全部来自 P2 评测台，A 类自优化真正新增的只是一个候选生成器。
「对这类问题先用哪个检索动作」的知识继续由经验库学；「这个库有哪些实体、缺什么」属于理解底座；
「这个领域怎么举例、怎么分层」才属于定制字段。

---

## 5. 子题 (b)：self-evo

### 5.1 范围

- **A 类**：推荐**只做 `expand_query.decomposition_guidance`**（D16）。`intent.cross_tool_mapping` 在公开语料里
  几乎没有跨工具题，样本不够。奖励是检索金标命中。产物是部署级的新默认文本。
- **B 类**（其余表达层片段）：**禁止指令自优化**，只允许为某个库**提**字段建议值，由 owner 逐条采纳。
- **永不**：L0、L0-only prompt、self-evo 自己的反思和提案 prompt、按库在线 bandit、以 LLM 打分作为优化目标。
- **关于文献的边界**：调研引用的 Wan et al.（NeurIPS'24）结论是「示例选择的收益常常压过指令优化」，
  它说的是成段的 few-shot 演示，**不能直接套用**到句中「(e.g. …)」这种短语级槽位上。B 类的价值需要 P2 的
  示例敏感度实验来验证（D1 选项 c）。

### 5.2 信号来源

| 信号 | 可判定 | 用途 |
|---|---|---|
| 金标章节或来源命中 | 是 | A 类目标与冒烟门 |
| 引用合法率（锚点能否在 `id_map` 中解析） | 是 | 合同判据 |
| `grounded=false` 时没有 `[k]`（规则 2） | 是 | 合同判据 |
| 回答语言匹配（`classify_ask_language`，`domain/search_profile.py:319`） | 是 | 合同判据 |
| `model_json_shape` 诊断事件率 | 是 | 合同判据 |
| L4 应拒答题没有伪造引用（`questions.yaml` 的 refuse_or_infer 类） | 是（看有无 `[k]`） | 合同判据 |
| 结论句缺少（推断）标注 | 启发式 | 只报警 |
| `inference.py` 评委分（带期望要点） | 弱 | 去锚定的回归探测，不当门 |
| `not_useful` 率、引用密度、grounded 率、空结果率、回答长度、追问纠偏率 | 弱 | **只当护栏**，永不当目标 |

生产问题与答案**不进**优化集，也不进仓库。

### 5.3 A 类分布偏差

在公开 arXiv 语料上调出的默认值会全局改变生产（电路、EDA）行为。处理（D16，推荐两条都做）：

1. P3 只做 `decomposition_guidance`。
2. 合入前必须做**只评测的复核**：在用户自有库上，用用户自己写的题（不少于 20 题，不入库）跑同一个冒烟门，
   把无内容证明附在 PR 里，不通过就不合入。复核的库里**至少要有一个已经定制过的库**（定制了 F1 或 F4）。
   这样能验证新核心和用户写的槽位拼在一起依然通顺、依然有效。

### 5.4 A 类离线流程

1. 种子是当前默认；规划 LLM 在环，检索真跑（P2 的「规划在环」模式）；开发集跑，hold-out 由脚本拒绝读取。
2. 反思模型必须是前沿档；hold-out 永远不给它看。
3. 候选约束：只能改该片段的**核心**文本；**槽位登记（id、位置、连接词、三态模板）不变**；**每个槽位依附的
   语法宿主必须保留**（3.1：槽位所在的那句话，以及它前后紧邻的核心文本，例如 `emit ONE sub-query per entity` +
   槽位 + `.`）；候选核心在三态下逐字节同源；锁定核心子串清单要么保留，要么由人在 PR 里显式修改；形状契约不变；≤1500 字符；过 3.6 的
   同一个校验。
4. 样本 20–100 题。
5. 产物：候选文本 + 无内容 attestation（套件版本、模型 id、逐题胜负计数、检验统计量、digest），格式照
   `app/eval/selected_source_graph.py`。
6. 落地：**由人**审候选，开代码 PR 改默认值，走 codex 评审与 CI（D15）。没有运行时覆盖表，因此不可能自动上线。

### 5.5 B 类：示例字段建议

1. 只有 owner 在设置卡上点「生成建议」时才触发，不做后台定时。
2. 一次有界 LLM 调用，读本库理解底座和少量本库来源片段，按字段产出建议值，每条附证据 id。只取本库自有来源，
   不取挂载参考库，**不取任何用户的问答内容**。
3. **来源内容是不可信的**，而建议值将进入指令位置，所以：
   - 建议值走与人写字段**同一个**校验（3.6）。
   - **禁止逐字照抄来源**：与证据原文的最长公共子串超过 20 字符（数值待定）的建议直接丢弃。
   - 界面逐条显示**建议全文与证据原文**。
   - **只能逐条采纳**，不提供批量采纳。采纳即写一版定制（`origin=accept`），之后与人写字段没有区别。
4. 建议存在 `notebook_prompt_proposals`（状态 pending / accepted / dismissed / retired），**不注入**。证据来源被删除
   或重新解析导致证据 id 失效时，未采纳的建议自动退役。
5. 新工作负载要写进模型绑定表与 `model-services.example.toml`（缺绑定会拒绝启动，#779）。

### 5.6 冒烟门（不叫「证明」）

hold-out 约 20 题，统计功效有限。判据如下：

- 每题每配置跑 3 次，取多数结果作为该题的「命中/未中」。
- **成对不一致计数**：b = 候选命中而基线未中的题数，c = 反过来的题数。用**精确符号检验**（等价于精确 McNemar）。
- **可检出的最小效应**：单侧 α=0.05 时，需要不一致对 5 题全胜（p=0.031）、8 题中胜 7、10 题中胜 9、12 题中胜 10。
  在 20 题上，这意味着至少要有约 25% 的题翻转且几乎没有反向翻转，才能宣称「有提升」。
- 门（全部满足才允许开 PR）：
  1. 改进检验 p ≤ 0.05（检不出提升就不改：没有证据的改动不值得冒险）；
  2. 每个语料 c ≤ b，且合计 c ≤ 1；
  3. 合同判据的违反数，在成对比较中不增加；
  4. 人盲评 20 题，候选判负不超过 2 题；
  5. 延迟与 token 中位数增幅 ≤ 5%；
  6. 5.3 的用户库复核通过。
- 定位：这道门**只能排除大的回退、检出大的提升**，不能证明「没有回退」。
- `reflect_t0` 题集（覆盖 probe-floor）在 reflect T0 中调优用过，**只进开发集**，不进 hold-out。hold-out 只从新题中抽取。

定制字段（子题 a）不做统计门：流量太小，没有金标。保障是 3.10 的零回退证明、3.6 的锁定与校验、试答对照与护栏。

### 5.7 成本（量级，P2 实测后更新）

- 默认态实测：规划 prompt 骨架约 2.9k 字符，意图 prompt 约 5.9k，答案 prompt 约 8.9k（空上下文）。
- A 类一轮：40 题 × 3 次 × 8–15 个候选 ≈ 1000–1800 次规划调用（便宜档），加约 15 次反思调用（前沿档，
  每次约 2 万 token），加 hold-out 20 题 × 3 次 × 2 配置 = 120 次，再加用户库复核 20 × 3 × 2 = 120 次。
  检索不调 LLM。
- 冻结合成对照：60 题 × 3 次 × 2 配置 = 360 次合成，每次约 1–1.5 万 token。
- 示例敏感度实验（7.3，按 4 组算）：冻结合成 40 题 × 3 次 × 4 组 = 480 次合成（每次约 1–1.5 万 token），
  规划在环 40 × 3 × 4 = 480 次规划调用加对应的检索，另需人工盲评约 60 对（约 2 小时）。
- B 类建议：每次点击一次有界调用。
- 定制运行时：零模型成本；每个 prompt 的增量见 3.2。
- **前缀缓存**：未定制的库之间，答案 prompt 共享规则 1–13 这一段前缀（之后是外部、报告、历史与问题等每题
  不同的内容）。某个库定制后，它与其他库从规则 8 起分叉，只影响**跨库**命中，**同库**照常命中。reflect 是
  L0-only，不受影响。用 `reflect_context_bench.py` 的 `cached_tokens` 列可以实测。

### 5.8 何时自动、何时必须人审

| 动作 | 自动 | 人审 |
|---|---|---|
| A 类候选生成、评测、出 attestation | 是 | |
| A 类候选成为默认 | | 是（代码 PR，外加用户库复核） |
| 生成 B 类建议 | owner 点击后自动 | |
| B 类建议注入 | | 是（逐条采纳） |
| 证据失效时建议退役 | 是 | |
| 定制保存与恢复 | | 是 |
| 护栏越线 | 记事件，卡上提示 | 由 owner 决定是否恢复（D14） |
| 已合入的 A 类默认回退 | | 是（revert PR） |

---

## 6. 安全

### 6.1 prompt 注入威胁模型

| 威胁 | 路径 | 防线 |
|---|---|---|
| 借定制越权读其他库或来源 | 定制文本 | 定制不改变任何权限，权限在检索层；能选检索动作的 reflect 是 L0-only |
| 伪造新规则行、伪造 JSON、伪造引用 | 换行、段头、`{"`、`[k` | 单行化（主防线）+ 3.6 结构记号拒收；引用由服务端解析 |
| **隐形文本或同形字绕过拒收清单与成员审阅** | 零宽字符、双向控制、BOM、标签字符、变体选择符、全角 `［k1］` `１０.` | 在原文上整类拒收 Cc/Cf/Co/Cs/Cn、Zl/Zp 和变体选择符，422 里指出是哪个码位；结构记号在 NFKC + casefold 后匹配（拒收模式本身也先归一化）；存原文、不存 NFKC 结果，不可见字符本来就进不了库，成员看到的就是模型收到的 |
| 诱导模型不标（推断）、乱设 grounded | 语义诱导 | 锁定规则在后 + 不枚举编号的框定句；仅 owner 可改；对成员可见、答案旁标注、首次保存前必须试答；护栏统计 grounded 率 |
| 导入归档带恶意定制 | 导出/复制 | 导入时重新校验；导入者成为作者，卡上可见 |
| 外部 Agent 写定制 | MCP | 不提供写工具 |
| B 类建议把不可信的来源内容提进指令位置 | 建议作业 | 同一校验；禁止逐字照抄；逐条显示全文与证据；不能批量采纳 |
| B 类建议把他人问答带进 prompt | 建议作业 | 只读本库来源与理解底座 |
| 变体串到别的任务 | 长寿线程池 | 上下文管理器 + token 复位；AST 守卫禁止裸 set；池复用测试 |
| 自优化改坏 L0 或它自己 | GEPA | 范围闭集；槽位登记不变；只能经代码 PR 落地 |

### 6.2 日志

- `events.jsonl` 与活动记录只写：`notebook_id`、字段名列表、版本号、digest、每字段字符数、操作者 id、
  拒收 `code`。**不写字段内容**。试答对照不写活动记录。
- `llm.jsonl` 维持现状（1.6），不为定制新开内容日志。
- attestation 与轨迹导出只含 digest 与指标。

---

## 7. 质量：没有问答评测台时怎么做 A/B（P2 内容）

### 7.1 原则

- **不做在线 A/B**：单库流量太小。「纠偏 0/38 vs 4/136，p≈0.37」的教训说明，在线小样本得不出因果。
- **做成对离线对照**：同一题、同一检索快照，只换 prompt；每个配置跑 3 次；看逐题的胜负，不看均值。
- **三档判据**：确定性合同判据、金标命中、少量人盲评，这三档可以做门。LLM 评委只做回归探测：必须去锚定
  （先独立作答再比较），并沿用 `inference.py` 的期望要点。
- 同一套工具还能直接推进三条现有待办：规则 12 的 A/B、经验注入开关的 A/B、首轮播种的人工抽问。

### 7.2 工作项（显式列出）

1. **套件格式与第一批题**：题面、语言、题型标签、金标（章节路径子串或来源别名，不用 KG 对象 id）、开发集或
   hold-out 标记、语料清单（公开论文的 arXiv id 与 sha256；受版权保护的语料只登记「需本地提供」）。
   种子包括：`reflect_t0/questions.json` 34+4 题（开发集，覆盖 probe-floor）、`questions.yaml` 30 题（需本地语料，含 7 道 L4 拒答题）、
   `recall_gold.yaml` 45 题的题面（金标重标）、2 套新公开语料各约 20 题（新题用 `leakage_ratio` 做泄漏体检）、
   规则 12 的三句式。
2. **跨后端 runner**：不走 `sqlite_only`。PG 跑法：一次性库（`createdb`，建 `pg_trgm`、`btree_gin` 扩展，URL 带
   用户名）或只读连接用户库；通过 `create_bare_schema_guest_repository(Settings())` 取仓储，`DATABASE_URL`
   指向 PG。SQLite 只用于测试 runner 本身（D18）。
3. **检索对照的三种新模式**：规划在环（真跑 `expand_query`）、意图在环（真跑 `query_intent`）、真 reflect
   （替换 `replay_retrieval.py:126-134` 的桩）。
4. **冻结合成清单**：在合成调用点捕获 `answer_prompt` 的**全部入参**，即 `question`、`context_block`、
   `history_block`、`sectioned` / `section_title` / `section_index` / `section_total`、`style_block`、
   `external_rules`、`peer_notebooks`、`report_block`，加上 `id_map`、schema hint、模型 id、thinking 模式、
   `answer_max_tokens`。整份清单落盘到评测输出目录（含内容，**不进日志、不进仓库**）。回放时只替换变体。
   报告分节（`report_section_prompt`）同样处理。
5. **确定性合同判据**：实现 5.2 中所有「是」的项。
6. **盲评**：脚本生成本地盲评表（随机左右、隐藏变体名），回收评分文件。
7. **无内容 attestation**与命令文档（`scripts/README.md`）。
8. **示例敏感度实验**（D1 选项 c）：设计见 7.3。

---

### 7.3 示例敏感度实验（P2 内，决定 P1 是否立项）

**为什么上一版的设计会得出假阴性**：

- 合同判据（引用合法、grounded 一致、语言等）按设计就不受示例影响；拿它们当决策指标，必然测出「没有差别」。
- 上一版的三组里没有「示例贴合本库」的一组。`reflect_t0` 的语料都是 ML 论文，而默认示例是电路和 EDA，
  所以在这些语料上「默认」组本身就是他领域示例。
- 只测了合成，没有测 F1/F4 规划槽位对检索金标命中的影响。

**四组示例**：

| 组 | 内容 |
|---|---|
| G0 默认 | 现有默认示例 |
| G1 通用 | 所有字段取通用态（3.1） |
| G2 贴合 | 由人为每套语料各写一份定制字段，例如 ML 论文的机制层次写成 `architecture -> training objective -> optimization -> scaling behavior -> deployment` |
| G3 他领域 | 刻意不贴合的领域，例如法律或生物 |

在 `questions.yaml`（CMOS 教材）上，G0 本身就相当于「贴合」，G2 不必另写，用 G0 代替。在 `reflect_t0` 语料 B 上，
F4 的默认例子（DeepSeek-V2）也是贴合的；这一点在分析时单独标出。

**题集**：`reflect_t0` 30 题（语料 A 与 B 各 15 题），加 `questions.yaml` 中 L2/L3 的 10 题，共 40 题。全部属于开发集，
本实验不评估 hold-out。

**决策指标（二选一成立即可）**：

1. **盲评成对胜负**：在冻结合成清单上（7.2 第 4 项）比较 G2 对 G0、G1 对 G0，各 30 对，共约 60 对，由一名评审
   （用户）随机左右、隐藏组名作答，人工约 2 小时。只看非平局对，用精确符号检验。30 对中若约 20 对非平局，
   单侧 α=0.05 需要 15 胜以上（即 75%）。
2. **规划在环的金标命中**：G0、G1、G2、G3 各跑规划在环模式 3 次，每题取多数结果，看 `gold_section_path` 是否
   命中，成对比较 G2 对 G0。门槛是 b−c ≥ 3 且 c ≤ 1，并且符号检验 p ≤ 0.05。

**合同判据只当护栏**：任何一组的合同违反数在成对比较中增加，都要单独报告；但它不参与「示例有没有用」的判断。

**结论怎么用**：

- G2 在任一决策指标上显著优于 G0：示例确实有影响，P1 值得立项。
- 两项都不显著：P1 降优先级，并把结论记为「未检出」而不是「无影响」（功效有限）。
- G3 显著劣于 G1（他领域示例有害）：即使不做 P1，也值得单独开一个 PR，把默认示例改为通用态。这对所有
  非电路领域的库都是收益。

## 8. 分期

每期独立交付，不留半截。下一期是否立项，由前一期的结果和用户拍板决定。

| 期 | 内容 | 依赖 | 交付即可用的东西 |
|---|---|---|---|
| **P2 最小评测台** | 7.2 全部工作项 | 无 | 任何 prompt 改动都能过冒烟门；三条 A/B 待办可以推进；得到示例敏感度结论 |
| **P1 按笔记本定制** | 片段重切与五个框定片段（零回退证明）、7 个字段、三态、校验（整类拒收不可见字符 + NFKC 后匹配结构记号）、版本表（PG 迁移 + SQLite 镜像）、作业钉版本与保留策略、解析器与上下文管理器、池守卫、总开关、轨迹 digest、API、设置卡（只读视图、试答对照、护栏）、答案旁标注或通知（按 D11）、导出格式 v2、复制/删除/账号合并登记、日志、文档（产品合同中英成对、ui-vocabulary）、修正 1.8 的漂移与守卫缺口 | 推荐在 P2 的敏感度结论之后 | owner 能为库设定领域示例；成员能看到并被告知 |
| **P3 A 类离线优化** | 只做 `decomposition_guidance`；优化脚本、候选约束、hold-out 隔离、冒烟门、用户库复核 | P2 | 部署级规划默认值的有据改进，或「当前默认在本门下没有可检出提升」的结论 |
| **P4 B 类示例建议** | 建议工作负载与模型绑定、建议表、反照抄、逐条采纳/驳回/退役 | P1 | 新领域的库一键得到可审阅的建议 |

原 P5（护栏）已并入 P1：P1 的安全论证用到了护栏，所以护栏必须随 P1 一起交付。

---

## 9. 不做清单

- 不做整段自由覆盖 L1 片段；不开放 `answer.style_language`；F6 不能替换通用视角。
- 不做按用户的槽位定制（按用户的措辞偏好已有风格档案）。
- 不做按库在线 bandit、在线自动晋升、以 LLM 打分为优化目标。
- 不做运行时部署级 prompt 覆盖表；A 类产物只以代码 PR 落地。
- 全局问答不用任何库的定制。
- 不提供 MCP 写定制的工具。
- 不用生产问答内容做优化或评测数据（用户库复核用的是用户自己写的题，且不入库）。
- 不做「库名检测」类校验（红线 4）。

---

## 10. 需要用户拍板的决策点

### 10.1 现在拍（只决定 P2 的范围）

**裁决（用户，2026-10-28）：D1=(c) P2 评测台先行；D17=(a)；D18=(a)。**

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| D1 | 立项范围 | (a) P1–P4 全做；(b) P1 与 P2 并行，self-evo 等 P2 出基线后再定；(c) **P2 先行，用 7.3 测示例敏感度，再决定 P1**；(d) 只做 P2；(e) 只做 P1，永不做 self-evo；(f) 都不做，关闭待办 | (c)。P2 本身能推进三条现有待办；7.3 能回答「换示例有没有用」，顺带回答「他领域示例有没有害」。P3 预期收益低（5.3），P4 取决于 P1 |
| D17 | LLM 评委的地位 | (a) **只做去锚定的回归探测，不做门**；(b) 完全不用；(c) 作为门之一 | (a) |
| D18 | 评测台跑在哪个后端 | (a) **PG**（规范环境，与生产同款）；(b) SQLite；(c) 两者都跑 | (a)；SQLite 只用于测试 runner 本身 |

### 10.2 P1 / P3 立项时再定（当前推荐仅供参考）

下面 18 条只在 P1 或 P3 立项时才需要决定。当前的推荐可能随 7.3 的结论变化。

**裁决（用户，2026-10-28）：D2–D16、D19–D21 暂不裁决，P1 / P3 立项时再定。**

| # | 问题 | 选项 | 当前推荐 |
|---|---|---|---|
| D2 | 定制形态 | (a) **字段化示例槽位**；(b) 代码内置的领域预设（只能选）；(c) 两者都有；(d) 整段自由改写 | (a)。(b) 零注入风险，但每加一个领域都要发版；(d) 会让 grounding 条款失守 |
| D3 | 开放哪些字段 | (a) **F1–F7 全部**（F6 只能追加）；(b) 只开答案侧 F1–F3；(c) 只开报告侧 F6–F7 | (a)；如果 7.3 只在规划指标上显著，可以收窄到 F1/F4 |
| D4 | 谁能改 | (a) **仅 owner**；(b) admin（owner 加组管理员）；(c) 所有成员 | (a)。定制影响所有成员，也影响 owner 档的报告（`deps.py:492`） |
| D5 | 若 D4 选 admin：报告侧怎么办 | (a) F6、F7 归 owner，其余归 admin，**F2 整体归 owner**（F2 一个字段同时填答案槽位和报告槽位，无法拆开）；(b) 全部归 admin，接受「能改报告写法却不能写报告」 | D4 选 (a) 时不需要决定；否则选 (a) |
| D6 | 全局问答 | (a) **一律默认**；(b) 只有一个参与库时用它的定制；(c) 用发起时所在库的定制 | (a) |
| D7 | 导出与复制 | (a) **带最新一版**：导出是独立的 json 条目，有定制才写格式 v2；复制是闭包之外的单独一步；导入时都重新校验；(b) 不带 | (a) |
| D8 | 增量同步镜像 | (a) **目标端自有、不同步、镜像可改**（sync manifest 登记为 LOCAL）；(b) 随同步走、镜像只读 | (a)，与理解底座一致 |
| D9 | MCP | (a) **MCP 提问应用定制，不提供读写工具**；(b) 另加只读工具；(c) MCP 提问不应用定制 | (a) |
| D10 | 对成员透明 | (a) **有读权即可只读查看当前定制**；(b) 只有 owner 能看 | (a) |
| D11 | 答案旁标注与变更通知 | (a) **答案旁标「本库启用了定制示例」，可点开看**；(b) 定制变更时给成员发通知；(c) 两者都做；(d) 都不做 | (a)。代价写实：现在**没有按成员推送的通知存储**（铃铛只基于全局问答作业），选 (b) 或 (c) 就等于新建一套成员通知子系统（存储、未读状态、投递、界面），工作量与 P1 同一量级。(a) 只读当前版本就能渲染 |
| D12 | 首次保存前是否必须试答 | (a) **首次保存前至少跑一次试答**；(b) 不强制 | (a)。试答能覆盖答案槽位，选 `include_planning` 时还能覆盖规划槽位；**覆盖不到 F6、F7 和 F2 的报告槽位**（3.12），这几个只改报告字段的首次保存不设这道门 |
| D13 | 试答对照 | (a) **随 P1 做，边界见 3.12**；(b) 不做 | (a)。D12 依赖它 |
| D14 | 护栏越线时 | (a) **只提示，由 owner 决定是否恢复**；(b) 自动恢复上一版 | (a) |
| D15 | A 类产物如何落地 | (a) **代码 PR 改默认**；(b) 运行时部署级覆盖表 + 后台审批 | (a) |
| D16 | A 类的分布偏差 | (a) **只做 `decomposition_guidance`，以用户库复核（含至少一个已定制的库）作为合入门**；(b) 只收窄；(c) 只复核 | (a)，同时接受预期收益低 |
| D19 | 版本保留 | (a) **每库最近 20 版，跳过在途作业引用的版本**；(b) 全部保留；(c) 最近 5 版 | (a) |
| D20 | 部署级总开关 | (a) **`PROMPT_CUSTOMIZATION_ENABLED` 默认开，关闭即逐字节回到默认**；(b) 默认关；(c) 不设开关 | (a)。只有 owner 写了定制才会生效，默认开没有行为变化 |
| D21 | 字段允许的语言 | (a) **中英文都允许**（匹配前做 NFKC）；(b) 只允许英文（prompt 骨架是英文） | (a) |

---

## 11. 风险与开放问题

- **示例敏感度未知**：如果 7.3 的实验在两个决策指标上都检不出「贴合语料的定制」优于默认，P1 与 P4 的价值
  都要重估。这是推荐 D1(c) 的原因。但功效有限，检不出不等于没有影响（7.3 写了阈值）。
- **默认值演进与存量定制**：字段闭集只增不改名；删除字段时读取侧丢弃，卡上标「已失效」，同一个 PR 写迁移说明。
  A 类 PR 不得改动槽位登记（5.4）。
- **冒烟门功效低**：20 题 hold-out 只能检出约 25% 的翻转；扩大 hold-out 是唯一的根本办法，代价是标注工作量。
- **跨工具语料缺口**：`cross_tool_mapping` 长期不做 A 类优化。
- **评测台维护成本**：套件要跟着检索和 prompt 演进更新，放在每日扩展门之外按需运行。

---

## 附录：评审处理记录（opus 第一轮）

| 编号 | 意见 | 核实 | 处理 |
|---|---|---|---|
| P1-1 | 框定句与「`prompts.py` 零改动」矛盾；枚举编号会给锁定条款降权 | 成立：规则 13 之后是 `prompts.py` 的内联文本 | 3.4 框定槽位，一次性插入，句子不枚举编号；红线 2 改写 |
| P1-2 | 「一次运行一个版本」在多请求流程中做不到；保留策略可能删掉在途版本 | 成立：`ask_routes.py:662`/`:1009`、`report_routes.py:223/286/660` | 3.8 作业行钉版本、保留策略跳过在途引用、意图预览与合成允许跨版本并说明理由 |
| P1-3 | Unicode 防线有洞 | 成立：`reflect_action.py:214` 只覆盖 C0、C1、U+2028/2029、部分双向控制 | 3.6 NFKC + 整类拒收 + 归一化后匹配 + 存归一化文本；6.1 补行 |
| P1-4 | 漏了 `backend/app/eval/` 的资产 | 成立 | 1.4 逐项列出与处置；7.2 写明复用、重写与种子来源 |
| P1-5 | 三态未定义 | 成立 | 3.3 |
| P1-6 | 「定制态核心」掏空了「只换例子」 | 成立 | 3.1 槽位包连接词、核心三态同源、逐片段列出各态；5.4 约束 A 类 |
| P2-1 | 统计判据 | 成立 | 5.6 改为精确符号检验并写明最小可检出效应，定位为冒烟门；probe-floor 只进开发集 |
| P2-2 | A 类分布偏差 | 成立 | 5.3、D16：两条都做 |
| P2-3 | P2/P3 工程量 | 成立：`replay_retrieval.py:53`、`:126-134` | 7.2 显式工作项与 PG 跑法 |
| P2-4 | ContextVar 安装方式 | 成立：`global_ask.py:711`、`model_scheduler.py:73` | 3.9 上下文管理器、AST 守卫、池复用测试 |
| P2-5 | 多人库的告知与同意 | 成立 | 删除「由作者承担」；新增 D11、D12；D4 改为推荐 owner |
| P2-6 | 红线 4 没有可执行的校验 | 成立 | 红线 4 改成「不改变权限，权限在检索层」，并说明为什么不做库名检测 |
| P2-7 | 护栏与试答的归属 | 成立 | P5 并入 P1；3.12 写明试答边界 |
| P2-8 | 补决策点 | 成立 | 新增 D1(c)、D5、D18、D20、D21 |
| P2-9 | B 类建议提升不可信内容 | 成立 | 5.5 第 3 条 |
| P3-1 | 前缀缓存措辞 | 成立，原文「只有规则 1–7 能共享」有误 | 5.7 改正 |
| P3-2 | 上限计数口径 | 成立 | 3.2 计数口径三条 |
| P3-3 | 拒收清单误杀 | 成立 | 3.6 放行单独花括号与句中数字点，说明主防线是单行化 |
| P3-4 | F6/F7 清空态 | 成立：storm 大纲规则 2 引用 skeptic；`frame_example` 的形状契约 | F6 改为只能追加；F7 通用态为代码固定文本；3.1 逐片段列出 |
| P3-5 | notebooks 行版本列 | 采纳为可选 | 3.9；补充说明该列须排除出复制与同步闭包 |
| P3-6 | 导出格式版本 | 成立：`notebook_archive.py:47-48` | 3.11：有定制才写 v2 |
| P3-7 | self-evo 表述直说 | 成立 | 0、4、5.1 |
| P3-8 | token 预算有界 | 成立 | 3.2 第 3 条 |

对 P1-6 第 4 点（「A 类候选必须保持槽位登记和各态核心不变」）的解读：A 类本来就是要改**核心指令**，所以这里
理解为两条：一是槽位登记（id、位置、连接词、三态模板）不变；二是候选的核心在三态下逐字节同源。锁定核心子串
如果需要调整，必须由人在 PR 里显式修改。见 5.4 第 3 条。

## 附录：评审处理记录（opus 复审）

| 编号 | 意见 | 核实 | 处理 |
|---|---|---|---|
| R2-P1 | 示例敏感度实验的判据会得出假阴性 | 成立：合同判据按设计不受示例影响；`reflect_t0` 全是 ML 论文，对它来说默认示例就是他领域；规划槽位没测 | 新增 7.3：四组（加入「贴合」）；决策指标改为盲评成对胜负与规划在环的金标命中，合同判据只当护栏；写明题量、人工量、阈值；`questions.yaml` 上用 G0 代替 G2；5.7 按 4 组重算 |
| R2-1 | 决策点拆成现在拍与立项时拍；写实 D11(b) 的代价 | 成立：铃铛只基于全局问答作业，没有按成员推送的通知存储 | 第 10 节拆成 10.1（D1、D17、D18）和 10.2（其余 18 条）；D11 改为推荐 (a)，写明 (b)/(c) 等于新建成员通知子系统 |
| R2-2 | 框定句的单一 id 与守卫冲突 | 成立：`test_prompt_layers.py:41-99`、`test_l1_fragment_count_is_nine` | 3.4 单独登记 `LockedFrame` 一类，按 prompt 分成五个 id，逐个写出锚点与结尾换行；report_section 的锚点在 `prompts.py:2058` 附近；按 prompt 判定是否追加；新增「未装变体时为空串」的测试 |
| R2-3 | 钉版本缺迁移清单、同步与影子 manifest、同步 `/ask`、轨迹记哪一版 | 成立：`shadow/manifest.py:252`（ask_jobs 19 列）、`:389`（reports 48 列）；`ask_routes.py:862` 不建 job | 3.8 加迁移清单 8 项；sync manifest 登记为 LOCAL；同步 `/ask` 在入口解析一次；轨迹只记合成用的那一版 |
| R2-4 | Unicode 细节 | 成立 | 3.6 改为：NFKC 只用于匹配、存原文；拒收模式也先归一化；`grounded` 与 `[k` 的模式收窄；显式列出 Zl/Zp；先拒收换行，再谈单行不变量；写明 emoji、软连字符、零宽空格被拒的代价；422 里点出码位 |
| R2-5 | 评测资产漏了三项 | 成立 | 1.4 补 `sa_calibration.py`、`ask_latency.py`、`reflect_t0/questions.json`；`reflect_t0` 已覆盖 probe-floor，作为第一套开发集 |
| R2-6 | F6 收到 cleared 时怎么处理 | 成立 | 拒收（422 `field_not_clearable`）：3.1、3.3 |
| R2-7 | `answer.domain_conventions` 切错 | 成立：槽位吞掉了「换算要写明」这条 L0 | 拆成 9a/9b 两个槽位，换算条款留在核心里 |
| R2-8 | A 类保留语法宿主；用户库复核要包含已定制的库 | 成立 | 3.1 登记语法宿主并加测试；5.4 第 3 条；5.3 第 2 条 |
| R2-9 | D5(a) 不成立；preview 归在哪个能力；D12 能覆盖哪些槽位 | 成立：F2 一个字段填两个槽位 | D5(a) 改为「F2 整体归 owner」；preview 需要写能力（3.7、3.12）；3.12 写明试答覆盖范围，D12 据此排除报告字段 |
| R2-10 | 复制闭包必须等于 `ARCHIVE_TABLES` | 成立：`test_notebook_archive_format.py:36-60` | 导出改为独立的 json 条目（有定制才写 v2，冻结测试改为 `{1: T, 2: T}`）；复制改为闭包之外的单独一步。说明：`agent_notebook_profile` 的先例是「整表不进闭包」，「复制后单独一步」是本设计新加的 |
| R2-11 | 行号与路径 | 成立 | `reports:write` 改为 `deps.py:492`；`search_profile.py` 与 `agent_profile_block.py` 写全路径 |
