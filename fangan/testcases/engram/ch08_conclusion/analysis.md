# Engram 论文 **第 8 章 Conclusion** 在 qiefen 方案下的结构化抽取（schema v0.3.3）

源文件（**权威**）：`pdf_parser/engram_paper_mineru.md`（MinerU 输出）
范围：**仅第 8 章 Conclusion（源文件第 341–349 行；References 从 351 行起为下边界）**
profile：`article_research`（论文型），schema_version `0.3.3`。
深度：**短章（conclusion）**，8 个 atom、1 个 chunk。
`source.md` 是 viewer-only 的逐字切片（行 341–349），所有严格评测坐标指向原始文件。
gold/source 由 `build.py` 生成、`validate.py` 校验（span 脚本定位，**不手工誊写**）。

可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)。

---

## v0.3.3 相对 v0.1 的变更（本次升级）

知识内容（8 个 atom / 1 个 chunk / 8 个 object / 7 条 relation / 12 条 mention 的 id 与语义）保持不变，
仅按 v0.3.3 schema 重表达并补逐字 span：

- **双坐标**：每个 atom 同时带 `source_span`（权威，file=`engram_paper_mineru.md`，含 line/char）与
  `viewer_span`（`viewer_only:true`，file=`source.md`）；二者切片都等于 `raw_text`（脚本断言）。
- **raw/normalized 纪律**：`raw_text` 是原文逐字连续 span（按句/子句锚定）；`normalized_text` 只渲染该 span
  （`$O(1)$`→ASCII `O(1)`、弯引号 `“deepen”`→直引号 `"deepen"`、合并换页空行），不引入 span 外的事实/章节号。
- **页面断行**（关键 quirk）：`A-CONC-MECH`（mechanism_sentence）跨 PDF 换页，源行 347–349 被 MinerU 切成
  `...attention capacity to focus` / 空行 / `on global context...`；`raw_text` 保留内嵌 `\n\n` 连续 span，
  `normalized_text` 合并成单句。行 343 段末缺句号，`A-CONC-ENGRAM` 的 raw_text 终止于 `static patterns`。
- **对象证据 local/supporting + home_package**：对象用 `local_evidence_atom_ids`（在 home_package 的 chunk 内）+
  `supporting_context_atom_ids`（来自其它 chunk，本章单 chunk 故全为空）+ `home_package`（均为 `PKG-CONCLUSION`）。
- **per-chunk context_packages + expected_objects**：唯一核心 chunk `C-CONCLUSION` → 唯一 package `PKG-CONCLUSION`，
  `atoms == chunk.atom_ids`（全 8 atom），`expected_objects` 列其 home_package 为该包的全部 8 个对象；
  单包无跨 package 对象，故不需要 `expected_local_fields`。
- **标签**：objects/relations 去掉小数 `confidence`，改用 `gold_label:true` + `difficulty`(easy/medium/hard) +
  `evidence_strength`(direct/indirect/cross_reference)。
- **do_not_extract（负例）**：`inline_author_year_citation` policy（结论切片内无内联 citation，policy 仅作统一抑制）；
  本章无图、无前向引用图，故无 figure label / cross_reference_block。
- **source_elements**：由 atoms 按 `source_element_id` 自动分组生成——`SE-8-H`（标题 341 行）+
  `SE-8-P1`（343 行）+ `SE-8-P2`（345 行）+ `SE-8-P3`（347–349 行，带换页 note）。
- **扩展关系类型**：保留 v0.1 的 `claim_instantiated_by_method`(R-01) 与 `claim_suggests_implication`(R-07)
  作为语义清晰的扩展类型（spec §10 词表外，已在此说明）；其余 5 条用词表内类型
  （`result_supports_claim` / `mechanism_explains_result` / `claim_suggests_design_rule` / `system_design_enables_efficiency`）。

