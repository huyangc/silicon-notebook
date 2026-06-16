# P4:KG 收缩到「按需 · 严格推理」+ 铺 two-tier 底座 设计

**状态:** 设计已与用户敲定全部决策(2026-06-16),待用户复审本 spec → writing-plans。

> 行号取自 worktree `worktree-p4-kg-shrink`(从 origin/master 起)快照,实施期(writing-plans)须按当时代码复核。

## 背景与根因
chunk-native(P1+P2+P3,#44/#50)上线后,**通用问答默认走 `mode=chunk`,与 KG 完全解耦**(两套表、无外键、抽取管线独立)。但当前每次摄取仍**无条件跑全量 KG 抽取**——即 `kg-extract-llm-timeout` 记忆里最慢最贵的一步(全量 LLM 窗口抽取),而这步产出只服务 `reasoning`/`graph`/`fast`/`global` 四个 KG 模式,其中:
- `fast`(`group=legacy`,`user_facing=False`):KG top-N + 1-hop,已被 chunk 取代;
- `global`(`group=global`,`user_facing=False`):社区 map-reduce,默认休眠(`kg_community_summary_enabled=False`),综述场景已由 chunk 大召回覆盖;
- `reasoning` / `graph`(`group=strict`):agentic 多步 + 多跳子图,是真正离不开 KG 的"严格推理"。

**根因:** KG 已不是通用问答主力,却仍在每次摄取付出最贵的抽取成本。P4 把 KG **收缩到只服务严格推理、按需构建**;并顺势铺好 two-tier 知识库的**底座**(底层权威库 + 并集检索 + 门控),把 two-tier 的富语义留给独立 roadmap。

## 已定决策(brainstorm,2026-06-16)
1. **抽取按需、默认关:** 新增 `KG_AUTO_EXTRACT`(默认 `False`);摄取默认只建 chunk,不抽 KG。
2. **模式收缩:** **删 `fast` + `global`**(`global` 仅删 mode handler + 注册,**C2 社区构建代码留休眠**、可逆);保留 `chunk`/`reasoning`/`graph`。
3. **底层 KG = 离线抽取:** 底层(共享/权威)库走**离线 CLI** 刻意构建;notebook(个人)KG 走 **in-app 按钮**(后台);二者共用同一抽取引擎。
4. **严格推理门控:** `(底层 KG 已配置且有内容) ∨ (本 notebook KG 有内容)` 才可用 `reasoning`/`graph`;两者皆空 → 不可用(前端置灰 + 后端兜底)。
5. **base 并集检索:** reasoning/graph 检索范围 = `本 notebook KG ∪ base KG`(简单并集、按对象去重,**无冲突消解**)。
6. **base 标识:** `BASE_KG_NOTEBOOK_IDS` 配置项指定哪些 notebook 当共享底层库,**复用 `knowledge_objects`,零新表零迁移**。

### KG「在场」怎么追踪(已定:派生,零迁移)
摄取终态统一仍是 `extracted`(语义改为"**摄取完成**",不再等价"有 KG")。"有没有 KG" = 该 notebook/source 下**是否存在 `knowledge_objects`**(复用 L702 的 `EXISTS` 模式);"建图中" = 有 source 处于瞬态 `extracting`。**不加列、不加状态值、不动前端徽章。** "抽取失败可重试"的精细态先用 event log 兜底(非目标)。

## 架构

### A. 配置(`backend/app/core/config.py`)
新增两项,其余 `kg_window_*`/`kg_extract_workers`/`kg_job_concurrency` 等不动:
```python
# 摄取默认不抽 KG;True 恢复旧"整库自动抽"(迁移/测试逃生口)。
kg_auto_extract: bool = Field(False, env="KG_AUTO_EXTRACT")
# 共享底层(权威)KG:逗号分隔的 notebook id 列表,这些 notebook 当全局 base 库。
base_kg_notebook_ids: List[str] = Field([], env="BASE_KG_NOTEBOOK_IDS")
```
- `base_kg_notebook_ids` 复用现有 `split_cors_origins` 同款 `field_validator(mode="before")` 把逗号串切成 list。
- 新增 property `base_kg_configured -> bool = bool(self.base_kg_notebook_ids)`。

### B. 摄取门控(`sqlite_repository.process_source`,L1109–1280)
把抽取块(现 L1222–1230:置 `extracting` → `_run_extraction` → `stage` → `_mark_unified_kg_dirty`)整体包进条件:
```python
should_extract = self.settings.kg_auto_extract or self._notebook_has_kg(notebook_id)
if should_extract:
    self._set_source_status(source_id, "extracting")
    ... _run_extraction(source_id) ... _mark_unified_kg_dirty(notebook_id) ...
# 终态无条件置 'extracted'(= 摄取完成;chunk/embedding 已在 L1193/后台完成)
self._set_source_status(source_id, "extracted", error_message=empty_hint or fallback_hint)
```
- `_notebook_has_kg(notebook_id) -> bool`:复用/抽取 L702 的 `SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id=?)`。
- **含义:** ① 默认新 notebook 只建 chunk;② **已建过 KG 的 notebook 新增 source 自动续抽**,保持图完整,免手动 rebuild;③ 跳过抽取的 source 仍达终态 `extracted`,chunk 问答立即可用。

### C. 建图引擎 + 两个触发
**引擎(新增方法):** `build_notebook_kg(notebook_id) -> dict`
- 对该 notebook 下**尚无 KG 的 source**(`NOT EXISTS knowledge_objects WHERE source_id=?`)逐个:置 `extracting` → `_run_extraction(source_id)` → 置 `extracted`;末尾 `_mark_unified_kg_dirty(notebook_id)` 一次。
- 复用 `kg_job_concurrency` 作业池并发。**幂等**:已有 KG 的 source 跳过;已在建中(有 source `extracting`)直接返回。
- 守卫:`settings.llm_configured` 为假 → 抛错/返回错误(不空跑)。
- 单 source 抽取失败:`try/except` 隔离,不连累其余、**不回退该 source 的 `extracted` 终态**,错误进 event log。

**触发 1(in-app · notebook/个人 KG):** `POST /api/notebooks/{id}/kg/build`(`routes.py`)
- 后台线程跑 `build_notebook_kg`,立即 `202`。LLM 未配 → `409`("先配 LLM")。已在建中 → 幂等 `202`。

**触发 2(离线 · base/大库):** 新增 CLI(`scripts/build_kg.sh <notebook_id>`,镜像 `scripts/backend.sh` / P1 chunk 回填脚本风格)
- 同步跑同一 `build_notebook_kg`,刻意 / 一次性构建。底层权威库走这条。

### D. `kg_state` 派生 + API 暴露
**派生(只读,不落库)** —— `notebook_kg_state(notebook_id) -> str`:
- `building`:存在 source 处于 `extracting`;
- 否则 `ready`:有 `knowledge_objects` 且**所有非 failed 的 source 都有 KG**;
- 否则 `partial`:部分 source 有 `knowledge_objects`;
- 否则 `none`。

**暴露:**
- `GET /api/notebooks/{id}` 响应新增 `kg_state` 字段;
- `/ask-modes`(L407)或 notebook 详情额外暴露 `base_kg_configured: bool`(前端门控需要全局信号);
- `/ask-modes` 的 `requires_kg` 已有,不动。

### E. 删 `fast` + `global` + 退役别名
- **删:** `ask_modes.py` 注册表移除 `fast`、`global` 两行;删 `sqlite_repository.ask_fast`(L4305)、`_ask_global`(L4595)及其私有 helper(仅它们用的);**保留 C2 社区代码休眠**(`rebuild_communities`/`summarize_communities`/`communities` 表/`list_communities` 等不删,只是没有 mode 入口)。
- **退役别名(窄例外):** `resolve_mode`(L45)对**退役但曾合法**的 id 映射到 `chunk` 而非 422:
  ```python
  _RETIRED_MODES = {"fast": "chunk", "global": "chunk"}
  # resolve_mode: key = _RETIRED_MODES.get(key, key) 后再查 ASK_MODES
  ```
  保护旧会话/书签里持久化的 `mode`。记一条 deprecation log。注意:这是对**两个具名退役 id** 的窄映射,**不是**恢复"未知 mode 静默回退"(其余未知 mode 仍 422)。
- **契约同步:** 更新 `scripts/check_ask_modes_contract.py` 与 `frontend/app/ask-modes.ts`(`fast`/`global` 本就 `user_facing=False`,大概率不在前端用户列表,但契约校验须随注册表收敛)。删两模式相关测试。

### F. 严格推理门控(后端兜底)+ base 并集检索
**门控:** `ask_reasoning`(L5010)/`ask_graph`(L5086)入口先算:
```python
base_available = settings.base_kg_configured and any(self._notebook_has_kg(b) for b in settings.base_kg_notebook_ids)
strict_available = base_available or self._notebook_has_kg(notebook_id)
```
- `strict_available` 为假 → 不空跑 agentic 循环,直接返回 `AskResponse(kg_required=True, conclusion="本笔记本尚未构建知识图谱,也未配置可用的底层 KG;请先点『构建知识图谱』或配置 BASE_KG_NOTEBOOK_IDS 后再用严格推理。")`。前端置灰是第一道,这是 API 兜底(防直连客户端)。
- `AskResponse` 新增字段 `kg_required: bool = False`(additive,向后兼容)。

**base 并集检索(最小爆炸半径:编排层按层各查一次 + 合并去重,不改底层 SQL):**
- 计算检索 `scope_ids = dedup([notebook_id] + settings.base_kg_notebook_ids)`。
- `reasoning` 的 `ReasoningRetriever.search()`:对 `scope_ids` 每个 id 调一次现有 `_retrieve_scored(nb_id, query, ...)`,合并候选、按 `object_id` 去重(对象 id 前缀 `K-/KL-/KF-/KP-` 且按 notebook 命名,跨层不撞),保留各自 `relevance`([0,1] 同尺度,可直接并入现有 `quota_fuse`/排序)。`neighbors`/`get`/`search_elements` 同法按 id 尝试 + 合并。
- `graph` 的 `multihop_subgraph`(`kg/graph_reason.py` L91):对 `scope_ids` 每个 id 各跑一次、**子图取并**(tier 间本就无跨库边,各 tier 子图内部自洽,语义干净)。
- **无冲突消解**:并集 + 去重即止。冲突(底层为准)、断链补桥、chain_trust 全留 two-tier-kb roadmap。

### G. 前端门控(并行 track · 本 spec 只定契约不实现)
消费 `notebook.kg_state` + `base_kg_configured` + `ask-modes.requires_kg`,对 `requires_kg=True` 的 `reasoning`/`graph`:
- `base_kg_configured` 为真 → 始终可用;
- 否则按 `kg_state`:`ready`/`partial` → 可用(`partial` 附"有新 source,重建以纳入");`building` → 禁用 + 进度;`none` → 置灰 + 显示「构建知识图谱」按钮(调 `POST /kg/build`)。

独立 PR,同 #49 处理方式。

### H. 向后兼容 / 迁移
- **零数据迁移**:已有 KG 的 notebook → `kg_state=ready`,`reasoning`/`graph` 照常。
- 仅**新摄取**行为变(默认不抽)。想全恢复旧行为:`KG_AUTO_EXTRACT=true`。
- 旧客户端发 `mode=fast/global` → 映射到 `chunk`,不 422。

## 错误处理(永不破坏既有路径)
- 摄取门控跳过抽取 → source 正常到 `extracted`,chunk 全程不受影响(chunk 在抽取前已建)。
- `build_notebook_kg` LLM 未配 → 端点 `409` / CLI 非零退出 + 明确信息;单 source 失败隔离、不回退、进 event log。
- 严格推理无 KG/无 base → `kg_required=True` 结构化返回,不抛异常、不空跑循环。
- base 配置了但某 base notebook 无内容 → `base_available` 计 false(不放行空头门控);并集检索对空 tier 不贡献候选。
- 退役别名只认 `fast`/`global` 两个具名 id,其余未知 mode 仍 `422`。

## 测试(TDD)
- **摄取门控:** `kg_auto_extract=False` 且无 base、notebook 无 KG → 摄取后 `knowledge_objects` 为 0、`chunks` 正常、`ask_chunk` 可答;notebook **已有 KG** → 新 source 摄取自动续抽(有 `knowledge_objects`)。
- **`build_notebook_kg` / 端点:** 无 LLM → 拒(`409`/非零);有 source 无 KG → 跑后有 `knowledge_objects`;已有 KG 的 source 跳过(幂等);已在建中幂等。
- **`kg_state` 派生:** `none`/`building`/`partial`/`ready` 四态单测(造不同 source 状态/对象组合)。
- **删模式 + 别名:** `resolve_mode("fast")`/`resolve_mode("global")` → `chunk`(不抛 `UnknownAskMode`);未知 mode 仍抛;`reasoning`/`graph`/`chunk` 仍解析;契约校验脚本过。
- **门控 + base 并集(hermetic:FakeEmbedder + 假 LLM):**
  - notebook 无 KG、无 base → `kg_required=True`,不跑循环;
  - notebook 无 KG,但 `BASE_KG_NOTEBOOK_IDS` 指向一个**有 KG 的 base notebook** → reasoning 可跑,且**断言检索候选里出现 base 的对象**(守住"base-alone 可推理");
  - notebook 有自己的 KG + 配了 base → 检索候选**两层对象都在**(守住并集);
  - graph 多跳:base 子图节点出现在结果(守住子图取并)。
- **回归:** 现有 `reasoning`/`graph`/`chunk` 测试全过;全量 `check.sh` EXIT=0。
- **真机:** 把一个 notebook 设为 base、离线 CLI 建图;另一个空 notebook 配上该 base → 严格推理能引用 base 内容;无 base 的空 notebook → reasoning 置灰/`kg_required`。

## 实施阶段(供 writing-plans)
- **P4-1** config:`kg_auto_extract` + `base_kg_notebook_ids` + `base_kg_configured` property + 校验器 + 单测。
- **P4-2** 摄取门控:`_notebook_has_kg` helper + `process_source` 包条件 + 单测(默认不抽 / 已有 KG 续抽)。
- **P4-3** 建图引擎 + 端点 + 离线 CLI:`build_notebook_kg` + `POST /kg/build` + `scripts/build_kg.sh` + 单测(幂等/无 LLM 拒/失败隔离)。
- **P4-4** `kg_state` 派生 + API 暴露(notebook 详情 + `base_kg_configured`)+ 四态单测。
- **P4-5** 删 `fast`/`global` + 退役别名 + 契约脚本/`ask-modes.ts` 同步 + 删旧测试 + `resolve_mode` 别名单测。
- **P4-6** 严格推理门控(`kg_required` + `AskResponse` 字段)+ base 并集检索(reasoning `search` 多层合并 / graph 子图取并)+ 集成测试(门控四情形 + 并集)。
- **P4-7** 全量验证 + 真机 base 演练 + PR。
- 前端门控(消费 `kg_state`/`base_kg_configured`)= **并行 track,独立 PR**,不在本 plan。

## YAGNI / 非目标
- **不瘦身节点类型**(Concept/Claim/Formula/Procedure 全留 —— 它们是 two-tier-kb 推导链底料:实测 formula 71% 落在推理子图、`derived_from` 两跳链 2054 条;砍了直接掏空 two-tier-kb)。
- **不做 two-tier 富语义**:冲突消解(底层为准)、断链补桥 + `[推断]` 标记、`chain_trust`、知识缺口队列、委员会角色治理、双轨评测 —— **全留 two-tier-kb roadmap**(`docs/superpowers/specs/2026-06-12-two-tier-roadmap-design.md`)。
- **不删 C2 社区代码**(留休眠;将来恢复 global 只是重新挂 handler)。
- **不做抽取失败的精细可重试态**(event log 兜底;真需要再上 `source.kg_status` 列)。
- **不动 Neo4j / 存储解耦**(另线 roadmap)。
- **base 并集不引 IN(...) SQL 大改**(编排层按层合并;如将来层数多/性能要求高再下沉到 SQL)。
- **前端门控 UX 实现**(并行 track)。

## 相关
- 收缩前提:chunk-native `docs/superpowers/specs/2026-06-15-chunk-native-retrieval-design.md`(本 spec 兑现其"KG 收缩到严格推理 + 路由"两个遗留问题)。
- 底座对接:two-tier-kb `docs/superpowers/specs/2026-06-12-two-tier-roadmap-design.md`(P4 铺底座,富语义由它实现)。
- ref-kg:删 `global` = 退役其 R4(全局 map-reduce);其余 Phase1+2 增强(query-refine 默认开、C1/C2 建图、C4、C7、R1/R2/R5)全在 P4 保留的 reasoning/graph/抽取路径内,不动。
