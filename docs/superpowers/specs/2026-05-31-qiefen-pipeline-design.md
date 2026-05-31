# qiefen 解析+抽取流水线 — 设计文档

- 日期：2026-05-31
- 状态：已通过 brainstorming 评审，待写实现计划
- 关联：`fangan/qiefen.md`（方案）、`fangan/article_research_gold_spec.md` / `fangan/textbook_gold_spec.md`（gold 规范）、`fangan/testcases/`（14 章 gold + harness）

## 1. 目标与背景

`fangan/testcases/` 提供了一套金标准测试集：两类文档（论文型 `engram` 9 章、书籍型 `cmos` 5 章）各章一套 `gold.yaml`，以及一个逐 stage 打分的 `harness`。`gold.yaml` 是 `qiefen.md` 所定义的 **8 段解析+抽取流水线** 的金标准输出。

要求：让 silicon-notebook 的解析+抽取代码在该测试集上达到 gold 的效果，并 **直接上线取代现有抽取功能**。

### 1.1 现状（为何无法达到 gold）

当前 backend 只有两段、且是另一种范式：

- `parsers.py`：产出扁平 `SourceElement`（heading/paragraph/list_item/table/formula/image_caption）。**致命点**：每个元素 `" ".join(text.split())` 抹掉空白、完全不记录字符偏移；无 section tree。
- `extraction.py`：把元素拼成 9000 字符窗口喂 LLM，产出 `CandidateRecord`（payload + 元素级 `Evidence{quoted_span}`，无字符 span）；离线退化为正则启发式。
- `extraction_profiles.py`：对象类型为 rule/method/risk/case/checklist/glossary（+ claim/finding/concept/principle/example），与 gold 词表不符。

缺失：section tree、字符级 evidence atom、带类型与 atom 成员的 semantic chunk、context package、mention、canonicalization、ID 化 relation、do_not_extract、以及任何 gold schema 的输出器。

结论：这不是调参问题，需要按 qiefen 重建整条流水线。

### 1.2 harness 打分模型（决定要对齐什么）

来源：`fangan/testcases/harness/{config,scorer,stages,metrics}.py`。

| 评分桶 | 权重 | 对齐方式 |
| --- | ---: | --- |
| evidence_atoms | 0.20 | `source_span` 字符级 IoU≥0.5 对齐 + `atom_type` 严格匹配的 F1 |
| semantic_chunks | 0.15 | chunk 内 atom_id 集合 Jaccard≥0.5 对齐 + `chunk_type` 严格 F1 |
| objects（存在） | 0.12 | 证据+类型复合分≥0.4 对齐，`type` 严格 F1 |
| object_payload | 0.13 | 匹配对象 payload 字段值 P/R/F1 |
| object_evidence | 0.10 | 对象 `local_evidence_atom_ids` 集合 Jaccard |
| relations | 0.15 | 端点（经对象对齐映射）+ `relation_type` |
| context_packages | 0.05 | 每 package 的 `expected_objects` 召回（0.7）+ local_field 覆盖（0.3）|
| structure | 0.05 | section path 集合 F1（0.5）+ mentions F1（0.5）|
| do_not_extract | 0.05 | 引用/图标号等负例抑制率 |

**最硬不变量**：`源文件[char_start:char_end] == raw_text`，坐标 **绝对偏移指向原始 MinerU `.md`**。下游所有桶复用 atom 对齐映射 `atom_p2g` —— **atom 层做不对，后面约 80% 的分自动归零**。

权重的 ~45%（atoms 20 + chunks 15 + structure 5 + packages 5）可确定性做到很高；objects/relations 的 ~50% 依赖语义抽取。

### 1.3 已确认的范围决策（brainstorming）

1. **落地形态**：重构产品后端（不是独立脚本）。
2. **抽取引擎**：objects/relations/mentions 走 LLM（`OpenAICompatibleClient`，`.env` 配置的 OpenAI 兼容接口）；atoms/chunks/section/span 走确定性。
3. **里程碑**：先攻确定性 45%（P0）作为内部检查点。
4. **与旧抽取关系**：新流水线取代旧抽取。
5. **取代边界**：取代 parse→extract→存储→策展→知识浏览 核心；Q&A/检索/scenario 改指向新 object 的通用路径（`related_knowledge`），退休 rule/case 专用卡片；前端只改策展/浏览区。

