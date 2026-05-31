# Engram paper **Chapter 2 Architecture** 在 qiefen 方案下的结构化抽取（v0.3.3）

> **v0.3.3（第五轮 review，微调）**：`IMPL-DECOUPLED-SCALING` 的 local evidence 补 `A-SYS-INFER`
> （payload 含 inference/host memory）；`do_not_extract` 的 `inline_author_year_citation` 规则补上 `examples` 列表；
> 4 个跨 package 对象的 `expected_local_fields` 已全覆盖；RMSNorm 维持在 `FORM-EQ4`（gating 经 `R-12 component_defined_by_formula` 可达，故对象本体不重复挂 `A-GATE-RMSNORM`）。详见 §13。

> **v0.3.2（第四轮 review）**：对象证据拆成 `local_evidence_atom_ids`（在对象 `home_package` 的 chunk 内）
> 与 `supporting_context_atom_ids`（来自其它 chunk），`expected_objects` 只按 local 证据考核；
> 拆分 `A-GATE-RMSNORM` → `A-GATE-WKV-DEF`（W_K/W_V 定义，供 Eq.3）+ `A-GATE-RMSNORM`（RMSNorm/梯度稳定，供 Eq.4）；
> `do_not_extract` 补 `(Xie et al., 2025)`、两条 Figure 2 子图 label、以及 inline-citation policy；
> `R-07 += A-MB-BACKBONE`、`R-16 += A-FIG2`、`R-05 += A-OV-ROADMAP/A-GATE-OUT`；
> package 增加 `expected_local_fields`。详见 §12。


> **v0.3.1（第三轮 review）**：权威坐标定为 `atom.source_span`（file = `engram_paper_mineru.md`）；
> `atom.viewer_span`（file = `source.md`）标 `viewer_only: true`，仅供人工 review / UI 跳转，可选。
> evaluator 必须只用 `source_span` 校验 `source_file[char] == raw_text`。
> 同时修正 `normalized_text` 的跨 span 补全：`A-OV-PHASES` 去掉 "(Section 2.2)/(Section 2.3)"；
> `A-TC-REDUCTION` 用 "This process"；把插入的主语（Engram/Context-aware gating）改回 "we"/被动；
> 并按 review 方案 B 新增 `A-OV-ROADMAP`（承载 Section 2.2/2.3 的阶段说明，与 `A-OV-PHASES`
> 一起支撑 retrieval→2.2 / fusion→2.3）。`raw_vs_normalized` convention 增加"仅公式 atom 可经
> `supported_by_context_atoms` 携带符号解释"的例外条款。


源文件：`pdf_parser/engram_paper_mineru.md`（MinerU 输出）
范围：**仅第 2 章 Architecture（源文件 31–109 行）**，含 2.1–2.5 与 Figure 1/2/3 图题。
profile：`article_research`（论文型）

可加载 gold 见同目录 [`gold.yaml`](./gold.yaml)；原始段落见 [`source.md`](./source.md)（逐字切片，
本测例的自包含输入）。

> **v0.3 变更（按第二轮 code review）**：本章是当前唯一升级到 v0.3 schema 的样例，作为扩展范本。
> 关键改动：**双坐标系**（`orig_*` 指向 source_file、`slice_*` 指向 source.md，二者切片都等于 `raw_text`）；
> `raw_text` 现在能完整支撑 `normalized_text`（A-TC-NGRAM 补全 g_{t,n} 公式；新增 A-MH-CONCAT 支撑
> Eq.2 的 concat、新增 A-GATE-RMSNORM 支撑 Eq.3/Eq.4 的上下文）；context_package 与所属 chunk 的 atom
> **完全一致**并带 `expected_objects`；新增 Formula/ExperimentSetup/IntermediateRepresentation 对象。
> 完整对照见 §9、§10。

---

## 0. Profile 判定（§2）

命中 `Section 2.x` / `Figure 1/2/3` / `Eq. (1)–(6)` / 学术引用 `(Vaswani et al., 2017)` /
`Appendix C` / `ablation in Section 6.2` → `article_research`。
抽取目标（§2.1）：`ArticleMethod` / `ArchitectureComponent` / `MechanisticExplanation` /
`SystemDesignClaim` / `Limitation` / `Implication`。本章是核心方法章，不含 ExperimentResult/ScalingLaw
本体，**Figure 3 是对 §3 scaling laws 的前向引用**，按跨节处理（见 §1、§4）。

