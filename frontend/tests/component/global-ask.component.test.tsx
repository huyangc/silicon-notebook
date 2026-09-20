import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { NotebookScopePicker } from "../../app/ask/notebook-scope-picker.tsx";
import { GlobalConversationList } from "../../app/ask/conversation-list.tsx";
import { useGlobalAsk } from "../../app/ask/use-global-ask.ts";
import GlobalAskPage from "../../app/ask/page.tsx";
import GlobalAskWorkspace from "../../app/ask/global-ask-workspace.tsx";
import { GlobalAskLauncher } from "../../app/ask/global-ask-launcher.tsx";
import { AnswerView } from "../../app/answer-panel.tsx";
import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator.ts";
import { STOPPED_TURN_KEPT_TEXT, STOPPED_TURN_TEXT } from "../../app/stopped-turn.tsx";
import { humanizedError } from "../../app/errors.ts";

/** 服务端已经没有这条会话了（对账读到的 404）。 */
const conversationGone = () => humanizedError("对话不存在，请刷新列表。", 404);
import type { GlobalConversation, GlobalConversationDetail, GlobalJob, GlobalScope } from "../../app/global-ask-api.ts";
import type { QueryIntentContract } from "../../app/ask-intent-model.ts";
import type { AskResponse, NotebookSummary } from "../../app/workspace-model.ts";

const api = vi.hoisted(() => ({
  me: vi.fn(), notebooks: vi.fn(), list: vi.fn(), detail: vi.fn(), ask: vi.fn(),
  poll: vi.fn(), cancel: vi.fn(), rename: vi.fn(), remove: vi.fn(), intent: vi.fn(),
  feedback: vi.fn(), assetBlob: vi.fn(),
}));
vi.mock("../../app/auth.ts", () => ({ fetchMe: api.me }));
vi.mock("../../app/notebook-api.ts", () => ({ listNotebooks: api.notebooks }));
vi.mock("../../app/global-ask-api.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/global-ask-api.ts")>(),
  listGlobalConversations: api.list, getGlobalConversation: api.detail,
  askGlobal: api.ask, getGlobalJob: api.poll, cancelGlobalJob: api.cancel,
  renameGlobalConversation: api.rename, deleteGlobalConversation: api.remove,
  previewGlobalAskIntent: api.intent, submitGlobalFeedback: api.feedback,
}));
// 附图走鉴权 fetch→blob→objectURL（`AuthedImage`）。这里只替掉那一次网络读取，
// 断言看的是它**拿着哪个笔记本的 URL** 去读的。
vi.mock("../../app/source-api.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/source-api.ts")>(),
  fetchInternalAssetBlob: api.assetBlob,
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
  notebook_scope: { mode: "all" }, resolved_notebook_ids: ["nb-0", "nb-1"], searched_notebook_ids: [], cited_notebook_ids: [], skipped_notebooks: [], error: null, mode: "chunk", response: null,
});
/** 单库引擎直出的标准回答（新作业恒走这条）。 */
const standardAnswer = (overrides: Partial<AskResponse> = {}): AskResponse => ({
  answer_id: "answer-standard", conversation_id: "conv-a",
  conclusion: "跨库结论。", answer: "跨库结论 [k1]。", grounded: true,
  anchors: [{
    key: "k1", object_id: "", object_type: "element", label: "测试记录",
    name: "测试记录", source_title: "测试记录", location_label: "第 2 页",
    source_id: "source-1", element_id: "element-1", notebook_id: "nb-0",
    tier: "personal", snippet: "低温环境下，容量下降。",
  }],
  related_knowledge: [], citations: [], llm_mode: "chunk",
  completeness_notice: "回答仅使用本次命中的有限原文。",
  ...overrides,
});
const clearContract = (question: string) => ({
  objective: question, resolved_question: question, intent_type: "explain",
  result_scope: "ranked" as const, completeness_required: false, entities: [],
  mandatory_topics: [], comparison_axes: [], constraints: [], excluded_topics: [],
  expected_output: "", assumptions: [], ambiguities: [], confidence: 0.9,
  needs_clarification: false, confirmed: false,
});
const clarifyingContract = (question: string) => ({
  ...clearContract(question),
  resolved_question: `${question}（按最近一年）`,
  ambiguities: [{ id: "a1", question: "要比较哪几个体系？", required: true }],
  confidence: 0.4, needs_clarification: true,
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
  // 高级界面：本文件绝大多数用例都要操作引擎控件；简化界面另有专门的用例。
  api.me.mockResolvedValue({ id: "user-1", ui_mode: "advanced" });
  api.notebooks.mockResolvedValue(notebooks);
  api.list.mockResolvedValue([]);
  api.detail.mockImplementation((id: string) => Promise.resolve(detail(id)));
  api.ask.mockResolvedValue(job());
  api.intent.mockImplementation((question: string) => Promise.resolve(clearContract(question)));
  api.assetBlob.mockResolvedValue(new Blob(["fake-image-bytes"], { type: "image/png" }));
  if (typeof URL.createObjectURL !== "function") URL.createObjectURL = vi.fn(() => "blob:mock-url");
  if (typeof URL.revokeObjectURL !== "function") URL.revokeObjectURL = vi.fn();
  // 引擎选择是 per-viewer 的浏览器存储记忆；用例之间不许互相串味。存储本身不可用
  // 的环境（隐私窗口、被清的站点数据）也必须能跑——那正是 hook 里 try/catch 的契约。
  try { window.localStorage.clear(); } catch { /* 存储不可用即无记忆，照常。 */ }
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
  vi.useFakeTimers();
  fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), { target: { value: "后台问题" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "发送问题" })); });
  // 停止键是图标键：名字在 aria-label 上，按钮面上没有字；外观取全站共用的那个类，
  // 并且与发送键同住工具条的右栏（不与范围、引擎挤在同一条换行里）。
  const stop = screen.getByRole("button", { name: "停止" });
  expect(stop.textContent).toBe("");
  expect(stop).toHaveClass("stop-control");
  expect(stop.querySelector(".stop-glyph")).toBeTruthy();
  expect(stop.parentElement).toHaveClass("global-composer-toolbar");
  expect(stop.closest(".global-composer-controls")).toBeNull();
  fireEvent.keyDown(screen.getByRole("dialog", { name: "全局问答" }), { key: "Escape" });
  expect(api.cancel).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1199); });
  expect(api.poll).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  expect(screen.getByText(STOPPED_TURN_TEXT)).toBeTruthy();
});

test("unchanged progress backs polling off, while new progress restores the short interval", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue(detail("conv-a", [job()]));
  api.poll.mockResolvedValue(job());
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  vi.useFakeTimers();
  act(() => result.current.retryPoll());
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(2399); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.poll).toHaveBeenCalledTimes(2);
  api.poll.mockResolvedValue({ ...job(), searched_notebook_ids: ["nb-0"] });
  await act(async () => { await vi.advanceTimersByTimeAsync(4799); });
  expect(api.poll).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(result.current.running?.searched_notebook_ids).toEqual(["nb-0"]);
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(api.poll).toHaveBeenCalledTimes(4);
  await act(async () => { await vi.advanceTimersByTimeAsync(2400 + 4800 + 9600); });
  expect(api.poll).toHaveBeenCalledTimes(7);
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(api.poll).toHaveBeenCalledTimes(8);
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(api.poll).toHaveBeenCalledTimes(9);
});

