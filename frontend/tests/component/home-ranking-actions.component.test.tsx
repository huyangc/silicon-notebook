import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import Home from "../../app/page";
import { setToken } from "../../app/auth";
import { notebookHash } from "../../app/memory-model";
import type { useSourceLibrary } from "../../app/use-source-library";
import { humanizeHttpError } from "../../app/errors";
import { KG_BUSY_NOTICE_HOLD_MS } from "../../app/use-kg-graph";
import { REBUILD_POLL_MAX_ATTEMPTS } from "../../features/kg-maintenance/kg-rebuild-status";

const observed = vi.hoisted(() => ({
  selectAll: vi.fn(),
  source: null as ReturnType<typeof useSourceLibrary> | null,
}));

// Wrap the real owner command: the scope state and submitted HTTP payload still
// come from the production hooks, rather than a copied submission implementation.
vi.mock("../../app/use-source-library.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/use-source-library")>();
  return {
    ...actual,
    useSourceLibrary: (...args: Parameters<typeof useSourceLibrary>) => {
      const owner = actual.useSourceLibrary(...args);
      observed.source = owner;
      return {
        ...owner,
        selectAllSources: () => { observed.selectAll(); owner.selectAllSources(); },
      };
    },
  };
});

const scrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  observed.source = null;
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  if (scrollDescriptor) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollDescriptor);
  else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
});

const QUESTION = "哪些概念被提及得最多？";
const HTTP_FAILURE = humanizeHttpError(503);
const FALLBACK = "没能按提及次数排行：这次更新没有产出排行数据。下面的回答只基于检索到的部分内容，不是统计结果。";