---

## 1. SourceElement（§3.1）— MinerU 在本章里的真实形态（坑）

gold 顶层新增 `source_elements`（31 个），显式记录 SourceElement 阶段：heading / paragraph /
formula / figure_caption，各带 `line_start/line_end`。要点与坑：

- **公式**：`$$ ... \tag{N} $$`，全章**连续编号 1–6**（不同于教材 cmos 章按小节重置）；
  atom 仍以 `section_path + tag` 限定。`raw_text` 保留原始 LaTeX（如 `A-GATE-EQ4` 的
  `\alpha_ {t} = \sigma \left(\frac {\mathrm{RMSNorm}...`），`normalized_text` 给 ascii 可读式。
- **run-in 子标题**：`Tokenizer Compression` 与 `Multi-Head Hashing` 是 2.2 段首的内联加粗式
  小标题（无 `#`），建为 `SEC-2.2-TC` / `SEC-2.2-MH`（`kind: run_in_heading`）。
- **图**：Figure 1/2/3 渲染为 `Figure N | ...` 纯文字段，**无 `![](images/...)`**。
  Figure 2 前还出现孤立的 `(a) Engram at training` / `(b) Engram at inference`（子图标签）。
- **跨页断行**：vocabulary projection 一句在行 41 末断开（"canonical"），行 43 续写；
  `A-TC-PROJ` 的 `raw_text` 因此 **横跨 41→43**（`source_line_start: 41, source_line_end: 43`），
  其 `raw_text` 真实包含中间空行——这是验证"跨页 span 还原"的好测例。
- **特殊符号**：`Apple vs. ⊔apple` 的空格符被渲染成 `$\sqcup$`；Eq.2 的连乘符 `∏` 语义其实是
  "concat 所有检索到的 embedding"（atom note 标注）。

---

## 2. Section Tree（§3.2）

```
2 Architecture
  2.1 Overview
  2.2 Sparse Retrieval via Hashed N-grams
      Tokenizer Compression      (run-in heading -> SEC-2.2-TC)
      Multi-Head Hashing         (run-in heading -> SEC-2.2-MH)
  2.3 Context-aware Gating
  2.4 Integration with Multi-branch Architecture
  2.5 System Efficiency: Decoupling Compute and Memory
  (cross-ref) SEC-XREF-SCALING   -> Figure 3，前向引用 Section 3
```

---

## 3. EvidenceAtom（§3.3）— 共 39 atoms，**raw_text + normalized_text + 权威/视图双坐标**

（v0.3.2 把 `A-GATE-RMSNORM` 拆为 `A-GATE-WKV-DEF` + `A-GATE-RMSNORM`，故 38→39。）


每个 atom 带：`raw_text`（逐字 span）、`normalized_text`（ascii 可读，**只渲染该 span 内的信息**）、
`source_element_id`、`evidence_strength`，以及两个坐标块：
- `source_span`（**权威**）：`{file: engram_paper_mineru.md, line_start/end, char_start/end}` —— 评测唯一依据。
- `viewer_span`（**可选/调试**）：`{file: source.md, line_start/end, char_start/end, viewer_only: true}` ——
  仅供人工 review / UI 跳转，source.md 重排不影响 gold 正确性。

硬不变量（脚本校验，必跑）：`source_file[source_span.char_*] == raw_text`；
可选调试校验：`source.md[viewer_span.char_*] == raw_text`。

**raw/normalized 一致性原则**：`normalized_text` 不得含 span 外的信息；若公式的解释依赖邻句，
用 `metadata.supported_by_context_atoms` / `interpretation_supported_by` 指向支撑 atom。例如
`A-MH-EQ2`（Eq.2）的 concat 读法由 `A-MH-CONCAT`（"...by concatenating all retrieved embeddings"）支撑；
`A-GATE-EQ3/EQ4` 的"W_K/W_V 可学习""RMSNorm 为梯度稳定""alpha_t∈(0,1)"由 `A-GATE-RMSNORM` 支撑。
跨章证据用 `requires_external_evidence`（如 `A-SYS-PLACEMENT` 标注证据在 Section 6.2，不在本章）。

分布（按小节）：

- **2.1 Overview**：方法定义 `A-OV-DEF`、两相位 retrieval/fusion `A-OV-PHASES`。
- **2.2 Sparse Retrieval**：相位引子 `A-RETR-INTRO`；Tokenizer Compression（`A-TC-MOTIV/PROJ/REDUCTION/NGRAM`）；
  Multi-Head Hashing（`A-MH-MOTIV/HEADS/HASHFN` + Eq.1 `A-MH-EQ1`、Eq.2 `A-MH-EQ2`）。
