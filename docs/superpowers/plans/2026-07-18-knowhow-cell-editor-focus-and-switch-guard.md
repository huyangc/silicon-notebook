# knowhow 格子编辑器：三态视图 + 切格子未保存守卫 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 编辑态格子浮窗默认单栏专注写作，可切「并列对比 / 铺满预览」，全屏正交手动触发；切兄弟格改走与关闭一致的「未保存提醒 + 明确丢弃」守卫，不再静默落草稿/丢字。

**Architecture:** 纯前端、无后端/数据流改动。一个 `viewMode`（sessionStorage 记忆）驱动工具栏分段控件 + `.kh-split` 布局；编辑态 `closeGuard` 泛化成 `pendingLeave`（close|switch），切格子拦在调用 `onSwitchCell` 之前。可测纯逻辑（视图态归一化 + 新文案常量）落 logic 文件走 node:test；组件/CSS 走 tsc + 浏览器预览验证。

**Tech Stack:** Next.js 15 / React 19 / TypeScript、lucide-react ^0.468、node:test（`.mjs`，只测 `.ts` 纯逻辑）、styled-jsx global（CSS 在 knowhow-panel.tsx）。

## Global Constraints

- **范围仅编辑态** `KnowhowCellEditor`；**不改** 只读预览弹窗 `KnowhowCellPreview`、`switchCell`（knowhow-panel.tsx）、关闭/取消/背景/Esc 现有守卫语义（只把「切格子」并入）。
- **文案走常量**：UI 字符串在 knowhow-cell-editor-logic.ts 定义、组件只引用；新文案加 byte-exact 单测（延续现有风格）。
- **UI 对齐精致**：分段控件/选中态符合项目 UI 对齐标准，不接受粗糙堆叠。
- **中文文案 + 弯引号**：page/组件中文文案里的「」“”是有意的，切勿批量替换直引号。
- **命令**（从 `frontend/`）：类型检查 `npm run lint`（tsc --noEmit）；单测 `npm test`（`node --test $(find app -name '*.test.mjs')`）。worktree 的 `frontend/node_modules` 已就绪，可直接跑。
- **提交**：每个 Task 末尾一次提交；本分支走 rebase 合并、保持线性。CSS 只增不移既有 `.kh-*` 规则的行序无关紧要（本文件非 surface-manifest 行号守卫对象）。

---

## 文件结构

- **Modify** `frontend/app/knowhow-cell-editor-logic.ts` — 加 `CellViewMode` 类型、`CELL_VIEW_MODE_STORAGE_KEY`、`normalizeCellViewMode`、三态标签、切格子守卫文案（纯逻辑/常量）。
- **Modify** `frontend/app/knowhow-cell-editor.test.mjs` — 加 normalizeCellViewMode + 新文案的单测。
- **Modify** `frontend/app/knowhow-cell-editor.tsx` — 加 `Columns2` 图标 import、`useCellViewMode` hook、分段控件、按 `viewMode` 条件渲染 body、`closeGuard`→`pendingLeave` 守卫 + `handleSwitchCell` + `flushDraft`。
- **Modify** `frontend/app/knowhow-panel.tsx` — 加 `.kh-view-switch` 分段控件样式、`.kh-split--edit/--split/--preview` 变体、移动端断点补 `--split`。

---

## Task 1: 纯逻辑——视图态归一化 + 新文案常量（TDD）

**Files:**
- Modify: `frontend/app/knowhow-cell-editor-logic.ts`（在文件末尾 `isImageFile` 之后追加）
- Test: `frontend/app/knowhow-cell-editor.test.mjs`

**Interfaces:**
- Produces:
  - `type CellViewMode = "edit" | "split" | "preview"`
  - `const CELL_VIEW_MODE_STORAGE_KEY: string`（`"knowhow.cellEditor.viewMode"`）
  - `function normalizeCellViewMode(raw: string | null): CellViewMode`
  - `const VIEW_MODE_EDIT_LABEL = "编辑"` / `VIEW_MODE_SPLIT_LABEL = "并列"` / `VIEW_MODE_PREVIEW_LABEL = "预览"`
  - `const SWITCH_GUARD_MESSAGE: string` / `SWITCH_GUARD_DISCARD_LABEL = "放弃并切换"`

