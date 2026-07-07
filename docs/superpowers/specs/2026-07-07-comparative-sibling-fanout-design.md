# 对比题兄弟实体 fan-out 设计（ask + 深度报告）

- 日期：2026-07-07
- 状态：设计已确认（模型驱动工具版），待写实现计划
- 分支：`claude/cool-liskov-f77904`

## 1. 问题陈述（诊断结论）

在 DeepSeek-V4 notebook（`nb-a73f16940c`，tier=personal）里问「分析 deepseekv4 相比其他 llm 的优势是什么，还有什么可以提升的空间」，无论 ask 还是深度报告，都**带不出基准库里的「其他 llm」**。

现场取证（真实运行库 = 仓库根 `.local/silicon_notebook.db`）：

- 基准库 `nb-b37185f4ae`「LLM Structure & Infra」（tier=base）**并不空**：41,713 KG 对象 / 7,026 chunk / 84 篇论文，**满库都是其他 LLM**——Qwen 912、GLM 597、Llama 485、GPT 384、Mixtral/Gemini 各 151、Claude 136、Mistral/Jamba/Mamba/Gemma… 数十到数百个对象。
- 最新报告 `rep-3f34a06b36`（done，depth=16）实际做的 13 条子查询**条条锚 DeepSeek**；2 条引用全 personal、全出自 DeepSeek-V4 论文本身；base 引用 0 条。

**根因（一句话）**：管线里没有任何一步把「其他 llm」这个笼统词落地成语料库里真实存在的兄弟实体（Qwen/Llama/GPT…）。当前「对比检测」只存在于两处 LLM prompt 的自然语言提示（`expand_query_prompt` 的「For a COMPARISON, emit ONE sub-query per entity」、`reflect_prompt` 的 ppr 提示），**没有任何代码级检测、更没有兄弟实体枚举**——LLM 只知道被点名的 X，看不到 base 库里有哪些同类，于是「X vs 其他 Y」退化成「只问 X」。

四层断点：①规划（无兄弟落地，决定性）②联邦融合无 per-tier 配额（base 弱命中被挤掉）③大库 `_retrieve_scored` FTS=0 硬 `return []`（已缓解，索引今日已建）④跨文档 PPR `no_participants`（留后续）。本设计主攻 ①②，兼修 ③。

## 2. 设计取向：模型驱动的工具调用

不做旁路词法检测，而是**给本就在做判断的 LLM 一个"查同类实体"的能力**，由模型自己"发现对比意图 → 触发工具 → 拿到兄弟 → 用兄弟做检索"。系统只负责：把"同类实体"这个能力暴露给模型、在底层高效实现枚举与 fan-out、在融合层给 base 保底。

覆盖三个表面，各按其形态接入同一个底层原语 `enumerate_siblings`：

| 表面 | 有无 agent 循环 | 接入方式 |
|---|---|---|
| ask-reasoning | 有（reflect 循环） | 新增 reflect 动作 `find_siblings` |
| 深度报告逐节深挖 | 有（复用 ReasoningRetriever.run） | **同一个** `find_siblings` 动作，白拿 |
| ask-chunk | 无（单发 expand→检索→答） | `expand_query` schema 加可选 `comparison` 字段 |

## 3. 效率预算（硬约束，逐项）

| 步骤 | 成本 | 说明 |
|---|---|---|
| 暴露工具/字段 | ~0 | reflect 多 1 个动作、expand 多 1 个可选字段，仅 prompt token 边际增量 |
| 触发判定 | 0 额外调用 | 复用现有 reflect / expand 的那次 LLM 调用，模型顺带产出 |
| 枚举 `enumerate_siblings` | 1×embed(entity 串) + 1×ANN + ≤1×有界聚合 | ANN 打 base 库既有 `ann.bin`；频次兜底是有界 GROUP BY；结果请求内缓存 |
| fan-out（选 B） | K 次检索 pass | K≤`COMPARATIVE_FANOUT_TOPK`(默认 8)，一个动作搞定，不占焦点子查询预算 |
| 联邦配额 + 语义保底 | ~0 | 纯合并策略 / 已加载矩阵，无新增调用 |

**总门控**：`COMPARATIVE_FANOUT_ENABLED` 默认开，关掉即不暴露工具/字段。所有枚举与 fan-out **只在模型真触发时发生**——非对比题模型不会触发，零额外开销；比纯词法检测更省（无误报枚举）。

