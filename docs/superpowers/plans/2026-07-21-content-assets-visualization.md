# 内容资产板块可视化升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把看板弹窗「内容资产」板块从纯文字数字升级为「图标块 + 大号总量 + 分段占比条 + 可点击图例」的视觉形态，交互与数据契约不变。

**Architecture:** 纯前端改动，只动三个文件：组件 `content-overview-cards.tsx`（结构重排）、`globals.css`（`.content-overview*` 样式块重写）、组件测试。无后端/API/类型改动，无新依赖（图标用已有 `lucide-react`，图形用纯 CSS）。

**Tech Stack:** Next.js 15 / React 19 / TypeScript / vitest + @testing-library/react / 全局 CSS（无 Tailwind）。

**Worktree:** `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade`，分支 `worktree-content-assets-visual-upgrade`。所有命令在 worktree 内执行。前端命令的工作目录是 `<worktree>/frontend`。

**Spec:** `docs/superpowers/specs/2026-07-21-content-assets-visualization-design.md`（已提交，用户已批准）。

## Global Constraints

- **aria-label 逐字保留**（现有测试与可访问性契约依赖）：
  `查看全部记忆`、`查看全部 Knowhow 表`、`查看 {N} 条已确认记忆`、`查看 {N} 条待确认记忆`、`查看 {N} 张待同步表`、`查看 {N} 张同步失败表`、`查看含 {N} 个过期代码单元格的表`、`打开记忆 {title}`、`打开 Knowhow 表 {title}`。
- 状态文案逐字保留：`正在加载内容资产…`、`内容资产暂时不可用`、`还没有已保存的记忆`、`还没有 Knowhow 表`、`只读`。
- 语义色用 index-tone 色板字面值：绿 `#1a7f5a`、黄 `#b97a00`、红 `#b42318`；中性灰用 `var(--line)`；图标块用 `var(--soft)` 底 + `var(--ink)` 图标（语义色只表状态，不表身份）。
- 占比条高 4px；分段宽度 = `段值 / max(total, 各段之和) * 100%`（脏数据永不溢出 100%）；总量为 0 不渲染占比条与图例。
- 数字不加千分位；单位格式 `countLabel(count, unit)` = `` `${count} ${unit}` `` 不变。
- 不引入图表库；不改动 `page.tsx`、后端、API、类型定义；组件 props 签名不变。
- 交互语言：UI 文案中文。
- **禁止**在 worktree 里跑 `npm install`（node_modules 是指向主 checkout 的软链，会写穿）。

---

### Task 1: 组件结构升级（TSX + 测试）

**Files:**
- Modify: `frontend/app/content-overview-cards.tsx`（整体重写，145 行 → 约 230 行）
- Test: `frontend/app/content-overview-cards.component.test.tsx`

**Interfaces:**
- Consumes: 现有 `NotebookContentOverview`（`workspace-model.ts:100-115`）、`KnowhowHealthFilter`（`knowhow-model.ts`）、`lucide-react` 导出的 `Brain` / `Table2` / `AlertTriangle`（0.468.0 均存在，已核实）。
- Produces（Task 2 的 CSS 按这些类名写样式，逐字一致）：
  `.content-overview-ic`、`.content-overview-total`（含 `strong` 与 `.content-overview-total-sub`）、`.content-overview-bar`（子元素 `<span class="seg-ok|seg-warn|seg-danger|seg-neutral">`，宽度为内联 style 百分比）、`.content-overview-legend`（含 `.legend-static` 与 `.content-overview-dot.dot-ok|dot-warn|dot-danger`）、`.content-overview-stale`。
  导出辅助组件 `ProportionBar`（props: `segments: BarSegment[]`、`total: number`、`label: string`）与类型 `BarSegment = { key: string; value: number; tone: "ok" | "warn" | "danger" | "neutral" }`，均不导出到模块外。

- [ ] **Step 1: 更新测试文件（先写失败测试）**

把 `frontend/app/content-overview-cards.component.test.tsx` 整体替换为：

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { ContentOverviewCards } from "./content-overview-cards";
import type { NotebookContentOverview } from "./workspace-model";

