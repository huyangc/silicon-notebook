# 书籍 / 教材型（`textbook`）Gold Fixture 规范与修订过程

本文件沉淀「书籍/教材型」测试用例（gold fixture）的**最终要求**与**修订过程**。
参考实现：`fangan/testcases/cmos/ch02_cmos_technology/`（CMOS Analog Circuit Design 第 2 章，schema `v0.3.3-textbook`），
另有 `cmos/ch01,ch03,ch04,ch09` 四章同标准。方案背景见 [`qiefen.md`](./qiefen.md)。
论文型（`article_research`）见 [`article_research_gold_spec.md`](./article_research_gold_spec.md)。

> **与论文型共享**：第 0–3 部分的**流程、坐标约定、不变量**与论文型规范**完全一致**（profile-agnostic）；
> 本文不重复细节，只给出**差异点**：textbook 的对象/关系/atom/chunk **类型词表**、以及教材特有的
> 解析坑（表格、图、跨节公式编号）与负例。通用约束以论文型规范为准。

> 一句话定位：gold 权威坐标绑定原始 MinerU 文件；`source.md` 仅 viewer；`raw_text` 逐字、`normalized_text`
> 只渲染该 span（公式 atom = **纯公式**，语义在对象层）；对象证据分 local/supporting；标签用
> gold_label/difficulty/evidence_strength；显式给出 do_not_extract 负例；全局无孤儿 atom。

---

# 第一部分　修订过程（v0.1 → v0.3.3-textbook）

## v0.1 —— 教材整章结构化（方向对，未达严格）
- 正确识别为 `textbook` profile，激活教材型对象类型（Concept/Formula/Derivation/TechnologyProcess/
  ProcessFlow/Example/ComponentModel/PhysicalEffect/DesignPrinciple/DesignRule/ProblemStatement）。
- 整章覆盖（2.1 工艺、2.2 pn 结、2.3 MOS、2.4 无源器件、2.5 其它考量、2.6 版图、2.7 小结、Problems）。
- 与论文型 v0.1 相同的缺陷：evidence atom 是摘要非原文；无坐标；用小数 confidence；无负例。

## v0.3.3-textbook 第一轮 —— 套用论文型 v0.3.3 流程（保留 textbook 类型）
- 双坐标（`source_span` 权威 / `viewer_span` 仅 viewer）；逐字 `raw_text` + 限 span 的 `normalized_text`；
  `source_elements`；对象 `local_evidence`/`supporting_context`/`home_package`；每 chunk 一个 package
  （`atoms==chunk.atom_ids` + `expected_objects`）；`gold_label`/`difficulty`/`evidence_strength`；
  `do_not_extract`；conventions/validation 块。
- 教材特有处理：**公式 tag 按小节重置**，按 `\tag {N}` 在小节行窗内定位；表格 HTML `<table>` 与 `<details>`；
  N-Well 流程子步骤用 anchored 子串；MinerU 拆数字 `2 0 0`、U+2212 `−4 V`、希腊 `Κ`、U+2126 `Ω`。

## v0.3.3-textbook review 修订
- **漏标补全**：`PKG-FAB-PHOTO` 补 `TECH-PHOTOLITHOGRAPHY` + `CONCEPT-PRINTING-SYSTEMS`；补 `PROB-8`。
- **关系类型纠偏**：`formula_quantifies_effect`（公式→PhysicalEffect）、`formula_uses_parameter_formula`
  （公式→公式）、`design_principle_applies_to_component`、新增 **`Tradeoff`** 与 **`ChecklistCandidate`** 对象类型
  （`TRADEOFF-LATCHUP-AREA`、`CHECK-CAP-SELECTION`）使关系端点类型匹配。
- **孤儿归位**：N-Well 子步骤并入 `PROCESS-NWELL-CMOS`、Table 2.3-1 并入 `DERIV-THRESHOLD-VOLTAGE`；
  新增 `FORMULA-PEAK-FIELD`/`PHYS-REVERSE-BREAKDOWN`/`FORMULA-REVERSE-CURRENT`/`PHYS-CAP-COEFFICIENTS`/
  `PRINCIPLE-ESD-PROTECTION` 等；章引/小结标 `context_only:true`。
