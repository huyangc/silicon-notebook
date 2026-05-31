# qiefen 方案测试用例 —— 章节级三元组（schema v0.3.3）

本目录基于 [`fangan/qiefen.md`](../qiefen.md) 的
**“文档切分 → 实体识别 → 关系识别”** 方案，为抽取算法开发提供 gold 测试用例。

## 规范文档（先读）

| 文档 | 内容 |
| --- | --- |
| [`../qiefen.md`](../qiefen.md) | 方案总体设计（流水线、对象/关系模型、评估指标） |
| [`../article_research_gold_spec.md`](../article_research_gold_spec.md) | **论文型** gold 规范 + 修订过程（v0.1→v0.3.3，参考 `engram/ch02_architecture`） |
| [`../textbook_gold_spec.md`](../textbook_gold_spec.md) | **书籍/教材型** gold 规范 + 修订过程（v0.3.3-textbook，参考 `cmos/ch02_cmos_technology`） |

> 两份 spec 的**流程/坐标约定/不变量一致**，只有**类型词表**与解析坑不同。下方为快速索引；细节以 spec 为准。

## 三元组（每个章节一套）

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `source.md` | **viewer-only 原文切片** | 原始 MinerU 文件对应行段的逐字拷贝，头部注释声明"非权威"。仅供人工 review / UI 跳转。 |
| `gold.yaml` | **golden parse result（权威）** | 全流水线金标准输出。所有严格评测坐标用 `atom.source_span`（指向**原始 MinerU 文件**）。 |
| `analysis.md` | **逐 stage 分析** | 说明该章如何被解析，记录 MinerU 渲染坑与切分理由 + 版本变更表。 |
| `build.py` / `validate.py` | 可选 | 由原始文件 anchor 计算 span 的构建器 + 不变量校验器（span 不手写）。 |

**坐标模型**：每个 atom 带 `source_span`（权威，file=原始 .md）+ `viewer_span`（`viewer_only:true`，file=source.md）。
不变量：`source_file[source_span.char_*] == source.md[viewer_span.char_*] == raw_text`。
`raw_text` 逐字、`normalized_text` 只渲染该 span（公式 atom = 纯公式，语义在对象层）。

## 目录结构

```
fangan/testcases/
  README.md                       ← 本文件
  _AGENT_SPEC.md                  ← 早期产出契约（已被两份 *_gold_spec.md 取代/细化）
  engram/   (论文型 article_research，9 章，schema v0.3.3)
    ch00_abstract/  ch01_introduction/  ch02_architecture/  ch03_scaling_laws/
    ch04_pretraining/  ch05_long_context/  ch06_analysis/  ch07_related_work/  ch08_conclusion/
  cmos/     (书籍型 textbook，5 章，schema v0.3.3-textbook —— 该 MinerU 文件实含 Ch1-4 与 Ch9)
    ch01_introduction/  ch02_cmos_technology/  ch03_device_modeling/
    ch04_analog_subcircuits/  ch09_switched_capacitor/
```

每个章节目录下：`source.md` + `gold.yaml` + `analysis.md`（+ `build.py`/`validate.py`）。

## 两个源文件

| profile | schema | 文件 | 说明 |
| --- | --- | --- | --- |
| `article_research` | v0.3.3 | `pdf_parser/engram_paper_mineru.md` | Engram 论文（Conditional Memory via Scalable Lookup） |
| `textbook` | v0.3.3-textbook | `pdf_parser/.../CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md` | CMOS 模拟电路设计教材 |

> CMOS 源文件经 MinerU 抽取实含 **Ch1–4 与 Ch9**（Ch5–8 是 "THIS ITEM IS NOT YET UPLOADED" 占位），故 cmos 只 5 章。

## 覆盖规模（全部通过 v0.3.3 不变量校验）

| 章节 | profile | atoms | chunks | objects | relations |
| --- | --- | --: | --: | --: | --: |
| engram/ch00_abstract | article | 11 | 1 | 9 | 9 |
| engram/ch01_introduction | article | 18 | 4 | 13 | 10 |
| engram/ch02_architecture *(参考)* | article | 39 | 6 | 21 | 18 |
| engram/ch03_scaling_laws | article | 17 | 3 | 9 | 8 |
| engram/ch04_pretraining | article | 27 | 2 | 4 | 3 |
| engram/ch05_long_context | article | 17 | 3 | 5 | 6 |
| engram/ch06_analysis | article | 44 | 7 | 21 | 20 |
| engram/ch07_related_work | article | 13 | 4 | 6 | 4 |
| engram/ch08_conclusion | article | 8 | 1 | 8 | 7 |
| cmos/ch01_introduction | textbook | 38 | 9 | 18 | 13 |
| cmos/ch02_cmos_technology *(参考)* | textbook | 86 | 21 | 64 | 29 |
| cmos/ch03_device_modeling | textbook | 75 | 17 | 33 | 30 |
| cmos/ch04_analog_subcircuits | textbook | 75 | 16 | 47 | 50 |
| cmos/ch09_switched_capacitor | textbook | 73 | 22 | 48 | 41 |
| **合计（14 章）** | | **541** | **116** | **306** | **248** |

## 不变量（CI 可执行；当前 14 章全部 0 问题）

权威 span（`source_file[source_span] == raw_text`，必跑）+ viewer span（可选）；结构引用
（section_id / source_element_id）；chunk 覆盖；`package.atoms == chunk.atom_ids`；
`expected_objects ⊆ objects` 且 `expected_local_fields ⊆ payload`；对象
`local ⊆ home-chunk & supporting ⊄ home-chunk` 且对象 ∈ 其 `home_package.expected_objects`；
关系端点 ∈ objects；mention / do_not_extract 引用解析；**无 `confidence`**；
raw/normalized 跨 span 审计（公式 atom 纯公式、数字去空格后比对）；
**全局无孤儿**（atom ∈ object/relation 证据，或 `context_only`，或 `core:false` cross_reference 且在 `do_not_extract`）。

完整 14 条清单见两份 spec 文档第三部分。

## 对应的评估指标（qiefen §13）

- **切分质量**：`section_tree` / `evidence_atoms` / `semantic_chunks`（`gold_must_cover_atoms` 给 Object Integrity）→ Evidence Recall@Chunk、Over/Under-splitting。
- **证据绑定**：`source_span` + `raw_text` → atomizer 原文定位（公式按 tag、表行、例题步骤）。
- **抽取质量**：`objects`（local/supporting 证据）+ `context_packages.expected_objects` / `expected_local_fields` → Object/Field P/R（按 package 算 object-recall）。
- **关系质量**：`relations`（端点类型匹配 + 证据）→ Endpoint Validity / Type Accuracy。
- **负例控制**：`do_not_extract` → over-extraction（citation / 图引 / 图内 label / 跨章引用 / netlist）抑制率。
