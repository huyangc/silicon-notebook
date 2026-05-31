# CMOS 教材 **第 4 章 Analog CMOS Subcircuits** 在 qiefen 方案下的完整结构化抽取

**schema：v0.3.3-textbook**（双坐标 + 逐字 span；由 `build.py` 对原始 MinerU `.md` 定位生成，`validate.py` 全绿）

源文件：`pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md`（MinerU 输出）
范围：**仅第 4 章（源文件 4873–7479 行）**
profile：`textbook`（书籍型）

本章是全套教材中的 **电路层级（circuit-hierarchy）章**：几乎每个电路都是上一层电路的
building block。因此本夹具的核心演示关系是 `circuit_block_composed_of_block`（共 16 条边）。
可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)；构建/校验脚本见 [`build.py`](./build.py) / [`validate.py`](./validate.py)。

---

## v0.1 → v0.3.3-textbook 变更说明（本轮）

保留 v0.1 的全部知识内容与 ID（原子/chunk/对象/关系），仅按 engram-ch v0.3.3 流程在 textbook 类型下重表达，并补齐严格评测所需结构：

- **双坐标 + 逐字 span**：每个 atom 增加 `source_span`（权威，指向 MinerU `.md`）+ `viewer_span`（`viewer_only`，指向 `source.md`）。`raw_text` 为逐字连续 span，`normalized_text` 仅渲染该 span（ascii 化数学符号、去 MinerU 数字空格如 `1 9. 7`→`19.7`）。FORMULA/CONDITION 的 normalized 只给公式体、不夹带说明。脚本断言 `source_file[span]==raw_text` 且 `source.md[span]==raw_text`。
- **`source.md` 重新生成**：4873–7479 行逐字切片 + viewer-only 头部声明（非权威）。
- **公式定位按小节窗口**：tag 每小节从 1 重编号（4.1 tag1–8、4.2 tag1–6、4.3 重启至 17、4.4/4.5/4.6 各自重启），`build.py` 用 `W41/W42/W43/W44/W45/W46` 行窗口锚定，避免跨节 tag 冲突。
- **Fig 4.0-1 op-amp 层级图处理**：该图是 `<details><summary>flowchart</summary> ```mermaid graph TD ... ``` </details>` 块。层级 atom `A-HIER-OPAMP` 锚定**散文段**（4877 行）陈述构成，而 mermaid 节点原文（`Operational Amplifier` / `Biasing Circuits` / `Source Coupled Pair` …）与 `graph TD`/`-->` 语法进 `do_not_extract`（`figure_label` / `mermaid_flowchart_node_label`）。构成关系仍以 `circuit_block_composed_of_block`（R-01..R-05）落地。
- **对象证据 local/supporting + home_package**：小数 `confidence` → `gold_label/difficulty/evidence_strength`；跨 chunk 证据拆 local（home chunk 内）/ supporting（其它 chunk，如各叶子电路引 `A-INTRO-SUB`/`A-HIER-OPAMP` 的章引/层级语句）。
- **每 chunk 一个 context_package**：`atoms == chunk.atom_ids`；`expected_objects` 列 home 于该包的对象；`PKG-HIER` 对从层级包可达的叶子 CircuitBlock 声明 `expected_local_fields=['name']`（跨包字段）。
- **章引/小结 `context_only:true`**：`A-INTRO-SUB`、`A-SUMMARY`。
- **`do_not_extract` 负例**：bracket `[n]` 引用 + policy、Fig./Eq./Table 交叉引用、mermaid/图内 OCR 节点标签、image markdown、出章前向引用（Chapter 5/6/7）。
- **原子数微调**：例 4.1-1 的 Case 1（19.7 mV）与 Case 2（10.95 mV）各为独立 `$$` 结果块，拆成 `A-EX411-RESULT` / `A-EX411-RESULT2`（v0.1 的 74 → 75 atoms）。其余计数：source_elements=75、chunks=16、packages=16、objects=47、relations=50、mentions=16、do_not_extract=11。
- **校验**：`validate.py` 跑双 span 逐字、结构引用、chunk 覆盖、package==chunk、expected_objects⊆objects、对象 local⊆home-chunk / supporting⊄home-chunk、关系端点（含全部 `circuit_block_composed_of_block`）∈objects、无 `confidence`、formula 数字 over-span 审计、formula/condition normalized 无 gloss、全局无孤儿（含 context_only）→ **ALL CHECKS PASS**。

