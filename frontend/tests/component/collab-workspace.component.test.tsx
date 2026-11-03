import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import Home from "../../app/page";
import { setToken } from "../../app/auth";

afterEach(() => {
  cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
  window.localStorage.clear(); window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

function server(active: boolean, kind: "sources" | "knowledge" = "sources", sourceNotebookId = "n1") {
  const calls: Array<{ path: string; body: unknown }> = [];
  let sourceReads = 0;
  let knowledgeVersion = 1;
  let sourceVersion = 1;
  let sourceMode: "updated" | "deleted" | "temporary" = "updated";
  let heldSourceDetail: Promise<Response> | null = null;
  let failSourceList = false;
  const notebook = { id: "n1", name: "协作库", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "", access: "owner", can_manage_content: true, can_manage_notebook: true, ask_available: false, paper_meta_missing: false, collab_pulse: active, pulse_cursor: "e:1" };
  const json = (value: unknown, status = 200) => Response.json(value, { status });
  const sourceDetail = () => ({ id: "s1", notebook_id: sourceNotebookId, title: sourceVersion === 1 ? "协作者新增资料" : "更新后的来源", display_title: sourceVersion === 1 ? "协作者新增资料" : "更新后的来源", type: "text", status: "ready", parse_status: "extracted", summary: sourceVersion === 1 ? "原来的来源摘要" : "协作者更新后的摘要", element_count: 1, file_name: "synthetic.txt", file_size: 5 });
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), window.location.origin).pathname;
    calls.push({ path, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (path === "/api/ready") return json({ ready: true });
    if (path === "/api/auth/capabilities") return json({ local_login: true, local_registration: true, sso_login: false });
    if (path === "/api/me") return json({ id: "owner", username: "a20261101", role: "user", ui_mode: "auto", memory_mode: "manual" });
    if (path === "/api/health") return json({ status: "ok", llm_configured: false });
    if (path === "/api/notebooks") return json([notebook]);
    if (path === "/api/notebooks/n1") return json(notebook);
    if (path === "/api/notebooks/n1/conversations") return json([]);
    if (path === "/api/notebooks/n1/sources") {
      sourceReads += 1;
      if (failSourceList) { failSourceList = false; return json({ detail: "synthetic private diagnostic" }, 503); }
      const items = sourceReads > 1 && sourceMode !== "deleted" ? [{ ...sourceDetail(), notebook_id: "n1" }] : [];
      return json({ items, total_count: items.length, offset: 0, limit: 50, visible_document_count: items.length });
    }
    if (path === "/api/notebooks/n1/pulse") {
      const cursor = `e:${(kind === "knowledge" ? knowledgeVersion : sourceVersion) + 1}`;
      return json({ cursor, reset: false, changes: JSON.parse(String(init?.body)).since !== cursor ? [{ kind, actor_name: "同事乙", at: "2026-11-01T00:00:00Z" }] : [], viewers: [{ user_id: "editor", display_name: "同事乙", role: "editor" }], other_reader_count: 2, next_interval_s: 60 });
    }
    if (path === "/api/notebooks/n1/sources/s1") {
      if (heldSourceDetail) { const held = heldSourceDetail; heldSourceDetail = null; return held; }
      if (sourceMode === "deleted") return json({ detail: "gone" }, 404);
      if (sourceMode === "temporary") return json({ detail: "synthetic private diagnostic" }, 503);
      return json(sourceDetail());
    }
    if (path === "/api/notebooks/n1/sources/s1/elements-page") return json({ items: [{ id: "e1", source_id: "s1", element_type: "text", location_label: "1", text: sourceVersion === 1 ? "原来的来源原文" : "协作者更新后的原文", metadata: {} }], total_count: 1, offset: 0, limit: 40 });
    if (path === "/api/notebooks/n1/knowledge-types") return json([{ object_type: "procedure", label: "流程", count: 1 }]);
    if (path === "/api/notebooks/n1/knowledge") return json({ items: [{ id: "procedure-a", object_type: "procedure", headline: knowledgeVersion === 1 ? "初版流程" : "更新后的流程", fields: [], status: "reviewed", evidence: [] }], total_count: 1, offset: 0, limit: 50 });
    if (path === "/api/notebooks/n1/objects/procedure-a/context") return json({ id: "procedure-a", object_type: "procedure", name: "流程", section_path: "", definition: null, occurrences: [], steps: [{ name: knowledgeVersion === 1 ? "最初的步骤" : "协作者修改的步骤", element_text: knowledgeVersion === 1 ? "最初的原文" : "修改后的原文" }] });
    if (path === "/api/me/pending-actions") return json({ count: 0, items: [] });
    if (path === "/api/me/notebook-deletions") return json({ items: [] });
    if (path === "/api/me/pending-actions/stream") return new Promise<Response>(() => undefined);
    if (path === "/api/document-types") return json([]);
    return json({ detail: "fixture not provided" }, 503);
  });
  vi.stubGlobal("fetch", fetcher);
  setToken("synthetic-session");
  window.history.replaceState(null, "", "/#notebook=n1");
  return {
    calls, sourceReads: () => sourceReads, updateKnowledge: () => { knowledgeVersion += 1; },
    updateSource: (mode: typeof sourceMode) => { sourceMode = mode; sourceVersion += 1; },
    restoreSource: () => { sourceMode = "updated"; },
    failNextSourceList: () => { failSourceList = true; },
    holdSourceDetail: () => {
      let release!: (response: Response) => void;
      heldSourceDetail = new Promise((resolve) => { release = resolve; });
      return () => release(json(sourceDetail()));
    },
  };
}

