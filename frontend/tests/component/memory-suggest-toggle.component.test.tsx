import { useEffect, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import type { AuthUser, MemoryMode } from "../../app/auth";
import { MemoryPanel } from "../../app/memory-panel";
import { MemorySuggestToggle } from "../../app/memory-suggest-toggle";
import { MEMORY_PAGE_SIZE } from "../../app/memory-model";
import { MEMORY_SUGGEST_NOTICE_MS, useMemorySuggestMode } from "../../app/use-memory-suggest-mode";
import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator";
import { ReleaseNotesModal } from "../../app/release-notes-modal";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const sessionSignal = new AbortController().signal;
function user(id: string, memory_mode: MemoryMode): AuthUser {
  return { id, email: "", display_name: id, username: id, role: "user", ui_mode: "auto", memory_mode, search_profile: null };
}
function page(total: unknown = 0) {
  return { items: [], total_count: total, offset: 0, limit: MEMORY_PAGE_SIZE, owner_total_count: 900, owner_pending_count: 800, notebook_options: [] };
}
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

type HarnessProps = {
  actor?: string;
  initialMode?: MemoryMode;
  enabled?: boolean;
  modelReady?: boolean;
  notebookId?: string | null;
  show?: boolean;
  signal?: AbortSignal;
  panel?: boolean;
  onChanged?: (user: AuthUser) => void;
  announcement?: boolean;
};
function Harness({ actor = "actor-a", initialMode = "manual", enabled = true, modelReady = true, notebookId = "nb-1", show = true, signal = sessionSignal, panel = false, onChanged = () => undefined, announcement = false }: HarnessProps) {
  const [updated, setUpdated] = useState<AuthUser | null>(null);
  const currentUser = updated?.id === actor ? updated : user(actor, initialMode);
  const modals = useRootModalCoordinator({ actorId: actor, sourceId: null, onClosed: (slot) => {
    if (slot === "memory-suggest-close") owner.commands.clearConfirmation();
  } });
  const owner = useMemorySuggestMode({ user: currentUser, sessionSignal: signal, modals, onUserChanged: (value) => { onChanged(value); setUpdated(value); } });
  useEffect(() => {
    if (announcement) modals.open("release-notes", modals.captureActorOwner());
  }, [announcement]); // eslint-disable-line react-hooks/exhaustive-deps
  const suggestionProps = { enabled, modelReady, view: owner.view, onSetMode: owner.commands.setMode, modalView: modals.view("memory-suggest-close"), onOpenConfirmation: owner.commands.openConfirmation, onCloseConfirmation: owner.commands.closeConfirmation };
  return <>
    <span aria-label="当前用户模式">{actor}:{owner.view.mode}</span>
    {show && (panel
      ? <MemoryPanel key={notebookId} scope={notebookId ? "notebook" : "global"} notebookId={notebookId} sessionSignal={signal} memorySuggest={suggestionProps} />
      : <MemorySuggestToggle key={notebookId} {...suggestionProps} sessionSignal={signal} />)}
    {modals.view("release-notes").open && <ReleaseNotesModal build={{ version: "test", ordinal: 1 }} notes={[]} interactive={modals.view("release-notes").topmost} zIndex={modals.view("release-notes").zIndex} onClose={() => { modals.requestClose("release-notes", "button"); }} />}
  </>;
}

function mockApi({ total = 37, patch = async (body: { memory_mode: MemoryMode }) => json({ user: user("actor-a", body.memory_mode), deleted_suggestion_count: 0 }) }: {
  total?: unknown;
  patch?: (body: { memory_mode: MemoryMode; delete_unadopted: boolean }) => Promise<Response>;
} = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/me/memory-mode")) return patch(JSON.parse(String(init?.body)));
    return json(page(url.searchParams.get("origin") === "suggested" && url.searchParams.get("status") === "candidate" ? total : 900));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
function patchCalls(fetchMock: ReturnType<typeof mockApi>) {
  return fetchMock.mock.calls.filter(([input]) => String(input).includes("/me/memory-mode"));
}

test("部署关闭或旧后端缺省时整个区块不显示、不请求计数", () => {
  const fetchMock = mockApi();
  render(<Harness enabled={false} />);
  expect(screen.queryByText(/记忆建议：/)).toBeNull();
  expect(screen.queryByRole("button", { name: "开启" })).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("模型未就绪：控件旁显示原因，开启不可提交", () => {
  const fetchMock = mockApi();
  render(<Harness modelReady={false} />);
  expect(screen.getByText("记忆建议暂不可用：管理员尚未配置模型")).toBeVisible();
  expect(screen.getByRole("button", { name: "开启" })).toBeDisabled();
  expect(fetchMock).not.toHaveBeenCalled();
});

test.each(["nb-readonly", null])("记忆页 %s 的开启是句中控件，个人设置不要求笔记本编辑权限", async (notebookId) => {
  const fetchMock = mockApi();
  render(<Harness panel notebookId={notebookId} />);
  const action = screen.getByRole("button", { name: "开启" });
  expect(action).toHaveClass("link-button", "inline-action");
  expect(action.parentElement).toHaveTextContent("记忆建议：已关闭");
  await userEvent.setup().click(action);
  expect(await screen.findByRole("status")).toHaveTextContent("已开启记忆建议");
  expect(screen.getByLabelText("当前用户模式")).toHaveTextContent("actor-a:suggest");
  expect(JSON.parse(String(patchCalls(fetchMock)[0][1]?.body))).toEqual({ memory_mode: "suggest", delete_unadopted: false });
});

test("关闭默认不勾批删；独立全局 candidate suggested total 不受当前笔记本和列表影响", async () => {
  const fetchMock = mockApi({ total: 73 });
  render(<Harness panel initialMode="suggest" notebookId="nb-readonly" />);
  const client = userEvent.setup();
  await client.selectOptions(screen.getByLabelText("状态"), "rejected");
  await client.type(screen.getByRole("searchbox"), "筛选词");
  await client.click(screen.getByRole("button", { name: "搜索" }));
  await client.click(screen.getByRole("button", { name: "关闭" }));
  const checkbox = await screen.findByRole("checkbox", { name: "同时删除 73 条未处理的建议" });
  expect(checkbox).not.toBeChecked();
  const countCall = fetchMock.mock.calls.find(([input]) => {
    const params = new URL(String(input), "http://localhost").searchParams;
    return params.get("origin") === "suggested" && params.get("status") === "candidate";
  });
  const url = new URL(String(countCall?.[0]), "http://localhost");
  expect(url.pathname).toBe("/api/memories");
  expect(Object.fromEntries(url.searchParams)).toEqual({ status: "candidate", origin: "suggested", offset: "0", limit: String(MEMORY_PAGE_SIZE) });
  await userEvent.setup().click(screen.getByRole("button", { name: "确认关闭" }));
  expect(await screen.findByRole("status")).toHaveTextContent("已关闭记忆建议");
  expect(JSON.parse(String(patchCalls(fetchMock)[0][1]?.body))).toEqual({ memory_mode: "manual", delete_unadopted: false });
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("取消后重新打开关闭弹窗仍默认不勾", async () => {
  mockApi();
  const client = userEvent.setup();
  render(<Harness initialMode="suggest" />);
  await client.click(screen.getByRole("button", { name: "关闭" }));
  await client.click(await screen.findByRole("checkbox", { name: /同时删除/ }));
  await client.click(screen.getByRole("button", { name: "取消" }));
  await client.click(screen.getByRole("button", { name: "关闭" }));
  expect(await screen.findByRole("checkbox", { name: /同时删除/ })).not.toBeChecked();
});

test("勾选删除后使用实际删除数提示，并刷新当前列表", async () => {
  const fetchMock = mockApi({ total: 73, patch: async () => json({ user: user("actor-a", "manual"), deleted_suggestion_count: 2 }) });
  const client = userEvent.setup();
  render(<Harness panel initialMode="suggest" />);
  await client.click(screen.getByRole("button", { name: "关闭" }));
  await client.click(await screen.findByRole("checkbox", { name: /同时删除 73/ }));
  const beforeList = fetchMock.mock.calls.filter(([url]) => String(url).includes("/notebooks/")).length;
  await client.click(screen.getByRole("button", { name: "确认关闭" }));
  expect(await screen.findByRole("status")).toHaveTextContent("已关闭记忆建议，已删除 2 条未处理的建议");
  expect(JSON.parse(String(patchCalls(fetchMock)[0][1]?.body)).delete_unadopted).toBe(true);
  await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/notebooks/")).length).toBeGreaterThan(beforeList));
});

test.each(["failed", -1, 1.5, "9", null])("计数失败或坏值 %s 不显示零、不允许勾选批删，仍能关闭而保留建议", async (total) => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const fetchMock = mockApi({ total });
  if (total === "failed") fetchMock.mockImplementation(async (input, init) => String(input).includes("/me/memory-mode")
    ? json({ user: user("actor-a", JSON.parse(String(init?.body)).memory_mode), deleted_suggestion_count: 0 })
    : json({ detail: "oops" }, 500));
  render(<Harness initialMode="suggest" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "关闭" }));
  const checkbox = await screen.findByRole("checkbox", { name: "未能读取未处理的建议数量" });
  expect(checkbox).toBeDisabled();
  expect(checkbox).not.toBeChecked();
  expect(screen.queryByText("同时删除 0 条未处理的建议")).toBeNull();
  expect(screen.getByRole("button", { name: "确认关闭" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "重试" })).toHaveClass("inline-action");
  await userEvent.setup().click(screen.getByRole("button", { name: "确认关闭" }));
  expect(await screen.findByRole("status")).toHaveTextContent("已关闭记忆建议");
  expect(JSON.parse(String(patchCalls(fetchMock)[0][1]?.body)).delete_unadopted).toBe(false);
});

test("数量读取失败可在弹窗内重试，重试成功后才可勾选", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const fetchMock = mockApi();
  fetchMock.mockResolvedValueOnce(json({ detail: "oops" }, 500));
  const client = userEvent.setup();
  render(<Harness initialMode="suggest" />);
  await client.click(screen.getByRole("button", { name: "关闭" }));
  await client.click(await screen.findByRole("button", { name: "重试" }));
  const checkbox = await screen.findByRole("checkbox", { name: "同时删除 37 条未处理的建议" });
  expect(checkbox).toBeEnabled();
  expect(checkbox).not.toBeChecked();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("开启与关闭请求期间双击只提交一次，关闭确认按钮保持禁用", async () => {
  const pending = deferred<Response>();
  const fetchMock = mockApi({ patch: () => pending.promise });
  render(<Harness initialMode="suggest" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "关闭" }));
  await screen.findByRole("checkbox", { name: /同时删除/ });
  const confirm = screen.getByRole("button", { name: "确认关闭" });
  fireEvent.click(confirm); fireEvent.click(confirm);
  expect(patchCalls(fetchMock)).toHaveLength(1);
  expect(screen.getByRole("button", { name: "正在关闭…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  await act(async () => pending.resolve(json({ user: user("actor-a", "manual"), deleted_suggestion_count: 0 })));
});

test("请求失败就地提示，保持模式和弹窗，可重试", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const fetchMock = mockApi({ patch: async () => json({ detail: { code: "memory_suggest_unavailable", message: "记忆建议暂不可用，请稍后重试" } }, 409) });
  render(<Harness initialMode="suggest" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "关闭" }));
  await userEvent.setup().click(screen.getByRole("button", { name: "确认关闭" }));
  expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("记忆建议暂不可用，请稍后重试");
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(screen.getByLabelText("当前用户模式")).toHaveTextContent("suggest");
  expect(screen.getByRole("button", { name: "确认关闭" })).toBeEnabled();
  expect(patchCalls(fetchMock)).toHaveLength(1);
});

test("切换笔记本或隐藏面板不释放全局在飞守卫；成功仍更新本人模式", async () => {
  const pending = deferred<Response>();
  const fetchMock = mockApi({ patch: () => pending.promise });
  const client = userEvent.setup();
  const mounted = render(<Harness notebookId="nb-1" />);
  await client.dblClick(screen.getByRole("button", { name: "开启" }));
  mounted.rerender(<Harness notebookId="nb-2" />);
  expect(screen.getByRole("button", { name: "正在设置…" })).toBeDisabled();
  expect(patchCalls(fetchMock)).toHaveLength(1);
  mounted.rerender(<Harness notebookId="nb-2" show={false} />);
  await act(async () => pending.resolve(json({ user: user("actor-a", "suggest"), deleted_suggestion_count: 0 })));
  expect(screen.getByLabelText("当前用户模式")).toHaveTextContent("actor-a:suggest");
  mounted.rerender(<Harness notebookId="nb-2" />);
  expect(screen.getByRole("status")).toHaveTextContent("已开启记忆建议");
});

test.each(["another actor", "a-b-a", "new session"])("迟到回包不能更新换号/新会话：%s", async (transition) => {
  const pending = deferred<Response>();
  mockApi({ patch: () => pending.promise });
  const onChanged = vi.fn();
  const mounted = render(<Harness onChanged={onChanged} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "开启" }));
  if (transition === "new session") mounted.rerender(<Harness onChanged={onChanged} signal={new AbortController().signal} />);
  else {
    mounted.rerender(<Harness onChanged={onChanged} actor="actor-b" />);
    if (transition === "a-b-a") mounted.rerender(<Harness onChanged={onChanged} actor="actor-a" />);
  }
  await act(async () => pending.resolve(json({ user: user("actor-a", "suggest"), deleted_suggestion_count: 0 })));
  expect(onChanged).not.toHaveBeenCalled();
  expect(screen.getByLabelText("当前用户模式")).toHaveTextContent(transition === "another actor" ? "actor-b:manual" : "actor-a:manual");
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByRole("button", { name: "开启" })).toBeEnabled();
});

