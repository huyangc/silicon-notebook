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
