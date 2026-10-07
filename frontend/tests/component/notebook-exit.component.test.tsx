import { useRef, useState } from "react";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { filenameFromDisposition } from "../../app/notebook-exit-api.ts";
import { NotebookExitPanel } from "../../app/notebook-exit-panel.tsx";
import { useDialogFocus } from "../../app/use-dialog-focus.ts";
import {
  NotebookMenuActions,
  ReaderNotebookBadge,
} from "../../app/notebook-reader-actions.tsx";
import { anchorAt, anchorOf, useNotebookExit } from "../../app/use-notebook-exit.ts";
import { DestinationPicker } from "../../app/transfer-picker.tsx";
import { placePanel } from "../../app/notebook-exit-placement.ts";
import { TRANSFER_BATCH_MAX } from "../../app/memory-transfer.ts";
import { ToastRegion, TOAST_DEFAULT_MS, TOAST_EXIT_MS } from "../../app/toast-region.tsx";
import type { NotebookSummary } from "../../app/workspace-model.ts";

// 「退出共享」会永久删掉成员自己在这本笔记本里的记忆,所以先告知、允许先导出/先转移、
// 最后带着确认过的条数才 DELETE。两个入口(顶栏按钮、卡片菜单)走同一份流程:这里各用
// 一个最小宿主把真实的组件 + 真实的 hook + 同一个面板接起来,只替换网络。

const NOTEBOOK = {
  id: "nb1",
  name: "封装工艺库",
  purpose: "",
  primary_domain: "",
  status: "ready",
  counts: {},
  created_label: "2026年8月17日",
  access: "reader",
  shared_from: "carol",
} as NotebookSummary;

type Call = { method: string; path: string; search: string; body: string };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

type Handler = (call: Call) => Response | Promise<Response> | undefined;

/** 按 (method, 路径后缀) 路由的假服务端;没人接的请求直接让用例红。 */
function installServer(handlers: Handler[]) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const call: Call = {
      method: (init?.method ?? "GET").toUpperCase(),
      path: url.pathname,
      search: url.search,
      body: typeof init?.body === "string" ? init.body : "",
    };
    calls.push(call);
    for (const handler of handlers) {
      const response = handler(call);
      if (response !== undefined) return response;
    }
    throw new Error(`unexpected request ${call.method} ${call.path}${call.search}`);
  }));
  return calls;
}

const disclosure = (...counts: number[]): Handler => {
  const queue = [...counts];
  return (call) => {
    if (call.method !== "GET" || !call.path.endsWith("/membership/exit-disclosure")) return undefined;
    return json({ memory_count: queue.length > 1 ? queue.shift() : queue[0] });
  };
};

const leave = (respond: (call: Call) => Response | Promise<Response>): Handler =>
  (call) => (call.method === "DELETE" && call.path.endsWith("/membership") ? respond(call) : undefined);

/** 服务端的成功响应:带了确认数就回 200 + 它自己数到的删除条数,没带回 204。 */
const leaveOk = (): Handler => leave((call) => {
  const acknowledged = Number(new URLSearchParams(call.search).get("acknowledged_memory_count") ?? 0);
  return acknowledged > 0
    ? json({ deleted_memory_count: acknowledged })
    : new Response(null, { status: 204 });
});

const deletes = (calls: Call[]) => calls.filter((call) => call.method === "DELETE");
const disclosures = (calls: Call[]) => calls.filter((call) => call.path.endsWith("/exit-disclosure"));

function Host({
  entry,
  afterLeave,
  onToast,
  onError,
}: {
  entry: "bar" | "menu";
  afterLeave: () => Promise<void>;
  onToast: (message: string) => void;
  onError: (error: unknown) => void;
}) {
  const exit = useNotebookExit({ onToast, onError });
  return (
    <>
      {entry === "bar" ? (
        <ReaderNotebookBadge
          notebook={NOTEBOOK}
          leaveBusy={exit.isBusy(NOTEBOOK.id)}
          onLeave={(anchor) => exit.start(NOTEBOOK.id, anchor, afterLeave)}
        />
      ) : (
        <NotebookMenuActions
          notebook={NOTEBOOK}
          canManageNotebook={false}
          canDeleteNotebook={false}
          onLeave={() => exit.start(NOTEBOOK.id, anchorAt(40, 60), afterLeave)}
          onEdit={() => undefined}
          onDelete={() => undefined}
        />
      )}
      <NotebookExitPanel exit={exit} />
    </>
  );
}

function mount(entry: "bar" | "menu") {
  const afterLeave = vi.fn(async () => undefined);
  const onToast = vi.fn();
  const onError = vi.fn();
  render(<Host entry={entry} afterLeave={afterLeave} onToast={onToast} onError={onError} />);
  return { afterLeave, onToast, onError };
}

const pressLeave = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole("button", { name: "退出共享" }));

const panel = () => screen.findByRole("dialog", { name: "退出共享" });

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:exit-test");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