### 1.4 原始源文件位置（可复现的前提）

gold 的 `source_file` 引用仓库外路径，已确认存在于本地：

- 论文：`/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md`
- 书籍：`/Users/hzf/workspace/pdf_parser/notebook_papers_mineru_skill_results/...`（见 `cmos/*/build.py` 的 `SOURCE_FILE`）

每章 `gold.yaml` 的 `source_meta.source_line_range` 给出该章在整文件中的行范围。

## 2. 架构（Approach A：隔离的 qiefen 包，每 stage 一模块）

```
backend/app/services/qiefen/
  models.py          # 镜像 gold schema 的 pydantic 类型 + QiefenDocument.to_pred_dict()
  source_elements.py # S1 带字符偏移的 SourceElement
  section_tree.py    # S2 章节树（breadcrumb path）
  atomizer.py        # S3 EvidenceAtom（精确 span + atom_type）—— 核心难点
  chunker.py         # S4 anchor-based SemanticChunk
  packager.py        # S5 ContextPackage
  mentions.py        # S6 Mention + canonicalization（LLM）
  objects.py         # S7 KnowledgeObject（LLM，内生证据绑定）
  relations.py       # S8 Relation（LLM，ID 化边）
  do_not_extract.py  # 负例抑制（确定性）
  profiles.py        # 重写的 article_research / textbook 类型词表
  emit.py            # QiefenDocument -> gold 顺序的 dict/yaml
  pipeline.py        # 编排器 run(source_file, profile, line_range?) -> QiefenDocument
```

确定性边界：S1–S5 + do_not_extract 不调 LLM；S6–S8 调 LLM。每个 stage 与一个 harness 桶 1:1，可独立驱动分数。

弃用 Approach B（在 `parsers.py`/`extraction.py` 原地扩展）的原因：旧 parser 的 `" ".join(text.split())` 从源头破坏偏移，与字符级 span 根本冲突。

## 3. 数据模型（`models.py`）

镜像 gold 顶层键（顺序固定）：`source_meta, section_tree, evidence_atoms, semantic_chunks, context_packages, mentions, canonicalization, objects, relations, do_not_extract`。

- `SourceSpan{file, line_start, line_end, char_start, char_end}` —— 偏移绝对指向原始 `.md`。
- `SourceElementQ{id, type, file, line_start, line_end, char_start, char_end, text}`。
- `SectionNode{id, path, title, parent?, kind?}`。
- `EvidenceAtom{id, section_id, atom_type, source_element_id, source_span, raw_text, normalized_text, evidence_strength, metadata?}` —— `raw_text` 逐字（不折叠空白），`normalized_text` 为渲染视图。
- `SemanticChunk{id, profile, chunk_type, section_path, atom_ids, central_atom_ids, boundary_reason, extraction_targets, gold_must_cover_atoms?}`。
- `ContextPackage{id, profile, chunk_id, section_path, document_title, atoms:[{atom_id,atom_type}], linked_context, extraction_targets, expected_objects}`。
- `Mention{id, text, type, atom_id, canonical_key}`；`Canon{canonical, aliases, note?}`。
- `KnowledgeObjectQ{id, type, section_path, home_package, payload, local_evidence_atom_ids, supporting_context_atom_ids}`。
- `RelationQ{id, relation_type, source_object_id, target_object_id, evidence_atom_ids}`。
- `DoNotExtract{text?|pattern?, examples?, atom_id?, reason, kind}`。
- `QiefenDocument{...}` + `to_pred_dict()`。

**核心不变量**（模型层强制）：`source_file[span.char_start:char_end] == raw_text`。

## 4. 确定性阶段 S1–S5（P0 核心，~45% 权重）

### S1 `source_elements.py`
读原始文件一次，建立 line→char-offset 索引；按行分组成元素：heading / paragraph / `$$…\tag{N}$$` 公式 / `<table>` 与 `<details><summary>…|…</details>` 表格 / `Figure N…` 与 `![](images/...)` 图题 / list。每元素记录精确 `[char_start, char_end]`（整文件绝对偏移）。

MinerU 渲染坑（来自 gold `analysis.md`/`parsing_notes`）：公式带 `\tag{N}`；表格为 HTML 或 details 折叠；作者上标 `$^{1,2}$` 不入原子；Unicode 箭头 `→`(U+2192)、`$O(1)$` 等在 normalized 层转 ASCII。

