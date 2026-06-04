# KG 问答重设计：可推演 + 逐句引用 + 多轮对话 — 设计文档（spec）

- 日期：2026-06-04
- 状态：待用户 review（用户要求 review spec+plan 后再开发）
- 背景与现状根因见 `docs/superpowers/specs/2026-06-03-kg-usage-current-state.md`（§6 死路根因、§7 期望形态、§8 前端/配置需求）。本 spec 把那次讨论固化为可执行设计。
- 前置已完成：embedder 上线（dashscope text-embedding-v4，URL）、batch 上限 bug 修复 → 语义+跨语种检索已可用；本地模型已移除（模型一律走 URL）。

## 0. 目标（一句话）
把 `/ask` 从「检索到就只复述、检索不到就 canned 拒答」改成：**先尽量用笔记本知识接地，再在其上向前推演，逐句标注出处；有出处的句子可点开看证据，没出处的部分明确是模型推断；整段问答在多轮对话框里进行。**

## 1. 用户故事（验收锚点）
- U1「engram 是什么，有哪些优点和问题，怎么改进」→ 一段答案：是什么/优点 句子带 `[id]` 引用；缺点若文中有则引用、若无则模型客观给出并标注「（推断，非来源）」；改进建议为模型推演、不带引用、显式标注。
- U2 点击答案里的 `[engram]` → 弹出该节点的完整信息（定义/所在原文句/出处文档+位置）。
- U3 笔记本里没有相关内容时问问题 → 模型用通用知识回答，整体标注「未基于本笔记本来源」，不再 canned 拒答。
- U4 追问「第 2 点怎么落地」→ 对话框保留上文，回答延续上一轮的概念与引用。

## 2. 核心机制：逐句 provenance（让"推演"安全的关键）
一条答案 = 若干句子，每句要么 **grounded**（用了某检索项）要么 **inferred**（模型自己的推断）。
- 给答题 LLM 的每个检索项一个**稳定短 id**（如 `k1,k2,…`，复用抽取期"回传 id 标号"的范式）。
- 指示 LLM：**用到某项就在该句末写 `[k_i]`**（可多个）；**纯推演的句子不写 id**，且在该处用自然语言点明是建议/推断。
- 后端把答案文本里的 `[k_i]` 解析成 **citation 锚点**：前端渲染为可点的短 token（显示用简短 KG 名/原文词），点击弹出该 id 对应的完整 payload（节点名+类型+定义+所在原文句+来源文档/位置）。
- 这样把旧版「grounded/partial/ungrounded 三态」收敛成**同一段答案内逐句标注的连续谱**，天然满足 U1–U3。

**Schema（答题 LLM 输出）**：`{"answer":"...带 [k_i] 标记的正文...","grounded":true|false}`。后端解析 `[k_i]` → 拼 `citations`（id→证据），`answer` 原样回前端渲染。

## 3. 范围与分期（每期独立可测、可验收）
> 建议分 4 期；Phase 1 是产品价值主体，Phase 3/4 可后续。各期之间是「先后」不是「强耦合」。

### Phase 0 — 检索召回收敛（小改，做 Phase 1 的地基）
现状（§4）：逐类型固定 top-K（5/5/4/4）、`keyword_score` 用「整条 query 的命中比例」（长问句被稀释）、scenario 仍拼进 query、`_TYPE_WEIGHT` 实际无效。embedder 上线后语义已能兜底，但仍建议：
- **去掉 scenario 入参**：`query = question`，删 `structured_boost`（§4-11）。
- **关键词指标改为 query 召回不受问句长度惩罚**：改成「命中 token 数 / 该对象 token 数」式或直接弱化关键词权重（语义为主），并把 floor 改成「关键词-only 时才用低 floor」。（细节待定，见决策 D2）
- **top-K**：单类型固定 → 全局统一排序取 top-N（type 作软先验，真正用上 `_TYPE_WEIGHT`），或保留逐类型但 K 自适应（决策 D3）。
- 产出：检索更稳，U1 这类长/跨语种问句稳定召回。**含离线小测 + 真机抽样。**

