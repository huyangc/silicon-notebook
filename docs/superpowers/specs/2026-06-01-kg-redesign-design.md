# 知识图谱中心的解析+抽取 重设计 — 设计文档

- 日期：2026-06-01
- 状态：已通过 brainstorming 四部分评审，待写实现计划
- 背景：旧设计（atom→chunk→object→relation 四级 + 20 类无定义细分原子类型）在"以细类型对齐 gold"上有结构性问题——细分类型在规范和代码里都**没有可操作定义**，导致类型不可复现、原子-对象互相 tradeoff、复合分卡顿。本设计以**知识图谱**为核心重做，并用**带可操作定义的生成契约**从根上治"无定义"。

## 0. 设计原则（治本）

1. **节点类型少且边界清晰**：价值放在"边 + 证据 + 规范化"，而非细分节点类型。
2. **可操作定义先行**：每个节点/边类型都有判定规则（见 §4 契约），人和模型都按同一标准。
3. **一次图谱抽取**：砍掉中间层级联，切窗 → 一次出图谱片段 → 合并规范化。
4. **证据 grounding 是硬不变量**：每个节点/边挂字符级原文 span（`source[span]==raw_text`），供引用+溯源。
5. **防循环作弊**：gold 由独立于产品流水线的过程生成 + 人工裁决；产品流水线不为背 gold 调参，沿用 held-out / 交叉验证纪律。

---

## 1. 文档类型模型

### 1.1 类型 = 自包含策略注册表
`DocumentType` 是一个注册表条目，把"是什么文档"和"怎么处理它"绑定：
```
DocumentType:
  id:            "academic" | "textbook"     # 当前仅 2 种；注册表可扩展
  label:         学术论文 / 教材课本
  detection:     启发式判定线索
  kg_schema:     §2 该类型的节点/边侧重
  segmentation:  §3 切分策略(N/M 超参)
  extraction:    §3 抽取 prompt/方法
```
`Source.doc_type` 指向某个 id；该 id 决定下游全部策略（不再有散落的 `if profile==...`）。

### 1.2 自动检测（启发式，当前）
- 学术类线索：`abstract / arXiv / we propose / references / et al. / 公式密集 + 实验表`
- 教材类线索：`Chapter / 第N章 / N.N 小节 / Exercise / Problems / definition / example`
- 输出 `(type, confidence)`，confidence 低时标"待确认"。不够再升 LLM 分类。

### 1.3 上传流程（类型在抽取前定稿，无重抽逻辑）
```
1) 用户选择文档
2) 自动判定类型
3) 界面显示默认类型
4) (可选) 用户更改类型
5) 按最终类型进行切分+抽取
```
类型在第 5 步前就定稿，因此**不需要"改类型→重抽"**的逻辑。

---

## 2. 知识图谱 schema

### 2.1 节点类型（4 种，两类文档共享，边界清晰）
> **属性(attrs)暂不建模**：每个节点当前只有 `id/type/name(节点文本)/section_path/evidence/mentions`，节点文本统一放在 `name`。富属性的形态未定，见 fangan_todo.md「KG 重构」。

| 节点 | 是什么 | name 放什么 |
| --- | --- | --- |
| **Concept** | 命名实体：术语/概念/方法/组件/器件/系统/材料（可跨文档合并） | 实体名 |
| **Claim** | 关于 Concept 的可判真伪断言：主张/结论/原理/机制/定义陈述 | 完整断言陈述 |
| **Formula** | 公式/方程 | 表达式 |
| **Procedure** | 有序过程：工艺流程/例题解法/推导链 | 过程名 |

### 2.2 每类文档的抽取侧重
| | 学术类(engram) | 教材类(cmos) |
| --- | --- | --- |
| Concept | 方法、架构组件、系统 | 概念、器件、工艺、材料 |
| Claim | 核心主张、实验结论、机制、局限 | 定义陈述、设计原则、物理效应、结果 |
| Formula | scaling law、定义式 | 大量推导公式(带 role) |
| Procedure | 实验流程 | 工艺流程、例题解法、推导链 |

### 2.3 边类型（按 4 个检索场景分组）
| 场景 | 边 |
| --- | --- |
| 概念探索 | `defines`(Claim→Concept)、`part_of`/`composed_of`(Concept→Concept)、`contrasts_with`、`kind_of` |
| 问答 | `about`(Claim/Formula→Concept)、`supports`(Claim/Formula→Claim) |
| 推导/前置 | `derived_from`(Formula→Formula)、`depends_on`/`prerequisite_of`(Concept→Concept, Formula→Concept)、`used_in`(Formula→Procedure)、`precedes`(Procedure 步序) |
| 跨文档 | Concept 规范化合并(canonical)；节点 `mentions:[span]` 记跨文档出处 |

### 2.4 证据 + 规范化
- **每个节点和每条边挂 `evidence:[source_span]`**（字符级，硬不变量）。
- **Concept 规范化**：同一概念跨小节/文档按规范化 `name` 合并成一个 canonical 节点，`mentions` 记所有出处 → 跨文档 + 概念探索枢纽。（aliases / embedding 合并待属性建模后再加，见 fangan_todo.md。）
- 节点 `name` 做 embedding，供 Q&A 向量召回。

