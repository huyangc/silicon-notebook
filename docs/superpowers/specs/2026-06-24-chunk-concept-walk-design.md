# Spec:概念漫游(PPR)接入通用问答(chunk)+ 全局改名

- 日期:2026-06-24
- 状态:设计已批(用户「都在新 PR 上进行」),待实现计划
- 分支:`claude/chunk-concept-walk`(off origin/master,已含 #70 reasoning PPR)
- 关联记忆:`hipporag-ppr-plan`、`comparative-retrieval-collapse`、`chunk-native-retrieval-state`、`model-error-observability`

## 背景 / 动机

通用问答(默认 chunk 模式,`ask_chunk`)的「rich 路径」(`overlay_on` = `chunk_kg_overlay_enabled` + rerank 配齐 + 有 KG)走 `_mix_retrieve`:**向量 chunk** + **KG-overlay chunk** 并池 → **rerank** → token 预算 → `_answer_mix`。但 overlay 的 KG 走是 **1-hop 子图**(query 锚定种子)→ 与 reasoning 的 expand 同病、**无跨文档桥**,对比/跨文档题仍坍缩到单篇。

PPR(`_ppr_retrieve`,HippoRAG 式跨文档传播)已是**模式无关原语**(graph/reasoning 在用,master 已有)。本设计把它接入 chunk。

**关键有利点**:chunk 的 rich 路径**本来就 rerank**,跨文档拉进来的离题 chunk 会被 rerank 按 query 相关度自然压低——「跨文档扩散加噪」这个在 reasoning 需专门处理的问题,在 chunk **被现成 rerank 免费解决**。无 agentic 循环,只是**多接一路候选流**。

**命名**:用户拍板 PPR 面向 UI 叫「**概念漫游**」(Concept Walk,贴 PPR 在概念图上游走传播的机制)。本 PR 一并把已发的 reasoning 轨迹串改名。

## 目标

- chunk 模式获跨文档检索:PPR chunk 作 `_mix_retrieve` **第 3 路**,复用现成 rerank + `_answer_mix`。
- 用户可见命名统一为「概念漫游」(reasoning 轨迹现成串 + chunk 诊断标记)。
- **零影响** reasoning/graph 检索、chunk 朴素路径;**不新增开关**;守 `[0,1]`/tau。

## 非目标

- **不门控**(rerank 已免费控噪;对比意图检测器 = YAGNI,逃生口=真机若见噪再加)。
- 不改内部 `GRAPH_PPR_ENABLED` / `_ppr_*` 方法命名(仅改用户可见串)。
- 不动 `_answer_mix` / `_mmr_select_chunks` / `quota_fuse` 逻辑。
- chunk 模式**用户可见**的「概念漫游」徽章 = 前端后续(本 PR 只落后端诊断标记,不塞投机响应字段)。

## 设计

### A. 架构
PPR 跨文档 chunk 作 `_mix_retrieve` 的第 3 路候选,gated `GRAPH_PPR_ENABLED`,只在 overlay 路。复用现成 rerank → token 预算 → `_answer_mix` 全管线。

### B. `_mix_retrieve` 改动([sqlite_repository.py:5661](backend/app/services/sqlite_repository.py:5661))
当前:`vector_chunks` + overlay 的 `kg_chunks` 两路 round-robin。改为三路:

- overlay 块算完 `kg_chunks` 后,加:
  ```python
  ppr_chunks = []
  if self.settings.graph_ppr_enabled:
      ppr_chunks = self._ppr_retrieve(notebook_id, query)
  ```
- round-robin([:5673](backend/app/services/sqlite_repository.py:5673))三路化:
  ```python
  merged, seen = [], set()
  for i in range(max(len(vector_chunks), len(kg_chunks), len(ppr_chunks))):
      for src in (vector_chunks, kg_chunks, ppr_chunks):
          if i < len(src) and src[i].chunk_id not in seen:
              seen.add(src[i].chunk_id)
              merged.append(src[i])
  ```
- 返回从 4-tuple 扩为 **5-tuple** `(merged, kg_block, kg_id_map, kg_hits, ppr_count)`(`ppr_count=len(ppr_chunks)`,供 C 的诊断标记)。唯一调用方 `ask_chunk`([:5301](backend/app/services/sqlite_repository.py:5301))同步解包多一个变量。`ask_chunk` 其余(rerank/truncate/`_answer_mix`)**零改**——只是候选更多。

> 注:`_ppr_retrieve` 用 `query`(=`retrieval_query`,与 overlay 同源);PPR relevance 已 min-max 归一∈[0,1];与 `_answer_mix` 既有 chunk 锚机制天然兼容(graph BFS / reasoning 已验证)。

### C. 可观测(诊断,非用户可见)
`ask_chunk` 的 `ask_stage("mix_rerank", ...)`([:5314](backend/app/services/sqlite_repository.py:5314))detail 增 `concept_walk=ppr_count`(用 B 的 5-tuple 透出的 `ppr_count`)。落 events.jsonl 供真机排查(对齐 [[model-error-observability]]:调试奇怪问答先看 events.jsonl)。

### D. 命名:概念漫游(用户可见串)
**用户可见 `summary=` 文案全改**(master 现成,本 PR 一并改;machine 键不动):
- **reasoning**(`reasoning_retrieval.py`):247 seed 兜底、350 未启用 skip、354 上限 skip、365 action —— 四处 `"PPR/ppr_retrieve …"` → `"概念漫游 …"`。
- **graph**(`sqlite_repository.py`):6147 `"PPR 跨文档召回 {n} 个 chunk"` → `"概念漫游:跨文档召回 {n} 个 chunk"`。
- **chunk**:C 的 `concept_walk` 标记(诊断,非 summary)。
- 守卫:`test_no_user_facing_ppr_string_remains` grep 服务层 `summary=` 行无残留「PPR」/「ppr_retrieve」(剔除机器变量名 `_MAX_PPR_RETRIEVES`)。
- 内部 `GRAPH_PPR_ENABLED` / `_ppr_*` / `step_type="ppr"` / `detail` 键(机器键)**不动**——只改人读 `summary`。

### E. 隔离 / 不变量
- `_mix_retrieve` **仅 `ask_chunk` 调用**(已 grep 核)→ 改它只影响 chunk。朴素路径(`elif len(sub_queries)>=2` / `else` 两支)、reasoning、graph **零改**;`_answer_mix` 不动。
- 守 `[0,1]`/tau:PPR relevance 已归一;经 rerank 重排后 `_answer_mix` 出 chunk 锚(现成)。reasoning 改名是纯文案,不碰逻辑。

### F. 错误 / 边界
- `_ppr_retrieve` 无 KG/无 reset → `[]` → 安全(无额外候选)。
- `graph_ppr_enabled=False` → `ppr_chunks=[]` → round-robin 退化为今天的两路 → **行为字节等价**。
- 去重:`seen` 集跨三路,防 PPR chunk 与向量/overlay 重复段。
- 仅 overlay 路接入;无 rerank / 无 KG → 朴素路径,不触发 PPR(可接受,overlay 本是「富」路)。

### G. 测试
1. **`_mix_retrieve` 三路**:2-doc moe + flag 开 → `merged` 含跨文档 chunk(如 `cB`);flag 关 → 仅两路(无 PPR chunk)。
2. **`ask_chunk` overlay 端到端**:2-doc moe + rerank stub + 对比 query → 答案 chunk 锚覆盖跨文档源。
3. **flag off 字节等价**:`graph_ppr_enabled=False` → `_mix_retrieve` 输出与改动前一致(无 PPR chunk)。
4. **隔离**:朴素路径(无 overlay)、reasoning(`ask_reasoning`)、graph(`ask_graph`)快照不变。
5. **去重**:PPR 与向量/overlay 重叠 chunk 不双计。
6. **改名**:reasoning 轨迹 `summary` 含「概念漫游」、不再含「PPR 跨文档」;`step_type` 仍 `"ppr"`(机器键不变)。

## 数据流
```
ask_chunk(overlay_on) → _mix_retrieve[向量 + KG-overlay + 概念漫游(PPR)]
  → 三路 round-robin 去重 → rerank(免费控噪)→ token 预算 → _answer_mix
  → 跨文档引用答案
flag off / 非 overlay 路 → 今天的行为(零 PPR)
```

## 未决 / 后续
- chunk 模式**用户可见**的「概念漫游」徽章(前端):需后端加一个响应信号(如 `AskResponse.used_concept_walk`/计数)。本 PR 不做(YAGNI,避免投机字段);真要做时一行后端 + 前端徽章。
- 真机:overlay 路每查 +1 次 PPR(默认高频模式)→ 观察延迟(图共享缓存、rerank 是大头);观察 rerank 是否确实压住跨文档噪声。
- 逃生口:若真机见噪/延迟,再评估对比意图门控。