function serveWorkspace({
  reason = "not_computed", probe = false, submittedVia = "web", stamp = "current", alreadyRunning = false,
  maintenanceUnreadable = false, twoAnswers = false, holdRebuild = false, holdProbe = false, holdAnalysis = false,
  probeError, access = "owner", mirror = false, uiMode = "advanced",
}: {
  reason?: "not_computed" | "scoped_large_notebook";
  probe?: boolean | null;
  submittedVia?: string;
  stamp?: "current" | "stale" | "missing";
  alreadyRunning?: boolean;
  maintenanceUnreadable?: boolean;
  twoAnswers?: boolean;
  holdRebuild?: boolean;
  holdProbe?: boolean;
  holdAnalysis?: boolean;
  probeError?: number;
  access?: string;
  mirror?: boolean;
  uiMode?: string;
} = {}) {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  vi.stubGlobal("scrollTo", vi.fn());
  let status = alreadyRunning ? "running" : "idle";
  let rebuildRejection: number | null = null;
  let releaseRebuild: (() => void) | undefined;
  let releaseProbe: (() => void) | undefined;
  let releaseAnalysis: (() => void) | undefined;
  const calls: { path: string; query: string; method: string; body: Record<string, unknown> | null }[] = [];
  const notebook = {
    id: "nb-ranking", name: "排行回归库", purpose: "", primary_domain: "", status: "ready",
    counts: { sources: 2 }, created_label: "今天", access, sync_origin: mirror ? "remote" : "", ask_available: true,
    local_evidence_available: true,
    base_notebooks: [
      { id: "base-included", name: "保留参考库", tier: "base" },
      { id: "base-excluded", name: "排除参考库", tier: "base" },
    ],
  };
  const sources = ["source-one", "source-two"].map((id) => ({
    id, notebook_id: notebook.id, title: id, display_title: id, type: "text", status: "ready",
    parse_status: "parsed", summary: "", element_count: 1, file_name: "", file_size: 0,
    created_at: "2026-11-01T00:00:00Z", created_label: "今天",
  }));
  const line = reason === "not_computed"
    ? "没能按提及次数排行：本笔记本还没有排行数据。下面的回答只基于检索到的部分内容，不是统计结果。"
    : "没能按提及次数排行：本笔记本内容较多，只勾选部分来源时不支持这类统计。下面的回答只基于检索到的部分内容，不是统计结果。";
  const response = {
    answer_id: "answer-ranking", conversation_id: "conversation-ranking", conclusion: "排行回归回答",
    answer: `> ${line}\n\n排行回归回答`, ranking_notice: line,
    ranking_failures: [{ reason, notebook_ids: [notebook.id], line, ...(reason === "not_computed" ? { fallback_line: FALLBACK } : {}) }],
    grounded: true, anchors: [], related_knowledge: [], citations: [], llm_mode: "test",
    mode: "chunk", retrieval_effort: "deep",
  };
  const summary = {
    id: response.conversation_id, title: "排行回归会话", updated_at: sources[0].created_at,
    turn_count: 1, used_reasoning: false, submitted_via: submittedVia,
  };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), window.location.origin);
    const path = url.pathname;
    const method = init?.method ?? "GET";
    calls.push({ path, query: url.search, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (path === "/api/ready") return Response.json({ ready: true });
    if (path === "/api/auth/capabilities") return Response.json({ sso_login: false, local_login: true, local_registration: true });
    if (path === "/api/me") return Response.json({ id: "user-ranking", username: "ranking", role: "user", ui_mode: uiMode, search_profile: null, memory_mode: "manual" });
    if (path === "/api/health") return Response.json({ status: "ok", llm_configured: false });
    if (path === "/api/system/config") return Response.json({ source_upload_max_bytes: 1048576, source_upload_max_files_per_batch: 5 });
    if (path === "/api/notebooks") return Response.json([notebook]);
    if (path === `/api/notebooks/${notebook.id}`) return Response.json(notebook);
    if (path.endsWith("/pending-actions/stream")) return new Promise<Response>(() => undefined);
    if (path.endsWith("/pending-actions")) return Response.json({ count: 0, items: [] });
    if (path.endsWith("/notebook-deletions")) return Response.json({ items: [] });
    if (path.endsWith("/sources")) return Response.json({ items: sources, total: 2, total_count: 2, offset: 0, limit: 20 });
    if (path.endsWith("/answer-memory-links")) return Response.json({ links: {}, states: {} });
    if (path.endsWith("/conversations")) return Response.json([summary]);
    if (path === "/api/conversations/conversation-ranking") return Response.json({
      ...summary, notebook_id: notebook.id,
      turns: [
        { answer_id: response.answer_id, question: QUESTION, response, asked_at: summary.updated_at, created_at: summary.updated_at },
        ...(twoAnswers ? [{ answer_id: "answer-other", question: QUESTION, response: { ...response, answer_id: "answer-other" }, asked_at: summary.updated_at, created_at: summary.updated_at }] : []),
      ],
    });
    if (path.endsWith("/ask/stream")) return new Response(JSON.stringify({ event: "final", response: { ...response, answer_id: "reasked-answer", answer: "再次提问完成", conclusion: "再次提问完成", ranking_notice: "", ranking_failures: [] } }) + "\n");
    if (path.endsWith("/ask-modes")) return Response.json([]);
    if (path.endsWith("/unified-kg/rebuild/model-calls")) {
      if (holdProbe) await new Promise<void>((resolve) => { releaseProbe = resolve; });
      return probeError ? Response.json({}, { status: probeError }) : Response.json({ model_calls_possible: probe });
    }
    if (path.endsWith("/unified-kg/rebuild") && method === "POST") {
      if (holdRebuild) await new Promise<void>((resolve) => { releaseRebuild = resolve; });
      if (rebuildRejection) return Response.json({ detail: "request rejected" }, { status: rebuildRejection });
      status = "running";
      return Response.json({ job_id: "rank-rebuild", notebook_id: notebook.id, status: "running" });
    }
    if (path.endsWith("/unified-kg/rebuild/status")) {
      if (maintenanceUnreadable && rebuildRejection) return Response.json({}, { status: 503 });
      return Response.json({ job_id: "rank-rebuild", notebook_id: notebook.id, status, running: status === "running", clusters: 1 });
    }
    if (path.endsWith("/relink/status") || path.endsWith("/kg/delete/status") || path.endsWith("/merges/review-job")) return Response.json({ status: "idle", running: false });
    if (path.endsWith("/unified-kg")) return Response.json({ nodes: [], edges: [], total_nodes: 0, total_edges: 0 });
    if (path.endsWith("/unified-kg/pending-merges")) return Response.json([]);
    if (path.endsWith("/unified-kg/status")) return Response.json({});
    if (path.endsWith("/kg-analysis")) {
      if (holdAnalysis) await new Promise<void>((resolve) => { releaseAnalysis = resolve; });
      return Response.json({
        concept_ranks_built_at_seq: stamp === "missing" ? null : { kg_seq: stamp === "stale" ? 4 : 5, cluster_seq: 3 },
        state: { kg_mutation_seq: 5, cluster_mutation_seq: 3 },
      });
    }
    return Response.json({ detail: "not supplied by this fixture" }, { status: 503 });
  }));
  setToken("ranking-test-session");
  window.history.replaceState(null, "", notebookHash(notebook.id));
  return {
    calls, line,
    finish: () => { status = "succeeded"; },
    fail: (outcome: "failed" | "idle" = "failed") => { status = outcome; },
    rejectRebuild: (code: number) => { rebuildRejection = code; },
    addOtherAnswer: () => { twoAnswers = true; },
    setProbe: (value: boolean | null) => { probe = value; },
    releaseRebuild: () => { holdRebuild = false; releaseRebuild?.(); },
    releaseProbe: () => { holdProbe = false; releaseProbe?.(); },
    releaseAnalysis: () => { holdAnalysis = false; releaseAnalysis?.(); },
    count: (suffix: string) => calls.filter((call) => call.path.endsWith(suffix)).length,
  };
}

