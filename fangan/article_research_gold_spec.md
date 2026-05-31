# 学术论文型（`article_research`）Gold Fixture 规范与修订过程

本文件沉淀「学术论文型」测试用例（gold fixture）的**最终要求**与**修订过程**。
参考实现：`fangan/testcases/engram/ch02_architecture/`（Engram 论文第 2 章 Architecture，schema v0.3.3）。
方案背景见 [`qiefen.md`](./qiefen.md)。书籍/教材型（`textbook`）见 [`textbook_gold_spec.md`](./textbook_gold_spec.md)。

> 一句话定位：gold 的**权威坐标绑定原始 MinerU 文件**；`source.md` 只是人类可读的 viewer 切片；
> `raw_text` 是逐字 span，`normalized_text` 只渲染该 span；对象证据分 local/supporting；
> 标签用 gold_label/difficulty/evidence_strength；并显式给出 do_not_extract 负例。

---

# 第一部分　修订过程（v0.1 → v0.3.3）

每一轮都是「人工 review → 技术核对 → 修正 → 脚本校验」。以下是各版本解决的核心问题。

## v0.1 —— 语义结构化答案（不可严格评测）
- 有 section_tree / evidence_atoms / semantic_chunks / objects / relations，ID 引用自洽，YAML 可解析。
- 问题：evidence atom 是**改写后的摘要**而非原文 span；无 line/char 坐标；图表归属错误；
  关系语义不够精确；context package 覆盖不全；用小数 `confidence`；无负例。
- 结论：可作 v0.1 人工样例，**不可作严格评测 gold**。

## v0.2 —— 引入证据 span 与图归属
- evidence atom 加 `raw_text`（原文逐字）+ `normalized_text`（可读化）+ `source_line/char span` + `source_element_id`；新增顶层 `source_elements`。
- **Figure 物理 vs 语义归属**：Figure 2（系统实现图）物理在 2.3、语义属 2.5；Figure 3（scaling）是对 §3 的**前向引用**，标 `include_in_chapter2_core_chunks:false` 并隔离进 `cross_reference_block`。
- 新增 **Sparse Retrieval / Fusion 两个相位父级组件**（`method_has_phase` → `component_has_component`），不再把所有组件平铺到 method 下。
- 关系类型收紧（`component_has_mechanism` 等）；用 `gold_label/difficulty/evidence_strength` 取代小数 confidence；新增 `do_not_extract`。

## v0.3 —— 双坐标 + raw/normalized 一致性 + 对象补全
- **双坐标**：`orig_*`（原始文件）+ `slice_*`（source.md），二者切片都等于 `raw_text`。
- raw_text 补全到能完整支撑 normalized（例：公式 atom 不再夹带公式外信息）；新增支撑 atom（`A-MH-CONCAT` 支撑 Eq.2 的 concat 读法、`A-GATE-RMSNORM` 支撑 Eq.3/4 上下文）。
- context_packages 覆盖全部核心 chunk，且 `atoms == chunk.atom_ids`，并带 `expected_objects`。
- 新增对象类型：`Formula`（Eq.1–6）、`ExperimentSetup`、`IntermediateRepresentation`。

## v0.3.1 —— 明确「权威坐标 = 原始文件，source.md 仅 viewer」
- 坐标拆成 `source_span`（**权威**，file=source_file）+ `viewer_span`（`viewer_only:true`，file=source.md）。
  `source_meta.conventions.coordinate_policy` + `validation` 明确：**必跑** source_span 校验，viewer_span 可选/可漂移。
- 修 `normalized_text` 跨 span 补全：`A-OV-PHASES` 去掉 "(Section 2.2/2.3)"；按方案 B 新增 `A-OV-ROADMAP`（承载 phase→section 映射）；把插入的主语（Engram/Context-aware gating）改回 "we"/被动；`A-TC-REDUCTION` 用 "this process"。
- `raw_vs_normalized` convention 增加「仅 formula_atom 可经 `supported_by_context_atoms` 携带符号解释」的例外条款。

