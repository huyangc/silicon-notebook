import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { NotebookScopePicker } from "../../app/ask/notebook-scope-picker.tsx";
import { GlobalConversationList } from "../../app/ask/conversation-list.tsx";
import { useGlobalAsk } from "../../app/ask/use-global-ask.ts";
import GlobalAskPage from "../../app/ask/page.tsx";
import { GlobalAskLauncher } from "../../app/ask/global-ask-launcher.tsx";
import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator.ts";
import type { GlobalConversation, GlobalConversationDetail, GlobalJob, GlobalScope } from "../../app/global-ask-api.ts";
import type { NotebookSummary } from "../../app/workspace-model.ts";

const api = vi.hoisted(() => ({
  me: vi.fn(), notebooks: vi.fn(), list: vi.fn(), detail: vi.fn(), ask: vi.fn(),
  poll: vi.fn(), cancel: vi.fn(), rename: vi.fn(), remove: vi.fn(), citation: vi.fn(),
}));
vi.mock("../../app/auth.ts", () => ({ fetchMe: api.me }));
vi.mock("../../app/notebook-api.ts", () => ({ listNotebooks: api.notebooks }));
vi.mock("../../app/global-ask-api.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/global-ask-api.ts")>(),
  listGlobalConversations: api.list, getGlobalConversation: api.detail,
  askGlobal: api.ask, getGlobalJob: api.poll, cancelGlobalJob: api.cancel,
  renameGlobalConversation: api.rename, deleteGlobalConversation: api.remove,
  getGlobalCitation: api.citation,
}));

const notebooks: NotebookSummary[] = ["材料研究", "热管理"].map((name, index) => ({
  id: `nb-${index}`, name, purpose: index ? "散热与低温" : "电池资料", primary_domain: "", status: "ready", counts: { sources: 2 }, created_label: "今天",
}));
const conversation = (id = "conv-a"): GlobalConversation => ({
  id, title: `对话 ${id}`, created_at: "2026-09-19T01:00:00Z", updated_at: "2026-09-19T01:00:00Z",
  notebook_scope: { mode: "all" }, submitted_via: "web",
});
const job = (status: GlobalJob["status"] = "running", id = "conv-a"): GlobalJob => ({
  job_id: `job-${id}`, conversation_id: id, status, question: "共同问题是什么？", created_at: "2026-09-19T01:00:00Z",
  notebook_scope: { mode: "all" }, resolved_notebook_ids: ["nb-0", "nb-1"], searched_notebook_ids: [], cited_notebook_ids: [], error: null, response: null,
});
const detail = (id = "conv-a", turns: GlobalJob[] = []): GlobalConversationDetail => ({ ...conversation(id), turns, has_more: false, next_offset: null });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (value: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, "", "/ask");
  api.me.mockResolvedValue({ id: "user-1" });
  api.notebooks.mockResolvedValue(notebooks);
  api.list.mockResolvedValue([]);
  api.detail.mockImplementation((id: string) => Promise.resolve(detail(id)));
  api.ask.mockResolvedValue(job());
});
afterEach(() => vi.useRealTimers());

function installDialogMethods() {
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  // jsdom has no native dialog implementation; browser QA verifies modal focus containment.
  for (const method of ["show", "showModal"] as const) {
    Object.defineProperty(HTMLDialogElement.prototype, method, { configurable: true, value() { this.setAttribute("open", ""); } });
  }
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value() { this.removeAttribute("open"); } });
}

function Launcher({ actorId = "user-1" }: { actorId?: string }) {
  const presentation = useRootModalCoordinator({ actorId, sourceId: null, onClosed() {} });
  return <>
    <button onClick={() => presentation.open("info", presentation.captureActorOwner())}>显示提示</button>
    <button onClick={() => presentation.requestClose("info", "button")}>关闭提示</button>
    <button onClick={() => presentation.open("model-service", presentation.captureActorOwner())}>显示模型设置</button>
    <GlobalAskLauncher key={actorId} presentation={presentation} />
  </>;
}

