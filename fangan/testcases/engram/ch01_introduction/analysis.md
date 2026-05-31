# Engram 论文 **第 1 章 Introduction** 在 qiefen 方案下的结构化抽取（schema v0.3.3）

源文件（**权威**）：`pdf_parser/engram_paper_mineru.md`（MinerU 输出）
范围：**仅第 1 章 Introduction（源文件第 13–30 行）**
profile：`article_research`（论文型），schema_version `0.3.3`。
`source.md` 是 viewer-only 的逐字切片（行 13–30），所有严格评测坐标指向原始文件。
gold/source 由 `build.py` 生成、`validate.py` 校验（span 脚本定位，**不手工誊写**）。

本文逐 stage 说明 Introduction 经 `qiefen.md` 流水线后的形态；可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)。

---

## v0.3.3 相对 v0.1 的变更（本次升级）

知识内容（atom/chunk/object/relation 的 id 与语义）保持不变，仅按 v0.3.3 schema 重表达并补逐字 span：

- **双坐标**：每个 atom 同时带 `source_span`（权威，file=`engram_paper_mineru.md`，含 line/char）与
  `viewer_span`（`viewer_only:true`，file=`source.md`）；二者切片都等于 `raw_text`，由 `build.py` 锚定计算。
- **raw/normalized 纪律**：`raw_text` 是原文逐字连续 span（保留内联 `(Author, year)` citation、em-dash U+2014、
  行内 `$O(1)$`、以及跨页断词 `knowl-\n\nedge`）；`normalized_text` 只渲染该 span
  （`$O(1)$`→`O(1)`、em-dash→` - `、`knowl-\n\nedge`→`knowledge`、消解 span 内代词），不引入 span 外的事实/章节号。
- **对象证据 local/supporting + home_package**：对象用 `local_evidence_atom_ids`（在 home_package 的 chunk 内）+
  `supporting_context_atom_ids`（来自其它 chunk，本章对象证据均在各自 home chunk 内故全为空）+ `home_package`。
- **per-chunk context_packages + expected_objects**：4 个核心 chunk（`C-PROBLEM`/`C-METHOD`/`C-ALLOCATION`/`C-MECH-EFF`）
  各对应一个 package（`PKG-PROBLEM`/`PKG-METHOD`/`PKG-ALLOCATION`/`PKG-MECH-EFF`），`atoms == chunk.atom_ids`，
  `expected_objects` 列 home_package 为该包的对象；本章对象均归各自 home package、无跨 package 对象，故不需要 `expected_local_fields`。
- **标签**：objects/relations 去掉小数 `confidence`，改用 `gold_label:true` + `difficulty`(easy/medium/hard) +
  `evidence_strength`(direct/indirect/cross_reference)。
- **do_not_extract（负例）**：补全切片内全部 14 条内联 `(Author, year)` citation（逐条带 `atom_id`）+
  `inline_author_year_citation` policy；前向引用 `(see Table 3)`(A-RECONSTRUCTION-WASTE)、`(detailed in Section 2)`(A-ENGRAM-METHOD)
  标 `cross_section_forward_reference`。Figure 1 题注物理且语义均在第 1 章，作为正例 `A-FIG1-CAPTION` 留在 method chunk，不入负例。
- **source_elements**：由 atoms 按 `source_element_id` 自动分组生成——`SE-1-H`（标题第 13 行）+ 6 个段落
  （`SE-1-P15`/`SE-1-P17`〔行 17–19，跨页断词段〕/`SE-1-P21`/`SE-1-P23`/`SE-1-P25`/`SE-1-P27`）+ `SE-1-FIG1`（Figure 1 题注第 29 行）。
- **扩展关系类型**：保留 v0.1 的 `method_addresses_problem`(R-01) 作为语义清晰的扩展类型（spec §10 词表外，已在此说明）；
  其余关系类型均取自 spec §10 论文型词表。

---

## 0. Profile 判定（§2）

命中 `1. Introduction` / `we propose Engram` / `we formulate the Sparsity Allocation problem` /
`U-shaped scaling law` / `iso-parameter and iso-FLOPs baseline` / `(see Table 3)` / `Figure 1` /
`(detailed in Section 2)` → `article_research`。
抽取目标（§2.1）：`ArticleClaim` / `ArticleMethod` / `ArchitectureComponent` /
`MechanisticExplanation` / `ScalingLaw` / `SystemDesignClaim` / `ExperimentResult`。

---

## 1. SourceElement（§3.1）— MinerU 在 Introduction 里的真实形态与坑