## 4. 组件设计

### 4.1 底层原语 `backend/app/services/comparative.py`（新文件，无 LLM）

单一职责、可独立测试，被 reasoning、chunk、（间接）报告三处消费；**刻意不往 God 对象 `sqlite_repository.py` 堆**（对齐架构评审「沿接缝抽取」）。

```python
@dataclass
class Sibling:
    canonical_name: str
    object_id: str
    tier: str            # 恒 "base"
    sim: float           # ANN 相似度（频次来源则 0）
    freq: int            # base 库该 canonical 的对象数
    source: str          # "ann" | "freq"

def first_base_notebook_id(repo, active_nb: str) -> str | None
    # SELECT id FROM notebooks WHERE tier='base' AND id != active LIMIT 1（复用 federated 同款查询）

def enumerate_siblings(repo, active_nb: str, focal_entity: str, *,
                       kind: str = "", k: int, min_siblings: int) -> list[Sibling]
```

`enumerate_siblings` 实现：
1. `base_nb = first_base_notebook_id(...)`；无 base → `[]`。
2. `focal_vector = repo._embed_query(f"{focal_entity} {kind}".strip())`（1 次 embed）。
3. **主路 ANN**：`idx = repo._scale_index(base_nb, allow_stale=True)`；有 `ann_labels` 则 `repo._kg_object_candidates(base_nb, focal_vector, idx, k*4)` 取候选 object_id+sim；映射 canonical 名（payload.name → 经 `repo.cluster_map(base_nb)` 折叠到 canonical）。
4. **排除 focal 同簇**：用 `cluster_map` 把 focal_entity 归一化，丢弃同簇成员（DeepSeek-V2/V3/R1 不算兄弟）。
5. **频次兜底**：ANN 去重后不足 `min_siblings`（或 idx 不可用）→ 有界 `SELECT json_extract(payload,'$.name') nm, COUNT(*) c FROM knowledge_objects WHERE notebook_id=? AND object_type='concept' GROUP BY lower(nm) ORDER BY c DESC LIMIT ?`，过滤实体型名、排除同簇，补足到 K。
6. **合并排序**：`score = w_ann*norm(sim) + w_freq*norm(log1p(freq))`；按 canonical 去重；截断 K。
7. 全程 fail-open：base 索引缺失/维度失配/空结果 → 返回已得或 `[]`，绝不抛错打断主检索。

### 4.2 reflect 动作 `find_siblings`（reasoning + 报告，选 B：一动作即 fan-out）

**prompts.py `reflect_prompt` + `REFLECT_SCHEMA_HINT`**：动作集加 `find_siblings`——"当且仅当问题是把某实体与其同类横向对比、而你手上缺少同类实体时，用它；给出 `sibling_entity`（被对比的焦点，如 DeepSeek-V4）与可选 `sibling_kind`（同类类别，如 LLM/model）"。schema 加 `"sibling_entity":"","sibling_kind":""`。

**reasoning_retrieval.py `reflect()`**：解析 `next_action=="find_siblings"`，读 `sibling_entity`/`sibling_kind` 写入 `ReflectDecision`（新增两字段）。

**reasoning_retrieval.py `run()`**：新增动作分支（镜像现有 `ppr_retrieve` 分支）：
1. `sibs = enumerate_siblings(repo, notebook_id, decision.sibling_entity, kind=decision.sibling_kind, k=settings.comparative_fanout_topk, min_siblings=settings.comparative_fanout_min)`。
2. 对每个 sib 生成子查询 `f"{sib.canonical_name} {angle}"`（`angle` = 原 question 的规范化核心，复用已有 query，不再调 LLM），并发 `search`（同 `_run_search`）拿命中，`setdefault` 折进 `collected`，记 `attempted`（防重）。
3. `record(TraceStep(step_type="find_siblings", summary=f"横向对比:纳入 {len(sibs)} 个同类({', '.join 名称}),新增候选 N", detail={...}))`。
4. 兄弟名 + 命中数进入下一轮 reflect 的 summary（模型看得见铺开了谁、有没有货，可继续或收敛）。
5. 触发去重：同一 run 内 `sibling_entity` 已 fan-out 过则跳过（防反复触发）。

**一处覆盖两路**：ask-reasoning 直接得到该动作；深度报告每节 `_deep_dive`→`ReasoningRetriever.run` 同样得到——无需报告侧额外代码。

