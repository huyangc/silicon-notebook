# 内容资产板块可视化升级设计

日期：2026-07-21
状态：已获用户批准（方案 A：指标磁贴 + 占比条）

## 背景与目标

知识分析看板弹窗（`frontend/app/page.tsx` 4925-5147）中，「内容资产」板块
（`frontend/app/content-overview-cards.tsx`）当前是「两张白卡 + 纯文字数字 +
链接按钮」，没有任何图形元素，是看板中最朴素的板块。

目标：在**不改变数据字段、交互、状态文案**的前提下，把两张卡升级为
「图标块 + 大号总量数字 + 分段占比条 + 可点击图例 + 最近条目」的形态，
与页面其他组件（`.index-card` 语义色板、`.tag` 胶囊、`--blue` 链接）的设计
语言一致。纯前端 CSS 改动，不引入图表库。

## 范围

只改三个文件：

- `frontend/app/content-overview-cards.tsx` — 组件结构重排
- `frontend/app/globals.css` — `.content-overview*` 系列样式扩展（约 38-130 行块）
- `frontend/app/content-overview-cards.component.test.tsx` — 测试更新与新增

不动：后端、API、`NotebookContentOverview` 类型、`page.tsx` 中组件的接入方式
（props 签名完全不变）、其他任何板块。

## 页面设计语言约束（必须遵守）

- 浅色极简：白卡 + 1px `--line` 浅灰描边 + 12-16px 圆角。
- **语义色仅用于状态**：绿 `#1a7f5a` / 黄 `#b97a00` / 红 `#b42318`
  （复用 `index-tone` 色板）。卡片图标块表「身份」而非状态，故用中性
  `--soft` 灰底 + 墨色图标，不用语义色。
- 蓝色 `--blue: #1f5eff` 仅用于文字链接按钮；数字不加千分位（与现有
  `countLabel` 风格一致）。
- 图标用已依赖的 `lucide-react`：Memory = `Brain`，Knowhow = `Table2`。

## 组件结构

每张卡自上而下五层（两卡同构，Knowhow 多警示行与只读徽标）：

```
┌ [图标块] 标题            [只读徽标?] 查看全部 → ┐
│   128 条                                       │  ← 大号粗体数字 + 小字单位
│   ━━━━━━━━━━━━━━━━░░░░                         │  ← 分段占比条
│   ● 已确认 96 条   ● 待确认 32 条               │  ← 图例(可点击)
│   最近条目（最多 3 条，整宽链接按钮，不变）        │
└────────────────────────────────────────────────┘
```

### Memory 卡

- 头部：图标块（`Brain`）+ `<h3>Memory</h3>` + 「查看全部」按钮
  （`onOpenMemory(null, null)`，aria-label `查看全部记忆`，不变）。
- 总量行：`<strong>{memory.total}</strong> 条`（大数字 + 小单位）。
- 占比条（`memory.total > 0` 时渲染）：分段 = 已确认（绿）/ 待确认（黄）/
  余量（中性灰 `--line`，仅当 `total > confirmed + candidate` 时出现，不进图例）。
- 图例：色点 + 「已确认 N 条」「待确认 N 条」，即现有的两个可点击按钮
  （`onOpenMemory("confirmed"/"candidate", null)`，aria-label 不变）。

### Knowhow 卡

- 头部：图标块（`Table2`）+ `<h3>Knowhow</h3>` + 只读徽标（`readOnly` 时，
  不变）+ 「查看全部」（`onOpenKnowhow("all", null)`，aria-label 不变）。
- 总量行：`<strong>{knowhow.table_count}</strong> 张表`，后跟次要信息
  `· {knowhow.row_count} 行`（muted 小字）。
- 占比条（`table_count > 0` 时渲染）：分段 = 已同步（绿）
  / 待同步（黄）/ 同步失败（红）；`已同步 = max(0, table_count - projection_pending - projection_failed)`。
- 图例：「已同步 N 张」（不可点击，无对应过滤视图）、「待同步 N 张」
  （`onOpenKnowhow("projection_pending", null)`）、「同步失败 N 张」
  （`onOpenKnowhow("projection_failed", null)`）——后两个即现有按钮，aria-label 不变。
