// Smoke coverage for the graph object-type registry domain owner on its own —
// no `Home`, no sibling KG domain, no composition layer. Two properties per
// domain hook: the owner-hidden view hands back stable values, and one
// command's late visible commit is refused by the exact-owner generation gate.
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { ObjectSchema } from "../../app/workspace-model";

const knowledgeApi = vi.hoisted(() => ({
  listNotebookObjectSchemas: vi.fn(),
  listObjectSchemas: vi.fn(),
}));

vi.mock("../../app/knowledge-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/knowledge-api.ts")>()),
  ...knowledgeApi,
}));

import { createTestKgAuthority } from "../../test-support/kg-owner-authority";
import { useKgSchema } from "../../app/use-kg-schema";

const policy = { canManageNotebookSchemas: true, canManageGlobalSchemas: true };

function effects() {
  return { notify: vi.fn(), reportError: vi.fn() };
}

const row = { object_type: "concept", label: "概念" } as unknown as ObjectSchema;

test("the owner-hidden registry view stays closed and issues no request", () => {
  const harness = createTestKgAuthority();
  const { result, rerender } = renderHook(() => useKgSchema({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  result.current.openSchemas();
  rerender();

  expect(result.current.view.open).toBe(false);
  expect(result.current.view.schemas).toBeNull();
  expect(result.current.view.busy).toBe(false);
  expect(result.current.view.view).toBe("notebook");
  expect(knowledgeApi.listNotebookObjectSchemas).not.toHaveBeenCalled();
});

test("a registry load that returns after the owner rotated is refused", async () => {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");

  let releaseRows: (rows: ObjectSchema[]) => void = () => {};
  knowledgeApi.listNotebookObjectSchemas.mockImplementation(() => new Promise((resolve) => {
    releaseRows = resolve;
  }));

  const { result } = renderHook(() => useKgSchema({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  await act(async () => { result.current.openSchemas(); });
  // The notebook is reopened under a brand-new owner generation while the
  // registry request is still in flight. The rotated owner is visible again,
  // so a published-anyway commit would be observable in the view below.
  harness.rotate();
  await act(async () => { releaseRows([row]); });

  expect(result.current.view.schemas).toBeNull();
});

test("the same load lands when the owner never moved (non-vacuous control)", async () => {
  const harness = createTestKgAuthority();
  harness.establish("user-a", "notebook-a");
  knowledgeApi.listNotebookObjectSchemas.mockResolvedValue([row]);

  const { result } = renderHook(() => useKgSchema({
    authority: harness.authority,
    policy,
    effects: effects(),
  }));

  result.current.openSchemas();

  await waitFor(() => expect(result.current.view.schemas).toHaveLength(1));
  expect(result.current.view.open).toBe(true);
});
