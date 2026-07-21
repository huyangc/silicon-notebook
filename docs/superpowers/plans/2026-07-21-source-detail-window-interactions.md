# Source Detail Window Interactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the source-detail dialog a conventional close control and desktop header dragging without changing its content or source actions.

**Architecture:** Extract the modal shell into a focused `SourceDetailWindow` component that owns presentation and the existing `useFloatingWindow` integration. Keep source data, source actions, and detail-body content in `page.tsx`; pass them through as children so the workspace remains the state orchestrator.

**Tech Stack:** Next.js 15, React 19, TypeScript, lucide-react, Vitest, Testing Library, Node test runner, CSS.

## Global Constraints

- The close control uses Lucide `X`, `title="关闭"`, and `aria-label="关闭来源详情"`.
- Only the source-detail dialog becomes draggable; other utility modals are unchanged.
- Dragging starts only from the source-detail header and never from its close button.
- Reuse `useFloatingWindow({ storageKey: "source.detail.window", resizable: false })`; do not add resize UI or duplicate pointer-event geometry.
- Keep `.source-detail-body` scrolling, source reparse/delete actions, element rendering, and all backend behavior unchanged.
- The shared hook disables floating geometry at viewport widths of 720 px or less and keeps the header recoverable in wider viewports.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together for the user-visible interaction change.
- This is an interaction refinement to an already completed source-detail feature, so do not add a new completion claim to `fangan_done.md`.

## File Structure

- Create `frontend/app/source-detail-window.tsx`: accessible source-detail modal shell, close action, and floating-window integration.
- Create `frontend/app/source-detail-window.component.test.tsx`: component-level close and real pointer-drag regression coverage.
- Modify `frontend/app/page.tsx`: compose `SourceDetailWindow` around the existing detail content and remove `PanelRightClose`.
- Modify `frontend/app/workspace-layout.test.mjs`: semantic integration contract proving the workspace uses the extracted shell.
- Modify `README.md`, `README_zh.md`, and `AGENTS.md`: keep product-behavior documentation in sync.

---

### Task 1: Build the tested source-detail window shell

**Files:**
- Create: `frontend/app/source-detail-window.component.test.tsx`
- Create: `frontend/app/source-detail-window.tsx`

**Interfaces:**
- Consumes: `children: ReactNode`, `onClose: () => void`, and `useFloatingWindow(options)`.
- Produces: `SourceDetailWindow(props): JSX.Element`, an accessible dialog shell with a draggable header and fixed close semantics.

- [ ] **Step 1: Write the failing component tests**

Create `frontend/app/source-detail-window.component.test.tsx`:

```tsx
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { SourceDetailWindow } from "./source-detail-window";


class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;
  readonly pointerType: string;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 0;
    this.pointerType = init.pointerType ?? "";
  }
}


afterEach(() => {
  vi.unstubAllGlobals();
});


test("uses a conventional accessible close control", async () => {
  const onClose = vi.fn();
  const user = userEvent.setup();

  render(
    <SourceDetailWindow onClose={onClose}>
      <p>详情正文</p>
    </SourceDetailWindow>,
  );

  const dialog = screen.getByRole("dialog", { name: "来源" });
  expect(dialog).toHaveTextContent("详情正文");
  const close = screen.getByRole("button", { name: "关闭来源详情" });
  expect(close).toHaveAttribute("title", "关闭");
  await user.click(close);
  expect(onClose).toHaveBeenCalledTimes(1);
});


test("drags the dialog card from its header", () => {
  vi.stubGlobal("PointerEvent", TestPointerEvent);
  let pendingFrame: FrameRequestCallback | null = null;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    pendingFrame = callback;
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 142,
    y: 84,
    width: 740,
    height: 600,
    top: 84,
    right: 882,
    bottom: 684,
    left: 142,
    toJSON: () => ({}),
  });

  render(
    <SourceDetailWindow onClose={() => undefined}>
      <p>详情正文</p>
    </SourceDetailWindow>,
  );

  const dialog = screen.getByRole("dialog", { name: "来源" });
  const card = dialog.querySelector<HTMLElement>(".source-detail-card");
  const header = dialog.querySelector<HTMLElement>(".source-detail-shell-header");
  expect(card).not.toBeNull();
  expect(header).not.toBeNull();
  expect(header).toHaveStyle({ cursor: "grab" });
  expect(card).toHaveStyle({ transform: "translate3d(0px, 0px, 0)" });

  fireEvent.pointerDown(header!, {
    pointerId: 7,
    pointerType: "mouse",
    button: 0,
    clientX: 100,
    clientY: 100,
  });
  fireEvent.pointerMove(window, {
    pointerId: 7,
    pointerType: "mouse",
    clientX: 140,
    clientY: 125,
  });
  act(() => pendingFrame?.(0));

  expect(card).toHaveStyle({ transform: "translate3d(40px, 25px, 0)" });
  fireEvent.pointerUp(window, { pointerId: 7, pointerType: "mouse" });
});
```

