# KG 抽取 + 问答质量评测套件 — 设计文档

- 日期:2026-06-06
- 状态:待用户审定(spec review)
- 评测对象:notebook `nb-012fb94249`("Analog CMOS IC Design",含 5 本模拟电路教材,KG 已用 deepseek-v4-flash 抽好)

## 1. 背景与目标

要构造一个测试用例,体现当前 KG 抽取 + 问答质量的好坏,且与真实用户流程一致(用真实 LLM 跑过的 KG)、高性价比(尽量少花 token)。重点三个场景:

1. **抽取速度** — 希望每个文档抽取在分钟量级;达不到则对文档大小提要求,产出"文档大小 → 预估解析时间"表与推荐上限。
2. **抽取质量** — concept 应是原子概念;符号变量(`Vb1`)、图引用(`Circuit of Fig.12.3`)、取值枚举(`Level 1/2/3 Model`,与 7nm/14nm 同构)等"非原子节点"是噪声,要可量化。
3. **问答推断** — 问答要能"综合多个分散事实做推断",而不只是检索原文;这部分质量重点考核。

### 设计约束
- **真实流程一致**:复用产品实际的抽取/问答链路与真实 LLM(deepseek-v4-flash),不用 mock。
- **高性价比**:核心思路"一套数据,三场景复用"(见 §3),把 token 花在刀刃上。
- **可回归**:第一次跑即现状体检;改进抽取 prompt/后处理后,重抽并重跑同一套脚本即可量化改进。

## 2. 现状盘点

### 2.1 代码链路(已确认)
- 抽取入口:`upload_sources` → `kg_scheduler.submit_job(process_source)` → `_run_extraction` → `extract_graph`(`kg_ingest.py`)→ 窗口并发 `extract_window`(`kg/extract.py`)→ `store_kg`。
- 并发两级:`KG_JOB_CONCURRENCY`(文档级,默认 8)、`KG_EXTRACT_WORKERS`(窗口级全局池,**当前 .env=1000**)。一篇文档所有窗口并发打到全局池。
- 自适应窗口:`窗口大小 = clamp(字数 / KG_EXTRACT_WORKERS, KG_WINDOW_MIN_CHARS=4000, KG_WINDOW_MAX_CHARS=8000)`,均分;窗口数 = `ceil(字数 / 窗口大小)`,重叠 `KG_WINDOW_OVERLAP_CHARS=450`。
- 抽取 prompt:`kg/extract.py:_prompt()`。Concept 定义为"具名、可复用的技术实体",已排除泛化词,但**未约束"不要抽数值/单位/符号/图引用/取值枚举"** —— 这是质量痛点根因。
- 代码块在切窗口阶段已被过滤(`_PROSE_TYPES` 白名单,`test_kg_excludes_code` 守护)。所以"代码节点"在本 notebook 表现为正文里的**符号变量/图引用**而非代码块。
- 问答:`routes.py:/ask` → `sqlite_repository.ask()`。混合检索(关键词 0.4 + 语义 0.6)+ 1-hop 邻居扩展 + `answer_prompt`("允许 reason beyond"、`[k_i]` 句级引用、推断句标"(推断)")+ `evidence_level` 三档(grounded/overview/inferred)。**多跳综合无显式 prompt 指导,靠 LLM 自觉** —— 这正是推断评测要逼出的能力。
- 耗时观测点现成,无需改代码:`.local/logs/llm.jsonl`(每次调用 `latency_ms`/token)、`.local/logs/events.jsonl`(pipeline 各 stage `latency_ms`)。

### 2.2 数据结构(已确认,评测脚本据此读库)
- `knowledge_objects(id, notebook_id, object_type, status, payload, evidence, source_candidate_id, ...)`。`object_type` 取值为**小写** `concept`/`claim`/`formula`/`procedure`。`source_candidate_id` 全为空。
- payload(JSON):
  - concept: `{"name","section_path"}`(**无 definition**)
  - claim: `{"name": "<完整断言句>","section_path"}`
  - formula: `{"name": "<公式文本/LaTeX>","section_path"}`
  - procedure: `{"name","section_path","steps":[{"name","element_id","quote"}]}`(steps 可缺失/为空)