test("hidden documents stop polling and resume immediately without overlapping an in-flight read", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue(detail("conv-a", [job()]));
  const pending = deferred<GlobalJob>();
  api.poll.mockReturnValueOnce(pending.promise).mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  vi.useFakeTimers();
  act(() => result.current.retryPoll());
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  act(() => { visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange")); });
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  expect(result.current.running?.job_id).toBe("job-conv-a");
  act(() => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  await act(async () => { pending.resolve(job()); });
  expect(api.poll).toHaveBeenCalledTimes(2);
  expect(result.current.turns[0].status).toBe("cancelled");
  expect(api.cancel).not.toHaveBeenCalled();
});

test("initially hidden tasks wait for visibility and a transient failure retries with backoff", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue(detail("conv-a", [job()]));
  api.poll.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  vi.useFakeTimers();
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(api.poll).not.toHaveBeenCalled();
  await act(async () => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  expect(result.current.pollError).toContain("自动重试");
  await act(async () => { await vi.advanceTimersByTimeAsync(2399); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.poll).toHaveBeenCalledTimes(2);
  expect(result.current.pollError).toBe("");
  expect(result.current.turns[0].status).toBe("cancelled");
});

test("scope narrowing explains lost follow-up context beside the composer and restores cleanly", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue(detail("conv-a", [job("cancelled")]));
  render(<GlobalAskPage />);
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  expect(screen.queryByText(/先前涉及其他笔记本的提问不会用于本次追问/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "全部笔记本 · 2 个" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /材料研究/ }));
  expect(screen.getByText(/先前涉及其他笔记本的提问不会用于本次追问/).closest("form")).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox", { name: /热管理/ }));
  expect(screen.queryByText(/先前涉及其他笔记本的提问不会用于本次追问/)).toBeNull();
});

test("partial coverage identifies skipped notebooks and discloses lexical fallback on completed answers", async () => {
  const skipped = [{ notebook_id: "nb-1", reason: "检索暂时不可用，请稍后重试。" }];
  const complete: GlobalJob = {
    ...job("done"), searched_notebook_ids: ["nb-0"], cited_notebook_ids: ["nb-0"], skipped_notebooks: skipped,
    response: {
      answer_id: "answer-partial", question: "共同问题是什么？", answer: "成功检索的资料表明温度影响性能。", grounded: true,
      anchors: [], citations: [], created_at: "2026-09-19T01:00:00Z", notebook_scope: { mode: "all" },
      resolved_notebook_ids: ["nb-0", "nb-1"], searched_notebook_ids: ["nb-0"], cited_notebook_ids: ["nb-0"],
      skipped_notebooks: skipped, degraded_notebook_ids: ["nb-0"], completeness_notice: "回答仅使用本次命中的有限原文。",
    },
  };
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue(detail("conv-a", [complete]));
  render(<GlobalAskPage />);
  await screen.findByText(complete.response!.answer);
  const receipt = screen.getByLabelText("本轮检索回执");
  expect(receipt).toHaveTextContent("范围 2 个 · 已检索 1 个 · 引用来自 1 个笔记本");
  expect(receipt).toHaveTextContent("热管理");
  expect(receipt).toHaveTextContent(skipped[0].reason);
  expect(receipt).toHaveTextContent("部分笔记本未完成检索");
  expect(receipt).toHaveTextContent("1 个笔记本使用词法降级检索，跨语言召回可能不完整");
});

const manyNotebooks: NotebookSummary[] = Array.from({ length: 11 }, (_, index) => ({
  ...notebooks[0], id: `many-${index}`, name: `笔记本 ${index}`, counts: { sources: index },
}));

test("over the notebook cap, the unusable all-scope becomes a visible preselection of the 8 richest notebooks", async () => {
  api.notebooks.mockResolvedValue(manyNotebooks);
  api.ask.mockResolvedValue(job("running"));
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  // 来源最多的 8 个 = many-3 … many-10，按列表原序给出。
  await waitFor(() => expect(result.current.scope).toEqual({
    mode: "include", notebook_ids: manyNotebooks.slice(3).map((item) => item.id),
  }));
  act(() => result.current.setDraft("问题"));
  await act(async () => { await result.current.submit(); });
  expect(api.ask).toHaveBeenCalledWith(expect.objectContaining({
    notebook_scope: { mode: "include", notebook_ids: manyNotebooks.slice(3).map((item) => item.id) },
  }));
  // 新建对话回到「全部」时同样被换掉，不会留下一个提交必然 422 的范围。
  act(() => result.current.newConversation());
  await waitFor(() => expect(result.current.scope.mode).toBe("include"));
});

test("the picker holds the selection at the cap and never offers an all-scope it cannot submit", () => {
  function Picker() {
    const [scope, setScope] = useState<GlobalScope>({ mode: "include", notebook_ids: manyNotebooks.slice(3).map((item) => item.id) });
    return <NotebookScopePicker notebooks={manyNotebooks} scope={scope} onChange={setScope} disabled={false} />;
  }
  render(<Picker />);
  fireEvent.click(screen.getByRole("button", { name: "已选择 8 个笔记本" }));
  expect(screen.queryByRole("button", { name: /全部可访问的笔记本/ })).toBeNull();
  expect(screen.getByRole("note")).toHaveTextContent("一次最多检索 8 个笔记本");
  // 选满：未选的勾不动；取消一个之后才能再选。
  const spare = screen.getByRole("checkbox", { name: /笔记本 0/ }) as HTMLInputElement;
  expect(spare.disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: /笔记本 10/ }));
  expect(spare.disabled).toBe(false);
  fireEvent.click(spare);
  expect(screen.getByRole("button", { name: "已选择 8 个笔记本" })).toBeTruthy();
  // 清空选择不回落到「全部」（那会被立刻重新预选）。
  for (const box of screen.getAllByRole("checkbox")) if ((box as HTMLInputElement).checked) fireEvent.click(box);
  expect(screen.getByRole("button", { name: "未选择笔记本" })).toBeTruthy();
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

test("reopening refreshes notebook choices without resetting the draft or explicit scope", async () => {
  api.notebooks.mockResolvedValueOnce([]);
  const hook = renderHook(({ active }) => useGlobalAsk({ syncUrl: false, active }), { initialProps: { active: true } });
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  act(() => hook.result.current.setDraft("创建资料后继续提问"));
  hook.rerender({ active: false });
  hook.rerender({ active: true });
  await waitFor(() => expect(hook.result.current.notebooks).toEqual(notebooks));
  expect(hook.result.current.draft).toBe("创建资料后继续提问");
  const selected: GlobalScope = { mode: "include", notebook_ids: ["nb-0"] };
  act(() => hook.result.current.setScope(selected));
  hook.rerender({ active: false });
  hook.rerender({ active: true });
  await waitFor(() => expect(api.notebooks).toHaveBeenCalledTimes(3));
  expect(hook.result.current.scope).toEqual(selected);
  expect(api.list).toHaveBeenCalledTimes(1);
});

test("embedded reload keeps the current conversation, failed draft, selected scope and retry identity", async () => {
  api.ask.mockResolvedValueOnce(job("done")).mockRejectedValueOnce(new Error("network"));
  api.detail.mockResolvedValue(detail("conv-a", [job("done")]));
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("第一个问题"));
  await act(async () => { await result.current.submit(); });
  const selected: GlobalScope = { mode: "include", notebook_ids: ["nb-0"] };
  act(() => { result.current.setDraft("只在指定笔记本追问"); result.current.setScope(selected); });
  await act(async () => { await result.current.submit(); });
  const request = api.ask.mock.calls[1][0];
  await act(async () => { await result.current.load(); });
  expect(result.current.conversationId).toBe("conv-a");
  expect(result.current.scope).toEqual(selected);
  expect(result.current.draft).toBe("只在指定笔记本追问");
  expect(result.current.turns).toHaveLength(1);
  api.ask.mockResolvedValueOnce(job("done"));
  await act(async () => { await result.current.submit(); });
  expect(api.ask.mock.calls[2][0]).toEqual(request);
});

