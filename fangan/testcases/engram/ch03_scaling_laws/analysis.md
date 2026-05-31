# Engram 论文 **第 3 章 Scaling Laws and Sparsity Allocation** 在 qiefen 方案下的结构化抽取（schema v0.3.3）

源文件（**权威**）：`pdf_parser/engram_paper_mineru.md`（MinerU 输出）
范围：**仅第 3 章（源文件第 110–157 行，含 3.1 / 3.2 两小节）**
profile：`article_research`（论文型），schema_version `0.3.3`。
`source.md` 是 viewer-only 的逐字切片（行 110–157），所有严格评测坐标指向原始文件。
gold/source 由 `build.py` 生成、`validate.py` 校验（span 脚本定位，**不手工誊写**）。

本章是论文的 headline scaling-law 结果，也是 qiefen `§5.2 Anchor Expansion` 的**标准范例**。
可加载 gold 数据见同目录 [`gold.yaml`](./gold.yaml)。

---

## v0.3.3 相对 v0.1 的变更（本次升级）

知识内容（atom/chunk/object/relation 的 id 与语义）保持不变，仅按 v0.3.3 schema 重表达并补逐字 span：

- **双坐标**：每个 atom 同时带 `source_span`（权威，file=`engram_paper_mineru.md`，含 line/char）与
  `viewer_span`（`viewer_only:true`，file=`source.md`）；二者切片都等于 `raw_text`（build/validate 断言）。
- **raw/normalized 纪律**：`raw_text` 是原文逐字连续 span（按句/子句锚定）；`normalized_text` 只渲染该 span
  （`$\rho$`→`rho`、`$P_{\text{tot}}$`→`P_tot`、`\triangleq`→`:=`、`\rightarrow`→`->`、`\times`→`x`、`$\Delta$`→`delta`、
  U+2013 en-dash→ASCII `-`），不引入 span 外的事实/数字/章节号。`validate.py` 额外跑**数值越界审计**：
  非公式 atom 的 normalized 里每个数字都必须出现在其 raw span 内。
- **公式 atom 纯净 + 拆分**：v0.1 的式(7)拆为两个 atom——`A-ALLOC-RHO-007`（formula_atom，raw 只含式(7)逐字串
  `... \tag {7}`）+ `A-ALLOC-RHO-DEF`（definition_atom，承载 `rho ∈ [0,1]` 的语义定义句）。式(7) atom 经
  `metadata.supported_by_context_atoms: [A-ALLOC-RHO-DEF]` 引用邻句符号解释，公式本体不夹带 span 外说明。
  故 atom 数由 v0.1 的 16 增至 **17**。
- **对象证据 local/supporting + home_package**：对象用 `local_evidence_atom_ids`（在 home_package 的 chunk 内）+
  `supporting_context_atom_ids`（来自其它 chunk）+ `home_package`。唯一跨 package 对象
  `CLAIM-COMPLEMENTARITY`（home=`PKG-INTRO`）的 supporting 证据 `A-INF-CLAIM-001` 来自 `PKG-INF`。
- **per-chunk context_packages + expected_objects**：每个核心 chunk 一个 package，`atoms == chunk.atom_ids`：
  `PKG-INTRO`(C3-INTRO) / `PKG-ALLOC`(C3-ALLOC) / `PKG-INF`(C3-INF)。`expected_objects` 列其 home_package 为该包的对象；
  跨 package 对象 `CLAIM-COMPLEMENTARITY` 在 `PKG-INTRO`（可抽 `statement`/`scoped_by`）与 `PKG-INF`（仅 `statement`）
  各声明 `expected_local_fields`。
- **标签**：objects/relations 去掉小数 `confidence`，改用 `gold_label:true` + `difficulty`(easy/medium/hard) +
  `evidence_strength`(direct/indirect/cross_reference)。
- **do_not_extract（负例）**：内联 author-year citation `(Huang et al., 2025a)` / `(Yu et al., 2025)` +
  `inline_author_year_citation` policy；图引用 label `Figure 3 (left)` / `Figure 3 (right)`（结果数值来自正文，图本身不可抽）；
  跨切片引用 `detailed in Section 2.5`（后向）/ `Section 3.1`（同级）。
- **source_elements**：由 atoms 按 `source_element_id` 自动分组生成（共 16 个：3 个 heading + 13 个段落/公式），
  式(7)元素 `SE-3.1-EQ7` 带 `metadata.tag:"7"`。

---

## 0. Profile 判定（§2.1）