- [ ] **Step 1: 写失败测试** — 在 `knowhow-cell-editor.test.mjs` 顶部 import 块（line 4-25）追加这些名字，并在文件末尾新增测试：

在 import 块（`} from "./knowhow-cell-editor-logic.ts";` 之前）加：
```js
  normalizeCellViewMode,
  CELL_VIEW_MODE_STORAGE_KEY,
  VIEW_MODE_EDIT_LABEL,
  VIEW_MODE_SPLIT_LABEL,
  VIEW_MODE_PREVIEW_LABEL,
  SWITCH_GUARD_MESSAGE,
  SWITCH_GUARD_DISCARD_LABEL,
```

在文件末尾追加：
```js
// --- 编辑器视图三态 + 切格子守卫文案 ---------------------------------------------

test("normalizeCellViewMode: null / 空 / 非法值 → edit（唯一默认口径）", () => {
  assert.strictEqual(normalizeCellViewMode(null), "edit");
  assert.strictEqual(normalizeCellViewMode(""), "edit");
  assert.strictEqual(normalizeCellViewMode("garbage"), "edit");
  assert.strictEqual(normalizeCellViewMode("EDIT"), "edit"); // 大小写敏感
});

test("normalizeCellViewMode: 三个合法值原样返回", () => {
  assert.strictEqual(normalizeCellViewMode("edit"), "edit");
  assert.strictEqual(normalizeCellViewMode("split"), "split");
  assert.strictEqual(normalizeCellViewMode("preview"), "preview");
});

test("CELL_VIEW_MODE_STORAGE_KEY: 与全屏键区分、editor 专用", () => {
  assert.strictEqual(CELL_VIEW_MODE_STORAGE_KEY, "knowhow.cellEditor.viewMode");
});

test("视图三态标签：编辑 / 并列 / 预览", () => {
  assert.strictEqual(VIEW_MODE_EDIT_LABEL, "编辑");
  assert.strictEqual(VIEW_MODE_SPLIT_LABEL, "并列");
  assert.strictEqual(VIEW_MODE_PREVIEW_LABEL, "预览");
});

test("切格子守卫：放弃按钮文案 + 提醒含未保存/可恢复", () => {
  assert.strictEqual(SWITCH_GUARD_DISCARD_LABEL, "放弃并切换");
  assert.match(SWITCH_GUARD_MESSAGE, /未保存/);
  assert.match(SWITCH_GUARD_MESSAGE, /可恢复/);
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL —— 新测试报 `normalizeCellViewMode is not a function` / 各常量 `undefined`（现有测试仍全绿）。

- [ ] **Step 3: 最小实现** — 在 `knowhow-cell-editor-logic.ts` 末尾（`isImageFile` 函数之后）追加：

```ts
// --- 编辑器视图三态（编辑/并列/预览）--------------------------------------------

// 编辑态浮窗的三种视图：edit=单栏专注写作（默认）、split=编辑+预览并列对比、
// preview=预览铺满（隐藏编辑器）。全屏与本视图态正交（见 knowhow-cell-editor.tsx
// 的 useFullscreenToggle），各用各的 sessionStorage 键、互不影响。
export type CellViewMode = "edit" | "split" | "preview";

// per-session 记忆键（跨会话回落 edit）；与全屏键 FULLSCREEN_STORAGE_KEY 分开。
export const CELL_VIEW_MODE_STORAGE_KEY = "knowhow.cellEditor.viewMode";

// sessionStorage 读到的原始值归一化：非 "split"/"preview" 的一切（null、旧值、
// 脏值）都回落默认 "edit"。抽纯函数便于单测，也保证组件侧默认口径只有一处。
export function normalizeCellViewMode(raw: string | null): CellViewMode {
  return raw === "split" || raw === "preview" ? raw : "edit";
}

// 三态分段控件按钮文案。
export const VIEW_MODE_EDIT_LABEL = "编辑";
export const VIEW_MODE_SPLIT_LABEL = "并列";
export const VIEW_MODE_PREVIEW_LABEL = "预览";

// 切兄弟格的未保存守卫（与关闭守卫 CLOSE_GUARD_* 同款「未保存提醒 + 明确丢弃」，
// 只是离开动作是"切到另一格"而非"关闭"；放弃时草稿同步保留、可恢复）。「继续
// 编辑」复用 CLOSE_GUARD_CONTINUE_LABEL。
export const SWITCH_GUARD_MESSAGE =
  "有未保存的修改，确定要切换到另一格吗？（草稿已自动保存，下次打开可恢复）";
