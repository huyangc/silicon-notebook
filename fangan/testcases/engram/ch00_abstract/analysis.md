# Engram 论文 **Abstract** 在 qiefen 方案下的结构化抽取（schema v0.3.3）

源文件（**权威**）：`pdf_parser/engram_paper_mineru.md`（MinerU 输出）
范围：**仅 Abstract（源文件第 9–11 行：`# Abstract` 标题第 9 行、正文段落第 11 行）**；标题/作者（第 1–7 行）仅作 `source_meta` 上下文。
profile：`article_research`（论文型），schema_version `0.3.3`。
`source.md` 是 viewer-only 的逐字切片（行 9–11），所有严格评测坐标指向原始文件。
gold/source 由 `build.py` 生成、`validate.py` 校验（span 脚本定位，**不手工誊写**）。

本文逐 stage 说明 Abstract 经 `qiefen.md` 流水线后的形态；可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)。

---

## v0.3.3 相对 v0.1 的变更（本次升级）

知识内容（atom/chunk/object/relation 的 id 与语义）保持不变，仅按 v0.3.3 schema 重表达并补逐字 span：

- **双坐标**：每个 atom 同时带 `source_span`（权威，file=`engram_paper_mineru.md`，含 line/char）与
  `viewer_span`（`viewer_only:true`，file=`source.md`）；二者切片都等于 `raw_text`。
- **raw/normalized 纪律**：`raw_text` 是原文逐字连续 span（按句/子句锚定）；`normalized_text` 只渲染该 span
  （`$O(1)$`→`O(1)`、`→`→`->`、补主语 `it`→`it [Engram]`），不引入 span 外的事实/章节号。
  knowledge/reasoning/code-math 三段在原文同一句内，按子 span 拆为三个 atom。
- **对象证据 local/supporting + home_package**：对象用 `local_evidence_atom_ids`（在 home_package 的 chunk 内）+
  `supporting_context_atom_ids`（来自其它 chunk，本章单 chunk 故全为空）+ `home_package`（均为 `PKG-ABS`）。
- **per-chunk context_packages + expected_objects**：唯一核心 chunk `C-ABS` → 唯一 package `PKG-ABS`，
  `atoms == chunk.atom_ids`（全 11 atom），`expected_objects` 列其 home_package 为该包的 9 个对象；
  单包无跨 package 对象，故不需要 `expected_local_fields`。
- **标签**：objects/relations 去掉小数 `confidence`，改用 `gold_label:true` + `difficulty`(easy/medium/hard) +
  `evidence_strength`(direct/indirect/cross_reference)。
- **do_not_extract（负例）**：补 `https://github.com/deepseek-ai/Engram` 代码链接 + `inline_author_year_citation`
  policy（Abstract 正文段无内联 citation，policy 仅作统一抑制）；本章切片内无图，故无 figure label。
- **source_elements**：由 atoms 按 `source_element_id` 自动分组生成——`SE-ABS-H`（标题第 9 行）+
  `SE-ABS-P`（段落第 11 行，承载全部 11 atom）。
- **扩展关系类型**：保留 v0.1 的 `method_addresses_problem`(R-01) 与 `claim_guided_by_scaling_law`(R-09)
  作为语义清晰的扩展类型（spec §10 词表外，已在此说明）。

---

## 0. Profile 判定（§2）

命中 `Abstract` / `we introduce` / `we observe` / `scaling law` / `iso-parameter and iso-FLOPs` /
benchmark 增益数字（`MMLU +3.4`）/ `Code available at` → `article_research`。
（现有检测器 `extraction_profiles.py` 会判为 `academic_paper`，记于 `profile_detected_as`。）
抽取目标（§2.1）：`ArticleClaim` / `ArticleMethod` / `ScalingLaw` / `ExperimentResult` /
`MechanisticExplanation` / `SystemDesignClaim` / `Implication`。

---

## 1. SourceElement（§3.1）— MinerU 在 Abstract 里的真实形态

`source_elements` 由 atoms 自动分组：`SE-ABS-H`（heading，行 9）+ `SE-ABS-P`（paragraph，行 11，承载全部 11 atom）。

