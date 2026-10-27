import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  CANCEL_DONE_TEXT,
  CANCEL_DUE_TEXT,
  CANCEL_GONE_TEXT,
  DELETION_DUE_TEXT,
  DELETIONS_BLOCK_TITLE,
  DELETIONS_UNAVAILABLE_TEXT,
  EXPORT_CLOSED_TEXT,
  EXPORT_FAILED_TEXT,
  TRANSFER_BUSY_TEXT,
  TRANSFER_RESTORED_TEXT,
  TRANSFER_CLOSED_TEXT,
  TRANSFER_NO_TARGET_TEXT,
  TRANSFER_WARNING_TEXT,
  deletionNoticeText,
  bellDeletionLabel,
  exportLabel,
  transferDoneText,
  transferProgressText,
  transferSummaryText,
  untransferableText,
} from "../../app/notebook-deletion-copy.ts";
import {
  NotebookDeletionsBlock,
  SETTLED_ROW_HOLD_MS,
  useNotebookDeletions,
  type DeletionFocusRequest,
  type DeletionTarget,
} from "../../app/notebook-deletions-block.tsx";
import { LEAVE_EXPORT_TOO_LARGE_TEXT } from "../../app/group-leave-dialog.tsx";
import type { NotebookDeletionNotice } from "../../app/notebook-deletion-api.ts";
import type { NotebookSummary } from "../../app/workspace-model.ts";
import { PendingBell, type PendingItem } from "../../app/pending-center.tsx";

// 「即将永久删除的笔记本」区块:只替换网络,区块、控件、网络客户端与下载 helper 都是真的。
// 钉的是三状态(窗口里 / 截止已过 / 读不到)× 两种身份(主人 / 有记忆的成员)的呈现,以及
// 每个控件的结果落在它自己旁边。

type Call = { method: string; path: string; body: string };
type Handler = (call: Call) => Response | Promise<Response> | undefined;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function installServer(handlers: Handler[]) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const call: Call = {
      method: (init?.method ?? "GET").toUpperCase(),
      path: url.pathname,
      body: typeof init?.body === "string" ? init.body : "",
    };
    calls.push(call);
    for (const handler of handlers) {
      const response = handler(call);
      if (response !== undefined) return response;
    }
    throw new Error(`unexpected request ${call.method} ${call.path}`);
  }));
  return calls;
}

const NOW = new Date("2026-10-22T09:00:00Z");
const DEADLINE = "2026-10-23T08:00:00+00:00";

const OWNER: NotebookDeletionNotice = {
  notebook_id: "nb-own", name: "我的旧库", delete_after: DEADLINE,
  is_owner: true, memory_count: 0, transferable_count: 0,
};
const MEMBER: NotebookDeletionNotice = {
  notebook_id: "nb-shared", name: "封装工艺库", delete_after: DEADLINE,
  is_owner: false, memory_count: 4, transferable_count: 2,
};
const TARGETS: DeletionTarget[] = [{ id: "nb-mine", name: "我的笔记本" }];

const listOf = (items: NotebookDeletionNotice[]): Handler => (call) =>
  call.method === "GET" && call.path === "/api/me/notebook-deletions" ? json({ items }) : undefined;