const overview: NotebookContentOverview = {
  memory: {
    total: 4,
    confirmed: 2,
    candidate: 2,
    recent: [
      { id: "m1", title: "Stable memory", status: "confirmed", updated_at: "2026-07-21T08:00:00Z" },
      { id: "m2", title: "Candidate memory", status: "candidate", updated_at: "2026-07-20T08:00:00Z" },
      { id: "m3", title: "Third memory", status: "confirmed", updated_at: "2026-07-19T08:00:00Z" },
      { id: "m4", title: "Not shown memory", status: "candidate", updated_at: "2026-07-18T08:00:00Z" },
    ],
  },
  knowhow: {
    table_count: 3,
    row_count: 12,
    projection_pending: 1,
    projection_failed: 2,
    stale_code_count: 3,
    recent_tables: [
      { id: "t1", title: "Bring-up", row_count: 4, last_activity_at: "2026-07-21T08:00:00Z" },
      { id: "t2", title: "Validation", row_count: 3, last_activity_at: "2026-07-20T08:00:00Z" },
      { id: "t3", title: "Release", row_count: 5, last_activity_at: "2026-07-19T08:00:00Z" },
      { id: "t4", title: "Not shown table", row_count: 1, last_activity_at: "2026-07-18T08:00:00Z" },
    ],
  },
};

const emptyOverview: NotebookContentOverview = {
  memory: { total: 0, confirmed: 0, candidate: 0, recent: [] },
  knowhow: {
    table_count: 0,
    row_count: 0,
    projection_pending: 0,
    projection_failed: 0,
    stale_code_count: 0,
    recent_tables: [],
  },
};

function renderCards(overrides: Partial<React.ComponentProps<typeof ContentOverviewCards>> = {}) {
  const onOpenMemory = vi.fn();
  const onOpenKnowhow = vi.fn();
  const { container } = render(
    <ContentOverviewCards
      overview={overview}
      loading={false}
      error=""
      readOnly={false}
      onOpenMemory={onOpenMemory}
      onOpenKnowhow={onOpenKnowhow}
      {...overrides}
    />,
  );
  return { onOpenMemory, onOpenKnowhow, container };
}