- **P0.5**：所有 formula/condition atom 的 `normalized_text` 严格收紧为**纯公式**（移除 `(carrier mobility
  temperature dependence)`、`(Sah equation)`、`Shot noise:` 等 span 外标签，语义由对象 `payload.name/role` 承载）；
  去掉 `PRINCIPLE-LATCHUP-PREVENT.payload.tradeoff`（与 `TRADEOFF-*` 对象去重）；`PKG-OV` 加 `note`。

**结论**：5 章（ch01/02/03/04/09）均为 `v0.3.3-textbook`，过全套不变量。

---

# 第二部分　最终要求（schema `v0.3.3-textbook`）

文件结构（三元组 source.md / gold.yaml / analysis.md）、`gold.yaml` 顶层键顺序、`source_meta`/`source_elements`/
`section_tree`/`evidence_atoms`/`semantic_chunks`/`context_packages`/`mentions`/`canonicalization`/`objects`/
`relations`/`do_not_extract` 的**字段定义与论文型一致**（见论文型规范第二部分）。
`schema_version` 用 `"0.3.3-textbook"`，`source_meta.profile: textbook`。

下面只列 **textbook 的差异**。

## 2.1 类型词表（textbook）

**atom_type**：
`concept_definition_atom, definition_atom, formula_atom, derivation_step_atom, condition_atom,
technology_process_atom, process_flow_atom, process_step_atom, example_problem_atom, given_atom,
formula_usage_atom, result_atom, table_caption_atom, table_header_atom, table_row_atom,
physical_effect_atom, design_principle_atom, design_rule_atom, problem_statement_atom, summary_atom,
structure_atom`。

**chunk_type**：
`chapter_overview_block, concept_definition_block, design_process_block, hierarchy_table_block,
technology_process_block, process_flow_block, derivation_block, formula_definition_block,
example_solution_block, circuit_hierarchy_block, component_model_block, physical_effect_block,
design_principle_block, layout_rule_block, problem_set_block, cross_reference_block`。

**object type**（qiefen §2.2 + 已用扩展）：
`Concept, Definition, Formula, Variable, Derivation, ExampleProblem, ExampleSolution,
TechnologyProcess, ProcessFlow, ComponentModel, PhysicalEffect, DesignPrinciple, DesignRule,
ProblemStatement, Tradeoff, ChecklistCandidate, IntermediateRepresentation`。
（`Tradeoff` / `ChecklistCandidate` 为本规范引入的扩展类型——前者承载设计权衡，后者承载由原则派生的检查项。）

**relation_type**（qiefen §8.2 + 已用扩展）：
`concept_defines_term, concept_contrasts_with_concept,
formula_defines_variable, formula_depends_on_variable, formula_derived_from_formula,
formula_used_in_example, formula_quantifies_effect, formula_uses_parameter_formula,
derivation_produces_formula,
process_flow_has_step, process_step_precedes_step, process_step_creates_structure,
process_step_mitigates_issue, process_step_has_nonideality, technology_process_defines_formula,
circuit_block_composed_of_block, component_has_property, component_has_nonideality, component_has_mechanism,
design_principle_applies_to_scenario, design_principle_applies_to_component,
design_principle_has_tradeoff, design_principle_mitigates_effect,
checklist_candidate_derived_from_principle, problem_extends_example, problem_uses_formula,
model_refines_model`（ch03 SPICE 模型链）等。扩展类型须语义清晰并在 analysis 说明。

> **类型匹配硬规则**（review 教训）：`formula_defines_variable` 的 target 必须是 `Variable`；指向 `PhysicalEffect`
> 用 `formula_quantifies_effect`；指向另一公式用 `formula_uses_parameter_formula` / `formula_derived_from_formula`。
> `design_principle_has_tradeoff` 的 target 必须是 `Tradeoff` 对象（不是被缓解的 `PhysicalEffect`）。
> `checklist_candidate_derived_from_principle` 是 `ChecklistCandidate -> DesignPrinciple`。

