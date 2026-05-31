# CMOS 教材 **第 2 章 CMOS Technology** 在 qiefen 方案下的完整结构化抽取（v0.3.3-textbook）

源文件：`pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md`
（MinerU 输出）
范围：**仅第 2 章（源文件第 743–2916 行）**
profile：`textbook`（书籍型）

本文逐 stage 说明第 2 章经 `qiefen.md` 流水线后的完整形态；
可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)；原始段落见 [`source.md`](./source.md)（逐字切片，viewer-only）。

> **v0.3.3-textbook（按 engram ch2 的修订过程/要求升级，保留 textbook 的对象/关系类型）**：
> - **双坐标**：每个 atom 带 `source_span`（**权威**，file = 该 mineru .md）+ `viewer_span`（`viewer_only:true`，file = source.md）。
>   不变量（脚本校验，全 86 atom 通过）：`source_file[source_span.char_*] == source.md[viewer_span.char_*] == raw_text`。
> - **raw_text + normalized_text**：`raw_text` 为逐字 span（段落 atom 取整段、子事实用 anchored 子串、公式取带 `\tag{N}` 的行、表取 `<table>` 行）；
>   `normalized_text` 只渲染该 span（可改写/转写 `phi/rho/eps/mu`、把整段浓缩为摘要；MinerU 把数字拆成 `2 0 0`/`0. 3 0 6`、用 U+2212 `−4 V`、希腊 `Κ`），**不得引入 span 外事实**；公式可经 `supported_by_context_atoms` 例外。
> - 顶层新增 **`source_elements`**（70 个，从 atom span 聚合）；
> - 对象用 **`local_evidence_atom_ids` + `supporting_context_atom_ids` + `home_package`**（`expected_objects` 只按 local 考核）；
>   全章唯一跨 package 对象是 `TECH-ETCHING`（local `A-FAB-ETCH` 在 C-FAB-STEPS，supporting 选择比/各向异性公式在 C-FAB-ETCH-F），并在 `PKG-FAB-STEPS.expected_local_fields` 标注；
> - **每个 chunk 配一个 package**（21 个，`atoms == chunk.atom_ids` + `expected_objects`）；
> - 标签改 **`gold_label` + `difficulty` + `evidence_strength`**（去掉小数 confidence）；
> - 新增 **`do_not_extract`**（`[n]` 引用、Fig./Eq. 交叉引用、`<details>` 图内 OCR 标签、image 标记、跨章引用）；
> - `source_meta` 增加 `conventions` / `validation` 块。
>
> 规模：86 atoms / 70 source_elements / 21 chunks / 21 packages / **64 objects / 29 relations**。
> 校验通过：双坐标 verbatim、引用完整性、`package.atoms==chunk.atom_ids`、`local⊂home-chunk & supporting⊄home-chunk`、
> `expected_local_fields⊆payload`、**全局无孤儿 atom**（每个 atom 都是某对象/关系的证据，或标 `context_only`）、
> 数字跨 span 审计无越界、无残留小数 confidence。下文 stage 讲解沿用 v0.1 内容（对象/切分思路不变）。

