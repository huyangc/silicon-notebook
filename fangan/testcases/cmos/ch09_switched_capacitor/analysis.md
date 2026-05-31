# CMOS 教材 **第 9 章 Switched Capacitor Circuits** 在 qiefen 方案下的结构化抽取（schema v0.3.3-textbook）

源文件：`pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md`
范围：**仅第 9 章（源文件第 7496–13705 行）**，约 6200 行，是本书最长章节之一。
profile：`textbook`（书籍型）

本文逐 stage 说明第 9 章经 `qiefen.md` 流水线后的形态；可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)。
深度策略：章节极大，按“每小节核心知识单元代表性覆盖”而非逐段原子化，覆盖 9.1–9.8 全部 8 个小节。

> **schema v0.3.3-textbook（本版）**：gold.yaml 由 [`build.py`](./build.py) 对原始 MinerU 文件逐字定位 span 生成、由 [`validate.py`](./validate.py) 校验，断言全绿。
> `source.md` 是 7496–13705 行的 viewer-only 逐字切片（含非权威头部声明）。

---

## v0.1 → v0.3.3-textbook 变更记录

沿用 engram-ch2 / cmos-ch2 的 v0.3.3 流程（保留 textbook 类型词表）。**知识内容（atom/chunk/object/relation 的 ID 与含义）整体沿用 v0.1 gold**，仅按 v0.3.3 重新表达并补双坐标：

- **双坐标**：每个 atom 带 `source_span`（权威，file=源 MinerU .md）+ `viewer_span`（`viewer_only:true`，file=source.md）。两个文件切片都 == `raw_text`（73×2=146 项断言全过）。`raw_text` 改为**逐字 span**（之前 v0.1 是改写摘要、无坐标）；`normalized_text` 只渲染该 span 的 ascii 形（`z^-1`/`omega`/`alpha`/`tau`/`+-`），不引入 span 外事实/章节号/图号。
- **公式/条件 atom 纯净**：formula 的 `normalized_text` 只保留**公式本体**（如 `R = T/C`、`H^oo(z) = (C_1/C_2) z^-1/(1 - z^-1)`），所有文字注释（含 Eq./Fig. 交叉引用、跨 tag 说明）下沉到 `metadata.note`。
- **对象证据 local/supporting**：object 拆 `local_evidence_atom_ids`（在 `home_package` 的 chunk 内）+ `supporting_context_atom_ids`（来自其它 chunk）+ `home_package`；package object-recall 只按 local 考核。跨 chunk 的支撑（如 BLOCK-SC-INTEGRATOR 引 transresistance/nonideal、PHYS-CLOCK-FEEDTHRU 引 summary）走 supporting。
- **标签**：object/relation 用 `gold_label/difficulty/evidence_strength` 取代小数 `confidence`。
- **package**：每 chunk 一个 package，`atoms == chunk.atom_ids`，列 `expected_objects`；跨 package 对象给 `expected_local_fields`（PKG-OV→PRINCIPLE-OVERSAMPLING、PKG-INT-NONIDEAL→PHYS-FINITE-GAIN）。
- **context_only**：章引 `A-INTRO`、小结 `A-SUMMARY`、以及 9.2 连续时间/charge-amp 前提块（`A-AMP-CT-12`/`A-AMP-AVD-3`/`A-AMP-FINITE-6`/`A-CHARGE-AMP-12`/`A-CHARGE-AMP-USE`）标 `context_only:true`，不产出 typed object；其 package（PKG-OV、PKG-AMP-CT）带 `note` 说明。
- **新增 4 个支撑 atom**（保持公式/对象证据纯净、消除孤儿）：`A-CHARGE-AMP-USE`（charge amp 在 SC 中可用的理由）、`A-SCAMP-NOEXCESS`（9.2-7b 无多余相位延迟，支撑 parasitic-insensitive 原则）、`A-FO-DIFF-COST`（差分代价：器件数↑、摆幅×2、偶次谐波↓，支撑 feedthrough 缓解 tradeoff）、`A-SO-BIQUAD-LOWQ`（Q>5 时 alpha_5 太小，支撑 biquad）。原 69 → **73 atoms**。
- **do_not_extract**：`[n]` 引用 + 政策、`Fig./Eq./Table` 交叉引用、`<details>` SC 原理图 OCR 噪声标注、**9.7 PSpice netlist 巨型 `<table>`（排除）**、image markup、跨章引用（Chap. 8 autozero、Sec. 5.1、next chapter）。
- **全局无孤儿**：每个 atom 要么进某 object/relation 证据，要么 `context_only:true`。

