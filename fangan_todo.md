# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-05-29
对照：`silicon_notebook_fangan.md`（产品方案）。已完成项见 `fangan_done.md`；本文件只列**尚未做完**的部分。

> 规则：完成某项后，从本文件移除并补进 `fangan_done.md`（见 `AGENTS.md` 的「Tracking Completed Spec Features」）。

## 状态速览

- ✅ 已完成主体：v0.1 闭环、v0.2 Rule 治理、PDF(MinerU+MLX/pypdf+KaTeX)、知识抽取(LLM 分窗+CJK 模糊绑定+启发式+去重/置信度)、混合检索、citation 校验、Article 研究简报+反馈。
- ✅ 本轮(dev 分支)新完成：**Explain Rule(§6.10)**、**Derived Rule Candidate 审核队列(§7.5)**、**创建富字段+6 模板(§6.1/§6.2)**、**CSV/Excel 解析(§6.3)**、**质量/分析看板(§16)**。详见 `fangan_done.md`。
- 🔧 剩余方向：Article 深度可视化 → v0.4 Review Mode → v1.0 企业。

---

## P0 — schema-guided 抽取（profile 注册表 + 新类型闭环 + schema 管理/归纳 + 关系图 + ask 织入 + 自我修正，全部已落地，见 fangan_done §21/§22/§23）
> 余下的增量优化（非阻塞）：
- [ ] **关系图升级**：当前 edges 用 headline 模糊匹配自由文本；可在抽取时直接产出 id 级关系，或加可视化布局（现为列表）。
- [ ] **Implication Map / Inference 分层（§7.3/§7.4）**：edges 层已具备，可在其上做 supports/extends/challenges 的分型与可视化树。
- [ ] **schema 归纳字段增补**：当前归纳只提议新「类型」；可扩展为对已有类型提议新「字段」。
- [ ] **自我修正成本**：refine 为每来源一次额外 LLM 调用；大库可加开关/抽样。

## P1 — Article Studio 深度（方案 §7 / v0.3 余项）
- [ ] **typed 关系下游动作（§7.2）**：claims 已有启发式 `relation_type`/`related_rule_id`/`implication` 且 derived 候选已可审核入库；仍缺 `suggests_checklist`/`creates_risk` 等关系驱动的"建议更新 checklist/新增风险"动作（目前仅展示）。
- [ ] **Implication Map（§7.4）**：Article Claim →（supports/extends/challenges）Rule/Method/Checklist 的可视化树。无端点、无前端。
- [ ] **Inference 分层（§7.3）**：Level 0–4（直接/内部/场景/假设/验证）+ Hypothesis 对象（§5.9）。当前 claims 扁平。
- [ ] **研究简报字段补齐（§7.1）**：每条 claim 的 `measurement_condition`、逐条 limitation 等。

---

## P2 — 检索深化（方案 §11，**已评估为低 ROI / 基础设施级，暂缓**）
> 现状已是 CJK 分词 + 向量余弦(真实 embedding)+ 场景 soft boost + 相关度地板 + 类型权重，覆盖 §11 主体。下列为增量基础设施，当前优先级低：
- [ ] **BM25 / FTS5 全文索引**：需 SQLite FTS5 虚拟表 + 同步维护；纯按文档的 tf 加权（无语料 df）收益有限，已验证不值得为此扰动现有阈值。
- [ ] **结构化 rule matching 硬过滤（§11.4）**：`structured_boost` 软加权已部分满足；硬过滤有清空结果风险，需谨慎。
- [ ] **Knowledge graph 遍历**：规则/案例/方法关系图召回，独立大特性。

---

## P3 — Review Mode（方案 v0.4）
- [ ] Review session（一次评审会话）。
- [ ] 场景化 checklist sign-off（逐条勾选 + evidence）。
- [ ] reviewer 评论 / action items。
- [ ] 导出 review 报告。
- [ ] project-level workspace。

---

## P4 — 企业能力（方案 v1.0，明确后期）
- [ ] 多用户 / 登录 / 会话（当前硬编码单 curator 用户）。
- [ ] RBAC + source 级权限 + access_scope 落地。
- [ ] 审计日志（结构化 audit trail；现有 `.local/logs/llm.jsonl` 只是 LLM 调用日志）。
- [ ] SSO / 私有(VPC)部署。
- [ ] Connectors：Confluence / SharePoint / Google Drive / Jira / Git / Slack·Teams / Email。
- [ ] 多 notebook 全局搜索。
- [ ] Rule 版本 diff / 历史。
- [ ] 自动用户记忆（当前 `memory_mode=manual`，刻意不做自动记忆）。

---

## KG 重构（知识图谱抽取，进行中）
> 4 粗节点(Concept/Claim/Formula/Procedure)+ 富边 + 字符级证据的重设计，gold 子系统已落地（`backend/app/services/kg*`、`scripts/kg_goldgen*`、`docs/superpowers/specs/2026-06-01-kg-*`）。

- [ ] **节点属性(attrs)形态未定**——当前已把 `attrs` 从数据模型 / 抽取 / spec / 已生成 gold 全部移除，节点文本统一进 `name`（Concept=实体名、Claim=完整断言、Formula=表达式、Procedure=过程名）。待决定每类节点是否需要细属性、放哪些：
  - Concept：`aliases[]`、`kind`(自由标签)、`definition`
  - Claim：`quantitative_values{}`、`polarity`
  - Formula：`variables{符号→含义}`、`role`(公式作用)
  - Procedure：`steps[]`(有序步骤)
  - 决策后牵动：抽取 prompt、`models.Node`、`emit`、`canonicalize`(aliases 合并)、`match._node_key`、评测维度、curation guide、embedding 召回(name vs statement)。决定前不要再往节点上加字段。