- **2.3 Context-aware Gating**：`A-GATE-PRIOR`、risk `A-GATE-RISK`、QKV `A-GATE-QKV`、
  Eq.3/4/5、`v~_t` `A-GATE-OUT`、抑噪机制 `A-GATE-MECH`、conv `A-GATE-CONV`、集成 `A-GATE-RESID`。
- **Figure 2**：`A-FIG2`，`figure_caption_atom`，`evidence_strength: cross_reference`（见 §4 归属）。
- **2.4 Multi-branch**：`A-MB-BACKBONE/SHARE/FUSE`、Eq.6 `A-MB-EQ6`、mHC 设置 `A-MB-MHC`。
- **Figure 3**：`A-FIG3`，`cross_section_figure_caption_atom`（前向引用 §3）。
- **2.5 System Efficiency**：`A-SYS-DECOUPLE/DETERM/TRAIN/INFER/PLACEMENT/ZIPF`。

---

## 4. SemanticChunk（§4.2）+ **图的物理 vs 语义归属**（review 问题 3/4）

| chunk | chunk_type | section_path | 说明 |
| --- | --- | --- | --- |
| `C-OV` | `article_core_claim_block` | 2 > 2.1 | 方法定义 + 两相位路线图 |
| `C-RETRIEVAL` | `architecture_component_block` | 2 > 2.2 | 相位引子 + tokenizer 压缩 + multi-head hashing，Eq.1/2 与说明同块 |
| `C-GATING` | `architecture_component_block` | 2 > 2.3 | risk→QKV(Eq.3)→gate(Eq.4)→v~→机制→conv(Eq.5)→集成。**不含 Figure 2** |
| `C-MULTIBRANCH` | `architecture_component_block` | 2 > 2.4 | 参数共享 + branch gate(Eq.6) + FP8 fuse + mHC(M=4)。**不含 Figure 3** |
| `C-SYSEFF` | `system_efficiency_block` | 2 > 2.5 | decouple→训练 All-to-All→推理 prefetch→placement→Zipfian 缓存 + **Figure 2** |
| `C-XREF` | `cross_reference_block` | (cross-ref) §3 | 仅含 `A-FIG3`，`core: false`，held out of 核心抽取 |

**Figure 2**（system implementation）：`physical_section_id: SEC-2.3`，
`semantic_section_ids: [SEC-2.5]`，`linked_objects: [SYSDESIGN-DETERMINISTIC-PREFETCH]`。
虽物理上印在 2.3 后，但内容（训练 All-to-All / 推理 host-memory prefetch）属 2.5，
故挂在 `C-SYSEFF` 而非 `C-GATING`，避免把 gating 块语义撑宽。

**Figure 3**（Sparsity allocation & scaling）：`physical_position_after: SEC-2.4`，
`semantic_owner: Section 3`，`include_in_chapter2_core_chunks: false`，单独隔离在 `C-XREF`，
并在 `do_not_extract` 标注——**不污染 Multi-branch 块**。

> anchor expansion（§5.2）：`C-GATING` 把 `Eq.3→Eq.4→v~_t→Eq.5` 公式链连同抑噪机制收进同块；
> `C-SYSEFF` 把一条系统论证的五个子论点保持完整不切。

---

## 5. ContextPackage（§6）— 6 个 package，**atoms == chunk.atom_ids 且带 expected_objects**

每个核心 chunk 配 package，且 **package.atoms 与所属 chunk 的 atom_ids 完全一致**（v0.2 的
`PKG-GATING` 漏了 A-GATE-PRIOR/CONV/RESID，导致 gold 对象的证据无法从输入复现——v0.3 已补齐）。
package 是 LLM 的实际输入；新增 `expected_objects` 列出该输入应当抽出的对象 id，用于 object-recall 评测。
另加 `PKG-XREF`（仅含 A-FIG3，`expected_classification: forward_reference`，`expected_objects: []`），
用于测试"前向引用应被路由到 Section 3 而非在本章抽成组件"。

---

## 6. Mention（§7.1）— 35 条，覆盖组件级 + **变量级 + 系统级**（review 问题 6）