for (const entry of ["bar", "menu"] as const) {
  test(`${entry}:没有记忆要删时直接退出——同一条无参数 DELETE,不出现确认面板`, async () => {
    const user = userEvent.setup();
    const calls = installServer([disclosure(0), leaveOk()]);
    const { afterLeave, onToast } = mount(entry);

    await pressLeave(user);

    await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
    expect(afterLeave).toHaveBeenCalledOnce();
    expect(deletes(calls)).toEqual([
      expect.objectContaining({ path: "/api/notebooks/nb1/membership", search: "" }),
    ]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  test(`${entry}:有记忆要删时就地告知条数,确认前不发 DELETE;确认带着条数发出`, async () => {
    const user = userEvent.setup();
    const calls = installServer([disclosure(3), leaveOk()]);
    const { afterLeave, onToast } = mount(entry);

    await pressLeave(user);
    const dialog = await panel();
    expect(
      within(dialog).getByText("退出后，你在这个笔记本里的 3 条记忆会被永久删除，无法恢复。"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("可以先把它们转移到你的其他笔记本，或导出为文件保存。"),
    ).toBeInTheDocument();
    expect(deletes(calls)).toHaveLength(0);

    await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));

    await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 3 条记忆"));
    expect(deletes(calls)).toEqual([
      expect.objectContaining({ search: "?acknowledged_memory_count=3" }),
    ]);
    expect(afterLeave).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
}

test("顶栏按钮的说明文字不再声称「只移除你自己的访问」", () => {
  installServer([]);
  mount("bar");
  expect(screen.getByRole("button", { name: "退出共享" })).toHaveAttribute("title", "退出该共享笔记本");
});

test("取消不发任何请求;确认在途时取消仍可点,已发出的 DELETE 照常落地", async () => {
  const user = userEvent.setup();
  const gate = deferred<Response>();
  const calls = installServer([disclosure(2), leave(() => gate.promise)]);
  const { afterLeave, onToast } = mount("bar");

  // 先取消一次:面板收起,没有 DELETE。
  await pressLeave(user);
  let dialog = await panel();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(deletes(calls)).toHaveLength(0);

  // 再走到确认在途:确认键禁用并写「正在退出…」,取消键**不**禁用。
  await pressLeave(user);
  dialog = await panel();
  await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));
  const busyButton = await within(await panel()).findByRole("button", { name: "正在退出…" });
  expect(busyButton).toBeDisabled();
  const cancel = within(await panel()).getByRole("button", { name: "取消" });
  expect(cancel).toBeEnabled();
  await user.click(cancel);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  gate.resolve(json({ deleted_memory_count: 2 }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  expect(afterLeave).toHaveBeenCalledOnce();
  expect(deletes(calls)).toHaveLength(1);
});

test("409:就地更新为服务端的最新条数并要求重新确认,第二次确认带新数字", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  const calls = installServer([
    disclosure(3),
    leave(() => {
      attempt += 1;
      return attempt === 1
        ? json({ detail: { code: "exit_disclosure_required", memory_count: 5 } }, 409)
        : json({ deleted_memory_count: 5 });
    }),
  ]);
  const { onToast, onError } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByText("退出后，你在这个笔记本里的 5 条记忆会被永久删除，无法恢复。");
  expect(within(dialog).getByText("记忆数量有变化，请重新确认。")).toBeInTheDocument();
  expect(onToast).not.toHaveBeenCalled();
  expect(onError).not.toHaveBeenCalled();

  await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 5 条记忆"));
  expect(deletes(calls).map((call) => call.search)).toEqual([
    "?acknowledged_memory_count=3",
    "?acknowledged_memory_count=5",
  ]);
});

test("读条数时数到 0、但退出时服务端已多出记忆(409):落到确认面板,不是报错", async () => {
  const user = userEvent.setup();
  const calls = installServer([
    disclosure(0),
    leave(() => json({ detail: { code: "exit_disclosure_required", memory_count: 1 } }, 409)),
  ]);
  const { onError } = mount("menu");

  await pressLeave(user);

  const dialog = await panel();
  expect(
    within(dialog).getByText("退出后，你在这个笔记本里的 1 条记忆会被永久删除，无法恢复。"),
  ).toBeInTheDocument();
  expect(within(dialog).getByText("记忆数量有变化，请重新确认。")).toBeInTheDocument();
  expect(onError).not.toHaveBeenCalled();
  expect(deletes(calls)).toHaveLength(1);
});

test("读条数失败不盲退:给出说明与重试/取消,重试成功后才继续", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  const calls = installServer([
    (call) => {
      if (!call.path.endsWith("/exit-disclosure")) return undefined;
      attempt += 1;
      return attempt === 1 ? json({ detail: "boom" }, 500) : json({ memory_count: 0 });
    },
    leaveOk(),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  const dialog = await panel();
  expect(
    within(dialog).getByText("暂时无法确认将删除多少条记忆，请稍后重试。"),
  ).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled();
  expect(deletes(calls)).toHaveLength(0);

  await user.click(within(dialog).getByRole("button", { name: "重试" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(disclosures(calls)).toHaveLength(2);
  expect(deletes(calls)).toHaveLength(1);
});

test("读条数失败后取消:收起面板,什么都没删", async () => {
  const user = userEvent.setup();
  const calls = installServer([(call) => (call.path.endsWith("/exit-disclosure") ? json({}, 500) : undefined)]);
  mount("menu");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "取消" }));

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(deletes(calls)).toHaveLength(0);
});

for (const entry of ["bar", "menu"] as const) {
  // 菜单入口的按钮在真实页面里一点就关(没有 disabled 可依赖),顶栏按钮则会被禁用——
  // 所以「只发一次」必须由流程自己的同步闸保证,不能靠按钮的 disabled。
  test(`${entry}:双击「退出共享」,条数只读一次,DELETE 只发一次`, async () => {
    const gate = deferred<Response>();
    const calls = installServer([
      (call) => (call.path.endsWith("/exit-disclosure") ? gate.promise : undefined),
      leaveOk(),
    ]);
    const { onToast } = mount(entry);
    const button = screen.getByRole("button", { name: "退出共享" });

    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);
    if (entry === "bar") expect(await screen.findByRole("button", { name: "退出中…" })).toBeDisabled();
    gate.resolve(json({ memory_count: 0 }));

    await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
    expect(disclosures(calls)).toHaveLength(1);
    expect(deletes(calls)).toHaveLength(1);
  });
}

test("双击「确认退出并删除」:DELETE 只发一次", async () => {
  const user = userEvent.setup();
  const gate = deferred<Response>();
  const calls = installServer([disclosure(2), leave(() => gate.promise)]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  const confirm = within(await panel()).getByRole("button", { name: "确认退出并删除" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  await within(await panel()).findByRole("button", { name: "正在退出…" });
  gate.resolve(json({ deleted_memory_count: 2 }));

  await waitFor(() => expect(onToast).toHaveBeenCalled());
  expect(deletes(calls)).toHaveLength(1);
});

// 按钮的 disabled 要等 React 重渲染才生效;流程自己的同步闸(ref)才是「同一个事件循环里
// 连发两次也只有一次 DELETE」的保证。这里绕开按钮,直接在一个 act 里连调两次。
test("同一个事件循环里连调两次确认:DELETE 只发一次", async () => {
  const gate = deferred<Response>();
  const calls = installServer([disclosure(2), leave(() => gate.promise)]);
  const onToast = vi.fn();
  const { result } = renderHook(() => useNotebookExit({ onToast, onError: vi.fn() }));

  act(() => { result.current.start("nb1", anchorAt(0, 0), async () => undefined); });
  await waitFor(() => expect(result.current.flow?.phase).toBe("confirm"));
  act(() => {
    result.current.confirm();
    result.current.confirm();
  });
  gate.resolve(json({ deleted_memory_count: 2 }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  expect(deletes(calls)).toHaveLength(1);
});

test("退出请求失败:确认面板就地写出原因并保持打开,可以重试", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  installServer([
    disclosure(2),
    leave(() => {
      attempt += 1;
      return attempt === 1 ? json({ detail: "x" }, 403) : json({ deleted_memory_count: 2 });
    }),
  ]);
  const { onToast, onError } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByRole("alert");
  expect(onError).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
});

test("导出:结果落在按钮自己身上——成功「已导出」、失败「导出失败」,到点还原", async () => {
  const user = userEvent.setup();
  let exportStatus = 200;
  const clicks: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function click(this: HTMLAnchorElement) {
    clicks.push(this.download);
  });
  const calls = installServer([
    disclosure(2),
    (call) => {
      if (!call.path.endsWith("/memories/export")) return undefined;
      return exportStatus === 200
        ? new Response("# 记忆", {
          status: 200,
          headers: { "Content-Disposition": "attachment; filename=\"memories-nb1.md\"" },
        })
        : json({ detail: "x" }, 500);
    },
  ]);
  mount("bar");

  await pressLeave(user);
  const dialog = await panel();
  await user.click(within(dialog).getByRole("button", { name: "导出为文件" }));
  const done = await within(dialog).findByRole("button", { name: "已导出" });
  expect(done).toHaveClass("copy-result-copied");
  expect(clicks).toEqual(["memories-nb1.md"]);
  expect(calls.some((call) => call.path === "/api/notebooks/nb1/memories/export")).toBe(true);
  // 1.6 秒后自己回到 idle。
  await waitFor(
    () => expect(within(dialog).getByRole("button", { name: "导出为文件" })).toBeInTheDocument(),
    { timeout: 4000 },
  );

  exportStatus = 500;
  await user.click(within(dialog).getByRole("button", { name: "导出为文件" }));
  const failed = await within(dialog).findByRole("button", { name: "导出失败" });
  expect(failed).toHaveClass("copy-result-failed");
  expect(deletes(calls)).toHaveLength(0);
});

test("转移:复用既有的目标笔记本选择器;转移后就地重读条数,到 0 时改口并把确认键改成「退出共享」", async () => {
  const user = userEvent.setup();
  const calls = installServer([
    disclosure(2, 0),
    (call) => (call.path === "/api/notebooks/nb1/memories"
      ? json({
        items: [{ id: "m1", status: "confirmed" }, { id: "m2", status: "confirmed" }],
        total_count: 2, offset: 0, limit: 100,
      })
      : undefined),
    (call) => (call.method === "GET" && call.path === "/api/notebooks"
      ? json([
        { id: "nb1", name: "封装工艺库", access: "reader" },
        { id: "nb2", name: "我的库", access: "owner" },
      ])
      : undefined),
    (call) => (call.path === "/api/memories/transfer"
      ? json({
        results: [
          { source_id: "m1", new_id: "n1", ok: true, error: null, error_code: null, status: "copied" },
          { source_id: "m2", new_id: "n2", ok: true, error: null, error_code: null, status: "copied" },
        ],
      })
      : undefined),
    leaveOk(),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  const dialog = await panel();
  await user.click(within(dialog).getByRole("button", { name: "转移到其他笔记本" }));

  const picker = await screen.findByRole("dialog", { name: "复制/移动 2 条记忆" });
  await waitFor(() => expect(within(picker).getByRole("option", { name: "我的库" })).toBeInTheDocument());
  // 只列这本库里我自己的、已确认的记忆(状态过滤在请求上)。
  expect(calls.find((call) => call.path === "/api/notebooks/nb1/memories")?.search)
    .toContain("status=confirmed");
  await user.selectOptions(within(picker).getByLabelText("目标笔记本"), "nb2");
  await user.click(within(picker).getByRole("button", { name: "确认" }));

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "复制/移动 2 条记忆" })).not.toBeInTheDocument());
  const transfer = calls.find((call) => call.path === "/api/memories/transfer");
  expect(JSON.parse(transfer?.body ?? "{}")).toMatchObject({
    memory_ids: ["m1", "m2"], target_notebook_id: "nb2", mode: "copy",
  });
  const after = await panel();
  await within(after).findByText("你在这个笔记本里已经没有记忆了。");
  expect(disclosures(calls)).toHaveLength(2);
  expect(within(after).queryByRole("button", { name: "导出为文件" })).not.toBeInTheDocument();
  expect(deletes(calls)).toHaveLength(0);

  await user.click(within(after).getByRole("button", { name: "退出共享" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(deletes(calls)).toEqual([expect.objectContaining({ search: "" })]);
});

test("转移后条数仍大于 0:确认面板就地换成新数字", async () => {
  const user = userEvent.setup();
  const calls = installServer([
    disclosure(3, 1),
    (call) => (call.path === "/api/notebooks/nb1/memories"
      ? json({ items: [{ id: "m1", status: "confirmed" }], total_count: 1, offset: 0, limit: 100 })
      : undefined),
    (call) => (call.method === "GET" && call.path === "/api/notebooks"
      ? json([{ id: "nb2", name: "我的库", access: "owner" }])
      : undefined),
    (call) => (call.path === "/api/memories/transfer"
      ? json({
        results: [{ source_id: "m1", new_id: "n1", ok: true, error: null, error_code: null, status: "copied" }],
      })
      : undefined),
  ]);
  mount("menu");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "转移到其他笔记本" }));
  const picker = await screen.findByRole("dialog", { name: "复制/移动 1 条记忆" });
  await waitFor(() => expect(within(picker).getByRole("option", { name: "我的库" })).toBeInTheDocument());
  await user.selectOptions(within(picker).getByLabelText("目标笔记本"), "nb2");
  await user.click(within(picker).getByRole("button", { name: "确认" }));

  await within(await panel()).findByText("退出后，你在这个笔记本里的 1 条记忆会被永久删除，无法恢复。");
  expect(within(await panel()).getByText("已复制到「我的库」")).toBeInTheDocument();
  expect(disclosures(calls)).toHaveLength(2);
});

test("没有可转移的记忆(都不是已确认)时给出说明,不打开选择器", async () => {
  const user = userEvent.setup();
  installServer([
    disclosure(2),
    (call) => (call.path === "/api/notebooks/nb1/memories"
      ? json({ items: [], total_count: 0, offset: 0, limit: 100 })
      : undefined),
  ]);
  mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "转移到其他笔记本" }));

  await within(await panel()).findByText("没有可以转移的记忆（只有「已确认」的记忆可以转移）。");
  expect(screen.queryByRole("dialog", { name: /复制\/移动/ })).not.toBeInTheDocument();
});

test("filenameFromDisposition:优先 filename*,退到 filename,不带路径", () => {
  expect(filenameFromDisposition("attachment; filename=\"a.md\"")).toBe("a.md");
  expect(filenameFromDisposition("attachment; filename=a.md")).toBe("a.md");
  expect(
    filenameFromDisposition("attachment; filename=\"x.md\"; filename*=UTF-8''%E8%AE%B0%E5%BF%86.md"),
  ).toBe("记忆.md");
  expect(filenameFromDisposition("attachment; filename=\"../../etc/x.md\"")).toBe("x.md");
  expect(filenameFromDisposition(null)).toBeNull();
  expect(filenameFromDisposition("attachment")).toBeNull();
});

// ---------------------------------------------------------------------------------------
// 结果如实(契约 v2):每个数字取自服务端响应;网络中断是「不知道」;晚到的结果照样告知。
// ---------------------------------------------------------------------------------------

const notebooksList = (present: boolean, grantedVia = false): Handler => (call) => (
  call.method === "GET" && call.path === "/api/notebooks"
    ? json(present
      ? [{
        id: "nb1", name: "封装工艺库", access: "reader",
        granted_via: grantedVia ? [{ id: "g1", name: "组" }] : undefined,
      }]
      : [])
    : undefined
);

const incomplete = (status: 409 | 503, deleted: number, remaining: number) =>
  json({ detail: { code: "exit_incomplete", deleted_memory_count: deleted, memory_count: remaining } }, status);

test("成功提示里的条数是服务端删掉的条数,不是用户确认时看到的那个", async () => {
  const user = userEvent.setup();
  installServer([disclosure(3), leave(() => json({ deleted_memory_count: 2 }))]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
});

test("409 告知条数变成 0:面板说没有要删的记忆,确认键是「退出共享」,再按发无参数 DELETE", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  const calls = installServer([
    disclosure(3),
    leave(() => {
      attempt += 1;
      return attempt === 1
        ? json({ detail: { code: "exit_disclosure_required", memory_count: 0 } }, 409)
        : new Response(null, { status: 204 });
    }),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByText("你在这个笔记本里已经没有记忆了。");
  expect(within(dialog).getByText("现在没有需要删除的记忆了，可以直接退出。")).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "退出共享" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(deletes(calls).map((call) => call.search)).toEqual(["?acknowledged_memory_count=3", ""]);
});

test("409 exit_incomplete:面板留着,说已删 d 条、新增 r 条,条数换成 r,并按契约重读一次告知", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  const calls = installServer([
    disclosure(3, 2),
    leave(() => {
      attempt += 1;
      return attempt === 1 ? incomplete(409, 3, 2) : json({ deleted_memory_count: 2 });
    }),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByText("已删除 3 条记忆。退出期间又新增了 2 条，确认后会删除它们并退出。");
  expect(within(dialog).getByText("退出后，你在这个笔记本里的 2 条记忆会被永久删除，无法恢复。")).toBeInTheDocument();
  await waitFor(() => expect(disclosures(calls)).toHaveLength(2)); // 未完成之后从告知重新开始
  expect(onToast).not.toHaveBeenCalled();

  await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  expect(deletes(calls).map((call) => call.search)).toEqual([
    "?acknowledged_memory_count=3",
    "?acknowledged_memory_count=2",
  ]);
});

test("503 exit_incomplete:面板留着,说已删几条、还剩几条、仍是成员;一条没删时明说没删", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  installServer([
    disclosure(4, 4),
    leave(() => {
      attempt += 1;
      return attempt === 1 ? incomplete(503, 2, 2) : incomplete(503, 0, 4);
    }),
  ]);
  const { onToast, onError } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));
  const dialog = await panel();
  await within(dialog).findByText("退出没有完成：已删除 2 条记忆，还剩 2 条，你仍是成员。可以重试。");
  expect(within(dialog).getByRole("alert")).toHaveTextContent("已删除 2 条记忆");

  // 重读告知(4)之后再试一次,这回一条也没删掉。
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeEnabled());
  await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));
  await within(dialog).findByText("退出没有完成：没有删除任何记忆，还剩 4 条，你仍是成员。可以重试。");
  expect(onToast).not.toHaveBeenCalled();
  expect(onError).not.toHaveBeenCalled();
});