export const SWITCH_GUARD_DISCARD_LABEL = "放弃并切换";
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS —— 全部测试绿（含新 5 条 + 既有）。

- [ ] **Step 5: 类型检查**

Run: `cd frontend && npm run lint`
Expected: 无报错。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/knowhow-cell-editor-logic.ts frontend/app/knowhow-cell-editor.test.mjs
git commit -m "feat(knowhow): 编辑器视图三态归一化 + 切格子守卫文案常量

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 三态视图——hook + 分段控件 + body 布局 + CSS

**Files:**
- Modify: `frontend/app/knowhow-cell-editor.tsx`
- Modify: `frontend/app/knowhow-panel.tsx`（styled-jsx global 区）

**Interfaces:**
- Consumes（Task 1）：`CellViewMode`、`CELL_VIEW_MODE_STORAGE_KEY`、`normalizeCellViewMode`、`VIEW_MODE_EDIT_LABEL`/`VIEW_MODE_SPLIT_LABEL`/`VIEW_MODE_PREVIEW_LABEL`。
- Produces：编辑器内部 `useCellViewMode(): [CellViewMode, (m: CellViewMode) => void]`、`.kh-view-switch` 控件、`.kh-split--{edit,split,preview}` 布局类。

- [ ] **Step 1: 图标 + 逻辑常量 import**

`knowhow-cell-editor.tsx` 的 lucide import 块（line 51-66）加 `Columns2`（按字母序放 `Check` 与 `Code` 之间即可）：
```ts
  Columns2,
```
在从 `./knowhow-cell-editor-logic.ts` 的 import 块（line 80-107）追加：
```ts
  CELL_VIEW_MODE_STORAGE_KEY,
  VIEW_MODE_EDIT_LABEL,
  VIEW_MODE_SPLIT_LABEL,
  VIEW_MODE_PREVIEW_LABEL,
  normalizeCellViewMode,
  type CellViewMode,
```

- [ ] **Step 2: 加 `useCellViewMode` hook** — 紧接现有 `useFullscreenToggle`（line 266 `}` 之后）新增，风格与之对齐：

```ts
// 编辑器视图三态（编辑/并列/预览）——sessionStorage per-session 记忆（跨会话回落
// edit），与 useFullscreenToggle 同款持久化方式但各用各的键、互不影响。仅编辑态
// 用，不导出。
function useCellViewMode(): [CellViewMode, (mode: CellViewMode) => void] {
  const [viewMode, setViewModeState] = useState<CellViewMode>(() => {
    if (typeof window === "undefined") return "edit";
    try {
      return normalizeCellViewMode(window.sessionStorage.getItem(CELL_VIEW_MODE_STORAGE_KEY));
    } catch {
      return "edit";
    }
  });
  const setViewMode = useCallback((mode: CellViewMode) => {
    setViewModeState(mode);
    try {
      if (typeof window !== "undefined") {
        window.sessionStorage.setItem(CELL_VIEW_MODE_STORAGE_KEY, mode);
      }
    } catch {
      // sessionStorage 不可用（隐私模式等）静默——只是记不住本会话选择。
    }
  }, []);
  return [viewMode, setViewMode];
}
```

- [ ] **Step 3: 组件内取 viewMode** — 在 `KnowhowCellEditor` 里，紧挨 fullscreen 那行（line 535 `const [fullscreen, toggleFullscreen] = useFullscreenToggle(...)`）下面加：

```ts
  const [viewMode, setViewMode] = useCellViewMode();
```

- [ ] **Step 4: procedure 提示按 viewMode 收起** — 把 line 912：
```tsx
              {column.role === "procedure" && <p className="kh-procedure-hint">{PROCEDURE_HINT_TEXT}</p>}
```
改为（预览态无编辑对象，不显示编辑向提示）：
```tsx
              {column.role === "procedure" && viewMode !== "preview" && (
                <p className="kh-procedure-hint">{PROCEDURE_HINT_TEXT}</p>
              )}
```

