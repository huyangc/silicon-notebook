import { useEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { MemoryPanel } from "../../app/memory-panel";
import { MemorySuggestionAdoptionModal, MemorySuggestionStats, MemorySuggestionSummary } from "../../app/memory-suggestion-card";
import { MEMORY_SUGGESTION_RESULT_MS, useMemorySuggestionReview } from "../../app/use-memory-suggestion-review";
import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator";
import { ReleaseNotesModal } from "../../app/release-notes-modal";
import type { MemoryRecord } from "../../app/workspace-model";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const signal = new AbortController().signal;
const suggestion: MemoryRecord = {
  id: "suggestion-1", notebook_id: "notebook-1", created_by: "actor-a", origin: "ask_answer", suggested_signal: "repeated", status: "candidate", promotion_state: "none",
  title: "时钟门控", content_md: "按项目保留关键时序约束", tags: ["时序"], embedding_status: "none", embedding_error: "", created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z",
  provenance: { conversation_id: "conversation-1", answer_id: "answer-1", suggestion: { cluster_size: 3, refines: "memory-old" } },
};
const oldMemory: MemoryRecord = { ...suggestion, id: "memory-old", title: "旧约束", status: "confirmed", suggested_signal: null, provenance: {} };
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
function page(items: MemoryRecord[] = [suggestion], total = items.length, eligible = true) {
  return { items, total_count: total, owner_total_count: 900, owner_pending_count: 800, notebook_options: [], offset: 0, limit: 20, kg_extract_eligible: eligible };
}

function api(options: { memory?: MemoryRecord; otherMemory?: MemoryRecord; refines?: MemoryRecord | null; loadRefines?: () => Promise<Response>; confirm?: (body: Record<string, unknown>) => Promise<Response>; save?: (body: Record<string, unknown>) => Promise<Response>; deprecate?: () => Promise<Response>; eligibility?: () => Promise<Response> } = {}) {
  let memory = options.memory ?? suggestion;
  let otherMemory = options.otherMemory;
  const calls = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/memory-old")) return options.loadRefines ? options.loadRefines() : response(options.refines === undefined ? oldMemory : options.refines, options.refines === null ? 404 : 200);
    if (url.pathname.endsWith("/confirm")) {
      const result = options.confirm ? await options.confirm(JSON.parse(String(init?.body))) : response({ ...memory, status: "confirmed" });
      if (result.ok) memory = { ...memory, status: "confirmed" };
      return result;
    }
    if (url.pathname.endsWith("/deprecate")) return options.deprecate ? options.deprecate() : response({ ...oldMemory, status: "deprecated" });
    if (otherMemory && url.pathname.endsWith(`/${otherMemory.id}/reject`)) { otherMemory = { ...otherMemory, status: "rejected" }; return response(otherMemory); }
    if (url.pathname.endsWith("/reject")) { memory = { ...memory, status: "rejected" }; return response(memory); }
    if (init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      const result = options.save ? await options.save(body) : response({ ...memory, ...body });
      if (result.ok) memory = { ...memory, ...body };
      return result;
    }
    if (url.pathname.endsWith("/memories")) {
      if (url.searchParams.get("origin") === "suggested" && url.pathname === "/api/memories") {
        const status = url.searchParams.get("status");
        return response(page([], status === "confirmed" ? 103 : status === "deprecated" ? 7 : status === "rejected" ? 21 : 0));
      }
      if (options.eligibility && url.pathname.includes("/notebooks/") && url.searchParams.get("origin") === null && url.searchParams.get("status") === null) return options.eligibility();
      return response(page(otherMemory ? [memory, otherMemory] : [memory]));
    }
    return response([]);
  });
  vi.stubGlobal("fetch", calls);
  return calls;
}
function mutations(calls: ReturnType<typeof api>, action: string) { return calls.mock.calls.filter(([input]) => String(input).endsWith(`/${action}`)); }