> **review 修订（在 v0.3.3-textbook 基础上）**：
> - **P0**：`PKG-FAB-PHOTO` 补 `TECH-PHOTOLITHOGRAPHY` + `CONCEPT-PRINTING-SYSTEMS`；补 `PROB-8`（+ `R-28 problem_uses_formula → FORMULA-BREAKDOWN`）；
>   `A-PN-BV-13` / `A-TCF-7` / `A-TCF-12` 的 `normalized_text` 去掉 span 外常量/结论（E_max=3e5 V/cm、ppm-C/X=R-or-C、"每 5℃ 翻倍"）改为纯公式（后两者结论已在 `A-EX251-RESULT`）。
> - **P1**：关系类型/端点纠偏 —— `R-03 formula_quantifies_effect`（→ PhysicalEffect）；`R-07`/`R-12 formula_uses_parameter_formula`（公式→公式）；
>   `R-21 → 新增 TRADEOFF-LATCHUP-AREA`（Tradeoff 类型）；`R-24 design_principle_applies_to_component → COMP-CAPACITORS`；
>   `R-25` 反向并指向 `新增 CHECK-CAP-SELECTION`（ChecklistCandidate）；`DERIV-PN-CAPACITANCE-CHAIN` 补 `A-PN-XN-7/A-PN-XP-8`（并补 `A-PN-XD-1`）。
> - **P2（已采纳）**：`PKG-NOISE`/`PKG-TEMP` 补对象（`PHYS-SHOT-NOISE`/`PHYS-THERMAL-NOISE`/`FORMULA-MOBILITY-TEMP`/`FORMULA-VT-TEMP`/`FORMULA-REVERSE-DIODE-TCF`）；
>   其余孤儿 atom 归位为对象（`FORMULA-PEAK-FIELD`/`PHYS-REVERSE-BREAKDOWN`/`FORMULA-REVERSE-CURRENT`/`PHYS-CAP-COEFFICIENTS`/`PRINCIPLE-ESD-PROTECTION`）或并入父对象证据（N-Well 子步骤并入 `PROCESS-NWELL-CMOS`、Table 2.3-1 并入 `DERIV-THRESHOLD-VOLTAGE`）；
>   章引/小结 `A-INTRO-TECH`/`A-SUMMARY` 标 `context_only:true`；package `section_path` 统一为 `2 > X` 形式。
> - **P2（暂缓）**：Table 2.6-1 拆成逐行 rule、为 phi_o/C_j/V_T/gamma/K' 单列 `Variable` 对象 —— 留待下一版。
>
> **P0.5（第二轮 review 微调）**：
> - `A-MOB-8` 等公式/条件 atom 的 `normalized_text` 严格收紧为**纯公式**——把语义标签/前缀（如 `(carrier mobility temperature dependence)`、`(Sah equation)`、`(component decomposition)`、`Shot noise:`、`(resistance of a conductive bar)`、`Latch-up regeneration requires...:` 等）一律移除，语义由对象层 `payload.name`/`role` 承载。为保持一致，对**全部** formula_atom / condition_atom（共 16 个）做了同样处理，不只 A-MOB-8。校验：所有公式/条件 atom 的 normalized 不再含 span 外 gloss。
> - `PRINCIPLE-LATCHUP-PREVENT.payload` 移除 `tradeoff` 字段（与 `TRADEOFF-LATCHUP-AREA` 对象 + `R-21` 去重），权衡只在对象层表达。
> - `PKG-OV` 加 `note` 说明其两个 atom（章引/小结）是 `context_only`、无 expected typed object。

---

## 0. Profile 判定（§2）

命中 `Chapter` / `Example 2.x-x` / `PROBLEMS` / `Eq. (n)` / `Fig. 2.x-x` → `textbook`。
抽取目标（§2.2）：`Concept` / `Definition` / `Formula` / `Variable` / `Derivation` /
`ExampleProblem` / `ExampleSolution` / `TechnologyProcess` / `ProcessFlow` /
`ComponentModel` / `PhysicalEffect` / `DesignPrinciple` / `DesignRule` / `ChecklistCandidate`。

---

## 1. SourceElement（§3.1）— MinerU 在第 2 章里的真实形态

- 标题：`# 2.2 The pn Junction`、`# Oxidation`、`# Example 2.2-1 ...`、`# PROBLEMS`
- 公式：`$$ ... \tag{N} $$`，**每个小节内 tag 独立从 1 重新编号**
  （2.1 有 tag1/2，2.2 有 tag1–24，2.3 又从 tag1 起 …）→ 解析时 tag 必须带 `section_path` 限定，否则跨节冲突。
- 表格：HTML `<table>`（Table 2.3-1 符号表、Table 2.4-1 无源器件、Table 2.6-1 layout 规则）
- 工艺剖面图 / 波形：`![](images/*.jpg)` + `<details><summary>text_image|flowchart|line</summary>…</details>`
  - 硅工艺族谱、N-Well 流程 (a)–(o) 用 `<details><summary>flowchart</summary>```mermaid```</details>` 渲染
- 课后题：`# PROBLEMS` 下编号列表 + 个别含公式/图

> 解析要点：N-Well CMOS 流程的 (a)–(o) 每一步都有独立剖面图，文字描述集中在一段，
> 需要把"文字步骤 + 对应 figure"绑成 `process_step` atom（§10 figure flowchart→graph edge）。

---

## 2. Section Tree（§3.2）— 第 2 章完整结构树