test("切换账号后迟到的计数不出现在新账号，也不会保留旧的批删选择", async () => {
  const pending = deferred<Response>();
  const fetchMock = mockApi();
  fetchMock.mockReturnValueOnce(pending.promise);
  const mounted = render(<Harness initialMode="suggest" />);
  await userEvent.setup().click(screen.getByRole("button", { name: "关闭" }));
  mounted.rerender(<Harness actor="actor-b" initialMode="suggest" />);
  await act(async () => pending.resolve(json(page(999))));
  expect(screen.queryByRole("dialog")).toBeNull();
  await userEvent.setup().click(screen.getByRole("button", { name: "关闭" }));
  const checkbox = await screen.findByRole("checkbox", { name: "同时删除 37 条未处理的建议" });
  expect(checkbox).not.toBeChecked();
  expect(screen.queryByText(/999 条未处理/)).toBeNull();
});

test("旧账号回包不释放新账号的在飞守卫", async () => {
  const first = deferred<Response>();
  const second = deferred<Response>();
  let request = 0;
  const fetchMock = mockApi({ patch: () => ++request === 1 ? first.promise : second.promise });
  const onChanged = vi.fn();
  const client = userEvent.setup();
  const mounted = render(<Harness onChanged={onChanged} />);
  await client.click(screen.getByRole("button", { name: "开启" }));
  mounted.rerender(<Harness actor="actor-b" onChanged={onChanged} />);
  await client.click(screen.getByRole("button", { name: "开启" }));
  await act(async () => first.resolve(json({ user: user("actor-a", "suggest"), deleted_suggestion_count: 0 })));
  expect(onChanged).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "正在设置…" })).toBeDisabled();
  expect(patchCalls(fetchMock)).toHaveLength(2);
  await act(async () => second.resolve(json({ user: user("actor-b", "suggest"), deleted_suggestion_count: 0 })));
  expect(onChanged).toHaveBeenCalledOnce();
  expect(screen.getByLabelText("当前用户模式")).toHaveTextContent("actor-b:suggest");
});