async function openHistoricalAnswer() {
  const actor = userEvent.setup();
  await actor.click(await screen.findByRole("button", { name: /历史/ }));
  await actor.click(await screen.findByRole("button", { name: /排行回归会话/ }));
  await screen.findAllByText("排行回归回答");
}

test.each(["partial", "empty"])("selecting all from %s scope calls the real source owner and preserves question, mode, effort and reference-library scope", async (selection) => {
  const server = serveWorkspace({ reason: "scoped_large_notebook" });
  render(<Home />);
  await openHistoricalAnswer();
  await act(async () => {
    if (selection === "empty") observed.source!.clearSourceSelection();
    else observed.source!.toggleSource("source-two");
  });
  await userEvent.setup().click(screen.getByRole("checkbox", { name: "检索参考库：排除参考库" }));
  if (selection === "empty") {
    await userEvent.setup().click(screen.getByRole("checkbox", { name: "检索参考库：保留参考库" }));
    expect(observed.source!.sourceScopeSelection.allSelected).toBe(false);
  } else expect(observed.source!.sourceScopeSelection.ids.has("source-two")).toBe(true);
  await userEvent.setup().click(await screen.findByRole("button", { name: "全选本库来源并重新提问" }));
  await waitFor(() => expect(server.count("/ask/stream")).toBe(1));
  expect(observed.selectAll).toHaveBeenCalledTimes(1);
  expect(observed.source!.sourceScopeSelection.allSelected).toBe(true);
  expect(observed.source!.sourceScopeSelection.ids.size).toBe(0);
  expect(server.calls.find((call) => call.path.endsWith("/ask/stream"))!.body).toMatchObject({
    question: QUESTION, mode: "chunk", retrieval_effort: "deep",
    source_scope: { mode: "exclude", source_ids: [] },
    base_scope: selection === "empty"
      ? { mode: "include", notebook_ids: [] }
      : { mode: "exclude", notebook_ids: ["base-excluded"] },
  });
  expect(screen.getByRole("checkbox", { name: "检索参考库：排除参考库" })).not.toBeChecked();
  const answer = await screen.findByText("再次提问完成");
  await waitFor(() => expect(answer.closest(".chat-turn")).toHaveFocus());
});

test("MCP-origin historical answers never expose select-all-and-reask", async () => {
  const server = serveWorkspace({ reason: "scoped_large_notebook", submittedVia: "mcp" });
  render(<Home />);
  await openHistoricalAnswer();
  await act(async () => { observed.source!.toggleSource("source-two"); });
  expect(screen.getByText(server.line)).toBeVisible();
  expect(screen.queryByRole("button", { name: "全选本库来源并重新提问" })).toBeNull();
});