test("error recovery restores a running conversation after stop fails", async () => {
  // 停止失败而作业确实还在跑（对账读得到它）：这是真失败，不是「响应丢了的丢弃」。
  api.cancel.mockRejectedValueOnce(new Error("network"));
  api.detail.mockResolvedValue(detail("conv-a", [job()]));
  api.poll.mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  vi.useFakeTimers();
  act(() => result.current.setDraft("后台问题"));
  await act(async () => { await result.current.submit(); });
  await act(async () => { await result.current.stop(); });
  expect(result.current.error).toBeTruthy();
  await act(async () => { await result.current.load(); });
  expect(result.current.running?.job_id).toBe("job-conv-a");
  await act(async () => { await vi.advanceTimersByTimeAsync(1199); });
  expect(api.poll).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  expect(result.current.running).toBeUndefined();
  expect(result.current.conversationId).toBe("conv-a");
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
  // 已有检索回执上屏：停止后这一轮留在对话里（情形一），迟到的轮询不得把它复活。
  const progressed = { ...job(), searched_notebook_ids: ["nb-0"] };
  api.detail.mockResolvedValue(detail("conv-a", [progressed]));
  api.poll.mockReturnValue(pending.promise);
  api.cancel.mockResolvedValue({ ...progressed, status: "cancelled" });
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  vi.useFakeTimers();
  // Restart the polling effect with the controlled clock.
  act(() => result.current.retryPoll());
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(api.poll).toHaveBeenCalledTimes(1);
  await act(async () => { await result.current.stop(); pending.resolve(progressed); });
  expect(api.cancel).toHaveBeenCalledWith(progressed.job_id, false);
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

test.each(["resolve", "reject"] as const)("deleting the active conversation retires a pending submit and its late %s cannot unlock a new submit", async (outcome) => {
  const deletion = deferred<void>();
  const oldSubmission = deferred<GlobalJob>();
  const newSubmission = deferred<GlobalJob>();
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.remove.mockReturnValue(deletion.promise);
  api.ask.mockReturnValueOnce(oldSubmission.promise).mockReturnValueOnce(newSubmission.promise);
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "删除 对话 conv-a" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  expect(api.remove).toHaveBeenCalledWith("conv-a");
  fireEvent.change(input, { target: { value: "即将删除的对话中的问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  expect(api.ask.mock.calls[0][0].conversation_id).toBe("conv-a");

  await act(async () => { deletion.resolve(); });
  expect(window.location.search).toBe("");
  expect(input).toBeEnabled();
  expect(input).toHaveValue("");
  expect(screen.queryByRole("button", { name: "对话 conv-a" })).toBeNull();
  fireEvent.change(input, { target: { value: "新对话中的问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  expect(api.ask.mock.calls[1][0].conversation_id).toBeUndefined();
  expect(api.ask.mock.calls[1][0].client_request_id).not.toBe(api.ask.mock.calls[0][0].client_request_id);

  await act(async () => {
    if (outcome === "resolve") oldSubmission.resolve({ ...job(), question: "已删除对话的迟到回答" });
    else oldSubmission.reject(new Error("deleted conversation"));
  });
  expect(window.location.search).toBe("");
  expect(screen.queryByText("已删除对话的迟到回答")).toBeNull();
  expect(input).toBeDisabled();
  // A stale finally must not release the synchronous guard of the new request.
  fireEvent.submit(input.closest("form")!);
  expect(api.ask).toHaveBeenCalledTimes(2);
  api.list.mockResolvedValue([conversation("conv-new")]);
  await act(async () => { newSubmission.resolve({ ...job("cancelled", "conv-new"), question: "新对话中的问题" }); });
  expect(window.location.search).toBe("?conversation_id=conv-new");
  expect(input).toBeEnabled();
  expect(screen.getByText("新对话中的问题")).toBeTruthy();
});

test.each(["other conversation", "failed deletion"] as const)("a pending submit survives %s without losing its owner", async (scenario) => {
  const deletion = deferred<void>();
  const submission = deferred<GlobalJob>();
  const deletedId = scenario === "other conversation" ? "conv-b" : "conv-a";
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation(), conversation("conv-b")]);
  api.remove.mockReturnValue(deletion.promise);
  api.ask.mockReturnValue(submission.promise);
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: `删除 对话 ${deletedId}` }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  fireEvent.change(input, { target: { value: "仍然有效的问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  expect(api.ask).toHaveBeenCalledTimes(1);
  await act(async () => {
    if (scenario === "other conversation") deletion.resolve();
    else deletion.reject(new Error("offline"));
  });
  expect(window.location.search).toBe("?conversation_id=conv-a");
  expect(input).toBeDisabled();
  // 问题在点击当下就上屏（pending 轮），输入框随之清空。
  expect(input).toHaveValue("");
  expect(screen.getByText("仍然有效的问题")).toBeTruthy();
  if (scenario === "failed deletion") expect(screen.getByText("操作失败，请重试")).toBeTruthy();
  else expect(screen.queryByRole("button", { name: "对话 conv-b" })).toBeNull();
  fireEvent.submit(input.closest("form")!);
  expect(api.ask).toHaveBeenCalledTimes(1);
  api.list.mockResolvedValue([conversation()]);
  await act(async () => { submission.resolve({ ...job("cancelled"), question: "仍然有效的问题" }); });
  expect(window.location.search).toBe("?conversation_id=conv-a");
  expect(input).toBeEnabled();
  expect(screen.getByText("仍然有效的问题")).toBeTruthy();
});

// 全局问答的引用呈现与笔记本内问答共用同一张小卡片（`CitationPopover` /
// `SelectedReferenceDetail`）：点行内标记，卡片就在标记旁弹出。答案下方那排引用
// 列表与右侧「引用原文」阅读栏已经撤掉，卡片直接用回答自带的 snippet/quoted_span，
// 不再为它多打一次全文读取。
const citedAnswer: GlobalJob = {
  ...job("done"), searched_notebook_ids: ["nb-0", "nb-1"], cited_notebook_ids: ["nb-0"],
  response: {
    answer_id: "answer-1", question: "共同问题是什么？", answer: "温度影响性能 [k1]。", grounded: true,
    anchors: [{
      key: "k1", object_id: "", object_type: "element", label: "测试记录",
      notebook_id: "nb-0", source_id: "source-1", element_id: "element-1",
      source_title: "测试记录", location_label: "第 2 页", tier: "personal",
      snippet: "低温环境下，容量下降。",
    }],
    citations: [], created_at: "2026-09-19T01:00:00Z", notebook_scope: { mode: "all" },
    resolved_notebook_ids: ["nb-0", "nb-1"], searched_notebook_ids: ["nb-0", "nb-1"], cited_notebook_ids: ["nb-0"],
    completeness_notice: "回答仅使用本次命中的有限原文，不代表逐篇穷尽检查。",
    skipped_notebooks: [],
  },
};

/** 新作业：单库引擎直出的标准回答，引用卡由 AnswerView 内部渲染。 */
const answeredJob: GlobalJob = {
  ...job("done"), cited_notebook_ids: ["nb-0"], answer: standardAnswer(),
};

test("page opens the shared citation card beside the inline marker, with source, notebook and an open-notebook link", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [citedAnswer]));
  const { container } = render(<GlobalAskPage />);
  await screen.findByText("温度影响性能", { exact: false });
  expect(screen.getByText(citedAnswer.response!.completeness_notice)).toBeTruthy();
  // 撤掉的两件东西不得以任何形式回来。
  expect(container.querySelector(".global-citation-list")).toBeNull();
  expect(container.querySelector(".global-evidence")).toBeNull();

  const marker = await screen.findByRole("button", { name: "[1]" });
  fireEvent.click(marker);
  const card = await screen.findByRole("dialog");
  expect(card).toHaveClass("cite-popover");
  expect(card).toHaveTextContent("测试记录");
  // 跨库徽章把 notebook_id 解成库名：这条引用来自哪个笔记本必须一眼可见。
  expect(card).toHaveTextContent("来自「材料研究」（个人知识库）");
  // 原文摘录直接来自回答自带的 anchor.snippet，没有第二次网络读取。
  expect(card).toHaveTextContent("低温环境下，容量下降。");
  // 选中态：卡片打开时对应标记高亮。
  expect(marker).toHaveAttribute("aria-expanded", "true");

  const openNotebook = await screen.findByRole("link", { name: "打开笔记本" });
  expect(openNotebook).toHaveAttribute("href", "/#notebook=nb-0&source=source-1");

  // 点卡片外部关闭（与笔记本内问答同一条路径：window 捕获期的 pointerdown）。
  await act(async () => { document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })); });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(marker).toHaveAttribute("aria-expanded", "false");
});

test("Escape closes only the citation card, never the floating chat window", async () => {
  installDialogMethods();
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [citedAnswer]));
  const { container } = render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "对话 conv-a" }));
  await screen.findByText("温度影响性能", { exact: false });
  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await waitFor(() => {
    const node = container.querySelector(".cite-popover");
    expect(node).not.toBeNull();
    return node!;
  });

  // 卡片必须渲染在 <dialog> 子树内：全屏形态 showModal() 把 dialog 提到 top layer，
  // 渲染在它之外的 fixed 元素会被盖住且不可交互。
  expect(screen.getByRole("dialog", { name: "全局问答" }).contains(card)).toBe(true);
  // 从 body 派发而不是从卡片派发：卡片从不接管焦点，真实按键的 target 不在卡片里。
  // 把拦截改成卡片上的局部 onKeyDown 的实现，在这里必须红。
  fireEvent.keyDown(document.body, { key: "Escape" });
  await waitFor(() => expect(container.querySelector(".cite-popover")).toBeNull());
  // 浮窗还在，草稿输入框仍可用——一次 Esc 只收一层。
  expect(screen.getByRole("dialog", { name: "全局问答" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "输入问题" })).toBeTruthy();
  // 卡片关掉之后，同一个 Esc 才轮到浮窗自己。
  fireEvent.keyDown(screen.getByRole("dialog", { name: "全局问答" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull());
});

test("collapsing the window closes the card and releases the page's Escape", async () => {
  installDialogMethods();
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [citedAnswer]));
  const { container } = render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "对话 conv-a" }));
  await screen.findByText("温度影响性能", { exact: false });
  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));
  await waitFor(() => expect(container.querySelector(".cite-popover")).not.toBeNull());

  // 「打开笔记本」的点击落在卡片内部，不会触发 pointerdown-outside；浮窗收起后
  // 工作区仍然挂载。卡片与它的捕获期 Esc 拦截器必须一起收掉。
  const pageEscape = vi.fn();
  window.addEventListener("keydown", pageEscape);
  try {
    fireEvent.click(within(container.querySelector(".cite-popover") as HTMLElement).getByRole("link", { name: "打开笔记本" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull());
    await waitFor(() => expect(container.querySelector(".cite-popover")).toBeNull());
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(pageEscape).toHaveBeenCalledTimes(1);
  } finally {
    window.removeEventListener("keydown", pageEscape);
  }
});

test("a modified click on open-notebook goes to a new tab without collapsing the window", async () => {
  installDialogMethods();
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [citedAnswer]));
  const { container } = render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "对话 conv-a" }));
  await screen.findByText("温度影响性能", { exact: false });
  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await waitFor(() => {
    const node = container.querySelector(".cite-popover");
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
  fireEvent.click(within(card).getByRole("link", { name: "打开笔记本" }), { metaKey: true });
  expect(screen.getByRole("dialog", { name: "全局问答" })).toBeTruthy();
});

test("the in-notebook citation card has no open-notebook link (no handler, no button)", async () => {
  const answer: AskResponse = {
    answer_id: "answer-in-notebook",
    conversation_id: "conversation-1",
    conclusion: "温度影响性能 [k1]。",
    answer: "温度影响性能 [k1]。",
    grounded: true,
    anchors: [{
      key: "k1", object_id: "", object_type: "element", label: "测试记录",
      name: "测试记录", source_title: "测试记录", location_label: "第 2 页",
      source_id: "source-1", element_id: "element-1", notebook_id: "nb-0",
      tier: "personal", snippet: "低温环境下，容量下降。",
    }],
    related_knowledge: [], citations: [], llm_mode: "reasoning",
  };
  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onOpenSource={() => undefined}
      notebookId="nb-0"
      notebookNames={{ "nb-0": "材料研究" }}
      buildingScaleIndex={false}
      memorySaved={false}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(card).toHaveTextContent("测试记录");
  expect(within(card).queryByRole("link", { name: "打开笔记本" })).toBeNull();
  expect(card.textContent).not.toContain("打开笔记本");
  // 同一张卡上笔记本内独有的入口照常在。
  expect(within(card).getByRole("button", { name: "查看原文" })).toBeTruthy();
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

test.each([false, true, undefined])("answer grounding notice follows the reliability value even with citations: %s", async (grounded) => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.detail.mockResolvedValue({
    ...detail(),
    turns: [{
      ...job("done"), cited_notebook_ids: ["nb-0"],
      response: {
        answer_id: "answer-grounding", question: "共同问题是什么？", answer: "部分结论仍需要核实 [k1]。", grounded,
        anchors: [{ key: "k1", object_id: "chunk-1", object_type: "chunk", label: "测试记录", notebook_id: "nb-0", source_id: "source-1", element_id: "element-1", source_title: "测试记录" }],
        citations: [{ label: "测试记录", notebook_id: "nb-0", source_id: "source-1", element_id: "element-1", location_label: "第 2 页", quoted_span: "已有部分依据" }],
        created_at: "2026-09-19T01:00:00Z", notebook_scope: { mode: "all" },
        resolved_notebook_ids: ["nb-0", "nb-1"], searched_notebook_ids: ["nb-0", "nb-1"], cited_notebook_ids: ["nb-0"],
        skipped_notebooks: [], completeness_notice: "回答仅使用本次命中的有限原文。",
      },
    }],
  });
  render(<GlobalAskPage />);
  await screen.findByText("部分结论仍需要核实", { exact: false });
  // 有引用可点（行内标记）与「有没有据」是两件事：引用在，提醒仍按可靠性取值走。
  expect(await screen.findByRole("button", { name: "[1]" })).toBeTruthy();
  const warning = screen.queryByText("以下回答未得到原文充分支持，请结合引用核对。");
  if (grounded === true) expect(warning).toBeNull();
  else {
    expect(warning).toHaveAttribute("role", "note");
    expect(warning?.closest(".chat-assistant")).toBeTruthy();
  }
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
        skipped_notebooks: [],
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

// --- 标准 AnswerView + 引擎选择器 --------------------------------------------

test("renders AnswerView for new jobs", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [{
    ...job("done"), cited_notebook_ids: ["nb-0"], answer: standardAnswer(),
  }]));
  const { container } = render(<GlobalAskPage />);
  await screen.findByText("跨库结论", { exact: false });
  // AnswerView 的根节点。历史轮次那套自绘渲染没有它。
  await waitFor(() => expect(container.querySelector(".chat-answer")).not.toBeNull());
  expect(container.querySelector(".global-answer-footer")).toBeNull();
  expect(screen.getByText("回答仅使用本次命中的有限原文。")).toBeTruthy();
  // 单库动作没有承接方时那几颗按钮不渲染（保存记忆）；反馈（ask.sendFeedback）与
  // 分享（全局会话自己的一组分享端点）现在都有承接方，所以两者的按钮都在。
  // 分享那一条的完整行为在 global-ask-share.component.test.tsx。
  expect(screen.queryByRole("button", { name: "保存到记忆" })).toBeNull();
  expect(screen.getByRole("button", { name: "分享到这条回答" })).toBeTruthy();
  const useful = screen.getByRole("button", { name: "有用" });
  const notUseful = screen.getByRole("button", { name: "需改进" });
  expect(useful).not.toBeDisabled();
  expect(notUseful).not.toBeDisabled();
  expect(useful.className).not.toContain("selected");
  expect(notUseful.className).not.toContain("selected");
  // 复制回答是自足动作，AnswerView 自己就带。
  expect(await screen.findByRole("button", { name: "复制回答" })).toBeTruthy();
});