## 2.2 教材特有 `source_meta.conventions`

在论文型 conventions（coordinate_policy / raw_vs_normalized / object_evidence / expected_local_fields /
labels / packages / context_only）基础上，增加：

```yaml
tables_and_figures: "表格渲染为 HTML <table> 或 <details> 块；电路剖面/版图等图渲染为 <details>
  text_image/flowchart/chemical 的 OCR 块。表对象：table_caption_atom 取 'Table X' 标题行，
  table_header_atom/table_row_atom 取 <table> 行内 anchored <tr>/<td> 子串。图内 OCR 标签、
  mermaid 节点文字、inline [n] 引用一律进 do_not_extract。"
```

`raw_vs_normalized` 在 textbook 下额外强调：MinerU 把数学内数字拆成 `2 0 0` / `0. 3 0 6`，
施加电压用 U+2212 `−4 V`，温度用希腊 `Κ`，欧姆用 U+2126 `Ω`（≠ U+03A9）——`raw_text` 逐字保留，
`normalized_text` 给 ascii 可读式。**formula_atom / condition_atom 的 normalized 必须是纯公式**
（不得带 `(...)` 语义标签或 `Label:` 前缀；语义放 object `payload.name/role`）。

`parsing_notes` 必含：
- 「公式 tag 在每个小节内从 1 重新编号；formula atom 必须以 `\tag {N}` 在该 atom 所属小节的行窗内定位」。
- 「N-Well 等多步骤流程是单个段落 element，子步骤用 anchored 子串」（视章节）。
- 「表 X 是单行 HTML `<table>`；若带续写文本列表（如 Table 2.6-1）则 design_rule atom 跨二者」。

## 2.3 教材特有 `do_not_extract` 负例

```yaml
- { pattern: bracket_reference_citation, examples: ["[24,25]","[12]","[1,2,3,4]"], kind: citation_policy,
    reason: "inline 数字引用不是知识对象" }
- { pattern: figure_or_equation_cross_reference, examples: ["Fig. 2.2-1","Eq. (12)","Figure 2.5-3"],
    kind: reference, reason: "图/式交叉引用是指针，不抽成对象" }
- { pattern: figure_internal_label, examples: ["n-well implant","p- substrate","FOX","Si3N4"],
    kind: figure_label, reason: "<details> 图块内 OCR 标签不是知识对象" }
- { pattern: mermaid_flowchart_node_label, examples: ["Operational Amplifier","graph TD","-->"],
    kind: figure_label, reason: "层级图 mermaid 节点文字；composition 用 circuit_block_composed_of_block 表达" }
- { pattern: image_markup, examples: ["![](images/<hash>.jpg)"], kind: image_markup }
- { pattern: out_of_chapter_cross_reference, examples: ["Appendix B","examined in more detail in the next chapter","Chapter 9"],
    kind: cross_reference, reason: "指向其它章/附录，不是本章对象" }
- { pattern: spice_netlist_table, kind: noise, reason: "9.7 PSpice netlist 大 <table> 是噪声，整体排除" }   # ch09
```

## 2.4 教材常见对象/关系范式（参考样例已实现）

- **工艺步骤**：五大工艺 → `TechnologyProcess`；`TechnologyProcess --technology_process_defines_formula--> Formula`（selectivity/anisotropy）。
- **工艺流程**：N-Well → `ProcessFlow`（payload.steps 有序）；`process_flow_has_step` / `process_step_has_nonideality`（LOCOS→bird's beak）。
- **公式链/推导**：`Derivation`（payload.steps）`--derivation_produces_formula--> Formula`；`formula_derived_from_formula`（C_j←Q_j）。
- **例题**：`ExampleSolution`（problem/given/approach/result）；`formula_used_in_example`；`problem_extends_example` / `problem_uses_formula`。
- **电路层级**：`circuit_block_composed_of_block`（op amp ← diff amp/current mirror/…；SC filter ← biquad ← integrator ← SC resistor）。
- **器件/物理效应**：`ComponentModel`、`PhysicalEffect`；`component_has_property` / `component_has_nonideality` / `component_has_mechanism`。
- **设计原则/权衡/检查项**：`DesignPrinciple`；`design_principle_applies_to_component` / `design_principle_mitigates_effect`；
  `design_principle_has_tradeoff -> Tradeoff`；`checklist_candidate_derived_from_principle: ChecklistCandidate -> DesignPrinciple`。
