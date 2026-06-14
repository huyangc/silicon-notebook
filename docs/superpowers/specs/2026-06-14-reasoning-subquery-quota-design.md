# Reasoning 子查询配额重排 设计

日期：2026-06-14
状态：已与用户对齐方案（方案①）与关键决策（加 config 开关、默认开）

## 1. 背景与根因

用户问「deepseekv3相比deepseekv2做了哪些优化？deepseek r1呢」（复合问题），轨迹和回答割裂：

- **轨迹**：agent 明明 expand 到了 DeepSeek-V3 的 MoE（step16）、Multi-token prediction（step18）、MLA（step28）等架构节点，`collected` 里有它们。
- **回答**：V3 vs V2 部分却说「笔记未包含具体信息」并走「（推断）」；R1 部分答得详细、有 `[k]` 引用。

诊断铁证（`ans-625d114d0e`）：

| 环节 | 事实 |
|---|---|
| 数据层 | 库里有 V3 论文、174 条含 `deepseek-v3` 的 claim（MoE/MTP/MLA 都在），数据不缺 |
| 轨迹层 | agent expand 到了 V3 架构节点，`collected` 含它们 |
| **排序层** | **最终 top-12 答题候选 12/12 全来自 `DeepSeek-R1` 论文，V3/V2 论文 0 条** ← 断点 |
| 回答层 | 答题 LLM 只拿到 R1 claim → V3 vs V2 缺料走推断；R1 料足答得好 |

**根因**：`reasoning_retrieval.run()` 末尾（约 295-298 行）用**整个复合问题串**对全库重打分、全局 `sort` 取 top-N。R1 论文的 claim 同时命中 `deepseek`+`v3`+`r1`（R1 基于 V3-base，论文大量提 V3），对整串综合匹配度最高，**独占 top-12**；V3 论文的 claim 只命中 `v3`、综合分更低被截断；agent expand 到的 V3 节点 relevance 是占位 0（`neighbors` 不打分），整串重打分也救不回。**plan 阶段拆出的子查询多样性、agent 深挖的候选，在最终单一排序时被信息量大的一方碾平。**

不是 reasoning 循环 bug——该轨迹 34 步正常收敛（熔断/软提示都正常工作）。

## 2. 方案选择

| 方案 | 评估 | 结论 |
|---|---|---|
| ② expand 节点加权 | 根因不是 expand vs 初检索（R1 节点也大量靠 expand）；统一加权 R1 一起加权，仍通吃；加权值难调易误伤 | 排除 |
| ③ 复合问题拆答 | 最彻底但要重构答题流程为 per-子问答+合并，多次答题 LLM 调用（慢+贵），改动面最大 | 过重，留作未来 |
| **① 子查询配额** | 直击根因；plan 已拆子查询，只是最终排序丢弃了它们；改动集中末尾、不碰答题流程；单查询自然退化兼容 | **选用** |

## 3. 详细设计

改动集中在 `backend/app/services/reasoning_retrieval.py` 的 `run()` 末尾 + 一个纯函数 helper，外加一个 config 开关。

### 3.1 config 开关

`backend/app/core/config.py` 新增：

```python
reasoning_quota_enabled: bool = Field(True, env="REASONING_QUOTA_ENABLED")
```

默认开。万一配额在某些问题上表现不如全局排序，可一键回退（`REASONING_QUOTA_ENABLED=false`）。

### 3.2 收集 used_queries

`run()` 内维护 `used_queries: List[str]`，保序去重：

- 初检索：`plan` 出的 `subqueries` 的每个 `sq.query`。
- `add_subquery` 分支：成功时 append `sq.query`。

去重用「保序去重」（首现保留），与现有 `collected` 去重风格一致。

### 3.3 配额重排 `_quota_rerank`

`ReasoningRetriever` 方法（经 `self.search` 调检索原语；离线单测用桩 LLM + FakeEmbedder 注入确定性检索结果，或 monkeypatch `search`）：

```
_quota_rerank(notebook_id, collected, used_queries, top_n) -> List[RetrievedKnowledge]
```

算法：

