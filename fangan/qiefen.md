下面基于这两个样例，整理一套 **“文档切分 → 实体识别 → 关系识别”** 的统一方案。

两个样例代表两类典型文档：
**Engram 论文**是“研究论文型文档”，核心结构是 claim、method、architecture、experiment、result、ablation、implication；文中明确包含 conditional memory、Engram 架构、tokenizer compression、multi-head hashing、context-aware gating、sparsity allocation、U-shaped scaling law、实验表格和 ablation 结论。 
**CMOS Analog Circuit Design** 是“书籍 / 教材型文档”，核心结构是 chapter、section、concept、formula、derivation、example、process flow、circuit hierarchy、design principle、problem set；文中包含 analog IC design process、设计层级表、CMOS 工艺、pn junction、MOS transistor、layout rules、subcircuits、switched-capacitor circuits 等内容。 

---

# 1. 总体思路

文档切分的目标不是简单把文本切成 chunk，而是把文档转成一套可以支撑知识抽取的结构。

推荐主流程是：

```text
Raw Document
  ↓
SourceElement
  ↓
Section Tree
  ↓
EvidenceAtom
  ↓
SemanticChunk
  ↓
ContextPackage
  ↓
Mention Extraction
  ↓
Object Extraction
  ↓
Relation Extraction
  ↓
Canonicalization + Evidence Validation
```

也就是：

```text
先还原文档结构
再生成最小证据单元
再组合成语义完整块
再做实体、对象、关系抽取
```

---

# 2. Profile 先行：不同文档用不同切分策略

同样是 markdown / PDF，不同文档类型的知识结构完全不同，所以第一步要做 profile 判断。

## 2.1 论文型 profile：Article Research Profile

适用于 Engram 这种论文。

目标是抽取：

```text
ArticleClaim
ArticleMethod
ArchitectureComponent
ExperimentSetup
ExperimentResult
AblationFinding
ScalingLaw
MechanisticExplanation
SystemDesignClaim
Limitation
Implication
DerivedRuleCandidate
```

论文型文档的核心 chunk 应围绕：

```text
问题定义
方法架构
公式定义
实验设置
实验结果
ablation
机制解释
系统效率
related work 对比
conclusion
```

例如 Engram 论文中，Abstract 适合抽核心 claim；2.2 Sparse Retrieval 适合抽 architecture component；2.3 Context-aware Gating 适合抽 method + risk mitigation；3.1 Sparsity Allocation 适合抽 scaling law；6.2 Ablation 适合抽 design implication。  

---

## 2.2 书籍型 profile：Book / Textbook Profile

适用于 CMOS Analog Circuit Design 这种教材。

目标是抽取：

```text
Concept
Definition
Formula
Variable
Derivation
ExampleProblem
ExampleSolution
DesignProcess
TechnologyProcess
ProcessFlow
CircuitBlock
ComponentModel
PhysicalEffect
DesignPrinciple
DesignRule
ChecklistCandidate
```

书籍型文档的核心 chunk 应围绕：

```text
章节知识结构
概念定义
公式推导
例题解法
表格知识
图示层级
工艺流程
电路层级
设计原则
课后问题
```

例如 CMOS 教材中的 Table 1.1-1 可以抽 analog design hierarchy；Chapter 2.1 可以抽 CMOS fabrication process flow；pn junction 章节可以抽公式推导链；Chapter 4 的 op amp 层级图可以抽 circuit hierarchy；Chapter 9 可以抽 switched-capacitor design principles。  

---

# 3. 切分层级设计

## 3.1 SourceElement：解析器原始元素

SourceElement 是从 parser 中得到的原始结构块。

```yaml
SourceElement:
  id:
  source_id:
  type:
    - heading
    - paragraph
    - bullet
    - formula
    - table
    - table_row
    - figure_caption
    - flowchart
    - example_title
    - example_solution_step
  text:
  page:
  order:
  bbox:
  section_path:
  metadata:
```

对于论文，SourceElement 要重点保留：

```text
heading
abstract paragraph
method paragraph
formula
table
figure caption
result paragraph
```

对于书籍，SourceElement 要额外保留：

```text
chapter heading
section heading
example title
problem statement
derivation formula
flowchart
summary
problem set
```