test("feedback buttons submit, highlight and disable immediately, and roll back on failure", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  const turn = { ...job("done"), cited_notebook_ids: ["nb-0"], answer: standardAnswer() };
  api.detail.mockResolvedValue(detail("conv-a", [turn]));
  const failure = deferred<never>();
  api.feedback.mockReturnValueOnce(failure.promise);
  render(<GlobalAskPage />);
  const useful = await screen.findByRole("button", { name: "有用" });
  fireEvent.click(useful);
  // 乐观：不等网络响应就立刻高亮并禁用（Interactive feedback：按下必须有可见变化）。
  expect(useful).toBeDisabled();
  expect(useful.className).toContain("selected");
  expect(api.feedback).toHaveBeenCalledWith(turn.job_id, "useful");
  await act(async () => { failure.reject(new Error("network down")); });
  // 失败回滚：按钮恢复可点、未选中，并给出中文提示（既有的 error 呈现方式）。
  await waitFor(() => expect(screen.getByRole("button", { name: "有用" })).not.toBeDisabled());
  expect(screen.getByRole("button", { name: "有用" }).className).not.toContain("selected");
  expect(await screen.findByText("反馈提交失败，请重试")).toBeTruthy();

  // 重试成功：服务端回执把 feedback 落定，按钮保持高亮禁用。
  api.feedback.mockResolvedValueOnce({ ...turn, feedback: "useful" });
  fireEvent.click(screen.getByRole("button", { name: "有用" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "有用" })).toBeDisabled());
  expect(screen.getByRole("button", { name: "有用" }).className).toContain("selected");
  expect(api.feedback).toHaveBeenCalledTimes(2);
});

test("a job that already carries feedback reopens with the matching button selected and disabled", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [{
    ...job("done"), cited_notebook_ids: ["nb-0"], answer: standardAnswer(), feedback: "not_useful",
  }]));
  render(<GlobalAskPage />);
  const notUseful = await screen.findByRole("button", { name: "需改进" });
  const useful = screen.getByRole("button", { name: "有用" });
  expect(notUseful).toBeDisabled();
  expect(notUseful.className).toContain("selected");
  expect(useful).toBeDisabled();
  expect(useful.className).not.toContain("selected");
});

test("renders legacy markdown for historical jobs without feedback buttons", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [citedAnswer]));
  render(<GlobalAskPage />);
  await screen.findByText("温度影响性能", { exact: false });
  // 历史形状（只有 `response`）走 AnswerMarkdown 自绘，不是 AnswerView：没有反馈按钮。
  expect(screen.queryByRole("button", { name: "有用" })).toBeNull();
  expect(screen.queryByRole("button", { name: "需改进" })).toBeNull();
});

test("renders legacy markdown for historical jobs", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [citedAnswer]));
  const { container } = render(<GlobalAskPage />);
  await screen.findByText("温度影响性能", { exact: false });
  // 历史轮次只有 `response`：保留今天这段渲染，删了它历史对话就是空白。
  await waitFor(() => expect(container.querySelector(".global-answer-footer")).not.toBeNull());
  expect(container.querySelector(".chat-answer")).toBeNull();
  expect(await screen.findByRole("button", { name: "[1]" })).toBeTruthy();
});

test("mode picker submits the selected engine", async () => {
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  // 扩展功能组整组不出现：后端对全局问答一律 422。
  expect(screen.queryByRole("button", { name: "扩展功能" })).toBeNull();
  expect(screen.getByRole("button", { name: "通用问答" })).toHaveClass("active");
  fireEvent.click(screen.getByRole("button", { name: "深入分析" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "深入分析" })).toHaveClass("active"));
  fireEvent.change(input, { target: { value: "请逐步比较两个体系" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  // 理解清楚就不打扰用户，直接把系统的理解当确认提交。
  expect(api.intent).toHaveBeenCalledTimes(1);
  expect(api.ask.mock.calls[0][0].mode).toBe("reasoning");
  expect(api.ask.mock.calls[0][0].intent.resolved_question).toBe("请逐步比较两个体系");
  expect(api.ask.mock.calls[0][0].retrieval_effort).toBe("standard");
});

test("the simplified interface offers no engine picker and submits the fixed engine after understanding", async () => {
  // 与笔记本内问答同一条规则（`submissionAskMode`）：简化界面没有引擎控件，提交固定走
  // SIMPLIFIED_ASK_MODE（reasoning），所以照样先过问题理解；`mode` 只是一份不可见的记忆。
  api.me.mockResolvedValue({ id: "user-1", ui_mode: "auto" });
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  expect(screen.queryByRole("button", { name: "深入分析" })).toBeNull();
  expect(screen.queryByRole("button", { name: "通用问答" })).toBeNull();
  fireEvent.change(input, { target: { value: "请逐步比较两个体系" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  expect(api.intent).toHaveBeenCalledTimes(1);
  expect(api.ask.mock.calls[0][0].mode).toBe("reasoning");
  expect(api.ask.mock.calls[0][0].intent.resolved_question).toBe("请逐步比较两个体系");
});

test("an embedded window follows the host's live interface mode, not the profile it loaded", async () => {
  // 笔记本页里切换自动/高级模式不会让浮窗重新载入档案，所以宿主把实时的界面模式传下来。
  api.me.mockResolvedValue({ id: "user-1", ui_mode: "advanced" });
  const view = render(<GlobalAskWorkspace embedded uiMode="auto" />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  expect(screen.queryByRole("button", { name: "深入分析" })).toBeNull();
  view.rerender(<GlobalAskWorkspace embedded uiMode="advanced" />);
  expect(await screen.findByRole("button", { name: "深入分析" })).toBeTruthy();
});

test("reasoning submission goes through intent review", async () => {
  api.intent.mockImplementation((question: string) => Promise.resolve(clarifyingContract(question)));
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "深入分析" }));
  fireEvent.change(input, { target: { value: "哪个更耐低温" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  // 审阅卡出来之前不得提交。
  const card = await screen.findByRole("region", { name: "确认逐步推理的问题理解" });
  expect(api.ask).not.toHaveBeenCalled();
  expect(within(card).getByRole("button", { name: "确认并开始检索" })).toBeDisabled();
  // 不跨 await 攥节点：审阅卡挂载后有一个按 contract 清空回答的 effect，慢 runner 上
  // 它会落在输入之后把回答冲掉，卡片重挂时先前取到的按钮还会变成永远 disabled 的
  // 过期节点（#758 同文件另一条用例红过，这条在 #761 的 CI 上红了）。每一轮重新查询、
  // 回答被冲掉就再填。
  let confirm!: HTMLElement;
  await waitFor(() => {
    const current = screen.getByRole("region", { name: "确认逐步推理的问题理解" });
    const box = within(current).getByRole("textbox", { name: "要比较哪几个体系？的补充答案" }) as HTMLTextAreaElement;
    if (box.value !== "磷酸铁锂与三元") fireEvent.change(box, { target: { value: "磷酸铁锂与三元" } });
    confirm = within(current).getByRole("button", { name: "确认并开始检索" });
    expect(confirm).toBeEnabled();
  }, { timeout: 5000 });
  fireEvent.click(confirm);
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  const submitted = api.ask.mock.calls[0][0];
  expect(submitted.mode).toBe("reasoning");
  expect(submitted.intent.resolved_question).toBe("哪个更耐低温（按最近一年）");
  expect(submitted.intent.answers).toEqual([{ id: "a1", answer: "磷酸铁锂与三元" }]);
});

test("a changed confirmation gets a new request id after an ambiguous failure; an unchanged one reuses it", async () => {
  // 后端认重试时刻意不比 intent（每次预检给出的理解都不同），所以「这次确认的内容
  // 被用户改了」只能由这里换 request id 来表达，否则会原样拿回上一次确认的作业。
  api.intent.mockImplementation((question: string) => Promise.resolve(clarifyingContract(question)));
  api.ask.mockRejectedValue(new Error("network"));
  render(<GlobalAskPage />);
  const input = await screen.findByRole("textbox", { name: "输入问题" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "深入分析" }));
  fireEvent.change(input, { target: { value: "哪个更耐低温" } });

  // 慢 runner 上的两条竞态都在这里收口（CI 在 #758 上红过一次，本机 8 连跑不复现）：
  // 审阅卡挂载后有一个按 contract 清空回答的 effect，它若落在输入之后，填进去的回答
  // 会被冲掉；卡片若在此期间重挂，先前取到的按钮就是一个永远 disabled 的过期节点。
  // 所以每一轮都**重新查询**输入框与按钮，回答被冲掉就再填一次，而不是攥着第一次
  // 查到的节点等它变可用。超时放宽只是给慢 runner 留余量，断言的仍是因果。
  const slow = { timeout: 5000 };
  async function submitWith(answer: string, nth: number) {
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByRole("region", { name: "确认逐步推理的问题理解" }, slow);
    const confirmButton = async () => {
      let button!: HTMLElement;
      await waitFor(() => {
        const card = screen.getByRole("region", { name: "确认逐步推理的问题理解" });
        const box = within(card).getByRole("textbox", { name: "要比较哪几个体系？的补充答案" }) as HTMLTextAreaElement;
        if (box.value !== answer) fireEvent.change(box, { target: { value: answer } });
        button = within(card).getByRole("button", { name: "确认并开始检索" });
        expect(button).toBeEnabled();
      }, slow);
      return button;
    };
    fireEvent.click(await confirmButton());
    await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(nth), slow);
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled(), slow);
  }

  await submitWith("磷酸铁锂与三元", 1);
  await submitWith("磷酸铁锂与三元", 2);
  await submitWith("钠离子与三元", 3);
  const ids = api.ask.mock.calls.map((call) => call[0].client_request_id);
  expect(ids[1]).toBe(ids[0]);
  expect(ids[2]).not.toBe(ids[0]);
});

test("a shared-engine job discloses partial retrieval, not the legacy lexical fallback", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [{
    ...job("done"), searched_notebook_ids: ["nb-0"], cited_notebook_ids: ["nb-0"],
    degraded_notebook_ids: ["nb-0"], answer: standardAnswer(),
  }]));
  render(<GlobalAskPage />);
  await screen.findByText("跨库结论", { exact: false });
  const receipt = await screen.findByLabelText("本轮检索回执");
  expect(receipt).toHaveTextContent("1 个笔记本有部分检索未完成，这些笔记本的资料可能覆盖不全");
  expect(receipt).not.toHaveTextContent("词法降级");
});