- 标题：`# Abstract`（单个 heading，行 9）。
- 正文：**单一连续段落**（行 11），无小节、无列表、无公式块。
- MinerU 坑：
  - 作者上标渲染为内联数学 `$^{1,2}$` / `$^{2}$`（第 3 行），属标题/作者区，不进 atom。
  - Abstract 内 `$O(1)$` 是内联数学，`raw_text` 逐字保留 `$O(1)$`，`normalized_text` 归一为 ASCII `O(1)`。
  - 箭头 `84.2 → 97.0` 是 Unicode 右箭头（U+2192），归一为 `84.2 -> 97.0`。
  - benchmark 增益用 `+3.4` 等内联写法，全部相对 iso-param/iso-FLOPs MoE baseline。
  - knowledge/reasoning/code-math 三段增益在原文是同一句（`Most notably, while ... we observe ... and ...`），
    按子 span 锚定拆为 `A-ABS-RESULT-KNOWLEDGE` / `A-ABS-RESULT-REASONING` / `A-ABS-RESULT-CODEMATH`。
  - `A-ABS-MECH-ATTENTION` 原文主语是代词 `it`（指代 Engram），`raw_text` 逐字保留 `it`，
    `normalized_text` 写 `it [Engram]` 以在 span 内消解代词。

---

## 2. Section Tree（§3.2）

```
Abstract
```

Abstract 只有一个节点 `SEC-ABS`；后续章节（1 Introduction、2 Architecture …）由各自夹具负责。

---

## 3. EvidenceAtom（§3.3）— 11 条，全部挂在 `SEC-ABS`

| atom | atom_type | 内容要点 |
| --- | --- | --- |
| `A-ABS-PROBLEM` | `claim_sentence` | 问题陈述：MoE 走 conditional computation，但 Transformer 缺 native lookup primitive，只能用 compute 低效模拟检索 |
| `A-ABS-INTRO-ENGRAM` | `method_sentence` | 引入 conditional memory 为互补稀疏轴，经 Engram 实例化（modernized N-gram embedding，O(1) lookup） |
| `A-ABS-SCALING-LAW` | `scaling_law_result_atom` | Sparsity Allocation 问题 → U-shaped scaling law，优化 MoE compute 与 Engram memory 的权衡 |
| `A-ABS-SCALE-27B` | `result_sentence` | 缩放至 27B 参数，胜过严格 iso-parameter 与 iso-FLOPs 的 MoE baseline |
| `A-ABS-RESULT-KNOWLEDGE` | `result_sentence` | 知识检索：MMLU +3.4；CMMLU +4.0 |
| `A-ABS-RESULT-REASONING` | `result_sentence` | 通用推理（更大增益）：BBH +5.0；ARC-Challenge +3.7 |
| `A-ABS-RESULT-CODEMATH` | `result_sentence` | 代码/数学：HumanEval +3.0；MATH +2.4 |
| `A-ABS-MECH-DEPTH` | `mechanism_sentence` | 机制：Engram 卸下 backbone 早层的 static reconstruction，等效加深网络利于复杂推理 |
| `A-ABS-MECH-ATTENTION` | `mechanism_sentence` | 机制：把 local dependency 委派给 lookup，释放 attention 容量给 global context，长上下文检索 Multi-Query NIAH 84.2 -> 97.0 |
| `A-ABS-SYS-EFFICIENCY` | `claim_sentence` | 系统：deterministic addressing 支持 host memory 运行时 prefetch，开销可忽略 |
| `A-ABS-VISION` | `claim_sentence` | 愿景：conditional memory 应成为下一代稀疏模型不可或缺的 primitive；代码地址 |

> 拆分原则：把 problem / method / scaling-law / scale / 三组 result（knowledge·reasoning·code-math）/
> 两条 mechanism / system / vision 各拆为独立 atom，使下游 result→claim、mechanism→result 关系可精确绑定证据。
> 三组 benchmark 数字分别落在 reasoning 与 code/math 句，原文为同一句中的并列子句，拆为 `A-ABS-RESULT-REASONING`
> 与 `A-ABS-RESULT-CODEMATH` 以便分别对接「更大增益」的机制解释。

