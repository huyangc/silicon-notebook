# 测试用例三元组生成规范（agent 契约）

本规范定义每个 **章节三元组** 的产出格式。目标：为"文档切分 → 实体识别 → 关系识别"
抽取算法（方案见 `fangan/qiefen.md`）构造 gold 测试用例。

每个章节产出**恰好三个文件**，放进该章节目录：

```
<chapter_dir>/
  source.md     # 原始文件段落（verbatim 原文）
  gold.yaml     # golden parse result（结构化金标准）
  analysis.md   # 逐 stage 分析
```

## 流水线 stage（qiefen.md）

```
SourceElement → SectionTree → EvidenceAtom → SemanticChunk → ContextPackage
              → Mention → Object(KnowledgeObject) → Relation
```

## 1) source.md —— 原始文件段落

- **逐字复制**源 markdown，不要改写/翻译/总结。
- 包含：标题(`#`)、关键段落、公式(`$$...\tag{N}$$` / `$...$`)、表格(HTML `<table>` 或
  `<details><summary>…</summary>| … |</details>`)、图题(`Figure N | ...` 或 `![](images/...)` 邻近文字)。
- 每段前用 HTML 注释标注源行号区间：`<!-- lines 117-148 -->`。
- 大章节可只收**高价值段落**（你 gold 里 atom 引用到的内容），但必须保证
  **每个 evidence_atom 都能在 source.md 中找到对应原文**。

## 2) gold.yaml —— golden parse result

顶层键（顺序固定）：`source_meta, section_tree, evidence_atoms, semantic_chunks,
context_packages, mentions, canonicalization, objects, relations`。

- **source_meta**: `{ source_id, scope, title, profile, profile_detected_as, profile_cues:[...],
  extraction_targets:[...], parsing_notes:[...] }`
- **section_tree**: `[{ id, path, title, parent?, kind? }]`（kind: example/problem 等）
- **evidence_atoms**: `[{ id, section_id, atom_type, text, metadata?{tag,role,table_id,...} }]`
  - `text` 用 ASCII 安全写法：phi/rho/eps/mu/Ohm/deg；`>=` `<=` `x`(乘) 等；公式写成
    `C_j = C_j0/[1-(v_D/phi_o)]^m` 这种纯文本。
  - `section_id` 必须是 section_tree 里存在的 id。
  - atom_type 词表——
    - 论文型: `claim_sentence, method_sentence, risk_sentence, mechanism_sentence,
      formula_atom, experiment_setup_atom, result_sentence, scaling_law_result_atom,
      table_caption_atom, table_header_atom, table_row_atom, figure_caption_atom,
      ablation_finding_atom, limitation_sentence`
    - 书籍型: `concept_definition_atom, definition_atom, formula_atom, derivation_step_atom,
      condition_atom, technology_process_atom, process_flow_atom, process_step_atom,
      example_problem_atom, given_atom, formula_usage_atom, result_atom,
      table_caption_atom, table_header_atom, table_row_atom, physical_effect_atom,
      design_principle_atom, design_rule_atom, problem_statement_atom, summary_atom`
- **semantic_chunks**: `[{ id, profile, chunk_type, section_path, atom_ids:[...],
  central_atom_ids:[...], boundary_reason, extraction_targets:[...], gold_must_cover_atoms:[...] }]`
  - chunk_type 词表——
    - 论文型: `article_core_claim_block, architecture_component_block, formula_definition_block,
      experiment_setup_block, experiment_result_block, scaling_law_block, ablation_finding_block,
      system_efficiency_block, related_work_comparison_block, conclusion_block`
    - 书籍型: `chapter_overview_block, concept_definition_block, design_process_block,
      hierarchy_table_block, technology_process_block, process_flow_block, derivation_block,
      formula_definition_block, example_solution_block, circuit_hierarchy_block,
      component_model_block, physical_effect_block, design_principle_block, layout_rule_block,
      problem_set_block`
  - `boundary_reason` 要写清为何在此切/不切（参考 qiefen §5.3：保持公式-说明、表头-表行、
    实验设置-结果、例题题干-解法、推导链、工艺连续步骤 在同块）。
  - 每个 evidence_atom 至少出现在一个 chunk 的 `atom_ids` 中。
- **context_packages**: 至少一个。`{ id, profile, chunk_id, section_path, document_title,
  atoms:[{atom_id, atom_type}], linked_context:{table_caption?,table_headers?,formula_context?,
  previous_heading?,next_heading?}, extraction_targets:[...] }`
- **mentions**: `[{ id, text, type, atom_id, canonical_key }]`
- **canonicalization**: `[{ canonical, aliases:[...], note? }]`
- **objects**(KnowledgeObject): `[{ id, type, section_path, payload:{...}, evidence_atom_ids:[...], confidence }]`
  - payload 字段必须被所引 atom 支撑，**不得编造**。
  - 对象 type 见 qiefen §2.1(论文)/§2.2(书籍)：ArticleClaim/ArticleMethod/ArchitectureComponent/
    ExperimentSetup/ExperimentResult/AblationFinding/ScalingLaw/MechanisticExplanation/
    SystemDesignClaim/Limitation/Implication ；Concept/Definition/Formula/Variable/Derivation/
    ExampleProblem/ExampleSolution/TechnologyProcess/ProcessFlow/ComponentModel/PhysicalEffect/
    DesignPrinciple/DesignRule/ProblemStatement 等。
- **relations**: `[{ id, relation_type, source_object_id, target_object_id, evidence_atom_ids:[...], confidence }]`
  - 端点必须是本文件 objects 里定义的 id。
  - relation_type 见 qiefen §8.1(论文)/§8.2(书籍)，如 `method_has_component, component_mitigates_risk,
    result_supports_claim, ablation_supports_component_importance, formula_derived_from_formula,
    formula_defines_variable, formula_used_in_example, process_flow_has_step,
    circuit_block_composed_of_block, component_has_property, design_principle_applies_to_scenario` 等。
    确有需要的新类型可用，但要语义清晰。

## 3) analysis.md —— 逐 stage 分析

按顺序说明本章：profile 判定 → SourceElement 要点(MinerU 渲染特性/坑) →
Section Tree → EvidenceAtom 分布 → SemanticChunk 表(chunk_type + boundary_reason) →
一个 ContextPackage 示例 → Mention/Object/Relation 概览。引用 gold.yaml 里的 id。

## 硬性约束（务必自检）

1. 一切以原文为准，引用真实数字/公式/表格，**绝不编造**。
2. 引用完整性：atom.section_id∈section_tree；chunk/package/object/mention 引用的 atom∈evidence_atoms；
   relation 端点∈objects；每个 atom 至少进一个 chunk。
3. YAML 必须可被 `python3 -c "import yaml;yaml.safe_load(open('gold.yaml'))"` 解析；
   含冒号的字符串要加引号。
4. ASCII 安全：φ→phi, ρ→rho, ε→eps, μ→mu, Ω→Ohm, ×→x, ≥→>=, °→deg。
5. 深度按章节规模标定：短章(abstract/intro/conclusion) ~5–15 atoms；大章节做到全面(40–90 atoms)，
   对标参考样例 `cmos/ch02_cmos_technology/`。

## 参考样例

- 方案：`fangan/qiefen.md`
- 完整样例三元组（书籍型大章节）：`fangan/testcases/cmos/ch02_cmos_technology/`
