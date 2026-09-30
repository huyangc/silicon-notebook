import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { filenameFromDisposition } from "../../app/notebook-exit-api.ts";
import { NotebookExitPanel } from "../../app/notebook-exit-panel.tsx";
import {
  NotebookMenuActions,
  ReaderNotebookBadge,
} from "../../app/notebook-reader-actions.tsx";
import { anchorAt, useNotebookExit } from "../../app/use-notebook-exit.ts";
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
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
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