function Harness({ actor = "actor-a", sessionSignal = signal, notebookId = "notebook-1", announcement = false, showPanel = true, onOpenAnswer = async () => true }: {
  actor?: string; sessionSignal?: AbortSignal; notebookId?: string | null; announcement?: boolean; showPanel?: boolean; onOpenAnswer?: (target: { notebookId: string; conversationId: string; answerId: string }) => Promise<boolean>;
}) {
  const modals = useRootModalCoordinator({ actorId: actor, sourceId: null, onClosed: (slot) => { if (slot === "memory-suggestion-adopt") review.commands.clear(); } });
  const review = useMemorySuggestionReview({ actorId: actor, sessionSignal, modals });
  useEffect(() => { if (announcement) modals.open("release-notes", modals.captureActorOwner()); }, [announcement]); // eslint-disable-line react-hooks/exhaustive-deps
  return <>
    {showPanel && <MemoryPanel key={`${actor}-${notebookId}`} scope={notebookId ? "notebook" : "global"} notebookId={notebookId} sessionSignal={sessionSignal} suggestionReview={review} onOpenAnswer={onOpenAnswer} />}
    <MemorySuggestionAdoptionModal review={review} modalView={modals.view("memory-suggestion-adopt")} />
    {modals.view("release-notes").open && <ReleaseNotesModal build={{ version: "test", ordinal: 1 }} notes={[]} interactive={modals.view("release-notes").topmost} zIndex={modals.view("release-notes").zIndex} onClose={() => { modals.requestClose("release-notes", "button"); }} />}
  </>;
}
async function adoption() {
  await userEvent.setup().click(await screen.findByRole("button", { name: "采纳" }));
  const dialog = screen.getByRole("dialog", { name: "采纳记忆建议" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认采纳" })).toBeEnabled());
  return dialog;
}

test("系统建议徽标采纳后仍保留，理由和原回答目标使用provenance", async () => {
  const onOpenAnswer = vi.fn(async () => true);
  api({ memory: { ...suggestion, status: "confirmed" } });
  render(<Harness onOpenAnswer={onOpenAnswer} />);
  const card = (await screen.findByRole("heading", { name: "时钟门控" })).closest("article")!;
  expect(within(card).getByText("系统建议")).toBeVisible();
  expect(within(card).getByText(/你问过 3 次相近的问题/)).toBeVisible();
  await userEvent.setup().click(within(card).getByRole("button", { name: "查看原回答" }));
  expect(onOpenAnswer).toHaveBeenCalledWith({ notebookId: "notebook-1", conversationId: "conversation-1", answerId: "answer-1" });
  expect(within(card).queryByRole("button", { name: "采纳" })).toBeNull();
});

test("澄清类显示确认理由；同步/传输存档不提供本地回答入口", () => {
  const { rerender } = render(<MemorySuggestionSummary memory={{ ...suggestion, suggested_signal: "clarified" }} onOpenAnswer={vi.fn()} sessionSignal={signal} />);
  expect(screen.getByText(/你在问题理解时确认了这一点/)).toBeVisible();
  rerender(<MemorySuggestionSummary memory={{ ...suggestion, provenance: { ...suggestion.provenance, sync_origin: "别处" } }} onOpenAnswer={vi.fn()} sessionSignal={signal} />);
  expect(screen.queryByRole("button", { name: "查看原回答" })).toBeNull();
});

test("来源筛选按suggested提交且不新增批量采纳或忽略", async () => {
  const calls = api();
  render(<Harness />);
  await screen.findByRole("heading", { name: "时钟门控" });
  await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "来源" }), "suggested");
  await waitFor(() => expect(calls.mock.calls.some(([input]) => String(input).includes("/notebooks/notebook-1/memories?") && String(input).includes("origin=suggested"))).toBe(true));
  expect(screen.queryByRole("button", { name: /批量采纳|批量忽略/ })).toBeNull();
});