test("real Home transition starts from its detail cursor and routes a sources pulse into the real source owner", async () => {
  const fixture = server(true);
  render(<Home />);
  await screen.findByRole("region", { name: "协作动态" });
  await waitFor(() => expect(fixture.calls.find((call) => call.path === "/api/notebooks/n1/pulse")?.body).toEqual({ since: "e:1", visible: true }));
  expect(await screen.findByText(/同事乙刚更新了来源/)).toBeVisible();
  await waitFor(() => expect(screen.getByText("协作者新增资料")).toBeVisible(), { timeout: 4_000 });
  expect(fixture.sourceReads()).toBe(2);
  expect(screen.getByText(/同事乙 · 另有 2 位读者/)).toBeVisible();
});

test("real Home opens a noncollaborative notebook without a pulse or activity request", async () => {
  const fixture = server(false);
  render(<Home />);
  await screen.findByRole("button", { name: "返回主页" });
  expect(screen.queryByRole("region", { name: "协作动态" })).toBeNull();
  expect(fixture.calls.filter((call) => /\/(pulse|activity)$/.test(call.path))).toEqual([]);
  expect(fixture.sourceReads()).toBe(1);
});

test.each(["updated", "deleted", "temporary"] as const)("real Home refreshes a source detail over a collapsed sidebar on %s", async (outcome) => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const fixture = server(true);
  const user = userEvent.setup();
  const home = render(<Home />);
  const row = await screen.findByRole("button", { name: "协作者新增资料" }, { timeout: 4_000 });
  const release = fixture.holdSourceDetail();
  await user.click(row);
  // The detail is still reading, so the sidebar remains interactive. Closing
  // it now is a real navigation sequence, before the modal publishes its lease.
  await user.click(screen.getByRole("button", { name: "收起来源栏" }));
  await act(async () => release());
  const dialog = await screen.findByRole("dialog", { name: "来源" });
  expect(home.container.querySelector(".workspace-grid")).toHaveClass("sources-collapsed");
  expect(within(dialog).getByText("原来的来源摘要")).toBeVisible();
  fixture.updateSource(outcome);
  visibility.mockReturnValue("hidden");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  visibility.mockReturnValue("visible");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  if (outcome === "deleted") {
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "来源" })).toBeNull(), { timeout: 4_000 });
  } else {
    if (outcome === "temporary") {
      await within(dialog).findByText("暂时没能刷新，当前内容仍保留", {}, { timeout: 4_000 });
      expect(within(dialog).getByText("原来的来源摘要")).toBeVisible();
      expect(within(dialog).getByText("原来的来源原文")).toBeVisible();
      expect(within(dialog).queryByText("synthetic private diagnostic")).toBeNull();
      fixture.restoreSource();
      await user.click(within(dialog).getByRole("button", { name: "刷新" }));
    }
    await within(dialog).findByText("协作者更新后的摘要", {}, { timeout: 4_000 });
    expect(within(dialog).getByText("协作者更新后的原文")).toBeVisible();
    expect(within(dialog).queryByText("原来的来源原文")).toBeNull();
  }
});