```
2 CMOS Technology
  2.1 Basic MOS Semiconductor Fabrication Processes
      Oxidation
      Diffusion
      Ion Implantation
      Deposition
      Etching
      Photolithography
      N-Well CMOS Fabrication Steps
      Silicide/Salicide Technology
  2.2 The pn Junction
      Example 2.2-1 Characteristics of a pn Junction
      Example 2.2-2 Calculation of the Saturation Current
  2.3 The MOS Transistor
      Example 2.3-1 Calculation of the Threshold Voltage
  2.4 Passive Components
      Capacitors
      Resistors
  2.5 Other Considerations of CMOS Technology
      (Substrate/Lateral BJT, Latch-up, ESD, Temperature, Noise)
      Example 2.5-1 Reverse Diode Current Temperature Dependence
  2.6 Integrated Circuit Layout
      Matching Concepts
      MOS Transistor Layout
      Resistor Layout
      Example 2.6-1 Resistance Calculation
      Capacitor Layout
      Layout Rules (Table 2.6-1)
  2.7 Summary
  PROBLEMS (1–25+)
```

---

## 3. EvidenceAtom（§3.3）— 按小节分布（完整见 `gold.yaml`）

下面给出每小节最关键的 atom 类型与代表内容。

### 2.1 工艺（technology_process / process_step / formula）
- 五大工艺步骤各一条 `technology_process_atom`：
  oxidation（SiO₂ 生长，56% 上/44% 下，t_ox 150Å–10000Å，700–1100℃）、
  diffusion（无限源/有限源；predeposition/drive-in；solid solubility 5e20–2e21）、
  ion implantation（精度 ±5%、室温、可穿薄层、可控 profile）、
  deposition（evaporation/sputtering/CVD/LPCVD）、
  etching（selectivity / anisotropy）。
- `formula_atom`：式(1) 选择比 `S_{A-B} = 期望层刻蚀率/非期望层刻蚀率`；
  式(2) 各向异性 `A = 1 − 横向/纵向刻蚀率`。
- photolithography：positive/negative photoresist；printing = contact/proximity/projection（step-and-repeat）。
- `process_step_atom` × N：N-Well CMOS 流程 (a)–(o)（n-well 注入→pad oxide→Si₃N₄→
  channel-stop→LOCOS→poly gate→LDD spacer→n⁺/p⁺ S/D→anneal→BPSG→Metal1→via→Metal2→passivation）。
- `physical_effect_atom`：LOCOS 的 **bird's beak**（氧化层侵入，缩小有源区）；STI 取代 LOCOS。
- `design_principle_atom`：模拟工艺应提供 **salicide block**（以便做高阻 poly/diffusion 电阻）。

### 2.2 pn 结公式链（formula chain / derivation / example）
- `formula_atom`：式(6) 势垒电势 φ_o、式(7)/(8) x_n/x_p、式(9) x_d、式(10) Q_j、
  式(11) E_o、式(12) C_j、式(13) BV、式(14) 倍增因子、式(24) 二极管方程 I_s。
- `definition_atom`：C_j0（v_D=0）、grading coefficient m（step=1/2, diffused=1/3, 范围 1/3–1/2）。
- `physical_effect_atom`：avalanche multiplication、Zener breakdown（<6V）。
- 例题：Example 2.2-1（φ_o=0.917V, x_p=1.128μm, C_j0=20.3fF, C_j(−4V)=9.18fF）；
  Example 2.2-2（I_s=1.346 fA）。

### 2.3 MOS 晶体管阈值电压链（derivation / formula / table / example）
- `concept_definition_atom`：strong inversion、threshold voltage V_T、device-transconductance K′。
- `formula_atom`：式(18) V_T 分解、式(19) V_T(含 body effect)、式(20) V_T0、式(21) body factor γ、
  式(27) Sah 漏电流方程、式(28) 有效条件、式(29) K′=μ_n C_ox。
- `table_row_atom`：Table 2.3-1 各参数在 N/P 沟道的符号。
- 例题：Example 2.3-1（φ_F(sub)=−0.377V, φ_MS=−0.940V, C_ox=1.727e-7 F/cm², V_T0=0.306V, γ=0.577 V^½）。

### 2.4 无源器件（component_model / design_principle / table）
- `design_principle_atom`：电容期望特性（好匹配 / 低压系数 / 高 C_desired:C_parasitic / 高单位面积电容）。
- `component_model_atom`：三类电容（poly-oxide-channel / double-poly / accumulation MOS）；
  三类电阻（diffused 50–150Ω/□、poly 30–200Ω/□、n-well 1–10kΩ/□）。
