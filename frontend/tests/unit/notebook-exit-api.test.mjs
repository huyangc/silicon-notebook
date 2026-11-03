import test from "node:test";
import assert from "node:assert/strict";

import { getExitDisclosure, leaveNotebook, readExitState } from "../../app/notebook-exit-api.ts";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
globalThis.window = { localStorage: { getItem: () => "exit-session" } };
test.after(() => { globalThis.fetch = originalFetch; globalThis.window = originalWindow; });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

for (const route of [{ kind: "membership" }, { kind: "collaborator", userId: "me" }]) {
  test(route.kind + " disclosure includes suggestions and defaults old responses to zero", async () => {
    for (const count of [undefined, 0, 3]) {
      globalThis.fetch = async (url) => {
        assert.ok(String(url).endsWith(route.kind === "membership"
          ? "/membership/exit-disclosure" : "/collaborators/me/exit-disclosure"));
        return json({ memory_count: 2, suggestion_count: count });
      };
      assert.deepEqual(await getExitDisclosure("nb1", undefined, route), {
        memory_count: 2, suggestion_count: count ?? 0,
      });
    }
  });

  test(route.kind + " delete retains suggestion counts on success and both structured failures", async () => {
    globalThis.fetch = async () => new Response(null, { status: 204 });
    assert.deepEqual(await leaveNotebook("nb1", 0, route), {
      kind: "left", deleted: 0, deletedSuggestions: 0,
    });
    for (const count of [undefined, 0, 4]) {
      globalThis.fetch = async () => json({ deleted_memory_count: 2, deleted_suggestion_count: count });
      assert.deepEqual(await leaveNotebook("nb1", 2, route), {
        kind: "left", deleted: 2, deletedSuggestions: count ?? 0,
      });
      globalThis.fetch = async () => json({
        detail: { code: "exit_disclosure_required", memory_count: 3, suggestion_count: count },
      }, 409);
      assert.deepEqual(await leaveNotebook("nb1", 2, route), {
        kind: "disclosure", memoryCount: 3, suggestionCount: count ?? 0,
      });
      for (const status of [409, 503]) {
        globalThis.fetch = async () => json({
          detail: { code: "exit_incomplete", deleted_memory_count: 2, memory_count: 1, deleted_suggestion_count: count },
        }, status);
        assert.deepEqual(await leaveNotebook("nb1", 3, route), {
          kind: "incomplete", deleted: 2, remaining: 1, deletedSuggestions: count ?? 0, status,
        });
      }
    }
  });

  test(route.kind + " committed delete with an unreadable success body requires reconciliation", async () => {
    const responses = [
      () => new Response('{"deleted_memory_count":', { status: 200 }),
      () => new Response(new ReadableStream({
        start(controller) { controller.error(new TypeError("response body interrupted")); },
      }), { status: 200 }),
      ...[null, [], [2], {}, { deleted_memory_count: null },
        { deleted_memory_count: -1 }, { deleted_memory_count: 0.5 },
        { deleted_memory_count: "2" }, { deleted_suggestion_count: 3 }]
        .map((body) => () => json(body)),
    ];
    for (const response of responses) {
      globalThis.fetch = async () => response();
      assert.deepEqual(await leaveNotebook("nb1", 2, route), { kind: "unknown" });
    }
    for (const deleted of [0, 2]) {
      globalThis.fetch = async () => json({ deleted_memory_count: deleted });
      assert.deepEqual(await leaveNotebook("nb1", 2, route), {
        kind: "left", deleted, deletedSuggestions: 0,
      });
    }
  });
}

test("explicit malformed suggestion counts never silently become zero", async () => {
  for (const count of [-1, 0.5, "4", null]) {
    globalThis.fetch = async () => json({ memory_count: 2, suggestion_count: count });
    await assert.rejects(getExitDisclosure("nb1"), /malformed exit disclosure/);
    globalThis.fetch = async () => json({ deleted_memory_count: 1, deleted_suggestion_count: count });
    assert.deepEqual(await leaveNotebook("nb1", 1), { kind: "unknown" });
    globalThis.fetch = async () => json({
      detail: { code: "exit_disclosure_required", memory_count: 2, suggestion_count: count },
    }, 409);
    assert.deepEqual(await leaveNotebook("nb1", 1), { kind: "unknown" });
  }
});

test("unknown exit reconciliation preserves the current suggestion disclosure too", async () => {
  globalThis.fetch = async (url) => String(url).endsWith("/notebooks")
    ? json([{ id: "nb1", access: "reader", collaborator_role: "", granted_groups: [] }])
    : json({ memory_count: 1, suggestion_count: 3 });
  assert.deepEqual(await readExitState("nb1"), { left: false, remaining: 1, suggestionCount: 3 });
});