---

## 3.2 Section Tree：文档结构树

Section Tree 是所有后续抽取的骨架。

论文型：

```text
Abstract
1. Introduction
2. Architecture
  2.1 Overview
  2.2 Sparse Retrieval
  2.3 Context-aware Gating
3. Scaling Laws
4. Large Scale Pre-training
5. Long Context Training
6. Analysis
8. Conclusion
```

书籍型：

```text
Chapter 1 Introduction and Background
  1.1 Analog Integrated Circuit Design
  1.2 Notation
  1.3 Analog Signal Processing
  1.4 Example Design
  Problems

Chapter 2 CMOS Technology
  2.1 Fabrication Processes
  2.2 pn Junction
  2.3 MOS Transistor
  2.4 Passive Components
  2.7 Summary
```

每个 atom、chunk、object 都要继承 `section_path`。这能显著提升 applies_to、condition、domain、topic、stage 的抽取质量。

---

## 3.3 EvidenceAtom：最小可引用证据单元

EvidenceAtom 是后续 citation 和 grounding 的最小单位。

```yaml
EvidenceAtom:
  id:
  source_id:
  element_id:
  section_id:
  atom_type:
  text:
  normalized_text:
  page:
  order:
  section_path:
```

不同文档类型的 atom 类型不同。

### 论文型 atom

```text
claim_sentence
method_sentence
formula_atom
experiment_setup_atom
result_sentence
table_row_atom
figure_caption_atom
ablation_finding_atom
limitation_sentence
```

例如 Engram 中：

```text
“Engram uses tokenizer compression, multi-head hashing, contextualized gating, and multi-branch integration.”
```

可以是 `method_sentence`。

```text
“Validation loss exhibits a U-shaped relationship with allocation ratio rho.”
```

可以是 `scaling_law_result_atom`。

---

### 书籍型 atom

```text
concept_definition_atom
formula_atom
derivation_step_atom
example_problem_atom
example_solution_step_atom
process_step_atom
table_row_atom
flowchart_edge_atom
design_principle_atom
problem_statement_atom
```

例如 CMOS 书中：

```text
“An analog signal is defined over a continuous range of time and amplitudes.”
```

可以是 `concept_definition_atom`。

```text
“Definition → Synthesis → Simulation → Layout → Parasitic Extraction → Fabrication → Testing”
```

可以是 `design_process_atom`。

```text
“Cj = Cj0 / [1 - (vD / phi0)]^m”
```

可以是 `formula_atom`。

---

## 3.4 SemanticChunk：语义完整抽取块

SemanticChunk 是真正送入抽取模型的基本单位。它由多个 EvidenceAtom 组成。

```yaml
SemanticChunk:
  id:
  chunk_type:
  source_id:
  section_id:
  atom_ids:
  central_atom_ids:
  heading_context:
  boundary_reason:
  profile:
  extraction_targets:
```

核心原则是：

```text
一个 chunk 应该包含完整知识单元，而不是固定长度文本。
```

---

# 4. SemanticChunk 类型设计

## 4.1 论文型 chunk

| Chunk 类型                        | 用途          | 示例                                    |
| ------------------------------- | ----------- | ------------------------------------- |
| `article_core_claim_block`      | 摘要和核心贡献     | Engram Abstract                       |
| `architecture_component_block`  | 架构组件        | Sparse Retrieval、Context-aware Gating |
| `formula_definition_block`      | 公式定义        | hash lookup、allocation ratio          |
| `experiment_setup_block`        | 实验设置        | iso-parameter / iso-FLOPs setup       |
| `experiment_result_block`       | 实验结果        | Table 1、Table 2                       |
| `scaling_law_block`             | scaling law | U-shaped allocation law               |
| `ablation_finding_block`        | 消融实验        | layer sensitivity、component ablation  |
| `system_efficiency_block`       | 系统效率        | host memory prefetch、throughput       |
| `related_work_comparison_block` | 与已有工作的差异    | MoE、memory network、N-gram work        |
| `conclusion_block`              | 结论和未来推演     | Conclusion                            |

Engram 中的 Sparsity Allocation 部分需要把参数定义、allocation ratio 公式、实验设置和 U-shaped 结果放在同一个或相邻 context package 中，因为这些内容共同支撑 scaling law object。