test.each([
  ["running", "running" as const, true],
  ["finished with an emptied answer trace", "done" as const, false],
])("trace panel appears when trace is non-empty (%s)", async (_label, status, live) => {
  const trace = [{ step_type: "retrieve", summary: "检索材料研究", detail: {}, duration_ms: 120 }];
  const turn: GlobalJob = {
    ...job(status), mode: "reasoning", trace,
    // 完成后 `job.trace` 可能被清空、以 `answer.reasoning_trace` 为准；这里反过来
    // 测另一半：答案里的轨迹空了，作业上那一份仍然要显示出来。
    ...(status === "done" ? { answer: standardAnswer({ reasoning_trace: [] }) } : {}),
  };
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [turn]));
  api.poll.mockResolvedValue(turn);
  const { container } = render(<GlobalAskPage />);
  expect(await screen.findByText("检索材料研究")).toBeTruthy();
  const panel = await waitFor(() => {
    const node = container.querySelector(".reasoning-trace-panel");
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
  expect(panel.classList.contains("live")).toBe(live);
  const summary = within(panel).getByRole("button", { expanded: false });
  fireEvent.click(summary);
  await waitFor(() => expect(summary).toHaveAttribute("aria-expanded", "true"));
  expect(within(panel).getByRole("listitem")).toHaveTextContent("检索材料研究");
});

/** 打开浮窗、载入这条新作业、点开第一条引用的卡片。返回卡片与它的行内标记。 */
async function openAnsweredCard(container: HTMLElement) {
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "对话 conv-a" }));
  await screen.findByText("跨库结论", { exact: false });
  const marker = await screen.findByRole("button", { name: "[1]" });
  fireEvent.click(marker);
  const card = await waitFor(() => {
    const node = container.querySelector(".cite-popover");
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
  return { card, marker };
}

// 新作业那条路径上，卡片由 AnswerView 内部渲染，走的是 citation-card 自己的 Esc
// 拦截（window 捕获期 preventDefault + stopPropagation）。#751 那三条保护必须在这
// 条路径上同样成立——workspace 那份 bespoke effect 已经删掉，靠的就是下沉的这一份。
test("Escape closes only the AnswerView citation card, never the floating chat window", async () => {
  installDialogMethods();
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [answeredJob]));
  const { container } = render(<Launcher />);
  const { card, marker } = await openAnsweredCard(container);
  // 卡片必须渲染在 <dialog> 子树内：全屏形态 showModal() 把 dialog 提到 top layer。
  expect(screen.getByRole("dialog", { name: "全局问答" }).contains(card)).toBe(true);

  // ⚠ 从 **dialog 子树内**的元素派发（刚点过的行内标记，真实按键时焦点就在它上面）。
  // 从 document.body 派发的事件压根不经过 dialog，把拦截删掉也不会红。
  fireEvent.keyDown(marker, { key: "Escape" });
  await waitFor(() => expect(container.querySelector(".cite-popover")).toBeNull());
  expect(screen.getByRole("dialog", { name: "全局问答" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "输入问题" })).toBeTruthy();
  // 卡片关掉之后，同一个 Esc 才轮到浮窗自己。
  fireEvent.keyDown(screen.getByRole("dialog", { name: "全局问答" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull());
});

test("collapsing the window closes the AnswerView card and releases the page's Escape", async () => {
  installDialogMethods();
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [answeredJob]));
  const { container } = render(<Launcher />);
  await openAnsweredCard(container);
  // 收起按钮是普通 click（不派发 pointerdown），所以这里单独验的就是 dismissSignal：
  // 浮窗收起后本组件仍然挂载，卡片不跟着收就会把宿主页面的下一次 Esc 整个吞掉。
  fireEvent.click(screen.getByRole("button", { name: "收起全局问答" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull());
  await waitFor(() => expect(container.querySelector(".cite-popover")).toBeNull());

  const pageEscape = vi.fn();
  window.addEventListener("keydown", pageEscape);
  try {
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(pageEscape).toHaveBeenCalledTimes(1);
  } finally {
    window.removeEventListener("keydown", pageEscape);
  }
});

test("a modified click on the AnswerView card's open-notebook goes to a new tab without collapsing the window", async () => {
  installDialogMethods();
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [answeredJob]));
  const { container } = render(<Launcher />);
  const { card } = await openAnsweredCard(container);
  fireEvent.click(within(card).getByRole("link", { name: "打开笔记本" }), { metaKey: true });
  expect(screen.getByRole("dialog", { name: "全局问答" })).toBeTruthy();
});

test("owner retirement during an in-flight intent preview cannot strand the composer", async () => {
  const pending = deferred<QueryIntentContract>();
  api.intent.mockReturnValue(pending.promise);
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.selectMode("reasoning"));
  act(() => result.current.setDraft("请逐步比较两个体系"));
  await act(async () => { void result.current.submit(); });
  await waitFor(() => expect(result.current.intentChecking).toBe(true));

  // load() 推进 owner。在途预检必须在同一处退休，否则它的 finally 会因 ticket 不
  // 匹配而跳过 setIntentChecking(false)，界面永久停在「取消问题理解」。
  await act(async () => { await result.current.load(); });
  expect(result.current.intentChecking).toBe(false);
  expect(result.current.intentReview).toBeNull();
  // 迟到的预检响应也不能把界面重新锁住，更不能建出作业。
  await act(async () => { pending.resolve(clearContract("请逐步比较两个体系")); });
  expect(result.current.intentChecking).toBe(false);
  expect(api.ask).not.toHaveBeenCalled();
  // 真正的判据是「界面没被卡死」：换回通用问答，下一次提问照常发得出去。
  act(() => result.current.selectMode("chunk"));
  act(() => result.current.setDraft("换个问题"));
  await act(async () => { await result.current.submit(); });
  expect(api.ask).toHaveBeenCalledTimes(1);
});

test("cancelling the understanding step after the stream returned still creates no job", async () => {
  const pending = deferred<QueryIntentContract>();
  api.intent.mockReturnValue(pending.promise);
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.selectMode("reasoning"));
  act(() => result.current.setDraft("请逐步比较两个体系"));
  await act(async () => { void result.current.submit(); });
  await waitFor(() => expect(result.current.intentChecking).toBe(true));

  await act(async () => {
    // 同一个同步块：流已返回（resolve），await 的后续还没跑，用户此刻按下取消。
    pending.resolve(clearContract("请逐步比较两个体系"));
    result.current.abortIntent();
  });
  expect(api.ask).not.toHaveBeenCalled();
  await waitFor(() => expect(result.current.intentChecking).toBe(false));
});

// ---- 提交与停止：与笔记本内问答同一套风格（规则见 app/stopped-turn.tsx）----

test("the question is on screen the moment it is sent, before the job exists", async () => {
  const submission = deferred<GlobalJob>();
  api.ask.mockReturnValue(submission.promise);
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("立即上屏的问题"));
  await act(async () => { void result.current.submit(); });
  expect(result.current.pending?.question).toBe("立即上屏的问题");
  expect(result.current.draft).toBe("");
  expect(result.current.turns).toEqual([]);
  await act(async () => { submission.resolve({ ...job(), question: "立即上屏的问题" }); });
  expect(result.current.pending).toBeNull();
  expect(result.current.turns.map((turn) => turn.question)).toEqual(["立即上屏的问题"]);
});