test("网络中断:重读列表与告知——已经退出了就说已退出,不说失败", async () => {
  const user = userEvent.setup();
  installServer([
    disclosure(3),
    leave(() => Promise.reject(new TypeError("Failed to fetch"))),
    notebooksList(false),
  ]);
  const { afterLeave, onToast, onError } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(afterLeave).toHaveBeenCalledOnce();
  expect(onError).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("网络中断后列表里这本库只剩群组授权:算已退出(靠授权继续读),不说失败", async () => {
  const user = userEvent.setup();
  installServer([
    disclosure(3),
    leave(() => Promise.reject(new TypeError("Failed to fetch"))),
    notebooksList(true, true),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
});

test("网络中断后,经个人或「所有人」授权仍可读的库不在列表里:算已退出,不说「仍是成员」", async () => {
  // 列表只列自有库、成员行与群组授权;个人授权与「所有人」授权从不进列表。
  const user = userEvent.setup();
  installServer([
    disclosure(3),
    leave(() => Promise.reject(new TypeError("Failed to fetch"))),
    notebooksList(false),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(onToast).not.toHaveBeenCalledWith(expect.stringContaining("仍是成员"));
});

test("面板开着时焦点掉到 body(按下的按钮被禁用或卸载)就收回到落点", () => {
  function Layer({ disabled }: { disabled: boolean }) {
    const containerRef = useRef<HTMLDivElement | null>(null);
    const cancelRef = useRef<HTMLButtonElement | null>(null);
    useDialogFocus({ containerRef, active: true, initialFocusRef: cancelRef });
    return (
      <div ref={containerRef} role="dialog">
        <button type="button" disabled={disabled}>导出为文件</button>
        <button ref={cancelRef} type="button">取消</button>
      </div>
    );
  }
  const { rerender } = render(<Layer disabled={false} />);
  const cancel = screen.getByRole("button", { name: "取消" });
  const exportButton = screen.getByRole("button", { name: "导出为文件" });
  expect(cancel).toHaveFocus();
  exportButton.focus();
  expect(exportButton).toHaveFocus();
  // 浏览器里被禁用的按钮把焦点丢到 body;jsdom 不会,这里显式丢一次再渲染。
  exportButton.blur();
  expect(document.activeElement).toBe(document.body);
  rerender(<Layer disabled />);
  expect(cancel).toHaveFocus();
});

test("代理超时(500)同样是「不知道」;重读到仍是成员且条数没变:只说还剩几条,不说「都还在」——确认过的被删、同时又新存了几条,总数照样不变", async () => {
  const user = userEvent.setup();
  installServer([
    disclosure(3, 3),
    leave(() => json({ detail: "timeout" }, 500)),
    notebooksList(true),
  ]);
  const { onToast, onError } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByText("退出的结果没有收到，重新核对后：你仍是成员，还有 3 条记忆。可以重试。");
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeEnabled();
  expect(onToast).not.toHaveBeenCalled();
  expect(onError).not.toHaveBeenCalled();
});

test("网络中断后重读到条数变少:只说还剩几条,不推断「删了一部分」,也不自己算差值", async () => {
  const user = userEvent.setup();
  installServer([
    disclosure(5, 2),
    leave(() => Promise.reject(new TypeError("Failed to fetch"))),
    notebooksList(true),
  ]);
  mount("menu");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByText("退出的结果没有收到，重新核对后：你仍是成员，还有 2 条记忆。可以重试。");
  expect(within(dialog).getByText("退出后，你在这个笔记本里的 2 条记忆会被永久删除，无法恢复。")).toBeInTheDocument();
});

test("网络中断后连核对也读不到:说无法确认,给「重试」和「取消」,不说失败", async () => {
  const user = userEvent.setup();
  installServer([
    disclosure(3),
    leave(() => Promise.reject(new TypeError("Failed to fetch"))),
    (call) => (call.method === "GET" && call.path === "/api/notebooks" ? json({}, 500) : undefined),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  const dialog = await panel();
  await within(dialog).findByText("暂时无法确认是否已经退出，请刷新页面查看笔记本列表。");
  expect(within(dialog).getByRole("button", { name: "重试" })).toBeEnabled();
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled();
  expect(onToast).not.toHaveBeenCalled();
});

test("条数为 0 的直接退出失败:结果落在面板(按钮旁),不只走页面横幅;可以就地再试", async () => {
  const user = userEvent.setup();
  let attempt = 0;
  const calls = installServer([
    disclosure(0),
    leave(() => {
      attempt += 1;
      return attempt === 1 ? json({ detail: "x" }, 403) : new Response(null, { status: 204 });
    }),
  ]);
  const { onToast, onError } = mount("bar");

  await pressLeave(user);

  const dialog = await panel();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("没有权限进行这个操作");
  expect(onError).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "退出共享" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(deletes(calls)).toHaveLength(2);
});

test("面板已被取消之后才到的结果照样告知:409 未完成、503、网络中断核对", async () => {
  const cases: Array<[string, () => Promise<Response> | Response, string, Handler[]]> = [
    ["409 exit_incomplete", () => incomplete(409, 3, 2),
      "已删除 3 条记忆，但退出没有完成：期间又新增了 2 条，你仍是成员。", []],
    ["503 exit_incomplete", () => incomplete(503, 1, 2),
      "退出没有完成：已删除 1 条记忆，还剩 2 条，你仍是成员。可以重试。", []],
    ["网络中断,核对出已退出", () => Promise.reject(new TypeError("Failed to fetch")),
      "已退出共享", [notebooksList(false)]],
    ["网络中断,核对出仍是成员", () => Promise.reject(new TypeError("Failed to fetch")),
      "退出的结果没有收到，重新核对后：你仍是成员，还有 3 条记忆。可以重试。", [notebooksList(true)]],
    ["网络中断,核对不出", () => Promise.reject(new TypeError("Failed to fetch")),
      "暂时无法确认是否已经退出，请刷新页面查看笔记本列表。",
      [(call) => (call.path === "/api/notebooks" ? json({}, 500) : undefined)]],
  ];
  for (const [label, respond, expected, extra] of cases) {
    const user = userEvent.setup();
    const gate = deferred<void>();
    installServer([disclosure(3), leave(async () => { await gate.promise; return respond(); }), ...extra]);
    const { onToast } = mount("bar");

    await pressLeave(user);
    await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));
    await user.click(within(await panel()).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    gate.resolve();

    await waitFor(() => expect(onToast, label).toHaveBeenCalledWith(expected));
    expect(screen.queryByRole("dialog"), label).not.toBeInTheDocument();
    cleanup();
    vi.unstubAllGlobals();
  }
});

test("刷新列表失败也要告诉用户退出已经发生:先报错,再给成功提示", async () => {
  const user = userEvent.setup();
  installServer([disclosure(3), leave(() => json({ deleted_memory_count: 3 }))]);
  const afterLeave = vi.fn(async () => { throw new Error("list down"); });
  const onToast = vi.fn();
  const onError = vi.fn();
  render(<Host entry="bar" afterLeave={afterLeave} onToast={onToast} onError={onError} />);

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 3 条记忆"));
  expect(onError).toHaveBeenCalledOnce();
});

// F6:条数请求已 resolve 为 0、续行还没跑时用户取消——之后不许再发 DELETE。
test("条数读到 0 之后、续行执行之前取消:不发 DELETE", async () => {
  const body = deferred<unknown>();
  const seen: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    seen.push(`${(init?.method ?? "GET").toUpperCase()} ${url.pathname}`);
    if (url.pathname.endsWith("/exit-disclosure")) {
      return { ok: true, status: 200, headers: new Headers(), json: () => body.promise } as unknown as Response;
    }
    throw new Error(`unexpected ${url.pathname}`);
  }));
  const { result } = renderHook(() => useNotebookExit({ onToast: vi.fn(), onError: vi.fn() }));

  act(() => { result.current.start("nb1", anchorAt(0, 0), async () => undefined); });
  await waitFor(() => expect(seen).toHaveLength(1));
  await act(async () => {
    body.resolve({ memory_count: 0 });
    result.current.cancel(); // 同一个同步段里:续行(await 之后)还没跑
  });
  await new Promise((done) => setTimeout(done, 30));

  expect(seen.filter((entry) => entry.startsWith("DELETE"))).toEqual([]);
});

// F7 及其后继:取消不再让一次在途 DELETE 把「另一本」笔记本的按钮吞掉;同一本再按则接回在途请求。
test("第一本的 DELETE 在途且已取消:第二本照常发起,两边的结果各自告知、各调各的刷新", async () => {
  const gate = deferred<Response>();
  const calls = installServer([
    (call) => (call.path === "/api/notebooks/nb1/membership/exit-disclosure" ? json({ memory_count: 2 }) : undefined),
    (call) => (call.path === "/api/notebooks/nb2/membership/exit-disclosure" ? json({ memory_count: 0 }) : undefined),
    (call) => (call.method === "DELETE" && call.path === "/api/notebooks/nb1/membership" ? gate.promise : undefined),
    (call) => (call.method === "DELETE" && call.path === "/api/notebooks/nb2/membership" ? new Response(null, { status: 204 }) : undefined),
  ]);
  const onToast = vi.fn();
  const after1 = vi.fn(async () => undefined);
  const after2 = vi.fn(async () => undefined);
  const { result } = renderHook(() => useNotebookExit({ onToast, onError: vi.fn() }));

  act(() => { result.current.start("nb1", anchorAt(0, 0), after1); });
  await waitFor(() => expect(result.current.flow?.phase).toBe("confirm"));
  act(() => { result.current.confirm(); });
  await waitFor(() => expect(result.current.isBusy("nb1")).toBe(true));
  act(() => { result.current.cancel(); });
  expect(result.current.isBusy("nb1")).toBe(true);
  expect(result.current.isBusy("nb2")).toBe(false);

  act(() => { result.current.start("nb2", anchorAt(0, 0), after2); });
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(after2).toHaveBeenCalledOnce();
  expect(after1).not.toHaveBeenCalled();

  gate.resolve(json({ deleted_memory_count: 2 }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  expect(after1).toHaveBeenCalledOnce();
  expect(deletes(calls).map((call) => call.path)).toEqual([
    "/api/notebooks/nb1/membership",
    "/api/notebooks/nb2/membership",
  ]);
});

test("同一本笔记本的 DELETE 还没落地时再按:接回在途请求(正在退出…),不发第二个 DELETE 也不重读条数", async () => {
  const gate = deferred<Response>();
  const calls = installServer([disclosure(2), leave(() => gate.promise)]);
  const onToast = vi.fn();
  const after = vi.fn(async () => undefined);
  const { result } = renderHook(() => useNotebookExit({ onToast, onError: vi.fn() }));

  act(() => { result.current.start("nb1", anchorAt(0, 0), after); });
  await waitFor(() => expect(result.current.flow?.phase).toBe("confirm"));
  act(() => { result.current.confirm(); });
  await waitFor(() => expect(result.current.flow?.leaving).toBe(true));
  act(() => { result.current.cancel(); });
  expect(result.current.flow).toBeNull();

  act(() => { result.current.start("nb1", anchorAt(0, 0), after); });
  expect(result.current.flow).toMatchObject({ phase: "leaving", leaving: true });
  expect(result.current.isBusy("nb1")).toBe(true);
  expect(deletes(calls)).toHaveLength(1);
  expect(disclosures(calls)).toHaveLength(1);

  gate.resolve(json({ deleted_memory_count: 2 }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  expect(result.current.flow).toBeNull();
  expect(after).toHaveBeenCalledOnce();
});

// ---------------------------------------------------------------------------------------
// 键盘与读屏
// ---------------------------------------------------------------------------------------

test("打开时焦点进「取消」(不是破坏性按钮);面板是模态对话框,带名称与含条数的描述", async () => {
  const user = userEvent.setup();
  installServer([disclosure(3)]);
  mount("bar");

  await pressLeave(user);
  const dialog = await panel();

  expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).not.toHaveFocus();
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog).toHaveAccessibleName("退出共享");
  expect(dialog).toHaveAccessibleDescription(/退出后，你在这个笔记本里的 3 条记忆会被永久删除，无法恢复。/);
});

test("Tab / Shift+Tab 只在面板内循环,焦点在面板外时 Tab 把它拉进来", async () => {
  const user = userEvent.setup();
  installServer([disclosure(3)]);
  mount("bar");
  const opener = screen.getByRole("button", { name: "退出共享" });

  await pressLeave(user);
  const dialog = await panel();
  const first = within(dialog).getByRole("button", { name: "导出为文件" });
  const last = within(dialog).getByRole("button", { name: "确认退出并删除" });

  last.focus();
  fireEvent.keyDown(last, { key: "Tab" });
  expect(first).toHaveFocus();
  fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
  expect(last).toHaveFocus();

  opener.focus();
  fireEvent.keyDown(opener, { key: "Tab" });
  expect(first).toHaveFocus();
  opener.focus();
  fireEvent.keyDown(opener, { key: "Tab", shiftKey: true });
  expect(last).toHaveFocus();
});

test("Escape 关闭面板、什么都不发,焦点回到打开它的按钮", async () => {
  const user = userEvent.setup();
  const calls = installServer([disclosure(3)]);
  mount("bar");
  const opener = screen.getByRole("button", { name: "退出共享" });

  await pressLeave(user);
  await panel();
  await user.keyboard("{Escape}");

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await waitFor(() => expect(opener).toHaveFocus());
  expect(deletes(calls)).toHaveLength(0);
});

test("点「取消」同样把焦点还给打开它的按钮", async () => {
  const user = userEvent.setup();
  installServer([disclosure(3)]);
  mount("bar");
  const opener = screen.getByRole("button", { name: "退出共享" });

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "取消" }));

  await waitFor(() => expect(opener).toHaveFocus());
});

test("退出请求在途时 Escape 不关面板(结果必须给用户看);「取消」仍可用", async () => {
  const user = userEvent.setup();
  const gate = deferred<Response>();
  installServer([disclosure(2), leave(() => gate.promise)]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));
  await within(await panel()).findByRole("button", { name: "正在退出…" });
  await user.keyboard("{Escape}");

  expect(screen.getByRole("dialog", { name: "退出共享" })).toBeInTheDocument();
  expect(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" })).toBeEnabled();
  gate.resolve(json({ deleted_memory_count: 2 }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

test("退出成功后打开它的控件已经消失:焦点落到页面主体,不丢在 body 上", async () => {
  const user = userEvent.setup();
  installServer([disclosure(2), leaveOk()]);
  function GoneHost() {
    const [shown, setShown] = useState(true);
    const exit = useNotebookExit({ onToast: vi.fn(), onError: vi.fn() });
    return (
      <>
        <main aria-label="页面主体"><h1>笔记本</h1></main>
        {shown && (
          <button
            type="button"
            onClick={(event) => exit.start("nb1", anchorOf(event.currentTarget), async () => setShown(false))}
          >
            退出共享
          </button>
        )}
        <NotebookExitPanel exit={exit} />
      </>
    );
  }
  render(<GoneHost />);

  await pressLeave(user);
  await user.click(within(await panel()).getByRole("button", { name: "确认退出并删除" }));

  await waitFor(() => expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument());
  await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
});

// ---------------------------------------------------------------------------------------
// 结果提示:role=status、停留时长
// ---------------------------------------------------------------------------------------

test("提示区域始终是 role=status 的 live region;默认 2.2 秒消失,退出共享的提示停留更久", () => {
  vi.useFakeTimers();
  try {
    const onExpire = vi.fn();
    const view = render(<ToastRegion message="" onExpire={onExpire} />);
    expect(screen.getByRole("status")).toBeInTheDocument(); // 没有内容时区域也在
    expect(screen.getByRole("status")).toBeEmptyDOMElement();

    view.rerender(<ToastRegion message="已保存" onExpire={onExpire} />);
    expect(within(screen.getByRole("status")).getByText("已保存")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(TOAST_DEFAULT_MS - 1); });
    expect(onExpire).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(onExpire).toHaveBeenCalledOnce();

    const exitExpire = vi.fn();
    view.rerender(
      <ToastRegion message="已退出共享，已删除 3 条记忆" lingerMs={TOAST_EXIT_MS} onExpire={exitExpire} />,
    );
    act(() => { vi.advanceTimersByTime(TOAST_DEFAULT_MS + 1000); });
    expect(exitExpire).not.toHaveBeenCalled();
    expect(TOAST_EXIT_MS).toBeGreaterThanOrEqual(6000); // 不短于待确认中心的完工提示
    act(() => { vi.advanceTimersByTime(TOAST_EXIT_MS); });
    expect(exitExpire).toHaveBeenCalledOnce();
  } finally {
    vi.useRealTimers();
  }
});

// ---------------------------------------------------------------------------------------
// 转移:分批、取消可用
// ---------------------------------------------------------------------------------------

const memoriesPage = (total: number): Handler => (call) => {
  if (call.path !== "/api/notebooks/nb1/memories") return undefined;
  const query = new URLSearchParams(call.search);
  const offset = Number(query.get("offset") ?? 0);
  const limit = Number(query.get("limit") ?? 100);
  const size = Math.max(0, Math.min(limit, total - offset));
  return json({
    items: Array.from({ length: size }, (_, index) => ({ id: `m${offset + index}`, status: "confirmed" })),
    total_count: total, offset, limit,
  });
};

const targetNotebooks: Handler = (call) => (
  call.method === "GET" && call.path === "/api/notebooks"
    ? json([
      { id: "nb1", name: "封装工艺库", access: "reader" },
      { id: "nb2", name: "我的库", access: "owner" },
    ])
    : undefined
);

const transferAll = (batches: string[][], respond?: (index: number) => Response | undefined): Handler => (call) => {
  if (call.path !== "/api/memories/transfer") return undefined;
  const ids = (JSON.parse(call.body) as { memory_ids: string[] }).memory_ids;
  batches.push(ids);
  const custom = respond?.(batches.length);
  if (custom) return custom;
  return json({
    results: ids.map((id) => ({ source_id: id, new_id: `n-${id}`, ok: true, error: null, error_code: null, status: "copied" })),
  });
};

async function openPicker(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(within(await panel()).getByRole("button", { name: "转移到其他笔记本" }));
  const picker = await screen.findByRole("dialog", { name: title });
  await waitFor(() => expect(within(picker).getByRole("option", { name: "我的库" })).toBeInTheDocument());
  await user.selectOptions(within(picker).getByLabelText("目标笔记本"), "nb2");
  return picker;
}

test("250 条已确认记忆的转移分成 200 + 50 两批提交,汇总成一句话,并重读告知", async () => {
  const user = userEvent.setup();
  const batches: string[][] = [];
  const calls = installServer([disclosure(250, 0), memoriesPage(250), targetNotebooks, transferAll(batches)]);
  mount("bar");

  await pressLeave(user);
  const picker = await openPicker(user, "复制/移动 250 条记忆");
  await user.click(within(picker).getByRole("button", { name: "确认" }));

  const dialog = await panel();
  await within(dialog).findByText("已复制 250 条记忆到「我的库」");
  expect(batches.map((batch) => batch.length)).toEqual([TRANSFER_BATCH_MAX, 50]);
  expect(new Set(batches.flat()).size).toBe(250);
  await within(dialog).findByText("你在这个笔记本里已经没有记忆了。");
  expect(disclosures(calls)).toHaveLength(2);
});

test("某一批整体失败不中断后面的批次:汇总里如实写成功与失败条数", async () => {
  const user = userEvent.setup();
  const batches: string[][] = [];
  const calls = installServer([
    disclosure(250, 200),
    memoriesPage(250),
    targetNotebooks,
    transferAll(batches, (index) => (index === 1 ? json({ detail: "x" }, 500) : undefined)),
  ]);
  mount("bar");

  await pressLeave(user);
  const picker = await openPicker(user, "复制/移动 250 条记忆");
  await user.click(within(picker).getByRole("button", { name: "确认" }));

  const dialog = await panel();
  await within(dialog).findByText("已复制 50 条记忆到「我的库」，另有 200 条没有成功。");
  expect(batches.map((batch) => batch.length)).toEqual([TRANSFER_BATCH_MAX, 50]);
  await within(dialog).findByText("退出后，你在这个笔记本里的 200 条记忆会被永久删除，无法恢复。");
  expect(disclosures(calls)).toHaveLength(2);
});

test("所有批次都失败:选择器留着并就地写出原因(可以换个目标重试),不重读条数", async () => {
  const user = userEvent.setup();
  const batches: string[][] = [];
  const calls = installServer([
    disclosure(250),
    memoriesPage(250),
    targetNotebooks,
    transferAll(batches, () => json({ detail: "x" }, 403)),
  ]);
  mount("bar");

  await pressLeave(user);
  const picker = await openPicker(user, "复制/移动 250 条记忆");
  await user.click(within(picker).getByRole("button", { name: "确认" }));

  expect(await within(picker).findByRole("alert")).toHaveTextContent("没有权限进行这个操作");
  expect(screen.getByRole("dialog", { name: "复制/移动 250 条记忆" })).toBeInTheDocument();
  expect(batches).toHaveLength(2);
  expect(disclosures(calls)).toHaveLength(1);
});

test("选择器焦点:打开时进选择器;Escape 只关选择器、面板还在,焦点回到面板", async () => {
  const user = userEvent.setup();
  installServer([disclosure(2), memoriesPage(2), targetNotebooks]);
  mount("bar");

  await pressLeave(user);
  const picker = await openPicker(user, "复制/移动 2 条记忆");
  expect(picker.contains(document.activeElement)).toBe(true);

  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "复制/移动 2 条记忆" })).not.toBeInTheDocument());
  const dialog = screen.getByRole("dialog", { name: "退出共享" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
});

test("转移提交中选择器的「取消」可用:关掉后面板显示「正在转移…」,结果落地照常汇报并重读条数", async () => {
  const user = userEvent.setup();
  const gate = deferred<Response>();
  const calls = installServer([
    disclosure(2, 0),
    memoriesPage(2),
    targetNotebooks,
    (call) => (call.path === "/api/memories/transfer" ? gate.promise : undefined),
  ]);
  mount("bar");

  await pressLeave(user);
  const picker = await openPicker(user, "复制/移动 2 条记忆");
  await user.click(within(picker).getByRole("button", { name: "确认" }));
  await within(picker).findByRole("button", { name: "处理中…" });
  const pickerCancel = within(picker).getByRole("button", { name: "取消" });
  expect(pickerCancel).toBeEnabled();
  await user.click(pickerCancel);

  const dialog = await panel();
  expect(screen.queryByRole("dialog", { name: "复制/移动 2 条记忆" })).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "正在转移…" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled();

  gate.resolve(json({
    results: ["m0", "m1"].map((id) => ({ source_id: id, new_id: `n-${id}`, ok: true, error: null, error_code: null, status: "copied" })),
  }));
  await within(dialog).findByText("你在这个笔记本里已经没有记忆了。");
  expect(within(dialog).getByText("已复制 2 条记忆到「我的库」")).toBeInTheDocument();
  expect(disclosures(calls)).toHaveLength(2);
});

test("面板被取消之后转移才落地:结果用提示告知", async () => {
  const user = userEvent.setup();
  const gate = deferred<Response>();
  installServer([
    disclosure(2), memoriesPage(2), targetNotebooks,
    (call) => (call.path === "/api/memories/transfer" ? gate.promise : undefined),
  ]);
  const { onToast } = mount("bar");

  await pressLeave(user);
  const picker = await openPicker(user, "复制/移动 2 条记忆");
  await user.click(within(picker).getByRole("button", { name: "确认" }));
  await user.click(within(picker).getByRole("button", { name: "取消" }));
  await user.click(within(await panel()).getByRole("button", { name: "取消" }));
  gate.resolve(json({
    results: ["m0", "m1"].map((id) => ({ source_id: id, new_id: `n-${id}`, ok: true, error: null, error_code: null, status: "copied" })),
  }));

  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已复制 2 条记忆到「我的库」"));
});

test("旧流程的转移还在途时重开退出、打开选择器又取消:新面板不被卡在「正在转移…」,旧转移落地只发提示", async () => {
  // codex #820 r2:在途转移曾是全局标记,新流程关掉选择器时被它置成「正在转移…」;旧转移落地
  // 时 epoch 不匹配、不更新新流程,面板就永远卡住,转移与确认退出都点不了。
  const user = userEvent.setup();
  const gate = deferred<Response>();
  let transfers = 0;
  installServer([
    disclosure(2), memoriesPage(2), targetNotebooks,
    (call) => {
      if (call.path !== "/api/memories/transfer") return undefined;
      transfers += 1;
      return gate.promise;
    },
  ]);
  const { onToast } = mount("bar");

  // 旧流程:发起转移 → 关掉选择器 → 取消退出面板(请求仍在途)
  await pressLeave(user);
  const oldPicker = await openPicker(user, "复制/移动 2 条记忆");
  await user.click(within(oldPicker).getByRole("button", { name: "确认" }));
  await within(oldPicker).findByRole("button", { name: "处理中…" });
  await user.click(within(oldPicker).getByRole("button", { name: "取消" }));
  await user.click(within(await panel()).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "退出共享" })).not.toBeInTheDocument());

  // 新流程:打开选择器,不提交就取消
  await pressLeave(user);
  const newPicker = await openPicker(user, "复制/移动 2 条记忆");
  await user.click(within(newPicker).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "复制/移动 2 条记忆" })).not.toBeInTheDocument());

  const dialog = await panel();
  expect(within(dialog).queryByRole("button", { name: "正在转移…" })).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "转移到其他笔记本" })).toBeEnabled();
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeEnabled();

  // 旧转移落地:只以提示告知,新面板仍可操作
  gate.resolve(json({
    results: ["m0", "m1"].map((id) => ({ source_id: id, new_id: `n-${id}`, ok: true, error: null, error_code: null, status: "copied" })),
  }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已复制 2 条记忆到「我的库」"));
  expect(within(dialog).queryByRole("button", { name: "正在转移…" })).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "转移到其他笔记本" })).toBeEnabled();
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeEnabled();
  expect(transfers).toBe(1);
});