test("root overlays arbitrate chat visibility and an actor change clears its local state", async () => {
  installDialogMethods();
  const view = render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), { target: { value: "用户一的草稿" } });
  fireEvent.click(screen.getByRole("button", { name: "全屏展开" }));
  fireEvent.click(screen.getByRole("button", { name: "显示提示" }));
  expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull();
  expect(document.body.style.overflow).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "关闭提示" }));
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("用户一的草稿");
  expect(document.body.style.overflow).toBe("hidden");
  fireEvent.click(screen.getByRole("button", { name: "显示模型设置" }));
  expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("用户一的草稿");
  view.rerender(<Launcher actorId="user-2" />);
  expect(screen.queryByRole("textbox", { name: "输入问题" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("");
});

test("bubble loads on demand and preserves draft and scope through minimize and fullscreen", async () => {
  installDialogMethods();
  window.history.replaceState(null, "", "/?conversation_id=host-state#notebook=nb-0");
  render(<Launcher />);
  expect(api.me).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  expect(api.detail).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), { target: { value: "保留这段草稿" } });
  fireEvent.click(screen.getByRole("button", { name: "全部笔记本 · 2 个" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /材料研究/ }));
  fireEvent.click(screen.getByRole("button", { name: "完成" }));
  fireEvent.click(screen.getByRole("button", { name: "全屏展开" }));
  expect(document.body.style.overflow).toBe("hidden");
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("保留这段草稿");
  expect(screen.getByRole("button", { name: "已选择 1 个笔记本" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "退出全屏" }));
  expect(document.body.style.overflow).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "收起全局问答" }));
  expect(screen.queryByRole("textbox", { name: "输入问题" })).toBeNull();
  expect(screen.getByRole("button", { name: "打开全局问答" })).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("保留这段草稿");
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  expect(api.ask.mock.calls[0][0].notebook_scope).toEqual({ mode: "include", notebook_ids: ["nb-0"] });
  expect(window.location.search + window.location.hash).toBe("?conversation_id=host-state#notebook=nb-0");
  expect(api.me).toHaveBeenCalledTimes(1);
});

test("a minimized chat keeps polling without cancellation and Escape restores the bubble", async () => {
  installDialogMethods();
  const finished = { ...job("cancelled"), question: "后台问题" };
  api.poll.mockResolvedValue(finished);
  render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), { target: { value: "后台问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "停止" })).toBeTruthy());
  fireEvent.keyDown(screen.getByRole("dialog", { name: "全局问答" }), { key: "Escape" });
  expect(api.cancel).not.toHaveBeenCalled();
  await waitFor(() => expect(api.poll).toHaveBeenCalledTimes(1), { timeout: 2500 });
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  expect(screen.getByText("已停止回答，可以修改问题后继续。")).toBeTruthy();
});

test("scope starts at all, searching preserves selection, clearing restores all", () => {
  function Picker() {
    const [scope, setScope] = useState<GlobalScope>({ mode: "all" });
    return <NotebookScopePicker notebooks={notebooks} scope={scope} onChange={setScope} disabled={false} />;
  }
  render(<Picker />);
  fireEvent.click(screen.getByRole("button", { name: "全部笔记本 · 2 个" }));
  expect(screen.getAllByRole("checkbox").every((box) => !(box as HTMLInputElement).checked)).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: /材料研究/ }));
  expect(screen.getByRole("button", { name: "已选择 1 个笔记本" })).toBeTruthy();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "热管理" } });
  expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  fireEvent.click(screen.getByRole("checkbox", { name: /热管理/ }));
  expect(screen.getByRole("button", { name: "已选择 2 个笔记本" })).toBeTruthy();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /材料研究/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /热管理/ }));
  expect(screen.getByRole("button", { name: "全部笔记本 · 2 个" })).toBeTruthy();
});

test("unavailable selected ids stay explicit until user removes them", () => {
  const changed = vi.fn();
  render(<NotebookScopePicker notebooks={notebooks} scope={{ mode: "include", notebook_ids: ["revoked"] }} onChange={changed} disabled={false} />);
  fireEvent.click(screen.getByRole("button", { name: "已选择 1 个笔记本" }));
  expect(screen.getByText(/有 1 个已选笔记本不可访问/)).toBeTruthy();
  expect(changed).not.toHaveBeenCalled();
});

test("default all submits once immediately and reuses request id after an ambiguous failure", async () => {
  const pending = deferred<GlobalJob>();
  api.ask.mockReturnValueOnce(pending.promise);
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("共同问题是什么？"));
  let submitted!: Promise<void>;
  act(() => { submitted = result.current.submit(); void result.current.submit(); });
  expect(api.ask).toHaveBeenCalledTimes(1);
  expect(result.current.submitting).toBe(true);
  expect(api.ask.mock.calls[0][0].notebook_scope).toEqual({ mode: "all" });
  const requestId = api.ask.mock.calls[0][0].client_request_id;
  await act(async () => { pending.reject(new Error("network")); await submitted; });
  expect(result.current.draft).toBe("共同问题是什么？");
  await act(async () => { await result.current.submit(); });
  expect(api.ask.mock.calls[1][0].client_request_id).toBe(requestId);
  expect(result.current.turns).toHaveLength(1);
  expect(window.location.search).toContain("conversation_id=conv-a");
});