test("the understanding step runs live under the pending question and is handed to the job", async () => {
  const understanding = deferred<QueryIntentContract>();
  let heartbeat: ((elapsed: number) => void) | undefined;
  api.intent.mockImplementation((_q: string, _c: unknown, _s: unknown, _signal: unknown, onHeartbeat: (elapsed: number) => void) => {
    heartbeat = onHeartbeat;
    return understanding.promise;
  });
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.selectMode("reasoning"));
  act(() => result.current.setDraft("请逐步比较"));
  await act(async () => { void result.current.submit(); });
  expect(result.current.pending?.trace.map((step) => step.step_type)).toEqual(["intent"]);
  act(() => heartbeat?.(2500));
  expect(result.current.pending?.trace).toHaveLength(1);
  expect(result.current.pending?.trace[0].duration_ms).toBe(2500);
  await act(async () => { understanding.resolve(clearContract("请逐步比较")); });
  await waitFor(() => expect(result.current.turns).toHaveLength(1));
  const seed = result.current.traceSeeds[result.current.turns[0].job_id];
  expect(seed.map((step) => step.summary)).toEqual(["已理解问题"]);
  // 交接时摘掉耗时：后端回放的 intent 步才是计时的那一份。
  expect(seed[0].duration_ms).toBeUndefined();
});

test("stopping during understanding returns the question to the input and leaves no turn", async () => {
  api.intent.mockImplementation((_q: string, _c: unknown, _s: unknown, signal: AbortSignal) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.selectMode("reasoning"));
  act(() => result.current.setDraft("还没想好的问题"));
  await act(async () => { void result.current.submit(); });
  await waitFor(() => expect(result.current.intentChecking).toBe(true));
  await act(async () => { await result.current.stop(); });
  await waitFor(() => expect(result.current.intentChecking).toBe(false));
  expect(result.current.draft).toBe("还没想好的问题");
  expect(result.current.pending).toBeNull();
  expect(result.current.error).toBe("");
  expect(api.ask).not.toHaveBeenCalled();
});

test("stopping before the submission returns cancels and discards the job once it exists", async () => {
  const submission = deferred<GlobalJob>();
  api.ask.mockReturnValue(submission.promise);
  api.cancel.mockResolvedValue({ ...job("cancelled"), question: "提交途中按了停止" });
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("提交途中按了停止"));
  await act(async () => { void result.current.submit(); });
  await act(async () => { await result.current.stop(); });
  expect(result.current.stopping).toBe(true);
  expect(api.cancel).not.toHaveBeenCalled();
  await act(async () => { submission.resolve({ ...job(), question: "提交途中按了停止" }); });
  expect(api.cancel).toHaveBeenCalledWith("job-conv-a", true);
  expect(result.current.turns).toEqual([]);
  expect(result.current.conversationId).toBe("");
  expect(result.current.draft).toBe("提交途中按了停止");
  expect(result.current.stopping).toBe(false);
});

test("in reasoning mode a stop after understanding returned still reaches the submission", async () => {
  // 理解已经返回、建作业的 POST 还在途：这一刻按下的停止不能落到「取消问题理解」
  // 那条已经没东西可取消的路径上（评审 P1）。
  const submission = deferred<GlobalJob>();
  api.ask.mockReturnValue(submission.promise);
  api.cancel.mockResolvedValue({ ...job("cancelled"), question: "请逐步比较" });
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.selectMode("reasoning"));
  act(() => result.current.setDraft("请逐步比较"));
  await act(async () => { void result.current.submit(); });
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  expect(result.current.intentChecking).toBe(false);
  expect(result.current.submitting).toBe(true);
  await act(async () => { await result.current.stop(); });
  expect(result.current.stopping).toBe(true);
  await act(async () => { submission.resolve({ ...job(), question: "请逐步比较" }); });
  expect(api.cancel).toHaveBeenCalledWith("job-conv-a", true);
  expect(result.current.turns).toEqual([]);
  expect(result.current.draft).toBe("请逐步比较");
  expect(result.current.pending).toBeNull();
});

test("stopping a job that has shown nothing discards it and returns the question", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  // 第一次读是打开会话；停止之后的对账读到 404——这条问题开出来的会话一起没了。
  api.detail.mockResolvedValueOnce(detail("conv-a", [job()])).mockRejectedValue(conversationGone());
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  expect(api.cancel).toHaveBeenCalledWith("job-conv-a", true);
  expect(result.current.turns).toEqual([]);
  expect(result.current.draft).toBe("共同问题是什么？");
  // 这条问题开出来的会话随它一起丢弃：本地退回「还没有会话」。
  expect(result.current.conversationId).toBe("");
  expect(window.location.search).toBe("");
  expect(result.current.conversations).toEqual([]);
});

test("discarding a listed conversation moves the history cursor with it", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-0");
  const page = Array.from({ length: 50 }, (_, index) => conversation(`conv-${index}`));
  api.list.mockImplementation((offset = 0) => Promise.resolve(offset === 0 ? page : []));
  api.detail.mockResolvedValueOnce(detail("conv-0", [job("running", "conv-0")])).mockRejectedValue(conversationGone());
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockResolvedValue(job("cancelled", "conv-0"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  expect(result.current.conversations).toHaveLength(49);
  await act(async () => { await result.current.loadMoreHistory(); });
  // 服务端少了一条会话：下一页从 49 而不是 50 开始取，才不会跳过边界上的那一条。
  expect(api.list).toHaveBeenLastCalledWith(49);
});

test("a discard the server did not carry out leaves the turn and the conversation alone", async () => {
  // 别的标签页先一步停了这条作业：`cancel?discard=true` 照样回 cancelled，却什么都
  // 没删。确认不到 404，本地就不许摘记录、更不许丢会话（codex #761 R2 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  // 对账读到的会话里它还在（已停止）：服务端没删，本地就不摘。
  api.detail.mockResolvedValueOnce(detail("conv-a", [job()])).mockResolvedValue(detail("conv-a", [job("cancelled")]));
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  expect(api.detail).toHaveBeenCalledTimes(2);
  expect(result.current.turns.map((turn) => turn.status)).toEqual(["cancelled"]);
  expect(result.current.conversationId).toBe("conv-a");
  expect(result.current.conversations).toHaveLength(1);
  expect(result.current.draft).toBe("");
});

test("discarding a loaded turn moves the older-turns cursor with it", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const earlier = { ...job("done"), job_id: "job-earlier", answer: standardAnswer() };
  let discarded = false;
  api.detail.mockImplementation((id: string, offset = 0) => Promise.resolve(offset !== 0 ? detail(id, [])
    : discarded ? { ...detail(id, [earlier]), has_more: true, next_offset: 1 }
      : { ...detail(id, [earlier, job()]), has_more: true, next_offset: 2 }));
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockImplementation(() => { discarded = true; return Promise.resolve(job("cancelled")); });
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  // 轮次与游标一起取自服务端的同一份第一页：少了一行，游标就是 1，不靠本地推算。
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-earlier"]);
  expect(result.current.turnOffset).toBe(1);
  expect(result.current.conversationId).toBe("conv-a");
  await act(async () => { await result.current.loadMoreTurns(); });
  expect(api.detail).toHaveBeenLastCalledWith("conv-a", 1);
});

test("a follow-up stopped before any output keeps the conversation and its earlier turns", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const earlier = { ...job("done"), job_id: "job-earlier", answer: standardAnswer() };
  api.detail.mockResolvedValueOnce(detail("conv-a", [earlier, job()])).mockResolvedValue(detail("conv-a", [earlier]));
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  expect(result.current.draft).toBe("共同问题是什么？");
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-earlier"]);
  expect(result.current.conversationId).toBe("conv-a");
});

test("a job stopped mid-process stays, can be edited, and the next question replaces it", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const progressed = { ...job(), trace: [{ step_type: "search", summary: "初检索得到 19 个候选", detail: {} }] };
  api.detail.mockResolvedValue(detail("conv-a", [progressed]));
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockResolvedValue({ ...progressed, status: "cancelled" });
  const replacement = deferred<GlobalJob>();
  api.ask.mockReturnValue(replacement.promise);
  render(<GlobalAskPage />);
  fireEvent.click(await screen.findByRole("button", { name: "停止" }));
  await waitFor(() => expect(api.cancel).toHaveBeenCalledWith("job-conv-a", false));
  // 情形一：记录留在对话里，问题不弹回输入框。
  expect(await screen.findByText(STOPPED_TURN_TEXT)).toBeTruthy();
  const input = screen.getByRole("textbox", { name: "输入问题" });
  expect(input).toHaveValue("");
  expect(screen.getByText("初检索得到 19 个候选")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "编辑问题" }));
  expect(input).toHaveValue("共同问题是什么？");
  expect(screen.getByText(STOPPED_TURN_TEXT)).toBeTruthy();
  fireEvent.change(input, { target: { value: "改好的问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  expect(api.ask.mock.calls[0][0].replaces_job_id).toBe("job-conv-a");
  // 新问题一上屏，旧记录就让位。
  expect(screen.queryByText(STOPPED_TURN_TEXT)).toBeNull();
  expect(screen.getByText("改好的问题")).toBeTruthy();
  await act(async () => { replacement.resolve({ ...job(), job_id: "job-new", question: "改好的问题" }); });
  expect(screen.queryByText("共同问题是什么？")).toBeNull();
  expect(screen.getByText("改好的问题")).toBeTruthy();
});

test("a late recovery read cannot overwrite a re-send that has since succeeded", async () => {
  // 带替换的提交失败后会自动重拉会话（不 await）；用户紧接着重试成功，迟到的旧快照
  // 不得把刚接受的新一轮换回那条已被服务端删掉的停止记录（codex #761 R1 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const stopped = { ...job("cancelled"), searched_notebook_ids: ["nb-0"] };
  const recovery = deferred<GlobalConversationDetail>();
  api.detail.mockResolvedValueOnce(detail("conv-a", [stopped])).mockReturnValueOnce(recovery.promise);
  api.ask.mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce({ ...job(), job_id: "job-new", question: "重试成功的问题" });
  api.poll.mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.turns).toHaveLength(1));
  act(() => result.current.setDraft("重试成功的问题"));
  await act(async () => { await result.current.submit(); });
  expect(result.current.error).not.toBe("");
  await act(async () => { await result.current.submit(); });
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-new"]);
  await act(async () => { recovery.resolve(detail("conv-a", [stopped])); });
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-new"]);
});

