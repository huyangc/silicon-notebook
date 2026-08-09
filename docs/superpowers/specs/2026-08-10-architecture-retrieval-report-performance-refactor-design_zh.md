# 架构、检索与 Deep Report 性能渐进重构方案

**日期**：2026-08-10
**状态**：已批准，直接实施
**基线**：`origin/master` at `f89927ed`

## 1. 背景

当前代码库同时存在两类债务：

1. 前后端文件按技术形态平铺，测试与生产代码混放，少数编排文件承担过多领域职责；
2. KG 在线检索与 Deep Report 的耗时缺少完整分段观测，并存在重复 query embedding、章节外层并发与内部检索扇出相乘、规划探针串行和启发式规则分散等问题。

最新 master 已经显著改善 KG 构建与维护路径：relink/rebuild 后台化并按 notebook 单飞，relink 按来源分页，增量融合不再为每个来源物化全库，embedding 和回填改为分页/批处理。本方案不重复这些工作，而是补齐仍未覆盖的在线检索、Deep Report 和代码组织问题。

## 2. 目标

本次改动交付一个保持默认效果与公共 API 兼容的基础重构 PR：

- 将前端测试移出 `frontend/app`，建立清晰的 production、feature、test-support 和 test 边界；
- 从超大前端编排器中抽出一个完整、可测试的 KG maintenance feature 纵切片，为后续逐 feature 迁移建立范式；
- 将 KG lifecycle 的后台维护任务编排从内容处理、融合、图查询和社区计算中分离；
- 建立 Ask 与 Deep Report 共用的 retrieval-run 上下文，复用同一轮中的 query embedding；
- 用共享的叶子检索 fan-out 闸限制报告章节内部并发，避免章节并发与子查询并发相乘压垮数据库连接池；
- 为 Deep Report 规划、检索、全篇综合、分节撰写和最终编辑补齐不含用户内容的墙钟观测；
- 将效果启发式和资源上限从流程代码抽到集中 policy，并保持默认值与当前行为一致；
- 让示例模型绑定对长正文 workload 使用非思考型服务，避免新部署重新引入已知空输出风险；
- 保持 SQLite/PostgreSQL 行为一致、来源范围和权限不变量不变、引用与可信度合同不变。

## 3. 非目标

- 不在没有评测数据时调整充分性阈值、PPR/精确检索/follow-chain 次数或社区扩展默认行为；
- 不删除多章节报告的“全部检索完成 → 全篇综合 → 并行撰写”一致性屏障；
- 不重写 KG 抽取、聚类、canonical relation、community 或 source-subgraph 算法；
- 不一次性拆完全部前端页面和全部后端 service；本 PR 建立可重复的迁移边界，后续按 feature 继续收敛；
- 不改变 HTTP/MCP payload、数据库 schema、持久化状态机或公开兼容 import。

## 4. 现状边界

### 4.1 前端

目标结构：

```text
frontend/
  app/                         # Next.js 路由和薄编排
  features/
    kg-maintenance/            # 本 PR 的首个纵切片
  shared/                      # 跨 feature UI、hooks、API 基础设施
  test-support/                # runner/setup/fakes
  tests/
    unit/
    component/
    guards/
```

测试移动必须保持既有 runner 完整递归收集，不允许通过漏收测试换取绿色。静态架构/安全守卫归入 `tests/guards`；纯逻辑归入 `tests/unit`；React Testing Library/Vitest 用例归入 `tests/component`。生产模块的测试不得再放回 `app` 或 `features`。

### 4.2 后端

本 PR 建立以下内部边界，同时保留现有 facade/engine 入口：

```text
backend/app/services/
  kg/
    maintenance_jobs.py        # relink/rebuild claim/status/settle/job orchestration
  reports/
    policy.py                  # Deep Report 规则与部署配置读取
    observability.py           # content-free timing/counter events
  retrieval_run.py             # request/report-run memo + fan-out gate
```

`report_engine.py`、`reasoning_retrieval.py`、`knowledge_lifecycle.py` 继续是兼容入口，但不再直接拥有上述职责。迁移用一跳委托和 characterization tests 证明行为不变。