---

## 4.2 书籍型 chunk

| Chunk 类型                   | 用途        | 示例                                                |
| -------------------------- | --------- | ------------------------------------------------- |
| `chapter_overview_block`   | 章节目标和范围   | Chapter 2 CMOS Technology                         |
| `concept_definition_block` | 概念定义      | analog / digital / sampled-data signal            |
| `design_process_block`     | 设计流程      | analog IC design process                          |
| `hierarchy_table_block`    | 层级表       | systems / circuits / devices                      |
| `technology_process_block` | 单个工艺步骤    | oxidation、diffusion、ion implantation              |
| `process_flow_block`       | 多步骤工艺流程   | N-well CMOS fabrication                           |
| `derivation_block`         | 公式推导      | pn junction depletion width                       |
| `formula_definition_block` | 单个公式定义    | threshold voltage equation                        |
| `example_solution_block`   | 例题        | Example 2.2-1、Example 2.3-1                       |
| `circuit_hierarchy_block`  | 电路层级      | op amp → stages → subcircuits                     |
| `design_principle_block`   | 设计原则      | switched-capacitor accuracy from capacitor ratios |
| `layout_rule_block`        | layout 规则 | width / spacing / contact / metal rules           |
| `problem_set_block`        | 课后问题      | Chapter problems                                  |

CMOS 教材中 analog IC design process、hierarchy table、CMOS fabrication、MOS switch、switched-capacitor circuits 都适合生成不同类型的 semantic chunk。  

---

# 5. Chunk 生成算法

建议采用 **anchor-based semantic chunking**。

## 5.1 Anchor 检测

先在 EvidenceAtom 中识别知识锚点。

### 论文型 anchor

```text
we propose
we introduce
we show
results indicate
experimental setup
ablation
Table
Figure
validation loss
outperforms
mechanistic analysis
limitation
```

对应对象：

```text
ArticleClaim
Method
ExperimentSetup
ExperimentResult
AblationFinding
ScalingLaw
MechanisticExplanation
```

---

### 书籍型 anchor

```text
definition
objective
the process steps include
the desired characteristics are
example
find
using Eq.
summary
problems
Table
Figure
```

对应对象：

```text
Concept
Formula
Derivation
ExampleProblem
ExampleSolution
ProcessFlow
DesignPrinciple
ChecklistCandidate
```

---

## 5.2 Anchor Expansion

检测到 anchor 后，向前后扩展，把完整知识单元包括进来。

### 对论文

例如检测到：

```text
“U-shaped relationship between validation loss and allocation ratio”
```

需要向前包括：

```text
P_tot / P_act / P_sparse 定义
rho 公式
实验设置
控制变量
```

向后包括：

```text
最优 rho
性能数值
机制解释
MoE-dominated / Engram-dominated 分析
```

---

### 对书籍

例如检测到：

```text
“Example 2.2-1”
```

需要包括：

```text
题目
given values
要求计算的量
使用的公式
每一步计算
最终结果
```

例如检测到：

```text
“pn junction depletion capacitance formula”
```

需要包括：

```text
depletion charge
capacitance definition
Cj 公式
Cj0
grading coefficient
适用条件
```

---

## 5.3 Boundary Scoring

在 atom 边界上判断是否切分。

推荐边界评分：

```text
boundary_score =
  structural_boundary
+ heading_change
+ anchor_type_change
+ semantic_drift
+ length_pressure
- formula_continuity
- table_header_row_dependency
- example_solution_continuity
- derivation_continuity
- process_flow_continuity
- experiment_setup_result_dependency
```

适合切分的位置：

```text
新章节
新小节
新 example
新 table
新 experiment
新 process step
新 design principle
```

适合保持连续的位置：

```text
公式和推导说明之间
表头和表格行之间
实验设置和结果之间
例题题干和解法之间
工艺流程连续步骤之间
architecture problem 和 method solution 之间
```

---

# 6. ContextPackage：给 LLM 的抽取输入

LLM 不直接吃 raw chunk，而是吃 context package。