test("纠正类展示理由与严格布尔冲突标记，采纳后仍保留", async () => {
  api({ memory: { ...suggestion, suggested_signal: "corrected", status: "confirmed",
    provenance: { ...suggestion.provenance, suggestion: { conflicts_with_sources: true } } } });
  render(<Harness />);
  const card = (await screen.findByRole("heading", { name: "时钟门控" })).closest("article")!;
  expect(within(card).getByText("系统建议")).toBeVisible();
  expect(within(card).getByText(/你在追问中纠正了回答/)).toBeVisible();
  expect(within(card).getByText("与资料不一致")).toBeVisible();
  expect(within(card).getByRole("button", { name: "查看原回答" })).toBeEnabled();
});

test("不把其它信号或非布尔冲突字段显示成资料冲突", () => {
  const { rerender } = render(<MemorySuggestionSummary memory={{ ...suggestion, suggested_signal: "corrected",
    provenance: { suggestion: { conflicts_with_sources: false } } }} sessionSignal={signal} />);
  for (const suggested_signal of ["corrected", "clarified", "repeated"] as const) {
    for (const conflicts_with_sources of [false, null, undefined, 1, "true", ...(suggested_signal === "corrected" ? [] : [true])]) {
      rerender(<MemorySuggestionSummary memory={{ ...suggestion, suggested_signal,
        provenance: { suggestion: { conflicts_with_sources } } }} sessionSignal={signal} />);
      expect(screen.queryByText("与资料不一致")).toBeNull();
    }
  }
});

test("统计使用各状态total并计入已弃用，忽略当前列表items和其它聚合数", async () => {
  const calls = api();
  render(<MemorySuggestionStats sessionSignal={signal} revision={0} />);
  expect(await screen.findByText("系统建议：已采纳 110、已忽略 21（含过期）")).toBeVisible();
  expect(calls).toHaveBeenCalledTimes(3);
  for (const [input] of calls.mock.calls) { expect(String(input)).toContain("origin=suggested"); expect(String(input)).not.toContain("notebook_id"); expect(String(input)).not.toContain("query="); }
});