test.each([true, null])("probe %s requires confirmation; cancelling restores the action and each click probes exactly once", async (probe) => {
  const server = serveWorkspace({ probe });
  const actor = userEvent.setup();
  render(<Home />);
  await openHistoricalAnswer();
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(0);
  for (const clicks of [1, 2]) {
    await actor.click(screen.getByRole("button", { name: "更新分析" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "生成或更新图谱分析" })).toBeVisible();
    expect(within(dialog).getByText(/这一步可能调用已配置的模型/)).toBeVisible();
    expect(server.count("/unified-kg/rebuild/model-calls")).toBe(clicks);
    expect(server.count("/unified-kg/rebuild")).toBe(0);
    await actor.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "更新分析" })).toBeEnabled());
  }
});

test.each(["current", "stale", "missing"] as const)("false probe starts directly; real status polling settles %s ranking data without probing again", async (stamp) => {
  const server = serveWorkspace({ stamp });
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(1);
  expect(server.count("/unified-kg/rebuild")).toBe(1);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByText("正在更新分析…")).toBeVisible();
  const polls = server.count("/unified-kg/rebuild/status");
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(server.count("/unified-kg/rebuild/status")).toBeGreaterThan(polls);
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(1);
  server.finish();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(server.count("/kg-analysis")).toBe(1);
  expect(Object.fromEntries(new URLSearchParams(server.calls.find((call) => call.path.endsWith("/kg-analysis"))!.query)))
    .toEqual({ boards: "1", top_members: "1", edges: "1" });
  expect(screen.queryByText("正在更新分析…")).toBeNull();
  if (stamp !== "missing") expect(screen.getByRole("button", { name: "重新提问" })).toBeEnabled();
  else {
    expect(screen.getByText(FALLBACK)).toBeVisible();
    expect(screen.queryByRole("button", { name: "重新提问" })).toBeNull();
  }
});

test("an adopted running rebuild shows progress on historical missing-ranking answers without a model-call probe", async () => {
  const server = serveWorkspace({ alreadyRunning: true });
  render(<Home />);
  await openHistoricalAnswer();
  expect(screen.getByText("正在更新分析…")).toBeVisible();
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(0);
});

test.each([409, 503, 403])("rebuild rejected with %s after confirmation reports locally without entering running", async (code) => {
  const server = serveWorkspace({ probe: true });
  const actor = userEvent.setup();
  render(<Home />);
  await openHistoricalAnswer();
  await actor.click(screen.getByRole("button", { name: "更新分析" }));
  const dialog = await screen.findByRole("dialog");
  // Permission/maintenance state changes after the confirmation was opened.
  server.rejectRebuild(code);
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确定" })); });
  const note = screen.getByText(server.line).closest('[role="note"]') as HTMLElement;
  expect(within(note).queryByText("正在更新分析…")).toBeNull();
  expect(server.count("/kg-analysis")).toBe(0);
  if (code === 403) {
    expect(within(note).queryByRole("button", { name: "更新分析" })).toBeNull();
  } else {
    const retry = within(note).getByRole("button", { name: "更新分析" });
    if (code === 409) {
      expect(retry).toBeDisabled();
      expect(within(note).getByText("图谱正在处理其他任务")).toBeVisible();
    } else {
      expect(retry).toBeEnabled();
      expect(within(note).queryByText(HTTP_FAILURE)).toBeNull();
      expect(screen.getAllByText(HTTP_FAILURE)).toHaveLength(1);
      expect(retry).toHaveFocus();
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(KG_BUSY_NOTICE_HOLD_MS); });
    expect(retry).toBeEnabled();
    expect(within(note).queryByText(HTTP_FAILURE)).toBeNull();
    expect(within(note).queryByText("图谱正在处理其他任务")).toBeNull();
  }
});

