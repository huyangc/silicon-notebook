# qiefen 抽取评分 Harness — 设计文档

- 日期：2026-05-31
- 状态：已通过设计评审，待写实现计划
- 相关规范：[`fangan/qiefen.md`](../../../fangan/qiefen.md)、[`fangan/article_research_gold_spec.md`](../../../fangan/article_research_gold_spec.md)、[`fangan/textbook_gold_spec.md`](../../../fangan/textbook_gold_spec.md)
- gold 数据：`fangan/testcases/{engram,cmos}/chXX_*/gold.yaml`（14 章，schema v0.3.3 / v0.3.3-textbook）

## 1. 问题与目标

我们已有 14 章 golden 抽取结果（`gold.yaml`，全流水线：`section_tree → evidence_atoms →
semantic_chunks → context_packages → objects → relations → mentions → do_not_extract`）。

现在要让 agent 生成「切分 + 抽取」的**代码**，该代码在同一 source 上运行并产出 `pred.yaml`
（与 `gold.yaml` **同 schema**）。Harness 的职责：

1. 把 `pred.yaml` 与 `gold.yaml` 逐 stage 对比；
2. 给出**每 stage 的 P/R/F1 + 一个 0–100 的加权总分**；
3. 产出**可操作的差异报告**（漏报 / 误报 / 类型错配 / span 偏移 / payload 缺失 /
   过抽取），用于驱动 agent 迭代改进。

**核心难点**：候选会使用自己的 ID，无法按 ID 对齐，必须**先按内容对齐，再评分**。

## 2. 设计决策（已评审确认）

| 决策点 | 选定方案 |
| --- | --- |
| 候选输出契约 | 与 `gold.yaml` **完全相同的 schema**（全流水线） |
| 对齐 / 评分引擎 | **混合**：确定性核心（span IoU / 集合 Jaccard / 类型 + 证据重叠）+ **可选** LLM 裁判（默认关闭） |
| 产出形态 | **JSON + Markdown 双产出** |
| 依赖 | Python 3 + **仅 `pyyaml`**（与现有 `build.py`/`validate.py` 一致，无 scipy/numpy） |
| 匹配算法 | 贪心最大分匹配 + 阈值；ties 按 id 序破，确定性可复现 |

## 3. 目录结构

```
fangan/testcases/harness/
  score.py        # CLI：评单章（gold 目录 + pred.yaml）-> report.json + report.md
  run_all.py      # 评候选树 vs 全 14 章 -> 聚合 JSON + leaderboard.md
  align.py        # 按内容的逐 stage 对齐（产出 gold↔pred id 映射）
  metrics.py      # P/R/F1、加权、聚合
  report.py       # JSON + Markdown 输出
  judge.py        # 可选 LLM 语义等价（--llm-judge 才启用；按内容 hash 缓存）
  config.py       # 阈值 + stage 权重（唯一调参入口）
  test_harness.py # 自检（gold-vs-gold == 100）+ 扰动测试
  README.md
```

## 4. 核心机制：一次对齐，下游复用

1. **先对齐 evidence_atoms**：pred→gold 原子按 `source_span` **字符区间 IoU**（同
   `source_file`/line）贪心最大匹配，IoU ≥ τ（默认 0.5）即配对。产出**原子对齐映射**
   `gold_atom_id ↔ pred_atom_id`。
2. 后续每个 stage 把 pred 中的 atom-id 引用**通过该映射**翻译到 gold-atom 空间，使
   chunks/objects/relations 在统一标识基底上比较——即便原始 ID 完全不同。

> `source_span` 是 spec 中的**权威坐标**；评测以 `source_file[source_span] == raw_text`
> 为准，`viewer_span` 仅供调试，不作为评测坐标。

## 5. 逐 stage 评分

| Stage | 匹配键 | 产出指标 |
| --- | --- | --- |
| **section_tree** | 节点 `path`（归一化） | 节点 P/R/F1（低权重） |
| **evidence_atoms** | `source_span` IoU | 原子 P/R/F1、平均 IoU（边界质量）、`atom_type` 准确率、`raw_text` 逐字校验、`normalized_text` 等价（串相等 / 裁判） |
| **semantic_chunks** | 映射后 atom 集合 Jaccard | chunk P/R/F1、`chunk_type` 准确率、**过切分 / 欠切分**计数 |
| **objects** | 类型 + 本地证据重叠 + payload 相似（贪心） | object P/R/F1、类型准确率、**payload 字段 P/R**、证据绑定 Jaccard（local）、`home_package` 正确性 |
| **context_packages** | 经 object 对齐 | 每包 `expected_objects` 召回 + `expected_local_fields` 覆盖（qiefen「Object Integrity」） |
| **relations** | 两端点均对齐到 gold object | relation P/R/F1、端点合法性、`relation_type` 准确率、证据重叠 |
| **mentions** | atom 映射 + 文本 | mention P/R、类型准确率（低权重） |
| **do_not_extract** | gold 禁取 span/text/pattern vs 任何覆盖它的 pred atom/object/mention | **过抽取违规数 / 抑制率**（负例控制） |