test("采纳KG默认勾选、弃用旧记忆默认不勾，只确认一次且默认不写旧记忆", async () => {
  const calls = api();
  render(<Harness />);
  const dialog = await adoption();
  expect(within(dialog).getByRole("checkbox", { name: "同时整理进知识图谱" })).toBeChecked();
  expect(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" })).not.toBeChecked();
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认采纳" }));
  expect(await screen.findByText("已采纳", { selector: "span" })).toBeVisible();
  expect(mutations(calls, "confirm")).toHaveLength(1);
  expect(JSON.parse(String(mutations(calls, "confirm")[0][1]?.body))).toMatchObject({ title: "时钟门控", content_md: suggestion.content_md, tags: ["时序"], extract_kg: true });
  expect(mutations(calls, "deprecate")).toHaveLength(0);
});

test("编辑建议沿用标题正文标签控件，采纳使用用户快照", async () => {
  const calls = api();
  render(<Harness />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox", { name: "标题" }), { target: { value: "修改的约束" } });
  fireEvent.change(screen.getByRole("textbox", { name: "标签（逗号分隔）" }), { target: { value: "检查,项目" } });
  const dialog = await adoption();
  expect(within(dialog).getByText("修改的约束")).toBeVisible();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时整理进知识图谱" }));
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await waitFor(() => expect(mutations(calls, "confirm")).toHaveLength(1));
  expect(JSON.parse(String(mutations(calls, "confirm")[0][1]?.body))).toMatchObject({ title: "修改的约束", tags: ["检查", "项目"], extract_kg: false });
});

test("采纳已提交但响应未到时继续编辑，完成与已确认状态刷新保留后来草稿", async () => {
  const pending = deferred<Response>();
  const stored = { ...suggestion, tags: [...suggestion.tags] };
  const calls = api({ memory: stored, confirm: async (body) => {
    Object.assign(stored, body, { status: "confirmed" });
    return pending.promise;
  } });
  const actor = userEvent.setup();
  render(<Harness />);
  await actor.click(await screen.findByRole("button", { name: "编辑" }));
  const dialog = await adoption();
  await actor.click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await waitFor(() => expect(mutations(calls, "confirm")).toHaveLength(1));
  expect(stored.status).toBe("confirmed");
  await actor.click(within(dialog).getByRole("button", { name: "取消" }));
  fireEvent.change(screen.getByRole("textbox", { name: "标题" }), { target: { value: "采纳之后尚未保存的新修改" } });
  await act(async () => { pending.resolve(response(stored)); });
  expect(await screen.findByRole("textbox", { name: "标题" })).toHaveValue("采纳之后尚未保存的新修改");
  expect(await screen.findByRole("button", { name: "保存" })).toBeEnabled();
  expect(JSON.parse(String(mutations(calls, "confirm")[0][1]?.body)).title).toBe(suggestion.title);
});

test.each(["unchanged", "reopened", "another"])("迟到采纳完成只关闭原提交generation（%s）", async (editing) => {
  const pending = deferred<Response>();
  const stored = { ...suggestion, tags: [...suggestion.tags] };
  const otherMemory = { ...suggestion, id: "suggestion-other", title: "另一条候选" };
  const calls = api({ memory: stored, otherMemory, confirm: async (body) => {
    Object.assign(stored, body, { status: "confirmed" });
    return pending.promise;
  } });
  const actor = userEvent.setup();
  render(<Harness />);
  const first = (await screen.findByRole("heading", { name: suggestion.title })).closest("article")!;
  await actor.click(within(first).getByRole("button", { name: "编辑" }));
  await actor.click(within(first).getByRole("button", { name: "采纳" }));
  const dialog = await screen.findByRole("dialog", { name: "采纳记忆建议" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认采纳" })).toBeEnabled());
  await actor.click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await waitFor(() => expect(stored.status).toBe("confirmed"));
  await actor.click(within(dialog).getByRole("button", { name: "取消" }));
  if (editing === "reopened") {
    await actor.click(within(first).getByRole("button", { name: "取消" }));
    await actor.click(within(first).getByRole("button", { name: "编辑" }));
  } else if (editing === "another") {
    const other = screen.getByRole("heading", { name: otherMemory.title }).closest("article")!;
    await actor.click(within(other).getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByRole("textbox", { name: "标题" }), { target: { value: "另一条未保存的修改" } });
  }
  expect(screen.getAllByRole("button", { name: "采纳" }).every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
  await act(async () => { pending.resolve(response(stored)); });
  await screen.findByText("已采纳", { selector: "span" });
  if (editing === "unchanged") expect(screen.queryByRole("textbox", { name: "标题" })).toBeNull();
  else expect(screen.getByRole("textbox", { name: "标题" })).toHaveValue(editing === "reopened" ? suggestion.title : "另一条未保存的修改");
  expect(mutations(calls, "confirm")).toHaveLength(1);
});

test("保存草稿请求迟到时不关闭期间继续修改的同一条草稿", async () => {
  const pending = deferred<Response>();
  const stored = { ...suggestion, tags: [...suggestion.tags] };
  const calls = api({ memory: stored, save: async (body) => { Object.assign(stored, body); return pending.promise; } });
  const actor = userEvent.setup();
  render(<Harness />);
  await actor.click(await screen.findByRole("button", { name: "编辑" }));
  await actor.click(screen.getByRole("button", { name: "保存草稿" }));
  fireEvent.change(screen.getByRole("textbox", { name: "标题" }), { target: { value: "保存请求之后的新修改" } });
  await act(async () => { pending.resolve(response(stored)); });
  await screen.findByText("已保存修改", { selector: "p" });
  expect(screen.getByRole("textbox", { name: "标题" })).toHaveValue("保存请求之后的新修改");
  const saves = calls.mock.calls.filter(([, init]) => init?.method === "PATCH");
  expect(saves).toHaveLength(1);
  expect(JSON.parse(String(saves[0][1]?.body)).title).toBe(suggestion.title);
});

test("忽略调用reject并在卡片旁反馈，不需要先进入编辑", async () => {
  const calls = api();
  render(<Harness />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "忽略" }));
  expect(await screen.findByText("已忽略", { selector: "p" })).toBeVisible();
  expect(mutations(calls, "reject")).toHaveLength(1);
  expect(mutations(calls, "confirm")).toHaveLength(0);
});

test("忽略另一张卡片的建议不关闭当前卡片尚未保存的编辑", async () => {
  const calls = api({ memory: oldMemory, otherMemory: suggestion });
  const actor = userEvent.setup();
  render(<Harness />);
  const oldCard = (await screen.findByRole("heading", { name: "旧约束" })).closest("article")!;
  await actor.click(within(oldCard).getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox", { name: "标题" }), { target: { value: "另一张卡片尚未保存的修改" } });
  const otherCard = screen.getByRole("heading", { name: "时钟门控" }).closest("article")!;
  await actor.click(within(otherCard).getByRole("button", { name: "忽略" }));
  await screen.findByText("已忽略", { selector: "p" });
  expect(screen.getByRole("textbox", { name: "标题" })).toHaveValue("另一张卡片尚未保存的修改");
  expect(mutations(calls, "reject")).toHaveLength(1);
  expect(calls.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
});

test("勾选弃用仅在采纳成功后执行；失败明确保留采纳结果且重试不重发confirm", async () => {
  let attempts = 0;
  const calls = api({ deprecate: async () => ++attempts === 1 ? response({}, 503) : response({ ...oldMemory, status: "deprecated" }) });
  render(<Harness />);
  const dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认采纳" }));
  expect(await screen.findByText("已采纳，但旧记忆未能弃用")).toBeVisible();
  await userEvent.setup().click(screen.getByRole("button", { name: "重试弃用旧记忆" }));
  expect(await screen.findByText("旧记忆已弃用", { selector: "span" })).toBeVisible();
  expect(mutations(calls, "confirm")).toHaveLength(1);
  expect(mutations(calls, "deprecate")).toHaveLength(2);
  expect(calls.mock.calls.findIndex(([input]) => String(input).endsWith("/confirm"))).toBeLessThan(calls.mock.calls.findIndex(([input]) => String(input).endsWith("/deprecate")));
});

test("弃用已提交但响应丢失时重读确认成功，清除重试回执并刷新列表", async () => {
  let refined = oldMemory;
  const calls = api({
    loadRefines: async () => response(refined),
    deprecate: async () => {
      if (refined.status === "deprecated") return response({}, 409);
      refined = { ...refined, status: "deprecated" };
      throw new TypeError("retirement response lost");
    },
  });
  render(<Harness />);
  const dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await screen.findByText("已采纳，但旧记忆未能弃用");
  const listReads = () => calls.mock.calls.filter(([input]) => String(input).includes("/memories?")).length;
  const beforeRetry = listReads();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "重试弃用旧记忆" })); });
  expect(screen.getByText("旧记忆已弃用", { selector: "span" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "重试弃用旧记忆" })).toBeNull();
  expect(mutations(calls, "confirm")).toHaveLength(1);
  expect(mutations(calls, "deprecate")).toHaveLength(1);
  expect(listReads()).toBeGreaterThan(beforeRetry);
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGESTION_RESULT_MS); });
  expect(screen.queryByText("旧记忆已弃用", { selector: "span" })).toBeNull();
});