- [ ] **Step 2: Run the component tests and verify RED**

Run:

```bash
cd frontend
npx vitest run app/source-detail-window.component.test.tsx
```

Expected: FAIL because `./source-detail-window` does not exist.

- [ ] **Step 3: Implement the minimal window shell**

Create `frontend/app/source-detail-window.tsx`:

```tsx
"use client";

import type { ReactNode } from "react";
import { X } from "lucide-react";

import { useFloatingWindow } from "./use-floating-window";


type SourceDetailWindowProps = {
  children: ReactNode;
  onClose: () => void;
};


export function SourceDetailWindow({ children, onClose }: SourceDetailWindowProps) {
  const floating = useFloatingWindow({
    storageKey: "source.detail.window",
    resizable: false,
  });

  return (
    <section
      className="utility-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-detail-window-title"
    >
      <div
        ref={floating.cardRef}
        className="utility-modal-card source-detail-card"
        style={floating.style}
      >
        <div className="source-detail-shell-header" {...floating.dragHandleProps}>
          <h2 id="source-detail-window-title">来源</h2>
          <button
            type="button"
            className="icon-button subtle-icon"
            onClick={onClose}
            title="关闭"
            aria-label="关闭来源详情"
          >
            <X size={22} />
          </button>
        </div>
        <div className="source-detail-body">{children}</div>
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Run the focused tests and TypeScript check**

Run:

```bash
cd frontend
npx vitest run app/source-detail-window.component.test.tsx
npm run lint
```

Expected: both component tests pass and `tsc --noEmit` exits 0.

- [ ] **Step 5: Commit the tested shell**

```bash
git add frontend/app/source-detail-window.tsx frontend/app/source-detail-window.component.test.tsx
git commit -m "feat(frontend): add draggable source detail window"
```

---

### Task 2: Integrate the shell into the notebook workspace

**Files:**
- Modify: `frontend/app/workspace-layout.test.mjs`
- Modify: `frontend/app/page.tsx:1-90,5008-5114`

**Interfaces:**
- Consumes: `SourceDetailWindow({ children, onClose })` from Task 1 and existing `sourceDetail`, `setSourceDetail`, source actions, and detail content.
- Produces: exactly one `SourceDetailWindow` composed by `Home`, with `onClose={() => setSourceDetail(null)}`.

- [ ] **Step 1: Write the failing semantic integration test**

Extend `frontend/app/workspace-layout.test.mjs`:

```js
test("source detail uses the dedicated draggable window shell", () => {
  assert.deepEqual(
    importsFrom(page, "./source-detail-window").map((item) => item.imported),
    ["SourceDetailWindow"],
  );
  const windows = jsxElements(page, "SourceDetailWindow");
  assert.equal(windows.length, 1);
  assert.deepEqual(windows[0].bindings, {
    onClose: "() => setSourceDetail(null)",
  });
  assert.equal(
    importsFrom(page, "lucide-react").some(({ imported }) => imported === "PanelRightClose"),
    false,
  );
});
```

- [ ] **Step 2: Run the layout test and verify RED**

Run:

```bash
cd frontend
node --test app/workspace-layout.test.mjs
```

Expected: FAIL because `page.tsx` does not import or render `SourceDetailWindow` and still imports `PanelRightClose`.

- [ ] **Step 3: Replace the source-detail modal shell**

In `frontend/app/page.tsx`:

1. Remove `PanelRightClose` from the `lucide-react` import and add:

```tsx
import { SourceDetailWindow } from "./source-detail-window";
```

2. Replace the current source-detail opening shell:

```tsx
        <section className="utility-modal" role="dialog" aria-modal="true">
          <div className="utility-modal-card source-detail-card">
            <div className="source-detail-shell-header">
              <h2>来源</h2>
              <button className="icon-button subtle-icon" onClick={() => setSourceDetail(null)} title="Close">
                <PanelRightClose size={22} />
              </button>
            </div>
            <div className="source-detail-body">