test("renders content asset metrics, recent items, and navigation callbacks", () => {
  const { onOpenMemory, onOpenKnowhow, container } = renderCards();

  expect(screen.getByRole("heading", { name: "内容资产" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Memory" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Knowhow" })).toBeInTheDocument();

  const totals = container.querySelectorAll(".content-overview-total");
  expect(totals[0].textContent).toBe("4 条");
  expect(totals[1].textContent).toContain("3 张表");
  expect(totals[1].textContent).toContain("12 行");

  expect(screen.getByText("已同步 0 张")).toBeInTheDocument();
  expect(screen.getByText("同步失败 2 张")).toBeInTheDocument();
  expect(screen.getByText("代码过期 3 格")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "打开记忆 Not shown memory" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "打开 Knowhow 表 Not shown table" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "查看全部记忆" }));
  expect(onOpenMemory).toHaveBeenCalledWith(null, null);
  fireEvent.click(screen.getByRole("button", { name: "查看 2 条已确认记忆" }));
  expect(onOpenMemory).toHaveBeenCalledWith("confirmed", null);
  fireEvent.click(screen.getByRole("button", { name: "查看 2 条待确认记忆" }));
  expect(onOpenMemory).toHaveBeenCalledWith("candidate", null);
  fireEvent.click(screen.getByRole("button", { name: "打开记忆 Stable memory" }));
  expect(onOpenMemory).toHaveBeenCalledWith(null, "m1");
  fireEvent.click(screen.getByRole("button", { name: "查看全部 Knowhow 表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("all", null);
  fireEvent.click(screen.getByRole("button", { name: "查看 1 张待同步表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("projection_pending", null);
  fireEvent.click(screen.getByRole("button", { name: "查看 2 张同步失败表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("projection_failed", null);
  fireEvent.click(screen.getByRole("button", { name: "查看含 3 个过期代码单元格的表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("stale_code", null);
  fireEvent.click(screen.getByRole("button", { name: "打开 Knowhow 表 Bring-up" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("all", "t1");
});

test("renders proportion bars with accessible labels and normalized widths", () => {
  renderCards();

  const memoryBar = screen.getByRole("img", { name: "已确认 2 条，待确认 2 条" });
  const memorySegments = memoryBar.querySelectorAll("span");
  expect(memorySegments).toHaveLength(2);
  expect(parseFloat(memorySegments[0].style.width)).toBeCloseTo(50, 1);
  expect(parseFloat(memorySegments[1].style.width)).toBeCloseTo(50, 1);

  const knowhowBar = screen.getByRole("img", {
    name: "已同步 0 张，待同步 1 张，同步失败 2 张",
  });
  const knowhowSegments = knowhowBar.querySelectorAll("span");
  expect(knowhowSegments).toHaveLength(3);
  expect(parseFloat(knowhowSegments[0].style.width)).toBeCloseTo(0, 1);
  expect(parseFloat(knowhowSegments[1].style.width)).toBeCloseTo(33.3, 1);
  expect(parseFloat(knowhowSegments[2].style.width)).toBeCloseTo(66.7, 1);
});

test("normalizes segment widths when parts exceed the total", () => {
  renderCards({
    overview: {
      ...overview,
      memory: { total: 1, confirmed: 2, candidate: 2, recent: [] },
      knowhow: {
        ...overview.knowhow,
        table_count: 3,
        projection_pending: 2,
        projection_failed: 2,
      },
    },
  });

  const memoryBar = screen.getByRole("img", { name: "已确认 2 条，待确认 2 条" });
  const memoryWidths = [...memoryBar.querySelectorAll("span")].map((seg) =>
    parseFloat(seg.style.width),
  );
  expect(memoryWidths.reduce((acc, w) => acc + w, 0)).toBeCloseTo(100, 1);

  const knowhowBar = screen.getByRole("img", {
    name: "已同步 0 张，待同步 2 张，同步失败 2 张",
  });
  const knowhowWidths = [...knowhowBar.querySelectorAll("span")].map((seg) =>
    parseFloat(seg.style.width),
  );
  expect(knowhowWidths.reduce((acc, w) => acc + w, 0)).toBeCloseTo(100, 1);
});

test("hides proportion bars when totals are zero", () => {
  renderCards({ overview: emptyOverview });

  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.getByText("还没有已保存的记忆")).toBeInTheDocument();
  expect(screen.getByText("还没有 Knowhow 表")).toBeInTheDocument();
});

test("renders a section-only loading state", () => {
  renderCards({ overview: null, loading: true });

  expect(screen.getByText("正在加载内容资产…")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Memory" })).not.toBeInTheDocument();
});

test("renders a section-only failure state", () => {
  renderCards({ overview: null, error: "network unavailable" });

  expect(screen.getByText("内容资产暂时不可用")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Knowhow" })).not.toBeInTheDocument();
});

test("keeps navigation enabled in read-only mode without edit controls", () => {
  renderCards({ readOnly: true });

  expect(screen.getByText("只读")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看 2 条已确认记忆" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "查看 1 张待同步表" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: /新建|编辑|删除/ })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade/frontend && npx vitest run app/content-overview-cards.component.test.tsx`
Expected: FAIL — `Unable to find role "img"`、`querySelectorAll(".content-overview-total")` 长度断言失败、`getByText("已同步 0 张")` 找不到等（旧组件没有这些结构）。

- [ ] **Step 3: 重写组件**

把 `frontend/app/content-overview-cards.tsx` 整体替换为：

```tsx
import { AlertTriangle, Brain, Table2 } from "lucide-react";

import type { KnowhowHealthFilter } from "./knowhow-model";
import type { NotebookContentOverview } from "./workspace-model";

export type ContentOverviewCardsProps = {
  overview: NotebookContentOverview | null;
  loading: boolean;
  error: string;
  readOnly: boolean;
  onOpenMemory: (
    status: "candidate" | "confirmed" | null,
    itemId: string | null,
  ) => void;
  onOpenKnowhow: (
    filter: KnowhowHealthFilter,
    tableId: string | null,
  ) => void;
};

function countLabel(count: number, unit: string): string {
  return `${count} ${unit}`;
}

type BarSegment = {
  key: string;
  value: number;
  tone: "ok" | "warn" | "danger" | "neutral";
};

function ProportionBar({
  segments,
  total,
  label,
}: {
  segments: BarSegment[];
  total: number;
  label: string;
}) {
  const sum = segments.reduce((acc, seg) => acc + seg.value, 0);
  const denom = Math.max(total, sum, 1);
  return (
    <div className="content-overview-bar" role="img" aria-label={label}>
      {segments.map((seg) => (
        <span
          key={seg.key}
          className={`seg-${seg.tone}`}
          aria-hidden="true"
          style={{ width: `${(seg.value / denom) * 100}%` }}
        />
      ))}
    </div>
  );
}

export function ContentOverviewCards({
  overview,
  loading,
  error,
  readOnly,
  onOpenMemory,
  onOpenKnowhow,
}: ContentOverviewCardsProps) {
  if (loading) {
    return (
      <section className="content-overview" aria-labelledby="content-overview-heading">
        <h2 id="content-overview-heading">内容资产</h2>
        <p className="content-overview-state" role="status">正在加载内容资产…</p>
      </section>
    );
  }

  if (error || !overview) {
    return (
      <section className="content-overview" aria-labelledby="content-overview-heading">
        <h2 id="content-overview-heading">内容资产</h2>
        <p className="content-overview-state" role="alert">内容资产暂时不可用</p>
      </section>
    );
  }

  const { memory, knowhow } = overview;

  const memoryRemainder = memory.total - memory.confirmed - memory.candidate;
  const memorySegments: BarSegment[] = [
    { key: "confirmed", value: memory.confirmed, tone: "ok" },
    { key: "candidate", value: memory.candidate, tone: "warn" },
  ];
  if (memoryRemainder > 0) {
    memorySegments.push({ key: "other", value: memoryRemainder, tone: "neutral" });
  }

  const knowhowSynced = Math.max(
    0,
    knowhow.table_count - knowhow.projection_pending - knowhow.projection_failed,
  );
  const knowhowSegments: BarSegment[] = [
    { key: "synced", value: knowhowSynced, tone: "ok" },
    { key: "pending", value: knowhow.projection_pending, tone: "warn" },
    { key: "failed", value: knowhow.projection_failed, tone: "danger" },
  ];

  return (
    <section className="content-overview" aria-labelledby="content-overview-heading">
      <h2 id="content-overview-heading">内容资产</h2>
      <div className="content-overview-grid">
        <article className="content-overview-card" aria-labelledby="content-overview-memory">
          <div className="content-overview-card-head">
            <span className="content-overview-ic" aria-hidden="true">
              <Brain size={17} />
            </span>
            <h3 id="content-overview-memory">Memory</h3>
            <button type="button" aria-label="查看全部记忆" onClick={() => onOpenMemory(null, null)}>
              查看全部
            </button>
          </div>
          <p className="content-overview-total">
            <strong>{memory.total}</strong> 条
          </p>
          {memory.total > 0 ? (
            <>
              <ProportionBar
                segments={memorySegments}
                total={memory.total}
                label={`已确认 ${countLabel(memory.confirmed, "条")}，待确认 ${countLabel(memory.candidate, "条")}`}
              />
              <div className="content-overview-legend">
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(memory.confirmed, "条")}已确认记忆`}
                  onClick={() => onOpenMemory("confirmed", null)}
                >
                  <span className="content-overview-dot dot-ok" aria-hidden="true" />
                  已确认 {countLabel(memory.confirmed, "条")}
                </button>
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(memory.candidate, "条")}待确认记忆`}
                  onClick={() => onOpenMemory("candidate", null)}
                >
                  <span className="content-overview-dot dot-warn" aria-hidden="true" />
                  待确认 {countLabel(memory.candidate, "条")}
                </button>
              </div>
            </>
          ) : null}
          {memory.recent.length > 0 ? (
            <div className="content-overview-recent" aria-label="最近记忆">
              {memory.recent.slice(0, 3).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  aria-label={`打开记忆 ${item.title}`}
                  onClick={() => onOpenMemory(null, item.id)}
                >
                  {item.title}
                </button>
              ))}
            </div>
          ) : <p className="content-overview-empty">还没有已保存的记忆</p>}
        </article>

        <article className="content-overview-card" aria-labelledby="content-overview-knowhow">
          <div className="content-overview-card-head">
            <span className="content-overview-ic" aria-hidden="true">
              <Table2 size={17} />
            </span>
            <h3 id="content-overview-knowhow">Knowhow</h3>
            {readOnly ? <span className="content-overview-read-only">只读</span> : null}
            <button type="button" aria-label="查看全部 Knowhow 表" onClick={() => onOpenKnowhow("all", null)}>
              查看全部
            </button>
          </div>
          <p className="content-overview-total">
            <strong>{knowhow.table_count}</strong> 张表
            <span className="content-overview-total-sub">· {countLabel(knowhow.row_count, "行")}</span>
          </p>
          {knowhow.table_count > 0 ? (
            <>
              <ProportionBar
                segments={knowhowSegments}
                total={knowhow.table_count}
                label={`已同步 ${countLabel(knowhowSynced, "张")}，待同步 ${countLabel(knowhow.projection_pending, "张")}，同步失败 ${countLabel(knowhow.projection_failed, "张")}`}
              />
              <div className="content-overview-legend">
                <span className="legend-static">
                  <span className="content-overview-dot dot-ok" aria-hidden="true" />
                  已同步 {countLabel(knowhowSynced, "张")}
                </span>
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(knowhow.projection_pending, "张")}待同步表`}
                  onClick={() => onOpenKnowhow("projection_pending", null)}
                >
                  <span className="content-overview-dot dot-warn" aria-hidden="true" />
                  待同步 {countLabel(knowhow.projection_pending, "张")}
                </button>
                <button
                  type="button"
                  aria-label={`查看 ${countLabel(knowhow.projection_failed, "张")}同步失败表`}
                  onClick={() => onOpenKnowhow("projection_failed", null)}
                >
                  <span className="content-overview-dot dot-danger" aria-hidden="true" />
                  同步失败 {countLabel(knowhow.projection_failed, "张")}
                </button>
              </div>
            </>
          ) : null}
          <button
            type="button"
            className="content-overview-stale"
            aria-label={`查看含 ${knowhow.stale_code_count} 个过期代码单元格的表`}
            onClick={() => onOpenKnowhow("stale_code", null)}
          >
            <AlertTriangle size={13} aria-hidden="true" />
            代码过期 {countLabel(knowhow.stale_code_count, "格")}
          </button>
          {knowhow.recent_tables.length > 0 ? (
            <div className="content-overview-recent" aria-label="最近 Knowhow 表">
              {knowhow.recent_tables.slice(0, 3).map((table) => (
                <button
                  key={table.id}
                  type="button"
                  aria-label={`打开 Knowhow 表 ${table.title}`}
                  onClick={() => onOpenKnowhow("all", table.id)}
                >
                  {table.title}
                </button>
              ))}
            </div>
          ) : <p className="content-overview-empty">还没有 Knowhow 表</p>}
        </article>
      </div>
    </section>
  );
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade/frontend && npx vitest run app/content-overview-cards.component.test.tsx`
Expected: PASS — 7 tests passed。

- [ ] **Step 5: 类型检查**

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade/frontend && npm run lint`
Expected: `tsc --noEmit` 无输出、退出码 0。

- [ ] **Step 6: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade
git add frontend/app/content-overview-cards.tsx frontend/app/content-overview-cards.component.test.tsx
git commit -m "feat(frontend): restructure content overview cards with proportion bars

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: globals.css 样式重写 + 视觉验证

**Files:**
- Modify: `frontend/app/globals.css:38-130`（整个 `.content-overview*` 块替换；其余行不动）

**Interfaces:**
- Consumes: Task 1 产出的全部类名（`.content-overview-ic`、`.content-overview-total`、`.content-overview-total-sub`、`.content-overview-bar`、`seg-ok|seg-warn|seg-danger|seg-neutral`、`.content-overview-legend`、`.legend-static`、`.content-overview-dot`、`dot-ok|dot-warn|dot-danger`、`.content-overview-stale`）。`.content-overview-metrics` 已被组件移除，其样式一并删除。
- Produces: 无（叶子任务）。

- [ ] **Step 1: 替换样式块**

在 `frontend/app/globals.css` 中，把第 38-130 行（从 `.content-overview {` 到 `@media (max-width: 760px) { ... }` 结束，即 `.hidden {` 之前的整个块）替换为：

```css
.content-overview {
  display: grid;
  gap: 12px;
}

.content-overview > h2,
.content-overview-card h3 {
  margin: 0;
}

.content-overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.content-overview-card {
  min-width: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 14px;
  background: var(--panel);
}

.content-overview-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.content-overview-card-head button {
  margin-left: auto;
}

.content-overview-ic {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: none;
  color: var(--ink);
  background: var(--soft);
}

.content-overview-total {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 13px;
}

.content-overview-total strong {
  margin-right: 2px;
  color: var(--ink);
  font-size: 28px;
  font-weight: 700;
  line-height: 1.1;
}

.content-overview-total-sub {
  margin-left: 6px;
}

.content-overview-bar {
  display: flex;
  height: 4px;
  margin-top: 10px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--soft);
}

.content-overview-bar > span {
  display: block;
  height: 100%;
}

.content-overview-bar .seg-ok,
.content-overview-legend .dot-ok {
  background: #1a7f5a;
}

.content-overview-bar .seg-warn,
.content-overview-legend .dot-warn {
  background: #b97a00;
}

.content-overview-bar .seg-danger,
.content-overview-legend .dot-danger {
  background: #b42318;
}

.content-overview-bar .seg-neutral {
  background: var(--line);
}

.content-overview-legend {
  display: flex;
  align-items: center;
  gap: 6px 12px;
  flex-wrap: wrap;
  margin-top: 10px;
  color: var(--muted);
  font-size: 12px;
}

.content-overview-legend button,
.content-overview-legend .legend-static {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.content-overview-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  flex: none;
}

.content-overview-card button {
  border: 0;
  border-radius: 6px;
  padding: 4px 6px;
  color: var(--blue);
  background: transparent;
}

.content-overview-card button:hover,
.content-overview-card button:focus-visible {
  background: var(--soft);
}

.content-overview-card button.content-overview-stale {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  gap: 5px;
  margin-top: 10px;
  color: #b97a00;
  font-size: 12px;
}

.content-overview-recent {
  display: grid;
  gap: 4px;
  margin-top: auto;
  padding-top: 12px;
}

.content-overview-recent button {
  width: 100%;
  min-width: 0;
  overflow: hidden;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.content-overview-state,
.content-overview-empty {
  margin: 0;
  color: var(--muted);
}

.content-overview-empty {
  margin-top: auto;
  padding-top: 12px;
}

.content-overview-read-only {
  border-radius: 999px;
  padding: 2px 7px;
  color: var(--muted);
  background: var(--soft);
  font-size: 12px;
}

@media (max-width: 760px) {
  .content-overview-grid {
    grid-template-columns: 1fr;
  }
}
```

注意：`.content-overview-empty` 的两条规则都要保留（第一条管颜色/基 margin，第二条管卡片内底部对齐），顺序不能颠倒。

- [ ] **Step 2: 跑全量前端测试**

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade/frontend && npm test`
Expected: `test:node`（node --test）与 `test:component`（vitest）全部通过；内容资产 7 个测试仍全绿。

- [ ] **Step 3: 类型检查**

Run: `cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade/frontend && npm run lint`
Expected: 无输出、退出码 0。

- [ ] **Step 4: 静态预览页 + 无头 Chrome 截图视觉验证**

写 `/tmp/content-overview-preview.html`（不提交，仅验证用；class 名与组件输出逐字一致）：

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>content overview preview</title>
<link rel="stylesheet" href="file:///Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade/frontend/app/globals.css" />
<style>
  body { padding: 32px; }
  .preview-wrap { max-width: 640px; margin: 0 auto 32px; }
</style>
</head>
<body>
<div class="preview-wrap">
  <section class="content-overview" aria-labelledby="content-overview-heading">
    <h2 id="content-overview-heading">内容资产</h2>
    <div class="content-overview-grid">
      <article class="content-overview-card">
        <div class="content-overview-card-head">
          <span class="content-overview-ic" aria-hidden="true">🧠</span>
          <h3>Memory</h3>
          <button type="button">查看全部</button>
        </div>
        <p class="content-overview-total"><strong>128</strong> 条</p>
        <div class="content-overview-bar" role="img" aria-label="已确认 96 条，待确认 32 条">
          <span class="seg-ok" style="width: 75%"></span>
          <span class="seg-warn" style="width: 25%"></span>
        </div>
        <div class="content-overview-legend">
          <button type="button"><span class="content-overview-dot dot-ok"></span>已确认 96 条</button>
          <button type="button"><span class="content-overview-dot dot-warn"></span>待确认 32 条</button>
        </div>
        <div class="content-overview-recent">
          <button type="button">某条已确认的记忆标题，很长很长会被省略号截断的示例文本</button>
          <button type="button">另一条候选记忆</button>
        </div>
      </article>
      <article class="content-overview-card">
        <div class="content-overview-card-head">
          <span class="content-overview-ic" aria-hidden="true">📊</span>
          <h3>Knowhow</h3>
          <button type="button">查看全部</button>
        </div>
        <p class="content-overview-total"><strong>24</strong> 张表<span class="content-overview-total-sub">· 1,240 行</span></p>
        <div class="content-overview-bar" role="img" aria-label="已同步 20 张，待同步 3 张，同步失败 1 张">
          <span class="seg-ok" style="width: 83.3%"></span>
          <span class="seg-warn" style="width: 12.5%"></span>
          <span class="seg-danger" style="width: 4.2%"></span>
        </div>
        <div class="content-overview-legend">
          <span class="legend-static"><span class="content-overview-dot dot-ok"></span>已同步 20 张</span>
          <button type="button"><span class="content-overview-dot dot-warn"></span>待同步 3 张</button>
          <button type="button"><span class="content-overview-dot dot-danger"></span>同步失败 1 张</button>
        </div>
        <button type="button" class="content-overview-stale">⚠ 代码过期 5 格</button>
        <div class="content-overview-recent">
          <button type="button">PLL 锁定时间表</button>
          <button type="button">Bandgap 验证清单</button>
        </div>
      </article>
    </div>
  </section>
</div>
<div class="preview-wrap">
  <section class="content-overview">
    <h2>内容资产（零态 / 只读）</h2>
    <div class="content-overview-grid">
      <article class="content-overview-card">
        <div class="content-overview-card-head">
          <span class="content-overview-ic" aria-hidden="true">🧠</span>
          <h3>Memory</h3>
          <button type="button">查看全部</button>
        </div>
        <p class="content-overview-total"><strong>0</strong> 条</p>
        <p class="content-overview-empty">还没有已保存的记忆</p>
      </article>
      <article class="content-overview-card">
        <div class="content-overview-card-head">
          <span class="content-overview-ic" aria-hidden="true">📊</span>
          <h3>Knowhow</h3>
          <span class="content-overview-read-only">只读</span>
          <button type="button">查看全部</button>
        </div>
        <p class="content-overview-total"><strong>0</strong> 张表<span class="content-overview-total-sub">· 0 行</span></p>
        <button type="button" class="content-overview-stale">⚠ 代码过期 0 格</button>
        <p class="content-overview-empty">还没有 Knowhow 表</p>
      </article>
    </div>
  </section>
</div>
</body>
</html>
```

截图并查看：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --screenshot=/tmp/content-overview-preview.png --window-size=1400,1100 \
  file:///tmp/content-overview-preview.html
```

然后用 Read 工具查看 `/tmp/content-overview-preview.png`，逐项核对：
- 两卡等高、图标块/标题/查看全部一行对齐；
- 大数字与单位基线协调；占比条 4px 细条、圆角、分段色正确（绿/黄/红）；
- 图例色点与文字居中对齐、可点击项为蓝色；
- 警示行琥珀色；最近条目整宽、省略号截断；
- 零态卡片不显示占比条；「最近」区域在两卡中底部对齐。

如有明显视觉问题（错位、溢出、颜色错误），修正 CSS 后重截图，直到通过。

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/content-assets-visual-upgrade
git add frontend/app/globals.css
git commit -m "feat(frontend): style content overview cards with icon chips and segmented bars

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 记录

- Spec 覆盖：组件结构/占比条/图例/警示行（Task 1）、样式与视觉验证（Task 2）、状态与 aria 契约（Task 1 测试锁定）、宽度归一化与零态（Task 1 实现+测试）。README/CLI 不涉及（纯 UI 板块，无用户可运行脚本）。
- 占位符：无，所有步骤含完整代码与命令。
- 类型一致：`BarSegment`/`ProportionBar` 在 Task 1 定义并使用；Task 2 只消费 Task 1 列出的类名字符串，已逐字核对。