## v0.3.2 —— 对象证据 local/supporting + 公式拆分 + 负例补全
- 对象证据拆成 `local_evidence_atom_ids`（在对象 `home_package` 的 chunk 内）+ `supporting_context_atom_ids`（来自其它 chunk）+ `home_package`；**`expected_objects` 只按 local 证据考核**。
- 拆分 `A-GATE-RMSNORM` → `A-GATE-WKV-DEF`（W_K/W_V 定义，供 `FORM-EQ3`）+ `A-GATE-RMSNORM`（RMSNorm/梯度稳定，供 `FORM-EQ4`），使公式证据纯净。
- `do_not_extract` 补 `(Xie et al., 2025)`、两条 Figure 2 子图 label、inline-citation policy；
  `R-07 += A-MB-BACKBONE`、`R-16 += A-FIG2`、`R-05 += A-OV-ROADMAP/A-GATE-OUT`；
  package 增加 `expected_local_fields`（跨 package 对象的「本 package 可抽字段」）。

## v0.3.3 —— 收尾微调
- `IMPL-DECOUPLED-SCALING` 的 local evidence 补 `A-SYS-INFER`（payload 含 inference）。
- `inline_author_year_citation` 的 do_not_extract 规则补 `examples` 列表（便于统一归类 citation over-extraction）。
- 4 个跨 package 对象的 `expected_local_fields` 全覆盖；RMSNorm 维持在 `FORM-EQ4`（gating 经 `R-12 component_defined_by_formula` 可达，对象本体不重复挂）。

**结论**：v0.3.3 可作为严格评测 gold。最关键的几个硬问题已解决——
权威坐标绑定原始文件、raw/normalized 约束清楚、对象证据 local/supporting 解决跨 package 歧义、
公式 atom 纯净、图归属正确、关系证据完整、负例覆盖前向引用/子图 label/citation。

---

# 第二部分　最终要求（schema v0.3.3）

## 0. 三元组与文件职责

每个章节一个目录，含三个文件：

| 文件 | 职责 |
| --- | --- |
| `source.md` | **viewer-only** 的逐字切片（原始文件对应行范围的 verbatim 拷贝，含头部注释声明非权威）。仅供人工 review / UI 跳转。 |
| `gold.yaml` | **权威** golden parse result。所有严格评测坐标指向 `source_file`（原始 MinerU `.md`）。 |
| `analysis.md` | 逐 stage 分析 + 本版相对上一版的差异表。 |

## 1. `gold.yaml` 顶层键（顺序固定）

```
schema_version
source_meta
source_elements
section_tree
evidence_atoms
semantic_chunks
context_packages
mentions
canonicalization
objects
relations
do_not_extract
```

## 2. `source_meta`

```yaml
source_meta:
  source_id: engram
  scope: "Chapter 2 (Architecture) only"
  title: ...
  source_file: engram_paper_mineru.md        # 权威文件名
  source_line_range: [31, 109]
  viewer_file: "source.md (verbatim slice; NOT authoritative)"
  profile: article_research
  profile_detected_as: article_research
  profile_cues: [...]
  extraction_targets: [ArticleClaim, ArticleMethod, ArchitectureComponent, Formula,
                       ExperimentSetup, IntermediateRepresentation, MechanisticExplanation,
                       SystemDesignClaim, Implication, Limitation, Risk]
  conventions:
    coordinate_policy: "权威坐标 = atom.source_span（file=source_file）；评测必须校验
      source_file[source_span.char_start:char_end] == raw_text。viewer_span（file=source.md）
      可选/viewer_only，不得作为主评测坐标。"
    raw_vs_normalized: "raw_text 是 source_file 的逐字连续 span；normalized_text 只渲染该 span
      （可改写、转写数学符号 phi/rho/eps/mu、消解 span 内代词），不得引入 span 外的事实/名称/章节引用。
      例外：formula_atom 可经 metadata.supported_by_context_atoms / interpretation_supported_by
      携带由其它 atom 提供的符号解释。"
    object_evidence: "对象证据 = local_evidence_atom_ids（在 home_package 的 chunk 内）
      ∪ supporting_context_atom_ids（来自其它 chunk）。package object-recall 只按 local 考核。"
    expected_local_fields: "跨 package 对象可在 package 内声明 expected_local_fields[object_id]
      = 仅凭该 package 原子可抽出的 payload 字段。"
    labels: "对象/关系用 gold_label + difficulty + evidence_strength，不用小数 confidence。"
    figures: "figure atom 区分 physical_section_id 与 semantic_section_ids；前向引用设
      include_in_chapter2_core_chunks:false 并隔离进 cross_reference_block。"
    external_evidence: "仅在本章陈述、证据在他处的 atom 设 requires_external_evidence:true
      + external_evidence_ref。"
    packages: "context_package.atoms 即 LLM 输入，等于其 chunk 的 atom_ids；expected_objects
      列该输入应抽出的对象 id。"
  validation:
    required: "for every atom: source_file[source_span.char_start:char_end] == raw_text"
    optional_debug: "for every atom with viewer_span: source.md[viewer_span...] == raw_text"
  parsing_notes: [ ... MinerU 渲染坑：公式 $$...\tag{N}$$ 编号规则、图为 'Figure N | ...' 纯文字段、
                   run-in 子标题、跨页断行、特殊符号等 ... ]
```