test("重试核对旧记忆期间换账号，迟到结果不会继续弃用或写入新账号回执", async () => {
  const reading = deferred<Response>();
  let reads = 0;
  const calls = api({
    loadRefines: () => ++reads === 1 ? Promise.resolve(response(oldMemory)) : reading.promise,
    deprecate: async () => response({}, 503),
  });
  const { rerender } = render(<Harness />);
  const dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await screen.findByText("已采纳，但旧记忆未能弃用");
  await userEvent.setup().click(screen.getByRole("button", { name: "重试弃用旧记忆" }));
  await waitFor(() => expect(reads).toBe(2));
  rerender(<Harness actor="actor-b" />);
  await act(async () => { reading.resolve(response(oldMemory)); });
  expect(mutations(calls, "deprecate")).toHaveLength(1);
  expect(screen.queryByText("旧记忆已弃用", { selector: "span" })).toBeNull();
  expect(screen.queryByRole("button", { name: "重试弃用旧记忆" })).toBeNull();
});

test.each([null, { ...oldMemory, status: "deprecated" } as MemoryRecord])("旧记忆不可读或已经弃用时单独采纳，不发送弃用请求", async (refines) => {
  const calls = api({ refines });
  render(<Harness />);
  const dialog = await adoption();
  expect(within(dialog).queryByRole("checkbox", { name: /同时弃用/ })).toBeNull();
  expect(within(dialog).getByText(refines ? "《旧约束》已经弃用，无需再次处理" : "旧记忆已不可用；仍可单独采纳这条建议")).toBeVisible();
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await waitFor(() => expect(mutations(calls, "confirm")).toHaveLength(1));
  expect(mutations(calls, "deprecate")).toHaveLength(0);
});