1. 对每个子查询 `q_i`：调 `self.search(notebook_id, q_i)`（即 `_retrieve_scored`）得全库打分，构 `{oid → relevance_i}`。单个子查询检索抛错 → 跳过该组（容错）。
2. `collected` 里每个候选，归到**它 relevance 最高的那个子查询组**（`argmax_i relevance_i`），组内分 = 该 max relevance；所有子查询里都查不到的候选（relevance 全 0）归入一个「兜底组」，分 0。
3. 各组内按 relevance 降序。
4. **round-robin**：按子查询顺序轮流从各组取队首一条，去重（已选过的 oid 跳过），直到凑满 `top_n` 或所有组取空。兜底组放在最后轮转。
5. 每个入选候选采用其所属子查询的打分版本（带 relevance，供 grounded 档位判断）。

### 3.4 run() 末尾接线

```
if settings.reasoning_quota_enabled and len(used_queries) >= 2:
    top_hits = self._quota_rerank(notebook_id, collected, used_queries, top_n)
else:
    # 现有全局重排（单查询/开关关 → 向后兼容，行为不变）
    scored_map = {h.object_id: h for h in self.repo._retrieve_scored(notebook_id, question)}
    top_hits = [scored_map.get(oid, rk) for oid, rk in collected.items()]
    top_hits.sort(key=lambda h: h.relevance, reverse=True)
    top_hits = top_hits[:top_n]
```

`top_hits` 仍只从 `collected`（agent 池）里选，不引入新候选源。

### 3.5 可观测

`answer` step 的 `detail` 增加 `quota`（每子查询贡献条数，如 `{"q0": 6, "q1": 6}`）便于前端展示与调试；非配额路径不带该字段。

## 4. 数据流

```
plan → subqueries → used_queries（初始）
reflect 循环 → add_subquery 成功 → used_queries.append
循环结束 → reasoning_quota_enabled 且 used_queries≥2 ?
  是 → _quota_rerank（per-子查询重打分 + round-robin 配额）
  否 → 全局重排（原行为）
→ top_hits → answer step（带 quota detail）
```

## 5. 错误处理

- `used_queries` 空或仅 1 个 → 走全局重排（不进配额）。
- 某子查询 `_retrieve_scored` 抛错 → 跳过该组，其余正常（沿用现有「单子查询失败被吞」风格）。
- 配额取空仍不足 `top_n` → 有多少给多少（不补全、不报错）。

## 6. 性能

末尾多 `len(used_queries) - 1` 次 `_retrieve_scored`。每 notebook 的 float32 向量矩阵已缓存，单次为一次 matmul（百 ms 级），子查询通常 2-5 个；reasoning 模式本就多轮检索，此开销可接受。

## 7. 测试（离线，桩 LLM + FakeEmbedder）

1. **配额生效**：复合 2 子查询，`collected` 含两类节点、两子查询各自全库 top 不同，验证 `top_hits` 同时含两组候选（不再一方通吃）。
2. **单查询退化**：`used_queries` 仅 1 个 → 走全局重排、行为不变。
3. **开关关退化**：`reasoning_quota_enabled=false` → 复合问题也走全局重排。
4. **子查询检索容错**：某子查询 `search` 抛错 → 不崩，其余组正常出候选。
5. **round-robin 均衡**：构造两组候选数悬殊（如 10 vs 2），验证 top_n 内两组都有名额、不被多的一方占满。
6. **可观测**：配额路径 `answer` step detail 带 `quota`，非配额路径不带。

测试隔离沿用 `rrepo` fixture（已清空 LLM/reasoning key，避免本地 .env 打真实网络）。

## 8. 明确不做（YAGNI）

- 不做复合问题拆答（方案③）——不重构答题流程、不引入多次答题 LLM 调用。
- 不改 plan/reflect 的 LLM 决策逻辑——只改最终排序。
- 不引入候选来源追踪（不改 `collected` 结构）——用 per-子查询重打分天然分组。
- 不动 fast/graph 模式——本改动仅作用于 `mode=reasoning` 的 `run()`。

## 9. 验证基线

- `scripts/check.sh` 全绿（py_compile + hermetic smoke + tsc）。
- `backend/tests/test_reasoning_retrieval.py` 全过（含新配额测试 + 现有不回归）。
- 生效需重启后端（逻辑改动，后端无 `--reload`）——交用户重启，不由我重启。
