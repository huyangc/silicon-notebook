# Engram 论文 **第 4 章 Large Scale Pre-training** 在 qiefen 方案下的结构化抽取（schema v0.3.3）

源文件：`pdf_parser/engram_paper_mineru.md`（MinerU 输出，**权威坐标**）
范围：**仅第 4 章（源文件 158–196 行）**，含 `4.1 Experimental Setup` + `4.2 Experimental Results` + **Table 1**。
**不含** Table 2（行 190–192，属第 5 章 Long Context Training）。
profile：`article_research`（论文型）。schema 版本：**v0.3.3**（gold/source 由 `build.py` 生成，`validate.py` 校验，勿手改）。

本文逐 stage 说明第 4 章经 `qiefen.md` 流水线后的形态；可加载 gold 见同目录 [`gold.yaml`](./gold.yaml)。
本章是 **experiment_setup_block + experiment_result_block** 的范例，重点演示 **HTML 表格**的 atom 化与证据绑定。

---

## 版本变更：v0.1 → v0.3.3（本次升级）

保留 v0.1 的全部知识内容（atom / object / relation 的 ID 与含义），仅按 v0.3.3 规范重新表达并补齐逐字 span。

| 维度 | v0.1（旧） | v0.3.3（本版） |
| --- | --- | --- |
| 坐标 | 无 line/char span，atom 只有改写后 `text` | **双坐标**：`source_span`（权威，绑 `engram_paper_mineru.md`）+ `viewer_span`（`viewer_only:true`，绑 `source.md`）。`raw_text` 为源文件逐字连续子串，`normalized_text` 只渲染该 span |
| 证据基准 | `text` 是摘要 | `raw_text` 逐字；脚本断言 `source_file[span]==raw_text` 且 `source.md[span]==raw_text` |
| Table 1 | atom 用人写管道串 | `table_caption_atom`（行 162 caption）+ `table_header_atom`（行 164 内表头 `<tr>` 逐字子串）+ 12 个 `table_row_atom`（行 164 内该行 `<td>` 逐字子串）；`normalized_text` 给可读 `Metric | v1 | v2 | v3 | v4`；表行数值经 NUMERIC over-span 审计 |
| 对象证据 | `evidence_atom_ids` + 小数 `confidence` | 拆为 `local_evidence_atom_ids`（在 home_package 的 chunk 内）+ `supporting_context_atom_ids`（空）+ `home_package`；标签改 `gold_label/difficulty/evidence_strength`（**去掉 confidence**） |
| package | 仅 PKG-TABLE1，且 atoms ⊂ chunk | 每 chunk 一个 package（PKG-SETUP / PKG-TABLE1），`atoms == chunk.atom_ids`，带 `expected_objects` |
| source_elements | 无 | 自动生成（heading + paragraph + 单个 `table` 元素 SE-T1） |
| 负例 | 无 | `do_not_extract`：4 条 inline citation + 1 条 citation_policy + Appendix A/B + **Table 2/ Figure 3 出界引用** |
| 章引拆分 | `A-CH4-INTRO` 一条（跨 Table 1 断句） | 拆 `A-CH4-INTRO`（行 160）+ `A-CH4-INTRO-CONT`（行 166），各为连续逐字 span |
| 4.2 散文 | 全部当作 4.2 | 行 188 在 viewer slice 内；行 194/196 被 MinerU 排到 Table 2 之后，**出 viewer slice**，故 `A-RES-ENGRAM-VS-MOE/A-RES-CLAIM/A-RES-ENGRAM40` 只带 `source_span`、`metadata.out_of_viewer_slice:true`，无 viewer_span |
| relation | `experiment_tests_setup` + 小数 confidence | 保留 `R-01..R-03` 与类型；`experiment_tests_setup` 作为**已说明的扩展类型**（结果由其设置产生，比 `experiment_tests_claim` 更贴切，因端点是 ExperimentSetup 而非 Claim）；标签改 gold_label/difficulty/evidence_strength |

校验（`validate.py`）全绿：YAML 可解析、双 span 逐字、结构引用、chunk 覆盖、`package.atoms==chunk.atom_ids`、
`expected_objects⊆objects`、对象 local⊆home-chunk 且 supporting⊄home-chunk、关系端点∈objects、无 confidence、
table_row NUMERIC 审计、GLOBAL no-orphan（27 atom 全部被某 object/relation 证据引用）。

计数：atoms=27, source_elements=17, chunks=2, packages=2, objects=4, relations=3, mentions=16, do_not_extract=8。

---

## 0. Profile 判定（§2.1）

命中 `Experimental Setup` / `Experimental Results` / `we train four models` / `Table 1` /
`validation loss` / `iso-FLOPs` / `Appendix B` → `article_research`。
抽取目标（§2.1）：本章主要落在 `ExperimentSetup` / `ExperimentResult` / `ArticleClaim`
（method/architecture 已在第 2 章，scaling law 在第 3 章）。

---

## 1. SourceElement（§3.1）— MinerU 在第 4 章里的真实形态与坑