test("旧流程的转移落地不会清掉新流程自己在途的转移:新流程关掉选择器后仍显示「正在转移…」,自己的结果照常落在面板", async () => {
  const user = userEvent.setup();
  const gates = [deferred<Response>(), deferred<Response>()];
  let transfers = 0;
  installServer([
    disclosure(2), memoriesPage(2), targetNotebooks,
    (call) => {
      if (call.path !== "/api/memories/transfer") return undefined;
      transfers += 1;
      return gates[transfers - 1].promise;
    },
  ]);
  const { onToast } = mount("bar");
  const copied = () => json({
    results: ["m0", "m1"].map((id) => ({ source_id: id, new_id: `n-${id}`, ok: true, error: null, error_code: null, status: "copied" })),
  });

  // 旧流程发起转移后连同面板一起取消
  await pressLeave(user);
  const oldPicker = await openPicker(user, "复制/移动 2 条记忆");
  await user.click(within(oldPicker).getByRole("button", { name: "确认" }));
  await within(oldPicker).findByRole("button", { name: "处理中…" });
  await user.click(within(oldPicker).getByRole("button", { name: "取消" }));
  await user.click(within(await panel()).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "退出共享" })).not.toBeInTheDocument());

  // 新流程也发起转移;旧转移先落地
  await pressLeave(user);
  const newPicker = await openPicker(user, "复制/移动 2 条记忆");
  await user.click(within(newPicker).getByRole("button", { name: "确认" }));
  await within(newPicker).findByRole("button", { name: "处理中…" });
  gates[0].resolve(copied());
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已复制 2 条记忆到「我的库」"));

  // 新流程关掉自己的选择器:它的转移还在途
  await user.click(within(newPicker).getByRole("button", { name: "取消" }));
  const dialog = await panel();
  expect(within(dialog).getByRole("button", { name: "正在转移…" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled();

  gates[1].resolve(copied());
  await within(dialog).findByText("已复制 2 条记忆到「我的库」");
  expect(within(dialog).getByRole("button", { name: "转移到其他笔记本" })).toBeEnabled();
  expect(transfers).toBe(2);
});

test("另一本笔记本的导出还在下载时打开这本的退出面板:这本的导出键不显示「正在导出…」、可以点", async () => {
  const OTHER = { ...NOTEBOOK, id: "nb9", name: "另一本" } as NotebookSummary;
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  const gate = deferred<Response>();
  const calls = installServer([
    disclosure(2),
    (call) => {
      if (!call.path.endsWith("/memories/export")) return undefined;
      if (call.path === "/api/notebooks/nb1/memories/export") return gate.promise;
      return new Response("# mine", {
        status: 200,
        headers: { "Content-Type": "text/markdown", "Content-Disposition": 'attachment; filename="m.md"' },
      });
    },
  ]);
  function TwoNotebooks() {
    const exit = useNotebookExit({ onToast: () => undefined, onError: () => undefined });
    return (
      <>
        {[NOTEBOOK, OTHER].map((notebook) => (
          <ReaderNotebookBadge
            key={notebook.id}
            notebook={notebook}
            leaveBusy={exit.isBusy(notebook.id)}
            onLeave={(anchor) => exit.start(notebook.id, anchor, async () => undefined)}
          />
        ))}
        <NotebookExitPanel exit={exit} />
      </>
    );
  }
  const user = userEvent.setup();
  render(<TwoNotebooks />);

  await user.click(screen.getAllByRole("button", { name: "退出共享" })[0]);
  await user.click(within(await panel()).getByRole("button", { name: "导出为文件" }));
  await within(await panel()).findByRole("button", { name: "正在导出…" });
  await user.click(within(await panel()).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "退出共享" })).not.toBeInTheDocument());

  await user.click(screen.getAllByRole("button", { name: "退出共享" })[1]);
  const dialog = await panel();
  const exportButton = await within(dialog).findByRole("button", { name: "导出为文件" });
  expect(exportButton).toBeEnabled();
  await user.click(exportButton);
  await within(dialog).findByRole("button", { name: "已导出" });
  expect(calls.some((call) => call.path === "/api/notebooks/nb9/memories/export")).toBe(true);
  gate.resolve(new Response("# first", { status: 200, headers: { "Content-Type": "text/markdown" } }));
});