校验（脚本 `validate.py`）：YAML 可解析；8 个 atom 的 source_span/viewer_span 切片均逐字等于 raw_text；
结构引用解析；每个 atom ∈ chunk；`package.atoms == chunk.atom_ids`；`expected_objects ⊆ objects`；
对象 local ⊆ home-chunk、supporting ⊄ home-chunk；relation 端点 ∈ objects；无 `confidence`；GLOBAL 无孤儿 atom。
全部通过（ALL CHECKS PASS）。

---

## 0. Profile 判定（§2.1）

命中 `we introduce` / `we instantiate` / `we uncover a U-shaped scaling law` /
`mechanistic analysis` / `we envision` → `article_research`。
抽取目标（§2.1，conclusion 子集）：`ArticleClaim` / `Implication` / `DerivedRuleCandidate`。
qiefen §9.1 已点名 `C-016 Conclusion` 用 `conclusion_block`，对象为 ArticleClaim + future 推演。

---

## 1. SourceElement（§3.1）— MinerU 在结论里的真实形态

- 标题：`# 8. Conclusion`（341 行）、`# References`（351 行，下边界 heading）。
- 正文为三段连续 paragraph（343 / 345 / 347–349）。
- **两个解析坑**（已写进 `gold.yaml::source_meta.parsing_notes`）：
  1. 第 343 行段末 **缺句号**（`...constant-time $O(1)$ lookups for static patterns`），不能据此误判段落未结束。
  2. 第 347–349 行因 PDF **换页**被 MinerU 切成两段：`...freeing up attention capacity to focus` / `on global context...`，需跨换行合并成同一 mechanism claim（`A-CONC-MECH`）。
- `$O(1)$` 内联公式 → ASCII 化为 `O(1)`。`"deepen"` 原文带引号（比喻），atom 保留语义。

---

## 2. Section Tree（§3.2）

```
8 Conclusion
```

单节，`SEC-8`。无子节、无表格、无图。

---

## 3. EvidenceAtom（§3.3）— 8 个 atom

| atom | atom_type | 内容要点 |
| --- | --- | --- |
| `A-CONC-THESIS` | `claim_sentence` | 核心论点：conditional memory 是 MoE(conditional computation) 的**互补稀疏轴** |
| `A-CONC-ENGRAM` | `claim_sentence` | Engram 现代化 N-gram embedding，做到 O(1) 静态模式查找 |
| `A-CONC-USHAPE` | `claim_sentence` | Sparsity Allocation → U-shaped scaling law；hybrid 严格优于 pure MoE |
| `A-CONC-SCALE27B` | `result_sentence` | 扩到 27B，跨域增益；reasoning/code/math 增益最大 |
| `A-CONC-MECH` | `mechanism_sentence` | "deepen" 网络：解放早层静态重建 → 释放 attention 容量 |
| `A-CONC-LONGCTX` | `result_sentence` | 长上下文增益：LongPPL、RULER |
| `A-CONC-INFRA` | `claim_sentence` | infra-aware 效率：deterministic addressing → host memory offload、开销可忽略 |
| `A-CONC-VISION` | `claim_sentence` | 愿景：conditional memory 是下一代稀疏模型不可或缺的 primitive |

类型分布以 `claim_sentence` 为主（结论本质是 claim），两条复述实验数字的句子标 `result_sentence`，机制句标 `mechanism_sentence`——与 §3.3 论文型 atom 词表一致。

---

## 4. SemanticChunk（§4.1 论文型 chunk）

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-CONCLUSION` | `conclusion_block` | 8 | 整节 8 atom 一块 |

**boundary_reason**：结论位于 related-work 段落与 `# References`（351 行）之间；所有句子同属一个修辞单元（全篇总结 + 未来推演），无内部 heading 变化，故不切；真正的切点是下游 `# References` 的 `heading_change`（§5.3）。central_atom 取核心论点 `A-CONC-THESIS` 与愿景 `A-CONC-VISION`。

---

## 5. ContextPackage（§6 论文型示例）

`C-CONCLUSION` 的 package（`PKG-CONCLUSION`）：