test("unconfirmed maintenance adoption keeps the rejection visible rather than claiming analysis is running", async () => {
  const server = serveWorkspace({ maintenanceUnreadable: true, twoAnswers: true });
  render(<Home />);
  await openHistoricalAnswer();
  server.rejectRebuild(409);
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "更新分析" })[0]); });
  await act(async () => { await vi.advanceTimersByTimeAsync(KG_BUSY_NOTICE_HOLD_MS); });
  expect(screen.queryByText("正在更新分析…")).toBeNull();
  for (const retry of screen.getAllByRole("button", { name: "更新分析" })) {
    expect(retry).toBeDisabled();
    expect(within(retry.closest('[role="note"]') as HTMLElement).getByText("图谱正在处理其他任务")).toBeVisible();
  }
  expect(server.count("/kg-analysis")).toBe(0);
});

test.each([true, false])("confirmation waits for the rebuild POST before reporting acceptance=%s on any answer", async (accepted) => {
  const server = serveWorkspace({ probe: true, twoAnswers: true, holdRebuild: true });
  const actor = userEvent.setup();
  render(<Home />);
  await openHistoricalAnswer();
  await actor.click(screen.getAllByRole("button", { name: "更新分析" })[0]);
  await actor.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "确定" }));
  await waitFor(() => expect(server.count("/unified-kg/rebuild")).toBe(1));
  expect(screen.queryByText("正在更新分析…")).toBeNull();
  for (const button of screen.getAllByRole("button", { name: "更新分析" })) expect(button).toBeDisabled();
  if (!accepted) server.rejectRebuild(503);
  await act(async () => { server.releaseRebuild(); });
  if (accepted) {
    expect(screen.getAllByText("正在更新分析…")).toHaveLength(2);
  } else {
    expect(screen.queryByText("正在更新分析…")).toBeNull();
    const buttons = screen.getAllByRole("button", { name: "更新分析" });
    for (const button of buttons) expect(button).toBeEnabled();
    expect(within(buttons[0].closest('[role="note"]') as HTMLElement).queryByText(HTTP_FAILURE)).toBeNull();
    expect(screen.getAllByText(HTTP_FAILURE)).toHaveLength(1);
    expect(buttons[0]).toHaveFocus();
    expect(server.count("/kg-analysis")).toBe(0);
  }
});

test("a rebuild finishing in another conversation settles the original answer before reopening it", async () => {
  const server = serveWorkspace();
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  expect(screen.getByText("正在更新分析…")).toBeVisible();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "新会话" })); });
  expect(screen.queryByText("排行回归回答")).toBeNull();
  server.finish();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(server.count("/kg-analysis")).toBe(1);
  vi.useRealTimers();
  await openHistoricalAnswer();
  expect(screen.getByRole("button", { name: "重新提问" })).toBeEnabled();
  expect(screen.queryByText("正在更新分析…")).toBeNull();
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(1);
});

test("a pending model probe cannot open a confirmation in a new conversation or strand its original answer", async () => {
  const server = serveWorkspace({ probe: true, holdProbe: true });
  render(<Home />);
  await openHistoricalAnswer();
  const actor = userEvent.setup();
  await actor.click(screen.getByRole("button", { name: "更新分析" }));
  await waitFor(() => expect(server.count("/unified-kg/rebuild/model-calls")).toBe(1));
  expect(screen.getByRole("button", { name: "更新分析" })).toBeDisabled();
  await actor.click(screen.getByRole("button", { name: "新会话" }));
  expect(screen.queryByText("排行回归回答")).toBeNull();
  await act(async () => { server.releaseProbe(); });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(server.count("/unified-kg/rebuild")).toBe(0);
  await openHistoricalAnswer();
  const retry = screen.getByRole("button", { name: "更新分析" });
  expect(retry).toBeEnabled();
  await actor.click(retry);
  expect(within(await screen.findByRole("dialog")).getByRole("heading", { name: "生成或更新图谱分析" })).toBeVisible();
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(2);
});

test("a successful rebuild report arriving in a new conversation still settles its original answer", async () => {
  const server = serveWorkspace({ holdAnalysis: true });
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  server.finish();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(server.count("/kg-analysis")).toBe(1);
  expect(screen.getByText("正在更新分析…")).toBeVisible();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "新会话" })); });
  expect(screen.queryByText("排行回归回答")).toBeNull();
  await act(async () => { server.releaseAnalysis(); });
  vi.useRealTimers();
  await openHistoricalAnswer();
  expect(screen.getByRole("button", { name: "重新提问" })).toBeEnabled();
  expect(screen.queryByText("正在更新分析…")).toBeNull();
  expect(server.count("/kg-analysis")).toBe(1);
});