- 标题：`# 4. Large Scale Pre-training`、`# 4.1. Experimental Setup`、`# 4.2. Experimental Results`。
- **Table 1 是单个 HTML `<table>`**（行 164），不是 markdown 管道表，也不是 `<details>`：
  - 表头是**第一行 `<tr>`**：`'' | Benchmark (Metric) | # Shots | Dense-4B | MoE-27B | Engram-27B | Engram-40B`
    （首列空 td 是 rowspan 分组列的占位）。
  - 左侧 `rowspan="5"/"16"/"4"/"7"` 的单元格是 **metric 分组标签**
    （`Language Modeling` / `Knowledge & Reasoning` / `Reading Comprehension` / `Code & Math`），
    **不是独立数据列**；解析时必须把它识别为 row-group 而非 benchmark。
  - HTML 实体 `&amp;` 出现在分组名里（`Knowledge &amp; Reasoning`）→ atom text 写成纯文本 `and`。
- **表题（caption）在表格上方一段**（行 162），含关键控制变量（262B tokens、3.8B activated、
  72→55 专家重分配、5.7B / 18.5B Engram）→ 单独成 `table_caption_atom`。
- 表格被正文打断：行 160 末尾 `(2) MoE-27B` 与行 166 `(26.7B total parameters), (3) ...`
  其实是同一句被 Table 1 插在中间——MinerU 的版式还原坑；语义上属章引，已合并进 `A-CH4-INTRO`。
- 公式行内 `$\rho = 74.3\%$`、`$5\times$`、`$N$`-gram → ASCII 化为 `rho = 74.3%`、`5x`、`N-gram`。

> 解析要点：**表头-表行依赖**（§5.3）。每个 `table_row_atom` 的 text 必须自带列语义
> （`Dense-4B X | MoE-27B Y | Engram-27B Z | Engram-40B W`），否则脱离表头后数值无法对位。

---

## 2. Section Tree（§3.2）

```
4 Large Scale Pre-training
  4.1 Experimental Setup
  4.2 Experimental Results   (Table 1 在 4.0/4.2 之间，挂在 SEC-4)
```

Table 1 的 caption/header/row atom 归到父节 `SEC-4`（表格物理上在 4.1 之前出现、被 4.2 正文引用），
4.1/4.2 的散文分别归 `SEC-4.1` / `SEC-4.2`。

---

## 3. EvidenceAtom（§3.3）— 27 个 atom 的分布

| 区块 | atom_type | 代表 atom |
| --- | --- | --- |
| 章引 | `experiment_setup_atom` ×2 | `A-CH4-INTRO`（行 160，模型 1-2）+ `A-CH4-INTRO-CONT`（行 166，模型 3-4 + 控制变量；Table 1 把该句断成两半） |
| 4.1 数据/骨干 | `experiment_setup_atom` | `A-SETUP-DATA`（262B/DeepSeek-v3 128k）、`A-SETUP-BACKBONE`（30 块、hidden 2560、MLA 32 头、mHC×4、Muon） |
| 4.1 四模型 | `experiment_setup_atom` | `A-SETUP-DENSE` / `A-SETUP-MOE`（72+2,top-6）/ `A-SETUP-ENGRAM27`（72→55, 5.7B, rho=74.3%, 层 2&15, N-gram 3, 8 头, dim 1280, Adam lr×5）/ `A-SETUP-ENGRAM40`（18.5B） |
| 4.1 评测 | `experiment_setup_atom` | `A-SETUP-EVAL`（四大类 benchmark 列表） |
| Table 1 表头/题 | `table_caption_atom` / `table_header_atom` | `A-T1-CAP`（行 162 caption）/ `A-T1-HEADER`（行 164 内表头 `<tr>` 逐字子串） |
| Table 1 关键行 | `table_row_atom` ×12 | 配置行（Total Params、Engram Params）+ loss + MMLU/MMLU-Pro/CMMLU/BBH/ARC-Challenge/DROP/HumanEval/GSM8K/MATH；每行 `raw_text` 是行 164 内该行 `<td>` 逐字子串 |
| 4.2 结果说明 | `result_sentence` ×4 | `A-RES-SPARSE-DENSE`（行 188，稀疏>稠密）、`A-RES-ENGRAM-VS-MOE`（行 194，逐项 +N.N 增益）、`A-RES-CLAIM`（行 194，超越纯 MoE 的假设）、`A-RES-ENGRAM40`（行 196，40B 欠训练）；后三者在 viewer slice 外，仅带 source_span |

金标准**只收高价值行**（loss + 各域代表 benchmark），不逐行展开 Table 1 的全部 27 个 metric 行；
这与 §3.1“大章节可只收高价值段落，但每个 atom 都能在 source 中找到原文”一致。

---

## 4. SemanticChunk（§4.1 论文型 chunk）— 两块