### Phase 1 — 答案合成重设计（产品主体，后端）
改 `ask()` 的合成段（`_answer_with_llm_kg` + `answer_prompt`）：
1. **门控放开**：不再要求"必须有命中才调用 LLM"。无命中 → 走 ungrounded 模式（U3）。
2. **喂富上下文**：把检索项的 `node_context`（定义 / 所在句 / procedure 有序步骤）一并给 LLM（现在只给 name+payload，§9）。每项带稳定 id。
3. **新 prompt（可推演 + 逐句引用）**：要求 grounded 句尾 `[k_i]`、inferred 句不带 id 且自陈是推断；并按 D1 的推演策略约束尺度。
4. **解析 + citations**：后端把 `[k_i]` 映射成 citation（id→节点/证据 payload），保留旧 `citations`（element 级）作兜底。
5. **AskResponse 扩展**：`answer`(带标记正文)、`grounded`(bool)、`citations`(含 id→popover payload)、`related_knowledge`(沿用)、`llm_mode`∈{grounded,ungrounded,deterministic}。向后兼容旧 `conclusion`（可由 answer 去标记得到）。
- 验收：U1、U3 成立；离线 mock 测 + 真机抽样核对引用正确性。

### Phase 2 — 前端逐句引用渲染（§8-1）
- 解析 `answer` 里的 `[k_i]`，渲染为可点短 token；点击弹出 `citations[k_i]` 的完整信息（复用 `/objects/{id}/context`/`concept_detail` 的富信息）。
- 验收：U2 成立。`tsc --noEmit` + 人工 eyeball。

### Phase 3 — 多轮对话（§8-2，后端+前端）
- **数据模型**：新增 conversation/thread（一个 notebook 下多个会话，每会话有序 turns：user 问 / assistant 答+citations）。`AskRequest` 增 `conversation_id?`；`ask` 读取该会话历史。
- **prompt 带历史**：把前 N 轮（问+答摘要+上一轮锚点）拼进上下文；本轮仍重新检索（history 影响表述，不替代检索）。
- **前端**：中间问答区改多轮对话框，发送后线程式追加；保留每轮引用可点。
- 验收：U4 成立。

## 4. 数据 / 接口变更（汇总）
- 无破坏性表变更（Phase 0–2）。Phase 3 新增 `conversations`/`conversation_turns` 表。
- API：`/ask` 入参去 scenario、加可选 `conversation_id`（Phase 3）；返回体加 `answer`/`grounded`/结构化 `citations`。
- 复用：`node_context`/`concept_detail`/`/objects/{id}/context` 作为 citation 弹窗数据源。

## 5. 测试
- Phase 0：检索打分单测（长问句不被稀释、scenario 去除后仍召回、全局排序/类型先验）。
- Phase 1：答题用 fake LLM（返回带 `[k_i]` 的文本）→ 测解析、citations 拼装、ungrounded 分支、富上下文注入；真机抽样核对「grounded 句确有出处、inferred 句无 id」。
- Phase 2：前端解析/渲染/弹窗（tsc + eyeball）。
- Phase 3：会话持久化 + 历史注入单测；多轮真机。

## 6. 决策（已确认 2026-06-04，采纳推荐）
- **D1 推演尺度 = 中间偏保守**：默认可向前推理，但推断句必须显式自陈「（推断）」，且**不得把推断写成像有出处**（不得给推断句加 `[k_i]`）。
- **D2 关键词指标 = 语义为主 + 停用词过滤**：embedder 已上线，关键词降权、加 en/zh 停用词过滤；关键词-only 时才用低 floor。
- **D3 top-K = 全局统一排序 top-N**：跨类型合并后按 `score × _TYPE_WEIGHT`(软先验) 全局排序取 top-N，不再逐类型凑配比。
- **D4 = 混合**：concept 检索结果按 unified 簇去重（跨文档同概念折叠到 canonical，保留多来源）；claim/formula/procedure 仍文档级。
- **D5 分期 = 本轮做 Phase 0+1+2**，Phase 3 多轮对话单独排期（plan 先覆盖 0/1/2）。

## 7. 非目标
- 不在本 spec 改抽取/KG 构建（已收敛）。
- 不做图的多跳推理/路径解释（§4-A 的更大方向，另议）。
- 不引入本地模型（模型一律 URL）。
- 不做检索的 FTS5/BM25 基础设施（低 ROI，见 fangan_todo P2）。
