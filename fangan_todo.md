# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-05-29
对照：`silicon_notebook_fangan.md`（产品方案）。已完成项见 `fangan_done.md`；本文件只列**尚未做完**的部分。

> 规则：完成某项后，从本文件移除并补进 `fangan_done.md`（见 `AGENTS.md` 的「Tracking Completed Spec Features」）。

## 状态速览

- ✅ 已完成主体：v0.1 闭环、v0.2 Rule 治理（状态机/owner/合并/冲突/Method·Risk·Glossary 浏览）、PDF 解析（MinerU+MLX / pypdf 回退 + KaTeX/表格渲染）、知识抽取（LLM 分窗 + CJK 模糊证据绑定 + 启发式回退 + 去重/置信度）、混合检索（关键词+向量+场景 boost）、citation 校验、Article 研究简报 + 反馈。
- 🔧 进行方向：v0.3 Article 深度 → 创建体验/数据类型/分析 → v0.4 Review Mode → v1.0 企业。

---

## P1 — 高价值、低/中成本（建议优先）

### T1. Article Studio 收尾（方案 §7 / v0.3）
- [ ] **Derived Rule Candidate 审核队列（§7.5）**：现在派生规则候选只作为字符串塞在 `ArticleResearchBrief.derived_rule_candidates` 里。需要：持久化为一等队列（`derived_rule_candidates` 表已存在但无端点）+ `GET 列表` + `POST /derived-rule-candidates/{id}/approve|reject`，approve 落入正式 `knowledge_objects`(rule)。
- [ ] **typed 关系结构化落库（§7.2）**：claims 已有启发式 `relation_type`/`related_rule_id`/`implication`，但缺 `suggests_checklist`/`creates_risk` 等关系的下游动作；关系仅展示，未驱动"建议更新 checklist"。
- [ ] **Implication Map（§7.4）**：Article Claim →（supports/extends/challenges）Rule/Method/Checklist 的可视化树。无端点、无前端。
- [ ] **Inference 分层（§7.3）**：Level 0–4（直接/内部/场景/假设/验证）。当前 claims 是扁平列表，无层级与 Hypothesis 对象（§5.9）。
- [ ] **研究简报字段补齐（§7.1）**：每条 claim 的 `measurement_condition`、逐条 limitation 等。

### T2. Explain Rule（方案 §6.10）
- [ ] "为什么有这条规则"：规则 → evidence → source/claim 反向追溯。新增 `GET /notebooks/{id}/rules/{rule_id}/explain` + 前端展示（来源、形成原因、相关案例、适用场景、例外、相关风险/检查）。

### T3. 创建体验与知识组织（方案 §6.1 / §6.2）
- [ ] **创建富字段（§6.1）**：`NotebookCreate` 仅 name/purpose/primary_domain；缺 `target_users` / `expected_questions` / `source_types` / `taxonomy` / `access_scope`。
- [ ] **Notebook 模板（§6.2）**：6 种模板（Rule / Method / Case / Review / Article / General）预设字段与引导。当前无模板系统。
- [ ] **自定义 taxonomy 编辑**（v0.2 余项）。

### T4. 数据类型扩展（方案 §6.3）
- [ ] **CSV / Excel 解析**：半导体规则常以表格维护。MinerU CLI 已支持 xlsx/docx/pptx，可顺势接入；CSV 需自写轻解析（按行/列 → table/row 元素）。

---

## P2 — 检索与分析深化

### T5. 检索增强（方案 §11）
- [ ] **BM25 / FTS5 全文**：当前是关键词命中率 + 向量余弦，无 BM25/词频权重。
- [ ] **结构化 rule matching 硬过滤（§11.4）**：现有 `structured_boost` 只是软加权；缺基于 applies_to/condition 的场景本体硬匹配/过滤。
- [ ] **Knowledge graph 遍历**：规则/案例/方法之间的关系图召回（如"提到 bondwire 的规则"链路）。

### T6. 质量与分析看板（方案 §16）
- [ ] 回答质量分析（基于已存的 `feedback`/`answers` 表做 usefulness 率、低分回答聚类）。
- [ ] 知识缺口看板（哪些场景/提问检索不到知识）。
- [ ] 抽取质量指标（候选 approve/reject 率、needs_review 占比）、检索质量评估。

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
