# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-09-07
对照：`silicon_notebook_fangan.md`（产品方案）。已完成项见 `fangan_done.md`；本文件只列**尚未做完**的部分。

> 规则：完成某项后，从本文件移除并补进 `fangan_done.md`（见 `AGENTS.md` 的「Documentation Ownership」）。
>
> 本版是 2026-09-07 按当前代码逐条对账后的重写。上一版（2026-05-29）里以下条目已经落地，
> 已从本文件移除：关系图改为 id 级 `knowledge_relations` + 力导图可视化；参考文献/索引等
> backmatter 小节在切窗阶段跳过（`kg/filters.py`）；合并候选批量单事务写入；rep-pair 配对改
> hnswlib ANN；分窗改贪心打包、并发按模型服务 `max_concurrency`；抽取回传 `ev:<int>` 做
> element-id 证据锚定；`knowledge_embeddings` payload 级向量进 `score_knowledge`；账号系统、
> 会话管理、分享链接、群组共享；Knowhow 版本历史/diff/回退；逐步推理大纲协同
> （PR #407/#411/#418）；在途提问接回（PR #661/#662/#664/#665）。Article Studio 与
> schema profile 抽取已退役，相关条目一并删除。

## 状态速览

- 产品主线已上线：KG-native 抽取 + 混合检索 + Ask（chunk / reasoning，含意图澄清与大纲协同）
  + Deep Report + Memory / Knowhow + MCP + 群组分享 + 部署插件扩展点。
- 2026-08-29 起的「生产热路径修复计划」（批 0 / 1 / 2 / 3·W1–W4）全部实施项已合入（至 PR #676）。
  剩余为用户侧生产动作与各设计稿登记的残余债。
- 本文件按「近期可动手」→「产品功能」→「长期方向」排列。可直接实现的项已排进
  `docs/superpowers/plans/2026-09-07-todo-closeout.md`（PR-1～PR-5），需拍板的项留在本文件。

---

## 一、生产热路径修复：收官后剩余

### 用户侧动作（需要生产环境，仓库内无法代做）

> T-0 生产只读测量、PostgreSQL 调参 runbook、批 1 索引 `--apply` 三项已由用户在生产环境完成
> （2026-09-07 告知）。SR-1（element 搜索腿 OR→UNION）已被差分测量证伪撤销，不再重试。

- [ ] **backfill-images 放量前**：原图缺口盘点只命中 12.3%，要么找回其它 MinerU output 根，
      要么接受部分回填；先挑 1–2 个来源用 `--source-id` 试点，在问答里亲眼看到引用带图再放量。

### 登记的残余债（有真实需求再做；真源为各设计稿的「残余债 / 登记」节）

- [ ] **多 worker 部署下进程内互斥失效**（W1 #10 / W2 #1）：正解是把 relink / unifiedkg /
      conflictresolve 的 claim 提升为 durable 行，而不是给删除加锁。当前沿用「生产单 worker」契约。
- [ ] **W1**：`_delete_indexing_pipeline_stages_in_batches` 级联未分批；
      `agent_access_tokens.default_notebook_id ON DELETE CASCADE` 语义登记不改；
      守卫若在 `scripts/` 扫出新的可见性谓词站点，逐条处置。
- [ ] **W2**：双代窗口时长无硬上界告警；`rebuild_canonical_relations` / `mention_bridge` 若仍是
      DELETE+INSERT 需并入代际协议；「翻转后 finish 前崩溃 ⇒ 下次全量重算 + 状态计数陈旧」
      是已知格。
- [ ] **旧索引退役债**：`idx_chunks_source`、`idx_clusters_nb_canonical`、0043 / 0007 原形索引
      均已被新索引前缀覆盖，登记为可回退的写放大冗余，尚未下线。
- [ ] **W3**：大库切换索引管线目前只是按活跃对象数阈值显式禁用（切回内建豁免）；
      「保存索引管线」发布事务的真正重构排到有真实需求时。
- [ ] **W4**：未回填老库（`source_index_backfilled=0`）的 legacy evidence 分支单语句可撞 30s，
      前置是既有离线 backfill；来源页签搜索改 UNION 后 `paper_meta_for_sources` 水合腿未跟进同口径。
- [ ] **PG 打开路径计数缓存的刻意延后项**：checkup H4/H5 缺向量 anti-join COUNT 仍是 30s TTL memo；
      SQLite 侧 pending memo 的全局 epoch 有跨库误伤弱点。

---

## 二、产品功能待办

### 架构与清理

- [ ] **架构渐进整改阶段 5：前端 workspace 状态拆分**。`frontend/app/page.tsx` 仍约 8900 行。
      其余阶段（Repository composition、application boundary、FastAPI lifespan）已交付。

### Ask / Deep Report

- [ ] **Deep Report 正文引用图片内联（第二期）**：Ask 与公开会话已交付；Deep Report 仍走引用详情
      展示，需复用同一套块级定位 / 去重 / 页内预览合同。
- [ ] **问答纠偏规则 12（限定词保真）人工 A/B**：仓库无问答质量评测台，放量前用「点名子部件 /
      周期性 / 方向」三句式各问一次验证；ledger 喂摘要未做。
