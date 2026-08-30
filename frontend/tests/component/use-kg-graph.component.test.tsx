// Smoke coverage for the unified-graph / merge-review / maintenance domain
// owner on its own — no `Home`, no sibling KG domain, no composition layer.
// Two properties per domain hook: the owner-hidden view hands back stable
// references, and one command's late visible commit is refused by the
// exact-owner generation gate.
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { ConceptDetailResp, KgNeighborsResp, PendingMerge, UnifiedGraphResp } from "../../app/workspace-model";

const kgApi = vi.hoisted(() => ({
  fetchPendingMerges: vi.fn(),
  fetchUnifiedGraph: vi.fn(),
  fetchUnifiedKgStatus: vi.fn(),
  fetchConceptDetail: vi.fn(),
  fetchKgNeighbors: vi.fn(),
  fetchNodeContext: vi.fn(),
  rejectMerge: vi.fn(),
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

// R3·T-B2: hub-cluster member pagination ("load more members" on the concept
// detail panel). `fetchConceptDetail`'s 4th argument is the keyset cursor.
const conceptGraphResponse = {
  nodes: [{ id: "k1", object_type: "concept" }],
  edges: [],
} as unknown as UnifiedGraphResp;

const emptyNeighbors = {
  nodes: [], edges: [], focus_id: undefined, focus_object_id: undefined,
  locating_unavailable: false, source_notebook_id: "",
} as unknown as KgNeighborsResp;

function conceptPage(
  members: string[],
  nextCursor: string | null,
  // R3 PR-B P1-1/P1-2: `attached` ids and `member_total` are overridable so
  // a test can shape a later page the way the real backend does — repeating
  // an `attached` id across pages, or answering `member_total: null` on
  // every page after the first.
  options: { attached?: string[]; memberTotal?: number | null } = {},
): ConceptDetailResp {
  const { attached = [], memberTotal = 5 } = options;
  return {
    canonical_id: "k1",
    canonical_name: "HUB",
    members: members.map((id) => ({ id, object_type: "concept", payload: { name: id }, evidence: [] })),
    attached: attached.map((id) => ({ id, object_type: "claim", payload: { name: id }, evidence: [] })),
    evidence: [],
    member_total: memberTotal,
    next_cursor: nextCursor,
  };
}

async function openWithConceptSelected(
  harness: ReturnType<typeof createTestKgAuthority>,
  firstPage: ConceptDetailResp = conceptPage(["m1", "m2"], "m2"),
) {
  harness.establish("user-a", "notebook-a");
  kgApi.fetchUnifiedGraph.mockResolvedValue(conceptGraphResponse);
  kgApi.fetchPendingMerges.mockResolvedValue([]);
  kgApi.fetchUnifiedKgStatus.mockResolvedValue({ dirty: false });
  // A real (if short) delay, deliberately, rather than a same-microtask
  // `mockResolvedValue`: `selectNode` reads `selectedNodeIdRef.current`
  // (kept in sync with `selectedNodeId` state only once React re-renders)
  // right after this resolves, to guard against a since-superseded
  // selection. In production a real network round-trip gives React ample
  // time to flush the `setSelectedNodeId` call made moments earlier in the
  // same `selectNode` invocation; an instantly-resolving mock can race
  // ahead of that flush in this environment and make the guard discard its
  // own fresh selection.
  kgApi.fetchKgNeighbors.mockImplementation(() => new Promise((resolve) => {
    setTimeout(() => resolve(emptyNeighbors), 20);
  }));
  kgApi.fetchNodeContext.mockResolvedValue({});

  const rendered = renderHook(() => useKgGraph({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));
  await rendered.result.current.openGraph();
  await waitFor(() => expect(rendered.result.current.view.graph).not.toBeNull());
  kgApi.fetchConceptDetail.mockResolvedValueOnce(firstPage);
  await rendered.result.current.selectNode("k1");
  return rendered;
}

test("loadMoreConceptMembers passes the previous page's last member id as the cursor", async () => {
  const harness = createTestKgAuthority();
  const { result } = await openWithConceptSelected(harness);

  await waitFor(() => expect(result.current.view.conceptDetail?.next_cursor).toBe("m2"));
  expect(result.current.view.conceptDetail?.members.map((m) => m.id)).toEqual(["m1", "m2"]);

  kgApi.fetchConceptDetail.mockResolvedValueOnce(conceptPage(["m3", "m4"], "m4"));
  await act(async () => { await result.current.loadMoreConceptMembers(); });

  // (nb, nodeId, sourceNotebookId, after) — the 4th argument is the cursor.
  const lastCall = kgApi.fetchConceptDetail.mock.calls.at(-1);
  expect(lastCall).toEqual(["notebook-a", "k1", "notebook-a", "m2"]);
  // Accumulated, not replaced: page 1 + page 2.
  expect(result.current.view.conceptDetail?.members.map((m) => m.id)).toEqual(["m1", "m2", "m3", "m4"]);
  expect(result.current.view.conceptDetail?.next_cursor).toBe("m4");
});

test("a fresh first page landing while a load-more is in flight wins — the stale page is dropped, not merged", async () => {
  // Mutation-checked (design review B8): reverting `setConceptDetailFirstPage`
  // to a plain `setConceptDetail(detail)` (no epoch bump) turns this red —
  // the stale in-flight page 2 response then merges onto decideMerge's fresh
  // page 3, corrupting both the member list and next_cursor.
  const harness = createTestKgAuthority();
  const { result } = await openWithConceptSelected(harness);
  await waitFor(() => expect(result.current.view.conceptDetail?.next_cursor).toBe("m2"));

  let releaseLoadMore: (page: ConceptDetailResp) => void = () => {};
  kgApi.fetchConceptDetail.mockImplementationOnce(() => new Promise((resolve) => { releaseLoadMore = resolve; }));
  const loadMore = result.current.loadMoreConceptMembers();

  // While page 2 is still in flight, a merge decision (confirm=false skips
  // the rebuild call) lands a completely fresh first page for the same node.
  kgApi.rejectMerge.mockResolvedValue({ ok: true });
  kgApi.fetchPendingMerges.mockResolvedValue([]);
  kgApi.fetchConceptDetail.mockResolvedValueOnce(conceptPage(["m9"], null));
  const candidate: PendingMerge = { id: "cand-1", canonical_a: "k1", canonical_b: "k9", score: 0.9, status: "pending" };
  await act(async () => {
    await result.current.decideMerge(candidate, false);
  });
  expect(result.current.view.conceptDetail?.members.map((m) => m.id)).toEqual(["m9"]);
  expect(result.current.view.conceptDetail?.next_cursor).toBeNull();

  // Now the stale page-2 response resolves. It must be discarded, not merged
  // onto the fresh page 3 that landed in the meantime.
  await act(async () => {
    releaseLoadMore(conceptPage(["m3", "m4"], "m4"));
    await loadMore;
  });
  expect(result.current.view.conceptDetail?.members.map((m) => m.id)).toEqual(["m9"]);
  expect(result.current.view.conceptDetail?.next_cursor).toBeNull();
});

test("loadMoreConceptMembers keeps the first page's member_total when a later page answers null (R3 PR-B P1-1)", async () => {
  // Mutation-checked: dropping the `member_total: page.member_total ??
  // current.member_total` override (letting `...page` clobber it) turns
  // this red — the merged view would carry `null` instead of 430 after
  // page 2 lands.
  const harness = createTestKgAuthority();
  const { result } = await openWithConceptSelected(
    harness,
    conceptPage(["m1", "m2"], "m2", { memberTotal: 430 }),
  );
  await waitFor(() => expect(result.current.view.conceptDetail?.next_cursor).toBe("m2"));
  expect(result.current.view.conceptDetail?.member_total).toBe(430);

  // The backend only recomputes the COUNT on the first page (`after` unset)
  // — every later page answers `member_total: null`.
  kgApi.fetchConceptDetail.mockResolvedValueOnce(
    conceptPage(["m3", "m4"], "m4", { memberTotal: null }),
  );
  await act(async () => { await result.current.loadMoreConceptMembers(); });

  expect(result.current.view.conceptDetail?.member_total).toBe(430);
});

test("loadMoreConceptMembers dedupes attached objects repeated across pages (R3 PR-B P1-2)", async () => {
  // Mutation-checked: reverting to a bare `[...current.attached,
  // ...page.attached]` concatenation turns this red — "a1" would appear
  // twice in the merged list.
  const harness = createTestKgAuthority();
  const { result } = await openWithConceptSelected(
    harness,
    conceptPage(["m1", "m2"], "m2", { attached: ["a1", "a2"] }),
  );
  await waitFor(() => expect(result.current.view.conceptDetail?.next_cursor).toBe("m2"));
  expect(result.current.view.conceptDetail?.attached.map((n) => n.id)).toEqual(["a1", "a2"]);

  // Page 2's adjacency read reaches "a1" again (a hub cluster's attached
  // objects are not partitioned by member) plus one genuinely new id.
  kgApi.fetchConceptDetail.mockResolvedValueOnce(
    conceptPage(["m3", "m4"], "m4", { attached: ["a1", "a3"] }),
  );
  await act(async () => { await result.current.loadMoreConceptMembers(); });

  expect(result.current.view.conceptDetail?.attached.map((n) => n.id)).toEqual(["a1", "a2", "a3"]);
});