const WAIT_FOR_EXPORT = "正在导出，导出完成后才能退出或转移，以免文件缺少内容。";

test("导出进行中:确认退出与转移都不可用、旁边说明原因,取消可用;关掉再开仍要等;导出完成后恢复", async () => {
  // codex #820 r3(P1):导出按页读取,导出没完就清除,后面的页读到空,下载「成功」却只有前一截。
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  const user = userEvent.setup();
  const gate = deferred<Response>();
  const calls = installServer([
    disclosure(2),
    (call) => (call.path === "/api/notebooks/nb1/memories/export" ? gate.promise : undefined),
  ]);
  mount("bar");

  await pressLeave(user);
  let dialog = await panel();
  await user.click(within(dialog).getByRole("button", { name: "导出为文件" }));
  await within(dialog).findByRole("button", { name: "正在导出…" });
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "转移到其他笔记本" })).toBeDisabled();
  expect(within(dialog).getByText(WAIT_FOR_EXPORT)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled();

  // 关掉再打开:同一本笔记本的导出仍在途,照样要等
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "退出共享" })).not.toBeInTheDocument());
  await pressLeave(user);
  dialog = await panel();
  expect(await within(dialog).findByRole("button", { name: "确认退出并删除" })).toBeDisabled();
  expect(within(dialog).getByText(WAIT_FOR_EXPORT)).toBeInTheDocument();

  gate.resolve(new Response("# 记忆", {
    status: 200,
    headers: { "Content-Disposition": "attachment; filename=\"memories-nb1.md\"" },
  }));
  await within(dialog).findByRole("button", { name: "已导出" });
  expect(within(dialog).getByRole("button", { name: "确认退出并删除" })).toBeEnabled();
  expect(within(dialog).getByRole("button", { name: "转移到其他笔记本" })).toBeEnabled();
  expect(within(dialog).queryByText(WAIT_FOR_EXPORT)).not.toBeInTheDocument();
  expect(deletes(calls)).toHaveLength(0);
});

