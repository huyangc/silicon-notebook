# Engram 论文 **第 5 章 Long Context Training** 在 qiefen 方案下的结构化抽取

源文件：`pdf_parser/engram_paper_mineru.md`
范围：**仅第 5 章（源文件 190–221 行）**，含 5.1 Experimental Setup、5.2 Experimental Results，以及物理上渲染在 5.x 文字之前的 **Table 2**（本章结果表）。
profile：`article_research`（论文型）
schema 版本：**v0.3.3**（由 v0.1 升级而来；变更见文末「v0.3.3 相对 v0.1 的差异」）。

可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)；spans 由 `build.py` 对原始文件定位计算（双坐标 + 逐字 span），`validate.py` 跑全部 v0.3.3 不变量。

---

## 0. Profile 判定（§2.1）

命中 `Experimental Setup` / `Experimental Results` / `Table 2` / `RULER` / `LongPPL` / "our results indicate" / "we demonstrate" / "iso-pretraining-loss" → `article_research`。
抽取目标（§2.1）：`ArticleClaim` / `ExperimentSetup` / `ExperimentResult` / `MechanisticExplanation`。

---

## 1. SourceElement（§3.1）— MinerU 在本章里的真实形态与坑

- 标题：`# 5. Long Context Training`、`# 5.1. Experimental Setup`、`# 5.2. Experimental Results`。
- 段落含粗体导语：`Training Details.` / `Model Configurations.` / `Evaluation Benchmarks.`（MinerU 不单独成元素，需按句切 atom）。
- 公式片段：YaRN 超参 `$s=10, \alpha=1, \beta=32$`、`f = 0.707`，ASCII 化为 `s=10, alpha=1, beta=32, f=0.707`。
- **Table 2 = 关键坑**：HTML `<table>` 用 **嵌套三行表头** 渲染——
  - 第 1 行：`Model`(rowspan=3) + `LongPPL (32k)`(colspan=4) + `RULER (32k)`(colspan=8)；
  - 第 2 行：`Perplexity (↓)`(colspan=4) + `NIAH Accuracy (↑)`(colspan=4) + `Other Tasks (↑)`(colspan=4)；
  - 第 3 行细列：`Book/Paper/Code/L-CoT`，`S/MK/MV/MQ`，`VT/CWE/FWE/QA`。
  解析时必须把三行折叠成单一列语义（列 = 指标组 + 子组 + 细列 + 方向 ↑/↓），`table_row` 数值才能对齐到正确列。
- **位置坑**：Table 2 物理上位于第 4 章末（紧接 Table 1 解读之后、`# 5.` 之前），但其 caption、表内 (steps,loss) 标注和全部解读都在第 5 章，故归入本章（见 `parsing_notes`）。

---

## 2. Section Tree（§3.2）

```
5 Long Context Training
  5.1 Experimental Setup
  5.2 Experimental Results
```

`SEC-5` 承载章引 claim/mechanism；`SEC-5.1` 承载实验设置；`SEC-5.2` 承载 Table 2 与三段解读。

---

## 3. EvidenceAtom（§3.3）— 共 17 条

- **章引（SEC-5）**：`A-INTRO-CLAIM`（claim_sentence，free attention capacity → long-context）、`A-INTRO-MECH`（mechanism_sentence，把局部依赖委托给 O(1) lookup）。
- **5.1 设置（5 条 experiment_setup_atom）**：`A-SETUP-YARN`（YaRN→32768，5000 步/30B token，超参）、`A-SETUP-CONFIGS`（四个 checkpoint：MoE-27B 50k；Engram-27B 41k/46k/50k）、`A-SETUP-ISOLOSS`（46k 预训练 loss == MoE 50k，Iso-Loss 控制变量）、`A-SETUP-LONGPPL`（book/paper/code/L-CoT 四类）、`A-SETUP-RULER`（14 子集→8 类）。
- **Table 2（6 条）**：`A-T2-CAPTION`(table_caption_atom)、`A-T2-HEADER`(table_header_atom，记录三行嵌套表头折叠语义)、四行 `table_row_atom`：`A-T2-ROW-MOE50K`/`-ENG41K`/`-ENG46K`/`-ENG50K`，逐行携带 12 列原始数值。
- **5.2 解读（4 条 result_sentence）**：`A-RES-COUPLING`（长上下文与基模质量耦合→须对齐 loss 而非步数）、`A-RES-ISOLOSS`（46k vs MoE50k：MQ NIAH 97.0 vs 84.2；VT 87.2 vs 77.0）、`A-RES-ISOFLOPS`（50k 全面最优）、`A-RES-EXTREME`（41k ~82% FLOPs：LongPPL 持平、RULER 反超）。

