# Ask Session Header Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Ask workspace's duplicated two-row conversation controls with one header row containing `历史 N` and a direct `+` new-session action.

**Architecture:** Extract the two Ask-only header actions into a focused, interaction-tested React component and keep all conversation state in `page.tsx`. Integrate the component into the existing workspace header, remove the obsolete session-context row, and move the existing absolute-positioned session manager up beneath the 64px header without changing backend or persistence behavior.

**Tech Stack:** Next.js 15, React 19, TypeScript, lucide-react, Vitest, Testing Library, Node test runner, CSS.

## Global Constraints

- `+` calls the existing `startNewSession` directly and never opens the old confirmation modal.
- `历史 N` is the only persistent entry point for the existing session manager and displays `sessions.length`.
- Keep session switching, current-session highlighting, rename, delete, bulk cleanup, and the manager's internal new-session card unchanged.
- Do not change backend routes, API models, conversation persistence, Ask modes, reasoning progress, citations, or composer behavior.
- Keep `frontend/app/page.tsx` as the workspace orchestrator; the extracted header component owns presentation only.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together, then update `fangan_done.md` only after `scripts/check.sh` has passed for the implementation.
- Execute implementation in a new `codex/ask-session-header` worktree branch with task-scoped implementation, specification review, and code-quality review.
- Do not test CSS geometry or read production source directly; use the shared semantic-source adapter for structural contracts and browser acceptance for geometry.
- Finish the verified feature by merging the latest `master`, re-running the complete gate, pushing the branch, and opening a pull request.

## File Structure

- Create `frontend/app/ask-session-header.tsx`: presentational Ask header actions with explicit callbacks.
- Create `frontend/app/ask-session-header.component.test.tsx`: user-level interaction coverage for history and direct-new-session actions.
- Modify `frontend/app/page.tsx`: compose the new component, remove the duplicate second row and stale outside-click selector, and identify the manager for `aria-controls`.
- Modify `frontend/app/workspace-layout.test.mjs`: semantic integration contract for the header component and removed context row.
- Modify `frontend/app/globals.css`: collapse the chat grid to three rows, remove obsolete context-row rules, and re-anchor the manager on desktop and narrow screens.
- Modify `README.md`, `README_zh.md`, `AGENTS.md`, and `fangan_done.md`: document the verified single-row interaction.

---

### Task 1: Build the Ask header actions as an interaction-tested component

**Files:**
- Create: `frontend/app/ask-session-header.component.test.tsx`
- Create: `frontend/app/ask-session-header.tsx`

**Interfaces:**
- Consumes: `sessionCount: number`, `sessionPanelOpen: boolean`, `onToggleSessionPanel: () => void`, `onStartNewSession: () => void`.
- Produces: `AskSessionHeaderActions(props): JSX.Element`, a `历史 N` toggle controlling `ask-session-manager` and an accessible `+` button named `新会话`.

- [ ] **Step 1: Write the failing component test**

Create `frontend/app/ask-session-header.component.test.tsx`:

```tsx
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AskSessionHeaderActions } from "./ask-session-header";


afterEach(cleanup);


test("shows one history entry and starts a new session directly", async () => {
  const onToggleSessionPanel = vi.fn();
  const onStartNewSession = vi.fn();
  const user = userEvent.setup();

  render(
    <AskSessionHeaderActions
      sessionCount={14}
      sessionPanelOpen={false}
      onToggleSessionPanel={onToggleSessionPanel}
      onStartNewSession={onStartNewSession}
    />,
  );

  const history = screen.getByRole("button", { name: "历史 14" });
  expect(history).toHaveAttribute("aria-expanded", "false");
  expect(history).toHaveAttribute("aria-controls", "ask-session-manager");
  await user.click(history);
  expect(onToggleSessionPanel).toHaveBeenCalledTimes(1);

  const newSession = screen.getByRole("button", { name: "新会话" });
  expect(newSession).toHaveAttribute("title", "新会话");
  await user.click(newSession);
  expect(onStartNewSession).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Run the component test and verify RED**

Run:

```bash
cd frontend
npx vitest run app/ask-session-header.component.test.tsx
```

Expected: FAIL because `./ask-session-header` does not exist.

- [ ] **Step 3: Implement the minimal header component**

Create `frontend/app/ask-session-header.tsx`:

```tsx
"use client";