除高层组件外，新增变量级实体（surjective function P、canonical ID x'_t、embedding table E_{n,k}、
hash index z_{t,n,k}、Key/Value projection W_K/W_V、scalar gate alpha_t、RMSNorm、retrieved embedding e_t）
与系统级实体（All-to-All、host memory、PCIe、GPU HBM、NVMe SSD、Zipfian distribution、MoE runtime routing、
Transformer backbone、static pattern storage、dynamic computation、residual connection）。

---

## 7. Object（§7.2）— 21 个，引入 **Sparse Retrieval / Fusion 父级相位** + Formula/ExperimentSetup/IntermediateRepresentation

v0.3 在 v0.2 的 13 个对象基础上新增 8 个：6 个 `Formula`（FORM-EQ1…EQ6，让"Eq.4 与 Eq.6 什么关系"
这类问题可经公式对象回答）、1 个 `ExperimentSetup`（`EXPSETUP-MHC-M4`，把 mHC(M=4) 从组件 payload 提为
独立对象，对齐 `PKG-MULTIBRANCH` 的 ExperimentSetup target）、1 个 `IntermediateRepresentation`
（`INTERMEDIATE-RETRIEVED-MEMORY` = `e_t`，由 Sparse Retrieval 产出、被 Gating 消费，取代抽象的
"component_consumes_output_of"）。父级相位结构仍是：

不再把所有组件直接挂到 `METHOD-ENGRAM`，而是建两层架构：

```
METHOD-ENGRAM
  ├─ has_phase → COMPONENT-SPARSE-RETRIEVAL
  │     ├─ has_component → COMPONENT-TOKENIZER-COMPRESSION
  │     └─ has_component → COMPONENT-MULTI-HEAD-HASHING
  ├─ has_phase → COMPONENT-FUSION
  │     ├─ has_component → COMPONENT-CONTEXT-AWARE-GATING
  │     └─ has_component → COMPONENT-DEPTHWISE-CAUSAL-CONV
  └─ has_component → COMPONENT-MULTI-BRANCH-INTEGRATION   (跨分支集成策略，非相位)
```

其余对象：`RISK-HASH-COLLISION-POLYSEMY`、`MECH-GATE-SUPPRESSES-NOISE`、
`SYSDESIGN-DETERMINISTIC-PREFETCH`、`IMPL-DECOUPLED-SCALING`（新增 Implication）、
`LIMIT-PLACEMENT-COASESIGN`。每个对象用 `gold_label: true` + `difficulty`（easy/medium/hard）
+ `evidence_strength`（direct/indirect/cross_reference）**取代小数 confidence**（review 问题 9）。

---

## 8. Relation（§8）— 18 条

| id | relation_type | source → target |
| --- | --- | --- |
| R-01/02 | `method_has_phase` | Engram → Sparse Retrieval / Fusion |
| R-03/04 | `component_has_component` | Sparse Retrieval → Tokenizer Compression / Multi-Head Hashing |
| R-05/06 | `component_has_component` | Fusion → Context-aware Gating / Depthwise Causal Conv |
| R-07 | `method_has_component` | Engram → Multi-branch Integration |
| R-08 | `component_produces_representation` | Sparse Retrieval → 中间表示 `e_t` |
| R-09 | `component_consumes_representation` | Context-aware Gating → 中间表示 `e_t` |
| R-10 | `component_mitigates_risk` | Context-aware Gating → hash-collision/polysemy noise |
| R-11 | `component_has_mechanism` | Context-aware Gating → Gate-suppresses-noise |
| R-12 | `component_defined_by_formula` | Context-aware Gating → FORM-EQ4 |
| R-13 | `component_adapts_component` | Multi-branch Integration → Context-aware Gating（Eq.6 是 Eq.4 的分支版） |
| R-14 | `formula_generalizes_formula` | FORM-EQ6 → FORM-EQ4 |
| R-15 | `component_has_default_setting` | Multi-branch Integration → EXPSETUP-MHC-M4 |
| R-16 | `method_has_system_design` | Engram → deterministic prefetch |
| R-17 | `system_design_enables_efficiency` | deterministic prefetch → decoupled scaling（证据扩为 decouple/determ/infer/zipf） |
| R-18 | `system_design_has_tradeoff` | deterministic prefetch → placement co-design 限制 |