```yaml
ContextPackage:
  id:
  profile:
  chunk_id:
  section_path:
  document_title:
  atoms:
    - atom_id
    - atom_type
    - text
  linked_context:
    - table_caption
    - table_headers
    - figure_caption
    - formula_context
    - previous_heading
    - next_heading
  extraction_targets:
```

示例：论文型 package

```text
Document: Engram paper
Section: 3.1 Optimal Allocation Ratio Between MoE and Engram
Atoms:
[A1] P_tot, P_act, P_sparse definitions
[A2] allocation ratio rho formula
[A3] experimental protocol
[A4] U-shaped validation loss result
[A5] optimum rho around 75%-80%
Targets:
ScalingLaw, ExperimentSetup, ExperimentResult, DesignImplication
```

示例：书籍型 package

```text
Document: CMOS Analog Circuit Design
Section: Chapter 2 > 2.2 pn Junction > Depletion Capacitance
Atoms:
[A1] depletion charge definition
[A2] capacitance definition
[A3] Cj formula
[A4] Cj0 and grading coefficient explanation
Targets:
Concept, Formula, Variable, Derivation, DesignImplication
```

---

# 7. 实体识别：Mention → Object → Canonical Entity

实体识别建议拆成三层。

```text
Mention Extraction
  ↓
Knowledge Object Extraction
  ↓
Canonicalization
```

---

## 7.1 Mention Extraction

Mention 是文本中的实体提及。

论文型 mention：

```text
Engram
MoE
conditional memory
conditional computation
tokenizer compression
multi-head hashing
context-aware gating
allocation ratio
validation loss
LongPPL
RULER
LogitLens
CKA
```

书籍型 mention：

```text
analog signal
sampled-data signal
CMOS technology
oxidation
ion implantation
pn junction
depletion region
barrier potential
MOS transistor
threshold voltage
body effect
MOS switch
switched-capacitor circuit
clock feedthrough
```

输出示例：

```yaml
Mention:
  id: M-001
  text: "context-aware gating"
  type: ArchitectureComponent
  atom_id: A-GATE-001
  canonical_key: context_aware_gating
```

---

## 7.2 Object Extraction

Object 是从 chunk 中抽出的知识对象。

论文型对象：

```yaml
ArticleMethod:
  name: "Context-aware Gating"
  problem_addressed:
    - "hash collisions"
    - "polysemy"
    - "context-independent memory"
  mechanism:
    - "use hidden state as dynamic Query"
    - "use retrieved memory as Key/Value source"
    - "suppress inconsistent retrieved memory"
  evidence_atom_ids:
    - A-GATE-001
    - A-GATE-002
    - A-GATE-003
```

书籍型对象：

```yaml
Formula:
  name: "pn junction depletion capacitance"
  expression: "Cj = Cj0 / [1 - (vD / phi0)]^m"
  variables:
    Cj: "depletion-layer capacitance"
    Cj0: "zero-bias depletion capacitance"
    m: "grading coefficient"
  applies_to:
    - "pn junction"
  evidence_atom_ids:
    - A-PN-CJ-001
    - A-PN-CJ-002
```

---

## 7.3 Canonicalization

把同义词、缩写、写法差异统一。

示例：

```text
Context-aware Gating
contextualized gating
gating mechanism
scalar gate alpha_t
```

统一为：

```text
Canonical Entity: Context-aware Gating
```

书籍型示例：

```text
bulk
substrate
B terminal
body
```

需要按上下文统一或建立 related_to 关系。

---

# 8. 关系识别：直接输出 ID 化关系

关系识别阶段要避免自由文本 related 字段，直接输出 ID 化边。

```yaml
Relation:
  id:
  relation_type:
  source_id:
  target_id:
  evidence_atom_ids:
  confidence:
```

---

## 8.1 论文型关系类型

```text
method_has_component
component_mitigates_risk
method_addresses_problem
formula_defines_metric
experiment_tests_claim
result_supports_claim
ablation_supports_component_importance
mechanism_explains_result
claim_extends_prior_work
claim_suggests_design_rule
system_design_enables_efficiency
```

示例：

```yaml
Relation:
  type: method_has_component
  source_id: METHOD-ENGRAM
  target_id: COMPONENT-CONTEXT-AWARE-GATING
  evidence_atom_ids:
    - A-ARCH-001
```