## 3. `source_elements`（SourceElement 阶段，§3.1）

```yaml
source_elements:
  - { id: SE-2.1-OV, type: paragraph,      file: engram_paper_mineru.md, line_start: 35, line_end: 35 }
  - { id: SE-2.2-EQ1, type: formula,       file: ..., line_start: 47, line_end: 49, metadata: {tag: "1"} }
  - { id: SE-FIG2,    type: figure_caption, file: ..., line_start: 84, line_end: 84 }
```
`type ∈ {heading, paragraph, formula, table, figure_caption}`。

## 4. `section_tree`（§3.2）

```yaml
- { id: SEC-2,   path: "2",        title: "Architecture" }
- { id: SEC-2.2, path: "2 > 2.2",  title: "Sparse Retrieval via Hashed N-grams", parent: SEC-2 }
- { id: SEC-2.2-TC, ..., kind: run_in_heading }          # 行内加粗子标题
- { id: SEC-XREF-SCALING, ..., kind: cross_reference }   # 前向引用宿主
```

## 5. `evidence_atoms`（§3.3）—— 核心

```yaml
- id: A-GATE-EQ4
  section_id: SEC-2.3
  atom_type: formula_atom
  source_element_id: SE-2.3-EQ4
  source_span: { file: engram_paper_mineru.md, line_start: 68, line_end: 68, char_start: 11471, char_end: 11616 }
  viewer_span: { file: source.md, line_start: 38, line_end: 38, char_start: 3960, char_end: 4105, viewer_only: true }
  raw_text: "\\alpha_ {t} = \\sigma \\left(...\\right). \\tag {4}"   # 逐字
  normalized_text: "alpha_t = sigma( RMSNorm(h_t)^T RMSNorm(k_t) / sqrt(d) )"  # ascii，仅 span 内
  evidence_strength: direct        # direct | indirect | cross_reference
  metadata: { tag: "4", role: scalar_gate, supported_by_context_atoms: [A-GATE-RMSNORM] }
```

**atom_type 词表（论文型）**：
`claim_sentence, method_sentence, risk_sentence, mechanism_sentence, formula_atom,
experiment_setup_atom, result_sentence, scaling_law_result_atom, table_caption_atom,
table_header_atom, table_row_atom, figure_caption_atom, cross_section_figure_caption_atom,
ablation_finding_atom, limitation_sentence`。

**metadata 可选字段**：`tag`（公式编号）、`role`、`supported_by_context_atoms` /
`interpretation_supported_by`（公式解释来源）、`requires_external_evidence` + `external_evidence_ref`、
图归属字段 `physical_section_id` / `semantic_section_ids` / `linked_objects` /
`physical_position_after` / `semantic_owner` / `include_in_chapter2_core_chunks`。

**raw/normalized 硬约束**：normalized 不得含 span 外的事实/名称/章节号；句子片段优先补成完整句或拆原子；
跨段汇总的 atom 用连续 span（含中间空行）或拆分；公式常量若在邻句，要么扩 span，要么经
`supported_by_context_atoms` 引用、要么单独建 atom。

## 6. `semantic_chunks`（§3.4 / §4.1）

