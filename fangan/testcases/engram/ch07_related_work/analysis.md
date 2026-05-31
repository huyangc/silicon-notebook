# Engram 论文 **第 7 章 Related Work** 在 qiefen 方案下的结构化抽取（schema v0.3.3）

源文件：`pdf_parser/engram_paper_mineru.md`（MinerU 输出）
范围：**仅第 7 章（源文件 322–339 行）**
profile：`article_research`（论文型）

本文逐 stage 说明第 7 章经 `qiefen.md` 流水线后的形态；gold 由 `build.py` 生成（anchor-based
span，绝不手写坐标），校验见 `validate.py`，可加载 gold 见同目录 [`gold.yaml`](./gold.yaml)。

> **v0.1 → v0.3.3 升级**：保留全部 atom/chunk/object/relation **ID 与语义**，仅按 v0.3.3 schema 重表，
> 见文末「v0.1 → v0.3.3 change-list」。

---

## 0. Profile 判定（§2）

命中 `Related Work` / 密集 `(Author, Year)` 引文 / `Mixture-of-Experts` / `Memory Network` /
`our work diverges fundamentally in two key dimensions` → `article_research`。
本章抽取目标聚焦 `RelatedWorkComparison`、`ArticleClaim`、`SystemDesignClaim`（§2.1）。

---

## 1. SourceElement（§3.1）— MinerU 在本章里的真实形态

- 标题：`# 7. Related Work`（§322 行）；四个主题用**粗体导语**（`N-gram Modeling and
  Embedding Scaling.` / `Mixture-of-Experts.` / `Memory Network.` / `Mechanisms of Knowledge
  Storage.`）起段，而非独立 markdown heading。
- 无公式、无表格；引文以 `(Shannon, 1948)`、`(Yu et al., 2025)` 形式密集出现。
- Engram 两个 key difference 用 markdown **bullet list**（`- First, ...` / `- Second, ...`，
  源 330/331 行）给出，是本章最高价值的 claim。

> 解析坑：**Figure 7 题注（源 326 行）物理上夹在 N-gram 段的两段之间**，但它属于上一章
> （Section 6 的 gating 可视化），不应纳入 Section 7 的 atom。section_tree 仅取 322–340 的
> Related Work 内容。

---

## 2. Section Tree（§3.2）

```
7 Related Work
  N-gram Modeling and Embedding Scaling   (SEC-7-NGRAM)
  Mixture-of-Experts                      (SEC-7-MOE)
  Memory Network                          (SEC-7-MEM)
  Mechanisms of Knowledge Storage         (SEC-7-KNOW)
```

四个粗体导语段被提升为 `SEC-7` 的子节，便于 atom 继承 `section_path`。

---

## 3. EvidenceAtom（§3.3）— 13 条，按主题分布

- **N-gram / Embedding Scaling**（6 条，最密）：
  `A-NGRAM-ORIGIN`（Shannon → smoothing → FastText）、`A-NGRAM-RESURGE`（Per-Layer
  Embeddings / DeepEmbed → embedding scaling）、`A-NGRAM-SUPERBPE-SCONE`、`A-NGRAM-OVERENC-BLT`
  四条勾勒 prior work；`A-DIFF1-PRIMITIVE`（一等建模原语 + iso-param/iso-FLOPs）与
  `A-DIFF2-COMUDESIGN`（深层注入 + compute-comm overlap + Zipfian cache）是 Engram 的两个差异。
- **MoE**（2 条）：`A-MOE-INTRO`（Shazeer→Switch/GLaM）、`A-MOE-DEEPSEEK`（DeepSeek-MoE/V3、Kimi-k2）。
- **Memory Network**（2 条）：`A-MEM-PARAMETRIC`（PKM/PEER/UltraMem…）vs `A-MEM-NONPARAMETRIC`（REALM/RETRO/PlugLM）。
- **Knowledge Storage**（3 条）：`A-KNOW-FFN-KV`（FFN as KV + knowledge neurons）、
  `A-KNOW-EDITING`（causal tracing → ROME/MEMIT）、`A-KNOW-WORLDMODEL`（Othello-GPT world models）。

atom_type 主要是 `claim_sentence`（论断/对比型）与 `mechanism_sentence`（描述 prior work 机制）。

---

## 4. SemanticChunk（§4.1 论文型 chunk）

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-RW-NGRAM` | `related_work_comparison_block` | 7 > N-gram… | 4 条 prior work + 2 条 Engram 差异，**差异 bullet 必须和它对比的 prior work 同块**（boundary_reason） |
| `C-RW-MOE`   | `related_work_comparison_block` | 7 > MoE       | MoE 谱系一题 |
| `C-RW-MEM`   | `related_work_comparison_block` | 7 > Memory Network | parametric vs non-parametric 分类一题 |
| `C-RW-KNOW`  | `related_work_comparison_block` | 7 > Knowledge Storage | FFN-as-KV→editing→world model 一题 |

切分原则（§5.3）：四个粗体主题之间是清晰的 `anchor_type_change` 边界，故按主题切四块；
但 `C-RW-NGRAM` 内部不切——Engram 的 first/second difference 只有与 SCONE/OverEncoding、
Layer-0 placement 等 prior work 并置才有意义（保持 architecture-problem 与 method-solution 同块）。

---

## 5. ContextPackage（§6 论文型示例）

`C-RW-NGRAM` 的 package `PKG-RW-NGRAM`：

```
Document: Engram paper
Section: Section 7 Related Work > N-gram Modeling and Embedding Scaling
Atoms:
[A-NGRAM-SUPERBPE-SCONE] SuperBPE / SCONE
[A-NGRAM-OVERENC-BLT]    OverEncoding / BLT (hash N-gram embeddings)
[A-DIFF1-PRIMITIVE]      diff #1: first-class primitive, iso-param/iso-FLOPs
[A-DIFF2-COMUDESIGN]     diff #2: deeper-layer injection + Zipfian cache
linked_context:
  previous_heading: "6. Analysis (gating visualization, Figure 7)"
  next_heading: "Mixture-of-Experts"
  formula_context: "diff #1 grounded in Sparsity Allocation framework (Section 3)"
