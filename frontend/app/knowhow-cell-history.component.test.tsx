// knowhow-cell-history.tsx 组件测试（vitest + jsdom，Node 原生 TS 类型剥离不
// 支持 .tsx，只能用 vitest 跑，同本仓库其余 *.component.test.tsx 的既有分工）。
//
// 覆盖三件事，均为"上一轮实现者写过、验证完就删掉"的一次性验证的永久化
// （评审要求，problem 3）+ 本轮评审修复本身的回归测试（problem 2）：
//
//   1. 三态（预览/编辑/历史）共用同一个 useFloatingWindow storageKey
//      （"knowhow.cellModal.window"）与 useFullscreenToggle 键
//      （FULLSCREEN_STORAGE_KEY）——含"预览态开全屏 → 切编辑态/历史态仍
//      全屏"的读后写场景，以及浮窗位置记忆同样跨三态共享的读后写场景。
//   2.「当前」徽标按 seq 判定（同 knowhow-history-drawer.tsx 的 isHead），
//      历史含重复值时只有最新那条被标为「当前」——旧实现按值比较
//      （entry.after === contentMd）会把多条都误标。
//   3. 确认恢复时的"合并共享格"提示（kh-affect-hint）只在 affectedBranchCount
//      > 1 时出现；且 handleRestore 把决定权完全委托给 onRestore 回调
//      （不自己直接打网络请求）。
//
// 批量 vs 单格的扇写路由决策测试（problem 1 的变异验证目标）不在本文件——
// 那个决策活在 knowhow-panel.tsx 的 handleCellSave 里，需要挂载整个
// KnowhowPanel 才能验证真实路由，见 knowhow-cell-restore.component.test.tsx。
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("./knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./knowhow-model.ts")>();
  return {
    ...actual,
    fetchKnowhowCellHistory: vi.fn(),
  };
});

import { fetchKnowhowCellHistory, type KnowhowColumn, type KnowhowTableDetail } from "./knowhow-model.ts";
import { KnowhowCellHistory } from "./knowhow-cell-history.tsx";
import {
  FULLSCREEN_LABEL,
  KnowhowCellEditor,
  KnowhowCellPreview,
  RESTORE_SIZE_LABEL,
} from "./knowhow-cell-editor.tsx";

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  // 三态共用的 sessionStorage 键（全屏 + 浮窗几何）不该跨 test 残留——每个
  // test 各自控制自己要不要预先种一份记忆。
  window.sessionStorage.clear();
});

const column: KnowhowColumn = { id: "c1", name: "备注", role: "attribute", position: 0 };

const table: KnowhowTableDetail = {
  id: "t1",
  title: "测试表",
  description: "",
  anchorColumnId: null,
  columns: [column],
  rows: [{ id: "r1", position: 0, projectionStatus: "synced", cells: { c1: "内容" } }],
};

// --- 1a. 全屏键共享：预览态开全屏 → 切编辑态/历史态仍全屏（读后写） -----------

test("三态共用同一个 fullscreen 键：预览态开全屏后，编辑态/历史态挂载时读到同一个已开全屏状态", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue([]);

  const { unmount: unmountPreview } = render(
    <KnowhowCellPreview
      rowTitle="行1"
      column={column}
      contentMd="内容"
      notebookId="nb"
      apiBase="http://api.test"
      canEdit
      onEdit={() => undefined}
      onClose={() => undefined}
      onHistory={() => undefined}
    />,
  );
  // 预览态挂载时还是"全屏"（未开）；点一下切到全屏。
  await user.click(screen.getByRole("button", { name: FULLSCREEN_LABEL }));
  expect(screen.getByRole("button", { name: RESTORE_SIZE_LABEL })).toBeInTheDocument();
  unmountPreview();

  // 切编辑态（panel 的 cellModal.mode 变化会卸载 Preview、挂载 Editor，效果
  // 等同这里的 unmount+render）：全新挂载的 Editor 应该在**第一帧**就读到
  // 已经是全屏——不需要用户再点一次。
  const { unmount: unmountEditor } = render(
    <KnowhowCellEditor
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      columnId="c1"
      rowTitle="行1"
      onSave={() => Promise.resolve()}
      onNavigate={() => undefined}
      onClose={() => undefined}
      onHistory={() => undefined}
    />,
  );
  expect(screen.getByRole("button", { name: RESTORE_SIZE_LABEL })).toBeInTheDocument();
  unmountEditor();

  // 再切历史态：同样应该读到全屏。
  render(
    <KnowhowCellHistory
      rowTitle="行1"
      column={column}
      contentMd="内容"
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      canEdit
      onRestore={() => Promise.resolve()}
      onBack={() => undefined}
      onClose={() => undefined}
    />,
  );
  expect(screen.getByRole("button", { name: RESTORE_SIZE_LABEL })).toBeInTheDocument();
});

// --- 1b. 浮窗几何键共享：三态挂载时读到同一份持久化位置 ----------------------

