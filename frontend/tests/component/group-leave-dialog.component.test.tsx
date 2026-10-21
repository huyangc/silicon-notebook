import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { COPY_RESULT_HOLD_MS } from "../../app/copy-result.ts";
import { parseGroupLeavePreview } from "../../app/group-api.ts";
import {
  GroupLeaveDialog,
  LEAVE_EXPORT_TOO_LARGE_TEXT,
  LEAVE_KEPT_TEXT,
  LEAVE_NOTHING_HIDDEN_TEXT,
  LEAVE_PREVIEW_UNAVAILABLE_TEXT,
  LEAVE_WAIT_FOR_EXPORT_TEXT,
  leaveHiddenText,
} from "../../app/group-leave-dialog.tsx";
import { anchorAt } from "../../app/use-notebook-exit.ts";

// 「退出群组」确认面板:先读服务端的退群预览(会看不到几个笔记本、几条本人的记忆),
// 逐本列出并允许先导出;没有会看不到的记忆时如实说、不出现导出入口。只替换网络,
// 面板、导出客户端与下载 helper 都是真的。

type Call = { method: string; path: string };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

type Handler = (call: Call) => Response | Promise<Response> | undefined;

function installServer(handlers: Handler[]) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const call: Call = { method: (init?.method ?? "GET").toUpperCase(), path: url.pathname };
    calls.push(call);
    for (const handler of handlers) {
      const response = handler(call);
      if (response !== undefined) return response;
    }
    throw new Error(`unexpected request ${call.method} ${call.path}`);
  }));
  return calls;
}

const PREVIEW = {
  notebook_count: 2,
  memory_count: 5,
  notebooks: [
    { notebook_id: "nb-a", name: "封装工艺库", memory_count: 3 },
    { notebook_id: "nb-b", name: "", memory_count: 2 },
  ],
};

const previewOf = (body: unknown, status = 200): Handler => (call) =>
  call.method === "GET" && call.path === "/api/groups/g1/membership/leave-preview"
    ? json(body, status)
    : undefined;

const exportOk = (id: string): Handler => (call) =>
  call.path === `/api/notebooks/${id}/memories/export`
    ? new Response("# 记忆", {
      status: 200,
      headers: { "Content-Disposition": `attachment; filename="memories-${id}.md"` },
    })
    : undefined;

function mount(props: Partial<Parameters<typeof GroupLeaveDialog>[0]> = {}) {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  const view = render(
    <GroupLeaveDialog
      groupId="g1"
      anchor={anchorAt(40, 60)}
      leaving={false}
      failure=""
      onCancel={onCancel}
      onConfirm={onConfirm}
      {...props}
    />,
  );
  return { onCancel, onConfirm, ...view };
}

const panel = () => screen.findByRole("dialog", { name: "退出群组" });

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:leave-test");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("有会看不到的记忆:说清几个笔记本、几条,逐本列出;记忆不删,重新加入后可见", async () => {
  const user = userEvent.setup();
  installServer([previewOf(PREVIEW)]);
  const { onConfirm } = mount();

  const dialog = await panel();
  expect(await within(dialog).findByText(leaveHiddenText(2, 5))).toBeInTheDocument();
  expect(within(dialog).getByText("退出后，你将看不到 2 个笔记本里你的 5 条记忆。")).toBeInTheDocument();
  expect(within(dialog).getByText(LEAVE_KEPT_TEXT)).toBeInTheDocument();
  const list = within(dialog).getByRole("list", { name: "退出后会看不到的笔记本" });
  const rows = within(list).getAllByRole("listitem");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("封装工艺库");
  expect(rows[0]).toHaveTextContent("3 条");
  expect(rows[1]).toHaveTextContent("未命名笔记本");
  expect(within(rows[1]).getByRole("button", { name: "导出「未命名笔记本」里你的 2 条记忆" })).toBeEnabled();
  // 打开时焦点在「取消」,不是破坏性按钮。
  expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();

  await user.click(within(dialog).getByRole("button", { name: "确认退出" }));
  expect(onConfirm).toHaveBeenCalledOnce();
});

test("没有会看不到的记忆:如实说明,不出现导出入口", async () => {
  installServer([previewOf({ notebook_count: 0, memory_count: 0, notebooks: [] })]);
  mount();

  const dialog = await panel();
  expect(await within(dialog).findByText(LEAVE_NOTHING_HIDDEN_TEXT)).toBeInTheDocument();
  expect(within(dialog).getByText("退出后，你将失去经本群组获得的知识库访问权。")).toBeInTheDocument();
  expect(within(dialog).queryByRole("list")).not.toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /导出/ })).not.toBeInTheDocument();
  expect(within(dialog).queryByText(/条记忆/)).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认退出" })).toBeEnabled();
});

test("预览读到之前不能确认;读不到时说明原因、可以重试,没有确认按钮", async () => {
  const user = userEvent.setup();
  let fail = true;
  const gate = deferred<Response>();
  const calls = installServer([
    (call) => {
      if (call.path !== "/api/groups/g1/membership/leave-preview") return undefined;
      if (fail) return json({ detail: "boom" }, 500);
      return gate.promise;
    },
  ]);
  mount();

  const dialog = await panel();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(LEAVE_PREVIEW_UNAVAILABLE_TEXT);
  expect(within(dialog).queryByRole("button", { name: "确认退出" })).not.toBeInTheDocument();

  fail = false;
  await user.click(within(dialog).getByRole("button", { name: "重试" }));
  expect(within(dialog).getByText("正在确认退出后会看不到哪些记忆…")).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认退出" })).toBeDisabled();
  await act(async () => { gate.resolve(json(PREVIEW)); });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认退出" })).toBeEnabled());
  expect(calls.filter((call) => call.path.endsWith("/leave-preview"))).toHaveLength(2);
});