| chunk | chunk_type | section_path | boundary_reason |
| --- | --- | --- | --- |
| `C-SETUP` | `experiment_setup_block` | 4 > 4.1 | `experiment_setup_result_dependency`：数据+骨干+四模型配置+评测协议是一个完整设置单元，保持同块 |
| `C-TABLE1` | `experiment_result_block` | 4 > 4.2 | `table_header_row_dependency`：caption+header+所有 metric 行必须与正文 result_sentence 同块，否则表行失去列语义；结果句与其支撑的 table_row 共块以支撑 ExperimentResult→ArticleClaim |

切点选在 4.1/4.2 边界（`heading_change` + `anchor_type_change`：setup→result），
**不**在表格内部切（`-table_header_row_dependency`），也不把 result_sentence 与 Table 1 拆开
（`-experiment_setup_result_dependency`）。

---

## 5. ContextPackage（§6 论文型示例）

`C-TABLE1` 的 package `PKG-TABLE1`：

```
Document: Engram paper
Section: Chapter 4 > 4.2 Experimental Results > Table 1
Atoms:
[A-T1-CAP]        Table 1 caption (262B tokens, 3.8B activated, 72->55 reallocation)
[A-T1-HEADER]     Benchmark (Metric) | # Shots | Dense-4B | MoE-27B | Engram-27B | Engram-40B
[A-T1-VALLOSS]    validation loss 1.768 / 1.634 / 1.622 / 1.610
[A-T1-MMLU/BBH/ARCC/GSM8K/MATH]  representative benchmark rows
[A-RES-ENGRAM-VS-MOE]  Engram-27B vs MoE-27B +N.N deltas
linked_context:
  table_caption: "Table 1 | Pre-training performance comparison ..."
  table_headers: "Benchmark (Metric) | # Shots | Dense-4B | MoE-27B | Engram-27B | Engram-40B"
  previous_heading: "4.2 Experimental Results"
  next_heading: "5. Long Context Training"
Targets: ExperimentResult, ArticleClaim
```

`linked_context.table_caption` / `table_headers` 把列语义随包带给 LLM——这是 §10
“表格证据绑定到 table_caption / table_header / table_row”在 package 层的体现。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8、§10）

- **Mention（16 个）**：四个模型名（Dense-4B / MoE-27B / Engram-27B / Engram-40B）、
  DeepSeek-v3 tokenizer / MLA / Muon / DeepSeekMoE（设置类）；
  validation loss / MMLU / BBH / ARC-Challenge / GSM8K / MATH（结果类）；
  iso-FLOPs / conditional computation（claim 类）。
- **Object（4 个）**：
  - `SETUP-PRETRAIN`（ExperimentSetup，home=PKG-SETUP）：四模型完整配置 + 262B tokens + 控制变量 + 评测协议，
    local evidence 绑到 9 个 `experiment_setup_atom`（含拆出的 `A-CH4-INTRO-CONT` 与 `A-SETUP-EVAL`）。
  - `RESULT-TABLE1`（ExperimentResult，home=PKG-TABLE1）：Table 1，payload.config/key_findings 逐行列出
    config（Total/Engram Params）+ loss / MMLU / CMMLU / BBH / ARC-Challenge / DROP / HumanEval / GSM8K / MATH 的四列数值，
    **local evidence 绑到 caption + header + 12 个 table_row atom + `A-RES-ENGRAM40`**（§10 表格证据绑定）。
  - `CLAIM-MEMORY-BEYOND-KNOWLEDGE`（ArticleClaim）：增益超越知识、延伸到推理/代码/数学，
    payload.supporting_deltas 记录 Engram-27B over MoE-27B 的逐项 +N.N。
  - `CLAIM-SPARSE-OVER-DENSE`（ArticleClaim）：稀疏架构 scaling law 优于稠密。
- **Relation（3 条，全 ID 化、带 gold_label/difficulty/evidence_strength）**：
  - `R-01 experiment_tests_setup`：`RESULT-TABLE1 → SETUP-PRETRAIN`（结果由该设置产生）。
    `experiment_tests_setup` 是对 §8.1 词表的**已说明扩展**：spec 的 `experiment_tests_claim` 端点是 Claim，
    而这里关系的终点是 ExperimentSetup（“这组结果检验/产生自这套设置”），语义更贴切，故保留 v0.1 的类型名。
  - `R-02 result_supports_claim`：`RESULT-TABLE1 → CLAIM-MEMORY-BEYOND-KNOWLEDGE`，
    evidence 绑到 MMLU/CMMLU/BBH/ARC-Challenge/DROP/HumanEval/GSM8K/MATH 行 + 结果句——
    **关系证据落在 table_row atom**（§10 的核心要求）。
  - `R-03 result_supports_claim`：`RESULT-TABLE1 → CLAIM-SPARSE-OVER-DENSE`（loss 行 + 稀疏>稠密句）。

> 关键设计：ExperimentResult 的字段（每个 benchmark 的四列数值）逐一被对应 `table_row_atom` 支撑，
> result→claim 关系的 evidence 也直接指向这些表行，满足 §10“表格证据绑定到 table_row”
> 与 §13.3“Relation Evidence Accuracy / Table Row Parsing Accuracy”。