function Host({ targets = TARGETS, onCancelled = vi.fn(), focus = null, refreshKey = "k" }: {
  targets?: DeletionTarget[];
  onCancelled?: (notebook: NotebookSummary) => void;
  focus?: DeletionFocusRequest | null;
  refreshKey?: string;
}) {
  const deletions = useNotebookDeletions(true, refreshKey);
  return (
    <NotebookDeletionsBlock
      deletions={deletions}
      targets={targets}
      focus={focus}
      onCancelled={onCancelled}
    />
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(NOW);
  URL.createObjectURL = vi.fn(() => "blob:deletion-test");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const block = () => screen.findByRole("region", { name: DELETIONS_BLOCK_TITLE });

function NotificationHost() {
  const deletions = useNotebookDeletions(true, "notification");
  const [focus, setFocus] = useState<DeletionFocusRequest | null>(null);
  const sequence = useRef(0);
  const item: PendingItem = {
    type: "notebook_pending_delete", notebook_id: MEMBER.notebook_id,
    notebook_name: MEMBER.name, delete_after: MEMBER.delete_after,
    memory_count: MEMBER.memory_count, transferable_count: MEMBER.transferable_count,
  };
  return <PendingBell
    userId="notification-action-test" snapshot={{ count: 1, items: [item] }} doneItems={[]}
    onOpenItem={() => setFocus({ id: MEMBER.notebook_id, seq: ++sequence.current })}
    onOpenDone={vi.fn()} onDismissDone={vi.fn()}
    detail={{
      key: focus?.seq ?? null, onBack: () => setFocus(null),
      render: (active) => <NotebookDeletionsBlock deletions={deletions} targets={TARGETS}
        focus={active ? focus : null} onCancelled={vi.fn()} showEmpty />,
    }}
  />;
}

for (const dismissal of ["铃铛", "外部", "返回通知"]) {
  test(`导出途中通过${dismissal}收起详情，下载仍完成，重新打开可见结果`, async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const gate = deferred<Response>();
    installServer([
      listOf([MEMBER]),
      (call) => call.path === "/api/notebooks/nb-shared/memories/export" ? gate.promise : undefined,
    ]);
    render(<NotificationHost />);
    await user.click(screen.getByRole("button", { name: "待确认中心" }));
    await user.click(screen.getByText(bellDeletionLabel(MEMBER.name, MEMBER.delete_after)));
    const region = await block();
    await user.click(within(region).getByRole("button", { name: /导出「封装工艺库」/ }));
    if (dismissal === "外部") await user.click(document.body);
    else await user.click(screen.getByRole("button", { name: dismissal === "铃铛" ? "待确认中心" : "返回通知" }));
    expect(region).not.toBeVisible();
    await act(async () => { gate.resolve(new Response("memory export", { status: 200 })); });
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledOnce());
    if (dismissal !== "返回通知") await user.click(screen.getByRole("button", { name: "待确认中心" }));
    await user.click(screen.getByText(bellDeletionLabel(MEMBER.name, MEMBER.delete_after)));
    expect(within(await block()).getByText("已导出")).toBeVisible();
    expect(within(await block()).getByRole("listitem")).toHaveFocus();
  });
}

test("分页转移途中返回通知，后续页继续执行，重新打开保留完整结果", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const first = deferred<Response>();
  const second = deferred<Response>();
  let pages = 0;
  installServer([
    listOf([MEMBER]),
    (call) => call.path === "/api/notebooks/nb-shared/memories/transfer" ? (++pages === 1 ? first.promise : second.promise) : undefined,
  ]);
  render(<NotificationHost />);
  await user.click(screen.getByRole("button", { name: "待确认中心" }));
  await user.click(screen.getByText(bellDeletionLabel(MEMBER.name, MEMBER.delete_after)));
  const region = await block();
  await user.click(within(region).getByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  await user.click(screen.getByRole("button", { name: "返回通知" }));
  await act(async () => first.resolve(json({ results: [{ id: "m1", ok: true }], remaining: 1, next_after: "m1" })));
  await waitFor(() => expect(pages).toBe(2));
  await act(async () => second.resolve(json({ results: [{ id: "m2", ok: true }], remaining: 0, next_after: "m2" })));
  await user.click(screen.getByText(bellDeletionLabel(MEMBER.name, MEMBER.delete_after)));
  expect(within(await block()).getByText(transferSummaryText(2, 0, 0, "我的笔记本"))).toBeVisible();
});

test("没有与我有关的删除:区块不出现", async () => {
  const calls = installServer([listOf([])]);
  const view = render(<Host />);
  await waitFor(() => expect(calls).toHaveLength(1));
  expect(view.container).toBeEmptyDOMElement();
});