```yaml
- id: C-GATING
  profile: article_research
  chunk_type: architecture_component_block
  section_path: "2 > 2.3"
  atom_ids: [...]            # 该 chunk 的全部原子
  central_atom_ids: [...]
  boundary_reason: "..."     # 为何在此切/不切（§5.3）
  extraction_targets: [...]
  gold_must_cover_atoms: [...]   # Object Integrity 约束
- id: C-XREF
  chunk_type: cross_reference_block
  core: false                # 前向引用隔离块，不进核心抽取
```

**chunk_type 词表（论文型）**：
`article_core_claim_block, architecture_component_block, formula_definition_block,
experiment_setup_block, experiment_result_block, scaling_law_block, ablation_finding_block,
system_efficiency_block, related_work_comparison_block, conclusion_block, cross_reference_block`。

## 7. `context_packages`（§6）

```yaml
- id: PKG-GATING
  profile: article_research
  chunk_id: C-GATING
  section_path: "Chapter 2 > 2.3 Context-aware Gating"
  document_title: ...
  atoms: [{atom_id, atom_type}, ...]   # 必须 == 该 chunk 的 atom_ids（即 LLM 输入）
  linked_context: { formula_context, table_caption, table_headers, figure, previous_heading, next_heading }
  extraction_targets: [...]
  expected_objects: [...]                       # 该输入应抽出的对象 id（object-recall 评测）
  expected_local_fields: { OBJ-ID: [field, ...] }   # 跨 package 对象的本地可抽字段（可选）
  expected_classification: forward_reference        # 仅 cross_reference 包用
```
- 每个**核心 chunk** 都应有一个 package；`cross_reference_block` 可配 `PKG-XREF`（`expected_objects: []` + `expected_classification`）。

## 8. `mentions` / `canonicalization`（§7.1 / §7.3）

```yaml
mentions:
  - { id: M-16, text: "context-aware gating", type: ArchitectureComponent, atom_id: A-GATE-QKV, canonical_key: context_aware_gating }
canonicalization:
  - { canonical: context_aware_gating, aliases: ["Context-aware Gating", "scalar gate", "alpha_t"] }
```
mention 应覆盖组件级 + **变量级**（W_K, e_t, alpha_t, RMSNorm…）+ **系统级**（All-to-All, PCIe, HBM, Zipfian…）实体。

## 9. `objects`（§7.2）

```yaml
- id: COMPONENT-CONTEXT-AWARE-GATING
  type: ArchitectureComponent
  section_path: "2 > 2.3"
  home_package: PKG-GATING
  payload: { ... }                       # 仅由证据支撑，不得编造
  local_evidence_atom_ids: [...]         # 在 home_package 的 chunk 内
  supporting_context_atom_ids: [...]     # 来自其它 chunk（可空）
  gold_label: true
  difficulty: medium                     # easy | medium | hard
  evidence_strength: direct              # direct | indirect | cross_reference
```

**object type 词表（论文型，qiefen §2.1 + 扩展）**：
`ArticleClaim, ArticleMethod, ArchitectureComponent, ExperimentSetup, ExperimentResult,
AblationFinding, ScalingLaw, MechanisticExplanation, SystemDesignClaim, Limitation, Implication,
DerivedRuleCandidate, Risk, Formula, IntermediateRepresentation`。
父子组件用 `payload.children: [object_id...]`（须为合法对象 id）。

## 10. `relations`（§8.1）

```yaml
- { id: R-10, relation_type: component_has_mechanism,
    source_object_id: COMPONENT-CONTEXT-AWARE-GATING, target_object_id: MECH-GATE-SUPPRESSES-NOISE,
    evidence_atom_ids: [A-GATE-MECH, A-GATE-EQ4], gold_label: true, difficulty: medium, evidence_strength: direct }
```
端点必须是本文件 `objects` 的 id。**relation_type 词表（论文型 §8.1 + 已用扩展）**：
`method_has_phase, method_has_component, component_has_component, component_produces_representation,
component_consumes_representation, component_mitigates_risk, component_has_mechanism,
component_defined_by_formula, component_adapts_component, formula_generalizes_formula,
component_has_default_setting, method_has_system_design, system_design_enables_efficiency,
system_design_has_tradeoff, experiment_tests_claim, result_supports_claim,
ablation_supports_component_importance, mechanism_explains_result, claim_extends_prior_work,
claim_suggests_design_rule`。扩展类型须语义清晰并在 analysis 说明。