test("导出进行中,绕过界面直接调用 confirm / openTransfer / submitTransfer 也都被拒绝", async () => {
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  const gate = deferred<Response>();
  const calls = installServer([
    disclosure(2), memoriesPage(2),
    (call) => (call.path === "/api/notebooks/nb1/memories/export" ? gate.promise : undefined),
    (call) => (call.path === "/api/memories/transfer" ? json({ results: [] }) : undefined),
  ]);
  const { result } = renderHook(() => useNotebookExit({ onToast: () => undefined, onError: () => undefined }));

  act(() => { result.current.start("nb1", anchorAt(40, 60), async () => undefined); });
  await waitFor(() => expect(result.current.flow?.phase).toBe("confirm"));
  let exported!: Promise<void>;
  act(() => { exported = result.current.exportMemories(); });
  await waitFor(() => expect(result.current.exporting).toBe(true));

  act(() => { result.current.confirm(); });
  await act(async () => { await result.current.openTransfer(); });
  await expect(result.current.submitTransfer("nb2", "move", "我的库")).rejects.toThrow(WAIT_FOR_EXPORT);
  expect(deletes(calls)).toHaveLength(0);
  expect(calls.some((call) => call.path === "/api/notebooks/nb1/memories")).toBe(false);
  expect(calls.some((call) => call.path === "/api/memories/transfer")).toBe(false);
  expect(result.current.flow?.transfer).toBe("idle");

  gate.resolve(new Response("# 记忆", { status: 200 }));
  await act(async () => { await exported; });
  expect(result.current.exporting).toBe(false);
});