test("a replacement whose response was lost is claimed, not offered as a retry", async () => {
  // 替换其实已经提交、只是响应丢了：重拉会话会看到一条没见过的、问题相同的作业。
  // 认领它；否则「重试」会因为已无可替换的记录而换一个 request id，建出重复回答
  // （codex #761 R3 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const stopped = { ...job("cancelled"), searched_notebook_ids: ["nb-0"] };
  api.detail.mockResolvedValueOnce(detail("conv-a", [stopped])).mockImplementationOnce(() => Promise.resolve(
    // 服务端那条作业带着**这次提交**的幂等 id：凭它认领，不凭问题文本。
    detail("conv-a", [{ ...job(), job_id: "job-accepted", question: "其实已经提交的问题", client_request_id: api.ask.mock.calls[0][0].client_request_id }]),
  ));
  api.ask.mockRejectedValueOnce(new Error("network"));
  api.poll.mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.turns).toHaveLength(1));
  act(() => result.current.setDraft("其实已经提交的问题"));
  await act(async () => { await result.current.submit(); });
  await waitFor(() => expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-accepted"]));
  expect(result.current.draft).toBe("");
  expect(result.current.error).toBe("");
  expect(result.current.running?.job_id).toBe("job-accepted");
  expect(api.ask).toHaveBeenCalledTimes(1);
});

test("a stale replacement drops its retry identity once the real turns are known", async () => {
  // 那条「已停止」记录在服务端已经不在了、也没有我们的作业：下一次提交必须是一次
  // 全新的、不带替换的提交，而不是带着同一个过期 id 再 409 一次。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const stopped = { ...job("cancelled"), searched_notebook_ids: ["nb-0"] };
  const others = { ...job("done"), job_id: "job-others", question: "别处问的问题", answer: standardAnswer() };
  api.detail.mockResolvedValueOnce(detail("conv-a", [stopped])).mockResolvedValueOnce(detail("conv-a", [others]));
  api.ask.mockRejectedValueOnce(new Error("conflict"))
    .mockResolvedValueOnce({ ...job(), job_id: "job-fresh", question: "再发一次" });
  api.poll.mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.turns).toHaveLength(1));
  act(() => result.current.setDraft("再发一次"));
  await act(async () => { await result.current.submit(); });
  await waitFor(() => expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-others"]));
  expect(result.current.draft).toBe("再发一次");
  await act(async () => { await result.current.submit(); });
  expect(api.ask.mock.calls[1][0].replaces_job_id).toBeUndefined();
  expect(api.ask.mock.calls[1][0].client_request_id).not.toBe(api.ask.mock.calls[0][0].client_request_id);
});

test("a stop that lost the race to the answer adopts the whole finished job", async () => {
  // 提交在途时按了停止，而作业抢在取消到达之前已经答完：取消接口交回的是带回答的
  // done 作业，必须整份接管——只抄状态会留下一条「done 却没有回答」的轮次（R4 P2）。
  const submission = deferred<GlobalJob>();
  api.ask.mockReturnValue(submission.promise);
  const finished = { ...job("done"), question: "抢先答完的问题", answer: standardAnswer() };
  api.cancel.mockResolvedValue(finished);
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("抢先答完的问题"));
  await act(async () => { void result.current.submit(); });
  await act(async () => { await result.current.stop(); });
  await act(async () => { submission.resolve({ ...job(), question: "抢先答完的问题" }); });
  // 取消接口交回的就是终态：无需再对账。
  expect(api.detail).not.toHaveBeenCalled();
  expect(result.current.turns).toHaveLength(1);
  expect(result.current.turns[0].status).toBe("done");
  expect(result.current.turns[0].answer?.answer).toBe("跨库结论 [k1]。");
  expect(result.current.draft).toBe("");
});

test("an unconfirmed discard keeps the turn, and a vanished conversation is dropped on the next failure", async () => {
  // 停掉了、但对账读不到（网络错）：什么都不能断言，记录留着。会话若真的已经不在，
  // 下一次提交失败后的对账读到 404，本地退回「还没有会话」，不再指着一个死会话。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValueOnce(detail("conv-a", [job()])).mockRejectedValueOnce(new Error("network"));
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockResolvedValue(job("cancelled"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  expect(result.current.turns.map((turn) => turn.status)).toEqual(["cancelled"]);
  expect(result.current.conversationId).toBe("conv-a");

  api.ask.mockRejectedValueOnce(new Error("对话不存在，请刷新列表。"));
  api.detail.mockRejectedValueOnce(conversationGone());
  act(() => result.current.setDraft("再问一次"));
  await act(async () => { await result.current.submit(); });
  await waitFor(() => expect(result.current.conversationId).toBe(""));
  expect(result.current.turns).toEqual([]);
  expect(result.current.draft).toBe("再问一次");
  expect(window.location.search).toBe("");
});

test("a discard whose response was lost is still reconciled", async () => {
  // 服务端已经停掉并删了作业、只是响应丢了：对账读到会话已不在，就按丢弃收尾，不留
  // 一条永远 404 的 running 作业把输入区锁死（codex #761 R5 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValueOnce(detail("conv-a", [job()])).mockRejectedValue(conversationGone());
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockRejectedValue(new Error("network"));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await act(async () => { await result.current.stop(); });
  expect(result.current.turns).toEqual([]);
  expect(result.current.running).toBeUndefined();
  expect(result.current.draft).toBe("共同问题是什么？");
  expect(result.current.error).toBe("");
  expect(result.current.conversationId).toBe("");
});

test("a job another tab discarded ends this tab's polling instead of retrying a 404 forever", async () => {
  // 两个标签页盯着同一条作业，另一个把它停掉并丢弃了：这边的轮询读到 404。那不是
  // 「暂时读不到」——对一次账、这一轮到此为止，输入区解锁（codex #761 R6 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const earlier = { ...job("done"), job_id: "job-earlier", answer: standardAnswer() };
  api.detail.mockResolvedValueOnce(detail("conv-a", [earlier, job()])).mockResolvedValue(detail("conv-a", [earlier]));
  api.poll.mockRejectedValue(humanizedError("问答任务不存在，请刷新对话。", 404));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  await waitFor(() => expect(result.current.running).toBeUndefined(), { timeout: 2500 });
  expect(api.poll).toHaveBeenCalledTimes(1);
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-earlier"]);
  expect(result.current.pollError).toBe("");
  expect(result.current.conversationId).toBe("conv-a");
});

test("a Stop pressed during a replacement whose response was lost is still honoured", async () => {
  // 替换已被服务端接受、响应丢了，而用户在提交途中按过停止：认领那条作业的同时要
  // 兑现这次停止，不能让它悄悄跑下去（codex #761 R6 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const stopped = { ...job("cancelled"), searched_notebook_ids: ["nb-0"] };
  const accepted = { ...job(), job_id: "job-accepted", question: "其实已经提交的问题" };
  const submission = deferred<GlobalJob>();
  api.detail.mockResolvedValueOnce(detail("conv-a", [stopped]))
    .mockImplementationOnce(() => Promise.resolve(detail("conv-a", [
      { ...accepted, client_request_id: api.ask.mock.calls[0][0].client_request_id },
    ])))
    .mockRejectedValue(conversationGone());
  // 提交途中按过停止 → 失败后会用同一个幂等 id 重发一次；这里两次都失败，走对账。
  api.ask.mockReturnValueOnce(submission.promise).mockRejectedValue(new Error("network"));
  api.cancel.mockResolvedValue({ ...accepted, status: "cancelled" });
  api.poll.mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.turns).toHaveLength(1));
  act(() => result.current.setDraft("其实已经提交的问题"));
  await act(async () => { void result.current.submit(); });
  await act(async () => { await result.current.stop(); });
  await act(async () => { submission.reject(new Error("network")); });
  await waitFor(() => expect(api.cancel).toHaveBeenCalledWith("job-accepted", true));
  await waitFor(() => expect(result.current.turns).toEqual([]));
  expect(result.current.running).toBeUndefined();
  expect(result.current.draft).toBe("其实已经提交的问题");
});

test("a Stop during a new conversation's first submission survives a lost response", async () => {
  // 新会话第一问：响应丢了时连会话 id 都没有、无从对账。用同一个幂等 id 再发一次，
  // 已建的作业被原样回放，那次停止才够得着它（codex #761 R7 P2）。
  const first = deferred<GlobalJob>();
  api.ask.mockReturnValueOnce(first.promise).mockResolvedValueOnce({ ...job(), question: "第一问" });
  api.cancel.mockResolvedValue({ ...job("cancelled"), question: "第一问" });
  api.detail.mockRejectedValue(conversationGone());
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("第一问"));
  await act(async () => { void result.current.submit(); });
  await act(async () => { await result.current.stop(); });
  await act(async () => { first.reject(new Error("network")); });
  await waitFor(() => expect(api.cancel).toHaveBeenCalledWith("job-conv-a", true));
  expect(api.ask).toHaveBeenCalledTimes(2);
  expect(api.ask.mock.calls[1][0].client_request_id).toBe(api.ask.mock.calls[0][0].client_request_id);
  await waitFor(() => expect(result.current.submitting).toBe(false));
  expect(result.current.turns).toEqual([]);
  expect(result.current.draft).toBe("第一问");
  expect(result.current.error).toBe("");
});

