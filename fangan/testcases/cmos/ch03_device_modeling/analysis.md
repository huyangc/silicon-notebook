# CMOS 教材 **第 3 章 CMOS Device Modeling** 在 qiefen 方案下的完整结构化抽取

源文件：`pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md`（MinerU 输出）
范围：**仅第 3 章（源文件第 3189–4872 行）**
profile：`textbook`（书籍型）

本文逐 stage 说明第 3 章经 `qiefen.md` 流水线后的完整形态；可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)。

> **schema 版本：`0.3.3-textbook`**（由 `build.py` 生成，`validate.py` 校验，请勿手改 `gold.yaml`）。
> 升级遵循 engram-ch2 的 v0.3.3 流程，但保留 textbook 类型词表（Concept/Formula/Variable/Derivation/ExampleSolution/ComponentModel/PhysicalEffect/DesignPrinciple/ProblemStatement）。

---

## 0. Profile 判定（§2）

命中 `Chapter 3` / `Example 3.1-1` / `Example 3.3-1` / `Example 3.6-1` / `PROBLEMS` / `Eq. (18)` / `Fig. 3.1-2` / `Table 3.1-2` / `SPICE LEVEL 1` / `BSIM3v3` → `textbook`。
抽取目标（§2.2）：`Concept` / `Definition` / `Formula` / `Variable` / `Derivation` / `ExampleProblem` / `ExampleSolution` / `ComponentModel` / `PhysicalEffect` / `DesignPrinciple` / `ProblemStatement`。

本章实体重心与第 2 章不同：第 2 章是工艺/pn 结，第 3 章是**器件模型**——核心对象是 `Formula`（漏电流公式族）、`ComponentModel`（Level1/Level3/BSIM3v3/小信号模型/电容模型）、`PhysicalEffect`（沟道长度调制、亚阈导通）。

---

## 1. SourceElement（§3.1）— MinerU 在第 3 章里的真实形态

- 标题：`# 3.1 Simple MOS Large-Signal`、`# Drain Current`（3.4 子小节）、`# Example 3.1-1 ...`、`# BSIM 3v3 Model`、`# PROBLEMS`。
- 公式：`$$ ... \tag{N} $$`，**每个小节内 tag 独立从 1 重新编号**——3.1 有 tag1–20、3.2 又从 tag1 起（含 1,2,3,4,5,7,8,9–11,12）、3.3 从 tag1 起、3.4 从 tag1 起、3.5 从 tag1 起、3.6 也从 tag1 起。解析时 tag 必须带 `section_path` 限定，否则 "Eq.(1)" 跨节冲突严重（本章 6 个小节各有 Eq.(1)）。
- 符号图：MOSFET/BJT 器件符号被 MinerU 误标成 `<details><summary>chemical</summary>`（Fig 3.1-1、3.2-1、3.3-1）。
- 波形/曲线：输出特性、跨导特性、噪声谱、亚阈温度曲线渲染为 `<details><summary>line</summary>| 表格 |</details>`，**数值多为占位/近似**（如 Fig 3.1-3 全是 1.0），不可当精确结果引用。
- 重叠电容剖面用 `<details><summary>flowchart</summary>```mermaid```</details>`（Fig 3.2-5）。
- 表格：HTML `<table>`（Table 3.1-1 硅常数、3.1-2 简单模型参数、3.2-1 电容系数、3.3-1/3.3-2 小信号关系、3.4-1 Level3 参数）。
- 例题 3.6-x 的主体是 SPICE 输入网表（```batch/```txt 代码块）+ 仿真曲线。

> 解析要点（写入 `source_meta.parsing_notes`）：
> 1) tag 按 section 重置；2) 3.4 用 SPICE 大写参数名 (KP/GAMMA/PHI/COX/U0/NSUB)，与简单模型小写物理符号 (mu_o,C_ox,2 phi_F) 是同一物理量，canonicalization 需对齐；3) BSIM3v3 无方程、只有参数集+效应清单，作 ComponentModel；4) 3.6 四个例题共用 Fig 3.6-4 同一电路。

---

## 2. Section Tree（§3.2）

```
3 CMOS Device Modeling
  3.1 Simple MOS Large-Signal Model
      Example 3.1-1 Application of the Simple MOS Large Signal Model
  3.2 Other MOS Large-Signal Model Parameters
  3.3 Small-Signal Model for the MOS Transistor
      Example 3.3-1 Typical Values of Small Signal Model Parameters
      Example 3.3-2 Small-Signal Parameters in the Nonsaturated Region
  3.4 Computer Simulation Models
      SPICE Level 3 Model (Drain Current / Threshold / Saturation Voltage / Effective Mobility / Channel-Length Modulation)
      BSIM3v3 Model
  3.5 Subthreshold MOS Model
  3.6 SPICE Simulation of MOS Circuits
      Example 3.6-1 / 3.6-3 / 3.6-4
  3.7 Summary
  PROBLEMS (1–23)
```