test("目标笔记本选择器的默认行为不变:提交中「取消」仍是禁用的(记忆页/知识表沿用)", async () => {
  const user = userEvent.setup();
  installServer([targetNotebooks]);
  render(
    <DestinationPicker
      sourceNotebookId="nb1"
      allowMove
      title="复制/移动 1 条记忆"
      onCancel={() => undefined}
      onSubmit={() => new Promise<void>(() => undefined)}
    />,
  );
  const picker = await screen.findByRole("dialog", { name: "复制/移动 1 条记忆" });
  await waitFor(() => expect(within(picker).getByRole("option", { name: "我的库" })).toBeInTheDocument());
  await user.selectOptions(within(picker).getByLabelText("目标笔记本"), "nb2");
  await user.click(within(picker).getByRole("button", { name: "确认" }));

  expect(await within(picker).findByRole("button", { name: "处理中…" })).toBeDisabled();
  expect(within(picker).getByRole("button", { name: "取消" })).toBeDisabled();
});

// ---------------------------------------------------------------------------------------
// 落点
// ---------------------------------------------------------------------------------------

test("placePanel:优先在入口正下方;下方放不下翻到上方;两边都放不下贴视口底边;四边夹住", () => {
  const viewport = { width: 1000, height: 700 };
  const panelSize = { width: 360, height: 200 };
  const box = (left: number, top: number, width = 90, height = 30) => ({
    left, top, right: left + width, bottom: top + height,
  });
  // 正下方:顶边 = 入口底边 + 8
  expect(placePanel(box(100, 100), panelSize, viewport)).toEqual({ left: 100, top: 138 });
  // 靠右:右边夹在视口内(留 8)
  expect(placePanel(box(900, 100), panelSize, viewport)).toEqual({ left: 632, top: 138 });
  // 靠左越界:左边夹到 8
  expect(placePanel(box(-40, 100), panelSize, viewport).left).toBe(8);
  // 最后一行:下方放不下,翻到入口上方且不盖住入口
  expect(placePanel(box(100, 640), panelSize, viewport)).toEqual({ left: 100, top: 432 });
  // 上下都放不下:贴视口底边(不出屏)
  expect(placePanel(box(100, 150), { width: 360, height: 640 }, viewport).top).toBe(52);
  // 面板比视口还高:顶边夹在 8(面板自己 max-height + 滚动)
  expect(placePanel(box(100, 150), { width: 360, height: 900 }, viewport).top).toBe(8);
  // 入口已滚出视口(在上方):面板仍留在视口内
  expect(placePanel(box(100, -300), panelSize, viewport).top).toBeGreaterThanOrEqual(8);
  expect(placePanel(box(100, 2000), panelSize, viewport).top).toBeLessThanOrEqual(700 - 200 - 8);
});

