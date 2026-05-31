# CMOS 教材 **第 1 章 Introduction and Background** 在 qiefen 方案下的结构化抽取（v0.3.3-textbook）

源文件：`pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md`
（MinerU 输出）
范围：**仅第 1 章（源文件第 3–742 行）**
profile：`textbook`（书籍型）

本文逐 stage 说明第 1 章经 `qiefen.md` 流水线后的形态；可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)；
原始段落见 [`source.md`](./source.md)（逐字切片，viewer-only）；构建/校验脚本见 `build.py` / `validate.py`。

> **v0.3.3-textbook（从 v0.1 升级，按 engram ch2 / CMOS ch2 的修订过程与要求，保留 textbook 的对象/关系类型）**：
> 升级**不改动任何 atom/chunk/object/relation/mention 的 ID 与语义**，只换成 v0.3.3 schema 并补逐字双坐标 span。
> - **双坐标**：每个 atom 带 `source_span`（**权威**，file = 该 mineru .md）+ `viewer_span`（`viewer_only:true`，file = `source.md`）。
>   不变量（脚本校验，全 38 atom 通过）：`source_file[source_span.char_*] == source.md[viewer_span.char_*] == raw_text`。
> - **span 定位（由 `build.py` 计算，不手写）**：段落 atom 取整段或 anchored 子串（同一行多 atom 用唯一子串，如 L13 的三类信号定义、表 `<tr>` 行）；
>   公式按小节行窗口内的 `\tag {N}` 定位（Eq.(1) = tag 1）；7 步流程用跨行连续 region 定位（L111–119）；三张表取其单行 `<table>`/`<tr>`/`<td>` 子串。
> - **raw_text + normalized_text**：`raw_text` 为逐字 span（保留 MinerU 掉空格 `analogdigital`/`Chapter 9Switched CapacitorCircuits`、缺微符 `0.8 m`、`[n]` 引用、`$...$` LaTeX）；
>   `normalized_text` 只渲染该 span（转写 `b_i 2^-i`、浓缩为摘要），**不得引入 span 外的事实/章节号/数字**；公式 atom 的 normalized 只给公式本体（语义名/角色放对象 payload）。
> - 顶层新增 **`source_elements`**（29 个，由 atom span 聚合，公式/表覆盖 type）。
> - 对象用 **`local_evidence_atom_ids` + `supporting_context_atom_ids` + `home_package`**（`expected_objects` 只按 local 考核）；本章无跨 package 对象，故无 `expected_local_fields`。
> - **每个 chunk 配一个 package**（9 个，`atoms == chunk.atom_ids` + `expected_objects`）；`PKG-OV`、`PKG-NOTATION` 的原子全为 `context_only`，`expected_objects: []` 且带 `note`。
> - 标签改 **`gold_label` + `difficulty` + `evidence_strength`**（去掉小数 `confidence`）。
> - 新增 **`do_not_extract`**：`[1]..[6]` 数字引用（policy + 一条 `A-EX-MASTERPLL` 例）、Fig./Eq. 交叉引用、`<details>` 图内 OCR 标签（Fig.1.4-1/1.4-4 错乱的 VCO/VCOREF mermaid）、image 标记、跨章/附录引用（Table 1.1-2 章节映射、1.5 的 Appendix A 指引）。
> - `source_meta` 增加 `conventions` / `validation` 块。
> - **章引/小结/记号表/未建对象的课后题** 标 `context_only`（A-INTRO-CMOS/A-INTRO-CAD/A-SUMMARY、A-NOTATION-INTRO + 四行 Table 1.2-1、A-PROB-2/A-PROB-12），满足全局无孤儿且不凭空造对象。
>
> 规模：**38 atoms / 29 source_elements / 9 chunks / 9 packages / 18 objects / 13 relations / 12 mentions / 6 do_not_extract**。
> 校验通过（`python3 validate.py` → ALL CHECKS PASS）：YAML 解析、双坐标 verbatim、section/source_element 引用、每 atom 入 chunk、
> `package.atoms==chunk.atom_ids`、`expected_objects⊆objects`、`local⊂home-chunk & supporting⊄home-chunk`、对象∈home.expected_objects、
> relation 端点∈objects、mention/do_not_extract 引用可解析、无小数 confidence、数字跨 span 审计无越界、公式 normalized 无 span 外注解、
> **全局无孤儿**（每 atom 是对象/关系证据，或 `context_only`）。下文各 stage 沿用 v0.1 内容（对象/切分思路不变）。