- 警示行：⚠「代码过期 N 格」（`onOpenKnowhow("stale_code", null)`，aria-label
  不变），琥珀色；`stale_code_count = 0` 时仍渲染（保持现有行为：现有实现无条件渲染该按钮）。

## 宽度归一化（异常数据守卫）

- `denom = max(total, 各段之和)`，每段宽度百分比 = `段值 / denom * 100`。
- 段和超过总量时（脏数据）：占比条仍填满 100%，各段按比例缩放，绝不溢出。
- 总量为 0：不渲染占比条与图例，只显示大数字 0 与空态文案。

## 状态与可访问性

以下全部**原样保留**：

- 加载态 `正在加载内容资产…`（`role="status"`）、错误态 `内容资产暂时不可用`
  （`role="alert"`）：整板块级，不出卡片。
- 空态文案 `还没有已保存的记忆` / `还没有 Knowhow 表`。
- 最近条目：最多 3 条、整宽省略号截断链接按钮、aria-label
  `打开记忆 {title}` / `打开 Knowhow 表 {title}`，点击参数不变。
- 只读徽标「只读」。

新增可访问性：

- 占比条容器 `role="img"` + `aria-label`（如 `已确认 2 条，待确认 2 条`；
  Knowhow 为 `已同步 0 张，待同步 1 张，同步失败 2 张`），分段本身 `aria-hidden="true"`。
- 图例色点 `aria-hidden="true"`，屏幕阅读器走按钮自身文本。

## 样式（globals.css 的 `.content-overview*` 块）

保留：`.content-overview`、`.content-overview-grid`、`.content-overview-card`、
`.content-overview-card-head`、`.content-overview-recent`、
`.content-overview-empty`、`.content-overview-state`、`.content-overview-read-only`。

新增/调整：

- `.content-overview-ic`：36×36、border-radius 10px、`--soft` 底、墨色图标
  （对齐 `.index-card .index-ic` 的尺寸与圆角）。
- `.content-overview-total`：大数字 28px/700 + 单位小字 muted；Knowhow 次要
  信息同行 muted。
- `.content-overview-bar`：高 4px、border-radius 999px、overflow hidden、
  背景 `--soft`（仅兜百分比舍入的残余空隙）；分段为内联块、宽度内联 style 百分比。
- 分段色类：`.seg-ok`（#1a7f5a）、`.seg-warn`（#b97a00）、`.seg-danger`
  （#b42318）、`.seg-neutral`（`--line`）。
- `.content-overview-legend`：图例行，色点 8px 圆 + 12px 文字；可点击项沿用
  现有蓝色链接按钮风格（hover `--soft` 底）。
- `.content-overview-stale`：警示行，琥珀色文字 + 图标。
- 移除 `.content-overview-metrics`（被总量行 + 图例取代；仅本组件使用）。

## 测试（vitest + @testing-library/react，沿用现有测试文件）

保留（aria-label 全部不变，预期原样通过）：

- 全部点击回调断言（查看全部 / 已确认 / 待确认 / 待同步 / 同步失败 /
  代码过期 / 最近条目）。
- 加载态、错误态、只读徽标、零态文案测试。

更新：

- `getByText("4 条")` 适配总量行新结构（数字与单位分元素，改为在总量行
  容器上断言 textContent 或分别断言）。

新增：

- 占比条 `role="img"` 的 aria-label 内容（Memory 与 Knowhow 各一）。
- 分段宽度百分比正确（用测试夹具：Memory 2/4=50%、2/4=50%；Knowhow
  已同步 0%、待同步 1/3、失败 2/3）。
- 总量为 0 时不渲染占比条。
- 段和超总量时归一化（宽度合计 = 100%）。

## 视觉验证

实现后启动前端，打开看板弹窗截图核对：两卡等高对齐、占比条与图例对齐、
圆角/间距/字重与页面其余板块协调（项目 UI 精致度标准）。

## 交付与流程

- worktree：`.claude/worktrees/content-assets-visual-upgrade`，分支
  `worktree-content-assets-visual-upgrade`（自 origin/master 切出，保持线性）。
- 完成后 rebase 到 master、push、`gh pr create --base master`。