## 11. `do_not_extract`（负例）

```yaml
- { ref: A-FIG3, reason: "...前向引用 Section 3，非本章组件", kind: cross_section_forward_reference }
- { text: "(Vaswani et al., 2017)", atom_id: A-GATE-QKV, reason: "inline citation", kind: citation }
- { text: "(a) Engram at training", source_lines: [81,81], reason: "subfigure label", kind: figure_label }
- { pattern: inline_author_year_citation, examples: ["(Vaswani et al., 2017)", "(Xie et al., 2025)"],
    reason: "inline citations 非知识对象", kind: citation_policy }
- { text: "Figure 1", atom_id: A-OV-DEF, reason: "在本章切片外", kind: out_of_slice_reference }
```
`kind ∈ {cross_section_forward_reference, citation, citation_policy, figure_label,
out_of_slice_reference}`。

---

# 第三部分　不变量与校验清单（CI 可执行）

对每个论文型 gold.yaml，下列必须全部通过：

1. **YAML 可解析**。
2. **权威 span**（必跑）：对每个 atom，`source_file[source_span.char_start:char_end] == raw_text`。
3. **viewer span**（可选）：`source.md[viewer_span.char_start:char_end] == raw_text`，且 `viewer_span.viewer_only == true`。
4. **结构引用**：`atom.section_id ∈ section_tree`；`atom.source_element_id ∈ source_elements`。
5. **chunk 覆盖**：每个 atom 至少属于一个 chunk 的 `atom_ids`；chunk 的 `central/gold_must_cover` ⊆ atoms。
6. **package**：`package.atoms == 对应 chunk.atom_ids`；`expected_objects ⊆ objects`；
   `expected_local_fields` 的字段 ⊆ 对应对象 payload 字段。
7. **对象证据**：`local_evidence ∪ supporting_context ⊆ evidence_atoms`；
   **local 原子 ∈ home_package 的 chunk**，**supporting 原子 ∉ home_package 的 chunk**；
   对象出现在其 `home_package.expected_objects` 中。
8. **关系**：端点 ∈ objects；`evidence_atom_ids ⊆ evidence_atoms`。
9. **mention**：`atom_id ∈ evidence_atoms`。
10. **do_not_extract**：`ref/atom_id`（若有）∈ evidence_atoms。
11. **标签**：objects/relations **无** `confidence` 字段；均有 `gold_label/difficulty/evidence_strength`。
12. **payload.children**（若有）∈ objects。
13. **raw/normalized 跨 span 审计**：非 formula atom 的 normalized 不得引入 span 外的数字/名称/章节号
    （formula atom 经 `supported_by_context_atoms` 例外）。
14. **全局无孤儿（global no-orphan）**：每个 atom 必须满足下列之一——
    (a) 是某 object 的 `local_evidence_atom_ids` / `supporting_context_atom_ids` 之一；或
    (b) 是某 relation 的 `evidence_atom_ids` 之一；或
    (c) 标了 `context_only: true`（章引/小结等框架性文字）；或
    (d) 属于某 `core: false` 的 `cross_reference_block`，且被 `do_not_extract` 以 `ref` 引用（前向引用图/表，如 Engram ch02 的 `A-FIG3`）。
    即：凡进入抽取的 atom 都要有去处；被 hold-out 的前向引用要显式声明。

> 参考校验脚本逻辑见 `fangan/testcases/engram/ch02_architecture/` 的构建/校验过程
> （span 由脚本对原始文件定位计算，不手工誊写）。

---

# 第四部分　对应的评估指标（qiefen §13）

- **切分质量**：`section_tree` / `evidence_atoms` / `semantic_chunks`（`gold_must_cover_atoms` 给 Object Integrity）→ Evidence Recall@Chunk、Over/Under-splitting。
- **证据绑定**：`source_span` + `raw_text` → atomizer 是否定位到正确原文 span。
- **抽取质量**：`objects`（local/supporting 证据）、`context_packages.expected_objects` / `expected_local_fields`
  → Object/Field P/R（按 package 算 object-recall）。
- **关系质量**：`relations` → Endpoint Validity / Evidence Accuracy / Type Accuracy。
- **负例控制**：`do_not_extract` → over-extraction（前向引用、citation、子图 label）抑制率。