test("读不懂的预览当失败处理,不放人确认", async () => {
  installServer([previewOf({ notebook_count: 1, memory_count: "3", notebooks: [] })]);
  mount();
  const dialog = await panel();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(LEAVE_PREVIEW_UNAVAILABLE_TEXT);
  expect(() => parseGroupLeavePreview({ notebook_count: 1, memory_count: 1, notebooks: [{ notebook_id: "", name: "", memory_count: 1 }] }))
    .toThrow();
  expect(parseGroupLeavePreview(PREVIEW)).toEqual(PREVIEW);
});

test("导出只导这一本:结果落在按钮自身;导出没完不能确认退出", async () => {
  const user = userEvent.setup();
  const gate = deferred<Response>();
  const calls = installServer([
    previewOf(PREVIEW),
    (call) => (call.path === "/api/notebooks/nb-a/memories/export" ? gate.promise : undefined),
  ]);
  mount();

  const dialog = await panel();
  const row = (await within(dialog).findAllByRole("listitem"))[0];
  await user.click(within(row).getByRole("button", { name: "导出「封装工艺库」里你的 3 条记忆" }));
  expect(within(row).getByRole("button", { name: /导出「封装工艺库」/ })).toHaveTextContent("正在导出…");
  expect(within(row).getByRole("button", { name: /导出「封装工艺库」/ })).toBeDisabled();
  expect(within(dialog).getByText(LEAVE_WAIT_FOR_EXPORT_TEXT)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "确认退出" })).toBeDisabled();
  // 取消永不禁用。
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled();

  await act(async () => {
    gate.resolve(new Response("# 记忆", {
      status: 200,
      headers: { "Content-Disposition": "attachment; filename=\"memories-nb-a.md\"" },
    }));
  });
  await waitFor(() => expect(within(row).getByRole("button", { name: /导出「封装工艺库」/ })).toHaveTextContent("已导出"));
  expect(within(row).getByRole("button", { name: /导出「封装工艺库」/ })).toHaveClass("copy-result-copied");
  expect(URL.createObjectURL).toHaveBeenCalledOnce();
  expect(within(dialog).getByRole("button", { name: "确认退出" })).toBeEnabled();
  expect(within(dialog).queryByText(LEAVE_WAIT_FOR_EXPORT_TEXT)).not.toBeInTheDocument();
  // 导出范围:只请求了被点的那一本,另一本没有被导出。
  const exports = calls.filter((call) => call.path.endsWith("/memories/export"));
  expect(exports).toEqual([{ method: "GET", path: "/api/notebooks/nb-a/memories/export" }]);
  // 结果态到点还原。
  await waitFor(
    () => expect(within(row).getByRole("button", { name: /导出「封装工艺库」/ })).toHaveTextContent(/^导出$/),
    { timeout: COPY_RESULT_HOLD_MS + 2000 },
  );
});

test("导出超过上限:按钮显示「导出失败」,原因留在那一行下面", async () => {
  const user = userEvent.setup();
  installServer([
    previewOf(PREVIEW),
    exportOk("nb-a"),
    (call) => (call.path === "/api/notebooks/nb-b/memories/export"
      ? json({ detail: { code: "export_too_large", memory_count: 2, limit_bytes: 10 } }, 413)
      : undefined),
  ]);
  mount();

  const dialog = await panel();
  const row = (await within(dialog).findAllByRole("listitem"))[1];
  await user.click(within(row).getByRole("button", { name: /导出「未命名笔记本」/ }));
  await waitFor(() => expect(within(row).getByRole("button", { name: /导出「未命名笔记本」/ })).toHaveTextContent("导出失败"));
  expect(within(row).getByRole("alert")).toHaveTextContent(LEAVE_EXPORT_TOO_LARGE_TEXT);
});

test("退出请求在途:确认按钮显示「正在退出…」并禁用,取消仍可用;Escape 不关", async () => {
  const user = userEvent.setup();
  installServer([previewOf(PREVIEW)]);
  const { onCancel } = mount({ leaving: true });

  const dialog = await panel();
  await within(dialog).findByText(leaveHiddenText(2, 5));
  const confirm = within(dialog).getByRole("button", { name: "正在退出…" });
  expect(confirm).toBeDisabled();
  await user.keyboard("{Escape}");
  expect(onCancel).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(onCancel).toHaveBeenCalledOnce();
});

test("没有在途请求时 Escape 关闭;退出失败的原因落在面板里", async () => {
  const user = userEvent.setup();
  installServer([previewOf(PREVIEW)]);
  const { onCancel } = mount({ failure: "你是这个群组唯一的组管理员,请先指定其他组管理员再退出" });

  const dialog = await panel();
  expect(within(dialog).getByRole("alert")).toHaveTextContent("唯一的组管理员");
  await user.keyboard("{Escape}");
  expect(onCancel).toHaveBeenCalledOnce();
});