- 标题：`# 1. Introduction`（单层，无小节）。
- **段内换行坑**：原文一个语义段（源行 17–19）被 MinerU 拆成三个物理行
  （`...two qualitatively different sub-tasks: compositional reasoning and knowl-` / `edge retrieval...`），
  含一个跨行连字符断词 `knowl- edge`。解析必须把这三行**重新拼成一个段落**再切 atom，
  否则 `A-LINGUISTIC-DUALITY` / `A-NGRAM-LOCAL` / `A-NO-LOOKUP-PRIMITIVE` /
  `A-RECONSTRUCTION-WASTE` 会被错误截断。
- 公式：仅行内 `$O(1)$`，无 `$$...\tag{N}$$` 编号块 → 本章无 `formula_atom`。
- 大量行内引用 `(Author, year)`：保留在 atom text 中作为证据，但**不抽成对象**（非本文贡献实体）。
- 前向引用：`(see Table 3)`、`(detailed in Section 2)`、`Figure 1` 题注——题注（源行 29）
  作为架构概览的 `figure_caption_atom` 归入 method chunk。

---

## 2. Section Tree（§3.2）

```
1 Introduction        (SEC-1)
```

Introduction 为单节，无子节；下游所有 atom 的 `section_id` 均为 `SEC-1`。

---

## 3. EvidenceAtom（§3.3）— 18 个 atom，按论证流分布

Introduction 是典型的"问题 → 论点 → 方法 → 分配律/规模 → 增益 → 机理/长上下文/基础设施"叙事。

- **问题框定**（`claim_sentence` / `mechanism_sentence`）：
  `A-SPARSITY-PRINCIPLE`（sparsity 原则，MoE = conditional computation）、
  `A-MOE-DEFACTO`（MoE 已是 frontier 默认）、
  `A-LINGUISTIC-DUALITY`（compositional reasoning vs static/local/stereotyped patterns）、
  `A-NGRAM-LOCAL`（N-gram 表明 local 规律天然可表示为廉价 lookup）、
  `A-NO-LOOKUP-PRIMITIVE`（Transformer 缺原生 lookup 原语 → 用计算模拟检索）、
  `A-RECONSTRUCTION-WASTE`（multi-token 实体重建消耗早层深度，浪费在 trivial 操作；见 Table 3）。
- **论点 + 方法**（`claim_sentence` / `mechanism_sentence` / `method_sentence` / `figure_caption_atom`）：
  `A-CONDITIONAL-MEMORY`（conditional memory 作为互补稀疏轴）、
  `A-NGRAM-O1-LOOKUP`（N-gram embedding，local context 作 key 索引大表，O(1) 查表）、
  `A-ENGRAM-METHOD`（Engram 四组件：tokenizer compression / multi-head hashing /
  contextualized gating / multi-branch integration）、
  `A-FIG1-CAPTION`（Figure 1 架构题注）。
- **Sparsity Allocation + 规模 + 增益**（`method_sentence` / `scaling_law_result_atom` / `result_sentence`）：
  `A-SPARSITY-ALLOCATION`（固定预算下 MoE vs Engram 容量分配）、
  `A-USHAPED-LAW`（U 形 scaling law）、
  `A-SCALE-27B`（扩到 27B，vs iso-param/iso-FLOPs MoE baseline）、
  `A-GAINS-PREVIEW`（MMLU +3.4 / CMMLU +4.0；BBH +5.0 / ARC-C +3.7；HumanEval +3.0 / MATH +2.4 …）。
- **机理 + 长上下文 + 基础设施**（`mechanism_sentence` / `result_sentence` / `method_sentence`）：
  `A-MECH-EARLY-LAYERS`（LogitLens/CKA：释放早层、加深有效深度）、
  `A-LONGCONTEXT`（NIAH 97.0 vs 84.2；Variable Tracking 89.0 vs 77.0）、
  `A-INFRA-PREFETCH`（deterministic ID → runtime prefetch）、
  `A-INFRA-RESULT`（100B 表 offload，开销 < 3%）。

---

## 4. SemanticChunk（§4.2 论文型 chunk）— 4 块

| chunk | chunk_type | section_path | boundary_reason |
| --- | --- | --- | --- |
| `C-PROBLEM` | `article_core_claim_block` | 1 | 问题框定：sparsity/MoE + 语言双重性 + Transformer 缺 lookup 原语并浪费早层深度，整段保持 claim-evidence 连续 |
| `C-METHOD` | `article_core_claim_block` | 1 | 论点 + 方法概览：conditional memory 互补轴，经 Engram 四组件实例化；Figure 1 题注作为架构概览随方法同块 |
| `C-ALLOCATION` | `scaling_law_block` | 1 | Sparsity Allocation 问题 → U 形律 → 扩到 27B → 头条增益，保持 setup-result 连续 |
| `C-MECH-EFF` | `system_efficiency_block` | 1 | why-it-works 预览：机理(有效深度) + 长上下文(注意力释放) + infra-aware 效率(prefetch, <3%) |