---

## 4. SemanticChunk（§4.1 论文型 chunk）

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-ABS` | `article_core_claim_block` | Abstract | 整个 Abstract 单块；problem→method→scaling law→scale→results→mechanism→efficiency→vision 不切 |

`boundary_reason`：Abstract 是论文 canonical 的核心 claim 块（§4.1），全部 11 个 atom 共同支撑同一组
`ArticleClaim` / `ArticleMethod` / `ScalingLaw` 对象，按 §5.3「保持 core-claim block 完整」不在段内切分。
`central_atom_ids`：`A-ABS-INTRO-ENGRAM`、`A-ABS-SCALING-LAW`（method + scaling law 是贡献核心）。

---

## 5. ContextPackage（§6 论文型示例）

`C-ABS` 的 package `PKG-ABS`：

```
Document: Conditional Memory via Scalable Lookup ...
Section: Abstract
Atoms:
[A-ABS-PROBLEM]          MoE 有 conditional computation，Transformer 缺 lookup primitive
[A-ABS-INTRO-ENGRAM]     引入 conditional memory / Engram (O(1) lookup)
[A-ABS-SCALING-LAW]      U-shaped Sparsity Allocation scaling law
[A-ABS-SCALE-27B]        27B 胜过 iso-param/iso-FLOPs MoE baseline
[A-ABS-RESULT-KNOWLEDGE] MMLU +3.4 / CMMLU +4.0
[A-ABS-RESULT-REASONING] BBH +5.0 / ARC-Challenge +3.7
[A-ABS-MECH-ATTENTION]   释放 attention → Multi-Query NIAH 84.2 -> 97.0
[A-ABS-SYS-EFFICIENCY]   deterministic prefetch，开销可忽略
linked_context:
  previous_heading: (document title)
  next_heading: "1. Introduction"
Targets: ArticleClaim, ArticleMethod, ScalingLaw, ExperimentResult, MechanisticExplanation, SystemDesignClaim
```

---

## 6. Mention → 7. Object → 8. Relation（§7–§8）

- **Mention**（11 个，详见 `gold.yaml::mentions`）：conditional memory / Engram / MoE /
  conditional computation / O(1) lookup / Sparsity Allocation / U-shaped scaling law /
  MMLU / BBH / Multi-Query NIAH / deterministic addressing。
- **Object**（详见 `gold.yaml::objects`）：
  - `ArticleClaim`：`CLAIM-CONDITIONAL-MEMORY`（互补稀疏轴）、`CLAIM-VISION-PRIMITIVE`（愿景 primitive）
  - `ArticleMethod`：`METHOD-ENGRAM`（modernized N-gram，O(1) lookup，27B，deterministic prefetch）
  - `ScalingLaw`：`SCALINGLAW-U-SHAPED`（Sparsity Allocation U 形律）
  - `ExperimentResult`：`RESULT-VS-MOE-BASELINE`（MMLU/CMMLU/BBH/ARC/HumanEval/MATH 增益）、
    `RESULT-LONG-CONTEXT`（Multi-Query NIAH 84.2→97.0）
  - `MechanisticExplanation`：`MECH-EFFECTIVE-DEPTH`（卸早层→加深网络）、`MECH-FREED-ATTENTION`（释放 attention）
  - `SystemDesignClaim`：`SYS-DETERMINISTIC-PREFETCH`（确定寻址→host prefetch）
- **Relation**（全 ID 化、带证据，详见 `gold.yaml::relations`）：
  - `method_addresses_problem`：Engram → conditional-memory claim
  - `result_supports_claim`：vs-MoE result / U-shaped law → conditional-memory claim；long-context → vision
  - `experiment_tests_claim`：vs-MoE result → Engram
  - `mechanism_explains_result`：effective-depth → vs-MoE（reasoning 增益）；freed-attention → long-context
  - `system_design_enables_efficiency`：Engram → deterministic-prefetch
  - `claim_guided_by_scaling_law`：Engram(27B scaling) ← U-shaped law（"Guided by this law" 的直接绑定）