- [ ] **Step 5: 工具栏——录入按钮按 viewMode 收起 + 加分段控件** — 现工具栏为 line 914-968 `<div className="kh-toolbar"> … </div>`。改造为：录入类按钮（列表/代码/图片/优化/上传状态）整体包进 `{viewMode !== "preview" && (<>…</>)}`；隐藏文件 input 保持常驻；末尾加 `.kh-view-switch`。将整段替换为：

```tsx
              <div className="kh-toolbar">
                {viewMode !== "preview" && (
                  <>
                    <button
                      type="button"
                      className="kh-toolbar-button"
                      title={TOOLBAR_LIST_LABEL}
                      onClick={handleListClick}
                      disabled={uploading}
                    >
                      <List size={15} /> {TOOLBAR_LIST_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button"
                      title={TOOLBAR_CODE_LABEL}
                      onClick={handleCodeClick}
                      disabled={uploading}
                    >
                      <Code size={15} /> {TOOLBAR_CODE_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button"
                      title={TOOLBAR_IMAGE_LABEL}
                      onClick={handleImageButtonClick}
                      disabled={uploading}
                    >
                      <ImagePlus size={15} /> {TOOLBAR_IMAGE_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button kh-toolbar-button--optimize"
                      title={optimizeDisabledReason ?? OPTIMIZE_CELL_BUTTON_LABEL}
                      onClick={handleOptimize}
                      disabled={optimizeDisabledReason !== null || uploading || savingMode !== null}
                    >
                      {isCellOptimizeLoading(optimizeState) ? (
                        <Loader2 size={15} className="knowhow-spin" />
                      ) : (
                        <Sparkles size={15} />
                      )}
                      {isCellOptimizeLoading(optimizeState) ? "优化中…" : OPTIMIZE_CELL_BUTTON_LABEL}
                    </button>
                    {uploading && (
                      <span className="kh-toolbar-status">
                        <Loader2 size={14} className="knowhow-spin" /> 图片上传中…
                      </span>
                    )}
                  </>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="kh-hidden-file-input"
                  onChange={handleFileInputChange}
                />
                <div className="kh-view-switch" role="group" aria-label="视图模式">
                  <button
                    type="button"
                    className={`kh-view-switch-button${viewMode === "edit" ? " kh-view-switch-button--active" : ""}`}
                    aria-pressed={viewMode === "edit"}
                    title={VIEW_MODE_EDIT_LABEL}
                    onClick={() => setViewMode("edit")}
                  >
                    <Pencil size={14} /> {VIEW_MODE_EDIT_LABEL}
                  </button>
                  <button
                    type="button"
                    className={`kh-view-switch-button${viewMode === "split" ? " kh-view-switch-button--active" : ""}`}
                    aria-pressed={viewMode === "split"}
                    title={VIEW_MODE_SPLIT_LABEL}
                    onClick={() => setViewMode("split")}
                  >
                    <Columns2 size={14} /> {VIEW_MODE_SPLIT_LABEL}
                  </button>
                  <button
                    type="button"
                    className={`kh-view-switch-button${viewMode === "preview" ? " kh-view-switch-button--active" : ""}`}
                    aria-pressed={viewMode === "preview"}
                    title={VIEW_MODE_PREVIEW_LABEL}
                    onClick={() => setViewMode("preview")}
                  >
                    <Eye size={14} /> {VIEW_MODE_PREVIEW_LABEL}
                  </button>
                </div>
              </div>
```

- [ ] **Step 6: body——按 viewMode 条件渲染两个 pane** — 现 `.kh-split`（line 973-993）双 pane 恒渲染。替换为：

```tsx
              <div className={`kh-split kh-split--${viewMode}`}>
                {viewMode !== "preview" && (
                  <div
                    className={`kh-editor-pane${dragActive ? " kh-editor-pane--drag" : ""}`}
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    onDrop={handleDrop}
                  >
                    <textarea
                      ref={textareaRef}
                      className="kh-textarea"
                      value={content}
                      disabled={uploading}
                      onChange={(event) => setContent(event.target.value)}
                      onPaste={handlePaste}
                      placeholder="输入 markdown 内容…"
                    />
                  </div>
                )}
                {viewMode !== "edit" && (
                  <div className="kh-preview-pane">
                    <KnowhowMarkdown md={content} notebookId={notebookId} apiBase={apiBase} />
                  </div>
                )}
              </div>
```

