# PR #306 接管与历史债务收口设计

日期：2026-07-20
状态：设计已确认

## 1. 背景

PR #306（`fix(knowhow): 收掉 PR#298 合入时登记的 4 条评审遗留`）在
`0e4b6e85edb22edd040c98595742c9d2d40d3a17` 上已经完成八个提交，覆盖：

1. 批量规整生命周期内冻结整表快照，陈旧后的父表重载推迟到关窗；
2. Markdown 行中注释 / processing instruction 开启识别；
3. 跨行 inline-link destination 与 title 保护；
4. GFM autolink 的 ASCII-alnum 起始边界；
5. 五轮自动评审随后发现的批量写目标漂移、inline span 误判、link title
   提前闭合、锚点离组、锚点指定和成员集漂移。

当前 head 的聚焦验证为：

- 后端相关测试：419 passed；
- 前端相关测试：469 passed；
- 接管前旧版完整门禁：backend 4333、harness 54、frontend 1286、
  TypeScript 与 Next build 全绿。

PR 的 base 仍是 `80501736`。其后 `master` 已合入：

- #307：测试架构治理与 60 秒完整门禁；
- #308：GitHub Actions CI。

因此 PR #306 目前不可直接合并。冲突不是产品实现冲突，而是 #306 在旧测试
架构上新增了源码切片 / 行号型守卫，#307 正好删除了这类测试并替换为语义 AST
契约。

## 2. 目标

- 在新的 `codex/pr306-takeover` worktree 中接管，不修改或解锁原 Claude
  worktree。
- 从 #306 exact head 合并最新 `origin/master`，通过普通 merge commit 保留
  八个原始提交及五轮审查脉络。
- 最终 fast-forward 推送到原远端分支
  `claude/knowhow-md-normalize-followup`，不 force-push、不另开重复 PR。
- 保留 #306 的全部产品行为与有效回归覆盖，同时遵循 #307 的测试规范：
  不使用源码行号、源码切片、函数顺序或格式作为测试身份。
- 对 #306 新增的锚点结构守卫做一次 fail-closed 复核，补齐同表绑定和参数长度
  这类存储边界约束。
- 使用 Homebrew Python 完成聚焦测试和完整 `scripts/check.sh`；合并 #307 后
  warm gate 目标仍为 60 秒内。
- 更新 PR 后，由独立 subagent 审查 exact pushed green SHA；Critical /
  Important 必须处理后再交付。

## 3. 非目标

- 不重写或 squash #306 的八个历史提交。
- 不借接管机会重构整个 knowhow panel、Markdown scanner 或 repository facade。
- 不重新引入 #307 已删除的行号型 repository surface 测试。
- 不开启 required check；GitHub Actions 当前只作信息性验证，不作为合入前提。
- 不把后续历史债务候选混入 #306。

## 4. 接管策略

采用“新本地分支 + 原 PR 远端分支”的方式：

```text
PR #306 head 0e4b6e85
          │
          ├── codex/pr306-takeover（新 worktree）
          │        │
          │        ├── 设计 / 计划
          │        ├── merge origin/master
          │        ├── 冲突迁移与守卫补强
          │        └── 本地完整绿灯
          │
          └── fast-forward push
                   ↓
claude/knowhow-md-normalize-followup（原 PR #306）
```

这能同时满足：

- 不触碰陈旧但仍登记为 locked 的原 Claude worktree；
- 不改写已有 SHA；
- PR URL、评论、审查历史保持连续；
- 新提交可被精确审查和回退。

不选 rebase：它需要 force-push，会让历史评审指向被改写的提交。

不选新 PR：它会丢失 #306 的五轮讨论脉络，并制造两个表达同一债务的 PR。

## 5. 冲突处理设计

### 5.1 Repository surface 测试

`backend/tests/test_repository_surface_manifest.py` 在 #306 中只因
`routes.py` 增行而更新源码行号；#307 已删除整个文件，并用以下语义契约替换：

- `backend/tests/architecture/repository_contract.py`
- `backend/tests/test_repository_surface_contract.py`
- `backend/tests/fixtures/repository_contract/facade_surface.json`

合并时保留删除，不迁移那些行号变化。#306 对
`backend/tests/fixtures/repository_contract/api_contract.json` 的真实 API schema
更新继续保留。