## 5. Retrieval-run 设计

### 5.1 生命周期

一个 retrieval run 持有可被 `contextvars.copy_context()` 传播到 worker 的共享状态：

- query embedding memo；
- embedding request/hit/error 计数；
- 叶子检索 fan-out semaphore；
- fan-out acquire 次数、等待次数和累计等待毫秒；
- run kind 与不含用户输入的关联 id。

Ask 在一次回答执行期间开启 run；Deep Report 的 planning 和 generation 分别开启独立 run。状态不得跨请求、报告阶段或用户共享。失败的 embedding 不缓存，保留瞬态错误重试语义。

### 5.2 并发

闸只包围真正执行 KG/chunk/element/PPR 查询的叶子调用。章节 worker、规划编排器或持有数据库连接的代码不得先占槽再等待子任务，否则会形成嵌套死锁。

Deep Report 的外层章节并发仍由现有报告并发设置控制；内部子查询、PPR 预取和规划 KG/Element 双通道共用 retrieval-run 的叶子额度。Ask 默认不改变当前并发上限，只获得 embedding memo 和计数。

## 6. Deep Report 观测设计

所有事件只允许写阶段名、索引、计数和毫秒，不写问题、章节标题、检索词、来源 id 或证据正文。

需要覆盖：

- planning：意图覆盖探针、corpus map、大纲模型、分节充分性探针/判断；
- generation：逐节 retrieve、全篇 synthesis、逐节 draft；
- assembly：最终 editor；
- retrieval run：embedding request/hit/error、fan-out acquire/wait/wait milliseconds；
- model attempt：章节外层生成尝试次数与最终结果。

观测失败必须 fail-open，不能成为报告失败的第二条通道。

## 7. 规则治理

规则分成三类：

1. **安全与正确性不变量**：来源范围、权限、引用绑定、KG edge schema、证据合法性。继续保持确定性且不可部署调松；
2. **资源保护规则**：并发、动作次数、候选窗口、扩展总量。由 Settings 或协议常量集中治理并可观测；
3. **效果启发式**：充分性、来源多样性、Top-1 集中度、偏好权重。由 report/retrieval policy 统一读取，默认值保持当前行为，后续通过冻结评测集调优。

精确数值只记录在 `docs/product-and-api.md` 与 `docs/product-and-api_zh.md` 的规范表中，其他文档只引用字段名和语义。

## 8. 模型 workload 绑定

`report_section` 和 `report_summary` 需要稳定地产出长正文或大型结构化 JSON。示例配置将其绑定到非思考型 general 服务；`report_outline` 可继续使用 reasoning 服务。部署仍可显式覆盖，但示例不能默认引导到已知的 reasoning-content 吞噬正文预算风险。

## 9. 验证

每个子任务先运行定向测试，整合后执行：

1. backend report/reasoning/retrieval/KG maintenance 定向测试；
2. frontend Node unit/guard tests 和 Vitest component tests；
3. `cd frontend && npm run build`；
4. `scripts/check.sh` 完整门禁；
5. 独立高推理 review subagent 检查正确性、并发死锁、ContextVar 隔离、范围/权限、测试收集和文档一致性；
6. review 修复后由同一 reviewer 复审；
7. PR CI 全绿后合入。

## 10. 交付拆分

### Lane A：前端结构（中高复杂度）

- 移动测试与 test support；
- 更新 runner 和相对路径；
- 抽出 KG maintenance feature；
- 保持所有交互和 guard 收集完整。

### Lane B：检索与 Deep Report（最高复杂度）

- retrieval-run 上下文；
- embedding memo；
- 共享叶子 fan-out gate；
- planning/final editor/counter observability；
- report/reasoning policy 集中化；
- 对应并发、隔离、失败语义测试。

### Lane C：KG lifecycle 边界（高复杂度）

- maintenance job orchestration 抽取；
- lifecycle 一跳兼容委托；
- relink/rebuild 状态、单飞、finish race 和失败恢复等价测试。

### Lane D：独立审查与交付（最高复杂度）

- 全 diff 审查；
- 修复并复审；
- draft PR、CI 诊断与修复；
- 全绿后转 ready 并合入。