计数：sections **18** / atoms **73** / source_elements **91** / chunks **22** / packages **22** / objects **48** / relations **41** / mentions **15** / do_not_extract **8**。`python3 validate.py` → ALL CHECKS PASS。

---

## 0. Profile 判定（§2）

命中 `Chapter 9` / `Example 9.1-1`…`9.6-1` / `Eq. (12)` / `Fig. 9.1-1` / `Table 9.1-1` / `9.8 - Summary` / `Homework Problems` → `textbook`。
抽取目标（§2.2）：`Concept` / `Formula` / `Variable` / `CircuitBlock` / `ComponentModel` /
`PhysicalEffect` / `DesignPrinciple` / `Derivation` / `ExampleProblem` / `ExampleSolution` / `ProblemStatement`。
与第 2 章相比，本章重心从“工艺/器件”转向“电路构件层级 + 设计原则”，因此 `CircuitBlock` 与 `circuit_block_composed_of_block` 成为主线（§8.2 点名的 op-amp hierarchy 那类边）。

---

## 1. SourceElement（§3.1）— MinerU 在第 9 章里的真实形态与坑

- 标题：`# 9.1 Switched Capacitor Circuits`、`# Resistor Emulation`、`# Example 9.1-1`、`# 9.8 - Summary`、`# Homework Problems`。
- 公式：`$$ ... \tag{N} $$`，**每个小节内 tag 从 1 重新编号**（9.1 到 tag48，9.2 又从 1 起，9.3/9.5/9.6/9.7 同理）→ atom 的 tag 必须带 `section_path` 限定，否则跨节冲突（见 `parsing_notes[0]`）。
- 电路图：大量 `![](images/*.jpg)` + `<details><summary>chemical|text_image</summary>…</details>`。**这些 summary 块里的 OCR 文本噪声极大**：节点标注（phi_1/phi_2、C_1/C_2、vC1/vC2）顺序错乱、上标 e/o 丢失。**图本身的拓扑基本不可靠，知识单元应取自正文与编号公式**（见 `parsing_notes[1]`）。
- 表格：`Table 9.1-1`（四种 SC 电阻等效值）、`Table 9.3-1`（GB 对积分器影响）以 HTML `<table>` 出现且含 `$...$` 内联公式；**9.7 的一段是巨大的 PSpice netlist `<table>`，纯噪声，不抽取**。
- z-domain 上标：`H^{oo}` / `H^{ee}` / `H^{oe}`（even/odd phase）在 MinerU 中常被渲染为普通字符或丢上标，需结合上下文判定（见 `parsing_notes[3]`）。
- 9.6/9.7 的 cascade/ladder 结构图以 `<details><summary>flowchart</summary>```mermaid```</details>` 渲染，适合直接转 `circuit_block_composed_of_block` 边。

---

## 2. Section Tree（§3.2）

```
9 Switched Capacitor Circuits
  9.1 Switched Capacitor Circuits (Resistor Emulation)
      Example 9.1-1 / 9.1-2 / 9.1-3
  9.2 Switched Capacitor Amplifiers
      Example 9.2-1 / 9.2-4
  9.3 Switched Capacitor Integrators
      Example 9.3-3
  9.4 z-domain Models of Two-Phase SC Circuits
  9.5 First-Order Switched Capacitor Circuits
      Example 9.5-1
  9.6 Second-Order Switched Capacitor Circuits
      Example 9.6-1
  9.7 Switched Capacitor Filters
  9.8 Summary
  Homework Problems
```

---

## 3. EvidenceAtom（§3.3）— 按小节分布（完整见 `gold.yaml`）