test.each(["network", "503"])("旧记忆临时读取失败可独立重试恢复且不阻止单独采纳（%s）", async (failure) => {
  let attempts = 0;
  const retry = deferred<Response>();
  const calls = api({ loadRefines: async () => {
    if (++attempts > 1) return retry.promise;
    if (failure === "network") throw new TypeError("network unavailable");
    return response({}, 503);
  } });
  render(<Harness />);
  const dialog = await adoption();
  expect(within(dialog).getByText(/旧记忆暂时未能读取/)).toBeVisible();
  expect(within(dialog).queryByText(/旧记忆已不可用/)).toBeNull();
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "重试读取旧记忆" }));
  expect(within(dialog).getByRole("button", { name: "正在读取旧记忆…" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "确认采纳" })).toBeEnabled();
  await act(async () => { retry.resolve(response(oldMemory)); });
  expect(await within(dialog).findByRole("checkbox", { name: "同时弃用《旧约束》" })).not.toBeChecked();
  expect(within(dialog).queryByRole("button", { name: "重试读取旧记忆" })).toBeNull();
  expect(mutations(calls, "confirm")).toHaveLength(0);
  expect(mutations(calls, "deprecate")).toHaveLength(0);
});

test("慢弃用前已读取采纳结果，弃用成功后再次刷新卡片筛选和统计", async () => {
  const retirement = deferred<Response>();
  let memory = suggestion;
  let refined = { ...oldMemory, suggested_signal: "repeated" as const };
  const countsRead: { confirmed: number; deprecated: number }[] = [];
  const calls = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/memory-old")) return response(refined);
    if (url.pathname.endsWith("/confirm")) { memory = { ...memory, status: "confirmed" }; return response(memory); }
    if (url.pathname.endsWith("/deprecate")) {
      const result = await retirement.promise;
      refined = { ...refined, status: "deprecated" };
      return result;
    }
    if (url.pathname.endsWith("/memories")) {
      const items = [memory, refined];
      const status = url.searchParams.get("status");
      const selected = !status ? items : items.filter((item) => item.status === status);
      if (url.pathname === "/api/memories" && url.searchParams.get("origin") === "suggested") {
        countsRead.push({ confirmed: items.filter((item) => item.status === "confirmed").length, deprecated: items.filter((item) => item.status === "deprecated").length });
      }
      return response(page(selected));
    }
    return response([]);
  });
  vi.stubGlobal("fetch", calls);
  render(<Harness />);
  const dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认采纳" }));
  await waitFor(() => expect(countsRead.some((count) => count.confirmed === 2 && count.deprecated === 0)).toBe(true));
  await screen.findByText("系统建议：已采纳 2、已忽略 0（含过期）");
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "取消" }));
  await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "状态" }), "confirmed");
  await screen.findByRole("heading", { name: "旧约束" });
  await act(async () => { retirement.resolve(response({ ...refined, status: "deprecated" })); });
  await waitFor(() => expect(screen.queryByRole("heading", { name: "旧约束" })).toBeNull());
  await waitFor(() => expect(countsRead.some((count) => count.confirmed === 1 && count.deprecated === 1)).toBe(true));
  expect(await screen.findByText("系统建议：已采纳 2、已忽略 0（含过期）")).toBeVisible();
  await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "状态" }), "deprecated");
  expect(await screen.findByRole("heading", { name: "旧约束" })).toBeVisible();
  expect(mutations(calls, "confirm")).toHaveLength(1);
  expect(mutations(calls, "deprecate")).toHaveLength(1);
});