test("读不到时如实说「暂时读不到」(不当作没有),可以重试", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  let fail = true;
  installServer([
    (call) => (call.path === "/api/me/notebook-deletions" && fail ? json({ detail: "x" }, 500) : undefined),
    listOf([OWNER]),
  ]);
  render(<Host />);
  const region = await block();
  expect(within(region).getByRole("alert")).toHaveTextContent(DELETIONS_UNAVAILABLE_TEXT);
  fail = false;
  await user.click(within(region).getByRole("button", { name: "重试" }));
  expect(await within(region).findByText(deletionNoticeText(OWNER))).toBeInTheDocument();
});

test("主人一行:说清截止时刻;撤销中… → 已撤销,结果落在按钮旁,列表收回这本笔记本", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const gate = deferred<Response>();
  const onCancelled = vi.fn();
  installServer([
    listOf([OWNER]),
    (call) => (call.method === "POST" && call.path === "/api/notebooks/nb-own/cancel-delete" ? gate.promise : undefined),
  ]);
  render(<Host onCancelled={onCancelled} />);
  const region = await block();
  expect(await within(region).findByText(deletionNoticeText(OWNER))).toBeInTheDocument();
  expect(deletionNoticeText(OWNER)).toContain("2026年10月23日");
  // 没有自己的记忆:没有导出 / 转移入口。
  expect(within(region).queryByRole("button", { name: /导出/ })).toBeNull();

  await user.click(within(region).getByRole("button", { name: "撤销删除" }));
  const pressed = within(region).getByRole("button", { name: "撤销中…" });
  expect(pressed).toBeDisabled();
  await act(async () => gate.resolve(json({ id: "nb-own", name: "我的旧库" })));
  expect(await within(region).findByRole("button", { name: "已撤销" })).toBeDisabled();
  expect(within(region).getByRole("status")).toHaveTextContent(CANCEL_DONE_TEXT);
  expect(onCancelled).toHaveBeenCalledWith(expect.objectContaining({ id: "nb-own" }));
});

test("撤销撞上截止(409)或库已不存在(404):原因落在那一行", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const second = { ...OWNER, notebook_id: "nb-gone", name: "另一本" };
  installServer([
    listOf([OWNER, second]),
    (call) => (call.path === "/api/notebooks/nb-own/cancel-delete"
      ? json({ detail: { code: "notebook_delete_due", message: "x" } }, 409) : undefined),
    (call) => (call.path === "/api/notebooks/nb-gone/cancel-delete" ? json({ detail: "Notebook not found" }, 404) : undefined),
  ]);
  render(<Host />);
  const region = await block();
  const [dueRow, goneRow] = await within(region).findAllByRole("listitem");
  await user.click(within(dueRow).getByRole("button", { name: "撤销删除" }));
  expect(await within(dueRow).findByRole("alert")).toHaveTextContent(CANCEL_DUE_TEXT);
  expect(within(dueRow).getByText(DELETION_DUE_TEXT)).toBeInTheDocument();
  await user.click(within(goneRow).getByRole("button", { name: "撤销删除" }));
  expect(await within(goneRow).findByRole("alert")).toHaveTextContent(CANCEL_GONE_TEXT);
});

test("成员一行:带记忆条数;能转移几条之外的那几条请先导出", async () => {
  installServer([listOf([MEMBER])]);
  render(<Host />);
  const region = await block();
  expect(await within(region).findByText(deletionNoticeText(MEMBER))).toBeInTheDocument();
  expect(deletionNoticeText(MEMBER)).toContain("4 条记忆");
  expect(within(region).getByText(untransferableText(2))).toBeInTheDocument();
  expect(within(region).queryByRole("button", { name: "撤销删除" })).toBeNull();
  expect(within(region).getByRole("button", { name: `导出「封装工艺库」里你的 4 条记忆` }))
    .toHaveTextContent(exportLabel(4));
});

