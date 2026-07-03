# 深度报告模式(Deep Report)设计 Spec

日期:2026-07-03 · 状态:**已评审通过**(用户 2026-07-03:①【通识】默认开 OK;②每节跑完整深挖循环、节间无耦合并行执行;③答案气泡同 PR 切 react-markdown OK)· 前置讨论:Bandgap 真机问答 GPT 评价 + 外部咨询报告对照分析

## 0. 背景与目标

真机案例:用户以模拟电路书籍为库,用 reasoning 模式问 Bandgap 多层推导题,答案 grounded 但被评「层次不清、深度不足」(6.5/10);对照的外部「技术咨询报告」以 多节模板+参数知识+知识缺口外显 呈现,完整感显著更强,但其量化数据全部未经验证。

**目标:让 silicon-notebook 产出结构化深度技术报告,同时守住本产品的差异化——可验证性。** 具体:
1. 多节报告(执行摘要/分层机理/工程建议/知识缺口/参考),几分钟级后台任务;
2. 证据三层制:`[k]` 库内引用 / `（推断）` 库内桥接 / `【通识】` 参数知识(显式标注、默认建议核实);
3. 知识缺口显性化(两层 KB roadmap 决策③首次落地);
4. 前后端同步交付(用户 2026-07-01 明确要求)。

**非目标(YAGNI,明确不做):** 外部 web 文献连接器(内网约束,列为远期可选)、定时/订阅报告、报告间对比、docx 导出、复活已删除的「文章研究」旧表。

## 1. 产品定位与入口

- **不是新 ask mode**。ask 模式保持 `['chunk','graph','reasoning']` 三元不变(流式问答 UX 与多分钟多调用任务不匹配)。
- 深度报告是 **notebook 级后台任务 + 持久化产物**,与「构建知识图谱」同一交互范式(POST 起 job → 进度 → 产物落库可回看)。
- 前端入口:ask 面板问题输入区旁「深度报告」action(与 mode 选择器并列但视觉区分为"任务"而非"模式");已生成报告在 notebook 侧栏「报告」列表。

## 2. 管线设计(后端)

四阶段,全程贯穿 cancel_event 与 per-user 模型解析(job 线程须 copy_context,教训见 memory per-user-model-config-state):

### Stage A 大纲规划(1 次 LLM)
- 输入:question + 对话 history(可选)+ notebook 概况(KG 规模/来源数)。
- 输出 outline:3–6 节(REPORT_MAX_SECTIONS 上限),每节 {title, scope 一句话, sub_queries 2-4 条}。
- prompt 给默认骨架作 fallback:执行摘要(末置生成)/分层机理/工程要求与建议/知识缺口/参考文献;**大纲由 LLM 按问题定制,不硬编码领域模板**(领域模板留作后续可配置项)。
- 解析失败 → 默认骨架 + expand_query 的子查询平铺分节。