```

with:

```tsx
        <SourceDetailWindow onClose={() => setSourceDetail(null)}>
```

3. Leave every existing child from `<div className="source-detail-title-row">` through `<div className="source-element-stack">` unchanged, then replace the old three closing tags immediately before `)}` with:

```tsx
        </SourceDetailWindow>
```

- [ ] **Step 4: Run the component, integration, and TypeScript tests**

Run:

```bash
cd frontend
npx vitest run app/source-detail-window.component.test.tsx
node --test app/workspace-layout.test.mjs
npm run lint
```

Expected: two component tests and all workspace-layout tests pass; `tsc --noEmit` exits 0.

- [ ] **Step 5: Commit the workspace integration**

```bash
git add frontend/app/page.tsx frontend/app/workspace-layout.test.mjs
git commit -m "feat(frontend): enable source detail window dragging"
```

---

### Task 3: Synchronize behavior docs and run the complete gate

**Files:**
- Modify: `README.md:850`
- Modify: `README_zh.md:755`
- Modify: `AGENTS.md:74-78`

**Interfaces:**
- Consumes: the verified `SourceDetailWindow` behavior from Tasks 1-2.
- Produces: synchronized English, Chinese, and agent-contract documentation of the close and drag behavior.

- [ ] **Step 1: Update all three behavior documents**

Append this sentence to the source-detail paragraph in `README.md`:

```markdown
On desktop, the source-detail window uses a conventional close control and can be dragged by its header; narrow screens keep the fixed modal layout, and the detail body remains independently scrollable.
```

Append the equivalent sentence to the source-detail paragraph in `README_zh.md`:

```markdown
桌面端的来源详情窗口使用常规关闭按钮，并可按住标题栏拖动；窄屏继续使用固定弹窗布局，详情正文保持独立滚动。
```

Add this nested source-detail requirement after the wrapping requirement in `AGENTS.md`:

```markdown
  - On desktop, source detail uses a conventional close control and supports dragging by the header; keep narrow-screen geometry fixed and the detail body independently scrollable.
```

- [ ] **Step 2: Verify documentation parity and focused frontend behavior**

Run:

```bash
rg -n "conventional close control|常规关闭按钮|supports dragging by the header" README.md README_zh.md AGENTS.md
cd frontend
npx vitest run app/source-detail-window.component.test.tsx
node --test app/workspace-layout.test.mjs
npm run lint
npm run build
```

Expected: all three documentation phrases are found; focused tests pass; type checking and the production build exit 0.

- [ ] **Step 3: Run the repository gate**

Run from the repository root:

```bash
scripts/check.sh
```

Expected: exit 0 with no failed backend, frontend, contract, or smoke checks.

- [ ] **Step 4: Inspect the final diff against the approved design**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; only the planned component, tests, workspace integration, and synchronized documentation are changed.

- [ ] **Step 5: Commit synchronized documentation**

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: document draggable source detail window"
```