test("三态共用同一个 useFloatingWindow storageKey：三态各自挂载时都读到同一份持久化位置", async () => {
  // 预先在这个共享键下种一份"非默认"位置——不带 width/height（避免触发按
  // 视口收窄尺寸的 clamp 分支，只验证位置本身跨三态一致，见
  // floating-window-logic.ts clampRectSizeToViewport 只处理 width/height）。
  window.sessionStorage.setItem("knowhow.cellModal.window", JSON.stringify({ x: 37, y: 24, width: null, height: null }));
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue([]);

  const { unmount: unmountPreview } = render(
    <KnowhowCellPreview
      rowTitle="行1"
      column={column}
      contentMd="内容"
      notebookId="nb"
      apiBase="http://api.test"
      canEdit
      onEdit={() => undefined}
      onClose={() => undefined}
      onHistory={() => undefined}
    />,
  );
  expect(screen.getByRole("dialog")).toHaveStyle({ transform: "translate3d(37px, 24px, 0)" });
  unmountPreview();

  const { unmount: unmountEditor } = render(
    <KnowhowCellEditor
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      columnId="c1"
      rowTitle="行1"
      onSave={() => Promise.resolve()}
      onNavigate={() => undefined}
      onClose={() => undefined}
      onHistory={() => undefined}
    />,
  );
  expect(screen.getByRole("dialog")).toHaveStyle({ transform: "translate3d(37px, 24px, 0)" });
  unmountEditor();

  render(
    <KnowhowCellHistory
      rowTitle="行1"
      column={column}
      contentMd="内容"
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      canEdit
      onRestore={() => Promise.resolve()}
      onBack={() => undefined}
      onClose={() => undefined}
    />,
  );
  expect(screen.getByRole("dialog")).toHaveStyle({ transform: "translate3d(37px, 24px, 0)" });
});

// --- 2. 「当前」徽标按 seq 判定（problem 2） ---------------------------------

test("历史含重复值时（A→B→A）只有最新那条被标为「当前」，不按值比较", async () => {
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue([
    { seq: 3, actor: "u", origin: "user", createdAt: "2026-07-20T00:00:00Z", before: "B", after: "A" },
    { seq: 2, actor: "u", origin: "user", createdAt: "2026-07-19T00:00:00Z", before: "A", after: "B" },
    { seq: 1, actor: "u", origin: "user", createdAt: "2026-07-18T00:00:00Z", before: null, after: "A" },
  ]);

  render(
    <KnowhowCellHistory
      rowTitle="行1"
      column={column}
      contentMd="A"
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      canEdit={false}
      onRestore={() => Promise.resolve()}
      onBack={() => undefined}
      onClose={() => undefined}
    />,
  );

  const items = await screen.findAllByRole("listitem");
  expect(items).toHaveLength(3);
  // 只有最新（seq=3，entries[0]）那条标「当前」，即便 seq=1 的 after 值
  // （"A"）与当前实时内容逐字相同。
  expect(within(items[0]).getByText("当前")).toBeInTheDocument();
  expect(within(items[1]).queryByText("当前")).toBeNull();
  expect(within(items[2]).queryByText("当前")).toBeNull();
  expect(screen.getAllByText("当前")).toHaveLength(1);
});

// --- 3a. 合并共享格提示只在确认恢复时、affectedBranchCount>1 时出现 ---------

test("确认恢复时：合并共享格（affectedBranchCount>1）显示同步提示，非共享格不显示", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue([
    { seq: 2, actor: "u", origin: "user", createdAt: "2026-07-20T00:00:00Z", before: "旧值", after: "当前" },
    { seq: 1, actor: "u", origin: "user", createdAt: "2026-07-18T00:00:00Z", before: null, after: "旧值" },
  ]);
  const onRestore = vi.fn().mockResolvedValue(undefined);

  const { rerender } = render(
    <KnowhowCellHistory
      rowTitle="行1"
      column={column}
      contentMd="当前"
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      canEdit
      affectedBranchCount={3}
      onRestore={onRestore}
      onBack={() => undefined}
      onClose={() => undefined}
    />,
  );

  await user.click(await screen.findByRole("button", { name: /恢复此版本/ }));
  expect(screen.getByText("恢复将同步到该概念下全部 3 个分支")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "取消" }));

  rerender(
    <KnowhowCellHistory
      rowTitle="行1"
      column={column}
      contentMd="当前"
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      canEdit
      onRestore={onRestore}
      onBack={() => undefined}
      onClose={() => undefined}
    />,
  );
  await user.click(screen.getByRole("button", { name: /恢复此版本/ }));
  expect(screen.queryByText(/恢复将同步到/)).toBeNull();
});

// --- 3b. handleRestore 把落库完全委托给 onRestore ---------------------------

test("确认恢复调用 onRestore(rowId, columnId, entry.after)，本组件不自己直接发请求", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue([
    { seq: 2, actor: "u", origin: "user", createdAt: "2026-07-20T00:00:00Z", before: "旧值", after: "当前" },
    { seq: 1, actor: "u", origin: "user", createdAt: "2026-07-18T00:00:00Z", before: null, after: "旧值" },
  ]);
  const onRestore = vi.fn().mockResolvedValue(undefined);

  render(
    <KnowhowCellHistory
      rowTitle="行1"
      column={column}
      contentMd="当前"
      notebookId="nb"
      apiBase="http://api.test"
      table={table}
      rowId="r1"
      canEdit
      onRestore={onRestore}
      onBack={() => undefined}
      onClose={() => undefined}
    />,
  );

  await user.click(await screen.findByRole("button", { name: /恢复此版本/ }));
  await user.click(screen.getByRole("button", { name: "确认恢复" }));

  await waitFor(() => expect(onRestore).toHaveBeenCalledWith("r1", "c1", "旧值"));
  expect(onRestore).toHaveBeenCalledTimes(1);
});