test("a failed submission nobody tried to stop is not re-sent behind the user's back", async () => {
  api.ask.mockRejectedValue(new Error("network"));
  const { result } = renderHook(() => useGlobalAsk({ syncUrl: false }));
  await waitFor(() => expect(result.current.loading).toBe(false));
  act(() => result.current.setDraft("普通失败"));
  await act(async () => { await result.current.submit(); });
  expect(api.ask).toHaveBeenCalledTimes(1);
  expect(result.current.draft).toBe("普通失败");
  expect(result.current.error).not.toBe("");
});

test("an older-turns page that was in flight cannot undo a reconciliation", async () => {
  // 「加载更早的问答」在途时一次停止并丢弃换掉了第一页与游标：迟到的旧页不得再把
  // 它的行与旧游标写回来（codex #761 R7 P2）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const earlier = { ...job("done"), job_id: "job-earlier", answer: standardAnswer() };
  const stale = deferred<GlobalConversationDetail>();
  let discarded = false;
  api.detail.mockImplementation((id: string, offset = 0) => offset !== 0 ? stale.promise
    : Promise.resolve(discarded ? { ...detail(id, [earlier]), has_more: true, next_offset: 1 }
      : { ...detail(id, [earlier, job()]), has_more: true, next_offset: 2 }));
  api.poll.mockReturnValue(new Promise(() => {}));
  api.cancel.mockImplementation(() => { discarded = true; return Promise.resolve(job("cancelled")); });
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.running).toBeTruthy());
  act(() => { void result.current.loadMoreTurns(); });
  await act(async () => { await result.current.stop(); });
  expect(result.current.turnOffset).toBe(1);
  await act(async () => {
    stale.resolve({ ...detail("conv-a", [{ ...job("done"), job_id: "job-oldest" }]), has_more: true, next_offset: 3 });
  });
  expect(result.current.turnOffset).toBe(1);
  expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-earlier"]);
});

test("another tab's identical question is never claimed, and never stopped on this tab's behalf", async () => {
  // 两个标签页用同一句话替换同一条记录：赢的是对面那条。这边失败后对账看到它，问题
  // 文本一模一样，但幂等 id 不是这边的——不认领、更不替用户把它停掉（codex #761 R8）。
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const stopped = { ...job("cancelled"), searched_notebook_ids: ["nb-0"] };
  const theirs = { ...job(), job_id: "job-theirs", question: "同一句话", client_request_id: "the-other-tab" };
  const submission = deferred<GlobalJob>();
  api.detail.mockResolvedValueOnce(detail("conv-a", [stopped])).mockResolvedValue(detail("conv-a", [theirs]));
  api.ask.mockReturnValueOnce(submission.promise).mockRejectedValue(humanizedError("上一条问题已无法替换，请刷新对话后重新提交。", 409));
  api.poll.mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useGlobalAsk());
  await waitFor(() => expect(result.current.turns).toHaveLength(1));
  act(() => result.current.setDraft("同一句话"));
  await act(async () => { void result.current.submit(); });
  await act(async () => { await result.current.stop(); });
  await act(async () => { submission.reject(humanizedError("上一条问题已无法替换，请刷新对话后重新提交。", 409)); });
  await waitFor(() => expect(result.current.turns.map((turn) => turn.job_id)).toEqual(["job-theirs"]));
  expect(api.cancel).not.toHaveBeenCalled();
  expect(result.current.draft).toBe("同一句话");
  expect(result.current.error).not.toBe("");
});

test("an older stopped turn wears the same notice without promising a replacement", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const older = { ...job("cancelled"), job_id: "job-older", question: "更早停下的问题" };
  const later = { ...job("done"), job_id: "job-later", answer: standardAnswer() };
  api.detail.mockResolvedValue(detail("conv-a", [older, later]));
  render(<GlobalAskPage />);
  expect(await screen.findByText(STOPPED_TURN_KEPT_TEXT)).toBeTruthy();
  expect(screen.queryByText(STOPPED_TURN_TEXT)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "编辑问题" }));
  expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("更早停下的问题");
});

test("a failed re-send brings the stopped record back and returns the new question", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const stopped = { ...job("cancelled"), searched_notebook_ids: ["nb-0"] };
  api.detail.mockResolvedValue(detail("conv-a", [stopped]));
  api.ask.mockRejectedValue(new Error("network"));
  render(<GlobalAskPage />);
  expect(await screen.findByText(STOPPED_TURN_TEXT)).toBeTruthy();
  const input = screen.getByRole("textbox", { name: "输入问题" });
  fireEvent.change(input, { target: { value: "另一个问题" } });
  fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(screen.getByText(STOPPED_TURN_TEXT)).toBeTruthy();
  expect(input).toHaveValue("另一个问题");
});

test("citation badge shows the owning notebook for every citation", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [{
    ...job("done"), cited_notebook_ids: ["nb-0", "nb-1"],
    answer: standardAnswer({
      answer: "两个库都提到 [k1][k2]。",
      anchors: [
        // 第一个库（resolved 集里的头一个）同样带非空 notebook_id：它不是「本库」，
        // 全局模式下没有哪一个库是隐含的当前笔记本。
        {
          key: "k1", object_id: "", object_type: "element", label: "材料记录",
          name: "材料记录", source_title: "材料记录", location_label: "第 2 页",
          source_id: "source-1", element_id: "element-1", notebook_id: "nb-0",
          tier: "personal", snippet: "低温环境下，容量下降。",
        },
        {
          key: "k2", object_id: "", object_type: "element", label: "散热记录",
          name: "散热记录", source_title: "散热记录", location_label: "第 5 页",
          source_id: "source-2", element_id: "element-2", notebook_id: "nb-1",
          tier: "personal", snippet: "风道改形后温升下降。",
        },
      ],
    }),
  }]));
  render(<GlobalAskPage />);
  for (const [marker, name, href] of [
    ["[1]", "材料研究", "/#notebook=nb-0&source=source-1"],
    ["[2]", "热管理", "/#notebook=nb-1&source=source-2"],
  ] as const) {
    fireEvent.click(await screen.findByRole("button", { name: marker }));
    const card = await screen.findByRole("dialog");
    expect(card).toHaveTextContent(`来自「${name}」（个人知识库）`);
    expect(within(card).getByRole("link", { name: "打开笔记本" })).toHaveAttribute("href", href);
    await act(async () => { document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })); });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }
});

// --- 回答里的附图 -------------------------------------------------------------
//
// 全局问答没有 active notebook，早先因此整块不渲染附图。现在取图归属由
// `assetNotebookId` 单点裁定：没有 active 就用这条引用**自己的**所属库——那是本轮
// 范围里用户自己有读权、经 `can_read_many` 准入过的库，而取图端点每次请求仍跑
// `require_notebook_read` + 「资产所属库在该 notebook 的有效参与集内」。断言因此盯
// 死请求 URL 里的笔记本 id：它必须是引用自己的库，而不是别的库、也不是拼不出来。
const answerWithImages = (overrides: Partial<AskResponse> = {}) => standardAnswer({
  answer: "跨库结论 [k1]。\n\n后一段。",
  anchors: [{
    key: "k1", object_id: "", object_type: "element", label: "测试记录",
    name: "测试记录", source_title: "测试记录", location_label: "第 2 页",
    source_id: "source-1", element_id: "element-1", notebook_id: "nb-1",
    tier: "personal", snippet: "低温环境下，容量下降。",
    images: [{ element_id: "img-el-1", asset_id: "asset-1", caption: "图 1：示意图" }],
  }],
  ...overrides,
});

test("global answers show citation images, read through each citation's own notebook", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  api.detail.mockResolvedValue(detail("conv-a", [{
    ...job("done"), cited_notebook_ids: ["nb-1"], answer: answerWithImages(),
  }]));
  render(<GlobalAskPage />);

  // ① 正文内联附图：`notebookId={null}` 不再让整块消失。
  const region = await screen.findByRole("complementary", { name: "引用图片 [1]" });
  await within(region).findByRole("img", { name: "图 1：示意图" });
  await waitFor(() => expect(api.assetBlob).toHaveBeenCalled());
  expect(api.assetBlob.mock.calls[0][0]).toContain("/notebooks/nb-1/assets/asset-1");
  // 没有承接方时不留一颗点了没反应的控件：全局问答不接页面级放大预览。
  expect(within(region).queryByRole("button", { name: /放大查看/ })).toBeNull();

  // ② 引用卡里的「本段附图」：同一条归属规则，同一个库。
  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(within(card).getByText("本段附图")).toBeTruthy();
  await within(card).findByRole("img", { name: "图 1：示意图" });
  await waitFor(() => expect(api.assetBlob).toHaveBeenCalledTimes(2));
  for (const [url] of api.assetBlob.mock.calls) {
    expect(url).toContain("/notebooks/nb-1/assets/asset-1");
  }
});

test("a citation with no owning notebook renders no image block at all", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  api.list.mockResolvedValue([conversation()]);
  // 更老的回答可能既没有 active 也没有 notebook_id：归属算出来是空串，整块不渲染，
  // 绝不拼一个取不到的 URL 去换一张「图片加载失败」。
  const answer = answerWithImages();
  delete answer.anchors![0].notebook_id;
  api.detail.mockResolvedValue(detail("conv-a", [{
    ...job("done"), cited_notebook_ids: [], answer,
  }]));
  render(<GlobalAskPage />);

  await screen.findByText("跨库结论", { exact: false });
  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(within(card).queryByText("本段附图")).toBeNull();
  expect(screen.queryByRole("img", { name: "图 1：示意图" })).toBeNull();
  expect(api.assetBlob).not.toHaveBeenCalled();
});