命中 `Compute-matched formulation` / `Experimental protocol` / `Results and Analysis` /
`validation loss` / `Figure 3` / `FLOPs` / `U-shaped relationship` / `power law` → `article_research`。
抽取目标（§2.1）：`ArticleClaim` / `ScalingLaw` / `ExperimentSetup` / `ExperimentResult` /
`MechanisticExplanation` / `DerivedRuleCandidate`。

---

## 1. SourceElement（§3.1）— MinerU 在第 3 章里的真实形态

- 标题：`# 3. Scaling Laws and Sparsity Allocation`、`# 3.1. Optimal Allocation Ratio ...`、`# 3.2. Engram under Infinite Memory Regime`（MinerU 把带点编号渲染成一级 `#`）。
- 内联变量：`$P_{\text{tot}}$`、`$P_{\text{act}}$`、`$P_{\text{sparse}} \triangleq P_{\text{tot}} - P_{\text{act}}$`、`$\rho \in [0,1]$`，需归一为 ASCII：`P_tot / P_act / P_sparse / rho`。
- 唯一块级公式：式(7) allocation ratio，`$$ ... \tag{7} $$`（论文**全局连续编号**，非按小节重排，与 CMOS 教材不同）。
  公式块 `$$` 定界符在源文件第 129/131 行，公式体（含 `\tag {7}`）在第 130 行；`build.py` 用完整逐字串
  （在切片内唯一出现，`\tag {7}` 也作为 anchor 的一部分参与匹配）定位，避免与其它公式或 inline `$...$` 误配。
- 数值散布在正文小项目符号里（两个 regime 的 FLOPs / P_tot / P_act / 专家数；slot sweep 范围）——不是表格，须从正文抽。
  P_tot/P_act/P_sparse 定义是 3 项 bullet（行 121–123，作为一个连续 span）；两个 FLOPs regime 是 bullet，含在 setup span（行 138–143）内。
- 图：`Figure 3 (left)` = allocation U-shape；`Figure 3 (right)` = infinite-memory power law；**图本体未在本切片中以 `<table>`/`![]()` 渲染**，结果数值全部来自正文叙述。

> 解析要点：3.2 的 baseline 含两条引用（OverEncoding=Huang et al. 2025a 用；SCONE=Yu et al. 2025 被排除），
> 抽 ExperimentSetup 时要把"为什么排除 SCONE（iso-compute 约束）"一起保留，否则丢失对比设计的关键约束。

---

## 2. Section Tree（§3.2）

```
3 Scaling Laws and Sparsity Allocation      (SEC-3)
  3.1 Optimal Allocation Ratio Between MoE and Engram   (SEC-3.1)
  3.2 Engram under Infinite Memory Regime               (SEC-3.2)
```

---

## 3. EvidenceAtom（§3.3）— 共 17 atom

| 区段 | atom | atom_type | 内容要点 |
| --- | --- | --- | --- |
| 3.0 | `A-SCALE-CLAIM-001` | claim_sentence | conditional memory 与 MoE conditional computation 结构互补 |
| 3.0 | `A-SCALE-Q-001` | claim_sentence | 两个研究问题：有限约束下分配 / 无限内存 regime |
| 3.1 | `A-ALLOC-DEF-001` | definition_atom | P_tot / P_act / P_sparse = P_tot − P_act 定义 |
| 3.1 | `A-ALLOC-CONTROL-001` | experiment_setup_atom | 控制变量：每个 FLOPs 预算内固定 P_tot/P_act |
| 3.1 | `A-ALLOC-RHO-007` | formula_atom | **式(7)** P_MoE^sparse=rho·P_sparse；P_Engram=(1−rho)·P_sparse |
| 3.1 | `A-ALLOC-RHO-DEF` | definition_atom | allocation ratio rho ∈ [0,1] 定义（经 supported_by_context_atoms 支撑式(7)） |
| 3.1 | `A-ALLOC-RHO-LIMITS` | definition_atom | rho=1 纯 MoE；rho<1 把专家预算挪给 Engram slots |
| 3.1 | `A-ALLOC-SETUP-001` | experiment_setup_atom | 两 regime（2e20:5.7B/568M/106 专家；6e20:9.9B/993M/99 专家），P_tot/P_act≈10 |
| 3.1 | `A-ALLOC-RESULT-USHAPE` | scaling_law_result_atom | U 形；rho≈40% 即可比肩纯 MoE |
| 3.1 | `A-ALLOC-RESULT-OPT` | scaling_law_result_atom | 最优 rho≈75–80%；10B regime loss 1.7248→1.7109（Δ=0.0139） |
| 3.1 | `A-ALLOC-MECH-MOE` | mechanism_sentence | MoE-dominated：无专用 memory，靠深度重建静态模式 |
| 3.1 | `A-ALLOC-MECH-ENGRAM` | mechanism_sentence | Engram-dominated：失去条件计算；memory 不能替代 compute |
| 3.2 | `A-INF-SETUP-001` | experiment_setup_atom | 固定 3B/568M backbone，100B tokens，slot M 从 2.58e5→1.0e7（+≈13B 参数） |
| 3.2 | `A-INF-BASELINE-001` | experiment_setup_atom | baseline=OverEncoding；排除 SCONE（破坏 iso-compute） |
| 3.2 | `A-INF-RESULT-POWERLAW` | scaling_law_result_atom | 严格幂律（log 空间线性）；memory 是可预测的 scaling knob |
| 3.2 | `A-INF-RESULT-VS-OE` | result_sentence | 同等内存预算下 Engram 解锁远大于 OverEncoding 的 scaling |
| 3.2 | `A-INF-CLAIM-001` | claim_sentence | conditional memory 是独立、可扩展的稀疏维度，补充 MoE |