```yaml
Relation:
  type: result_supports_claim
  source_id: RESULT-U-SHAPED-ALLOCATION
  target_id: CLAIM-CONDITIONAL-MEMORY-COMPLEMENTS-MOE
  evidence_atom_ids:
    - A-SCALING-RESULT-001
```

```yaml
Relation:
  type: component_mitigates_risk
  source_id: COMPONENT-CONTEXT-AWARE-GATING
  target_id: RISK-HASH-COLLISION-POLYSEMY
  evidence_atom_ids:
    - A-GATE-001
```

Engram 的 ablation 部分还可以生成：

```text
branch-specific fusion / context-aware gating / tokenizer compression
  → important_component_of
Engram
```

因为论文明确报告去掉这些组件会导致最大的 validation loss regression。

---

## 8.2 书籍型关系类型

```text
concept_defines_term
concept_contrasts_with_concept
formula_defines_variable
formula_depends_on_variable
formula_derived_from_formula
formula_used_in_example
example_uses_formula
process_flow_has_step
process_step_precedes_step
process_step_creates_structure
process_step_mitigates_issue
circuit_block_composed_of_block
component_has_property
design_principle_applies_to_scenario
design_principle_has_tradeoff
checklist_candidate_derived_from_principle
```

示例：

```yaml
Relation:
  type: design_process_has_step
  source_id: PROCESS-ANALOG-IC-DESIGN
  target_id: STEP-PARASITIC-EXTRACTION
  evidence_atom_ids:
    - A-DESIGN-PROCESS-001
```

```yaml
Relation:
  type: circuit_block_composed_of_block
  source_id: CIRCUIT-OPERATIONAL-AMPLIFIER
  target_id: CIRCUIT-INPUT-DIFFERENTIAL-AMPLIFIER
  evidence_atom_ids:
    - A-OPAMP-HIERARCHY-001
```

```yaml
Relation:
  type: formula_used_in_example
  source_id: FORMULA-PN-JUNCTION-CAPACITANCE
  target_id: EXAMPLE-2.2-1
  evidence_atom_ids:
    - A-EXAMPLE-2.2-1-STEP-001
```

CMOS 文档中的 op amp hierarchy 图非常适合直接转为 `circuit_block_composed_of_block` 边，例如 operational amplifier 由 biasing circuits、input differential amplifier、second gain stage、output stage 构成。

---

# 9. 两个样例的具体抽取结果形态

## 9.1 Engram 论文

建议生成的高价值 chunks：

```text
C-001 Abstract Core Claims
C-002 Introduction Problem Framing
C-003 Architecture Overview
C-004 Sparse Retrieval: Tokenizer Compression + Hashing
C-005 Context-aware Gating
C-006 Multi-branch Integration
C-007 System Efficiency
C-008 Sparsity Allocation Formulation
C-009 U-shaped Scaling Result
C-010 Large-scale Pretraining Table
C-011 Long-context Training Table
C-012 Effective Depth Analysis
C-013 Ablation and Layer Sensitivity
C-014 System Throughput
C-015 Related Work
C-016 Conclusion
```

建议抽取对象：

```text
ArticleClaim:
- Conditional memory complements conditional computation.
- Engram provides O(1) lookup for static local patterns.
- Hybrid allocation between MoE and Engram follows a U-shaped scaling law.
- Engram improves long-context retrieval by freeing attention capacity.

ArticleMethod:
- Sparse Retrieval via Hashed N-grams
- Tokenizer Compression
- Multi-Head Hashing
- Context-aware Gating
- Multi-branch Integration
- Prefetch-and-overlap Inference

ExperimentResult:
- Engram-27B vs MoE-27B benchmark improvements
- RULER / LongPPL improvements
- U-shaped allocation result
- Layer 2 / Layer 2+6 placement ablation
- Component ablation result
```

建议生成关系：

```text
Engram has_component TokenizerCompression
Engram has_component MultiHeadHashing
Engram has_component ContextAwareGating
ContextAwareGating mitigates HashCollisionAndPolysemy
SparsityAllocationExperiment supports UShapedScalingLaw
AblationResult supports ImportanceOfContextAwareGating
SystemEfficiencyResult supports DeterministicPrefetchClaim
```