- `table_row_atom`：Table 2.4-1（0.8μm 工艺无源器件：值域 / 匹配 / 温度系数 / 电压系数）。

### 2.5 其它考量（physical_effect / formula / design_principle / example）
- `component_model_atom`：substrate BJT（collector 受限于 V_SS）、lateral BJT（寄生）。
- `physical_effect_atom`：latch-up（PNPN SCR）；触发三条件，式(6) `β_NPN·β_PNP ≥ 1`。
- `design_principle_atom`：latch-up 防护（guard ring 降低 R_N/R_P、加大 n 沟到 n-well 间距）；ESD 保护(R + 双二极管)。
- `formula_atom`：式(7)/(12) TC_F、式(8) μ=K_μT^−1.5、式(9) V_T(T) 温漂(α≈2.3mV/℃)、
  噪声：式(18) shot、式(19) thermal、式(20) flicker(1/f)。
- 例题：Example 2.5-1（TC_F=0.165，反向电流约每 5℃ 翻倍/实测 8℃）。

### 2.6 版图（design_principle / formula / example / design_rule）
- `design_principle_atom`：unit-matching principle、common-centroid（消线性梯度误差）。
- `formula_atom`：式(12) `R=ρL/WT`、式(13) sheet resistance `R=ρ_s·(L/W)`、式(14) `C=ε_ox·A/t_ox`。
- 例题：Example 2.6-1（ρ_s=30Ω/□, N=L/W=25, R=750Ω）。
- `design_rule_atom`：Table 2.6-1 全部 layout 规则（以 λ 为单位：N-Well 宽 6λ、AA 宽 4λ、
  poly gate 宽 2λ、contact 2×2λ、Metal-1 间距 3λ、via 3×3λ、bonding pad 100μm×100μm …）。

### PROBLEMS（problem_statement）
- `problem_statement_atom`：题1（列五大工艺步骤及功能）、题4（−2V 重做 Example 2.2-1）、
  题8（N_A=N_D=1e16 的击穿电压）、题11（V_SB=2V 求 V_T）等。

---

## 4. SemanticChunk（§4.2 书籍型 chunk）— 第 2 章主要块

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-OV` | `chapter_overview_block` | 2 | 章目标/范围（2.0 + 2.7 小结） |
| `C-FAB-STEPS` | `technology_process_block` | 2 > 2.1 | 五大工艺步骤（每步一个知识单元） |
| `C-FAB-ETCH` | `formula_definition_block` | 2 > 2.1 > Etching | selectivity/anisotropy 公式 |
| `C-FAB-NWELL` | `process_flow_block` | 2 > 2.1 > N-Well | (a)–(o) 连续流程，保持步骤连续 |
| `C-PN-DERIV` | `derivation_block` | 2 > 2.2 | φ_o→x_d→Q_j→C_j 公式链 |
| `C-PN-DIODE` | `formula_definition_block` | 2 > 2.2 | 二极管方程 + 击穿 |
| `C-EX-221` | `example_solution_block` | 2 > 2.2 > Example 2.2-1 | 题/given/公式引用/结果 |
| `C-EX-222` | `example_solution_block` | 2 > 2.2 > Example 2.2-2 | I_s 计算 |
| `C-MOS-VT` | `derivation_block` | 2 > 2.3 | V_T 推导链 + body factor |
| `C-MOS-ID` | `formula_definition_block` | 2 > 2.3 | Sah 漏电流方程 + K′ |
| `C-EX-231` | `example_solution_block` | 2 > 2.3 > Example 2.3-1 | V_T0 / γ 计算 |
| `C-CAP` | `component_model_block` | 2 > 2.4 > Capacitors | 三类电容 + 期望特性 |
| `C-RES` | `component_model_block` | 2 > 2.4 > Resistors | 三类电阻 + Table 2.4-1 |
| `C-LATCHUP` | `physical_effect_block` | 2 > 2.5 | latch-up SCR + 防护 |
| `C-TEMP` | `formula_definition_block` | 2 > 2.5 | TC_F / 温漂公式 |
| `C-NOISE` | `formula_definition_block` | 2 > 2.5 | shot/thermal/1-f 噪声 |
| `C-MATCH` | `design_principle_block` | 2 > 2.6 | 匹配原则 + sheet resistance |
| `C-EX-261` | `example_solution_block` | 2 > 2.6 > Example 2.6-1 | 电阻计算 |
| `C-RULES` | `layout_rule_block` | 2 > 2.6 > Layout Rules | Table 2.6-1 设计规则 |
| `C-PROB` | `problem_set_block` | 2 > PROBLEMS | 课后题 |

> anchor expansion（§5.2）的两个典型仍在：`C-PN-DERIV` 把 `Q_j(式10)→C_j(式12)→
> C_j0/m/适用条件` 收进同块；`C-EX-221` 把 `problem/given/公式引用/结果` 收进同块。

---

## 5. ContextPackage（§6 书籍型示例）

`C-MOS-VT` 的 package：

```
Document: CMOS Analog Circuit Design
Section: Chapter 2 > 2.3 The MOS Transistor > Threshold Voltage
Atoms:
[A-MOS-PHIF-10]  phi_F definitions (Eq.10/11)
[A-MOS-QB-16]    bulk charge Q_b (Eq.16)
[A-MOS-VT-19]    V_T with body effect (Eq.19)
[A-MOS-VT0-20]   V_T0 (Eq.20)
[A-MOS-GAMMA-21] body factor gamma (Eq.21)
linked_context:
  table: "Table 2.3-1 signs of threshold-voltage quantities for N/P channel"
  previous_heading: "2.3 The MOS Transistor"
  next_heading: "Example 2.3-1 Calculation of the Threshold Voltage"