---

## 4. SemanticChunk（§4.1 论文型 chunk）— 本章 3 块

| chunk | chunk_type | section_path | boundary_reason 摘要 |
| --- | --- | --- | --- |
| `C3-INTRO` | `article_core_claim_block` | 3 | 章引：互补论点 + 两个研究问题（界定 3.1/3.2） |
| `C3-ALLOC` | `scaling_law_block` | 3 > 3.1 | **§5.2 锚点扩展整块**（见下） |
| `C3-INF` | `scaling_law_block` | 3 > 3.2 | 无限内存实验：setup+baseline 与幂律结果同块（实验设置-结果耦合） |

### 4.1 §5.2 Anchor Expansion 范例（核心）

`C3-ALLOC` 完整复现 qiefen §5.2 给出的论文型锚点扩展示例：

- **anchor 检测**：`"U-shaped relationship between validation loss and allocation ratio"`（`A-ALLOC-RESULT-USHAPE`，对应 ScalingLaw）。
- **向前扩展**（§5.2 列出的 4 项全部到位）：
  - `P_tot / P_act / P_sparse` 定义 → `A-ALLOC-DEF-001`
  - `rho` 公式（式7）→ `A-ALLOC-RHO-007`（+ `A-ALLOC-RHO-DEF` 定义、`A-ALLOC-RHO-LIMITS` 极值语义）
  - 实验设置 → `A-ALLOC-SETUP-001`
  - 控制变量 → `A-ALLOC-CONTROL-001`
- **向后扩展**（§5.2 列出的 4 项全部到位）：
  - 最优 rho → `A-ALLOC-RESULT-OPT`（75–80%）
  - 性能数值 → `A-ALLOC-RESULT-OPT`（1.7248→1.7109, Δ=0.0139）
  - 机制解释 / MoE-dominated 分析 → `A-ALLOC-MECH-MOE`
  - Engram-dominated 分析 → `A-ALLOC-MECH-ENGRAM`

整条 scaling law 是**一个知识单元，不可切开**：边界打分上 `formula_continuity` 与
"实验设置-结果耦合"为负，压制了 `heading_change` 之外的所有切点，故 10 个 atom 收进同一 `scaling_law_block`。

---

## 5. ContextPackage（§6 论文型示例）

`C3-ALLOC` 的 package `PKG-ALLOC`（`atoms == C3-ALLOC.atom_ids`，全 10 atom）：

```
Document: Engram: Conditional Memory as a Scalable Axis of Sparsity
Section: Chapter 3 > 3.1 Optimal Allocation Ratio Between MoE and Engram
Atoms:
[A-ALLOC-DEF-001]       P_tot/P_act/P_sparse 定义
[A-ALLOC-CONTROL-001]   控制变量
[A-ALLOC-RHO-007]       式(7) allocation ratio rho
[A-ALLOC-RHO-DEF]       rho ∈ [0,1] 定义
[A-ALLOC-RHO-LIMITS]    rho 极值语义
[A-ALLOC-SETUP-001]     两 regime 实验设置
[A-ALLOC-RESULT-USHAPE] U 形结果（anchor）
[A-ALLOC-RESULT-OPT]    最优 rho + loss 数值
[A-ALLOC-MECH-MOE]      MoE-dominated 机制
[A-ALLOC-MECH-ENGRAM]   Engram-dominated 机制
linked_context:
  formula_context: "Eq.(7) rho 在 P_sparse 上分配 MoE 与 Engram"
  figure: "Figure 3 (left): validation loss vs rho (U-shaped)"
  previous_heading: "3. Scaling Laws and Sparsity Allocation"
  next_heading: "3.2 Engram under Infinite Memory Regime"
Targets: ScalingLaw, ExperimentSetup, ExperimentResult, MechanisticExplanation, DerivedRuleCandidate
expected_objects: SETUP-ALLOC, SCALINGLAW-USHAPE, RESULT-ALLOC, MECH-COMPLEMENTARITY, RULE-OPTIMAL-RHO
```