test("请求未结束时连点/取消不解除写入锁", async () => {
  const pending = deferred<Response>();
  const calls = api({ confirm: () => pending.promise });
  render(<Harness />);
  const dialog = await adoption();
  const confirm = within(dialog).getByRole("button", { name: "确认采纳" });
  fireEvent.click(confirm); fireEvent.click(confirm);
  expect(within(dialog).getByRole("button", { name: "正在采纳…" })).toBeDisabled();
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog", { name: "采纳记忆建议" })).toBeNull();
  expect(screen.getByRole("button", { name: "采纳" })).toBeDisabled();
  await act(async () => { pending.resolve(response({ ...suggestion, status: "confirmed" })); });
  expect(mutations(calls, "confirm")).toHaveLength(1);
});

test("全局采纳读取该建议笔记本KG资格，读取失败阻止确认并可重试", async () => {
  let attempts = 0;
  api({ eligibility: async () => ++attempts === 1 ? response({}, 503) : response(page([], 0, true)) });
  render(<Harness notebookId={null} />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "采纳" }));
  const dialog = screen.getByRole("dialog", { name: "采纳记忆建议" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "重试" })).toBeEnabled());
  expect(within(dialog).getByRole("button", { name: "确认采纳" })).toBeDisabled();
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "重试" }));
  await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: "同时整理进知识图谱" })).toBeChecked());
});

test("晚到公告覆盖采纳弹窗时仅顶层可交互并归还原复选框焦点", async () => {
  api();
  const { rerender } = render(<Harness />);
  const dialog = await adoption();
  const checkbox = within(dialog).getByRole("checkbox", { name: "同时整理进知识图谱" });
  checkbox.focus();
  rerender(<Harness announcement />);
  await waitFor(() => expect(dialog).toHaveAttribute("inert"));
  expect(dialog).toHaveAttribute("aria-hidden", "true");
  expect(dialog).toHaveAttribute("aria-modal", "false");
  await userEvent.setup().click(screen.getByRole("button", { name: /知道了|关闭/ }));
  await waitFor(() => expect(dialog).not.toHaveAttribute("inert"));
  await waitFor(() => expect(checkbox).toHaveFocus());
});

test("A-B-A账号替换隔离迟到确认结果及新操作锁", async () => {
  const pending = deferred<Response>();
  let count = 0;
  const calls = api({ confirm: () => ++count === 1 ? pending.promise : Promise.resolve(response({ ...suggestion, status: "confirmed" })) });
  const { rerender } = render(<Harness />);
  const dialog = await adoption();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认采纳" }));
  rerender(<Harness actor="actor-b" />);
  rerender(<Harness actor="actor-a" />);
  await act(async () => { pending.resolve(response({ ...suggestion, status: "confirmed" })); });
  expect(screen.queryByText("已采纳", { selector: "span" })).toBeNull();
  expect(mutations(calls, "confirm")).toHaveLength(1);
});