test("a forbidden probe hides rebuild on every answer in the notebook", async () => {
  const server = serveWorkspace({ probeError: 403, twoAnswers: true });
  render(<Home />);
  await openHistoricalAnswer();
  await userEvent.setup().click(screen.getAllByRole("button", { name: "更新分析" })[0]);
  await waitFor(() => expect(screen.queryByRole("button", { name: "更新分析" })).toBeNull());
  expect(server.count("/unified-kg/rebuild")).toBe(0);
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("a forbidden probe arriving after switching conversations still removes every rebuild action in that notebook", async () => {
  const server = serveWorkspace({ probeError: 403, holdProbe: true, twoAnswers: true });
  render(<Home />);
  await openHistoricalAnswer();
  const actor = userEvent.setup();
  await actor.click(screen.getAllByRole("button", { name: "更新分析" })[0]);
  await waitFor(() => expect(server.count("/unified-kg/rebuild/model-calls")).toBe(1));
  await actor.click(screen.getByRole("button", { name: "新会话" }));
  await act(async () => { server.releaseProbe(); });
  await openHistoricalAnswer();
  expect(screen.getAllByText(server.line)).toHaveLength(2);
  expect(screen.queryByRole("button", { name: "更新分析" })).toBeNull();
  expect(server.count("/unified-kg/rebuild")).toBe(0);
});

test("an unavailable probe requires model-use confirmation", async () => {
  const server = serveWorkspace({ probeError: 503 });
  render(<Home />);
  await openHistoricalAnswer();
  await userEvent.setup().click(screen.getByRole("button", { name: "更新分析" }));
  expect(within(await screen.findByRole("dialog")).getByText(/这一步可能调用已配置的模型/)).toBeVisible();
  expect(server.count("/unified-kg/rebuild")).toBe(0);
});

test.each(["failed", "idle", "timed_out"])("%s restores the update action without claiming no ranking was produced", async (outcome) => {
  const server = serveWorkspace();
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  if (outcome === "failed" || outcome === "idle") server.fail(outcome);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000 * (outcome === "timed_out" ? REBUILD_POLL_MAX_ATTEMPTS + 1 : 1)); });
  const note = screen.getByText(server.line).closest('[role="note"]') as HTMLElement;
  expect(within(note).getByRole("button", { name: "更新分析" })).toBeEnabled();
  expect(within(note).getByText(outcome === "failed"
    ? "重新合并没有完成，请稍后重试" : outcome === "timed_out"
      ? "重新合并还在进行，稍后打开知识图谱即可看到结果" : "暂时无法确认更新结果，请稍后重试")).toBeVisible();
  expect(screen.queryByText(FALLBACK)).toBeNull();
  expect(server.count("/kg-analysis")).toBe(0);
});

test.each([{ access: "reader" }, { mirror: true }])("unwritable notebook %j offers no rebuild action", async (options) => {
  const server = serveWorkspace(options);
  render(<Home />);
  await openHistoricalAnswer();
  expect(screen.getByText(server.line)).toBeVisible();
  expect(screen.queryByRole("button", { name: "更新分析" })).toBeNull();
});

test.each(["advanced", "simple"])("%s mode with all sources selected offers no select-all action", async (uiMode) => {
  serveWorkspace({ reason: "scoped_large_notebook", uiMode });
  render(<Home />);
  await openHistoricalAnswer();
  if (uiMode === "simple") await act(async () => { observed.source!.toggleSource("source-two"); });
  expect(screen.queryByRole("button", { name: "全选本库来源并重新提问" })).toBeNull();
});

test("reasking after production focuses the new turn", async () => {
  const server = serveWorkspace();
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  server.finish();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  vi.useRealTimers();
  await userEvent.setup().click(screen.getByRole("button", { name: "重新提问" }));
  const answer = await screen.findByText("再次提问完成");
  await waitFor(() => expect(answer.closest(".chat-turn")).toHaveFocus());
});