另两个 package：`PKG-INTRO`（C3-INTRO，expected_objects=`CLAIM-COMPLEMENTARITY`，
并声明该跨 package 对象本地可抽 `statement`/`scoped_by`）；
`PKG-INF`（C3-INF，expected_objects=`SETUP-INF`/`SCALINGLAW-INF`/`RESULT-INF`，
并声明 `CLAIM-COMPLEMENTARITY` 在本包仅可抽 `statement`）。

---

## 6. Mention → 7. Object → 8. Relation（§7–§8.1）

- **Mention**（10 条）：P_sparse / allocation ratio rho / iso-FLOPs setup / U-shaped relationship /
  optimal rho 75-80% / MoE-dominated regime / infinite memory regime / OverEncoding /
  power law (log-linear) / conditional memory。
- **Object**（9 个，覆盖任务要求的全部论文型类型；均带 local/supporting + home_package + 标签）：
  - `ArticleClaim`：`CLAIM-COMPLEMENTARITY`（互补性总论点，home=`PKG-INTRO`，supporting 证据 `A-INF-CLAIM-001`@PKG-INF）
  - `ExperimentSetup`：`SETUP-ALLOC`（3.1 iso-FLOPs 双 regime）、`SETUP-INF`（3.2 无限内存）
  - `ScalingLaw`：`SCALINGLAW-USHAPE`（U 形 allocation law）、`SCALINGLAW-INF`（幂律）
  - `ExperimentResult`：`RESULT-ALLOC`、`RESULT-INF`
  - `MechanisticExplanation`：`MECH-COMPLEMENTARITY`（MoE-/Engram-dominated 双侧）
  - `DerivedRuleCandidate`：`RULE-OPTIMAL-RHO`（分≈20–25% 给 Engram；rho≈75–80%）
- **Relation**（8 条，§8.1，端点全为合法 object_id、均带证据）：
  - `experiment_tests_claim`：`SETUP-ALLOC`→`CLAIM-COMPLEMENTARITY`（R3-01）；`SETUP-INF`→`CLAIM-COMPLEMENTARITY`（R3-06）
  - `result_supports_claim`：`RESULT-ALLOC`→`CLAIM-COMPLEMENTARITY`（R3-02）、→`SCALINGLAW-USHAPE`（R3-03）；
    `RESULT-INF`→`SCALINGLAW-INF`（R3-07）、→`CLAIM-COMPLEMENTARITY`（R3-08）
  - `mechanism_explains_result`：`MECH-COMPLEMENTARITY`→`RESULT-ALLOC`（R3-04）
  - `claim_suggests_design_rule`：`SCALINGLAW-USHAPE`→`RULE-OPTIMAL-RHO`（R3-05），
    该 DerivedRuleCandidate 即第 4 章 large-scale 预训练采用的 "empirically derived allocation law"。

---

## 自检（`validate.py`，全绿）

- YAML 可解析；`schema_version == 0.3.3`。
- **双坐标逐字**：每 atom `source_file[source_span] == raw_text` 且 `source.md[viewer_span] == raw_text`（viewer_only:true）。
- 引用完整性：atom.section_id∈section_tree、source_element_id∈source_elements；17 个 atom 每个至少进一个 chunk；
  chunk.central/gold_must_cover ⊆ atom_ids。
- package：`package.atoms == chunk.atom_ids`；`expected_objects ⊆ objects`；`expected_local_fields` 字段 ⊆ 对象 payload。
- 对象证据：local ⊆ home-chunk、supporting ⊄ home-chunk；对象 ∈ home_package.expected_objects。
- 关系端点 ∈ objects；mention.atom_id ∈ atoms；do_not_extract.atom_id ∈ atoms。
- 标签：objects/relations 无 `confidence`，均有 gold_label/difficulty/evidence_strength。
- GLOBAL no-orphan：每个 atom 都是某 object/relation 的证据（本章无 context_only）。
- 数值越界审计：非公式 atom 的 normalized 数字（5.7/9.9/568/993/106/99/46/43/40/100/20/25/75/80/
  1.7248/1.7109/0.0139/3/100B/2.58/1.0/13 等）全部出现在其 raw span 内。
- ASCII 安全：rho/P_tot/P_act/P_sparse、`~=`、`->`、`x`(乘)、Δ 写作 delta；`:=` 替 `\triangleq`；U+2013→`-`。