---

## 9.2 CMOS 教材

建议生成的高价值 chunks：

```text
C-001 Analog Signal / Digital Signal / Sampled-data Signal
C-002 Analysis vs Design
C-003 Analog IC Design Process
C-004 Design Hierarchy Table
C-005 Signal Processing System Block Diagram
C-006 CMOS Technology Overview
C-007 Basic Fabrication Processes
C-008 Photolithography Flow
C-009 N-Well CMOS Fabrication Flow
C-010 pn Junction Derivation
C-011 pn Junction Capacitance Formula
C-012 Example 2.2-1
C-013 MOS Transistor Structure
C-014 Threshold Voltage Derivation
C-015 Example 2.3-1
C-016 Passive Capacitor Design Principles
C-017 Layout Rules
C-018 MOS Switch Properties
C-019 Op Amp Circuit Hierarchy
C-020 Switched-Capacitor Design Principles
```

建议抽取对象：

```text
Concept:
- Analog Signal
- Digital Signal
- Sampled-data Signal
- pn Junction
- Depletion Region
- MOS Transistor
- Threshold Voltage
- Body Effect
- Switched-Capacitor Circuit

Formula:
- Digital representation formula
- pn junction depletion width
- barrier potential
- depletion capacitance
- diode current equation
- MOS threshold voltage
- Sah equation

ProcessFlow:
- Analog IC Design Process
- Photolithography Process
- N-Well CMOS Fabrication Process

CircuitBlock:
- Operational Amplifier
- Input Differential Amplifier
- Current Mirror
- MOS Switch
- Switched-Capacitor Circuit

DesignPrinciple:
- Use simulation and post-layout parasitics in analog IC design.
- Use capacitor ratios for switched-capacitor accuracy.
- Consider ON resistance, OFF leakage, and parasitic capacitance in MOS switches.
```

建议生成关系：

```text
AnalogICDesignProcess has_step Simulation
AnalogICDesignProcess has_step ParasiticExtraction
SystemsLevel has_description BehavioralModel
CircuitLevel has_description Macromodel
NWellCMOSProcess has_step NWellImplant
NWellCMOSProcess has_step LOCOSIsolation
LOCOSIsolation introduces BirdsBeak
PNJunction has_property DepletionCapacitance
ThresholdVoltage depends_on BodyEffectCoefficient
OperationalAmplifier composed_of InputDifferentialAmplifier
MOSSwitch has_nonideality LeakageCurrent
SwitchedCapacitorCircuit has_tradeoff ClockFeedthrough
```

---

# 10. 证据绑定策略

新的方案里，证据绑定从后处理改成抽取时内生完成。

每个对象必须输出：

```yaml
evidence_atom_ids:
  - A-001
  - A-002
```

每个关系也必须输出：

```yaml
evidence_atom_ids:
  - A-003
```

这样 S4 证据绑定可以从：

```text
对象文本 → 精确子串 / token overlap 找证据
```

升级为：

```text
抽取时直接选择证据 atom
→ evidence validation 检查字段是否被 atom 支撑
```

对于表格，证据应绑定到：

```text
table_caption
table_header
table_row
```

对于公式，证据应绑定到：

```text
formula_atom
definition_atom
condition_atom
```

对于例题，证据应绑定到：

```text
problem_atom
given_atom
formula_usage_atom
solution_step_atom
result_atom
```

---

# 11. 最小数据结构

## 11.1 EvidenceAtom

```yaml
EvidenceAtom:
  id:
  source_id:
  element_id:
  section_path:
  atom_type:
  text:
  page:
  order:
  parent_context:
```

---

## 11.2 SemanticChunk

```yaml
SemanticChunk:
  id:
  profile:
  chunk_type:
  section_path:
  atom_ids:
  central_atom_ids:
  extraction_targets:
  boundary_reason:
```

---

## 11.3 Mention

```yaml
Mention:
  id:
  text:
  type:
  atom_id:
  span:
  canonical_key:
```

---

## 11.4 KnowledgeObject

```yaml
KnowledgeObject:
  id:
  type:
  payload:
  evidence_atom_ids:
  section_path:
  confidence:
  status:
```

---

## 11.5 Relation