第一轮 review 的三处关系修正（保留）：`component_has_mechanism`（R-11）、gating 消费检索输出
（v0.3 经由中间表示对象 R-08/R-09，取代抽象的 `component_consumes_output_of`）、
系统设计方向拆分（`method_has_system_design` R-16 + `system_design_enables_efficiency` R-17）。
第二轮新增：`component_defined_by_formula`（R-12）、`formula_generalizes_formula`（R-14，Eq.6↔Eq.4）、
`component_has_default_setting`（R-15，→ ExperimentSetup）；R-17 证据扩充为四条
（A-SYS-DECOUPLE/DETERM/INFER/ZIPF）。

---

## 9. v0.2 与 v0.1 的差异（对照 code review 十条）

| # | review 问题 | v0.2 处理 |
| --- | --- | --- |
| 1 | EvidenceAtom 非原文 | 每个 atom 加 `raw_text`（逐字）+ `normalized_text`（ascii） |
| 2 | 缺 line/char span / source_element_id | 加 `source_line_start/end`、`char_start/char_end`（源文件绝对偏移）、`source_element_id`；新增顶层 `source_elements` |
| 3 | Figure 3 误入 C-MULTIBRANCH | 改 `cross_section_figure_caption_atom`，`semantic_owner: §3`，隔离进 `C-XREF`，列入 `do_not_extract` |
| 4 | Figure 2 只归 C-GATING | 加 `physical_section_id` vs `semantic_section_ids`，移入 `C-SYSEFF`，`linked_objects` 指向系统设计对象 |
| 5 | context_packages 不全 | 补到 5 个，覆盖全部核心 chunk（含 `PKG-RETRIEVAL`） |
| 6 | mention 太少 | 13 → 35，含变量级与系统级实体 |
| 7 | 缺 Sparse Retrieval / Fusion 父级 | 新增两个相位对象 + `method_has_phase` / `component_has_component` 两层结构 |
| 8 | relation 类型不准 | R-08/R-10/R-12/R-13 重定向与拆分（见 §8） |
| 9 | confidence 写成小数 | 改 `gold_label` + `difficulty` + `evidence_strength` |
| 10 | 缺负样本 | 新增 `do_not_extract`（Figure 3 前向引用、若干 inline citation、out-of-slice 的 Figure 1） |

> （以上为 v0.2 相对 v0.1 的改动，保留作历史。）

---

## 10. v0.3 与 v0.2 的差异（对照第二轮 code review 的 P0/P1）

| 项 | review 要点 | v0.3 处理 |
| --- | --- | --- |
| P0-1 | span 坐标不自洽（char 指 original，但 source.md 是另一坐标系） | 双坐标：`orig_*`（source_file）+ `slice_*`（source.md），二者切片都 == `raw_text`；`slice_line = orig_line − 30`；source.md 重生成为纯逐字切片 |
| P0-2 | `A-TC-NGRAM` raw 截断、撑不起 normalized | raw 延伸到完整 `g_{t,n} = (...)` 公式 |
| P0-3 | Eq.2 concat 解释缺原文 atom | 新增 `A-MH-CONCAT`（"...by concatenating all retrieved embeddings"），`A-MH-EQ2.metadata.interpretation_supported_by: [A-MH-CONCAT]` |
| P0-4 | Eq.4 normalized/metadata 引入公式外信息 | 新增 `A-GATE-RMSNORM`（承载 W_K/W_V 可学习、RMSNorm 梯度稳定、alpha_t∈(0,1)）；`A-GATE-EQ3/EQ4` 改为纯公式 + `supported_by_context_atoms` |
| P0-5 | `PKG-GATING` 缺 atom | 所有 package 的 atoms == 所属 chunk 的 atom_ids（PKG-GATING 补回 PRIOR/CONV/RESID + 新增 RMSNORM） |
| P0-6 | `PKG-MULTIBRANCH` 声明抽 ExperimentSetup 但无对象 | 采纳方案 B：新增 `EXPSETUP-MHC-M4` + 关系 `component_has_default_setting` |
| P0-7 | `A-SYS-PLACEMENT` 证据强度 | 加 `requires_external_evidence: true` + `external_evidence_ref: Section 6.2`（对象 `LIMIT-PLACEMENT-COASESIGN` 同步标注） |
| P0-8 | `R-13` 证据不贴合 | `R-17`（原 R-13）证据扩为 A-SYS-DECOUPLE/DETERM/INFER/ZIPF |
| P1-1 | 句子片段 atom 用了 *_sentence | 把片段补成完整句子（A-TC-MOTIV 含 "While..."、A-GATE-RISK 改为完整句、A-GATE-PRIOR 仅首句、A-GATE-QKV 含 "Specifically,"） |
| P1-2 | 缺 Formula 对象 | 新增 FORM-EQ1…EQ6 + `formula_generalizes_formula`（Eq.6→Eq.4）、`component_defined_by_formula` |
| P1-3 | 缺中间表示对象 | 新增 `INTERMEDIATE-RETRIEVED-MEMORY`（produced_by / consumed_by）+ 关系 R-08/R-09 |
| P1-4 | C-XREF 缺 package | 新增 `PKG-XREF`（`expected_classification: forward_reference`） |
| P1-5 | package 缺 expected_objects | 每个 package 增加 `expected_objects`（object-recall 评测用） |