- evidence(JSON 数组):`[{"source_id","source_title","element_id","element_type","location_label","quoted_span"}]`。**按书归属 = `json_extract(evidence,'$[0].source_id')`**。
- `knowledge_relations(id, notebook_id, source_id, source_object_id, target_object_id, edge_type, evidence, created_at)`。**关系按书归属 = 表内 `source_id` 列**。
- 规模(nb-012,5 本书合计):claim 11088 / formula 9032 / concept 7955 / procedure 1707。其中 Razavi《Design of Analog CMOS IC》(`src-9c312953d7`)concept 1500。

### 2.3 痛点实证(纯 SQL,0 token;Razavi 本 1500 concept)
| 探针信号 | 命中 | 占比 | 真实样例 |
|---|---|---|---|
| 含括号/等号 | 159 | 10.6% | `Circuit of Fig. 12.3(a)`、`g11 (input conductance of feedback network)` |
| 含下划线 | 78 | 5.2% | `Z_out,0`、`Z_in,0`、`R_0` |
| 含数字 | 51 | 3.4% | `Level 1/2/3 Model`、`Vb1`/`Vb2`/`Vb3` |
| 数字开头 | 1 | 0.07% | 本书几无 7nm 式 |

痛点三类形态:**符号变量、图表/电路引用、取值枚举**(`Level 1/2/3 Model` 与 7nm/14nm 同构)。基线"疑似非原子"约 10–15%。

## 3. 整体架构:一套数据,三场景复用

```
评测对象 = nb-012fb94249 现有 KG(deepseek 真实抽取,≈3 万对象)
   ┌───────────────┼─────────────────────────┐
场景② 质量探针      场景③ 推断问答            场景① 抽取速度
扫现有 KG(SQL)     现有 notebook 问答+judge   Razavi 截 5 档片段重抽计时
【0 token】        【低:~30 题×2 调用】       【低:~5 次小抽取】
```

省 token 原理:KG 已是真实 LLM 抽的,质量(②)与问答(③)无需重抽;只有速度(①)必须重抽计时,且用"小样本实测 + 公式外推"代替真跑 2.1M 整本。

**总成本量级**:② 0 + ① ~5 万 token + ③ ~30 万 token,deepseek-flash 单价极低,整套一遍约几分钱~几毛钱。

## 4. 场景② — KG 抽取质量探针

### 4.1 目标
量化"非原子 concept 噪声率"及 claim/formula/procedure 退化率,**全扫 5 本、按书 + 按类型拆分对比**,可回归。

### 4.2 concept 探针清单
每个探针 = 一条规则,输出:命中数、占比、Top-20 样例。归属用 `evidence[0].source_id`。

| # | 探针 | 规则(Python regex / 逻辑) | 抓的痛点 | 已验证样例 |
|---|---|---|---|---|
| P1 | 符号变量 | `^[A-Za-z]{1,3}[\s_,]?\d+$` 或含 `_`/下标逗号且无空格短名 | 局部符号非概念 | `Vb1`,`Z_out,0`,`R_0`,`V1` |
| P2 | 图表/章节引用 | 含 `\b(Fig|Figure|Table|Eq|Equation|Section|Chapter)\b\.?\s*\d` 或 `Circuit of` | 对图的指代 | `Circuit of Fig. 12.3(a)`,`CMFB ... (Fig. 9.53)` |
| P3 | 取值枚举 | name 把 `\d+`/罗马数字掩码为 `#` 后归一,**同掩码下 ≥2 个不同变体** | 同概念的取值被拆开(=7nm/14nm) | `Level 1/2/3 Model` |
| P4 | 数字+单位 | `\d+\.?\d*\s?(nm|um|µm|mm|V|mV|kV|A|mA|uA|Hz|kHz|MHz|GHz|dB|Ω|ohm|F|fF|pF|nF|W|mW)\b` | 7nm 式工艺/参数取值 | (本书少,跨书对比用) |
| P5 | 代码标识符 | 驼峰 `[a-z]+[A-Z]`、全大写下划线 `^[A-Z][A-Z_]{2,}$`、调用样式 `\w+\(` | EDA 命令/函数名 | (innovus 多,本书少) |
| P6 | 过短/泛化 | `len(name)<=2` 或命中泛化词表(training/buffer/value/...) | 退化概念 | `M5`,`V1` |
| P7 | 孤儿节点 | evidence 数=1 **且** 在 relations 中度数(src+tgt)=0 | 抽取噪声 | — |
| P8 | 近重复 | name 归一(小写、去标点、合空格)后同名计数 ≥2 | 去重残留 | formula `S_A-B = ...` 两版 |