test("采纳取消恢复原控件焦点，重新打开重置两项选择", async () => {
  api();
  render(<Harness />);
  const opener = await screen.findByRole("button", { name: "采纳" });
  let dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时整理进知识图谱" }));
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  await userEvent.setup().click(within(dialog).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(opener).toHaveFocus());
  dialog = await adoption();
  expect(within(dialog).getByRole("checkbox", { name: "同时整理进知识图谱" })).toBeChecked();
  expect(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" })).not.toBeChecked();
});

test.each([false, true])("采纳结果提示使用自身timer清除（失败：%s）", async (failed) => {
  api({ confirm: async () => response(failed ? {} : { ...suggestion, status: "confirmed" }, failed ? 503 : 200) });
  render(<Harness />);
  const dialog = await adoption();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确认采纳" })); });
  expect(screen.getAllByRole(failed ? "alert" : "status").some((element) => element.textContent)).toBe(true);
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGESTION_RESULT_MS); });
  expect(screen.queryByText("已采纳", { selector: "span" })).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("弃用失败的短提示消失后仍保留准确的单独重试入口", async () => {
  api({ deprecate: async () => response({}, 503) });
  render(<Harness />);
  const dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确认采纳" })); });
  expect(screen.getByText("已采纳，但旧记忆未能弃用")).toBeVisible();
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGESTION_RESULT_MS); });
  expect(screen.queryByText("已采纳，但旧记忆未能弃用")).toBeNull();
  expect(screen.getByRole("button", { name: "重试弃用旧记忆" })).toBeVisible();
  expect(screen.getByText(/旧记忆《旧约束》尚未弃用/)).toBeVisible();
});

test("采纳后的提示过期与单独弃用重试不关闭新打开的编辑草稿", async () => {
  let retireAttempts = 0;
  const calls = api({ deprecate: async () => ++retireAttempts === 1 ? response({}, 503) : response({ ...oldMemory, status: "deprecated" }) });
  render(<Harness />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "编辑" }));
  const dialog = await adoption();
  await userEvent.setup().click(within(dialog).getByRole("checkbox", { name: "同时弃用《旧约束》" }));
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确认采纳" })); });
  expect(screen.getByText("已采纳，但旧记忆未能弃用")).toBeVisible();
  expect(screen.queryByRole("textbox", { name: "标题" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox", { name: "标题" }), { target: { value: "下一次尚未保存的修改" } });
  await act(async () => { vi.advanceTimersByTime(MEMORY_SUGGESTION_RESULT_MS); });
  expect(screen.getByRole("textbox", { name: "标题" })).toHaveValue("下一次尚未保存的修改");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "重试弃用旧记忆" })); });
  expect(screen.getByRole("textbox", { name: "标题" })).toHaveValue("下一次尚未保存的修改");
  expect(mutations(calls, "confirm")).toHaveLength(1);
  expect(mutations(calls, "deprecate")).toHaveLength(2);
});

test("统计坏总数显示未知并允许重试，不伪造零或后台轮询", async () => {
  let broken = true;
  const calls = vi.fn(async () => response(page([], broken ? -1 : 5)));
  vi.stubGlobal("fetch", calls);
  render(<MemorySuggestionStats sessionSignal={signal} revision={0} />);
  await screen.findByText(/系统建议数量暂不可用/);
  expect(screen.queryByText(/已采纳 0/)).toBeNull();
  expect(calls).toHaveBeenCalledTimes(3);
  broken = false;
  await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText("系统建议：已采纳 10、已忽略 5（含过期）")).toBeVisible();
  expect(calls).toHaveBeenCalledTimes(6);
});

test("同一账号换会话会撤销旧采纳lease且可以重新打开", async () => {
  api();
  const { rerender } = render(<Harness />);
  await adoption();
  const replacement = new AbortController().signal;
  rerender(<Harness sessionSignal={replacement} />);
  expect(screen.queryByRole("dialog", { name: "采纳记忆建议" })).toBeNull();
  expect(await adoption()).toBeVisible();
});
