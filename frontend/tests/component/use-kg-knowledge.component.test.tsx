// Smoke coverage for the Knowledge domain owner on its own — no `Home`, no
// sibling KG domain, no composition layer. Two properties per domain hook:
// the owner-hidden view hands back stable references, and one command's late
// visible commit is refused by the exact-owner generation gate.
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

const knowledgeApi = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listKnowledgeTypes: vi.fn(),
  findDuplicates: vi.fn(),
  findOwnMemoryDuplicates: vi.fn(),
  mergeKnowledge: vi.fn(),
}));

vi.mock("../../app/knowledge-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/knowledge-api.ts")>()),
  ...knowledgeApi,
}));

import { createTestKgAuthority } from "../../test-support/kg-owner-authority";
import { MERGE_DONE, MERGE_TARGET_GONE, useKgKnowledge } from "../../app/use-kg-knowledge";
import { humanizedError } from "../../app/errors";
import type { DuplicateGroup } from "../../app/workspace-model";

const policy = { canGovernKnowledge: true, canMergeOwnMemory: true };
/** A read-only member: no knowledge governance, but her own Memory merges. */
const readerPolicy = { canGovernKnowledge: false, canMergeOwnMemory: true };

function effects() {
  return {
    notify: vi.fn(),
    reportError: vi.fn(),
    refreshCollection: vi.fn().mockResolvedValue(undefined),
    refreshNotebook: vi.fn(),
  };
}