Targets: Concept, Formula, Variable, Derivation, PhysicalEffect
```

---

## 6. Mention → 7. Object → 8. Relation（§7–§8）

- **Mention**：oxidation / diffusion / ion implantation / photolithography / LOCOS /
  bird's beak / LDD / salicide / pn junction / depletion region / barrier potential /
  depletion capacitance / grading coefficient / MOS transistor / threshold voltage /
  strong inversion / body effect / latch-up / sheet resistance / shot noise / 1-f noise。
- **Object**（覆盖该章全部对象类型，详见 `gold.yaml::objects`）：
  - `Concept`：pn junction / depletion region / MOS transistor / threshold voltage /
    strong inversion / sheet resistance / latch-up
  - `TechnologyProcess`：oxidation / diffusion / ion implantation / deposition / etching
  - `ProcessFlow`：N-Well CMOS fabrication（含有序步骤）
  - `Formula`：barrier potential / depletion width / depletion charge / depletion capacitance /
    threshold voltage / body factor / Sah drain current / K′ / diode equation /
    selectivity / anisotropy / sheet resistance / capacitance / TC_F / shot / thermal / 1-f noise
  - `Derivation`：pn 结公式链、阈值电压推导链
  - `ExampleSolution`：2.2-1 / 2.2-2 / 2.3-1 / 2.5-1 / 2.6-1
  - `ComponentModel`：poly-poly cap / MOS cap / accumulation cap / diffused/poly/n-well resistor /
    substrate BJT / lateral BJT
  - `PhysicalEffect`：depletion capacitance / body effect / bird's beak / avalanche / Zener /
    latch-up / shot/thermal/flicker noise
  - `DesignPrinciple`：capacitor desired characteristics / unit-matching / common-centroid /
    latch-up prevention / salicide block for analog
  - `DesignRule`：layout rules（Table 2.6-1）
  - `ProblemStatement`：课后题
- **Relation**（全 ID 化、带证据，详见 `gold.yaml::relations`）：
  - `formula_derived_from_formula`：C_j ← Q_j；x_d ← x_n,x_p
  - `formula_defines_variable`：C_j → {C_j0, m}；V_T → γ
  - `formula_used_in_example`：C_j → Example 2.2-1；V_T → Example 2.3-1；sheet-R → Example 2.6-1
  - `process_flow_has_step` / `process_step_precedes_step`：N-Well 流程 → 各步骤、步骤间先后
  - `process_step_creates_structure`：LOCOS → field oxide；`process_step_mitigates_issue`：channel-stop → 寄生管
  - `process_step_has_nonideality`：LOCOS → bird's beak
  - `concept_contrasts_with_concept`：analog vs digital（章引）/ substrate BJT vs lateral BJT
  - `design_principle_applies_to_scenario`：common-centroid → 线性梯度误差
  - `design_principle_has_tradeoff`：latch-up 防护(加大间距) → 面积代价
  - `component_has_property`：MOS transistor → threshold voltage；pn junction → depletion capacitance
  - `checklist_candidate_derived_from_principle`：capacitor desired characteristics → 选型 checklist