---

## 0. Profile 判定（§2）

命中 `Chapter 4` / `Example 4.x-x` / `Problems` / `Eq. (n)` / `Fig. 4.x-x` → `textbook`。
本章抽取目标偏 **电路** 而非工艺：`CircuitBlock` / `ComponentModel` / `Formula` / `Variable` /
`ExampleSolution` / `PhysicalEffect` / `DesignPrinciple` / `ProblemStatement`。

---

## 1. SourceElement（§3.1）— MinerU 在第 4 章里的真实形态

- 标题：`# 4.1 MOS Switch`、`# Example 4.3-1 ...`、`# Problems`。
- **关键坑：Fig 4.0-1 是一张 `<details><summary>flowchart</summary> mermaid graph TD</details>` 的 op-amp 层级图。**
  这正是把图直接转为 `circuit_block_composed_of_block` 边的最佳来源（§10 figure flowchart → graph edge）：
  `op amp → biasing / diff-amp / 2nd-stage / output`；`diff-amp → current sink + current-mirror load + source-coupled pair`。
- 公式：`$$ ... \tag{N} $$`，**每个小节内 tag 独立从 1 重新编号**（4.1 tag1–8、4.2 tag1–6、4.3 又从 1 起 …）
  → atom 的 tag 必须以 `section_path` 限定，否则跨节冲突。
- 大量电路原理图渲染成 `<details><summary>text_image</summary> 节点标签</details>`（M1/M2/iOUT/VGG…），
  以及若干 `<details><summary>line</summary>| 表 |</details>` 形式的特性曲线（r_ON vs V_GS、ratio error vs ΔV_T、bandgap V_REF vs T）。
- 数值精度高：例题给出真实结果（19.7 mV / 10.95 mV、250 kΩ / 9.25 MΩ、W/L=7.27/1.82、4±0.05、−928 ppm/℃、K=21.62、R2/R1=9.39、1.262→1.153 V），夹具逐一引用。

---

## 2. Section Tree（§3.2）

```
4 Analog CMOS Subcircuits
  4.1 MOS Switch
      Example 4.1-1 Calculation of Charge Feedthrough Error
  4.2 MOS Diode/Active Resistor
      Example 4.2-1 Resistance of an Active Resistor
  4.3 Current Sinks and Sources
      Example 4.3-1 Output Resistance for a Current Sink
      Example 4.3-2 Cascode Current Sink for a Given V_MIN
  4.4 Current Mirrors
      Example 4.4-1 Aspect Ratio Errors in Current Amplifiers
  4.5 Current and Voltage References
      Example 4.5-1 Threshold Voltage Reference Circuit
  4.6 Bandgap Reference
      Example 4.6-1 Design of a Bandgap-Voltage Reference
  4.7 Summary
  Problems (1–26)
```

---

## 3. EvidenceAtom（§3.3）— 74 atoms，按小节分布（完整见 `gold.yaml`）

- **4.0 层级**：`A-INTRO-SUB`（章是 building-block 集合）、`A-HIER-OPAMP`（Fig 4.0-1 op-amp 构成图）。
- **4.1 MOS Switch**（`component_model` / `formula` / `physical_effect`）：开关模型 `A-SW-MODEL`、
  ON 电流式(1) `A-SW-ID-1`、ON 电阻 r_ON 式(2) `A-SW-RON-2`、r_ON 随 W/L 变化 `A-SW-RON-WL`、
  OFF 漏电 `A-SW-OFF`、寄生电容映射 `A-SW-CAPS`；clock feedthrough/charge injection `A-SW-CLOCKFEED`、
  slow/fast 判据 `A-SW-GVT-3`、误差式(6)(8) `A-SW-VERR-SLOW-6`/`A-SW-VERR-FAST-8`、抑制法 `A-SW-FEEDFIX`、
  CMOS 开关 `A-SW-CMOS`；例题 4.1-1（19.7 mV / 10.95 mV）。