- [ ] **Step 7: CSS——分段控件 + `.kh-split` 变体（knowhow-panel.tsx）**

7a. 把现有 `.kh-split` 规则（line 2231-2237）里的 `grid-template-columns: 1fr 1fr;` 移出，改成变体类。将：
```css
        .kh-split {
          flex: 1 1 auto;
          min-height: 260px;
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
        }
```
替换为：
```css
        .kh-split {
          flex: 1 1 auto;
          min-height: 260px;
          display: grid;
          gap: 14px;
        }

        /* 三态视图：编辑/预览单栏铺满，并列双栏。 */
        .kh-split--edit,
        .kh-split--preview {
          grid-template-columns: 1fr;
        }
        .kh-split--split {
          grid-template-columns: 1fr 1fr;
        }

        /* 工具栏右侧三选一视图切换（编辑/并列/预览）——分段控件，选中态浮起白底。
           margin-left:auto 把它推到录入按钮的另一端；预览态录入按钮隐藏时它仍靠右。 */
        .kh-view-switch {
          margin-left: auto;
          display: inline-flex;
          align-items: center;
          gap: 2px;
          padding: 2px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--soft);
        }
        .kh-view-switch-button {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          border: 0;
          border-radius: 6px;
          background: transparent;
          padding: 4px 10px;
          font-size: 12.5px;
          color: var(--muted);
          cursor: pointer;
        }
        .kh-view-switch-button:hover {
          color: var(--ink);
        }
        .kh-view-switch-button--active {
          background: #fff;
          color: var(--ink);
          box-shadow: 0 1px 2px rgba(24, 39, 75, 0.12);
        }
```

7b. 移动端断点（line 2758）把 `--split` 也压单栏。将：
```css
          .kh-split {
            grid-template-columns: 1fr;
          }
```
替换为：
```css
          .kh-split,
          .kh-split--split {
            grid-template-columns: 1fr;
          }
```

- [ ] **Step 8: 类型检查 + 单测**

Run: `cd frontend && npm run lint && npm test`
Expected: tsc 无报错；测试全绿（Task 1 的仍在）。

- [ ] **Step 9: 浏览器预览验证**（preview 工具）

启动 dev（`.claude/launch.json` 无则建一个 `next dev` 配置）→ 打开一张 knowhow 表 → 点一格进编辑态：
- 默认单栏（只有 textarea 铺满），工具栏右侧见 `[编辑|并列|预览]`，「编辑」选中。
- 点「并列」→ 编辑 + 预览左右各半；点「预览」→ 编辑器隐藏、预览铺满、录入按钮消失、分段控件仍在。
- 点 header 最大化（全屏）：在「编辑」下得大编辑器、「预览」下得铺满预览、「并列」下大对照；再点还原。全屏默认关。
- `read_console_messages` 无报错。截图存证。

- [ ] **Step 10: 提交**

```bash
git add frontend/app/knowhow-cell-editor.tsx frontend/app/knowhow-panel.tsx
git commit -m "feat(knowhow): 格子编辑器三态视图（编辑/并列/预览）+ 正交全屏

默认单栏专注写作，工具栏分段控件切并列对比/铺满预览；预览态收起录入工具。
全屏沿用现有最大化按钮、与视图正交、用户手动触发。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 切格子未保存守卫（`closeGuard` → `pendingLeave`）

**Files:**
- Modify: `frontend/app/knowhow-cell-editor.tsx`

**Interfaces:**
- Consumes（Task 1）：`SWITCH_GUARD_MESSAGE`、`SWITCH_GUARD_DISCARD_LABEL`；既有 `CLOSE_GUARD_MESSAGE`/`CLOSE_GUARD_CONTINUE_LABEL`/`CLOSE_GUARD_DISCARD_LABEL`、`hasUnsavedChanges`、`draftStorageKey`。
- Produces：编辑器内部 `type PendingLeave`、`pendingLeave` 状态、`handleSwitchCell`、`flushDraft`。

- [ ] **Step 1: import 切格子守卫文案** — `knowhow-cell-editor.tsx` 从 `./knowhow-cell-editor-logic.ts` 的 import 块追加：
```ts
  SWITCH_GUARD_MESSAGE,
  SWITCH_GUARD_DISCARD_LABEL,