### 2.5 检索用图谱的哪些信息
1. **Q&A**：query 向量召回 Concept/Claim/Formula 节点 → 沿 `about`/`supports` 扩 1 跳 → 用节点 payload 生成答案 + 引用 evidence span。
2. **概念探索**：Concept → 展开 `defines`/`part_of`/`contrasts_with`/`depends_on` 邻居 + 挂载的 Claim/Formula。
3. **跨文档**：canonical Concept + `mentions` 显示同概念在论文+教材的出处。
4. **推导/前置**：沿 `derived_from` / `depends_on` 走有序路径。

---

## 3. 切分 + 知识图谱抽取方法

### 3.1 流水线（5 步，砍掉中间层级联）
```
1. 解析 → SourceElement(字符偏移)          [保留：span 不变量]
2. 章节树 → 结构骨架                         [保留：数字链路径]
3. 切分成抽取窗口(非细原子)                   [简化]
4. 逐窗口 KG 抽取(LLM, 一次出 节点+边+证据)   [新核心]
5. 合并 + Concept 规范化 → 文档级知识图谱      [新]
```

### 3.2 切分 = 抽取窗口
- 按章节切，节内按 **N 字 / M 重叠**开窗（超参，默认 N=9000, M=5%N）。
- 公式三行块 `$$/latex/$$` 按 S1 修复正确成段（text=内层 latex，对齐证据）。
- 切分只保证：窗口连续(证据可定位)、大小可控；**不预判类型、不预切句**。

### 3.3 逐窗口 KG 抽取（核心）
一次 LLM 调用：输入 窗口原文 + 章节路径 + 文档类型；输出图谱片段：
```json
{"nodes":[{"id":"n1","type":"Concept","name":"<实体名>","evidence":"<逐字引用>"},
          {"id":"n2","type":"Formula","name":"<表达式>","evidence":"<逐字>"}],
 "edges":[{"type":"about","source":"n2","target":"n1","evidence":"<逐字>"}]}
```
（属性 attrs 暂不抽，节点文本统一进 `name`，见 §2.1 与 fangan_todo.md。）
- 节点类型 4 选 1（无歧义）；证据**逐字引用 → 定位回原文 span**（exact→空白容错，定位不到丢弃；技术已验证 0 违例）。
- 节点 id 窗口内局部，第 5 步合并分配全局 id。

### 3.4 合并 + 规范化
- 跨窗口/章节/文档，Concept 按 name+aliases+embedding 合并；Claim/Formula/Procedure 经 `about`/`defines` 边重指向 canonical 全局 id。
- 产出文档级 KG，可与其它文档 KG 再合并（跨文档检索）。

---

## 4. golden 生成方案（含治本契约 + 图匹配评测）

### 4.1 生成契约（带可操作定义 —— 体系可复现的根，需 review）
只有 4 个粗节点，这次写得清判据：
```
Concept   = 被命名的名词性实体。判据：能做主语/宾语、跨句可复现。
Claim     = 关于 Concept 的可判真伪断言。判据：有谓语、陈述一个事实。
Formula   = 方程/表达式。判据：含 = 或数学算子。
Procedure = 有序过程(≥2 有序步骤)。
每类边：给 源类型→目标类型 + 触发语义。
```

### 4.2 生成流水线
```
1. 每章用独立于产品流水线的生成器(强模型/精心 prompt) 按契约提议 KG
2. 自动校验: 证据 span 逐字成立、节点类型合法、边端点存在、Concept 初步合并
3. 人工策展(按契约): 修边界、合并/拆分、补漏边、去噪 —— 权威来源
4. 锁定为该章 gold KG
```
- 防循环：生成器独立于产品抽取流水线；产品流水线不为背 gold 调参。
- 旧 gold 仅作种子参考加速人工，不直接转换（避免带入旧细类型偏见）。

### 4.3 评测（语义图匹配，避开旧两坑）
| 指标 | 算法 |
| --- | --- |
| 节点 P/R | (类型一致[仅 4 类] + canonical-name 相似 + 证据区间重叠) 综合匹配，**不要求 span 精确相等** |
| 边 P/R | 端点映射到 gold 节点后，(端点对 + 边类型) 匹配 |
| 证据 grounding | 匹配节点/边的证据 span 必须逐字定位回原文 |
| 规范化质量 | 跨章/跨文档 Concept 合并正确性 |
| 检索效用(可选) | 4 个检索场景样例 query 端到端有用性 |

对边界宽容、类型仅 4 类 → 从根上消除旧的 span-精确 + 细类型-strict 两个雷。

### 4.4 测试用例
engram(学术) + cmos(教材)，按 §4.1 契约 + §4.2 流水线重做 gold；held-out/交叉验证纪律评测泛化；用户另用未见文档防作弊。

---

## 5. 与现有代码的关系
- 保留并复用：S1 解析(字符偏移 + 公式三行块修复)、S2 章节树、证据逐字定位技术、DeepSeek 客户端 + 并发。
- 重做：抽取层（atom/chunk/object/relation 四级 → 切窗+一次 KG 抽取+合并）、类型体系（20 细原子 → 4 粗节点 + 富边）、gold + harness（精确 span/细类型 strict → 语义图匹配）。
- 产品落地：`Source.doc_type` 复用；抽取产出从"typed objects"改为"KG(nodes/edges/evidence)"，知识浏览/检索改读图谱。

## 6. 非目标（YAGNI）
- 暂不支持第 3 种文档类型（注册表留位）。
- 暂不做审核反馈回流优化。
- 暂不替换非学术/教材文档的处理。