- **4.2 MOS Diode/Active Resistor**：定义 `A-DIODE-DEF`、I 式(1)、V 式(2)、`r_out≈1/g_m` 式(3) `A-DIODE-ROUT-3`；例题 4.2-1（W/L=4.6）。
- **4.3 Current Sinks/Sources**：定义 `A-CS-DEF`、V_MIN 式(1)、`r_out≈1/(λI_D)` 式(2) `A-CS-ROUT-2`、
  cascode 原理 `A-CS-CASCODE-PRIN`、cascode `r_out≈g_m2 r_ds2 r_ds1` 式(6)(7) `A-CS-CASCODE-ROUT-7`、
  V_ON 偏置原理 `A-CS-VON-PRIN`、high-swing V_MIN=2V_ON `A-CS-HIGHSWING`；例题 4.3-1（250 kΩ/9.25 MΩ）、4.3-2（7.27/1.82）。
- **4.4 Current Mirrors**：定义 `A-CM-DEF`、ratio 通式(1)/理想(3)、三大非理想 `A-CM-NONIDEAL`、
  一阶增益误差式(14) `A-CM-KVT-14`、匹配原则 `A-CM-MATCH`、简单 mirror r_out 式(15)、cascode mirror 式(16)、Wilson/regulated `A-CM-WILSON`；例题 4.4-1（4±0.05，1.25%）。
- **4.5 References**：定义 `A-REF-DEF`、pn 参考式(2)(4)(5) `A-REF-PN-4`、MOS V_T 参考式(8) `A-REF-MOS-8`、
  bootstrap 原理 `A-REF-BOOTSTRAP`、温漂 `A-REF-TCF`；例题 4.5-1（−928 ppm/℃）。
- **4.6 Bandgap**：原理 `A-BG-PRINCIPLE`、V_REF=V_BE+K·V_t 式(1) `A-BG-VREF-1`、ΔV_BE PTAT 式(12) `A-BG-DVBE-12`、
  零温漂 K 式(16)(18) `A-BG-K-16`、常规 CMOS bandgap 式(22)(23) `A-BG-CONV-22`、二阶误差 `A-BG-2NDORDER`；例题 4.6-1（K=21.62, R2/R1=9.39, 1.262→1.153 V）。
- **4.7 / Problems**：`A-SUMMARY`；课后题 2/12/20/22。

---