注：3.4 的 Drain Current / Threshold Voltage / Saturation Voltage / Effective Mobility / Channel-Length Modulation 是 SPICE Level 3 的内部子标题，未单独建 section（归入 `SEC-3.4-L3`），但其公式各成 atom（A-CS-L3-*）。

---

## 3. EvidenceAtom（§3.3）— 按小节分布（完整见 `gold.yaml`，共 75 atoms）

### 3.1 简单大信号模型（formula / derivation / variable / example）
- `formula_atom`：Sah 方程 Eq.(1) `i_D=(mu_o C_ox W/L)[...]`（A-LS-SAH-1）；V_T Eq.(2)、gamma Eq.(4)；电学写法 Eq.(12)/beta Eq.(13)。
- 工作区推导链：cutoff Eq.(14) → v_DS(sat) Eq.(15) → 非饱和 Eq.(16) → 饱和 Eq.(17) → 含沟道长度调制饱和 Eq.(18)；外加 V_ON Eq.(19/20)。
- `physical_effect_atom`：channel length modulation（A-LS-CLM-EFFECT）——以 `(1+lambda v_DS)` 修正饱和电流。
- `definition_atom`：五参数 (K',V_T,gamma,lambda,2 phi_F) 定义 Level 1（A-LS-FIVE-PARAMS）。
- `table_row_atom`：Table 3.1-2（A-LS-T312，含 lambda、K' 典型值）。
- Example 3.1-1：n 沟 i_D=520 uA，p 沟 i_D=243 uA（A-EX311-RESULT，原文真实数字）。

### 3.2 其它大信号参数（formula / component_model）
- bulk pn 结二极管 Eq.(1)/(2)（A-LS2-DIODE-1）；r_D/r_S 50–100 Ohm。
- 耗尽电容两区 Eq.(3)/(4) + bottom/sidewall Eq.(5)（A-LS2-CBX-3/4/SW-5）。
- 电荷存储电容：overlap C_1=C_3 Eq.(7)、gate-channel C_2 Eq.(8)、按 off/saturation/nonsaturation 分配 C_GS/C_GD/C_GB Eq.(9–11)（A-LS2-CGREGIONS）。
- Table 3.2-1 系数（A-LS2-T321）。噪声 Eq.(12)（A-LS2-NOISE-12）。

### 3.3 小信号模型（formula / component_model / example）
- 定义式 g_m/g_mbs/g_ds = 偏导 Eq.(3–5)；饱和区 g_m=sqrt(2K'(W/L)|I_D|) Eq.(6)、g_mbs=eta g_m Eq.(8)、g_ds≈I_D lambda Eq.(9)；Table 3.3-1。
- Example 3.3-1（饱和：g_m=105/70.7 uA/V 等）；Example 3.3-2（非饱和：r_ds=3.05/6.99 KOhm 等）。

### 3.4 计算机仿真模型（formula / component_model / physical_effect）
- SPICE Level 3：drain current Eq.(1)+BETA、threshold Eq.(12)（含短沟 v_DS 项）、saturation voltage Eq.(15/16)、effective mobility Eq.(18/19)（THETA 退化）、channel-length modulation Eq.(20/21)（KAPPA）、mobility 温漂 Eq.(15) UO(T)。Table 3.4-1 参数。
- BSIM3v3：型谱 BSIM1(60 参数)→BSIM2(99)→BSIM3(40)→BSIM3v3（业界标准）（A-CS-BSIM-HISTORY）；所建模 8 类深亚微米效应（A-CS-BSIM-EFFECTS）；Level1/Level3/BSIM3v3 精度对比（A-CS-MODEL-COMPARE）。

### 3.5 亚阈模型（physical_effect / formula / condition）
- weak inversion 概念（square-law→exponential）（A-ST-CONCEPT）；SPICE v_on Eq.(1/2)；SPICE 弱反型电流 Eq.(3)；手算 `i_D≈(W/L)I_DO exp(v_GS/(n kT/q))` Eq.(5)（A-ST-IDS-HAND-5，1<n<3）；进入条件 Eq.(6) + 中度反型区；温度行为（电流随温升而增）。

### 3.6 SPICE 仿真（technology_process / design_principle / example）
- 网表语法 M<#> D G S B（A-SP-NETLIST）；multiplier M= 对应 unit-matching（A-SP-MULT）。
- Example 3.6-1（LEVEL 1 输出特性，含 Table 3.6-1 网表）；3.6-3（ac，~36 dB，180→90 deg）；3.6-4（瞬态 PWL）。

### PROBLEMS（problem_statement）
- 题1（画输出特性）、题5（Eq.18 与 Eq.11 在 v_DS(sat) 衔接）、题11（W/L=100/10 重做 Ex.3.3-1/2）、题15（弱反型 V_ON）、题16（弱反型小信号跨导推导）。

---

## 4. SemanticChunk（§4.2）— 第 3 章主要块（17 块）

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-OV` | `chapter_overview_block` | 3 | 三级模型概述（intro + summary） |
| `C-LS-MODEL` | `formula_definition_block` | 3 > 3.1 | Sah 方程 + 参数定义 + Table 3.1-2（公式-说明同块） |
| `C-LS-REGIONS` | `derivation_block` | 3 > 3.1 | cutoff→v_DS(sat)→非饱和→饱和→含 CLM 饱和 推导链 |
| `C-EX-311` | `example_solution_block` | 3 > 3.1 > Example 3.1-1 | 题/given/公式引用/结果 |
| `C-LS2-JUNC` | `formula_definition_block` | 3 > 3.2 | bulk 二极管 + r_D/r_S + 耗尽电容两区/侧壁 |
| `C-LS2-CAPS` | `component_model_block` | 3 > 3.2 | 电荷存储电容(overlap/C_2/分区) + Table 3.2-1 + 噪声 |
| `C-SS-MODEL` | `component_model_block` | 3 > 3.3 | 小信号模型 g_m/g_mbs/g_ds 定义+饱和闭式+Table 3.3-1 |
| `C-EX-331` | `example_solution_block` | 3 > 3.3 > Example 3.3-1 | 饱和小信号参数 |
| `C-EX-332` | `example_solution_block` | 3 > 3.3 > Example 3.3-2 | 非饱和小信号参数 |
| `C-L3` | `formula_definition_block` | 3 > 3.4 > SPICE Level 3 | Level 3 五个子小节方程 + Table 3.4-1 |
| `C-BSIM` | `component_model_block` | 3 > 3.4 > BSIM3v3 | BSIM 型谱 + 深亚微米效应 + 模型对比 |
| `C-SUBTHRESHOLD` | `physical_effect_block` | 3 > 3.5 | 弱反型效应 + SPICE/手算指数电流 + 温度 |
| `C-SPICE` | `technology_process_block` | 3 > 3.6 | SPICE 实例/模型语法 + multiplier 原则 |
| `C-EX-361` | `example_solution_block` | 3 > 3.6 > Example 3.6-1 | 输出特性仿真 + 网表 + 结果 |
| `C-EX-363` | `example_solution_block` | 3 > 3.6 > Example 3.6-3 | ac 分析 |
| `C-EX-364` | `example_solution_block` | 3 > 3.6 > Example 3.6-4 | 瞬态分析 |
| `C-PROB` | `problem_set_block` | 3 > PROBLEMS | 课后题 |

> anchor expansion（§5.2）典型：`C-LS-REGIONS` 把 Eq.(15)→(16)→(17)→(18) 与 CLM 文字效应收进同块（推导连续性）；`C-L3` 把 SPICE Level 3 的 Drain Current/Threshold/Saturation/Mobility/CLM 五个子标题方程 + Table 3.4-1 当作**一个模型参数集**不切开；`C-EX-361` 把例题题干 + Table 3.6-1 网表 + Fig 3.6-3 结果绑同块。

---

## 5. ContextPackage（§6 书籍型示例）

`C-LS-REGIONS` 的 package（`PKG-LS-REGIONS`）：

```
Document: CMOS Analog Circuit Design
Section: Chapter 3 > 3.1 Simple MOS Large-Signal Model > Regions of Operation
Atoms:
[A-LS-VDSAT-15]   v_DS(sat) = v_GS - V_T (boundary)
[A-LS-NONSAT-16]  nonsaturation i_D (Eq.16)
[A-LS-SAT-17]     saturation i_D, lambda=0 (Eq.17)
[A-LS-CLM-EFFECT] channel length modulation (physical effect)
[A-LS-SAT-CLM-18] saturation i_D with (1+lambda v_DS) (Eq.18)
linked_context:
  formula_context: "Eq.(17) modified by (1+lambda v_DS) -> Eq.(18); boundary v_DS(sat)=v_GS-V_T"
  previous_heading: "3.1 Simple MOS Large-Signal Model (Eq.1 Sah equation)"
  next_heading: "Example 3.1-1"
Targets: Formula, Variable, PhysicalEffect
```

第二个 package `PKG-L3` 演示 SPICE Level 3 模型参数集（drain current/threshold/mobility/CLM + Table 3.4-1）。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8）

- **Mention（15）**：Sah equation / channel length modulation / lambda / K' / threshold voltage / saturation voltage / g_m / g_ds / g_mbs / small-signal model / SPICE Level 3 model / BSIM3v3 / weak inversion / depletion capacitance / overlap capacitance。
- **canonicalization** 重点对齐：`K' ~ KP`、`2 phi_F ~ PHI`（SPICE 恒正）、`Sah equation ~ Shichman-Hodges ~ Level 1 drain current`、`subthreshold ~ weak inversion`。
- **Object（33）**：
  - `Concept`：small-signal model。
  - `Formula`：Sah drain current / beta / v_DS(sat) / 非饱和 i_D / 饱和 i_D(含 CLM) / V_T(body effect)。
  - `Variable`：lambda、K'。
  - `Derivation`：regions of operation（工作区推导链）。
  - `ExampleSolution`：3.1-1 / 3.3-1 / 3.3-2 / 3.6-1 / 3.6-3 / 3.6-4。
  - `ComponentModel`：bulk junctions / depletion caps / charge-storage caps / small-signal model / SPICE Level 1 / SPICE Level 3 / BSIM3v3。
  - `PhysicalEffect`：channel length modulation / subthreshold conduction / deep-submicron BSIM effects / MOS drain-current noise。
  - `DesignPrinciple`：multiplier(unit-matching) in SPICE。
  - `ProblemStatement`：题 1/5/11/15/16。
- **Relation（30，全 ID 化、带证据）**——覆盖 §8.2 既有类型 + 本章按语义新增的清晰类型：
  - `formula_defines_variable`：Sah drain current → K'（R-01）；饱和 i_D → lambda（R-05）；小信号模型 → lambda（R-16）。
  - `formula_derived_from_formula`：饱和 i_D ← 非饱和 i_D / Sah（R-02,R-03）；v_DS(sat) ← Sah（R-06）；小信号模型 ← 饱和 i_D（R-15，偏导导出）。
  - `physical_effect_modifies_formula`（题目点名的类型）：channel length modulation → 饱和电流 Eq.18（R-04）；CLM → Level 3 模型（R-23）；subthreshold → 饱和电流（R-25）。
  - `formula_used_in_example`：饱和 i_D / v_DS(sat) → Ex.3.1-1（R-08,R-09）；小信号模型 → Ex.3.3-1/3.3-2（R-17,R-18）。
  - `model_refines_model`（题目点名的类型）：Level 3 → Level 1（R-20）；BSIM3v3 → Level 3（R-21）。这是本章相对第 2 章新增的核心关系，刻画三级模型精度递进。
  - `model_includes_formula` / `model_includes_component`：Level 1 → Sah 方程（R-10）、→ 耗尽电容/电荷存储电容（R-11,R-12）。
  - `component_models_effect`：BSIM3v3 → 深亚微米效应（R-22）；Level 3 → 弱反型（R-24）。
  - `derivation_produces_formula`：regions 推导 → 饱和 i_D（R-07）。
  - `concept_defines_term` / `component_has_property` / `design_principle_applies_to_scenario`：见 R-19/R-13/R-14/R-27。
  - `problem_extends_example` / `problem_extends_formula` / `problem_extends_effect`：题11 → Ex.3.3-1（R-28）；题5 → 饱和 i_D（R-29）；题15 → 弱反型（R-30）。

> 新增关系类型说明：`model_refines_model`、`model_includes_formula`、`model_includes_component`、`component_models_effect`、`physical_effect_modifies_formula`、`problem_extends_formula/effect` 不在 §8.2 原始清单里，但语义清晰、贴合"器件模型章"特性（模型层级 + 效应建模），符合 §8.2 "确有需要的新类型可用，但要语义清晰"的放行条款。

---

## 7. 版本变更记录（v0.1 → v0.3.3-textbook）

本轮把 v0.1（改写摘要、无坐标、用小数 `confidence`、无负例）整体升级到 engram-ch2 的 v0.3.3 严格评测形态，**ID 与语义全部保留**，仅改"表达方式"：

- **双坐标 + 逐字 span**：每个 atom 现有 `source_span`（权威，指向原始 MinerU `.md`）+ `viewer_span`（`viewer_only:true`，指向 `source.md`），`raw_text` 为逐字连续 span，`normalized_text` 仅渲染该 span。`build.py` 用三个定位器算 span，不手写：
  - `by_tag(N, win)`：在**子小节行窗**内按 `\tag {N}` 定位公式行（解决 tag 逐节重置：3.1 tag1=Sah@3233、3.2 tag1=二极管@3466、3.4-L3 tag1=漏电流@4036 互不冲突）。
  - `by_anchor(raw, win)`：窗内唯一子串（散文/例题/表/网表）。
  - `by_line` / `line_between`：行内精确切片（MinerU 把数字打散成 `1 1 0`、`0. 7`、`5 2 0 \mu A`，并用 U+2212 减号、`KΩ`，故用标记切片避免手抄出错）。
- **公式/条件 atom 纯净**：`formula_atom` / `condition_atom` 的 `normalized` 只渲染公式本体（去掉先前夹带的英文解释），物理语义移入 object payload；`validate.py` 用 `_GLOSS` 正则强制（33 个带 tag 的公式/条件 atom 全部通过）。
- **对象证据 local/supporting + home_package**：每个 object 拆 `local_evidence_atom_ids`（在 home package 的 chunk 内）+ `supporting_context_atom_ids`（他 chunk）。跨 package 对象：`VAR-LAMBDA`（home `PKG-LS-REGIONS`，Table 3.1-2 典型值在 `PKG-LS-MODEL` 作 supporting）、`COMP-SPICE-LEVEL1`（home `PKG-LS-REGIONS`，Sah 方程在 `PKG-LS-MODEL` 作 supporting；`PKG-EX-361` 仅凭 LEVEL 1 网表可按名识别，故声明 `expected_local_fields`）、`COMP-DEPLETION-CAPS`（Table 3.2-1 在 `PKG-LS2-CAPS` 作 supporting）。
- **每 chunk 一个 context_package**：17 chunk ↔ 17 package，`package.atoms == chunk.atom_ids`，`expected_objects` = home 在该 package 的对象；`PKG-OV`（章引/小结）`note` 标明全 `context_only`、不出对象。
- **标签**：object/relation 全部 `gold_label/difficulty/evidence_strength`，删除所有 `confidence`。
- **do_not_extract 负例**（9 条）：bracket `[n]` 引用 + 政策、`Eq./Fig./Table` 跨引、SPICE 关键字/控制卡 token（`.MODEL/.DC/.AC/.TRAN/NMOS/PWL…`，非知识对象——知识是 `COMP-SPICE-LEVEL1` 而非 `.MODEL` 关键字）、图内子标签（Fig 3.1-1(a)）、出章引用（Chapter 2、Sec 2.x）、curated 集外的 Example 3.6-2。
- **跨 package 对象示例（本章特点）**：器件模型对象的证据天然跨小节——`COMP-SPICE-LEVEL3`（3.4 漏电流/阈值/迁移率/CLM + Table 3.4-1）、小信号 `g_m/g_ds`（3.3）经 `R-15 formula_derived_from_formula` 连回 3.1 的饱和 i_D。
- **校验**：`validate.py` 跑全部不变量（双 span 逐字、引用解析、chunk 覆盖、package==chunk、expected_objects⊆objects、local⊆home-chunk / supporting⊄home-chunk、关系端点∈objects、无 confidence、数据表数值越界审计、公式/条件无 gloss、全局无孤儿=对象/关系证据∨context_only∨do_not_extract 引用），结果 **ALL CHECKS PASS**。
- **计数**：atoms=75、source_elements=75、chunks=17、context_packages=17、objects=33、relations=30、mentions=15、do_not_extract=9。

> 源文件坑（写入 `source_meta.parsing_notes`）：① 公式 tag 逐子小节重置（3.1 tag1–20、3.2 含 9a/9b/9c…分裂子式、3.3 Eqs1–9 后例题间 10/11/12、Level 3 tag1–22 后再来一段温度 tag15–24、3.5 tag1–6、3.6 参数散文 tag1–4）；② Eqs 9/10/11（分区电荷存储电容）渲染成 9 个单行子式（3714–3764），`A-LS2-CGREGIONS` 锚定其引导散文行，分区分配进 payload；③ Level 3 用大写 SPICE 名（KP/GAMMA/PHI…）= 简单模型小写物理量，canonicalization 对齐；④ BSIM3v3 无方程、只有参数计数谱系 + 8 项深亚微米效应清单；⑤ Example 3.6-x 共用 Fig 3.6-4，主体是 ```batch/```txt 网表 + 占位仿真曲线。