test("导出:导出中… → 已导出;失败、超限、截止已过各自把原因留在那一行", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const outcomes: Array<() => Response> = [
    () => new Response("# 记忆", { status: 200, headers: { "Content-Disposition": "attachment; filename=\"m.md\"" } }),
    () => json({ detail: "boom" }, 500),
    () => json({ detail: { code: "export_too_large", memory_count: 4, limit_bytes: 1 } }, 413),
    () => json({ detail: { code: "delete_window_closed" } }, 404),
  ];
  const gate = deferred<void>();
  let index = 0;
  installServer([
    listOf([MEMBER]),
    (call) => {
      if (call.path !== "/api/notebooks/nb-shared/memories/export") return undefined;
      const next = outcomes[index++];
      return index === 1 ? gate.promise.then(next) : next();
    },
  ]);
  render(<Host />);
  const region = await block();
  const button = () => within(region).getByRole("button", { name: /导出「封装工艺库」/ });
  await within(region).findByText(deletionNoticeText(MEMBER));

  await user.click(button());
  expect(button()).toHaveTextContent("导出中…");
  expect(button()).toBeDisabled();
  await act(async () => gate.resolve());
  await waitFor(() => expect(button()).toHaveTextContent("已导出"));
  expect(URL.createObjectURL).toHaveBeenCalled();

  await act(async () => { vi.advanceTimersByTime(2000); });
  await user.click(button());
  expect(await within(region).findByRole("alert")).toHaveTextContent(EXPORT_FAILED_TEXT);
  expect(button()).toHaveTextContent("导出失败");

  await user.click(button());
  await waitFor(() => expect(within(region).getByRole("alert")).toHaveTextContent(LEAVE_EXPORT_TOO_LARGE_TEXT));

  await user.click(button());
  await waitFor(() => expect(within(region).getByRole("alert")).toHaveTextContent(EXPORT_CLOSED_TEXT));
});

test("转移:先看到「不会回来」的提示,挑一本自己的笔记本确认;转移中… → 结果条数", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const gate = deferred<Response>();
  const calls = installServer([
    listOf([MEMBER]),
    (call) => (call.method === "POST" && call.path === "/api/notebooks/nb-shared/memories/transfer" ? gate.promise : undefined),
  ]);
  render(<Host targets={[...TARGETS, { id: "nb-other", name: "另一本我的" }]} />);
  const region = await block();
  await within(region).findByText(deletionNoticeText(MEMBER));

  await user.click(within(region).getByRole("button", { name: "转移到我的笔记本" }));
  const panel = within(region).getByRole("group", { name: "转移到我的笔记本" });
  expect(within(panel).getByText(TRANSFER_WARNING_TEXT)).toBeInTheDocument();
  await user.selectOptions(within(panel).getByRole("combobox"), "nb-other");
  await user.click(within(panel).getByRole("button", { name: "确认转移" }));
  expect(within(panel).getByRole("button", { name: "转移中…" })).toBeDisabled();
  // 取消永不禁用。
  expect(within(panel).getByRole("button", { name: "取消" })).toBeEnabled();
  const transfer = calls.find((call) => call.path.endsWith("/memories/transfer"))!;
  expect(JSON.parse(transfer.body)).toEqual({ target_notebook_id: "nb-other" });

  await act(async () => gate.resolve(json({
    results: [
      { source_id: "m1", new_id: "x1", ok: true, error: null, error_code: null, status: "moved" },
      { source_id: "m2", new_id: null, ok: false, error: "e", error_code: "delete_window_closed", status: "failed" },
    ],
    remaining: 0,
    next_after: "m2",
  })));
  expect(await within(region).findByRole("alert")).toHaveTextContent(transferDoneText(1, 1, "另一本我的"));
});

test("转移撞上截止:原因落在那一行;没有自己的笔记本时只说先导出", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  installServer([
    listOf([MEMBER]),
    (call) => (call.path === "/api/notebooks/nb-shared/memories/transfer"
      ? json({ detail: { code: "delete_window_closed" } }, 404) : undefined),
  ]);
  const view = render(<Host />);
  let region = await block();
  await within(region).findByText(deletionNoticeText(MEMBER));
  await user.click(within(region).getByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  expect(await within(region).findByRole("alert")).toHaveTextContent(TRANSFER_CLOSED_TEXT);

  view.unmount();
  render(<Host targets={[]} />);
  region = await block();
  await within(region).findByText(deletionNoticeText(MEMBER));
  await user.click(within(region).getByRole("button", { name: "转移到我的笔记本" }));
  expect(within(region).getByText(TRANSFER_NO_TARGET_TEXT)).toBeInTheDocument();
  expect(within(region).queryByRole("button", { name: "确认转移" })).toBeNull();
});