关键数值核对：MoE-27B(50k) MQ=84.2 / VT=77.0 / FWE=73.0；Engram-27B(46k) MQ=97.0 / VT=87.2；Engram-27B(50k) MQ=97.0 / VT=89.0——均与 Table 2 原文一致。

---

## 4. SemanticChunk（§4.1 论文型 chunk）— 3 块

| chunk | chunk_type | section_path | boundary_reason |
| --- | --- | --- | --- |
| `C-INTRO` | `article_core_claim_block` | 5 | 章引 claim + 其机制依据放一块 |
| `C-SETUP` | `experiment_setup_block` | 5 > 5.1 | training details + 四 checkpoint + Iso-Loss 控制 + 两个 benchmark 构成自洽设置单元；与结果块相邻（experiment_setup_result_dependency） |
| `C-RESULT` | `experiment_result_block` | 5 > 5.2 | Table 2（caption+表头+四行）与其文字解读保持同块（table_header_row_dependency；result 句直接引用表内精确数值） |

切分依据（§5.3）：在 `# 5.1` / `# 5.2` 章节边界切（structural_boundary + heading_change）；但 **不在** 表头与表行之间、设置与结果之间切——这正是 `C-RESULT` 把 caption/header/4 行/解读绑在一起、`C-SETUP` 与 `C-RESULT` 紧邻的原因。

---

## 5. ContextPackage（§6 论文型示例）

`C-RESULT` 的 package `PKG-RESULT`：

```
Document: Engram
Section: 5 > 5.2 Experimental Results
Atoms:
[A-T2-CAPTION]    Table 2 caption
[A-T2-HEADER]     nested 3-row header (折叠语义)
[A-T2-ROW-MOE50K] baseline 行
[A-T2-ROW-ENG46K] Iso-Loss 行
[A-T2-ROW-ENG50K] Iso-FLOPs 行
[A-RES-ISOLOSS]   MQ 97.0 vs 84.2; VT 87.2 vs 77.0
[A-RES-EXTREME]   41k ~82% compute 持平/反超
linked_context:
  table_caption: "Table 2 | Long-context performance comparison"
  table_headers: "Model | LongPPL(32k){Book,Paper,Code,L-CoT} | RULER(32k){NIAH:S,MK,MV,MQ ; Other:VT,CWE,FWE,QA}"
  previous_heading: "5.1 Experimental Setup"
  next_heading: "6. Analysis"
Targets: ExperimentResult, ArticleClaim
```

`linked_context.table_headers` 显式携带折叠后的表头语义，使 LLM 能把每个 `table_row_atom` 的 12 个数值对齐到正确列。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8.1）

- **Mention（10 条）**：YaRN / Iso-Loss / LongPPL / RULER / Multi-Query NIAH / Multi-hop Variable Tracking / Engram-27B(46k) / Engram-27B(50k) / MoE-27B(50k) / attention capacity。
- **Canonicalization**：`iso_loss_setting`（Iso-Loss = iso-pretraining-loss = Engram-27B(46k)）、`iso_flops_setting`、`mq_niah`（MQ = Multi-Query NIAH）、`ruler_vt`（VT = Multi-hop Variable Tracking）、`longppl_benchmark`。
- **Object（5 个）**：
  - `ArticleClaim` x 2：`CLAIM-FREE-ATTENTION`（Engram 释放注意力容量→更好长上下文检索）、`CLAIM-ALIGN-LOSS`（长上下文与基模质量耦合，须对齐 loss）。
  - `MechanisticExplanation`：`MECH-LOOKUP-DELEGATION`（把局部依赖委托给 O(1) lookup）。
  - `ExperimentSetup`：`SETUP-LONGCTX`（YaRN/步数/超参/四 config/Iso-Loss 控制/两 benchmark）。
  - `ExperimentResult`：`RESULT-TABLE2`（Table 2，含 metric_groups 折叠表头 + key_rows 关键数值 + findings）。