- **章引/小结**：`A-INTRO` / `A-SUMMARY` 标 `context_only:true`，其所在 package `expected_objects: []` + `note`。

---

# 第三部分　不变量与校验清单（与论文型一致）

逐条同论文型规范第三部分（1–14），对每个 textbook gold.yaml 必须全部通过：
YAML 可解析；**权威 span** `source_file[source_span.char_*] == raw_text`（必跑）；viewer span（可选）；
结构引用（section_id/source_element_id）；chunk 覆盖；`package.atoms == chunk.atom_ids`；
`expected_objects ⊆ objects` 且 `expected_local_fields` 字段 ⊆ payload；
对象 `local ⊆ home-chunk & supporting ⊄ home-chunk`，且对象 ∈ 其 `home_package.expected_objects`；
关系端点 ∈ objects；mention / do_not_extract 引用解析；**无 `confidence`**；
**raw/normalized 跨 span 审计**（数字 + 非公式 atom 不引入 span 外事实；formula atom 经 `supported_by_context_atoms` 例外，且 normalized 为纯公式）；
**全局无孤儿**：每个 atom ∈ object.local ∪ object.supporting ∪ relation.evidence，或 `context_only:true`，
或属于 `core:false` 的 `cross_reference_block` 且被 `do_not_extract` 以 `ref` 引用。

> 教材专项审计（参考样例已内置于各章 `validate.py`）：
> (a) **公式无 gloss**：formula_atom / condition_atom 的 normalized 不得含 span 外 `(...)` 语义标签或 `Label:` 前缀
>     （但允许公式自身的运算括号，如 `(v_GS - V_T)`、`(gamma - alpha)`、`(lateral/vertical)`）。
> (b) **数字去空格审计**：因 MinerU 拆数字，比对前先合并 `\d\s+\d`（`2 0 0`→`200`、`0. 3 0 6`→`0.306`）。

---

# 第四部分　评估指标（qiefen §13，textbook 侧重）

- **profile 检测**：`textbook`（Chapter/Example/PROBLEMS/Eq.(N)/Table/Fig.）。
- **切分质量**：section_tree（含 run-in 子标题与 Example/PROBLEMS 节点）、evidence_atoms、semantic_chunks
  （`gold_must_cover_atoms` 给 Object Integrity）→ Evidence Recall@Chunk、Over/Under-splitting。
- **证据绑定**：`source_span` + `raw_text` → 公式（按 tag 定位）、表行、例题步骤、工艺步骤的原文定位。
- **抽取质量**：objects（local/supporting）、`context_packages.expected_objects` / `expected_local_fields`
  → Concept/Formula/Derivation/Process/Example/Principle/Rule 抽取的 Object/Field P/R，
  Formula Variable Accuracy、Table Row Parsing、Example Step Accuracy。
- **关系质量**：relations（端点类型匹配 + evidence）→ Endpoint Validity / Type Accuracy
  （尤其 `circuit_block_composed_of_block` 的层级、`formula_*` 的端点类型、`design_principle_has_tradeoff -> Tradeoff`）。
- **负例控制**：do_not_extract → over-extraction（[n] 引用、Fig./Eq. 交叉引用、图内 OCR 标签、mermaid 节点、PSpice netlist）抑制率。

---

# 附：两套 fixture 现状

```
engram/  article_research   9 章 (ch00..ch08)   schema v0.3.3
cmos/    textbook           5 章 (ch01/02/03/04/09)   schema v0.3.3-textbook
```
两者合计覆盖两类文档，端到端支撑 文档切分 → EvidenceAtom → SemanticChunk → KnowledgeObject → Relation 的评测；
参考实现分别为 `engram/ch02_architecture/` 与 `cmos/ch02_cmos_technology/`。