- [ ] **窗口阶段过滤 reference / bibliography**：cmos 等把参考文献条目误抽成 Claim（如 `Reference N: ...`）；在切窗时识别并跳过 references 小节，减少噪声。
- [ ] **gold 人工策展**：14 章 pro 草稿(`fangan/testcases_kg/`，gitignored)按 curation guide 逐章裁决后，移出 ignore 锁为权威 gold。
- [x] **产品抽取流水线落地（后端）**：KG 成为唯一抽取+知识模型，仅支持 academic_paper/textbook；`_run_extraction` 走 `kg_ingest`（deepseek-v4-flash）→ 自动入库 `knowledge_objects(approved)` + 新表 `knowledge_relations`；`/graph`·`/ask` 改 KG-native；qiefen/legacy/其它类型全删。spec `docs/superpowers/specs/2026-06-01-kg-product-integration-design.md`、plan `…/plans/2026-06-01-kg-product-integration-backend.md`。
  - 真机 smoke 基线（`scripts/kg_product_smoke.py`，deepseek-v4-flash）：engram(学术) 458 节点/366 边；cmos 摘录(教材) 180 节点/122 边；类型均 ⊆ {concept,claim,formula,procedure}，空标题=0，抽样证据未落地=0。
  - [x] **前端改造**：`frontend/app/page.tsx` 对齐 KG-only 后端——删除 rules/methods/risks/glossary/scenario-query/case/checklist/explain 的调用与 UI；AskResponse 渲染收敛到 conclusion+related_knowledge+citations；知识浏览改为按 `/knowledge-types` 动态出 tab（不再硬编码旧类型）；featured 过滤与文案改 KG 类型。`tsc --noEmit` 通过。plan `docs/superpowers/plans/2026-06-02-kg-frontend-alignment.md`。

## 工程/技术债（非方案功能，但影响质量）
- [ ] 抽取 LLM 让模型回传 `element_id` 做精确证据绑定（当前靠 quoted_span 精确子串 + CJK `token_overlap≥0.6` 模糊回退；已可用，但模型回传 id 会更稳）。
- [ ] 抽取/问答的 LLM 延迟与成本：`qwen3.7-max` 较慢（单文档可达 ~分钟级，异步不阻塞）；可评估抽取用 `qwen-plus`/`qwen-turbo`。
- [ ] `knowledge_embeddings` 表已建——确认 payload 级向量在 approve 时写入并被 `score_knowledge` 用上（避免只靠 evidence 元素向量）。
- [ ] 后端运行规范：真实处理务必**不带 `--reload`**（见 README），否则后台 BackgroundTask 会被中断。
- [ ] 扫描件本地 OCR、DOCX/PPTX 的 OMML 公式解析（PDF 公式已由 MinerU 覆盖）。

---

## 验证基线（每完成一项都要保持）
- `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh` 全绿（py_compile + 离线 hermetic smoke + tsc）。
- 离线（无 LLM/embedding/MinerU）闭环不回退；`smoke_backend.py` 已钉死 `mineru_mode=off` 且不读真实 `.env` 密钥。
- 前端 `npm run build` 通过。

## 统一 KG（跨文档合并）+ 可视化（后端已落地，2026-06-02）
> 非破坏性 concept_clusters 跨文档合并 + Embedder 接口 + 检查视图 API。spec `docs/superpowers/specs/2026-06-02-kg-unified-and-viz-design.md`、plan `…/plans/2026-06-02-kg-unified-backend.md`。
- [x] **Embedder 接口**：local BGE(dev) / dashscope text-embedding-v4(prod) / FakeEmbedder(test)，配置 `EMBED_PROVIDER` 切换；`store_kg` 批量建节点向量（仅 `embedder_configured` 时）。
- [x] **跨文档 Concept 合并**：`concept_clusters`/`concept_merge_candidates`；分层匹配(名称精匹+向量阈值 0.90/[0.82,0.90) 灰区)；`rebuild_unified_kg`(抽取后自动+可手动)；confirm/reject 持久化、rebuild 强制 union/阻断。
- [x] **统一图谱派生**：边重指向 canonical + 去重，按 notebook 内存缓存(写时失效)；concept 级 `/unified-kg` + `/concepts/{id}/detail` + 合并审核 API（48 后端测试通过）。
- 真机集成+性能 smoke(FakeEmbedder 注入, 600 概念/2 文档)：合并正确(480 簇)，rebuild=1654ms(<3s)，unified_graph 冷 4ms/热 0.3ms(<150ms)。
- [ ] **真模型验证**：配 `EMBED_PROVIDER=local`(bge-m3, 需装 sentence-transformers + 首次下模型) 或 `dashscope`(text-embedding-v4) 后，跑真机 smoke 看语义合并质量 + 灰区候选量(FakeEmbedder 下噪声候选很多，真模型应大幅减少)。
- [ ] **性能跟进**：`rebuild` 的 pending 候选写入改批量单事务(当前每候选一连接，pending 大时偏慢)；rep-pair 配对在 _MAX_REPS 上限附近是 O(reps²) Python 循环，必要时再优化。
- [ ] **前端可视化视图**（另起 plan）：全屏三区(过滤/搜索/待审 · react-force-graph-2d 概念画布 · 证据下钻+合并审核)，接 `/unified-kg`·`/concepts/{id}/detail`·pending/confirm/reject。
