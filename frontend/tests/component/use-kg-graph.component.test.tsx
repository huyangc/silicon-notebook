// Smoke coverage for the unified-graph / merge-review / maintenance domain
// owner on its own — no `Home`, no sibling KG domain, no composition layer.
// Two properties per domain hook: the owner-hidden view hands back stable
// references, and one command's late visible commit is refused by the
// exact-owner generation gate.
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { UnifiedGraphResp } from "../../app/workspace-model";

const kgApi = vi.hoisted(() => ({
  fetchPendingMerges: vi.fn(),
  fetchUnifiedGraph: vi.fn(),
  fetchUnifiedKgStatus: vi.fn(),
}));

vi.mock("../../features/kg-maintenance/kg-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../features/kg-maintenance/kg-api.ts")>()),
  ...kgApi,
}));

import { createTestKgAuthority } from "../../test-support/kg-owner-authority";
import { useKgGraph } from "../../app/use-kg-graph";

const policy = { canWriteKg: true, externalBuildPolling: false };

function effects() {
  return {
    notify: vi.fn(),
    reportError: vi.fn(),
    refreshNotebook: vi.fn(),
    focusGraphNode: vi.fn(),
  };
}

const graphResponse = { nodes: [{ id: "k1" }], edges: [] } as unknown as UnifiedGraphResp;

test("the owner-hidden graph view keeps stable references across renders", () => {
  const harness = createTestKgAuthority();
  const { result, rerender } = renderHook(() => useKgGraph({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  const first = result.current.view;
  rerender();
  const second = result.current.view;

  expect(first.searchHits).toHaveLength(0);
  expect(Object.is(first.searchHits, second.searchHits)).toBe(true);
  expect(Object.is(first.selectedTypes, second.selectedTypes)).toBe(true);
  expect(Object.is(first.pendingMerges, second.pendingMerges)).toBe(true);
  expect(first.graph).toBeNull();
});

test("a graph open that returns after the owner rotated is refused", async () => {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");

  let releaseGraph: (graph: UnifiedGraphResp) => void = () => {};
  kgApi.fetchUnifiedGraph.mockImplementation(() => new Promise((resolve) => {
    releaseGraph = resolve;
  }));
  kgApi.fetchPendingMerges.mockResolvedValue([]);
  kgApi.fetchUnifiedKgStatus.mockResolvedValue({ dirty: false });

  const { result } = renderHook(() => useKgGraph({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  const opened = result.current.openGraph();
  // The notebook is reopened under a brand-new owner generation while the
  // graph request is still in flight. The rotated owner is visible again, so a
  // published-anyway commit would be observable in the view below.
  harness.rotate();
  await act(async () => {
    releaseGraph(graphResponse);
    await opened;
  });

  expect(result.current.view.graph).toBeNull();
});

test("the same open lands when the owner never moved (non-vacuous control)", async () => {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");
  kgApi.fetchUnifiedGraph.mockResolvedValue(graphResponse);
  kgApi.fetchPendingMerges.mockResolvedValue([]);
  kgApi.fetchUnifiedKgStatus.mockResolvedValue({ dirty: false });

  const { result } = renderHook(() => useKgGraph({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  await result.current.openGraph();

  await waitFor(() => expect(result.current.view.graph).not.toBeNull());
  expect(result.current.view.open).toBe(true);
});