- **9.0 章引**：`A-INTRO`（SC 是 analog sampled data，优/缺点列表）、`A-INTRO-ACCURACY`（精度正比于电容比值——本章的核心立论）。
- **9.1 电阻仿真**：核心是公式链 `i_1(avg)=C[v_C(T/2)-v_C(0)]/T (式6)` → `=C(V_1-V_2)/T (式10)` → **`R = T/C = 1/(f_c C)` (式12)**；`A-SC-TABLE-911`（Table 9.1-1 四种等效电阻 T/C、T/C、T/(C1+C2)、T/4C）；精度对比 `A-SC-ACC-TAUC-20`（连续时间 5–20%）vs `A-SC-ACC-TAUD-22`（SC 0.1%）；z 变换定义 `A-SC-ZTRANS-25` 与 SC 低通 `H^oo(z)` 式37。例题 9.1-1/9.1-2/9.1-3。
- **9.2 放大器**：连续时间增益（式1/2）、有限增益模型（式3/5/6）、charge amplifier（式10/11/12）；**SC 放大器增益 `H(z)=-(C1/C2) z^-1` 式19/23**；parasitic-insensitive transresistance `R_T=±T/C` 式28；**clock feedthrough 式50（三项：理想/输入相关/常数）**；有限增益 `A_vd(0)` 仅影响幅度 式52。例题 9.2-1（finite gain）、9.2-4（feedthrough 数值）。
- **9.3 积分器**：`A-INT-ROLE`（一切滤波器可归约为积分器）；连续时间式1/2；**noninverting `H^oo(z)=(C1/C2)z^-1/(1-z^-1)` 式16 / inverting `H^ee(z)=-(C1/C2)/(1-z^-1)` 式24**；积分器频率 `w_I=(C1/C2)f_c` 式19；相位误差抵消原则；非理想（式36/37 m/theta，kT/C 噪声式41）。例题 9.3-3。
- **9.4 z-domain 模型**：分解时变→时不变；三种 admittance（`Y`、`Y z^-1/2`、`(1-z^-1)Y`，且 Y=C）式1/2/3；四种两端口 + 两种不可约模型目录。
- **9.5 一阶电路**：两种滤波器设计途径；一阶通式 H(s)/H(z) 式1/2；低通 式4/5（`alpha_2 C` 阻尼把积分器变低通）、高通 式13；differential 实现降 feedthrough。例题 9.5-1。
- **9.6 二阶电路**：cascade 分解；**biquad = noninverting + inverting 积分器（一个 damped）+ 输入零点**；连续时间 biquad 式1；低 Q biquad 电容比 式11（Q>5 不适用）。例题 9.6-1。
- **9.7 滤波器**：三大指标（passband ripple / transition / stopband）；**cascade**（first/second-order 块级联）与 **ladder**（RLC 原型→state equations→积分器综合，电容比敏感性更低）两条路线。
- **9.8 小结** `A-SUMMARY`；**Homework Problems** 取 9.1-1 / 9.1-3 / 9.5-1。

---