```yaml
Relation:
  id:
  relation_type:
  source_object_id:
  target_object_id:
  evidence_atom_ids:
  confidence:
```

---

# 12. 推荐落地顺序

## P0：先替换字符窗口

先实现：

```text
1. Section Tree
2. EvidenceAtom
3. SemanticChunk
4. ContextPackage
5. LLM 输出 evidence_atom_ids
```

目标是让抽取输入从：

```text
9000 字符窗口
```

变成：

```text
带 section_path、atom_id、table header、formula context 的语义包
```

---

## P1：实现两套 profile-aware chunker

先支持两个 profile：

```text
article_research
textbook
```

### article_research chunker

重点处理：

```text
abstract
method
formula
experiment setup
table result
ablation
conclusion
```

### textbook chunker

重点处理：

```text
chapter
section
concept
formula
derivation
example
table
flowchart
process
problem
```

---

## P2：实体 / 对象 / 关系三阶段抽取

把当前直接抽 rule/case 的方式改成：

```text
Pass 1: mention extraction
Pass 2: object extraction
Pass 3: relation extraction
Pass 4: canonicalization
Pass 5: evidence validation
```

---

## P3：表格、公式、例题专项解析

对两个样例来说，最值得优先做的是：

```text
表格行结构化
公式变量抽取
公式链条识别
例题 problem / given / solution / result 拆解
figure flowchart 转 graph edge
```

---

## P4：审核反馈回流

记录 curator 的修改：

```text
对象被 approve / reject
evidence 被修改
relation 被修改
chunk 被认为过碎或过宽
字段被补充
```

反向优化：

```text
chunk boundary
anchor expansion
profile schema
relation schema
confidence calibration
```

---

# 13. 评估指标

针对这块能力，可以建立以下指标。

## 13.1 切分质量

```text
Evidence Recall@Chunk:
gold evidence atom 是否进入抽取 context。

Object Integrity Rate:
一个完整对象的证据是否落在同一个 semantic chunk 或相邻 context package 中。

Over-splitting Rate:
一个完整公式推导、例题、实验结果被切碎的比例。

Under-splitting Rate:
一个 chunk 混入多个无关知识单元的比例。
```

---

## 13.2 抽取质量

```text
Mention Precision / Recall
Object Precision / Recall
Field Completeness
Formula Variable Accuracy
Table Row Parsing Accuracy
Example Step Accuracy
```

---

## 13.3 关系质量

```text
Relation Endpoint Validity:
关系端点是否是合法 object_id / entity_id。

Relation Evidence Accuracy:
关系是否被 evidence_atom 支撑。

Relation Type Accuracy:
关系类型是否正确。

Canonicalization Accuracy:
同义实体是否正确合并。
```

---

# 14. 最终总结

基于 Engram 论文和 CMOS 教材这两个样例，文档切分到实体/关系识别的完整方案可以总结为：

```text
1. 先做 profile 判断：
   论文走 article_research；
   书籍走 textbook。

2. 文档先转结构树：
   chapter / section / subsection / table / formula / figure / example 全部保留。

3. 最小证据单位用 EvidenceAtom：
   sentence、formula、table row、figure caption、example step、flowchart edge。

4. 抽取单位用 SemanticChunk：
   按 claim、method、experiment、concept、derivation、example、process flow 等知识单元切。

5. LLM 输入用 ContextPackage：
   chunk + section_path + neighboring atoms + table headers + formula context。

6. 实体识别分三层：
   mention → knowledge object → canonical entity。

7. 关系识别直接输出 ID 化边：
   relation endpoint 使用 object_id / entity_id，所有关系绑定 evidence_atom_ids。

8. 证据绑定内生化：
   抽取时直接引用 atom_id，后续只做 validation。

9. 两类文档使用不同对象 schema：
   论文抽 claim/method/result/implication；
   书籍抽 concept/formula/derivation/example/process/design principle。

10. 后续用审核反馈优化：
   用 approved/rejected/modified objects 反向改进 chunking、schema 和 confidence。
```

一句话版本：

> **把文档切分从“按长度切文本”升级为“按文档结构和知识单元切 evidence atoms / semantic chunks”，再用 profile-aware schema 做 mention、object、relation 三阶段抽取。**