> **源文件处理要点（本次升级关注）**：
> - **设计层级双表**：Table 1.1-1（L170，hierarchy × design/physical/model）、Table 1.1-2（L176，设计层级→章节映射，含 `colspan="3"` 的 "CMOS Technology" banner 与掉空格 `Chapter 9Switched CapacitorCircuits`）、Table 1.2-1（L188，信号记号，example 列是 `$q_{A}$`/`$Q_{A}$`/`$q_{a}$`/`$Q_{a}$`）。
>   caption atom 锚定 "Table 1.x-1" 标题行（L168/L186）；header/row atom 锚定 `<table>` 单行里的 `<tr>` 子串。`A-T112-MAP` 取整行 `<table>`（L176）。
> - **Eq.(1)**：全章唯一编号 display 公式（L16，`$$ D = ... = \sum b_i 2^{-i} \tag {1} $$`，二进制加权数字表示）。tag 按小节复位；`A-SIG-EQ1` 在 1.1 行窗口内按 tag 1 定位，normalized 只给公式本体（变量含义放 `FORMULA-DIGITAL-REP.payload`）。

---

## 0. Profile 判定（§2）

命中 `Chapter 1` / `1.1 …` 小节编号 / `Eq. (1)` / `Table 1.1-1`、`Table 1.2-1` / `Fig. 1.1-1` / `PROBLEMS` → `textbook`。
本章是导论章，**定义性内容多、公式极少**（全章唯一编号公式是 Eq.(1)）。
抽取目标（§2.2）：`Concept` / `Formula` / `ProcessFlow` / `DesignPrinciple` / `ExampleProblem` / `ProblemStatement`。

---

## 1. SourceElement（§3.1）— MinerU 在第 1 章里的真实形态

- 标题：`# Chapter 1 …`、`# 1.1 Analog Integrated Circuit Design`、`# PROBLEMS`。
- 公式：仅 Eq.(1) `$$ D = ... = \sum b_i 2^{-i} \tag{1} $$`（书籍型 tag 按小节编号，本章只有这一条）。
- 表格：三张全部渲染为 HTML `<table>`：
  - Table 1.1-1（设计层级 Hierarchy x {Design, Physical, Model}）、
  - Table 1.1-2（设计层级 -> 章节映射，含 `colspan="3"`），
  - Table 1.2-1（信号记号约定）。
- 波形 / 框图：`<details><summary>line|flowchart|text_image|other|chemical|natural_image</summary>…</details>`：
  - Fig.1.1-1 三个子图各配一个 `<summary>line</summary>` 的小幅值表（采样波形数据）。
  - analysis vs design、design-process、signal-processing-system 是 ```mermaid``` flowchart。
- 图：均为 `![](images/*.jpg)`。

> 解析坑（写入 gold `parsing_notes`）：
> 1. Fig.1.4-1 / Fig.1.4-4 的 mermaid 被 MinerU 解析得**严重错乱**（出现大量重复伪节点 `VCO`/`VCOREF`/`Gain Control` 边），**不可作为 graph edge 证据**——1.4 实例只取正文文字描述的 read-path 步骤。
> 2. 正文的设计步骤是 7 步**编号列表**（Definition…Testing），而 Fig.1.1-3 流程图节点措辞略不同（Implementation / Physical definition / Parasitic extraction / Test and verification）。以正文列表为权威，流程图作旁证；`A-DESIGN-STEPS` 取正文 7 步。
> 3. Fig.1.3-2 / 1.3-3 的频率表数字被 MinerU OCR 成大量 `~10^1`，明显失真，不取其数字，只取"带宽是关键系统考量、CMOS 整合趋势"的定性结论。

---

## 2. Section Tree（§3.2）

```
1 Introduction and Background
  1.1 Analog Integrated Circuit Design
  1.2 Notation, Symbology, and Terminology
  1.3 Analog Signal Processing
  1.4 Example of Analog VLSI Mixed-Signal Circuit Design   (kind: example)
  1.5 Summary
  PROBLEMS (1–12)
```

对应 `section_tree` 的 `SEC-1 / SEC-1.1 / SEC-1.2 / SEC-1.3 / SEC-1.4 / SEC-1.5 / SEC-PROB`。

---