> （以上为 v0.3 相对 v0.2 的改动，保留作历史。）

---

## 11. v0.3.1 与 v0.3 的差异（对照第三轮 code review）

| review 要点 | v0.3.1 处理 |
| --- | --- |
| 主坐标应绑定原始文件，`source.md` 只做 human-readable view | 坐标拆成 `source_span`（权威，file=source_file）+ `viewer_span`（`viewer_only:true`，file=source.md）；`source_meta.conventions.coordinate_policy` 与 `validation` 明确"必跑 source_span、可选 viewer_span"；source.md 重生成为干净 viewer slice（头部注释声明 viewer-only） |
| §4.1 `A-OV-PHASES.normalized` 带 "(Section 2.2/2.3)"（出 span） | 去掉 section 引用；按方案 B 新增 `A-OV-ROADMAP`（raw = "First, as detailed in Section 2.2 ... system-level design in Section 2.5."），与 `A-OV-PHASES` 一起被 METHOD-ENGRAM / SPARSE-RETRIEVAL / FUSION 及 R-01/R-02 引用，支撑 retrieval→2.2、fusion→2.3 |
| §4.2 `A-TC-REDUCTION.normalized` 用了出 span 的 "vocabulary projection" | 改回 "This process achieves a 23% reduction ..."（与 raw 一致） |
| §4.3 normalized/formula 规则需更精确 | `raw_vs_normalized` 改为："normalized 只渲染 span 内信息、可改写/转写/消解 span 内代词，但不得引入 span 外的事实/名称/章节引用；**例外**：formula_atom 可经 `supported_by_context_atoms`/`interpretation_supported_by` 携带符号解释" |
| 一致性：其余 normalized 不应插入 span 外主语 | `A-TC-MOTIV`/`A-MH-MOTIV` 的 "Engram" 改回 "we"；`A-GATE-QKV`/`A-SYS-INFER` 改被动；`A-MB-SHARE` 去掉 "For the multi-branch framework"；`A-OV-ROADMAP.normalized` 不再用出 span 的 "fusion/Retrieval" 标签（阶段名由 `A-OV-PHASES` 提供） |
| §4.4 `A-SYS-PLACEMENT` external evidence | 保留 `requires_external_evidence:true` + `external_evidence_ref: Section 6.2` |

> v0.3.1 校验（脚本）：权威 `source_file[source_span.char_*] == raw_text`（全 38 atom 通过）、
> 可选 `source.md[viewer_span.char_*] == raw_text`（全通过）、`viewer_span.viewer_only==true`、引用完整性、
> `package.atoms == chunk.atom_ids`、`expected_objects`/metadata 交叉引用可解析、无残留小数 confidence、
> normalized 跨 span 审计（formula 例外后）无真实越界 —— 全部通过。

> （以上为 v0.3.1 相对 v0.3 的改动，保留作历史。）

---

## 12. v0.3.2 与 v0.3.1 的差异（对照第四轮 code review）