test("成功提示使用自己的定时器自动清除", async () => {
  mockApi();
  vi.useFakeTimers();
  render(<Harness />);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "开启" })); });
  expect(screen.getByRole("status")).toHaveTextContent("已开启记忆建议");
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGEST_NOTICE_MS); });
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByRole("button", { name: "关闭" })).toBeVisible();
});

test.each([false, true])("失败反馈使用自有定时器自动清除（取消确认弹窗：%s）", async (cancelDialog) => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  mockApi({ patch: async () => json({ detail: { code: "memory_suggest_unavailable", message: "记忆建议暂不可用，请稍后重试" } }, 409) });
  vi.useFakeTimers();
  render(<Harness initialMode="suggest" />);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "关闭" })); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "确认关闭" })); });
  if (cancelDialog) fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(screen.getByRole("alert")).toHaveTextContent("记忆建议暂不可用，请稍后重试");
  if (!cancelDialog) expect(within(screen.getByRole("dialog")).getByRole("alert")).toBeVisible();
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGEST_NOTICE_MS - 1); });
  expect(screen.getByRole("alert")).toBeVisible();
  await act(async () => { vi.advanceTimersByTime(1); });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByLabelText("当前用户模式")).toHaveTextContent("actor-a:suggest");
});

test("操作失败提示到期不清除未知计数的安全状态", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  mockApi({ total: -1, patch: async () => json({ detail: { code: "memory_suggest_unavailable", message: "记忆建议暂不可用，请稍后重试" } }, 409) });
  vi.useFakeTimers();
  render(<Harness initialMode="suggest" />);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "关闭" })); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "确认关闭" })); });
  expect(screen.getAllByRole("alert")).toHaveLength(2);
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGEST_NOTICE_MS); });
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(screen.getByRole("alert")).toHaveTextContent("建议数量暂不可用，请重试");
  expect(screen.getByRole("checkbox", { name: "未能读取未处理的建议数量" })).toBeDisabled();
  expect(screen.queryByText("同时删除 0 条未处理的建议")).toBeNull();
});