- **Relation（6 条，全 ID 化、带证据）**：
  - `experiment_tests_claim`：`SETUP-LONGCTX` → `CLAIM-FREE-ATTENTION`（R-01）
  - `result_supports_claim`：`RESULT-TABLE2` → `CLAIM-FREE-ATTENTION`（R-02，证据为 46k/MoE50k 行 + Iso-Loss 解读）；`RESULT-TABLE2` → `CLAIM-ALIGN-LOSS`（R-03，证据为 41k/50k 行 + coupling 句）
  - `mechanism_explains_result`：`MECH-LOOKUP-DELEGATION` → `RESULT-TABLE2`（R-04）
  - `mechanism_explains_claim`：`MECH-LOOKUP-DELEGATION` → `CLAIM-FREE-ATTENTION`（R-05）
  - `result_reported_in_setup`：`RESULT-TABLE2` → `SETUP-LONGCTX`（R-06，把表与其设置 benchmark 绑回）

> 任务点名的两条核心关系 `result_supports_claim`（R-02/R-03）与 `mechanism_explains_result`（R-04）均落地，证据 atom 全部存在于本章。
> 扩展关系类型（须语义清晰）：`mechanism_explains_claim`（R-05，机制直接解释章引 claim）、`result_reported_in_setup`（R-06，把 Table 2 绑回其 benchmark 设置）。

---

## 7. v0.3.3 相对 v0.1 的差异（本章升级记录）

| v0.1 问题（旧语义结构化答案，不可严格评测） | v0.3.3 处理 |
| --- | --- |
| evidence atom 是改写摘要、无坐标 | 17 个 atom 全部带**双坐标**：`source_span`（权威，file=`engram_paper_mineru.md`）+ `viewer_span`（`viewer_only:true`，file=`source.md`）；`raw_text` 逐字、`normalized_text` 仅渲染该 span（math/箭头/破折号 ASCII 化）。span 由 `build.py` 定位计算并断言 `source_file[span]==raw_text` 且 `source.md[span]==raw_text`，不手写 |
| Table 2 归属/结构错误 | Table 2（物理在 §5 之前、紧接第 4 章）按其 caption/loss 标注/解读归入本章；拆成 `A-T2-CAPTION`（table_caption_atom）+ `A-T2-HEADER`（table_header_atom，**嵌套三行表头**的锚定子串）+ 四个 `table_row_atom`（MoE-27B(50k)/Engram-27B 41k/46k/50k 的锚定 `<tr>` 子串，逐行 12 个 `<td>`）。新增 numeric-over-span 审计：每个 row 的 normalized 数值必逐字出现在该行 raw_text 内 |
| 无 source_elements / 章节坐标 | 新增 `source_elements`（13 条，heading/paragraph/table/table_caption）；`section_tree` = SEC-5 / 5.1 / 5.2 |
| context package 覆盖不全 | 每个核心 chunk 一个 package（PKG-INTRO/SETUP/RESULT），`package.atoms == chunk.atom_ids`，带 `expected_objects`；跨 package 对象 `CLAIM-FREE-ATTENTION` 在 PKG-RESULT 声明 `expected_local_fields`（scope） |
| 对象证据混杂、跨 package 歧义 | 5 个对象拆 `local_evidence_atom_ids`（在 home_package 的 chunk 内）+ `supporting_context_atom_ids`（他 chunk）+ `home_package`；`CLAIM-FREE-ATTENTION` 的 Iso-Loss 证据（A-RES-ISOLOSS / 表行）置为 supporting |
| 用小数 confidence、无负例 | objects/relations 改用 `gold_label/difficulty/evidence_strength`；新增 `do_not_extract`（inline citation：Peng/Fang/Press 等 + `inline_author_year_citation` policy；`Figure 4` 前向引用 §6） |

> v0.3.3 校验（`validate.py`）：YAML 可解析、双坐标逐字（17 atom 全过）、结构引用、每 atom ∈ 某 chunk、
> `package.atoms == chunk.atom_ids`、`expected_objects ⊆ objects`、object local ∈ home chunk 且 supporting ∉ home chunk、
> relation 端点 ∈ objects、mention/do_not_extract 引用可解析、无小数 confidence、GLOBAL 无 orphan、
> table_row numeric-over-span —— **全部通过**。
> counts: atoms=17 source_elements=13 chunks=3 packages=3 objects=5 relations=6 mentions=10 do_not_extract=5。