- [ ] **Prompt 三层化后的 per-notebook 定制与 self-evo**：接缝只有 `fragment_text()`；L1 片段分
      两类（A 类离线 GEPA + 人审，B 类只改示例槽位），尚未拍板开放。
- [ ] **Agentic Memory 注入开闸与 A/B**：P1–P4 已合入，注入默认关闭，开闸是独立决定。
- [ ] 待办中心露出「问答进行中」（在途提问接回设计 §6.3 的可选项）。
- [ ] 自动模式对含「刚才 / 这个 / 那个」的订正句落 chunk+standard：登记为已知行为不修。

### 知识图谱

- [ ] **节点属性 attrs 形态未定**：`Node` 仍只有 name / section_path / evidence / mentions /
      steps / validity_scope；决定前不要再往节点加字段（`scripts/kg_strip_attrs.py` 头注释同此）。
      候选：Concept `aliases[]`/`kind`/`definition`、Claim `quantitative_values{}`/`polarity`、
      Formula `variables{}`/`role`。决策牵动抽取 prompt、`models.Node`、canonicalize、评测维度。
- [ ] **gold 人工策展**：`fangan/testcases_kg/` 仍在 `.gitignore`，未有策展后的权威 gold 入库。
- [ ] **跨文档概念合并的真模型质量验证**：Embedder 现只有 openai / dashscope 协议 + FakeEmbedder
      （本地 BGE 路线已随 model_registry 退役），真模型下灰区候选量未做正式 smoke。
- [ ] **推理分层**：边类型已有 supports / contrasts_with，无 extends、无 Level 0–4 分层、无
      Hypothesis 对象。
- [ ] schema 归纳只提议新类型，不对既有类型提议新字段。
- [ ] KG refine 自我修正只有总开关 `KG_REFINE_ENABLED`，无抽样 / 比率控制。

### 检索

- [ ] BM25 / FTS5 / tsvector 全文索引：已评估为低 ROI、基础设施级，暂缓。
- [ ] 结构化硬过滤：软加权已够用，硬过滤有清空结果风险，暂缓。

### 解析

- [ ] 扫描件本地 OCR（MinerU 之外无 OCR 路径）；DOCX / PPTX 的 OMML 公式解析。

### 多领域基准库合入后遗留（2026-09-07 对账）

真源 `docs/superpowers/specs/2026-07-19-multi-domain-bases-followups.md`；B 节四项 master 既有缺陷已全部修复。

- [ ] A1 半做：所有已知 Citation / AnswerAnchor 构造点已归一化并有测试，但缺「新构造点必须
      归一化 notebook_id」的静态守卫。
- [ ] A2 graph BFS 节点的锚点不带来源库 id（`evidence_context.py` 注释仍写「暂未填」）。
- [ ] A3 `target_base_id` 为空的存量待批晋升候选在队列里仍可点、必失败，前端未提示
      `scripts/backfill_promotion_targets.py`。
- [ ] A4 知识晋升用 `currentNotebookBases`、Memory 晋升用 `base_notebooks`，挂载数据源两处未统一。
- [ ] A5 「选择贡献目标」弹窗 JSX 在 `page.tsx` 与 `memory-panel.tsx` 重复。
- [ ] A6 深拷贝不复制 `notebook_bases` 挂载边，且未登记为刻意缺席。
- [ ] A7 SQLite `migrate()` 对「库版本高于代码」不报错；schema 单向性无文档。
- [ ] A8 `MOUNT_VALID_EXPR` 唯一定义点无结构守卫（`access_sql` 有同款守卫可照抄）。
- [ ] A9 残留：README API 清单未提 promote 端点的 `target_base_id` 与 400 态；点名的陈旧注释因
      行号漂移未核。

---

## 三、长期方向（方案 v0.4 / v1.0）

- [ ] **Review Mode**：review session、场景 checklist sign-off、reviewer 评论 / action items、
      导出 review 报告、project-level workspace。均无代码。
- [ ] **企业能力**：source 级 ACL（现只有 notebook 级 capability tier + 群组角色）、结构化审计
      日志（现只有 Knowhow 的 actor 标签投影）、SSO / VPC、Connectors（Confluence / SharePoint /
      Drive / Jira / Git / Slack）、多 notebook 全局搜索。多用户 / 登录 / 管理员角色 / 分享链接 /
      群组已交付。
- [ ] **分享的 edit 权限层与近实时协作**：写权限仍 owner-only；无 presence / revision 轮询
      （管理员用户列表的「在线状态」是另一特性）。原 spec
      `docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` 的 D2 / D3 决策已被
      账号系统取代，动手前须重写。
- [ ] **自动用户记忆**：`memory_mode` 固定 manual；Agentic Memory 已提供候选 / 巡固机制，是否
      开放自动写入待决。
- [ ] **插件化 X 系列剩余**：X6 / X7 等首个真实消费者；X10 后端热更用户明确要求「完全热更或不做」；
      niuma 插件 e2e 后的问题回流。

---

## 验证基线（每完成一项都要保持）

- `bash scripts/check.sh` 全绿（后端 pytest 全量 + 前端测试 + tsc + production build）；
  PostgreSQL 相关改动另跑 PG lane。
- 离线（无 LLM / embedding / MinerU）闭环不回退。