## 4. SemanticChunk（§4.2）— 第 9 章主要块

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-OV` | `chapter_overview_block` | 9 | 章引 + 电容比精度原则 + 小结 |
| `C-RES-EMU` | `design_principle_block` | 9 > 9.1 | i_avg→R=T/C 推导 + Table 9.1-1（公式-说明连续） |
| `C-ACCURACY` | `design_principle_block` | 9 > 9.1 | tau_C vs tau_D 精度对比（SC 核心卖点） |
| `C-Z-ANALYSIS` | `formula_definition_block` | 9 > 9.1 | z 变换 + SC 低通 H(z) |
| `C-EX-911/912/913` | `example_solution_block` | 9 > 9.1 > Ex | 三个例题 |
| `C-AMP-CT` | `formula_definition_block` | 9 > 9.2 | 连续时间/有限增益/charge amp（SC 放大器前提） |
| `C-SCAMP` | `circuit_hierarchy_block` | 9 > 9.2 | SC 放大器组成 + 增益 -C1/C2 + parasitic-insensitive |
| `C-FEEDTHRU` | `physical_effect_block` | 9 > 9.2 | clock feedthrough（三项）+ 缓解 + 有限增益误差 |
| `C-EX-921/924` | `example_solution_block` | 9 > 9.2 > Ex | finite gain / feedthrough 例题 |
| `C-INT` | `formula_definition_block` | 9 > 9.3 | 积分器 noninv/inv H(z) + w_I + 相位抵消 |
| `C-INT-NONIDEAL` | `physical_effect_block` | 9 > 9.3 | 非理想 + kT/C 噪声 |
| `C-EX-933` | `example_solution_block` | 9 > 9.3 > Ex | 积分器误差例题 |
| `C-ZMODEL` | `component_model_block` | 9 > 9.4 | z-domain 模型法 + 三 admittance + 两端口目录 |
| `C-FIRSTORDER` | `design_process_block` | 9 > 9.5 | 一阶构件（低/高通/差分） |
| `C-EX-951` | `example_solution_block` | 9 > 9.5 > Ex | 一阶设计例题 |
| `C-SECONDORDER` | `circuit_hierarchy_block` | 9 > 9.6 | biquad = 两积分器 + cascade |
| `C-EX-961` | `example_solution_block` | 9 > 9.6 > Ex | 低 Q biquad 例题 |
| `C-FILTERS` | `circuit_hierarchy_block` | 9 > 9.7 | cascade / ladder 两路线 |
| `C-PROB` | `problem_set_block` | 9 > Homework Problems | 课后题 |

> boundary 关键（§5.3）：`C-RES-EMU` 保持“平均电流推导链 → R=T/C → Table”在同块；
> 各 `example_solution_block` 保持“题干/公式引用/结果”连续；`C-INT` 把 noninverting 与 inverting
> 两个 H(z) 收在一块（它们仅左侧两开关相位不同、幅度相同），符合公式-说明连续性。

---

## 5. ContextPackage（§6 书籍型示例）

`C-RES-EMU` 的 package（`PKG-RES-EMU`）：

```
Document: CMOS Analog Circuit Design
Section: Chapter 9 > 9.1 > Resistor Emulation
Atoms:
[A-SC-RES-CONCEPT] parallel SC resistor concept (switches + C + nonoverlapping clock)
[A-SC-IAVG-10]     i_1(average) = C(V_1 - V_2)/T
[A-SC-REQ-12]      R = T/C = 1/(f_c C)
[A-SC-REQ-SP-17]   R = T/(C_1 + C_2)
[A-SC-TABLE-911]   Table 9.1-1 four emulation circuits
linked_context:
  formula_context: "R=T/C by equating i_avg=C(V1-V2)/T with i_avg=(V1-V2)/R"
  table: "Table 9.1-1 (Parallel/Series T/C, Series-Parallel T/(C1+C2), Bilinear T/4C)"
  next_heading: "Example 9.1-1"