### S2 `section_tree.py`
heading 元素 → 节点，`path` 为 breadcrumb（如 `2. Architecture > 2.2 Sparse Retrieval`、`Chapter 2 > 2.2 pn Junction`），与 gold `>` 规范一致（按归一化 path 集合打分）。`kind` 标 example/problem 等。

### S3 `atomizer.py`（核心难点）
把元素切成 atom，带精确 span + atom_type：

- **句/子句分割**，对齐 gold 粒度，含子句拆分（如 "while X (…) we observe Y (…)" 拆成多个 result 子原子）。
- **公式** → 单个 `formula_atom`（raw 含 `\tag`，normalized 为纯公式）。
- **表格** → `table_caption_atom` + `table_header_atom` + 每行 `table_row_atom`，各自 span 落在 HTML/details 块内。
- **图题** → `figure_caption_atom`。
- **atom_type 分类**（确定性，按 profile 词表 + 线索）：
  - 论文：`we propose/introduce/instantiate→method_sentence`；`we observe/achieving/+N.N/improvement→result_sentence`；`U-shaped/scaling law→scaling_law_result_atom`；`Mechanistic/relieves/frees→mechanism_sentence`；`risk/collision/polysemy→risk_sentence`；默认 `claim_sentence`；表/图/公式按结构。
  - 书籍：`is defined as/refers to→concept_definition_atom|definition_atom`；含 `=` → `formula_atom`；`Step/then/next→process_step_atom`；`Example N-→example_problem_atom`；`Given→given_atom`；`find/calculate→formula_usage_atom`；等。
- **span 计算**：在元素切片内定位 atom raw_text 的偏移（精确算术，同 gold `build.py`）。`evidence_strength` 默认 `direct`。

**风险（明确）**：atom F1（20%）的上限取决于边界+类型与 gold 手工标注的吻合度。IoU≥0.5 给容差，但需 **用 harness 的逐章 missed/spurious atom 报告迭代该启发式**，而非一次到位。

### S4 `chunker.py`
anchor-based 语义切块（qiefen §5.3）：在 section 内按 anchor 检测 + 边界评分分组，保持 公式+说明 / 表头+表行 / 实验设置+结果 / 例题题干+解法 / 推导链 / 连续工艺步骤 同块。`chunk_type` 由 profile 词表 + 主导 atom 类型/section 赋值。输出 atom_ids / central_atom_ids / boundary_reason / extraction_targets。

### S5 `packager.py`
每 chunk 一个 ContextPackage：atoms = chunk atoms；`linked_context`（prev/next heading、table caption/headers、formula context）；`document_title`；`extraction_targets`。`expected_objects` 在 S7 后回填。

## 5. LLM 阶段 S6–S8 + profiles 重写

### profiles 重写（`profiles.py`，替换 `extraction_profiles.py` 旧六类）
采用 gold 类型词表，类型名严格对齐（type-strict 打分）：

- **article_research** objects：`ArticleClaim, ArticleMethod, ArchitectureComponent, ScalingLaw, ExperimentSetup, ExperimentResult, AblationFinding, MechanisticExplanation, SystemDesignClaim, Limitation, Implication`。relations：`method_has_component, component_mitigates_risk, method_addresses_problem, result_supports_claim, experiment_tests_claim, ablation_supports_component_importance, mechanism_explains_result, system_design_enables_efficiency, claim_guided_by_scaling_law` 等。
- **textbook** objects：`Concept, Definition, Formula, Variable, Derivation, ExampleProblem, ExampleSolution, TechnologyProcess, ProcessFlow, ComponentModel, PhysicalEffect, DesignPrinciple, DesignRule, ProblemStatement`。relations：`concept_defines_term, formula_defines_variable, formula_derived_from_formula, formula_used_in_example, process_flow_has_step, process_step_precedes_step, circuit_block_composed_of_block, component_has_property, design_principle_applies_to_scenario` 等。

每类型的 payload 字段形状取自 gold `objects[].payload`。

