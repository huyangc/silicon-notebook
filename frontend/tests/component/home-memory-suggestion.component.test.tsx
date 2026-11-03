import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import Home from "../../app/page";
import { setToken } from "../../app/auth";
import { memoryHash, notebookHash } from "../../app/memory-model";

const scrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  if (scrollDescriptor) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollDescriptor);
  else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
});

function serveWorkspace({ suggestionReady = true, memoryMode = "suggest", startInAsk = false, twoAnswers = false } = {}) {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  let deleted = false;
  let mode = memoryMode;
  let saved = false;
  const notebook = { id: "nb-memory", name: "记忆回归库", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "今天", access: "owner", ask_available: true, local_evidence_available: true };
  const user = () => ({ id: "user-memory", username: "a20261101", role: "user", ui_mode: "advanced", search_profile: null, memory_mode: mode });
  const memory = { id: "suggestion-1", notebook_id: notebook.id, created_by: "user-memory", origin: "ask_answer", suggested_signal: "repeated", status: "candidate", promotion_state: "none", title: "验收约束", content_md: "保留接口约束", tags: [], embedding_status: "none", embedding_error: "", created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z", provenance: { conversation_id: "conversation-memory", answer_id: "answer-memory", suggestion: { cluster_size: 3 } } };
  const response = { answer_id: "answer-memory", conversation_id: "conversation-memory", conclusion: "回归回答", answer: "回归回答", grounded: true, anchors: [], related_knowledge: [], citations: [], llm_mode: "test", mode: "chunk" };
  const turns = [
    ...(twoAnswers ? [{ answer_id: "answer-manual", question: "先保存的问题", response: { ...response, answer_id: "answer-manual", conclusion: "手动保存的回答", answer: "手动保存的回答" }, asked_at: memory.created_at, created_at: memory.created_at }] : []),
    { answer_id: response.answer_id, question: "回归问题", response, asked_at: memory.created_at, created_at: memory.created_at },
  ];
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), window.location.origin);
    const path = url.pathname;
    calls.push(path);
    if (path === "/api/ready") return Response.json({ ready: true });
    if (path === "/api/auth/capabilities") return Response.json({ sso_login: false, local_login: true, local_registration: true });
    if (path === "/api/me") return Response.json(user());
    if (path === "/api/me/memory-mode") {
      expect(JSON.parse(String(init?.body))).toEqual({ memory_mode: "manual", delete_unadopted: true });
      mode = "manual";
      deleted = true;
      return Response.json({ user: user(), deleted_suggestion_count: 1 });
    }
    if (path === "/api/memories/bulk-delete") {
      expect(JSON.parse(String(init?.body))).toEqual({ memory_ids: [memory.id] });
      deleted = true;
      return Response.json({ deleted_count: 1 });
    }
    if (path === "/api/health") return Response.json({ status: "ok", llm_configured: false });
    if (path === "/api/system/config") return Response.json({ source_upload_max_bytes: 1048576, source_upload_max_files_per_batch: 5, auto_memory_suggest_enabled: true, auto_memory_suggest_model_ready: true });
    if (path === "/api/notebooks") return Response.json([notebook]);
    if (path === `/api/notebooks/${notebook.id}`) return Response.json(notebook);
    if (path.endsWith("/pending-actions/stream")) return new Promise<Response>(() => undefined);
    if (path.endsWith("/pending-actions")) return Response.json({ count: 0, items: [] });
    if (path.endsWith("/notebook-deletions")) return Response.json({ items: [] });
    if (path.endsWith("/sources")) return Response.json({ items: [], total: 0, total_count: 0, offset: 0, limit: 20 });
    if (path.endsWith("/answer-memory-links")) return Response.json({ links: saved ? { "answer-manual": "saved-manual" } : {}, states: deleted || !suggestionReady ? {} : { "answer-memory": { memory_id: memory.id, status: "candidate", suggested: true } } });
    if (path === "/api/answers/answer-manual/memory-preview/stream") return new Response(JSON.stringify({ event: "final", stage: "memory-preview", result: { title: "手动记忆", content_md: "已确认的手动保存内容", tags: [], provenance_summary: {}, kg_extract_eligible: false } }) + "\n");
    if (path.endsWith("/memories/from-answer")) {
      expect(JSON.parse(String(init?.body)).answer_id).toBe("answer-manual");
      saved = true;
      return Response.json({ ...memory, id: "saved-manual", status: "confirmed", suggested_signal: null });
    }
    if (path.endsWith("/conversations")) return Response.json([{ id: "conversation-memory", title: "回归会话", updated_at: memory.created_at, turn_count: turns.length, used_reasoning: false }]);
    if (path === "/api/conversations/conversation-memory") return Response.json({ id: "conversation-memory", notebook_id: notebook.id, title: "回归会话", updated_at: memory.created_at, turn_count: turns.length, turns });
    if (path.endsWith("/memories")) {
      const status = url.searchParams.get("status");
      const items = deleted || !suggestionReady || status && status !== "candidate" ? [] : [memory];
      return Response.json({ items, total_count: items.length, owner_total_count: items.length, owner_pending_count: items.length, notebook_options: [], offset: 0, limit: 20, kg_extract_eligible: false });
    }
    if (path.endsWith("/ask-modes")) return Response.json([]);
    return Response.json({ detail: "not supplied by this fixture" }, { status: 503 });
  }));
  setToken("memory-test-session");
  window.history.replaceState(null, "", startInAsk ? notebookHash(notebook.id) : memoryHash(notebook.id));
  return { calls, notebookId: notebook.id, publishSuggestion: () => { suggestionReady = true; } };
}