test("the owner-hidden Knowledge view keeps stable references across renders", () => {
  const harness = createTestKgAuthority();
  const { result, rerender } = renderHook(() => useKgKnowledge({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  const first = result.current.view;
  rerender();
  const second = result.current.view;

  expect(first.types).toHaveLength(0);
  expect(Object.is(first.types, second.types)).toBe(true);
  expect(Object.is(first.contexts, second.contexts)).toBe(true);
  expect(first.items).toBeNull();
});

test("a Knowledge load that returns after the owner rotated is refused", async () => {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");

  let releaseTypes: (rows: { object_type: string; count: number }[]) => void = () => {};
  knowledgeApi.listKnowledgeTypes.mockImplementation(() => new Promise((resolve) => {
    releaseTypes = resolve;
  }));
  knowledgeApi.listKnowledge.mockResolvedValue({ items: [], total_count: 0 });

  const { result } = renderHook(() => useKgKnowledge({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  const entered = result.current.enterKnowledge();
  // The notebook is reopened under a brand-new owner generation while the type
  // request is still in flight. The rotated owner is visible again, so a
  // published-anyway commit would be observable in the view below.
  harness.rotate();
  await act(async () => {
    releaseTypes([{ object_type: "concept", count: 3 }]);
    await entered;
  });

  expect(result.current.view.types).toHaveLength(0);
  expect(knowledgeApi.listKnowledge).not.toHaveBeenCalled();
});

test("the same load lands when the owner never moved (non-vacuous control)", async () => {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");
  knowledgeApi.listKnowledgeTypes.mockResolvedValue([{ object_type: "concept", count: 3 }]);
  knowledgeApi.listKnowledge.mockResolvedValue({ items: [], total_count: 0 });

  const { result } = renderHook(() => useKgKnowledge({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  await result.current.enterKnowledge();

  await waitFor(() => expect(result.current.view.types).toHaveLength(1));
  expect(knowledgeApi.listKnowledge).toHaveBeenCalled();
});

function group(prefix: string): DuplicateGroup {
  return {
    object_type: "concept",
    similarity: 1,
    members: [
      { id: `${prefix}-a`, object_type: "concept", headline: `${prefix} a`, status: "approved" },
      { id: `${prefix}-b`, object_type: "concept", headline: `${prefix} b`, status: "approved" },
    ],
  };
}

function renderKnowledge(hookPolicy: { canGovernKnowledge: boolean; canMergeOwnMemory: boolean }) {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");
  const fx = effects();
  const hook = renderHook(() => useKgKnowledge({
    authority: harness.authority,
    policy: hookPolicy,
    effects: fx,
  }));
  return { ...hook, fx };
}

function resetDuplicateApi() {
  for (const fn of Object.values(knowledgeApi)) fn.mockReset();
  knowledgeApi.findDuplicates.mockResolvedValue([group("shared")]);
  knowledgeApi.findOwnMemoryDuplicates.mockResolvedValue([group("own")]);
  knowledgeApi.listKnowledge.mockResolvedValue({ items: [], total_count: 0 });
  knowledgeApi.listKnowledgeTypes.mockResolvedValue([{ object_type: "concept", count: 2 }]);
}

test("a reader's 查重 asks for her own Memory groups only, never the shared ones", async () => {
  resetDuplicateApi();
  const { result } = renderKnowledge(readerPolicy);
  await act(async () => { await result.current.findDuplicates(); });
  expect(knowledgeApi.findDuplicates).not.toHaveBeenCalled();
  expect(knowledgeApi.findOwnMemoryDuplicates).toHaveBeenCalledWith("notebook-a", "concept");
  expect(result.current.view.duplicates).toBeNull();
  expect(result.current.view.ownDuplicates).toEqual([group("own")]);
});

test("a governor's 查重 fills both partitions", async () => {
  resetDuplicateApi();
  const { result } = renderKnowledge(policy);
  await act(async () => { await result.current.findDuplicates(); });
  expect(result.current.view.duplicates).toEqual([group("shared")]);
  expect(result.current.view.ownDuplicates).toEqual([group("own")]);
});

test("a shared merge keeps its request sequence and answers in place, not in the page banner", async () => {
  resetDuplicateApi();
  const calls: string[] = [];
  knowledgeApi.mergeKnowledge.mockImplementation(async () => { calls.push("merge"); });
  knowledgeApi.listKnowledge.mockImplementation(async () => {
    calls.push("list");
    return { items: [], total_count: 0 };
  });
  knowledgeApi.listKnowledgeTypes.mockImplementation(async () => {
    calls.push("types");
    return [{ object_type: "concept", count: 1 }];
  });
  knowledgeApi.findDuplicates.mockImplementation(async () => { calls.push("shared"); return []; });
  knowledgeApi.findOwnMemoryDuplicates.mockImplementation(async () => { calls.push("own"); return []; });
  const { result, fx } = renderKnowledge(policy);
  let outcome: unknown;
  await act(async () => { outcome = await result.current.mergeKnowledge("shared-b", "shared-a", "shared"); });
  expect(calls).toEqual(["merge", "list", "types", "shared"]);
  expect(knowledgeApi.mergeKnowledge).toHaveBeenCalledWith("notebook-a", "shared-b", "shared-a");
  expect(outcome).toEqual({ tone: "done", text: MERGE_DONE });
  expect(fx.notify).not.toHaveBeenCalled();
  expect(fx.reportError).not.toHaveBeenCalled();
  expect(result.current.view.duplicates).toEqual([]);
});

test("a reader merges her own pair and only her partition is refreshed", async () => {
  resetDuplicateApi();
  knowledgeApi.mergeKnowledge.mockResolvedValue(undefined);
  knowledgeApi.findOwnMemoryDuplicates.mockResolvedValue([]);
  const { result, fx } = renderKnowledge(readerPolicy);
  let outcome: unknown;
  await act(async () => { outcome = await result.current.mergeKnowledge("own-b", "own-a", "own"); });
  expect(outcome).toEqual({ tone: "done", text: MERGE_DONE });
  expect(knowledgeApi.findDuplicates).not.toHaveBeenCalled();
  expect(result.current.view.ownDuplicates).toEqual([]);
  expect(fx.notify).not.toHaveBeenCalled();
});

test("a reader cannot start a shared merge at all", async () => {
  resetDuplicateApi();
  const { result } = renderKnowledge(readerPolicy);
  let outcome: unknown = "unset";
  await act(async () => { outcome = await result.current.mergeKnowledge("shared-b", "shared-a", "shared"); });
  expect(outcome).toBeNull();
  expect(knowledgeApi.mergeKnowledge).not.toHaveBeenCalled();
});

test("409 shows the server's words and refreshes that partition", async () => {
  resetDuplicateApi();
  knowledgeApi.mergeKnowledge.mockRejectedValue(humanizedError("要合并的知识对象刚被修改，请稍后重试", 409));
  const { result, fx } = renderKnowledge(readerPolicy);
  let outcome: unknown;
  await act(async () => { outcome = await result.current.mergeKnowledge("own-b", "own-a", "own"); });
  expect(outcome).toEqual({ tone: "failed", text: "要合并的知识对象刚被修改，请稍后重试" });
  expect(knowledgeApi.findOwnMemoryDuplicates).toHaveBeenCalledOnce();
  expect(knowledgeApi.listKnowledge).not.toHaveBeenCalled();
  expect(fx.reportError).not.toHaveBeenCalled();
});

test("404 says the entry is gone and refreshes that partition", async () => {
  resetDuplicateApi();
  knowledgeApi.mergeKnowledge.mockRejectedValue(humanizedError("资源不存在", 404));
  const { result } = renderKnowledge(policy);
  let outcome: unknown;
  await act(async () => { outcome = await result.current.mergeKnowledge("shared-b", "shared-a", "shared"); });
  expect(outcome).toEqual({ tone: "failed", text: MERGE_TARGET_GONE });
  expect(knowledgeApi.findDuplicates).toHaveBeenCalledOnce();
  expect(knowledgeApi.findOwnMemoryDuplicates).not.toHaveBeenCalled();
});

test("one failed partition does not swallow the other's groups", async () => {
  resetDuplicateApi();
  knowledgeApi.findOwnMemoryDuplicates.mockRejectedValue(humanizedError("服务暂时不可用，请稍后再试", 503));
  const { result, fx } = renderKnowledge(policy);
  await act(async () => { await result.current.findDuplicates(); });
  expect(result.current.view.duplicates).toEqual([group("shared")]);
  expect(result.current.view.ownDuplicates).toBeNull();
  expect(fx.reportError).toHaveBeenCalledOnce();
});

test("a merge in one section keeps the other section's in-flight 查重 result", async () => {
  resetDuplicateApi();
  let releaseShared: (rows: DuplicateGroup[]) => void = () => {};
  knowledgeApi.findDuplicates.mockImplementation(() => new Promise((resolve) => { releaseShared = resolve; }));
  knowledgeApi.mergeKnowledge.mockResolvedValue(undefined);
  const { result } = renderKnowledge(policy);
  let find: Promise<void> = Promise.resolve();
  act(() => { find = result.current.findDuplicates(); });
  await act(async () => { await result.current.mergeKnowledge("own-b", "own-a", "own"); });
  await act(async () => { releaseShared([group("late")]); await find; });
  expect(result.current.view.duplicates).toEqual([group("late")]);
});