```

- [ ] **Step 2: 定义 `PendingLeave` 类型** — 在 `KnowhowCellEditor` 组件函数上方（紧接 `export interface KnowhowCellEditorProps { … }` 之后、`export function KnowhowCellEditor(` 之前）加：

```ts
// 编辑态「带未保存改动的离开」意图：关闭整窗，或切到本行另一格（columnId）。
// 二者共用同一条守卫 UI（footer），放弃时都同步落草稿、可恢复。
type PendingLeave = { kind: "close" } | { kind: "switch"; columnId: string };
```

- [ ] **Step 3: 状态 `closeGuard` → `pendingLeave`** — 把 line 554：
```ts
  const [closeGuard, setCloseGuard] = useState(false);
```
替换为：
```ts
  const [pendingLeave, setPendingLeave] = useState<PendingLeave | null>(null);
```

- [ ] **Step 4: 加 `flushDraft` + `handleSwitchCell`** — 在 `runSave`（line 630-658）之后新增：

```ts
  // 放弃/切走前把当前内容同步落进草稿键——兜住「组件卸载抢在 300ms 自动草稿
  // 防抖之前、草稿没写成」的漏洞，兑现守卫文案「草稿已自动保存、可恢复」。无
  // 改动时清掉旧草稿（与自动草稿 effect 同口径）。close/switch 放弃分支共用。
  function flushDraft() {
    try {
      if (hasUnsavedChanges(content, savedContent)) {
        window.localStorage.setItem(draftStorageKey(rowId, columnId), content);
      } else {
        window.localStorage.removeItem(draftStorageKey(rowId, columnId));
      }
    } catch {
      /* ignore（隐私模式/配额）——写不进只是这次草稿没留下，不比现状差 */
    }
  }

  // 点「本行其他格子」的兄弟格：有未保存改动 → 弹守卫（与关闭一致的「未保存提醒
  // + 明确丢弃」），让用户明确选择；无改动直接切、不打扰。守卫的放弃分支才真正
  // 调 onSwitchCell（见 footer）。
  const handleSwitchCell = useCallback(
    (targetColumnId: string) => {
      if (hasUnsavedChanges(content, savedContent)) {
        setPendingLeave({ kind: "switch", columnId: targetColumnId });
      } else {
        onSwitchCell?.(targetColumnId);
      }
    },
    [content, savedContent, onSwitchCell],
  );
```

- [ ] **Step 5: `requestClose` 适配 pendingLeave** — 把 line 664-674 的 `requestClose` 替换为：

```ts
  const requestClose = useCallback(() => {
    // 关闭守卫弹着时再次触发（第二次 Esc / 关闭）→ 强制关闭（保留既有习惯）。
    if (pendingLeave?.kind === "close") {
      onClose();
      return;
    }
    // 切格子守卫弹着时按 Esc / 点关闭 → 取消这次切换、回到编辑，不误关整窗。
    if (pendingLeave?.kind === "switch") {
      setPendingLeave(null);
      return;
    }
    if (hasUnsavedChanges(content, savedContent)) {
      setPendingLeave({ kind: "close" });
    } else {
      onClose();
    }
  }, [pendingLeave, content, savedContent, onClose]);
```

- [ ] **Step 6: 行上下文用 `handleSwitchCell`** — 把 line 877：
```tsx
          <KnowhowRowContext table={table} rowId={rowId} currentColumnId={columnId} onSwitchCell={onSwitchCell} />
```
改为：
```tsx
          <KnowhowRowContext table={table} rowId={rowId} currentColumnId={columnId} onSwitchCell={handleSwitchCell} />
```

- [ ] **Step 7: footer 守卫按 kind 分文案 + 放弃分支执行离开** — 把 footer 里 `closeGuard ? (…) :`（line 999-1010）那段替换为：

```tsx
          {pendingLeave ? (
            <div className="kh-close-guard">
              <span>{pendingLeave.kind === "switch" ? SWITCH_GUARD_MESSAGE : CLOSE_GUARD_MESSAGE}</span>
              <div className="kh-close-guard-actions">
                <button type="button" onClick={() => setPendingLeave(null)}>
                  {CLOSE_GUARD_CONTINUE_LABEL}
                </button>
                <button
                  type="button"
                  className="kh-danger-button"
                  onClick={() => {
                    flushDraft();
                    if (pendingLeave.kind === "switch") {
                      const target = pendingLeave.columnId;
                      setPendingLeave(null);
                      onSwitchCell?.(target);
                    } else {
                      onClose();
                    }
                  }}
                >
                  {pendingLeave.kind === "switch" ? SWITCH_GUARD_DISCARD_LABEL : CLOSE_GUARD_DISCARD_LABEL}
                </button>
              </div>
            </div>
          ) : optimizeState.status === "ready" ? (
```

- [ ] **Step 8: 核对无残留 `closeGuard`**

Run: `cd frontend && grep -n "closeGuard\|setCloseGuard" app/knowhow-cell-editor.tsx`
Expected: 无输出（全部已替换）。

- [ ] **Step 9: 类型检查 + 单测**

Run: `cd frontend && npm run lint && npm test`
Expected: tsc 无报错；测试全绿。

- [ ] **Step 10: 浏览器预览验证**（preview 工具）

编辑态 + 「本行其他格子」有兄弟格的行：
- 打字改动 → 点一个兄弟格 → footer 弹「有未保存的修改，确定要切换到另一格吗？」+「继续编辑 / 放弃并切换」。
- 点「继续编辑」→ 留在原格、内容还在。
- 再点兄弟格 → 点「放弃并切换」→ 切到该格；回到原格（再点它的兄弟格或重开）→ 顶部出「检测到草稿」可「恢复」。
- 不改动直接点兄弟格 → 直接切、不弹守卫。
- 关闭/取消/Esc 行为不变（关闭仍弹未保存提醒）。
- `read_console_messages` 无报错；截图存证。

- [ ] **Step 11: 提交**

```bash
git add frontend/app/knowhow-cell-editor.tsx
git commit -m "feat(knowhow): 切兄弟格并入未保存守卫（继续编辑/放弃并切换）

编辑态点本行其他格子若有未保存改动 → 与关闭一致的提醒 + 明确丢弃，放弃时
同步落草稿可恢复（顺带修 close 放弃分支同一草稿竞态）；无改动直接切。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review（对照 spec）

**Spec 覆盖**：
- A 视图三态（编辑/并列/预览）→ Task 2（hook + 分段控件 + `.kh-split--*` + 条件渲染）。✓
- A 正交全屏、用户手动触发 → 不改现有 fullscreen，Task 2 Step 9 验证三态 × 全屏组合。✓
- A 预览态收起录入工具、分段控件常驻 → Task 2 Step 4/5。✓
- A sessionStorage 记忆、默认 edit → Task 1 `normalizeCellViewMode` + Task 2 `useCellViewMode`。✓
- B 切格子走未保存守卫 + 明确丢弃 → Task 3（pendingLeave + handleSwitchCell + footer）。✓
- B 放弃同步落草稿可恢复（含 close 竞态修正）→ Task 3 `flushDraft`。✓
- B 无改动直接切、只读预览态不受影响、`switchCell` 不改 → Task 3 Step 4/6（只包 editor 的 onSwitchCell）。✓
- 移动端三态单栏 → Task 2 Step 7b。✓
- 文案常量 + byte-exact 单测 → Task 1。✓

**占位符扫描**：无 TBD/TODO；每个改码步骤含完整代码。✓

**类型一致**：`CellViewMode`/`normalizeCellViewMode`/`CELL_VIEW_MODE_STORAGE_KEY`（Task 1 定义 → Task 2 消费）、`PendingLeave`/`pendingLeave`/`handleSwitchCell`/`flushDraft`（Task 3 内自洽）、`SWITCH_GUARD_*` 常量（Task 1 → Task 3）均一致。✓

## 风险与回滚

- 每 Task 独立提交、可单独回退。Task 1 纯加法（新常量/测试），Task 2/3 改同一组件但互不重叠（Task 2 动 viewMode/布局、Task 3 动守卫），冲突面小。
- `.kh-split` 去掉基类 `grid-template-columns` 后，任何未加 `--{mode}` 类的历史用法都会塌成单列——本仓库 `.kh-split` 仅此一处消费（Task 2 已同步加类），grep 确认无他处。
- lucide `Columns2` 已确认存在于 ^0.468（root/worktree node_modules dts 均有）；若某环境缺失，退 `SquareSplitHorizontal`。