test("面板跟着入口走:页面滚动、窗口缩放后重新量入口再摆", async () => {
  const user = userEvent.setup();
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(360);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(200);
  installServer([disclosure(3)]);
  mount("bar");
  const opener = screen.getByRole("button", { name: "退出共享" });
  const rect = (top: number) => ({
    left: 100, right: 190, top, bottom: top + 30, width: 90, height: 30, x: 100, y: top, toJSON: () => ({}),
  }) as DOMRect;
  const measure = vi.spyOn(opener, "getBoundingClientRect").mockReturnValue(rect(300));

  await pressLeave(user);
  const dialog = await panel();
  expect(dialog.style.top).toBe("338px");
  expect(dialog.style.left).toBe("100px");

  measure.mockReturnValue(rect(120)); // 页面滚动了
  act(() => { window.dispatchEvent(new Event("scroll")); });
  expect(dialog.style.top).toBe("158px");

  measure.mockReturnValue(rect(650)); // 滚到视口底部(jsdom 视口高 768):放不下,翻到上方
  act(() => { window.dispatchEvent(new Event("scroll")); });
  expect(dialog.style.top).toBe("442px");

  Object.defineProperty(window, "innerWidth", { configurable: true, value: 300 });
  act(() => { window.dispatchEvent(new Event("resize")); }); // 窗口缩窄:左边夹住
  expect(dialog.style.left).toBe("8px");
});