### 4.3 chunk 模式 `comparison` 字段（无循环表面）

**prompts.py `expand_query_prompt` + `EXPAND_SCHEMA_HINT`**：schema 加可选 `"comparison":{"entity":"","kind":""}`——"若问题在把某实体与其同类横向对比，填 entity=焦点、kind=同类类别；否则省略"。

**query_rewrite.py `ExpandedQuery`**：加字段 `comparison: Optional[dict] = None`；`expand_query()` 解析该字段（缺省 None）。

**sqlite_repository.py `ask_chunk`（expand 调用点 ~10988）**：拿到 `ex` 后，若 `ex.comparison` 且 `comparative_fanout_enabled` → `enumerate_siblings(...)` → 把 `f"{sib} {angle}"` 兄弟子查询**追加**进该路的子查询集（走独立预算，不挤原子查询），再照常检索。

### 4.4 深度报告 STORM 规划（保留「横向对比」节，模型驱动）

**prompts.py `report_storm_outline_prompt`**：加一句——"若问题是把某实体与其同类横向对比，规划一节专门横向对比（其 sub_queries 面向同类实体的对应维度）"。规划器照旧产出「横向对比」节（title+sub_queries），**真正的兄弟落地在该节深挖时由 `find_siblings` 动作完成**（4.2）。规划期不做枚举（零额外调用）。

### 4.5 联邦融合：per-tier 保底 + 语义保底

- `retrieval.py` 新增 `apply_tier_floor(hits, ratio, window)`：在前 `window`（默认 24）个位置内，保证 base tier ≥ `ceil(ratio*window)` 席位（不足则从窗外把最佳 base 命中提上来），其余按相关度；`ratio=FEDERATED_BASE_QUOTA`（默认 0.3）。稳定、有界、可测。
- `sqlite_repository.py `federated_retrieve`（排序在 `:10478`）：纯相关度排序后调 `apply_tier_floor`，使下游任意 `[:top_n]` 切片都含 base 保底。
- `sqlite_repository.py `_retrieve_scored`（`:10343` 大库 FTS=0 分支）：加**语义保底**——若 `idx` 可用，退而取其 top-K 最近邻（即便低于阈值）作候选，而非硬 `return []`；仍拿不到才 `[]`。`FEDERATED_SEMANTIC_FLOOR_ENABLED` 默认开。

### 4.6 前端（同 PR，co-design）

**来源分布可见性**（读现有 `references_json` 的 `tier` 字段，零后端新增）：
- 深度报告整体 + 每节显示 `active N · base M` 徽章。
- ask 答案区小提示「base 命中 M」（读检索结果 tier 标记）。

目的：把「base 有没有被带出」从隐性失败变成一眼可见，便于验证特性生效。

### 4.7 配置项（pydantic `validation_alias`，避坑）

| flag | 默认 | 作用 |
|---|---|---|
| `COMPARATIVE_FANOUT_ENABLED` | `true` | 总开关：是否暴露 `find_siblings` 动作 / `comparison` 字段 |
| `COMPARATIVE_FANOUT_TOPK` | `8` | 每次 fan-out 的兄弟数 K |
| `COMPARATIVE_FANOUT_MIN` | `3` | 触发频次兜底的阈值 `min_siblings` |
| `FEDERATED_BASE_QUOTA` | `0.3` | tier 保底比例：前 window 内 base 席位 = ⌈0.3·window⌉ |
| `FEDERATED_QUOTA_WINDOW` | `24` | 保底作用窗口大小 |
| `FEDERATED_SEMANTIC_FLOOR_ENABLED` | `true` | 大库 FTS=0 时语义保底 |

（config.py 是 pydantic-settings v2；新增项 env 映射用 `validation_alias`，Settings 构造用 alias 名。）

## 5. 数据流

```
                          模型(reflect / expand)自己判定"这是对比、我缺同类"
                                          │ 触发
      reasoning/报告 ── reflect: find_siblings{entity,kind} ─┐
      chunk ─────────── expand: comparison{entity,kind} ─────┤
                                                             ▼
                          enumerate_siblings(base_nb, entity, kind, K)
                          = embed(entity) → base ann.bin ANN  (+频次兜底, 排除同簇)
                                                             │ [Sibling...]
                                          ┌──────────────────┴───────────────────┐
                                          ▼ 选B: 一动作即 fan-out                 ▼ chunk
                        对每个兄弟跑 "sibling angle" 子查询             追加兄弟子查询
                        → federated_retrieve(每次) → 命中折进候选
                                                             │
                                    apply_tier_floor(base 保底) + 语义保底
                                                             ▼
                          答案/报告引用出现 tier=base 的兄弟实体 + 来源分布徽章
```