## 4. SemanticChunk（§4.2）— 16 块

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-HIER` | `circuit_hierarchy_block` | 4 | **Fig 4.0-1 op-amp 构成层级**（+章引/小结），circuit_block_composed_of_block 边的来源 |
| `C-SW-MODEL` | `component_model_block` | 4 > 4.1 | 开关模型：r_ON 式 + OFF 漏电 + 寄生电容 |
| `C-SW-CLOCK` | `physical_effect_block` | 4 > 4.1 | clock feedthrough + slow/fast 误差模型 + 抑制 + CMOS 开关 |
| `C-EX-411` | `example_solution_block` | 4 > 4.1 > Ex 4.1-1 | 电荷馈通误差例题 |
| `C-DIODE` | `component_model_block` | 4 > 4.2 | MOS 二极管/有源电阻 + I/V/r_out |
| `C-EX-421` | `example_solution_block` | 4 > 4.2 > Ex 4.2-1 | 有源电阻定尺寸 |
| `C-CS` | `component_model_block` | 4 > 4.3 | 电流沉/源 + r_out + cascode 增强 + V_ON 偏置 + high-swing |
| `C-EX-431` | `example_solution_block` | 4 > 4.3 > Ex 4.3-1 | 输出电阻（简单 vs cascode） |
| `C-EX-432` | `example_solution_block` | 4 > 4.3 > Ex 4.3-2 | 给定 V_MIN 设计 cascode |
| `C-CM` | `component_model_block` | 4 > 4.4 | 电流镜 + ratio + 三非理想 + 匹配 + 简单/cascode/Wilson r_out |
| `C-EX-441` | `example_solution_block` | 4 > 4.4 > Ex 4.4-1 | 纵横比误差 |
| `C-REF` | `design_principle_block` | 4 > 4.5 | pn / MOS V_T 参考 + bootstrap + 温漂限制 |
| `C-EX-451` | `example_solution_block` | 4 > 4.5 > Ex 4.5-1 | V_T 参考温度系数 |
| `C-BG` | `design_principle_block` | 4 > 4.6 | bandgap 原理（CTAT+PTAT 抵消）+ 公式 + 常规实现 + 二阶误差 |
| `C-EX-461` | `example_solution_block` | 4 > 4.6 > Ex 4.6-1 | bandgap 设计 |
| `C-PROB` | `problem_set_block` | 4 > Problems | 课后题 |

> boundary：公式-说明同块（r_ON 式与其非饱和来源、cascode r_out 与其原理）；
> 例题题干/given/公式引用/结果同块；clock feedthrough 的 slow/fast 双式与效应描述同块。

---

## 5. ContextPackage（§6 示例）

`C-HIER` 的 package（`PKG-HIER`）把 Fig 4.0-1 的层级 mermaid 作为 `linked_context.figure`，
是 `circuit_block_composed_of_block` 抽取的上下文锚点；
`C-CS` 的 package（`PKG-CS-CASCODE`）把 `cascode r_out ≈ (g_m2 r_ds2) r_ds1` 与简单沉 `r_out ≈ 1/(λI_D)`
绑成 formula_context，紧接 Example 4.3-1。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8.2）

- **Object（47 个）**：`CircuitBlock`（Analog Subcircuits 根、op amp、diff-amp、MOS switch、CMOS switch、
  MOS diode、current sink、cascode sink、current mirror、current reference、bandgap）、
  `ComponentModel`（switch model、Wilson）、`Formula`（r_ON、charge-feedthrough、diode r_out、CS r_out、
  cascode r_out、mirror ratio、mirror error、pn ref、MOS V_T ref、bandgap、conventional bandgap）、
  `ExampleSolution`（4.1-1/4.2-1/4.3-1/4.3-2/4.4-1/4.5-1/4.6-1）、
  `PhysicalEffect`（clock feedthrough、r_ON/OFF、aspect-ratio error、ref temp-dependence、PTAT、bandgap 2nd-order）、
  `DesignPrinciple`（feedthrough mitigation、cascode、V_ON biasing、mirror matching、bootstrap、bandgap cancellation）、
  `ProblemStatement`（2/12/20/22）。

- **Relation（50 条，§8.2）**：
  - **`circuit_block_composed_of_block`（16 条，核心）**：
    - op amp → diff-amp / current sink / current mirror（R-01..03）；diff-amp → current sink / current mirror（R-04,05）；
    - Analog Subcircuits → {MOS switch, MOS diode, current sink, current mirror, current reference, bandgap}（R-06..11）；
    - cascode sink → current sink（堆叠晶体管，R-12）；current mirror → MOS diode（M1 二极管接法，R-13）；
    - **current reference → current mirror**（参考电流由电流镜构成，R-14）；**bandgap → current mirror**（R-15）；current mirror → Wilson 变体（R-16）。
  - `component_has_property`（7）：switch→r_ON/r_ON-OFF；MOS diode→r_out；current sink→r_out；cascode sink→cascode r_out；mirror→ratio；bandgap→V_REF。
  - `component_has_nonideality`（4）：switch→clock feedthrough；mirror→aspect-ratio error；reference→temp-dependence；bandgap→2nd-order errors。
  - `formula_used_in_example`（8）：charge-feedthrough→Ex4.1-1；CS/cascode r_out→Ex4.3-1；mirror ratio→Ex4.4-1；MOS V_T ref→Ex4.5-1；conventional/general bandgap→Ex4.6-1；diode r_out→Ex4.2-1。
  - `design_principle_applies_to_scenario`（7）+ `design_principle_has_tradeoff`（5）：cascode↔headroom、feedthrough-mit↔dummy-clock 代价、mirror-match↔面积、bandgap-cancel↔曲率/二阶误差、bootstrap↔温漂。
  - `problem_extends_example`（3）：题20→Ex4.4-1、题22→Ex4.6-1、题12→Ex4.3-1。