**核心指标**:每书「**疑似非原子概念率**」= P1–P6 命中并集 / concept 总数(去重)。辅以健康面:原子概念率、name 词数分布(原子概念通常 1–4 词)、关系覆盖率(有 ≥1 关系的 concept 占比)。

### 4.3 claim/formula/procedure 退化探针
- claim 退化:词数 < 4;或不含动词/系动词(启发式动词表 is/are/has/causes/increases/reduces/requires/...);或以介词/冠词结尾(疑似截断)。
- formula 退化:name 不含 `=` 且不含运算符 `[+\-*/^]` 且不含 `$`/`\`(疑似非公式)。
- procedure 退化:`steps` 缺失或为空(疑似伪流程,如 `Analysis process`);单独统计 steps==1 的占比。

### 4.4 精度校准(重要,写入报告免被误读)
探针给的是**疑似信号**,存在误报:如 cascode 有 standard/high-swing/regulated/Sooch/MOS/bipolar 等**合理变体**,会被 P3/P8 误命中。因此:
- 报告对每个探针随机抽 20 条样例并列出,供人工 5 分钟扫一眼估"真噪声占命中的比例"(精度)。
- 报告里"疑似非原子率"与"(抽样估计的)真噪声率"分开呈现,不把命中数当定论。
- (可选,默认关)`--llm-audit N`:抽 N 条让 LLM 判"是否原子概念",佐证探针精度;关闭时纯 0 token。

### 4.5 产物 `quality_report.md`
- 每本书 × 每类型:对象数、各探针命中率、疑似非原子率、健康面指标。
- 5 本横向对比表(哪本书、哪类节点最脏)。
- 每探针 Top-20 样例清单。
- 运行配置与时间戳(便于回归 diff)。

## 5. 场景① — KG 抽取速度

### 5.1 方法:小样本实测 + 公式外推
不真跑 2.1M 整本。从 Razavi 本按 **markdown 段落边界**截取 5 档(避免截断公式/表格),各跑真实抽取计时:
- 默认档位:**5K / 20K / 50K / 100K / 200K 字符**(开放项:是否加 500K)。
- 每档跑 1–2 次取中位数。
- 隔离:用一个**临时 notebook**(或临时 DB 文件 `DATABASE_URL` 覆盖)抽取,避免污染主库;抽完即弃。

### 5.2 采集字段(来自现成日志)
| 字段 | 来源 |
|---|---|
| 字数、窗口数、窗口大小 | `plan_window_size` / events `pipeline extract` |
| 墙钟耗时(extract stage) | `events.jsonl` stage=extract `latency_ms` |
| 单窗口延迟 p50/p95/max | `llm.jsonl` `latency_ms` |
| token(prompt/completion) | `llm.jsonl` `usage` |
| 失败窗口/重试数(限流信号) | `llm.jsonl` error/attempt |

### 5.3 外推公式(代码已确认)
```
窗口大小 = clamp(字数 / KG_EXTRACT_WORKERS, 4000, 8000)
窗口数   = ceil(字数 / 窗口大小)
有效并发 = min(KG_EXTRACT_WORKERS, 窗口数, deepseek 实测承载)   # 由实测拟合
耗时     ≈ ceil(窗口数 / 有效并发) × 单窗口延迟 p50 + 固定开销   # 固定开销由实测拟合
```
关键:`WORKERS=1000` 下本地并发几乎不设限,真实瓶颈是 **deepseek 限流/承载**,实测的失败/重试率会暴露有效并发上限。

### 5.4 产物 `speed_report.md`
- 实测表:字数 | 窗口数 | 墙钟耗时 | 单窗口 p50/p95 | token | 失败率。
- 外推表:1 万 / 5 万 / 10 万 / 20 万 / 50 万 / 100 万字 → 预估耗时。
- **推荐文档上限**:满足"≤ 2 分钟"(开放项:目标阈值)对应的最大字数;超出建议拆分上传。
- 调参建议:若失败率随并发上升(限流),给 `KG_EXTRACT_WORKERS` 推荐值。

## 6. 场景③ — 推断问答

### 6.1 问题集(草稿,4 层 ~30 题,在整个 5 本书 notebook 上问)
每题字段:`id, level, question, expected_points(期望要点), expected_evidence_level, expected_behavior(是否应有引用/是否应标推断), notes`。下为草稿题干(用户在 spec review 逐条审定/增删):

**L1 直接检索(单对象,验证基础检索+引用;期望 grounded、有 [k])**
1. 什么是 cascode connection?它的主要作用是什么?
2. MOSFET 的 square-law characteristic 指什么?
3. 什么是 current mirror?
4. bandgap reference 的目标是什么?
5. flicker noise(1/f noise)是什么?
6. 什么是 switched-capacitor circuit?
7. differential amplifier 的基本概念是什么?
8. MOS 晶体管进入 saturation 的条件是什么?

**L2 单跳邻居(需 1-hop,验证邻居扩展;期望用到邻居对象)**
9. current mirror 有哪些典型实现/变体?
10. cascode 技术能把输出电阻提高多少(给出量级/因子)?
11. 哪些电路用到了 cascode connection?
12. 与 bandgap reference 相关的关键公式有哪些?
13. regulated cascode 的输出电阻能达到什么量级?
14. CMFB(共模反馈)用在什么放大器里?
15. 影响 current mirror 精度(ratio error)的因素有哪些?

**L3 多跳综合(答案分散多处,必须综合 ≥2 事实;核心考点)**
16. **为什么 cascode 既能提高输出阻抗、又会限制输出摆幅?**(需综合"rout↑ by g_m·r_ds"与"堆叠增加 threshold+overdrive 的电压需求"两处)
17. 为什么 regulated cascode 比 standard cascode 输出电阻更高,代价是什么?
18. 高增益与输出摆幅在 cascode 结构里如何权衡?
19. mismatch 如何影响 current mirror 与 differential pair,机理上有何共性?
20. 为什么 CMOS 适合做模拟/混合信号 VLSI,但模拟设计仍需"hands-on"?(综合多条 claim)
21. 增大 cascode 级数对增益和电压裕度分别有什么影响?
22. 反馈如何同时影响放大器的带宽与稳定性?
23. 从器件 square-law 出发,如何解释 current mirror 的电流复制原理?

**L4 应拒答 / 标注推断(KG 无此内容,验证诚实性;期望 grounded=false 或标"(推断)",严禁伪造 [k])**
24. 用 3nm FinFET 工艺实现这个 bandgap,失调会有什么具体变化?(书无先进工艺节点)
25. 在 Innovus 里用什么命令做 place_opt_design?(EDA 工具命令,本 notebook 无)
26. 这个 op amp 在 -40°C~125°C 车规下的具体失调电压是多少 mV?(无具体数值)
27. 台积电 N5 工艺的具体 SPICE 模型参数是多少?(无厂商参数)
28. 把这套电路改成 GaN 工艺需要改哪些版图规则?(跨工艺,书无)
29. 这本书第 20 章讲了什么?(超出实际章节,验证不臆造)
30. ChatGPT 的注意力机制和这里的 feedback 有关系吗?(完全跨域,验证不强行关联)

### 6.2 LLM-judge(deepseek 评委,结构化)
输入每题:`{question, expected_points, system_answer, anchors, evidence_level}`。judge 用与答题不同的 prompt,输出 JSON:
```json
{"correctness": 0|1|2, "inference_quality": 0|1|2, "grounding_consistency": true|false,
 "fabricated_citation": true|false, "reason": "一句话"}