## 6. 边界与错误处理

- 无 base 库 / base 无索引 / 枚举空 → `enumerate_siblings` 返回 `[]`，动作/字段成为 no-op，主检索照常。
- 模型不触发（非对比题）→ 零枚举、零 fan-out。
- 兄弟去重必须排除 focal 同簇，避免自我「对比」。
- K 封顶 + 同一 run 内同一 focal 只 fan-out 一次，防检索 pass 爆炸 / 反复触发。
- ANN 维度失配 / 索引打不开 → fail-open 走频次兜底或空，绝不让主检索失败。
- reflect 动作集扩容后，未知/异常 action 仍回退 `answer`（现有容错不变）。

## 7. 测试策略（TDD）

- **enumerate_siblings**：ANN 命中→canonical 去重；ANN 不足→触发频次兜底；排除 focal 同簇（DeepSeek 家族不入）；K 截断；无 base / 索引缺失 fail-open 返回 `[]`。
- **reflect() 解析**：`find_siblings` 动作 + `sibling_entity/kind` 正确解析；缺字段回退 `answer`。
- **run() find_siblings 分支**：给定 stub 的 `enumerate_siblings`，断言按兄弟发子查询、命中折进 collected、记 TraceStep、同一 focal 二次触发被跳过。
- **expand_query comparison 字段**：LLM 返回含 comparison → 解析到 `ExpandedQuery.comparison`；不含 → None；坏 JSON 回退。
- **apply_tier_floor**：base 保底席位被尊重、不足从窗外提升、可得<配额时不越界、纯 active 时不崩。
- **语义保底**：大库 FTS=0 且有 idx → 返回 top-ANN；无 idx → 仍 `[]`。
- **集成**：active(焦点)+base(若干兄弟) 小型 fixture，reasoning 走 stub-LLM 触发 find_siblings，断言检索结果出现 `tier=base` 兄弟；非对比题不触发（零 fan-out）。

## 8. 涉及文件

- 新增：`backend/app/services/comparative.py`（+ `backend/tests/test_comparative.py`）。
- 改：`backend/app/services/prompts.py`（reflect 动作、expand comparison 字段、storm 节提示 + 三个 SCHEMA_HINT）。
- 改：`backend/app/services/reasoning_retrieval.py`（`reflect()` 解析、`ReflectDecision` 两字段、`run()` find_siblings 分支）。
- 改：`backend/app/services/query_rewrite.py`（`ExpandedQuery.comparison` + 解析）。
- 改：`backend/app/services/sqlite_repository.py`（`ask_chunk` 消费 comparison、`federated_retrieve` 调 `apply_tier_floor`、`_retrieve_scored` 语义保底）。
- 改：`backend/app/services/retrieval.py`（新增 `apply_tier_floor`）。
- 改：`backend/app/core/config.py`（6 个 flag，`validation_alias`）。
- 前端：报告与 ask 视图的来源分布徽章（读现有 `tier` 字段）。
- 文档：README / README_zh 若涉及新 env 则补（通用口径）。

## 9. 已确认决策

- 范围：ask + 报告一起。
- 检测：**模型驱动**（reflect 动作 / expand 字段），非旁路词法。
- 兄弟枚举：ANN 主 + 名称频次兜底。
- fan-out：**选 B**——`find_siblings` 一个动作即返回清单并直接铺开 top-K 兄弟子查询检索。
- 报告结构：保留自动「横向对比」节（STORM 规划产出，深挖时由动作落地）。
- 默认：`COMPARATIVE_FANOUT_ENABLED` 开、K=8、排除 focal 同簇、兄弟子查询独立预算、base 保底比例 0.3。

## 10. 后续（本 PR 外）

- 第 ④ 层：跨 tier 兄弟同义边 + PPR 种子纳入 base 命中，让 graph-walk 能焦点→兄弟。
- `sibling_kind` 可进一步用于按 KG 类型/簇过滤，提升兄弟精度。