test("本地时钟过了截止:只说「正在永久删除」,没有任何控件;到点后重新拉取", async () => {
  const calls = installServer([listOf([{ ...MEMBER, delete_after: "2026-10-22T09:00:30+00:00" }])]);
  render(<Host />);
  const region = await block();
  expect(await within(region).findByRole("button", { name: /导出「封装工艺库」/ })).toBeInTheDocument();
  await act(async () => { vi.advanceTimersByTime(32_000); });
  await waitFor(() => expect(within(region).getByText(DELETION_DUE_TEXT)).toBeInTheDocument());
  expect(within(region).queryByRole("button")).toBeNull();
  expect(calls.filter((call) => call.path === "/api/me/notebook-deletions").length).toBeGreaterThan(1);
});

test("铃铛点进来的那一行高亮、聚焦一次;重新拉取不再抢焦点,高亮一会儿就收;再点一次再定位", async () => {
  installServer([listOf([OWNER, MEMBER])]);
  const focus = { id: "nb-shared", seq: 1 };
  const view = render(<Host focus={focus} />);
  const region = await block();
  const rows = await within(region).findAllByRole("listitem");
  await waitFor(() => expect(rows[1]).toHaveClass("is-focused"));
  expect(rows[0]).not.toHaveClass("is-focused");
  expect(rows[1]).toHaveFocus();

  // 焦点移走后,列表重新拉取(refreshKey 变了):不把焦点抢回去。
  rows[0].querySelector("button")!.focus();
  view.rerender(<Host focus={focus} refreshKey="k2" />);
  await act(async () => { await Promise.resolve(); });
  expect(rows[1]).not.toHaveFocus();

  await act(async () => { vi.advanceTimersByTime(5000); });
  expect(within(region).getAllByRole("listitem")[1]).not.toHaveClass("is-focused");

  view.rerender(<Host focus={{ id: "nb-shared", seq: 2 }} refreshKey="k2" />);
  await waitFor(() => expect(within(region).getAllByRole("listitem")[1]).toHaveFocus());
  expect(within(region).getAllByRole("listitem")[1]).toHaveClass("is-focused");
});

test("有了结果、服务端已不再列出的一行:结果留在原处一会儿(控件停用),之后才收起", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  let items = [MEMBER];
  const calls = installServer([
    (call) => (call.method === "GET" && call.path === "/api/me/notebook-deletions" ? json({ items }) : undefined),
    (call) => {
      if (call.method !== "POST" || call.path !== "/api/notebooks/nb-shared/memories/transfer") return undefined;
      // 全部移走之后,这本笔记本里不再有我的记忆:通知投影不再列出它。
      items = [];
      return json({
        results: [
          { source_id: "m1", new_id: "x1", ok: true, error: null, error_code: null, status: "moved" },
          { source_id: "m2", new_id: "x2", ok: true, error: null, error_code: null, status: "moved" },
        ],
        remaining: 0,
        next_after: "m2",
      });
    },
  ]);
  const view = render(<Host />);
  const region = await block();
  await within(region).findByText(deletionNoticeText(MEMBER));
  await user.click(within(region).getByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  const done = transferDoneText(2, 0, "我的笔记本");
  expect(await within(region).findByRole("status")).toHaveTextContent(done);

  // 重新拉取已经返回空列表,结果仍在,控件全部停用。
  const listed = () => calls.filter((call) => call.path === "/api/me/notebook-deletions").length;
  const before = listed();
  view.rerender(<Host refreshKey="after-transfer" />);
  await waitFor(() => expect(listed()).toBeGreaterThan(before));
  await act(async () => { await Promise.resolve(); });
  // 重新查询区块(拿着旧节点会在它被卸下后照样「找得到」)。
  const live = within(screen.getByRole("region", { name: DELETIONS_BLOCK_TITLE }));
  expect(live.getByRole("status")).toHaveTextContent(done);
  for (const button of live.getAllByRole("button")) expect(button).toBeDisabled();

  await act(async () => { vi.advanceTimersByTime(SETTLED_ROW_HOLD_MS + 100); });
  await waitFor(() => expect(view.container).toBeEmptyDOMElement());
});