## 3. EvidenceAtom（§3.3）— 共 38 个，按小节分布

- **章引（SEC-1）**：`A-INTRO-CMOS`（CMOS 是混合信号主力、本书主题）、`A-INTRO-CAD`（数字可 CAD 自动化、模拟仍需 hands-on）。
- **1.1 信号定义 + 设计**（核心）：
  - `concept_definition_atom`：`A-SIG-DEF`（信号定义）、`A-SIG-ANALOG`（模拟信号：时间+幅值双连续）、`A-SIG-DIGITAL`（数字信号：幅值离散/量化）、`A-SIG-SAMPLED`（采样数据/采样保持信号）。
  - `formula_atom`：`A-SIG-EQ1` = Eq.(1) 二进制加权 `D = sum b_i 2^-i`。
  - `concept_definition_atom`：`A-ANALYSIS-DESIGN`（analysis 解唯一 vs design 解不唯一）、`A-DESIGN-FORMATS`（design/physical/model 三种描述格式）、`A-HIER-CONCEPT`（devices<circuits<systems 层级）。
  - `design_principle_atom`：`A-IC-VS-DISCRETE`（集成 vs 分立：共衬底匹配、几何可控、不能面包板、器件受限）、`A-DESIGN-SIM-PARASITIC`（**设计原则：仿真 + 版图后寄生迭代**）。
  - `process_flow_atom`：`A-DESIGN-STEPS`（7 步设计流程）。
  - 层级表 Table 1.1-1：`A-T111-CAPTION`/`A-T111-HEADER`/`A-T111-SYSTEMS`/`A-T111-CIRCUITS`/`A-T111-DEVICES`；章节映射表 Table 1.1-2：`A-T112-MAP`。
- **1.2 记号约定**：`A-NOTATION-INTRO` + Table 1.2-1 的 caption/header + 四行约定（`A-T121-TOTAL` 小写量/大写下标、`A-T121-DC` 大写/大写、`A-T121-AC` 小写/小写、`A-T121-RMS` 大写/小写）。
- **1.3 信号处理**：`A-SP-SYSTEM`（输入->预处理->DSP->后处理->输出 流水线）、`A-SP-ANALOG-DIGITAL`（模拟/数字划分原则）、`A-SP-BANDWIDTH`（带宽是关键系统考量）。
- **1.4 混合信号实例**：`A-EX-READCHANNEL`（read/write channel 目标/规格：PRML、64 Mbit/s、0.8um CMOS）、`A-EX-READPATH`（read-path 步骤链）、`A-EX-MASTERPLL`（Master PLL 锁定 C/g_m 时间常数）、`A-EX-RESULT`（强调层级化设计）。
- **1.5 小结**：`A-SUMMARY`。
- **PROBLEMS**：`A-PROB-1`（Eq.(1) 算 11010 的十进制）、`A-PROB-2`（采样保持）、`A-PROB-3`（4-bit 数字化）、`A-PROB-12`（Miller 输入电阻）。

---