import { MessageSquareText, Plus } from "lucide-react";


type AskSessionHeaderActionsProps = {
  sessionCount: number;
  sessionPanelOpen: boolean;
  onToggleSessionPanel: () => void;
  onStartNewSession: () => void;
};


export function AskSessionHeaderActions({
  sessionCount,
  sessionPanelOpen,
  onToggleSessionPanel,
  onStartNewSession,
}: AskSessionHeaderActionsProps) {
  return (
    <>
      <button
        className={`chat-session-toggle ${sessionPanelOpen ? "active" : ""}`}
        type="button"
        aria-expanded={sessionPanelOpen}
        aria-controls="ask-session-manager"
        onClick={onToggleSessionPanel}
      >
        <MessageSquareText size={15} />
        历史 {sessionCount}
      </button>
      <button
        className="icon-button compact"
        type="button"
        aria-label="新会话"
        title="新会话"
        onClick={onStartNewSession}
      >
        <Plus size={18} />
      </button>
    </>
  );
}
```

- [ ] **Step 4: Run the component test and TypeScript check**

Run:

```bash
cd frontend
npx vitest run app/ask-session-header.component.test.tsx
npm run lint
```

Expected: one component test passes and `tsc --noEmit` exits 0.

- [ ] **Step 5: Commit the focused component**

```bash
git add frontend/app/ask-session-header.tsx frontend/app/ask-session-header.component.test.tsx
git commit -m "feat(frontend): add compact Ask session header actions"
```

---

### Task 2: Replace the duplicate row and re-anchor the session manager

**Files:**
- Modify: `frontend/app/workspace-layout.test.mjs`
- Modify: `frontend/app/page.tsx:1-90,1816-1846,4214-4271`
- Modify: `frontend/app/globals.css:1070-1180,4770-4805`

**Interfaces:**
- Consumes: `AskSessionHeaderActions` from Task 1 and existing `sessions`, `sessionPanelOpen`, `setSessionPanelOpen`, and `startNewSession` state/actions in `page.tsx`.
- Produces: one Ask header row; `div#ask-session-manager.chat-session-popover` anchored immediately below it.

- [ ] **Step 1: Write failing semantic integration assertions**

Extend the first test so the workspace must compose the new header component:

```js
  assert.deepEqual(
    importsFrom(page, "./ask-session-header").map((item) => item.imported),
    ["AskSessionHeaderActions"],
  );
  const sessionHeaders = jsxElements(page, "AskSessionHeaderActions");
  assert.equal(sessionHeaders.length, 1);
  assert.deepEqual(sessionHeaders[0].bindings, {
    sessionCount: "sessions.length",
    sessionPanelOpen: "sessionPanelOpen",
    onToggleSessionPanel: "() => setSessionPanelOpen(open => !open)",
    onStartNewSession: "startNewSession",
  });
```

Add a dedicated layout test:

```js
test("Ask session controls occupy one header row", () => {
  assert.equal(
    jsxElements(page, "div").some(
      ({ attributes }) => attributes.className === "chat-session-context",
    ),
    false,
  );
  assert.ok(
    jsxElements(page, "div").some(
      ({ attributes }) => (
        attributes.id === "ask-session-manager"
        && attributes.className === "chat-session-popover"
        && attributes.role === "dialog"
        && attributes["aria-label"] === "会话管理"
      ),
    ),
  );
});
```

- [ ] **Step 2: Run the layout test and verify RED**

Run:

```bash
cd frontend
node --test app/workspace-layout.test.mjs
```

Expected: FAIL because `page.tsx` does not import/render `AskSessionHeaderActions` and still renders `chat-session-context`.

- [ ] **Step 3: Integrate the new actions and remove the duplicate row**

In `frontend/app/page.tsx`:

1. Remove `MessageSquareText` from the `lucide-react` import and add:

```tsx
import { AskSessionHeaderActions } from "./ask-session-header";
```

2. Narrow the outside-click exemption and update its comment:

```tsx
  // 会话历史面板:点面板外部(或按 Esc)关闭。历史切换按钮排除在外——
  // 交给按钮自己的 onClick 切换,否则 pointerdown 先关、click 再开会「关了又开」。
```

```tsx
      if (target instanceof Element && target.closest(".chat-session-toggle")) {
        return;
      }
```

3. Replace the existing Ask `会话` button, vertical-dots confirmation button, and full `chat-session-context` block with:

```tsx
                <div className="chat-header-actions">
                  {chatMode === "ask" && (
                    <AskSessionHeaderActions
                      sessionCount={sessions.length}
                      sessionPanelOpen={sessionPanelOpen}
                      onToggleSessionPanel={() => setSessionPanelOpen((open) => !open)}
                      onStartNewSession={startNewSession}
                    />
                  )}
                </div>
```

4. Give the existing popover the id referenced by the history button:

```tsx
                <div
                  id="ask-session-manager"
                  className="chat-session-popover"
                  role="dialog"
                  aria-label="会话管理"
                  ref={sessionPopoverRef}
                >
```

- [ ] **Step 4: Collapse the CSS grid and move the manager up**

In `frontend/app/globals.css`, change the chat grid and popover geometry:

```css
.chat-panel {
  grid-template-rows: 64px minmax(0, 1fr) auto;
  border-top: 3px solid #0f6d7a;
  position: relative;
}
```

Delete the complete rules for `.chat-session-context`, `.chat-current-session`, `.chat-current-session span`, `.chat-current-session small`, `.chat-session-new`, and `.chat-session-toggle.slim`. Change the hover selector to:

```css
.chat-session-toggle.active,
.chat-session-toggle:hover {
  border-color: #9aa6b7;
  background: #f7f9fc;
}
```

Move the desktop popover beneath the header and reclaim the deleted row's vertical room:

```css
.chat-session-popover {
  position: absolute;
  z-index: 25;
  top: 64px;
  left: 18px;
  right: 18px;
  max-height: min(428px, calc(100% - 122px));
```

In the narrow-screen media query, delete all `.chat-session-context` and `.chat-current-session` overrides and set:

```css
  .chat-session-popover {
    top: 64px;
    left: 10px;
    right: 10px;
    max-height: min(468px, calc(100% - 112px));
  }
```

- [ ] **Step 5: Run targeted tests and verify GREEN**

Run:

```bash
cd frontend
node --test app/workspace-layout.test.mjs
npx vitest run app/ask-session-header.component.test.tsx
npm run lint
```

Expected: workspace layout tests and the header component test pass; TypeScript exits 0.

- [ ] **Step 6: Commit the integrated layout**

```bash
git add frontend/app/page.tsx frontend/app/globals.css frontend/app/workspace-layout.test.mjs
git commit -m "feat(frontend): merge Ask session controls into one row"
```

---

### Task 3: Synchronize documentation and run the complete verification gate

**Files:**
- Modify: `README.md:274`
- Modify: `README_zh.md:245`
- Modify: `AGENTS.md:83`
- Modify: `fangan_done.md:75`

**Interfaces:**
- Consumes: the verified one-row frontend behavior from Tasks 1-2.
- Produces: repository documentation that describes the shipped interaction in both languages and the completed-feature ledger.

- [ ] **Step 1: Verify the implementation before marking it complete**

Run from the repository root:

```bash
scripts/check.sh
cd frontend
npm run build
```

Expected: `scripts/check.sh` exits 0 and Next.js production build exits 0.

- [ ] **Step 2: Update all synchronized behavior documentation**

In `README.md`'s Main column bullet, add:

```markdown
Conversation history uses a single-row `历史 N` entry in the Ask header plus an expandable manager; the adjacent `+` starts a new session directly.
```

In `README_zh.md`'s 主栏 bullet, add:

```markdown
会话历史收进 Ask 顶栏的单行 `历史 N` 入口和可展开管理面板，旁边的 `+` 会直接开始新会话。
```

Replace the Ask conversation-history requirement in `AGENTS.md` with:

```markdown
  - Ask conversation history should use a single-row `历史 N` header entry and an expandable session manager; the adjacent `+` starts a new session directly. Do not restore a separate current-session context row or permanently split the constrained center panel.
```

Replace the corresponding Ask sentence in `fangan_done.md` with:

```markdown
  - 问答：自由提问走 `/ask`（已移除写死 scenario）；支持多个 conversation/session，会话历史通过 Ask 顶栏单行 `历史 N` 入口 + 可展开会话管理面板切换/新建/重命名/删除，旁边的 `+` 直接开始新会话，不再保留重复的当前会话上下文栏，避免压缩主问答区；欢迎区标题与 prompt chips 会根据 notebook 已导入来源的标题/摘要生成，并触发真实 ask。输入框支持 `Enter` 发送、`Shift+Enter` 换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制并恢复草稿问题。
```

- [ ] **Step 3: Run fresh final verification after the documentation ledger changes**

Run from the repository root:

```bash
git diff --check
scripts/check.sh
cd frontend
npm run build
```

Expected: no whitespace errors, all repository checks pass, and the production build exits 0.

- [ ] **Step 4: Perform visual acceptance in the local app**

From the repository root, run `npm run dev`, open `http://localhost:3000`, and enter a notebook's Ask tab. Verify at desktop and narrow widths:

1. Only one fixed header row remains above the answer body.
2. The right side reads `历史 N` followed by a `+` icon.
3. `历史 N` opens the manager directly beneath the header without covering the tabs.
4. `+` immediately returns the Ask body to the new-session welcome state.
5. The manager still switches, renames, deletes, and bulk-cleans sessions.

- [ ] **Step 5: Commit the synchronized documentation**

```bash
git add README.md README_zh.md AGENTS.md fangan_done.md
git commit -m "docs: describe single-row Ask session controls"
```

---

### Task 4: Final review and pull request

**Files:**
- Review: every file changed by Tasks 1-3
- Modify only if review or the latest `master` merge exposes a scoped defect

**Interfaces:**
- Consumes: the committed implementation and documentation from Tasks 1-3.
- Produces: a reviewed, up-to-date feature branch and a pull request targeting `master`.

- [ ] **Step 1: Run task-scoped specification and code-quality reviews**

Review the final diff against `docs/superpowers/specs/2026-07-21-ask-session-header-consolidation-design.md`. Confirm every confirmed interaction is present, no old second-row control remains, no backend/API behavior changed, and no unrelated file is modified. Then review component boundaries, accessibility names, callback wiring, CSS selectors, and test intent; fix any concrete issue and re-run the affected targeted tests before continuing.

- [ ] **Step 2: Merge the latest local `master` into the feature branch**

```bash
git fetch origin master
git merge --no-ff origin/master
```

Expected: a clean merge or explicitly resolved conflicts that preserve both newer `master` work and the single-row session controls.

- [ ] **Step 3: Run the fresh complete gate after the merge**

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: all backend, contract, frontend test/typecheck, and production-build lanes pass with exit code 0.

- [ ] **Step 4: Push and open the pull request**

```bash
git push -u origin codex/ask-session-header
gh pr create --base master --head codex/ask-session-header --title "feat(frontend): merge Ask session controls into one row" --body "$(printf '%s\n' '## Background' 'The Ask workspace duplicated conversation controls across two fixed rows, reducing the visible answer area.' '' '## Approach' '- replace the duplicate controls with a single `历史 N` header entry and adjacent direct-new-session `+`' '- keep the existing expandable manager and all switch/rename/delete/cleanup behavior' '- re-anchor the manager beneath the compact header and synchronize product documentation' '' '## Testing' '- `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`' '- desktop and narrow-width browser acceptance' '' 'Generated-with-Claude-Code')"
```

Expected: push succeeds and `gh pr create` returns the new pull-request URL.