```
Document: Engram: Conditional Memory as a Complementary Sparsity Axis
Section: 8 Conclusion
Atoms:
[A-CONC-THESIS]   conditional memory complements MoE
[A-CONC-ENGRAM]   Engram = modernized N-gram, O(1) lookup
[A-CONC-USHAPE]   U-shaped allocation law, hybrid > pure MoE
[A-CONC-SCALE27B] 27B; biggest gains in reasoning/code/math
[A-CONC-MECH]     deepens net by relieving early layers
[A-CONC-LONGCTX]  LongPPL / RULER gains
[A-CONC-INFRA]    deterministic addressing -> host-memory offload
[A-CONC-VISION]   conditional memory as next-gen primitive
linked_context:
  previous_heading: "Mechanisms of Knowledge Storage (Related Work)"
  next_heading: "References"
  formula_context: "O(1) lookup; Sparsity Allocation ratio rho (defined in ch03)"
Targets: ArticleClaim, Implication, DerivedRuleCandidate
```

---

## 6. Mention → 7. Object → 8. Relation（§7–§8.1）

- **Mention**（12 条）：conditional memory / conditional computation (MoE) / Engram /
  N-gram embeddings / Sparsity Allocation / U-shaped scaling law / Engram-27B /
  mechanistic analysis / LongPPL / RULER / deterministic addressing / host memory offloading。

- **Object**：
  - `ArticleClaim` ×5：核心论点 `CLAIM-CONDITIONAL-MEMORY`（thesis）、`CLAIM-ENGRAM-O1`、
    `CLAIM-USHAPE-HYBRID`、`CLAIM-SCALE-27B`、`CLAIM-INFRA-EFFICIENCY`。
  - `MechanisticExplanation` ×1：`MECH-EFFECTIVE-DEPTH`（deepen / relieve early layers）。
  - `Implication` ×1：`IMPL-PRIMITIVE`（future primitive 愿景）。
  - `DerivedRuleCandidate` ×1：`RULE-HYBRID-ALLOCATION`（hybrid 分配设计规则）。

- **Relation**（7 条，全 ID 化、带证据）：
  - `claim_instantiated_by_method`：核心论点 ← Engram-O1（R-01）
  - `result_supports_claim`：U-shaped law / 27B 结果 → 核心论点（R-02, R-03）
  - `mechanism_explains_result`：effective-depth → 长上下文/跨域增益（R-04）
  - `claim_suggests_design_rule`：U-shaped law → hybrid 分配规则（R-05，§8.1 点名类型）
  - `system_design_enables_efficiency`：Engram-O1 → infra 效率（R-06）
  - `claim_suggests_implication`：核心论点 → primitive 愿景（R-07）

---

## 9. 跨章 canonicalization 提示（关键）

本章是**结论**，其 claim **复述前面各章的结果**，不是新事实。`canonicalization` 段已把每个复述 mention
标注应链回的章节对象，下游跨章合并时不应在本章新建独立事实：

| 本章 mention / claim | 应链回的章节对象 |
| --- | --- |
| `u_shaped_scaling_law`（`CLAIM-USHAPE-HYBRID`） | **ch03** Sparsity Allocation 的 `ScalingLaw` 对象 |
| `engram_27b_result`（`CLAIM-SCALE-27B`） | **ch04** Large-scale Pre-training 的 `ExperimentResult` 对象 |
| `longppl` / `ruler`（`A-CONC-LONGCTX`） | **ch05** Long Context Training 的 `ExperimentResult` 对象 |
| `effective_depth_analysis`（`MECH-EFFECTIVE-DEPTH`） | **ch06** Analysis 的 `MechanisticExplanation` 对象 |
| `deterministic_addressing`（`CLAIM-INFRA-EFFICIENCY`） | ch07 / System Efficiency 的 `SystemDesignClaim` 对象 |

即：结论里的 object 在跨文档合并阶段应作为各章原始 result/finding 对象的 **别名 / 复述引用**（`canonicalization` + 后续 `result_supports_claim` 跨章边），而非平行事实，以避免重复计数。