test("晚到更新公告覆盖确认窗时仅公告可交互，关闭后恢复确认窗焦点与批删选择", async () => {
  mockApi();
  const client = userEvent.setup();
  const mounted = render(<Harness initialMode="suggest" />);
  const opener = screen.getByRole("button", { name: "关闭" });
  await client.click(opener);
  const confirmation = screen.getByRole("dialog", { name: "关闭记忆建议" });
  expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
  const checkbox = await screen.findByRole("checkbox", { name: /同时删除/ });
  await client.click(checkbox);
  expect(checkbox).toHaveFocus();
  mounted.rerender(<Harness initialMode="suggest" announcement />);
  const announcement = screen.getByText("系统已更新").closest('[role="dialog"]')!;
  expect(confirmation).toHaveAttribute("aria-modal", "false");
  expect(confirmation).toHaveAttribute("aria-hidden", "true");
  expect(confirmation).toHaveAttribute("inert");
  expect(announcement).toHaveAttribute("aria-modal", "true");
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(Number((announcement as HTMLElement).style.zIndex)).toBeGreaterThan(Number(confirmation.style.zIndex));
  expect(screen.getByRole("button", { name: "知道了" })).toHaveFocus();
  await client.click(screen.getByRole("button", { name: "知道了" }));
  expect(confirmation).toHaveAttribute("aria-modal", "true");
  expect(confirmation).not.toHaveAttribute("inert");
  expect(checkbox).toHaveFocus();
  expect(checkbox).toBeChecked();
  await client.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(opener).toHaveFocus();
});