Targets: RelatedWorkComparison, ArticleClaim, SystemDesignClaim
```

---

## 6. Mention → 7. Object → 8. Relation（§7–§8）

- **Mention**（24 条）：prior-work 名称为主（FastText / SuperBPE / SCONE / OverEncoding / BLT /
  MoE / DeepSeek-MoE / DeepSeek-V3 / Kimi-k2 / PKM / UltraMem / REALM / RETRO / ROME / MEMIT /
  Othello-GPT），外加 Engram 侧概念（conditional memory / Sparsity Allocation / algorithm-system
  co-design / Zipfian distribution）。`type=RelatedWork` 用于 prior work 提及。
- **Object**（6 个）：
  - 两个 Engram 自身差异 claim：`OBJ-DIFF-PRIMITIVE`（`ArticleClaim`, claim_type=comparison）、
    `OBJ-DIFF-CODESIGN`（`SystemDesignClaim`, claim_type=comparison）。
  - 四个主题 comparison 对象：`OBJ-RW-NGRAM` / `OBJ-RW-MOE` / `OBJ-RW-MEM` / `OBJ-RW-KNOW`
    （type=`RelatedWorkComparison`，payload 列 prior_work + summary，全部被所引 atom 支撑）。
- **Relation**（4 条，全 ID 化、带证据）：
  - `R-01 claim_extends_prior_work`：`OBJ-DIFF-PRIMITIVE` → `OBJ-RW-NGRAM`（Engram 延续 N-gram embedding 谱系）。
  - `R-02 claim_contrasts_with_work`：`OBJ-DIFF-PRIMITIVE` → `OBJ-RW-NGRAM`（对比 SCONE/OverEncoding 的不公平协议）。
  - `R-03 claim_contrasts_with_work`：`OBJ-DIFF-CODESIGN` → `OBJ-RW-NGRAM`（对比 Layer-0 placement）。
  - `R-04 claim_contrasts_with_work`：`OBJ-DIFF-PRIMITIVE` → `OBJ-RW-MOE`（iso-param/iso-FLOPs MoE baseline 对比）。

---

## 7. 负例 do_not_extract（§11）

- **Figure 7 题注（源 326 行）**：物理上夹在 N-gram 两段之间，但属于上一章 Section 6 的 gating 可视化，
  **不纳入 Section 7 的 atom**。以 `kind: cross_section_forward_reference` 记入 `do_not_extract`，
  并在 `source.md` 头部注释 + `parsing_notes` + `conventions.figures` 三处声明，确保不被误抽。
- **密集 inline 引文**：本章 prose 被 `(Author, Year)` 引文饱和。处理方式有二：
  (1) 引文**逐字保留在各 atom 的 `raw_text`** 里（不破坏 verbatim span），但在 `normalized_text` 中压缩/省略；
  (2) `do_not_extract` 给出 4 条代表性 citation 负例（`(Shannon, 1948)` / `(Yu et al., 2025)` /
  `(Huang et al., 2025a; Yu et al., 2025)` / `(Geva et al., 2021)`）+ 一条
  `kind: citation_policy` 的 `inline_author_year_citation` 规则（含 24 条 `examples`），
  统一压制全章 citation over-extraction。

---

## v0.1 → v0.3.3 change-list

保留全部 atom/chunk/object/relation 的 **ID 与语义**；仅按 v0.3.3 schema 重表 + 补 span/负例。

| 维度 | v0.1 | v0.3.3 |
| --- | --- | --- |
| 证据 atom | 改写摘要、无坐标 | `raw_text` 逐字 + `normalized_text` 仅渲染该 span；双坐标 `source_span`（权威，file=source_file）+ `viewer_span`（viewer_only，file=source.md） |
| span 来源 | 无 / 手写 | `build.py` 以 anchor 在原始文件定位，断言 `source_file[span]==raw_text` 且 `source.md[span]==raw_text`（绝不手写坐标） |
| source_elements | 无 | 由 atom 的 `source_element_id` 自动分组生成（1 个 heading + 10 个 paragraph 元素） |
| chunk | 4 个 related_work_comparison_block | 不变，新增 `boundary_reason` / `central_atom_ids` / `gold_must_cover_atoms` |
| package | 覆盖不全 | **每个 chunk 一个 package**，`atoms == chunk.atom_ids`，带 `expected_objects`；`PKG-RW-NGRAM` 加 `expected_local_fields[OBJ-DIFF-PRIMITIVE]`（跨 package 对象本地可抽字段） |
| 对象证据 | 平铺 | 拆 `local_evidence_atom_ids`（home chunk 内）+ `supporting_context_atom_ids`（他 chunk）+ `home_package`；`OBJ-DIFF-PRIMITIVE` 的 `A-MOE-INTRO` 移入 supporting |
| 标签 | 小数 `confidence` | `gold_label` + `difficulty` + `evidence_strength`（无 confidence） |
| 负例 | 无 | Figure 7 跨章题注（`cross_section_forward_reference`）+ 4 条 citation + `inline_author_year_citation` policy（24 examples） |
| 校验 | 仅人工 | `validate.py`（与 ch00 模板一致）跑全部 v0.3.3 不变量，**ALL CHECKS PASS** |

**counts**：atoms=13、source_elements=12、chunks=4、packages=4、objects=6、relations=4、mentions=24、do_not_extract=6。