test("real Home does not attribute a borrowed detail over a collapsed sidebar to this notebook's pulse", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const fixture = server(true, "sources", "reference-book");
  const user = userEvent.setup();
  render(<Home />);
  const row = await screen.findByRole("button", { name: "协作者新增资料" }, { timeout: 4_000 });
  const release = fixture.holdSourceDetail();
  await user.click(row);
  await user.click(screen.getByRole("button", { name: "收起来源栏" }));
  await act(async () => release());
  await screen.findByRole("dialog", { name: "来源" });
  const before = fixture.calls.filter((call) => call.path.includes("/sources/s1")).length;
  fixture.updateSource("updated");
  visibility.mockReturnValue("hidden");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  visibility.mockReturnValue("visible");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await waitFor(() => expect(fixture.calls.filter((call) => call.path.endsWith("/pulse"))).toHaveLength(2));
  // No debounce timer is created for an unmounted domain.
  expect(fixture.calls.filter((call) => call.path.includes("/sources/s1"))).toHaveLength(before);
  expect(screen.getByText("原来的来源摘要")).toBeVisible();
});

test.each(["success", "temporary"] as const)("real Home reloads a collapsed sources panel only when it is expanded again (%s)", async (outcome) => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const fixture = server(true);
  const user = userEvent.setup();
  const home = render(<Home />);
  await screen.findByRole("button", { name: "协作者新增资料" }, { timeout: 4_000 });
  await user.click(screen.getByRole("button", { name: "收起来源栏" }));
  const before = fixture.sourceReads();
  fixture.updateSource("updated");
  visibility.mockReturnValue("hidden");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  visibility.mockReturnValue("visible");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await waitFor(() => expect(fixture.calls.filter((call) => call.path.endsWith("/pulse"))).toHaveLength(2));
  expect(fixture.sourceReads()).toBe(before);
  if (outcome === "temporary") fixture.failNextSourceList();
  await user.click(screen.getByRole("button", { name: "展开来源栏" }));
  if (outcome === "temporary") {
    const panel = within(home.container.querySelector(".sources-panel") as HTMLElement);
    await panel.findByText("暂时没能刷新，当前内容仍保留");
    expect(panel.getByRole("button", { name: "协作者新增资料" })).toBeVisible();
    expect(panel.queryByText("synthetic private diagnostic")).toBeNull();
    await user.click(panel.getByRole("button", { name: "刷新" }));
  }
  await screen.findByRole("button", { name: "更新后的来源" });
  expect(fixture.sourceReads()).toBe(before + (outcome === "temporary" ? 2 : 1));
});

test("real Home keeps expanded knowledge steps aligned with a collaborator's updated row", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const fixture = server(true, "knowledge");
  const user = userEvent.setup();
  render(<Home />);
  await screen.findByRole("region", { name: "协作动态" });
  await user.click(screen.getByRole("button", { name: "知识库" }));
  await screen.findByRole("heading", { name: "初版流程" });
  await user.click(screen.getByRole("button", { name: "展开原文" }));
  await screen.findByText("最初的步骤");
  fixture.updateKnowledge();
  visibility.mockReturnValue("hidden");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  visibility.mockReturnValue("visible");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await waitFor(() => expect(screen.getByRole("heading", { name: "更新后的流程" })).toBeVisible(), { timeout: 4_000 });
  await screen.findByText("协作者修改的步骤");
  expect(screen.queryByText("最初的步骤")).toBeNull();
  expect(screen.queryByRole("button", { name: "展开原文" })).toBeNull();
  expect(fixture.calls.filter((call) => call.path.endsWith("/procedure-a/context"))).toHaveLength(2);
});

test("real Home retains an already received change across the debounce-to-hidden transition", async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const fixture = server(true);
  render(<Home />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByRole("region", { name: "协作动态" })).toBeVisible();
  expect(fixture.calls.filter((call) => call.path.endsWith("/pulse"))).toHaveLength(1);
  visibility.mockReturnValue("hidden");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(fixture.sourceReads()).toBe(1);
  visibility.mockReturnValue("visible");
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("协作者新增资料")).toBeVisible();
  expect(fixture.sourceReads()).toBe(2);
  expect(fixture.calls.filter((call) => call.path.endsWith("/pulse"))[1]?.body).toEqual({ since: "e:2", visible: true });
});
