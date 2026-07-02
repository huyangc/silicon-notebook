# Source Stack 侧栏可收起 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** notebook 工作区左侧「Source Stack」栏可收起/展开,收起后右侧对话区铺满整页(Gemini 式边缘 hover 把手 + localStorage 持久化)。

**Architecture:** 一个持久化布尔 `sourcesCollapsed` 给 `.workspace-grid` 加 `sources-collapsed` 修饰类;CSS 切网格列宽(左列→0)并隐藏来源面板、右列铺满;展开态右边缘 hover 收起钮、收起态左侧常驻细 rail hover 展开钮。纯前端展示状态,无后端。

**Tech Stack:** Next.js(client component)+ React `useState`/`useEffect` + localStorage;lucide-react 图标;CSS(globals.css)。

## Global Constraints

- 只作用于 notebook 工作区的 `.workspace-grid`(問答/知識庫);KG 视图不受影响。
- 移动端 `@media (max-width: 760px)`(现有堆叠布局)**不启用收起**:中和 `.sources-collapsed` 列覆盖、隐藏两个把手。`@media (max-width: 1100px)`(仍两列)收起正常工作。
- localStorage key `sn.sourcesCollapsed`(`"1"`/`"0"`);读写包 try/catch,失败静默降级为默认展开。
- 图标:lucide-react 的 `PanelLeftClose`(收起)/ `PanelLeftOpen`(展开),加入现有 `lucide-react` import 行。
- 把手对齐/圆钮精致,符合项目 UI 对齐标准;中文文案 `收起来源栏`/`展开来源栏`。
- 前端从 `frontend/` 跑:`cd frontend && npx tsc --noEmit`、`npm test`。

---

### Task 1: 收起/展开侧栏(状态 + 持久化 + 把手 + CSS)

**Files:**
- Modify: `frontend/app/page.tsx`(import;`sourcesCollapsed` state + 两个持久化 effect;`workspace-grid` 类;收起把手 + 收起态 rail 标记)
- Modify: `frontend/app/globals.css`(`.workspace-grid` 过渡/relative;`.sources-collapsed` 列切换 + 面板隐藏;`.sources-collapse-handle`;`.sources-reveal-rail`;两处 media query 守卫)

**Interfaces:**
- Produces: 无对外接口(纯 UI 状态)。

- [ ] **Step 1: import 加两个图标**

`frontend/app/page.tsx:4` 的 lucide-react import 里,在 `PanelRightClose,` 前后加入 `PanelLeftClose, PanelLeftOpen,`。例如把 `... Network, PanelRightClose, Plus, ...` 改为 `... Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, Plus, ...`(保持字母序不强制,能编译即可)。

- [ ] **Step 2: 加 state + 持久化 effect**

在 `page.tsx` 组件内(与其它 `useState` 相邻,如 `sourcesTotal`/`sourcesPage` 附近)加:

```tsx
  const [sourcesCollapsed, setSourcesCollapsed] = useState(false);
```

在组件内(与其它 `useEffect` 相邻)加读/写两个 effect:

```tsx
  // 侧栏收起状态持久化(localStorage;隐私模式等读写失败静默降级)
  useEffect(() => {
    try {
      if (window.localStorage.getItem("sn.sourcesCollapsed") === "1") setSourcesCollapsed(true);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    try { window.localStorage.setItem("sn.sourcesCollapsed", sourcesCollapsed ? "1" : "0"); } catch { /* ignore */ }
  }, [sourcesCollapsed]);
```

- [ ] **Step 3: workspace-grid 加条件类 + 把手标记**

`page.tsx:3001` 把 `<section className="workspace-grid">` 改为:

```tsx
          <section className={`workspace-grid${sourcesCollapsed ? " sources-collapsed" : ""}`}>
```

在其内、`<aside className="workspace-panel sources-panel">`(3002)**开标签之后、`<div className="workspace-panel-header">`(3003)之前**插入收起把手:

```tsx
              <button
                type="button"
                className="sources-collapse-handle"
                aria-label="收起来源栏"
                title="收起来源栏"
                onClick={() => setSourcesCollapsed(true)}
              >
                <PanelLeftClose size={16} />
              </button>
```

在 `</aside>`(sources-panel 闭合)之后、`<section className="workspace-panel chat-panel">`(3158)之前,插入收起态展开 rail:

```tsx
            {sourcesCollapsed && (
              <button
                type="button"
                className="sources-reveal-rail"
                aria-label="展开来源栏"
                title="展开来源栏"
                onClick={() => setSourcesCollapsed(false)}
              >
                <PanelLeftOpen size={16} />
              </button>
            )}
```

- [ ] **Step 4: CSS —— 网格切换 + 面板隐藏 + 两个把手**

`frontend/app/globals.css` 的 `.workspace-grid`(701)块加 `position: relative;` 与过渡;并在其后新增规则。把 701-708 块改为:

```css
.workspace-grid {
  display: grid;
  grid-template-columns: minmax(270px, 25%) minmax(0, 1fr);
  gap: 18px;
  padding: 18px 24px 24px;
  height: calc(100vh - 72px);
  min-height: 0;
  position: relative;
  transition: grid-template-columns 200ms ease;
}
```

`.sources-panel`(746)块加 `position: relative;`(供收起把手绝对定位锚定):把

```css
.sources-panel {
  grid-template-rows: 64px 1fr;
}
```

改为

```css
.sources-panel {
  grid-template-rows: 64px 1fr;
  position: relative;
}
```

在 `.sources-panel` 相关规则之后(如 `.sources-body` 块后)新增:

```css
/* 侧栏收起把手(展开态,右边缘,hover 浮出) */
.sources-collapse-handle {
  position: absolute;
  top: 14px;
  right: 10px;
  z-index: 3;
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(34, 48, 68, 0.14);
  border-radius: 8px;
  background: #fff;
  color: #475467;
  cursor: pointer;
  opacity: 0.32;
  transition: opacity 140ms ease, background 140ms ease, color 140ms ease;
}
.sources-panel:hover .sources-collapse-handle { opacity: 1; }
.sources-collapse-handle:hover { background: #f3f5f9; color: #101828; }

/* 收起态:左列压到 0,来源面板隐藏,对话区铺满 */
.workspace-grid.sources-collapsed { grid-template-columns: 0 minmax(0, 1fr); }
.workspace-grid.sources-collapsed .sources-panel {
  width: 0;
  min-width: 0;
  padding: 0;
  border: 0;
  box-shadow: none;
  opacity: 0;
  pointer-events: none;
  overflow: hidden;
}

/* 展开 rail(收起态,左边缘常驻,hover 浮出圆钮) */
.sources-reveal-rail {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 16px;
  z-index: 4;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  padding: 0;
  cursor: pointer;
  background: linear-gradient(to right, rgba(34, 48, 68, 0.06), transparent);
}
.sources-reveal-rail > svg {
  width: 26px;
  height: 26px;
  padding: 4px;
  border: 1px solid rgba(34, 48, 68, 0.14);
  border-radius: 8px;
  background: #fff;
  color: #475467;
  box-shadow: 0 6px 18px rgba(31, 42, 58, 0.12);
  opacity: 0;
  transform: translateX(-4px);
  transition: opacity 140ms ease, transform 140ms ease;
}
.sources-reveal-rail:hover > svg { opacity: 1; transform: translateX(0); }
```

- [ ] **Step 5: CSS —— 移动端守卫(760px 断点中和收起)**

在 `@media (max-width: 760px)`(globals.css:3135)块内(该块已含 `.workspace-grid { grid-template-columns: 1fr; ... }`)追加:

```css
  .workspace-grid.sources-collapsed { grid-template-columns: 1fr; }
  .workspace-grid.sources-collapsed .sources-panel {
    width: auto;
    min-width: 0;
    padding: 0;
    border: 1px solid rgba(34, 48, 68, 0.12);
    box-shadow: 0 16px 40px rgba(31, 42, 58, 0.08);
    opacity: 1;
    pointer-events: auto;
  }
  .sources-collapse-handle,
  .sources-reveal-rail { display: none; }
```

（说明:`@media (max-width: 1100px)` 那块**不改**——那里仍是两列,`.sources-collapsed`(更高特异度)收起照常生效。）

- [ ] **Step 6: tsc + 现有测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误(exit 0)。

Run: `cd frontend && npm test --silent 2>&1 | tail -6`
Expected: 全绿。

- [ ] **Step 7: Commit**

```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(ui): Source Stack 侧栏可收起/展开(对话区铺满,localStorage 持久)"
```

- [ ] **Step 8: 视觉验证(控制器执行)**

preview 打开某 notebook 問答页:①hover 左栏右边缘 → 浮出「«」收起钮;②点击 → 左栏收起、对话区铺满、200ms 过渡;③收起态左边缘细 rail,hover → 浮出「»」;④点击 → 复原;⑤刷新页面保持收起态;⑥窄到 ≤760px → 把手消失、布局同现状(堆叠)。截图给用户。

---

## Self-Review 记录

- **Spec 覆盖**:状态+持久化=Step 2;布局切换=Step 3/4;收起把手=Step 3/4;展开 rail=Step 3/4;移动端守卫=Step 5;测试/视觉=Step 6/8。无遗漏。
- **一致性**:类名 `sources-collapsed`/`sources-collapse-handle`/`sources-reveal-rail`、state `sourcesCollapsed`、key `sn.sourcesCollapsed`、图标 `PanelLeftClose`/`PanelLeftOpen` 跨步一致。
- **无占位符**:每步含完整代码/确切命令/预期。
- **YAGNI**:无键盘快捷键、无多面板通用化;仅本栏收起。