```
- correctness:是否覆盖期望要点、有无事实错误(0 错/1 部分/2 准确)。
- inference_quality:该综合时是否综合、该标推断时是否标(0 差/1 一般/2 好)。
- grounding_consistency:`evidence_level` 与实际是否相符。
- fabricated_citation:是否给推断句/无关项强加 `[k]`(伪引用,严重扣分,尤其 L4)。
- 评委默认 deepseek(便宜);开放项:可另配更强模型(如 Claude)做评委以提可信度,报告标注评委来源。

### 6.3 产物 `inference_report.md`
- 分层得分表:各层平均 correctness / inference_quality、grounding 一致率、伪引用率。
- **核心信号**:`L3(多跳综合)均分 − L1(直接)均分` 的落差 = 推断能力量化;L4 伪引用率 = 诚实性。
- 逐题明细:问题/系统答案(含 [k])/evidence_level/judge 各分/理由。

## 7. 目录结构与文件职责

```
backend/eval/                 # 新建评测包(PYTHONPATH=backend,可 import app)
  __init__.py
  db.py            # 读 sqlite:按 notebook/source/type 取对象与关系、按书归属
  probes.py        # 场景② 探针规则与聚合(0 token)
  speed.py         # 场景① 截片段、触发真实抽取、采集日志、外推
  inference.py     # 场景③ 调 ask + judge、聚合
  questions.yaml   # 场景③ 问题集(本 spec §6.1 草稿落盘,用户审定)
  report.py        # 三种 markdown 报告渲染
  run_all.py       # 一键三场景;支持 --only quality|speed|inference