test("failed sidebar refresh does not turn an accepted question into a submission failure", async () => {
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  api.list.mockRejectedValueOnce(new Error("offline"));
  act(() => result.current.setDraft("共同问题是什么？"));
  await act(async () => { await result.current.submit(); });
  expect(result.current.error).toBe("");
  expect(result.current.historyError).toContain("问题已提交");
  expect(result.current.turns[0].status).toBe("running");
});

test("explicit new conversation gets a fresh request id after an ambiguous failure", async () => {
  api.ask.mockRejectedValueOnce(new Error("network"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("重复的问题"));
  await act(async () => { await result.current.submit(); });
  const previousId = api.ask.mock.calls[0][0].client_request_id;
  act(() => result.current.newConversation());
  act(() => result.current.setDraft("重复的问题"));
  await act(async () => { await result.current.submit(); });
  expect(api.ask.mock.calls[1][0].client_request_id).not.toBe(previousId);
  expect(api.ask.mock.calls[1][0].conversation_id).toBeUndefined();
});

test("late conversation detail cannot replace a later selected conversation", async () => {
  const pending = deferred<GlobalConversationDetail>();
  api.detail.mockImplementation((id: string) => id === "conv-a" ? pending.promise : Promise.resolve(detail(id, [job("done", id)])));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  let first!: Promise<void>;
  act(() => { first = result.current.openConversation("conv-a"); });
  await act(async () => { await result.current.openConversation("conv-b"); });
  await act(async () => { pending.resolve(detail("conv-a")); await first; });
  expect(result.current.conversationId).toBe("conv-b");
  expect(result.current.turns[0].conversation_id).toBe("conv-b");
});

test("denied history blocks submission, including an MCP conversation deep link", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=revoked");
  api.detail.mockRejectedValue(new Error("denied"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.openFailed).toBe(true);
  act(() => result.current.setDraft("不要自动扩大范围"));
  await act(async () => { await result.current.submit(); });
  expect(api.ask).not.toHaveBeenCalled();
  act(() => result.current.newConversation());
  expect(result.current.openFailed).toBe(false);
});

test("late running poll cannot resurrect an explicitly cancelled turn", async () => {
  const pending = deferred<GlobalJob>();
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue(detail("conv-a", [job()]));
  api.poll.mockReturnValue(pending.promise);
  api.cancel.mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  vi.useFakeTimers();
  // Restart the polling effect with the controlled clock.
  act(() => result.current.retryPoll());
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  await act(async () => { await result.current.stop(); pending.resolve(job()); });
  expect(result.current.turns[0].status).toBe("cancelled");
  expect(result.current.running).toBeUndefined();
});

test("history and conversation turns beyond the first page remain reachable", async () => {
  const first = Array.from({ length: 50 }, (_, index) => conversation(`conv-${index}`));
  api.list.mockImplementation((offset = 0) => Promise.resolve(offset === 0 ? first : [conversation("older")]));
  api.detail.mockImplementation((id: string, offset = 0) => Promise.resolve(offset === 0
    ? { ...detail(id, [job("done", "latest")]), has_more: true, next_offset: 50 }
    : detail(id, [job("done", "oldest")])));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.moreHistory).toBe(true);
  await act(async () => { await result.current.loadMoreHistory(); });
  expect(api.list).toHaveBeenLastCalledWith(50);
  expect(result.current.conversations).toHaveLength(51);
  await act(async () => { await result.current.openConversation("conv-0"); });
  await act(async () => { await result.current.loadMoreTurns(); });
  expect(api.detail).toHaveBeenLastCalledWith("conv-0", 50);
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-oldest", "job-latest"]);
});

test("rename shows local success and delete requires explicit confirmation", async () => {
  const updated = vi.fn();
  const deleted = vi.fn();
  api.rename.mockResolvedValue({ ...conversation(), title: "比较记录" });
  api.remove.mockResolvedValue(undefined);
  render(<GlobalConversationList items={[conversation()]} activeId="conv-a" disabled={false} more={false} loadingMore={false} error="" onLoadMore={vi.fn()} onOpen={vi.fn()} onNew={vi.fn()} onUpdated={updated} onDeleted={deleted} />);
  fireEvent.click(screen.getByRole("button", { name: "重命名 对话 conv-a" }));
  fireEvent.change(screen.getByRole("textbox", { name: "对话名称" }), { target: { value: "比较记录" } });
  fireEvent.click(screen.getByRole("button", { name: "保存名称" }));
  await waitFor(() => expect(updated).toHaveBeenCalled());
  expect(screen.getByText("名称已更新")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "删除 对话 conv-a" }));
  expect(api.remove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(deleted).toHaveBeenCalledWith("conv-a"));
});

