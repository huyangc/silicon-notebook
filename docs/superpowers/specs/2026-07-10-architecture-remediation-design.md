# 架构渐进整改设计

**日期**：2026-07-10
**状态**：已批准；以 `master` 当前代码与绿色测试为真实行为
**基线提交**：`aba02c4`

## 目标

在不改变 endpoint、SQLite schema、Repository 公共 API、前端交互与异步任务语义的前提下，降低 `SQLiteRepository`、FastAPI 总路由和前端 `Home` 编排器的耦合，使每个后续改动都能由现有测试证明行为保持一致。

## 真实行为判定

当历史文档与代码冲突时，按以下顺序判定当前行为：

1. 已通过的回归/characterization 测试。
2. 生产代码中被测试覆盖的行为。
3. `README.md`、`README_zh.md`、`AGENTS.md` 与 `architecture.md`。

文档必须追随前两项，不能用过期文字反向改变已经上线且被测试固定的语义。当前需要首先对齐的契约是：

- Ask transport 断连只停止向该客户端继续推送；detached worker 继续执行并持久化结果。只有用户显式点击中断、调用 `POST /notebooks/{id}/ask/jobs/{job_id}/cancel` 时才取消 worker。
- `base` 与 `personal` 命中的相关度不乘 tier 权重；只在 score 完全相同时让 `base` 先排。回答阶段遇到矛盾仍按 prompt 规则服从 base 并披露差异。
- notebook workspace 是来源栏 + Ask/Knowledge 主区域的两列结构；Studio 不是固定第三栏。

## 现有结构判断

现有 identity 与 sharing mixin 是有效的第一步：它们降低了单文件冲突，并通过 facade 保持兼容。但 mixin 只是迁移接缝，不是最终的 persistence port：`SQLiteRepository` 仍同时承担连接、迁移、模型客户端、缓存、作业状态、检索、回答和业务编排。

整改采用 contract-first strangler，而不是一次性 clean-architecture 重写：

- 保留 `SQLiteRepository` 作为现有消费者的公共 facade。
- 先建立行为契约和小型 Protocol，再移动实现。
- 每次只拆一个高内聚领域；旧 import、方法名与兼容导出继续有效。
- 每个阶段单独 PR，完整运行 `scripts/check.sh`，并在合并前同步最新 `master`。

## 分阶段顺序

### 阶段 1：行为契约与文档对齐

修复 Ask disconnect、tier 排序和 workspace 结构的文档漂移；重写过期的 `architecture.md`，增加文档契约测试。无运行时代码改动。

### 阶段 2：Notebook 规模策略与 Repository ports

引入中性的 `NotebookScaleProfile`，让 sharing 和 retrieval 分别消费 `is_copyable` / `requires_scale_index`，初始仍使用现有阈值以保持行为。把巨型 `NotebookRepository` 拆成按领域的小型 Protocol，再组合成兼容类型；移除 production Protocol 中的测试专用 helper。

### 阶段 3：FastAPI routers 与前端 API client

按 notebook/source/ask/knowledge/report/admin 拆 router，由聚合 router 保持全部路径和依赖不变。前端统一 JSON、NDJSON、Blob、认证与错误解析，领域 API 模块只描述 endpoint。

### 阶段 4：SQLite migrations 与模型边界

把版本迁移注册表、DDL 和 schema helper 迁入 `sqlite_migrations.py`；为全新库与旧库升级增加 schema snapshot/兼容测试。Pydantic 模型按领域拆文件，并从 `schemas.py` re-export 所有旧符号。

### 阶段 5：前端 workspace 状态拆分

先把源码字符串测试补成可迁移的 helper/hook 行为测试，再抽 `useAskSession`、`useSourceLibrary`、`useKnowledgeGraphWorkspace` 及对应 panel。第一轮不引入新的全局状态库，也不改变加载与轮询节奏。

### 阶段 6：Runtime 生命周期与 Retrieval/Ask 实现

引入 FastAPI lifespan 管理的 application runtime、job executor 和 cache coordinator；最后才把 retrieval/ask 实现从 SQLite facade 迁出。该阶段必须保留取消、重连、缓存版本和大库守卫的全部 characterization 测试。

## 非目标

- 不在一个 PR 内完成全部阶段。
- 不引入 PostgreSQL、SQLAlchemy、容器或新模型服务。
- 不改变公开 API、数据库表、检索排序、Ask 持久化、取消/断连语义或 UI 布局。
- 不以行数下降作为唯一完成标准；依赖方向和可测试接口才是验收依据。

## 验证策略

每个阶段必须具备：

1. 针对待迁移行为的失败测试或 characterization 测试。
2. 对兼容 facade/import/endpoint 的结构测试。
3. 受影响领域的定向回归。
4. `bash scripts/check.sh` 完整门禁。
5. 最新 `master` 三方合并后的再次完整门禁。