> 边界判定（§5.3）：问题与论点之所以分两块，是因为 `A-RECONSTRUCTION-WASTE` 结束于
> "浪费深度"的痛点，而 `A-CONDITIONAL-MEMORY` 开启"我们主张的解法"——一个 problem→solution
> 的自然切点。机理/长上下文/infra 三个 why 段合为一个 efficiency block，因为它们都在
> *解释/量化*同一贡献而非引入新方法。

---

## 5. ContextPackage（§6 论文型示例）

`C-METHOD` 的 package（`PKG-METHOD`）：

```
Document: Conditional Memory via Scalable Lookup: A New Axis of Sparsity for LLMs
Section: Engram > 1. Introduction > proposal of conditional memory / Engram
Atoms:
[A-CONDITIONAL-MEMORY] conditional memory as complementary sparsity axis
[A-NGRAM-O1-LOOKUP]     N-gram embedding, O(1) lookup, complement to MoE
[A-ENGRAM-METHOD]       Engram = N-gram + tokenizer compression / multi-head hashing / contextualized gating / multi-branch integration
[A-FIG1-CAPTION]        Figure 1 architecture caption
linked_context:
  figure_context: "Figure 1 | The Engram Architecture (retrieval + context-aware gating fusion)"
  previous_heading: "1. Introduction (problem framing)"
  next_heading:     "2. Architecture (Section 2 detail)"
Targets: ArticleClaim, ArticleMethod, ArchitectureComponent
```

---

## 6. Mention → 7. Object → 8. Relation（§7–§8.1）

- **Mention**（12 个）：Sparsity / MoE / conditional computation / conditional memory /
  knowledge lookup primitive / N-gram embeddings / Engram / contextualized gating /
  Sparsity Allocation problem / U-shaped scaling law / Engram-27B / linguistic duality。
  规范化（§7.3）：`Engram` ⊇ `Engram-27B`；`contextualized gating` ≡ `context-aware gating`（Intro 与 Figure 1 题注同指）。
- **Object**（13 个，覆盖论文型主要类型，详见 `gold.yaml::objects`）：
  - `ArticleClaim`：`CLAIM-PROBLEM`（问题陈述）/ `CLAIM-CONDITIONAL-MEMORY`（thesis）/ `CLAIM-MOE-PRIOR`（先验/baseline）
  - `ArticleMethod`：`METHOD-ENGRAM`（含四组件）/ `METHOD-SPARSITY-ALLOCATION`
  - `ArchitectureComponent`：`COMPONENT-CONTEXT-GATING`
  - `ScalingLaw`：`SCALINGLAW-USHAPED`（U 形分配律）
  - `ExperimentResult`：`RESULT-27B-GAINS` / `RESULT-LONGCONTEXT` / `RESULT-INFRA`
  - `MechanisticExplanation`：`MECH-EFFECTIVE-DEPTH` / `MECH-ATTENTION-FREED`
  - `SystemDesignClaim`：`SYS-PREFETCH`
- **Relation**（10 个，全 ID 化、带证据，详见 `gold.yaml::relations`）：
  - `method_addresses_problem`：`METHOD-ENGRAM -> CLAIM-PROBLEM`（R-01）
  - `claim_extends_prior_work`：`CLAIM-CONDITIONAL-MEMORY -> CLAIM-MOE-PRIOR`（R-02）
  - `method_has_component`：`METHOD-ENGRAM -> COMPONENT-CONTEXT-GATING`（R-03）、`-> METHOD-SPARSITY-ALLOCATION`（R-04）
  - `result_supports_claim`：`RESULT-27B-GAINS -> CLAIM-CONDITIONAL-MEMORY`（R-05）、`SCALINGLAW-USHAPED -> CLAIM-CONDITIONAL-MEMORY`（R-06）、`SCALINGLAW-USHAPED -> METHOD-SPARSITY-ALLOCATION`（R-10）
  - `mechanism_explains_result`：`MECH-EFFECTIVE-DEPTH -> RESULT-27B-GAINS`（R-07）、`MECH-ATTENTION-FREED -> RESULT-LONGCONTEXT`（R-08）
  - `system_design_enables_efficiency`：`SYS-PREFETCH -> RESULT-INFRA`（R-09）

> 关系类型均取自 qiefen §8.1 论文型词表；Introduction 作为"全文缩影"恰好覆盖
> problem->method->component->law->result->mechanism->system 的完整论证骨架。
