import test from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  findFunctionIn,
  ifConditionsIn,
  jsxElements,
  parseModule,
} from "./test/semantic-source.mjs";
import {
  claimSourceDeleteRefresh,
  filterDeletedSourceItems,
  ownsSourceDeleteRefresh,
  readStableSourceSnapshot,
} from "./source-delete-state.ts";
import {
  clampSourcePage,
  sourcePageRequestIsCurrent,
} from "./source-page-state.ts";


const page = await parseModule("page.tsx");


test("source deletion is single-flight and releases its busy state on every exit", () => {
  const remove = findFunctionIn(page, "Home", "deleteSource");
  const conditions = ifConditionsIn(remove);
  const calls = callSitesIn(remove);

  assert.ok(
    conditions.includes("!notebookId || deletingSourceIdsRef.current.has(source.id)"),
    "the delete request must reject duplicate clicks before awaiting the network",
  );
  assert.ok(calls.some((call) => call.target === "deletingSourceIdsRef.current.add"));
  assert.ok(calls.some((call) => call.target === "deletingSourceIdsRef.current.delete"));
  assert.ok(calls.some((call) => call.target === "setDeletingSourceIds"));
});


test("successful deletion commits locally before guarded parallel reconciliation", () => {
  const remove = findFunctionIn(page, "Home", "deleteSource");
  const calls = callSitesIn(remove);
  const parallelRefresh = calls.find((call) => call.target === "Promise.allSettled");

  assert.ok(calls.some((call) => call.target === "setSources"));
  assert.ok(calls.some((call) => call.target === "setNotebookSourceTotal"));
  assert.ok(calls.some((call) => call.target === "setSourceDetail"));
  assert.ok(calls.some((call) => call.target === "sourceDeleteRefreshGenerationRef.current.get"));
  assert.ok(calls.some((call) => call.target === "sourceDeleteRefreshGenerationRef.current.set"));
  assert.ok(parallelRefresh, "collection, notebook and checkup refreshes must not form a serial waterfall");
  assert.match(parallelRefresh.arguments[0], /loadNotebookCollection\(\{ guard: isCurrent \}\)/);
  assert.match(parallelRefresh.arguments[0], /loadSourcesPage\(notebookId/);
  assert.match(parallelRefresh.arguments[0], /getNotebook\(notebookId\)/);
  assert.match(parallelRefresh.arguments[0], /fetchCheckup\(notebookId\)/);
  assert.match(parallelRefresh.arguments[0], /if \(isCurrent\(\)\)/);
});


test("a DELETE response converges after A to B to A without accepting stale refreshes", () => {
  const notebookA = "notebook-a";
  const requestStartedAtEpoch = 11;

  // The request started in A, but its original epoch is intentionally irrelevant when
  // the response arrives: B must not be touched, while returning to A must claim the
  // response using A's new epoch rather than the stale request epoch.
  assert.equal(claimSourceDeleteRefresh(notebookA, "notebook-b", 12, 1), null);
  const owner = claimSourceDeleteRefresh(notebookA, notebookA, 13, 1);
  assert.deepEqual(owner, { notebookId: notebookA, workspaceEpoch: 13, generation: 1 });
  assert.notEqual(owner?.workspaceEpoch, requestStartedAtEpoch);
  assert.equal(ownsSourceDeleteRefresh(owner, notebookA, 13, 1), true);

  // Once that authoritative refresh starts, a later navigation invalidates it—even if
  // the user eventually comes back to A again under another epoch.
  assert.equal(ownsSourceDeleteRefresh(owner, "notebook-b", 14, 1), false);
  assert.equal(ownsSourceDeleteRefresh(owner, notebookA, 15, 1), false);
});


test("same-notebook delete reconciliation is latest-wins", () => {
  const older = claimSourceDeleteRefresh("notebook-a", "notebook-a", 7, 4);
  const newer = claimSourceDeleteRefresh("notebook-a", "notebook-a", 7, 5);

  assert.equal(ownsSourceDeleteRefresh(older, "notebook-a", 7, 5), false);
  assert.equal(ownsSourceDeleteRefresh(newer, "notebook-a", 7, 5), true);
});


test("source page requests reject stale responses and clamp an emptied last page", () => {
  assert.equal(sourcePageRequestIsCurrent(8, 9, "a", "a", true), false);
  assert.equal(sourcePageRequestIsCurrent(9, 9, "a", "a", false), false);
  assert.equal(sourcePageRequestIsCurrent(9, 9, "a", "b", true), false);
  assert.equal(sourcePageRequestIsCurrent(9, 9, "a", "a", true), true);

  assert.equal(clampSourcePage(2, 21, 10), 2);
  assert.equal(clampSourcePage(2, 20, 10), 1);
  assert.equal(clampSourcePage(1, 0, 10), 0);

  const loadPage = findFunctionIn(page, "Home", "loadSourcesPage");
  const calls = callSitesIn(loadPage);
  assert.ok(calls.some((call) => call.target === "sourcePageRequestIsCurrent"));
  assert.ok(calls.some((call) => call.target === "clampSourcePage"));
  assert.equal(calls.filter((call) => call.target === "listSources").length, 2);
});


test("delete tombstones suppress stale rows and snapshot reads converge across two deletes", async () => {
  const result = filterDeletedSourceItems(
    [{ id: "keep" }, { id: "deleted" }],
    new Set(["deleted"]),
  );
  assert.deepEqual(result, { items: [{ id: "keep" }], removedCount: 1 });

  const remove = findFunctionIn(page, "Home", "deleteSource");
  const removeText = remove.getText(page);
  assert.match(removeText, /const notebookId = source\.notebook_id/);
  assert.match(removeText, /deletedSourceIdsByNotebookRef\.current\.set/);

  const open = findFunctionIn(page, "Home", "openNotebook");
  assert.ok(callSitesIn(open).some((call) => call.target === "filterDeletedSourceItems"));
  const openText = open.getText(page);
  assert.match(openText, /readStableSourceSnapshot/);

  let generation = 0;
  let reads = 0;
  const snapshot = await readStableSourceSnapshot(
    () => generation,
    async () => {
      reads += 1;
      if (reads <= 2) generation += 1;
      return { reads, generation };
    },
  );
  assert.deepEqual(snapshot, { reads: 3, generation: 2 });

  const openDetail = findFunctionIn(page, "Home", "openSourceById");
  const detailText = openDetail.getText(page);
  assert.match(detailText, /const requestGeneration = \+\+sourceDetailRequestGenerationRef\.current/);
  assert.match(detailText, /sourceDetailRequestGenerationRef\.current !== requestGeneration/);
  assert.match(detailText, /workspaceEpochRef\.current !== workspaceEpoch/);
  assert.match(detailText, /deletedSourceIdsByNotebookRef\.current\.get\(detail\.notebook_id\)/);
  const loadPage = findFunctionIn(page, "Home", "loadSourceElementPage");
  const loadPageText = loadPage.getText(page);
  assert.match(loadPageText, /const requestGeneration = sourceDetailRequestGenerationRef\.current/);
  assert.match(loadPageText, /sourceDetailRequestGenerationRef\.current !== requestGeneration/);
  assert.match(removeText, /if \(sourceDetailRef\.current\?\.id === source\.id\)/);
});


test("source list and detail expose an accessible deleting state", () => {
  const buttons = jsxElements(page, "button");
  const sourceMain = buttons.find(({ attributes }) => attributes.className === "source-row-main");
  const deleteButtons = buttons.filter(
    ({ attributes }) => attributes.className === "source-delete-button"
      || attributes.className === "icon-button subtle-icon danger-icon",
  );

  assert.equal(sourceMain?.bindings?.disabled, "deletingSource");
  assert.ok(deleteButtons.some(({ bindings }) => (
    bindings?.disabled === "deletingSource"
    && bindings?.["aria-label"]?.includes("正在删除来源")
  )));
  assert.ok(deleteButtons.some(({ bindings }) => (
    bindings?.disabled === "reparsingSource || sourceDetailDeleting"
    && bindings?.["aria-label"]?.includes("正在删除来源")
  )));
  assert.equal(jsxElements(page, "Loader2").length >= 3, true);
});