## 4. SemanticChunk（§4.2 书籍型 chunk）— 9 块

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-OV` | `chapter_overview_block` | 1 | 章引 + 1.5 小结 |
| `C-SIGNALS` | `concept_definition_block` | 1 > 1.1 | analog/digital/sampled-data 定义 + Eq.(1) |
| `C-ANALYSIS-DESIGN` | `concept_definition_block` | 1 > 1.1 | analysis vs design + 集成 vs 分立 |
| `C-DESIGN-PROCESS` | `design_process_block` | 1 > 1.1 | 7 步设计流程 + 仿真/寄生原则 + 三描述格式 |
| `C-HIERARCHY` | `hierarchy_table_block` | 1 > 1.1 | Table 1.1-1 层级表 + 层级 prose + Table 1.1-2 章节映射 |
| `C-NOTATION` | `concept_definition_block` | 1 > 1.2 | Table 1.2-1 记号约定（表头+四行） |
| `C-SIGPROC` | `concept_definition_block` | 1 > 1.3 | 信号处理框图 + 模拟/数字划分 + 带宽 |
| `C-EX-READCHANNEL` | `example_solution_block` | 1 > 1.4 | read-channel 实例：目标->步骤->Master PLL->层级化结论 |
| `C-PROB` | `problem_set_block` | 1 > PROBLEMS | 课后题 |

> boundary 要点（§5.3）：
> - `C-SIGNALS` 把 Eq.(1) 与它形式化的"数字信号"定义放同块（**formula_continuity**：公式不与其说明分离）。
> - `C-HIERARCHY` / `C-NOTATION` 把表 caption+header+rows 收进同块（**table_header_row_dependency**）。
> - `C-DESIGN-PROCESS` 把 7 步流程与"仿真+版图后寄生"原则、三描述格式放一起（**process_flow_continuity**）。
> - `C-EX-READCHANNEL` 把题/步骤/调谐原则/结论收进同块（**example continuity**）。

---

## 5. ContextPackage（§6 书籍型示例）

`C-SIGNALS` 的 package（`PKG-SIGNALS`）：

```
Document: CMOS Analog Circuit Design
Section: Chapter 1 > 1.1 Analog Integrated Circuit Design > Signal Definitions
Atoms:
[A-SIG-ANALOG]  analog signal (time+amplitude continuous)
[A-SIG-DIGITAL] digital signal (quantized amplitude)
[A-SIG-EQ1]     D = sum b_i 2^-i (Eq.1)
[A-SIG-SAMPLED] sampled-data / sampled-and-held signal
linked_context:
  figure_caption: "Figure 1.1-1 Signals (a)analog (b)digital (c)sampled data"
  formula_context: "Eq.(1) is the binary-weighted representation under the digital-signal definition"
  previous_heading: "1.1 Analog Integrated Circuit Design"
  next_heading:     "Analysis vs Design"
Targets: Concept, Formula
```

第二个示例 `PKG-DESIGN-PROCESS` 绑定 `C-DESIGN-PROCESS`（7 步流程 + 寄生原则 + 描述格式，linked Fig.1.1-3）。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8）

- **Mention（12 个）**：analog signal / digital signal / analog sampled-data signal / binary-weighted sum / synthesis(design) / analysis / design process / parasitic extraction / design hierarchy / signal-processing system / read/write channel / Master PLL。
- **Object（18 个，覆盖该章对象类型，详见 `gold.yaml::objects`）**：
  - `Concept`：analog/digital/sampled-data signal、analysis、design、design hierarchy（含 Table 1.1-1 结构化 payload）、description formats、signal-processing system。
  - `Formula`：digital representation（Eq.1）。
  - `ProcessFlow`：analog IC design process（7 步）、read-channel read path。
  - `DesignPrinciple`：仿真+版图后寄生（`PRINCIPLE-SIM-PARASITIC`）、集成 vs 分立、模拟/数字划分、Master PLL 调谐。
  - `ExampleProblem`：read/write channel 实例。
  - `ProblemStatement`：课后题 1、3。
- **Relation（13 条，全 ID 化、带证据，详见 `gold.yaml::relations`）**：
  - `concept_contrasts_with_concept`：analog vs digital（R-01）、sampled vs analog（R-02）、analysis vs design（R-03）。
  - `formula_defines_variable`：digital representation -> digital signal（R-04）。
  - `process_flow_has_step`：design process -> 仿真/寄生原则（R-05）；read-channel 实例 -> read path（R-10）。
  - `design_principle_applies_to_scenario`：仿真+寄生 -> design process（R-06）、集成 vs 分立 -> design process（R-07）、模拟/数字划分 -> 信号处理系统（R-09）、Master PLL -> read path（R-11）。
  - `concept_defines_term`：design hierarchy -> description formats（R-08）。
  - `formula_used_in_example`：digital representation -> 课后题 1（R-12）/ 课后题 3（R-13）。

---

## 9. 自检（硬性约束）

- YAML 可被 `yaml.safe_load` 解析；含冒号字符串均已引号化。
- 引用完整性：38 atoms / 29 source_elements / 9 chunks / 9 packages / 18 objects / 13 relations / 12 mentions，全部 atom.section_id ∈ section_tree、chunk/package/object/mention 引用 atom ∈ evidence_atoms、relation 端点 ∈ objects、每个 atom 至少进一个 chunk（`validate.py` 0 error）。
- ASCII 安全：g_m、C/g_m、0.8 um、50 kOhm、A_v=0.99、2^-i 等均纯文本写法。
- 一切据原文：Eq.(1)、7 步流程、三表内容、PRML/64 Mbit/s/0.8um、Master PLL 的 C/g_m 均逐字核对；MinerU 失真的 Fig.1.3-2/1.4-1 数字未采纳。