async function openHistoricalAnswer() {
  const actor = userEvent.setup();
  await actor.click(await screen.findByRole("button", { name: /历史/ }));
  await actor.click(await screen.findByRole("button", { name: /回归会话/ }));
  await screen.findByText("回归回答");
}

test("a suggestion generated after answer delivery becomes visible without transcript changes", async () => {
  const server = serveWorkspace({ suggestionReady: false, startInAsk: true });
  render(<Home />);
  await openHistoricalAnswer();
  await waitFor(() => expect(server.calls.some((path) => path.endsWith("/answer-memory-links"))).toBe(true));
  expect(screen.queryByText("有建议")).toBeNull();
  server.publishSuggestion();
  expect(await screen.findByText("有建议")).toBeVisible();
});

async function saveFirstAnswer() {
  const actor = userEvent.setup();
  await actor.click(screen.getAllByRole("button", { name: "保存到记忆" })[0]);
  const dialog = await screen.findByRole("dialog", { name: "保存回答到记忆" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认保存" })).toBeEnabled());
  await actor.click(within(dialog).getByRole("button", { name: "确认保存" }));
  await screen.findByRole("button", { name: "已保存到记忆" });
}

test.each(["follow-up", "visible"])("manually saving answer A preserves %s observation for a later suggestion on answer B", async (trigger) => {
  const server = serveWorkspace({ suggestionReady: false, startInAsk: true, twoAnswers: true });
  render(<Home />);
  await openHistoricalAnswer();
  await waitFor(() => expect(server.calls.some((path) => path.endsWith("/answer-memory-links"))).toBe(true));
  const beforeSave = server.calls.filter((path) => path.endsWith("/answer-memory-links")).length;
  await saveFirstAnswer();
  await waitFor(() => expect(server.calls.filter((path) => path.endsWith("/answer-memory-links")).length).toBeGreaterThan(beforeSave));
  if (trigger === "visible") vi.useFakeTimers();
  server.publishSuggestion();
  if (trigger === "visible") {
    await act(async () => { fireEvent(document, new Event("visibilitychange")); });
    expect(screen.getByText("有建议")).toBeVisible();
  } else expect(await screen.findByText("有建议")).toBeVisible();
  expect(screen.getByRole("button", { name: "已保存到记忆" })).toBeDisabled();
  expect(screen.getAllByRole("button", { name: "保存到记忆" })).toHaveLength(1);
});

test("manual saving keeps the definitive saved flag without starting suggestion observation for manual users", async () => {
  const server = serveWorkspace({ suggestionReady: false, memoryMode: "manual", startInAsk: true, twoAnswers: true });
  render(<Home />);
  await openHistoricalAnswer();
  const reads = () => server.calls.filter((path) => path.endsWith("/answer-memory-links")).length;
  await waitFor(() => expect(reads()).toBeGreaterThan(0));
  const before = reads();
  await saveFirstAnswer();
  server.publishSuggestion();
  vi.useFakeTimers();
  await act(async () => {
    fireEvent(document, new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(reads()).toBe(before);
  expect(screen.getByRole("button", { name: "已保存到记忆" })).toBeDisabled();
  expect(screen.queryByText("有建议")).toBeNull();
});

test.each(["Ask", "visible"])("suggestion follow-up is bounded; %s invalidates an exhausted lookup", async (trigger) => {
  const server = serveWorkspace({ suggestionReady: false, startInAsk: true });
  render(<Home />);
  await openHistoricalAnswer();
  const reads = () => server.calls.filter((path) => path.endsWith("/answer-memory-links")).length;
  await waitFor(() => expect(reads()).toBeGreaterThan(0));
  vi.useFakeTimers();
  fireEvent.click(screen.getByRole("button", { name: "记忆" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(screen.getByRole("button", { name: "问答" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const beforeWindow = reads();
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  const afterWindow = reads();
  expect(afterWindow - beforeWindow).toBe(3);
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(reads()).toBe(afterWindow);
  server.publishSuggestion();
  if (trigger === "Ask") {
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    fireEvent.click(screen.getByRole("button", { name: "问答" }));
  } else fireEvent(document, new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("有建议")).toBeVisible();
});

test("manual users do not add follow-up or visibility-triggered answer-memory reads", async () => {
  const server = serveWorkspace({ suggestionReady: false, memoryMode: "manual", startInAsk: true });
  render(<Home />);
  await openHistoricalAnswer();
  const reads = () => server.calls.filter((path) => path.endsWith("/answer-memory-links")).length;
  await waitFor(() => expect(reads()).toBeGreaterThan(0));
  const before = reads();
  vi.useFakeTimers();
  await act(async () => { await vi.advanceTimersByTimeAsync(120000); });
  fireEvent(document, new Event("visibilitychange"));
  fireEvent.click(screen.getByRole("button", { name: "记忆" }));
  fireEvent.click(screen.getByRole("button", { name: "问答" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(reads()).toBe(before);
});

test("opening an original answer within the current notebook updates the URL to Ask", async () => {
  const server = serveWorkspace();
  render(<Home />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "查看原回答" }));
  await screen.findByText("有建议");
  expect(window.location.hash).toBe(notebookHash(server.notebookId));
});

test("closing with deletion reconciles existing answer suggestion badges in the real workspace", async () => {
  const server = serveWorkspace();
  const actor = userEvent.setup();
  render(<Home />);
  await actor.click(await screen.findByRole("button", { name: "查看原回答" }));
  await screen.findByText("有建议");
  const before = server.calls.filter((path) => path.endsWith("/answer-memory-links")).length;
  await actor.click(screen.getByRole("button", { name: "记忆" }));
  await actor.click(await screen.findByRole("button", { name: "关闭" }));
  const dialog = await screen.findByRole("dialog", { name: "关闭记忆建议" });
  await actor.click(await within(dialog).findByRole("checkbox", { name: /同时删除/ }));
  await actor.click(within(dialog).getByRole("button", { name: "确认关闭" }));
  await waitFor(() => expect(server.calls.filter((path) => path.endsWith("/answer-memory-links")).length).toBeGreaterThan(before));
  await actor.click(screen.getByRole("button", { name: "问答" }));
  expect(screen.queryByText("有建议")).toBeNull();
});

test("bulk deletion reconciles existing answer suggestion badges in the real workspace", async () => {
  const server = serveWorkspace();
  const actor = userEvent.setup();
  render(<Home />);
  await actor.click(await screen.findByRole("button", { name: "查看原回答" }));
  await screen.findByText("有建议");
  const before = server.calls.filter((path) => path.endsWith("/answer-memory-links")).length;
  await actor.click(screen.getByRole("button", { name: "记忆" }));
  await actor.click(await screen.findByRole("button", { name: "批量选择" }));
  await actor.click(screen.getByRole("checkbox", { name: "选择「验收约束」" }));
  await actor.click(screen.getByRole("button", { name: "删除选中（1）" }));
  await actor.click(within(screen.getByRole("dialog", { name: "确认删除" })).getByRole("button", { name: "删除" }));
  await waitFor(() => expect(server.calls.filter((path) => path.endsWith("/answer-memory-links")).length).toBeGreaterThan(before));
  await actor.click(screen.getByRole("button", { name: "问答" }));
  expect(screen.queryByText("有建议")).toBeNull();
});