.local/eval_runs/<timestamp>/ # 报告产物(gitignore)
  quality_report.md / speed_report.md / inference_report.md / raw.json
```
(开放项:目录 `backend/eval/` vs `scripts/eval/`)

## 8. token 成本预估
| 场景 | LLM 调用 | 量级 |
|---|---|---|
| ② 质量 | 无(可选 LLM 抽查) | 0 |
| ① 速度 | 5 档抽取 ≈ 49 窗口/轮 × 1–2 轮 | ~3–6 万 token |
| ③ 推断 | 30 题 ×(1 ask + 1 judge) | ~20–30 万 token |

## 9. 非目标(YAGNI)
- 不做 gold ↔ 产品 KG 的 P/R/F1 适配层(用户选规则探针;`fangan/testcases` gold 留待将来)。
- 本套件不修改抽取 prompt / 后处理 / 问答逻辑;只**度量**,不改产品行为。
- 不做评测 UI / 仪表盘;产物是 markdown + json。
- 速度不真跑 2.1M 整本;靠实测 + 外推。

## 10. 回归用法(改进后如何复测)
1. 改进抽取(如给 prompt 增加"不要抽符号/图引用/取值枚举"约束)。
2. 重抽一本(或一章)到临时 notebook。
3. 重跑 `probes.py` 对同书 diff "疑似非原子率";重跑 `speed.py` 看耗时变化;重跑 `inference.py` 看 L3 落差是否收窄。
4. 把新 `eval_runs/<ts>/` 与基线对比。

## 11. 待用户审定的开放项
1. 速度档位是否加 500K?速度目标阈值用 2 分钟还是其他?
2. judge 评委:deepseek 自评 vs 另配 Claude key?
3. 评测脚本目录:`backend/eval/` vs `scripts/eval/`?
4. 问题集 §6.1 30 题:逐条审定(删/改/增),尤其 L3 多跳题是否贴合你真实关心的推断场景。
5. 质量探针阈值/泛化词表是否要补充你领域里的特定噪声模式。