test("撤销成功后列表立刻刷新、服务端不再列出:「已撤销」与说明仍留在按钮旁", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  let items = [OWNER];
  const calls = installServer([
    (call) => (call.method === "GET" && call.path === "/api/me/notebook-deletions" ? json({ items }) : undefined),
    (call) => {
      if (call.method !== "POST" || call.path !== "/api/notebooks/nb-own/cancel-delete") return undefined;
      items = [];
      return json({ id: "nb-own", name: "我的旧库" });
    },
  ]);
  const view = render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "撤销删除" }));
  expect(await within(region).findByRole("button", { name: "已撤销" })).toBeDisabled();
  // 撤销会让铃铛与列表马上刷新(refreshKey 变了)。
  const listed = () => calls.filter((call) => call.path === "/api/me/notebook-deletions").length;
  const before = listed();
  view.rerender(<Host refreshKey="after-cancel" />);
  await waitFor(() => expect(listed()).toBeGreaterThan(before));
  await act(async () => { await Promise.resolve(); });
  const live = within(screen.getByRole("region", { name: DELETIONS_BLOCK_TITLE }));
  expect(live.getByRole("button", { name: "已撤销" })).toBeDisabled();
  expect(live.getByRole("status")).toHaveTextContent(CANCEL_DONE_TEXT);
});

const moved = (id: string) => ({ source_id: id, new_id: `x-${id}`, ok: true, error: null, error_code: null, status: "moved" });