### Stage B 逐节深挖(用户拍板 2026-07-03:每节跑**完整 reasoning 深挖循环**,节间并行)
每节以「节 scope + 节子查询」为问题,完整跑 `ReasoningRetriever.run`(Plan→Retrieve→Reflect 深挖→候选,含 expand_graph/add_subquery/search_elements/ppr 全部动作与 #177 防重账目):
- 每节独立预算:top_n=REPORT_SECTION_TOP_N(默认 12)、chunk 预算 REPORT_SECTION_CHUNK_BUDGET(默认 20000 字)——从根上解决"层间配额挤压";PPR 以节查询做种(修"恒锚定主实体")。
- **节间无耦合 → ThreadPoolExecutor 并行执行**,并发度 REPORT_SECTION_CONCURRENCY(默认 3,尊重限流退避机制);墙钟≈最慢一节。
- ⚠ 工程要点:worker 线程不继承 ContextVar——per-user 模型客户端须**主线程解析后传入**或每 worker `copy_context`(踩坑记录见 memory kg-incremental-fusion-state / per-user-model-config-state);共享同一 cancel_event,取消即全节停。
- 每节深挖的 attempted 账目与 trace 保留,直接喂 Stage D 知识缺口节。

### Stage C 逐节撰写(每节 1 次 LLM)
- answer_prompt 的报告变体:继承引用/LaTeX/base-personal 全部纪律 + R0 新增的分层/量纲/数值区间条款,追加**三层证据规则**(见 §3)与节 scope 约束("只写本节主题,勿重复其它节")。
- 每节返回 {markdown, grounded, used_k_ids};max_tokens 走 REPORT_SECTION_MAX_TOKENS(默认 8192,接 llm-max-tokens-caps 机制)。

### Stage D 汇总(1-2 次 LLM + 纯代码)
- **执行摘要**:由各节 markdown 压缩生成(1 次 LLM)。
- **参考文献**:纯代码——各节 [k] 去重映射回 source 列表(库内引用区);【通识】提及的外部文献单独列"未经库内验证,建议核实"区。
- **知识缺口节**(纯代码+模板,零新基建):
  - 子查询级:各节检索账目里 new=0/命中稀薄的子查询清单(PR#177 attempted 账目思路,报告管线内同构记账);
  - KG 级:取各节 top 概念(canonical id),两两查 relations/neighbors(现有 API),报告"X↔Y 在图谱中无连接"类结构缺口;
  - 落款"建议补充语料"清单(可后续接知识缺口队列表)。
- **分析计划头**:outline+各节子查询原样呈现(即 trace 外显)。

## 3. 证据三层制(报告模式核心规则)

| 层 | 标记 | 规则 |
|---|---|---|
| 库内引用 | `[k]` | 与现行规则完全一致(DIRECTLY 支持才可挂) |
| 库内桥接 | `（推断）` | 与现行一致(从库内证据出发的推理) |
| 参数知识 | `【通识】` | **报告模式新增**:允许引入库外领域通识(量化典型值/工程惯例),必须行内标注,数值给区间不给点值,节尾自动附"【通识】内容未经库内验证"提示 |

- 开关 `REPORT_ALLOW_PARAMETRIC`(默认 true,**仅报告管线读取**;三个 ask 模式不变,严格 grounding 差异化不动摇)。
- grounded 语义:节级 grounded = 该节存在 [k] 支撑;报告级汇总各节。

## 4. 数据模型与 API

```sql
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,            -- rep-<hex>
  notebook_id TEXT NOT NULL,
  question TEXT NOT NULL,
  outline_json TEXT NOT NULL DEFAULT '[]',
  content_md TEXT NOT NULL DEFAULT '',   -- 汇总后的完整 markdown(单文档,分节标题内嵌)
  sections_json TEXT NOT NULL DEFAULT '[]', -- [{title,markdown,grounded,k_count}]
  gaps_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|cancelled
  progress TEXT NOT NULL DEFAULT '',      -- 人读进度句
  error TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- `POST /notebooks/{id}/reports` {question, history?} → {report_id}(后台线程,复用 KG job 并发/事件框架;event_log 记 report_* 事件)
- `GET /notebooks/{id}/reports` 列表(owner/member 按 require_notebook_read 守卫;created_by 归属)
- `GET /notebooks/{id}/reports/{rid}` 详情(status+progress+content)
- `POST /notebooks/{id}/reports/{rid}/cancel`、`DELETE .../{rid}`
- 前端轮询详情(沿用来源状态轮询的自调度+退避模式,不搞整页 re-render,教训见 kb-list-scale-and-poll-storm)

## 5. 前端(同一 PR 交付)

1. ask 面板「深度报告」按钮 → 弹出确认(预计耗时/token 提示)→ 起 job → 输入区上方出现进度卡(节 x/y + 当前阶段句)。
2. 侧栏「报告」列表:标题=question 截断,状态徽章;点击进查看页。
3. 报告查看:**react-markdown + remark-gfm 分节渲染**(表格/公式;KaTeX 沿用现有方案)——**同 PR 收掉 task#31**:report viewer 与答案气泡一并切换 react-markdown(用户 2026-07-03 已确认)。
4. 导出:「下载 .md」按钮(content_md 直出)。
5. UI 按 ui-polish-bar 标准:对齐/省略号/加载态,改完给视觉验证截图。

## 6. Config 旋钮(pydantic-settings v2,注意 validation_alias 坑)

| 项 | 默认 | 说明 |
|---|---|---|
| REPORT_MAX_SECTIONS | 6 | 大纲节数上限 |
| REPORT_SECTION_TOP_N | 12 | 每节 KG 名额 |
| REPORT_SECTION_CHUNK_BUDGET | 20000 | 每节 chunk 字预算 |
| REPORT_SECTION_MAX_TOKENS | 8192 | 每节生成上限 |
| REPORT_ALLOW_PARAMETRIC | true | 【通识】层开关(仅报告管线) |
| REPORT_SECTION_CONCURRENCY | 3 | 节间并行度(节深挖无耦合,墙钟≈最慢节;尊重 429 退避) |

## 7. 测试与验收

- 单测:outline 解析与 fallback/三层标注 prompt 契约(文本断言)/缺口段生成(zero-hit 子查询→清单;无边概念对→清单)/端点权限(owner/member/非成员 403)/**并行执行**(节结果按大纲序聚合、单节失败不拖垮整报告、共享 cancel_event 取消全停、per-user 客户端主线程解析传入 worker)。
- smoke:离线起一份 2 节小报告(stub LLM)走通 pending→done。
- 真机验收:同一 Bandgap 题生成报告,对照外部咨询报告逐节比较;GPT 复评目标 ≥8/10(结构、缺口外显、三层标注为主要增益点)。

## 8. 分期

- **R1a(本 spec 主体)**:Stage A-D + 数据模型/API + 前端全部(§5)。
- **R1b(可并行,与代码无关)**:语料补充——固体物理教材+精密基准文献(Fruett&Meijer、Abesingha、Varshni)进 base 库(tier=base + scripts/build_kg.sh),【通识】逐步升级为 [k]。
- **R2(远期可选)**:领域报告模板配置、外部文献连接器、知识缺口队列表落库。

## 9. 风险

- 时长与成本(按 2026-07-03 拍板的逐节完整深挖):每节 ≈ plan(1)+reflect 深挖(3-8)+refine+撰写(1) ≈ 6-12 次 LLM 调用,6 节 ≈ 40-70 次;并行度 3 下墙钟 ≈ 2×单节深挖时长(约 5-15 分钟量级,视模型与限流)→ 进度可视+可取消是一等需求,起 job 前给预计耗时提示。
- 【通识】幻觉:三层标注+区间化+节尾提示缓解;默认开是报告模式的定位选择,可 env 关。
- 与 #177/#178(R0) 的 prompts.py 邻接改动:报告 prompt 独立新函数,不叠同一文本块,冲突面小。