test("a reask refused by empty scope does not move focus when the next ordinary question is submitted", async () => {
  const server = serveWorkspace();
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  server.finish();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  vi.useRealTimers();
  const actor = userEvent.setup();
  await act(async () => { observed.source!.clearSourceSelection(); });
  await actor.click(screen.getByRole("checkbox", { name: "检索参考库：排除参考库" }));
  await actor.click(screen.getByRole("checkbox", { name: "检索参考库：保留参考库" }));
  await actor.click(screen.getByRole("button", { name: "重新提问" }));
  expect(server.count("/ask/stream")).toBe(0);
  expect(screen.getByText("当前检索范围为空，请至少选择一个来源或参考库。")).toBeVisible();
  await act(async () => { observed.source!.selectAllSources(); });
  const input = screen.getByRole("textbox", { name: "提问" });
  await actor.type(input, "新的普通问题");
  await actor.click(screen.getByRole("button", { name: "发送" }));
  const answer = await screen.findByText("再次提问完成");
  expect(server.count("/ask/stream")).toBe(1);
  expect(server.calls.find((call) => call.path.endsWith("/ask/stream"))!.body?.question).toBe("新的普通问题");
  expect(answer.closest(".chat-turn")).not.toHaveFocus();
});

test("replacing a ranking confirmation with another workspace info modal releases its pending action", async () => {
  const server = serveWorkspace({ probe: true });
  render(<Home />);
  await openHistoricalAnswer();
  const analysis = screen.getByRole("button", { name: "分析" });
  await userEvent.setup().click(screen.getByRole("button", { name: "更新分析" }));
  expect(within(await screen.findByRole("dialog")).getByRole("heading", { name: "生成或更新图谱分析" })).toBeVisible();
  // The same workspace owner replaces the existing info slot (as a later
  // asynchronous opener can do); exercise its real public control callback.
  fireEvent.click(analysis);
  const replacement = screen.getByRole("dialog");
  expect(within(replacement).getByRole("heading", { name: "分析" })).toBeVisible();
  fireEvent.click(within(replacement).getByTitle("关闭"));
  await userEvent.setup().click(screen.getByRole("button", { name: "更新分析" }));
  expect(within(await screen.findByRole("dialog")).getByRole("heading", { name: "生成或更新图谱分析" })).toBeVisible();
  expect(server.count("/unified-kg/rebuild/model-calls")).toBe(2);
  expect(server.count("/unified-kg/rebuild")).toBe(0);
});

test.each(["current", "missing"] as const)("a settled %s answer stays settled while another answer confirms or has an unaccepted claim", async (stamp) => {
  const server = serveWorkspace({ stamp, maintenanceUnreadable: true });
  render(<Home />);
  await openHistoricalAnswer();
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "更新分析" })); });
  server.finish();
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  vi.useRealTimers();
  const expectSettled = () => {
    const settledNote = (stamp === "current" ? screen.getAllByText(server.line)[0] : screen.getByText(FALLBACK))
      .closest('[role="note"]') as HTMLElement;
    expect(within(settledNote).queryByRole("button", { name: "更新分析" })).toBeNull();
    expect(within(settledNote).queryByText("正在更新分析…")).toBeNull();
    if (stamp === "current") expect(within(settledNote).getByRole("button", { name: "重新提问" })).toBeEnabled();
    else expect(within(settledNote).getByText(FALLBACK)).toBeVisible();
  };
  expectSettled();
  server.addOtherAnswer();
  await openHistoricalAnswer();
  server.setProbe(true);
  await userEvent.setup().click(screen.getByRole("button", { name: "更新分析" }));
  const confirmation = await screen.findByRole("dialog");
  expectSettled();
  server.rejectRebuild(409);
  fireEvent.click(within(confirmation).getByRole("button", { name: "确定" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "更新分析" })).toBeDisabled());
  expectSettled();
  expect(screen.queryByText("正在更新分析…")).toBeNull();
  expect(server.count("/kg-analysis")).toBe(1);
});
