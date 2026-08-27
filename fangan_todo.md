# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-05-29
对照：`silicon_notebook_fangan.md`（产品方案）。已完成项见 `fangan_done.md`；本文件只列**尚未做完**的部分。

> 规则：完成某项后，从本文件移除并补进 `fangan_done.md`（见 `AGENTS.md` 的「Documentation Ownership」）。

## 状态速览

- ✅ 已完成主体：v0.1 闭环、v0.2 Rule 治理、PDF(MinerU+PyMuPDF4LLM/pypdf 最终兜底+KaTeX)、知识抽取(LLM 分窗+CJK 模糊绑定+启发式+去重/置信度)、混合检索、citation 校验、Article 研究简报+反馈。
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
- [x] **前端可视化视图（已落地）**：`frontend/app/page.tsx` 全屏「知识图谱」视图——左栏搜索+待确认合并、中区 react-force-graph-2d 概念力导图(按类型上色/按度数定大小)、右栏概念详情(成员/挂载断言公式/证据)；合并/拒绝即时重算。入口由工作区「关系图」按钮改为「知识图谱」。`tsc --noEmit` + `next build` 通过。plan `docs/superpowers/plans/2026-06-02-kg-viz-frontend.md`。
- [x] **节点内容丰富化（读取期 + prompt 微调，已落地）**：`node_context`(证据→所在句子 / Concept 定义(defines 边) / Procedure 有序步骤(section+文档序))；`/objects/{id}/context` 端点 + `concept_detail` 带 element_text；前端 KG 详情面板与知识库浏览器都呈现完整句子+定义+流程步骤；抽取 prompt 改为「证据=完整句子」+「procedure 用 precedes 串步」。真机 smoke(deepseek-v4-flash, engram)：证据 span 中位数 ~170 字符(原裸词~5-20)，MoE/Sparsity 等概念已带原文句子，procedure 经 node_context 还原有序步骤。spec/plan `docs/superpowers/specs|plans/2026-06-02-kg-node-enrichment*`。
  - 后续(非阻塞)：网络稳定后重抽看 precedes 是否变多；procedure 步骤的 step-specific 证据可再精化(当前偶尔引用小节概述句)。

## KG 性能（构建质量已验证 OK，2026-06-03，仅性能待优化）
> 构建侧已收敛：**element-id 锚定**（LLM 只回传 `ev:<int>` 标号，后端映射回该 element 的精确 text/offsets → 确定性 grounding，删掉脆弱的 `_locate`）+ **Concept 选择性 prompt**（丢弃 training/inference/buffer 等泛词，保留全部 formula/procedure）。同一文档 A/B：formula 0→11、procedure 5→19、concept 回到干净的 ~34、节点全部 grounded。已上线 master(`c5294f1`)。**抽取效果用户认可，下列纯性能项后续再优化：**
- [ ] **抽取超时偏紧**：`.env` `OPENAI_COMPAT_TIMEOUT_SECONDS=60`，但密集窗口单窗输出 10–17k token、生成可 >60s → deepseek 高负载时被 60s 截断 → 重试拖慢(早期甚至丢窗导致少节点)。实测 timeout=240s 时 0 错误全部完成。可评估常设 ~150s。当前靠 bounded-retry(`OPENAI_COMPAT_MAX_RETRIES=2`)兜底但慢。
- [ ] **并发受「按 section 切窗」约束**：`extract_graph` 并发度 = `min(_WORKERS=16, 窗口数)`，窗口按 section 切，故并发 ≈ section 数（ch02 仅 6 窗，16 worker 闲 10 个）。sweep 实测：N=9000/4500/3000 都只 6 窗、wall 68–78s 持平；N=2000→9 窗也不提速、且 overlap 变大→总输出反增(49k vs 34k token)。结论：**element 级/更小窗不是有效并发杠杆**，还有丢跨元素边风险。若个别 section 远大于其它，可只对超大 section 二次切分。
- [ ] **吞吐/成本**：每窗仍 ~3–8k 输出 token（selectivity 已削）；真正的延迟杠杆是上面的 timeout，不是并发。如要再降，可评估更快模型或在 A/B 守护下微调窗口。
- 说明：sweep/A-B 均为一次性脚本已删；细节见 `docs/superpowers/specs/2026-06-02-kg-evidence-id-anchoring.md` 与本轮对话。

## 多用户系统（用户身份 + 分享 + 近实时协作，2026-06-04，spec+plan 已就绪，暂缓执行）
> 用户决定：先搁置、记录待办。**Phase A（聊天会话管理）已落地上线 master**（会话 `created_by` 归属 + 列表/切换/新建/删除/重命名，前后端，真机验证过）。下列 B/C/D 暂缓。
> spec：`docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md`（决策 D1 协作=轮询+presence、D2 用户名 `^[A-Za-z]00[0-9]{6}$`、D3 仅用户名无密码经 `X-User-Id`、D4 存量数据归首登用户）。建设顺序 B→C→D，每个已有独立 plan。
- [ ] **Phase B —用户身份 + 数据隔离**：用户名校验 + `POST /login`(upsert) + `X-User-Id` 中间件(ContextVar 解析 current_user) + `list_notebooks` 按 owner 隔离 + 存量迁移给首登用户 + 前端登录闸/退出。含「单用户回退」兼容（系统仅 user-local 时无头放行）。plan `docs/superpowers/plans/2026-06-04-phase-b-user-identity.md`。
- [ ] **Phase C —分享 + 权限(view/edit)**：`notebook_shares` + `_access_tier`/`_require_access`(读≥view 写≥edit，前后端双拦) + `list_notebooks` 并入「分享给我的」+`access_tier` + 前端分享 UI/列表分区/view 隐写入口。`ask` 归 view。plan `docs/superpowers/plans/2026-06-04-phase-c-sharing-permissions.md`。
- [ ] **Phase D —近实时协作**：`notebook_presence` 心跳 + `notebooks.revision` + `notebook_activity` + `GET /notebooks/{id}/state?since=` + 前端 ~4s 轮询(presence + revision 变了刷新来源/KG/文章；聊天保持个人态)。无 websocket/CRDT/锁。plan `docs/superpowers/plans/2026-06-04-phase-d-cowork.md`。
