// Smoke coverage for the Knowledge domain owner on its own — no `Home`, no
// sibling KG domain, no composition layer. Two properties per domain hook:
// the owner-hidden view hands back stable references, and one command's late
// visible commit is refused by the exact-owner generation gate.
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

const knowledgeApi = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listKnowledgeTypes: vi.fn(),
}));

vi.mock("../../app/knowledge-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/knowledge-api.ts")>()),
  ...knowledgeApi,
}));

import { createTestKgAuthority } from "../../test-support/kg-owner-authority";
import { useKgKnowledge } from "../../app/use-kg-knowledge";

const policy = { canGovernKnowledge: true };

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