### 5.2 前端接线测试

`frontend/app/knowhow-optimize.test.mjs` 同时被两边修改：

- #306 添加快照、延迟重载、冻结 fan-out、anchor guard 的接线守卫；
- #307 把原有 `readFile + indexOf + slice + regex` 迁移到
  `frontend/app/test/semantic-source.mjs`。

合并规则：

1. 纯函数行为继续直接测试：
   `planReformatSaves`、`coversAnchorGroup`、`anchorMd`、stale 状态机等。
2. React 编排中无法廉价挂载的少量接线约束使用语义 AST：
   `findFunctionIn`、`variableInitializersIn`、`callSitesIn`、
   `controlFlowIn`、`callbackFlowsIn`、`jsxElements`。
3. 删除 `readFile`、`panelSrc`、`runSaveSrc`、`handleCellSaveSrc` 及所有
   `indexOf/slice/split` 源码断言。
4. 类型签名不再用正则复制一遍；由 TypeScript 编译和真实调用点共同约束。
5. 不为迁移而扩大 `semantic-source.mjs`。只有现有 API 无法表达一个必要语义时，
   才添加小而通用的 AST 投影，并给投影自身加测试。

### 5.3 关键语义

迁移后的测试仍必须证明：

- `requestStaleReload` 只置 `pendingReloadRef.current = true`；
- 唯一的 `onStaleReload` 调用位于 modal 卸载 cleanup；
- `snapshotRef` 在挂载时冻结 `allRows` 与 `anchorColumnId`；
- `runSave` 从冻结快照生成 save units、target row ids 与 anchor guard；
- 显式 target ids 优先于实时分组重算；
- anchor guard 会让单例组也走 guarded batch endpoint；
- 多行组的非共享列不会误带整组守卫；
- 两个 modal 入口和 cell editor 仍连接到同一个 reload path。

## 6. 锚点守卫的边界补强

#306 的 store guard 已检查：

- 锚点列仍被指定为 anchor；
- 每个冻结目标的 anchor 值未变；
- 当前成员集合等于冻结成员集合；
- 目标格 `expected_before` 未变；
- 全部检查和写入位于同一个 `BEGIN IMMEDIATE`。

接管时再补两条 fail-closed 边界：

1. `expected_anchor` 长度必须与 `updates` 相等。API 已校验 wire 数组长度，但
   store/facade 是独立可调用边界，不能依赖上层永远正确。
2. `anchor_column_id` 所属表必须与每个 update 的 `table_id` 相同。否则一个
   外表的合法 anchor 列配合空锚点值，可能绕过“当前逻辑组”校验。

失败语义沿用现有 `{"written": [], "conflict": True}`，不新增异常类型和用户文案。
测试先失败、实现后转绿。

## 7. 文档同步

这次变更补充的是现有 knowhow 批量规整的并发保证。按照仓库约定，同步：

- `README.md`
- `README_zh.md`
- `AGENTS.md`

三者只描述事实：

- 一个批次使用打开时冻结的完整表快照；
- 合并共享格保存会在同一事务校验目标内容、锚点指定和精确成员集；
- 发现并发漂移时整组拒绝并在关闭弹窗后刷新；
- 不描述内部审查轮次或临时实现细节。

这不是 `silicon_notebook_fangan.md` 的新增功能，不更新 `fangan_done.md`。

## 8. 验证与 PR 审查

验证顺序：

1. Markdown scanner 的 Python / TypeScript 聚焦测试和差分一致性；
2. knowhow store / API 并发守卫测试；
3. knowhow frontend logic / semantic wiring 测试；
4. TypeScript；
5. `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`。

PR 更新流程：

1. 将本地 green head fast-forward 推送到原 PR 分支；
2. 更新 PR body，记录最新测试数量、耗时、merge base 和验证命令；
3. 使用 `superpowers:requesting-code-review`；
4. 派 `gpt-5.6-terra`、`reasoning_effort=high` 的独立 subagent 审查 exact
   pushed SHA；
5. 处理所有 Critical / Important；
6. 若有改动，重新完整验证、推送，并对新的 exact SHA 复审；
7. 最终交付 PR URL、head SHA、本地验证和 CI 信息状态。