| review 要点 | v0.3.2 处理 |
| --- | --- |
| §4.1 `expected_objects` 与 object evidence 跨 package 不一致 | 采纳方案 B：每个 object 拆 `local_evidence_atom_ids`（在其 `home_package` 的 chunk 内）+ `supporting_context_atom_ids`（来自其它 chunk）+ `home_package`；`object_evidence` convention 说明"object 全证据=并集，package object-recall 只按 local 考核"。脚本新不变量：local 原子∈home chunk、supporting 原子∉home chunk。受影响的 4 个跨 package 对象：METHOD-ENGRAM（supporting A-GATE-RESID）、SPARSE-RETRIEVAL / FUSION（supporting A-OV-PHASES/A-OV-ROADMAP）、INTERMEDIATE-RETRIEVED-MEMORY（supporting A-GATE-PRIOR） |
| §4.2 `FORM-EQ3` 证据混杂（RMSNorm 属 Eq.4） | 拆 `A-GATE-RMSNORM` → `A-GATE-WKV-DEF`（"W_K, W_V are learnable projection matrices"，供 `FORM-EQ3`）+ `A-GATE-RMSNORM`（"To ensure gradient stability...RMSNorm...alpha_t∈(0,1)"，供 `FORM-EQ4`）；`A-GATE-EQ3.supported_by_context_atoms=[A-GATE-WKV-DEF]`；M-17/M-18（W_K/W_V）改指 `A-GATE-WKV-DEF` |
| §4.3 do_not_extract 补负例 | 补 `(Xie et al., 2025)`（A-MB-MHC）、两条子图 label `(a) Engram at training` / `(b) Engram at inference`（orig 81/83）、以及 `pattern: inline_author_year_citation` 的 citation_policy |
| §4.4 `R-07` 证据偏弱 | `R-07 += A-MB-BACKBONE`（multi-branch 是 default backbone） |
| §4.5 `R-16` 可加图证据 | `R-16 += A-FIG2`（Figure 2 = system implementation） |
| §4.6 `R-05` 证据偏窄 | `R-05 += A-OV-ROADMAP, A-GATE-OUT`（fusion→2.3 + gated output） |
| P1.4 expected_objects 字段级 | package 增加 `expected_local_fields`：PKG-OV→METHOD-ENGRAM=[name,summary,phases]；PKG-RETRIEVAL→SPARSE-RETRIEVAL=[name,description,children]、INTERMEDIATE-RETRIEVED-MEMORY=[name,description,produced_by]；PKG-GATING→FUSION=[name,description,children]（其余字段需 supporting_context 才能补全） |

> v0.3.2 校验（脚本）：权威 `source_file[source_span] == raw_text`（全 39 atom）、可选 viewer_span、引用完整性、
> `package.atoms == chunk.atom_ids`、**object.local 原子∈home chunk 且 supporting 原子∉home chunk**、
> `expected_local_fields` 字段∈对象 payload、do_not_extract 引用可解析、无残留小数 confidence —— 全部通过。

> （以上为 v0.3.2 相对 v0.3.1 的改动，保留作历史。）

---

## 13. v0.3.3 与 v0.3.2 的差异（对照第五轮 review）

| review 要点 | v0.3.3 处理 |
| --- | --- |
| §3.1 `IMPL-DECOUPLED-SCALING` 缺 `A-SYS-INFER` | local_evidence 改为 `[A-SYS-DECOUPLE, A-SYS-TRAIN, A-SYS-INFER, A-SYS-ZIPF]`（payload 提到 inference / host memory；A-SYS-INFER 在 home chunk C-SYSEFF 内，不破坏不变量；与 R-17 证据一致） |
| §3.2 `COMPONENT-CONTEXT-AWARE-GATING` 是否加 `A-GATE-RMSNORM` | 选择"RMSNorm 只留给 `FORM-EQ4-SCALAR-GATE`"：对象 payload 未显式写 RMSNorm，故不加该 atom（保持字段↔证据一致）；需要 RMSNorm 细节时经 `R-12 component_defined_by_formula → FORM-EQ4` 可达 |
| §3.3 `expected_local_fields` 全覆盖 | 已对全部 4 个跨 package 对象覆盖：PKG-OV→METHOD-ENGRAM、PKG-RETRIEVAL→{SPARSE-RETRIEVAL, INTERMEDIATE-RETRIEVED-MEMORY}、PKG-GATING→FUSION（v0.3.2 即完成，v0.3.3 脚本校验"无跨 package 对象缺 ELF"） |
| §3.4 inline citation 规则化 | `do_not_extract` 的 `inline_author_year_citation` 增加 `examples: [(Vaswani et al., 2017), (He et al., 2016), (Haber and Poesio, 2024), (Dehghani et al., 2023), (Xie et al., 2025)]`，便于 evaluator 统一归类 citation over-extraction |

> v0.3.3 校验（脚本）：上述全部 v0.3.2 不变量 + `IMPL-DECOUPLED-SCALING` local 含 A-SYS-INFER +
> citation policy 带 examples + 4 个跨 package 对象 ELF 全覆盖 —— 全部通过。