### 计算顺序
`atoms → chunks → objects → context_packages → relations → mentions → do_not_extract → section_tree`。
`context_packages` 依赖 object 对齐，故在 objects 之后计算。

### payload 字段 P/R 细则
对每个 gold object 的 payload 字段，检查匹配到的 pred object 是否有等价字段值：
- 默认：键归一化后值的**字符串归一化相等**；
- `--llm-judge` 开启时：值的**语义等价**由裁判判定（payload 多为改写文本）。
字段级 P/R/F1 计入 object 综合分。嵌套 dict/list 字段递归展平为 `path.to.field` 后逐项比较。

## 6. 加权总分（0–100，`config.py` 可调）

默认权重（体现 qiefen 重点，合计 1.0）：

| 维度 | 权重 |
| --- | --: |
| evidence_atoms | 0.20 |
| semantic_chunks | 0.15 |
| objects（存在性） | 0.12 |
| objects.payload 字段 | 0.13 |
| objects 证据绑定（local） | 0.10 |
| relations | 0.15 |
| context_packages | 0.05 |
| do_not_extract（负例控制） | 0.05 |
| section_tree + mentions | 0.05 |

加权 F1（及子分）求和后 ×100。权重集中在 `config.py`，便于按 profile 或实验调整。

## 7. 匹配算法

贪心最大分匹配 + 每 stage 阈值：构造 pred×gold 候选分矩阵，按分数降序贪心配对，分数低于
阈值的不配；ties 按 id 字典序破，保证确定性可复现。

**被否决的替代**：scipy 的最优二分匹配（Hungarian）——匹配略优，但引入重依赖且确定性需额外
处理；P/R/F1 评测用贪心是业界常规，足够。

## 8. LLM 裁判（可选，默认关闭 → 完全确定性、零密钥）

`--llm-judge` 仅对以下三处启用语义等价判定：
1. `normalized_text` 改写等价；
2. object payload 字段值等价；
3. 类型「相近但不完全相等」的部分给分。

调用封装在 `judge.py` 接口后，按内容 hash 缓存到本地 json。**默认关闭**，回退到确定性串归一化
相等，使 harness 可离线、可复现、无需任何密钥即可运行。

## 9. 产出（改进闭环）

- **`report.json`**：每 stage 全部指标 + 匹配/未匹配 id 列表 + `weighted_score`。供 CI /
  回归 / agent 自消费。
- **`report.md`**：标题分 → 每 stage 指标表 → 可操作分区：
  - **Missed（漏报 FN）**：gold id + `raw_text` 片段 + span；
  - **Spurious（误报 FP）**；
  - **Type mismatches（类型错配）**：pred 类型 vs gold 类型；
  - **Span boundary issues（边界偏移）**：低 IoU 配对；
  - **Payload-field gaps（字段缺失）**；
  - **do_not_extract violations（过抽取）**。
  这是喂回 agent 驱动下一轮迭代的内容。
- **`run_all.py`** → 跨全 14 章的聚合 JSON + leaderboard markdown。

## 10. 输入/调用约定

- 单章：`python score.py --gold fangan/testcases/engram/ch00_abstract --pred <pred.yaml>
  [--out report.json] [--md report.md] [--llm-judge]`
  （`--gold` 接章节目录则读其中 `gold.yaml`，也可直接接 `gold.yaml` 路径。）
- 全量：候选目录镜像 `engram/chXX_* + cmos/chXX_*` 结构，每章一个 `pred.yaml`；
  `python run_all.py --gold-root fangan/testcases --pred-root <candidate_dir>`。
- profile（`article_research` / `textbook`）从 gold 的 `source_meta.profile` 读取，
  类型词表差异不影响打分逻辑（按内容比对，不内置类型白名单）。

## 11. 正确性保证（测试）

`test_harness.py`：
1. **gold-vs-gold 必须每 stage 满分（总分 100）**——核心 sanity 不变量；
2. **扰动测试**：删一个原子（recall↓）、移一段 span（IoU↓）、改一个类型（type_acc↓）、
   注入一个多余 object（precision↓）——断言每个指标朝正确方向变化；
3. 至少覆盖一篇论文型（engram）与一篇教材型（cmos）章节，确保两 profile 都能跑通。

## 12. 范围与非目标（YAGNI）

- **不做** Web UI / 可视化前端（Markdown 报告足够人读）。
- **不做** 历史趋势库 / 排行持久化数据库（`run_all` 输出文件即可，外部自行归档）。
- **不内置 LLM 调用实现**：`judge.py` 只定义接口 + 缓存；具体后端（API / CLI）由使用方注入，
  默认关闭。
- **不改 gold 数据**：harness 只读 `gold.yaml`；若发现 gold 问题另行修订，不在本工程内。