test("转移按页提交到剩余为 0,途中显示「已转移 X / N 条」,每页接着上一页", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const firstPage = deferred<Response>();
  const pages = [
    () => firstPage.promise,
    () => json({ results: [moved("m3"), moved("m4"), moved("m5")], remaining: 0, next_after: "m5" }),
  ];
  const calls = installServer([
    listOf([{ ...MEMBER, transferable_count: 5 }]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? pages.shift()!() : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  await act(async () => firstPage.resolve(json({ results: [moved("m1"), moved("m2")], remaining: 3, next_after: "m2" })));
  expect(await within(region).findByRole("status")).toHaveTextContent(transferDoneText(5, 0, "我的笔记本"));
  const bodies = calls.filter((call) => call.path.endsWith("/memories/transfer")).map((call) => JSON.parse(call.body));
  expect(bodies).toEqual([
    { target_notebook_id: "nb-mine" },
    { target_notebook_id: "nb-mine", after: "m2" },
  ]);
});

test("转移进度在两页之间可见", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const secondPage = deferred<Response>();
  const pages = [
    () => json({ results: [moved("m1"), moved("m2")], remaining: 3, next_after: "m2" }),
    () => secondPage.promise,
  ];
  installServer([
    listOf([{ ...MEMBER, transferable_count: 5 }]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? pages.shift()!() : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  expect(await within(region).findByText(transferProgressText(2, 0, 5))).toBeInTheDocument();
  expect(within(region).getByRole("button", { name: "转移中…" })).toBeDisabled();
  await act(async () => secondPage.resolve(json({ results: [moved("m3"), moved("m4"), moved("m5")], remaining: 0, next_after: "m5" })));
  expect(await within(region).findByRole("status")).toHaveTextContent(transferDoneText(5, 0, "我的笔记本"));
});

test("同一渲染周期里连点两次确认,只发起一次转移", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const gate = deferred<Response>();
  const calls = installServer([
    listOf([MEMBER]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? gate.promise : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  const confirm = within(region).getByRole("button", { name: "确认转移" });
  act(() => {
    confirm.click();
    confirm.click();
  });
  await act(async () => gate.resolve(json({ results: [moved("m1")], remaining: 0, next_after: "m1" })));
  expect(calls.filter((call) => call.path.endsWith("/memories/transfer"))).toHaveLength(1);
});

test("转移途中点「取消」:面板收起,守卫不放开,当前这一页之后不再提交", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const gate = deferred<Response>();
  const calls = installServer([
    listOf([{ ...MEMBER, transferable_count: 5 }]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? gate.promise : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  await user.click(within(region).getByRole("button", { name: "取消" }));
  expect(within(region).queryByRole("group", { name: "转移到我的笔记本" })).toBeNull();
  // 在途时不能再发起一次。
  expect(within(region).queryByRole("button", { name: "转移到我的笔记本" })).toBeNull();
  await act(async () => gate.resolve(json({ results: [moved("m1"), moved("m2")], remaining: 3, next_after: "m2" })));
  expect(await within(region).findByRole("status")).toHaveTextContent(
    transferSummaryText(2, 0, 3, "我的笔记本"),
  );
  expect(calls.filter((call) => call.path.endsWith("/memories/transfer"))).toHaveLength(1);
});

test("另一次转移正在进行(409):原因落在那一行", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  installServer([
    listOf([MEMBER]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer")
      ? json({ detail: { code: "transfer_in_progress", message: "x" } }, 409)
      : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  expect(await within(region).findByRole("alert")).toHaveTextContent(TRANSFER_BUSY_TEXT);
});

const failedItem = (id: string) => ({ source_id: id, new_id: null, ok: false, error: "e", error_code: "x", status: "failed" });

test("进度只数转走的,失败另计;结束时如实列出转走 / 没转成 / 未处理", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const secondPage = deferred<Response>();
  const pages = [
    () => json({ results: [moved("m1"), failedItem("m2")], remaining: 3, next_after: "m2" }),
    () => secondPage.promise,
  ];
  installServer([
    listOf([{ ...MEMBER, transferable_count: 5 }]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? pages.shift()!() : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  expect(await within(region).findByText(transferProgressText(1, 1, 5))).toBeInTheDocument();
  expect(transferProgressText(1, 1, 5)).toBe("已转移 1 / 5 条，1 条没有转移成功");
  await act(async () => secondPage.resolve(json({ results: [moved("m3"), moved("m4"), moved("m5")], remaining: 0, next_after: "m5" })));
  const result = await within(region).findByRole("alert");
  expect(result).toHaveTextContent(transferSummaryText(4, 1, 0, "我的笔记本"));
  expect(result).toHaveTextContent("1 条没有转移成功（仍在这本笔记本里，请先导出）");
});

test("转移途中主人撤销了删除(409 notebook_not_pending_delete):说库已恢复、剩下的留在原处", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const pages = [
    () => json({ results: [moved("m1"), moved("m2")], remaining: 3, next_after: "m2" }),
    () => json({ detail: { code: "notebook_not_pending_delete", message: "x" } }, 409),
  ];
  installServer([
    listOf([{ ...MEMBER, transferable_count: 5 }]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? pages.shift()!() : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  const result = await within(region).findByText(
    transferSummaryText(2, 0, 3, "我的笔记本", TRANSFER_RESTORED_TEXT),
  );
  expect(result).toHaveAttribute("role", "status");
  expect(result).toHaveTextContent("所有者已撤销删除，这本笔记本已恢复，剩下的记忆留在原处。");
  expect(result).toHaveTextContent("3 条未处理");
});

test("分页中途截止已到:原因在前,再列出已转走与未处理的条数", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const pages = [
    () => json({ results: [moved("m1"), moved("m2")], remaining: 3, next_after: "m2" }),
    () => json({ detail: { code: "delete_window_closed" } }, 404),
  ];
  installServer([
    listOf([{ ...MEMBER, transferable_count: 5 }]),
    (call) => (call.method === "POST" && call.path.endsWith("/memories/transfer") ? pages.shift()!() : undefined),
  ]);
  render(<Host />);
  const region = await block();
  await user.click(await within(region).findByRole("button", { name: "转移到我的笔记本" }));
  await user.click(within(region).getByRole("button", { name: "确认转移" }));
  expect(await within(region).findByRole("alert")).toHaveTextContent(
    transferSummaryText(2, 0, 3, "我的笔记本", TRANSFER_CLOSED_TEXT),
  );
});