Targets: Concept, Formula, DesignPrinciple, CircuitBlock
```

第二个 package `PKG-INT`（9.3 积分器）给出 noninverting/inverting H(z) 与 w_I 的组合上下文。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8）

- **Mention**：switched capacitor circuit / capacitor ratio accuracy / nonoverlapping clock / R=T/C /
  parallel SC resistor / SC amp gain -C1/C2 / transresistance T/C / clock feedthrough /
  SC integrator / integrator frequency w_I / kT/C noise / biquad / SC filter / ladder filter / first-order lowpass。
  canonicalization 处理 `sc_integrator`（noninv/inv 同一构件两变体）、`clock_feedthrough`（含 charge injection 别名）等。

- **Object**（覆盖该章对象类型，详见 `gold.yaml::objects`）：
  - `Concept`：switched capacitor circuit / nonoverlapping clock / z-domain model / biquad
  - `Formula`：R=T/C、R=T/(C1+C2)、tau_D 精度、SC 低通 H(z)、SC 放大器增益 -C1/C2、transresistance ±T/C、
    feedthrough 式50、noninv/inv 积分器 H(z)、w_I、有限增益 m/theta、kT/C、z-admittance、一阶低通、biquad H(s)、biquad 电容比
  - `CircuitBlock`：SC resistor / transresistance / SC amplifier / SC integrator / first-order block / biquad / SC filter
  - `ComponentModel`：z-domain 两端口模型集
  - `PhysicalEffect`：clock feedthrough / finite op-amp gain error / kT/C noise
  - `DesignPrinciple`：电容比精度（核心）/ 积分器是滤波器基础 / parasitic-insensitive / feedthrough 缓解 / oversampling / 滤波器设计路线
  - `ExampleSolution`：9.1-1/9.1-2/9.1-3/9.2-1/9.2-4/9.3-3/9.5-1/9.6-1
  - `ProblemStatement`：9.1-1 / 9.1-3 / 9.5-1

- **Relation**（全 ID 化、带证据，详见 `gold.yaml::relations`，§8.2 类型）：
  - `formula_defines_variable`：R=T/C → SC resistor；SC 放大器增益 → SC amplifier；积分器 H(z)/w_I → SC integrator；feedthrough 式50 → clock feedthrough effect。
  - `formula_used_in_example`：R=T/C → Ex 9.1-1；R=T/(C1+C2) → Ex 9.1-2；SC 低通 → Ex 9.1-3；feedthrough → Ex 9.2-4；积分器误差 → Ex 9.3-3；一阶低通 → Ex 9.5-1；biquad 电容比 → Ex 9.6-1。
  - `formula_derived_from_formula`：R=T/C → R=T/(C1+C2)。
  - **`circuit_block_composed_of_block`（本章主线，对标 §8.2 op-amp hierarchy）**：
    SC filter ← biquad ← SC integrator ← transresistance ← SC resistor；
    SC filter ← first-order block ← SC integrator；SC amplifier ← transresistance。
  - `component_has_property`：SC amplifier → clock feedthrough；SC integrator → finite-gain error / kT/C noise。
  - `design_principle_applies_to_scenario`：电容比精度 → SC 电路 / tau_D 精度；parasitic-insensitive → SC amplifier；feedthrough 缓解 → clock feedthrough；oversampling → R=T/C；滤波器路线 → SC filter。
  - **`design_principle_has_tradeoff`**：电容比精度（SC 优势）↔ clock feedthrough（必然代价）；feedthrough 缓解（差分）↔ 器件数/复杂度；ladder 路线（低敏感）↔ 设计更复杂/仅限 RLC。
  - `concept_defines_term`：z-domain model → 两端口模型集。
  - `problem_extends_example`：9.1-1 → Ex 9.1-2；9.5-1 → Ex 9.5-1。

---

## 校验（v0.3.3-textbook）

`python3 build.py` 重生成 `source.md` + `gold.yaml`；`python3 validate.py` → **ALL CHECKS PASS**。
计数 sections 18 / atoms 73 / source_elements 91 / chunks 22 / packages 22 / objects 48 / relations 41 / mentions 15 / do_not_extract 8。

校验项（全过）：YAML 可解析；**双坐标逐字**（73 个 atom 的 source_span 在原始 MinerU .md、viewer_span 在 source.md 都 == raw_text，共 146 项）；
`atom.section_id ∈ section_tree`、`source_element_id ∈ source_elements`；每个 atom 至少进一个 chunk，`central/gold_must_cover ⊆ atom_ids`；
`package.atoms == chunk.atom_ids`、`expected_objects ⊆ objects`、`expected_local_fields` 字段 ⊆ payload；
对象证据 local ∈ home-chunk、supporting ∉ home-chunk，且对象出现在 home_package.expected_objects；
relation 端点 ∈ objects、证据 ∈ evidence_atoms；object/relation 无 `confidence`，均带 gold_label/difficulty/evidence_strength；
**数值越界审计**（非 formula atom 的 normalized 数字必须出现在其 raw span，已处理 MinerU 数字加空格如 `9 . 9 0 1`）；
**formula/condition normalized 纯公式**（无 gloss，注释在 metadata.note）；**全局无孤儿**（每 atom 进 object/relation 证据或 context_only）。

### 源文件坑（本章）
- **每小节 tag 从 1 重编号**：R=T/C 是 9.1 的 Eq.12（行 7628），SC 放大器增益是 9.2 的 Eq.19（行 8384），积分器 H(z) 是 9.3 的 Eq.16；formula atom 以 sub-section line window 定位 tag。
- **MinerU 数字加空格**：`1 0 ^ {- 5}`、`0. 6 2 8 3`、`9 . 9 0 1`；formula raw_text 逐字（看似乱码），normalized 给 ascii 可读形。
- **SC 原理图 OCR 噪声**：`<details>` chemical/text_image 块的节点标注（phi_1/phi_2、C_1/C_2、v_C、even/odd 上标 e/o）顺序错乱、上标丢失，拓扑不可靠 → 知识只取自正文 + 编号公式，图 OCR 全部进 do_not_extract。
- **9.7 PSpice netlist `<table>`**（约行 12294 起）：整段 SPICE 卡（NC11/PC1/DELAY/.SUBCKT/.AC…）的巨型 HTML 表格，纯噪声，排除。
- viewer 切片 6210 行（整章），但只对 v0.1 的 73 个 atom 打 span，按各 atom 的 section_id 锁定 sub-section 行窗，不线性通读全章。