test("取消确认只关闭呈现，仍保留全局写请求的 single-flight 与成功反馈", async () => {
  const pending = deferred<Response>();
  const fetchMock = mockApi({ patch: () => pending.promise });
  const client = userEvent.setup();
  render(<Harness initialMode="suggest" />);
  await client.click(screen.getByRole("button", { name: "关闭" }));
  await client.click(screen.getByRole("button", { name: "确认关闭" }));
  await client.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "正在设置…" })).toBeDisabled();
  expect(patchCalls(fetchMock)).toHaveLength(1);
  await act(async () => pending.resolve(json({ user: user("actor-a", "manual"), deleted_suggestion_count: 0 })));
  expect(screen.getByRole("status")).toHaveTextContent("已关闭记忆建议");
});

test("面板卸载清理确认 lease，不释放仍在途的全局模式请求", async () => {
  const pending = deferred<Response>();
  const fetchMock = mockApi({ patch: () => pending.promise });
  const client = userEvent.setup();
  const mounted = render(<Harness initialMode="suggest" />);
  await client.click(screen.getByRole("button", { name: "关闭" }));
  await client.click(screen.getByRole("button", { name: "确认关闭" }));
  mounted.rerender(<Harness initialMode="suggest" show={false} />);
  mounted.rerender(<Harness initialMode="suggest" show />);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "正在设置…" })).toBeDisabled();
  expect(patchCalls(fetchMock)).toHaveLength(1);
  await act(async () => pending.resolve(json({ user: user("actor-a", "manual"), deleted_suggestion_count: 0 })));
  expect(screen.getByRole("status")).toHaveTextContent("已关闭记忆建议");
});