test("page renders notebook-aware references, coverage notice and authorized original content", async () => {
  const complete: GlobalJob = {
    ...job("done"), searched_notebook_ids: ["nb-0", "nb-1"], cited_notebook_ids: ["nb-0"],
    response: {
      answer_id: "answer-1", question: "共同问题是什么？", answer: "温度影响性能 [k1]。", grounded: true,
      anchors: [{ key: "k1", object_id: "", object_type: "element", label: "测试记录", notebook_id: "nb-0", source_id: "source-1", element_id: "element-1", source_title: "测试记录" }],
      citations: [], created_at: "2026-09-19T01:00:00Z", notebook_scope: { mode: "all" },
      resolved_notebook_ids: ["nb-0", "nb-1"], searched_notebook_ids: ["nb-0", "nb-1"], cited_notebook_ids: ["nb-0"],
      completeness_notice: "回答仅使用本次命中的有限原文，不代表逐篇穷尽检查。",
    },
  };
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [complete]));
  const pending = deferred<{ id: string; source_id: string; element_type: string; location_label: string; text: string; metadata: object }>();
  api.citation.mockReturnValue(pending.promise);
  render(<GlobalAskPage />);
  await screen.findByText("温度影响性能", { exact: false });
  expect(screen.getByText(complete.response!.completeness_notice)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /材料研究.*测试记录/ }));
  expect(api.citation).toHaveBeenCalledWith("job-conv-a", "element-1");
  expect(screen.getByText("正在读取原文…")).toBeTruthy();
  await act(async () => { pending.resolve({ id: "element-1", source_id: "source-1", element_type: "paragraph", location_label: "第 2 页", text: "低温环境下，容量下降。", metadata: {} }); });
  expect(screen.getByText("低温环境下，容量下降。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭引用原文" }));
  expect(screen.queryByText("低温环境下，容量下降。")).toBeNull();
});

test("page hides raw transport failures and preserves explicitly safe job guidance", async () => {
  const diagnostic = "secret-upstream-payload /private/model-config";
  api.ask.mockRejectedValueOnce(new Error(diagnostic));
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).not.toBeDisabled());
  fireEvent.change(input, { target: { value: "请比较材料" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByText(/提交失败，请重试/);
  expect(screen.queryByText(diagnostic)).toBeNull();
  const safeMessage = "引用资料在回答期间发生了变化，请重新提问。";
  api.ask.mockResolvedValueOnce({ ...job("failed"), error: safeMessage });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByText(safeMessage);
  expect(screen.queryByText(diagnostic)).toBeNull();
});

test("pending clipboard work belongs to its conversation and cannot unlock a newer copy", async () => {
  const first = deferred<void>();
  const second = deferred<void>();
  const writeText = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const previousClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  try {
    api.list.mockResolvedValue([conversation("conv-a"), conversation("conv-b")]);
    api.detail.mockImplementation((id: string) => Promise.resolve(detail(id, [{
      ...job("done", id),
      response: {
        answer_id: `answer-${id}`, question: "共同问题是什么？", answer: `回答 ${id}`, grounded: true,
        anchors: [], citations: [], created_at: "2026-09-19T01:00:00Z", notebook_scope: { mode: "all" },
        resolved_notebook_ids: ["nb-0"], searched_notebook_ids: ["nb-0"], cited_notebook_ids: [],
        completeness_notice: "仅使用命中的原文。",
      },
    }])));
    window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
    render(<GlobalAskPage />);
    const copyA = await screen.findByRole("button", { name: "复制回答" });
    fireEvent.click(copyA);
    expect(copyA).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "对话 conv-b" }));
    await screen.findByText("回答 conv-b");
    const copyB = screen.getByRole("button", { name: "复制回答" });
    expect(copyB).not.toBeDisabled();
    fireEvent.click(copyB);
    expect(writeText).toHaveBeenNthCalledWith(2, "回答 conv-b");
    await act(async () => { first.resolve(); });
    expect(copyB).toBeDisabled();
    expect(screen.queryByText("已复制")).toBeNull();
    await act(async () => { second.resolve(); });
    expect(copyB).not.toBeDisabled();
    expect(screen.getByText("已复制")).toBeTruthy();
  } finally {
    if (previousClipboard) Object.defineProperty(navigator, "clipboard", previousClipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});