### S6–S8（每 ContextPackage 调 `OpenAICompatibleClient`）
- `objects.py`：prompt 喂入 package 的 atoms **带 id**；模型返回带类型对象，从这些 id 里选 `local_evidence_atom_ids`（实现 qiefen「证据绑定内生化」），每个 payload 字段需被证据支撑。`supporting_context_atom_ids` 指向其它 chunk 的 atom。S7 后回填各 package 的 `expected_objects` 与对象 `home_package`。
- `mentions.py` / canonicalization：实体 span → type + canonical_key；别名合并。
- `relations.py`：在对象间产出 ID 化边，`relation_type` 取 profile relation 词表，带 `evidence_atom_ids`，端点必须是本文件 object id。
- `do_not_extract.py`（**确定性**负例）：作者-年份引用 `(Author, 2025)`、图/表引用、URL、netlist label 等列入抑制面。

prompt 硬约束：只用列出的类型、只引用 package 内存在的 atom id、不得编造。无 LLM key 时该层退化（产出空 objects/relations，确定性桶仍可评分）。

## 6. Emit、harness 适配、上线切换

### Emit + harness 适配
`emit.py` 将 `QiefenDocument` 按 gold 顺序导出 YAML。适配脚本（`scripts/` 下）对 14 章：用 gold `source_meta.source_line_range` 在 **整文件** 上切该章行范围 → 跑 pipeline（span 保持整文件绝对偏移）→ 把 `pred.yaml` 写进镜像 `engram/chXX cmos/chXX` 的候选目录 → 跑 `python -m harness.run_all` 得均分。

### 上线切换（「取代」）
- DB 迁移：新增 `q_atoms / q_chunks / q_packages / q_relations`；objects 落 `knowledge_objects`（新 `object_type` 值 + `section_path/home_package/local_evidence`）。
- `process_source`：parse(整文档) → qiefen pipeline → 持久化各层；策展候选 = objects；approve/reject 作用于 objects。
- profiles 注册表：用新词表替换 `OBJECT_SCHEMAS` 旧六类；`object_schemas` 表重新 seed。
- 路由：保留通用 `list_knowledge/knowledge_types/find_duplicates/update_knowledge`；`ask/scenario/checklist/case_search/retrieval` 改走通用 object 路径（`related_knowledge`）；退休 `list_rules/methods/risks/glossary/explain_rule`。
- 前端 `page.tsx`（单文件 2826 行）：策展 + 知识浏览区切到通用 type-tab/object/relation + atom 证据视图（`knowledge_types`+`KnowledgeRecord` 通用路径已存在）；移除 rule/case 专用卡片。

## 7. 分期（同一轮工作内的实现顺序）

- **P0**：S1–S5 + emit + harness 适配；确定性桶（atoms/chunks/structure/packages/do_not_extract）跑绿。内部检查点。
- **P1**：LLM S6–S8（mentions/objects/relations/canon）；补齐 ~50%。
- **P2**：上线切换接线（routes/repository/migration/前端）；退休旧抽取。

上线 = 完整流水线（P0→P1→P2 全做完）；P0 只是先验证核心的内部检查点。

## 8. 测试策略

- **TDD**：每 stage 对 gold 字段写单测（至少 1 个 engram + 1 个 cmos 章）：S1 元素偏移、S2 section path、S3 atom span/类型、S4 chunk atom 集、S5 package。
- **集成回归**：harness `run_all` 作为端到端回归。
- **目标分**：在 **第一次端到端跑出确定性基线后** 设定具体目标（gold-vs-gold=100），不预先猜测。先确保确定性桶达到可观水平，再迭代 objects/relations。

## 9. 主要风险

1. **atom 边界/类型与 gold 吻合度**（S3）—— 决定 20% 桶及下游对齐。缓解：harness 逐章报告驱动迭代。
2. **章节切分（行范围）**—— 产品侧整文档无 gold 行范围；harness 侧用 gold 行范围切片，二者一致性需保证 span 绝对偏移。
3. **LLM 类型/payload 与 gold 严格对齐**—— prompt 词表约束 + 字段模板。
4. **上线切换的下游耦合**—— Q&A/检索/前端改走通用路径，rule/case 专用面退休，可能改变这些产品功能的语义（已确认接受）。

## 10. 非目标（YAGNI）

- 不做 schema induction（新类型自动提案）—— 沿用 closed 词表。
- 不重写 Q&A/scenario/checklist 的业务语义到新模型（仅改走通用 object 路径）。
- 不做审核反馈回流优化（qiefen §12 P4）本轮不实现。
