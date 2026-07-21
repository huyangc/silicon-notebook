import test from "node:test";
import assert from "node:assert/strict";

import {
  KnowhowSourceCleanupError,
  transferKnowhowTable,
} from "./knowhow-transfer.ts";

async function withAuthenticatedResponse(response, run) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const calls = [];
  globalThis.window = { localStorage: { getItem: () => "session-token" } };
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return response;
  };
  try {
    await run(calls);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
}

for (const [code, newTableId, message] of [
  ["source_cleanup_failed", "copied-cleanup", "副本已创建，源表暂未删除"],
  ["source_changed_kept", "copied-changed", "源表已更新，已保留原表"],
]) {
  test(`transfer preserves the ${code} cleanup result after cloned-body inspection`, async () => {
    const response = new Response(JSON.stringify({
      detail: { code, new_table_id: newTableId, message },
    }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
    await withAuthenticatedResponse(response, async (calls) => {
      await assert.rejects(
        transferKnowhowTable("nb-source", "table-1", "nb target", "move"),
        (error) => {
          assert.ok(error instanceof KnowhowSourceCleanupError);
          assert.equal(error.newTableId, newTableId);
          assert.equal(error.message, message);
          return true;
        },
      );
      assert.match(calls[0].url, /\/notebooks\/nb-source\/knowhow\/table-1\/transfer$/);
      assert.equal(calls[0].init.method, "POST");
      assert.deepEqual(JSON.parse(calls[0].init.body), {
        target_notebook_id: "nb target",
        mode: "move",
      });
      assert.equal(calls[0].init.headers.get("Authorization"), "Bearer session-token");
      assert.equal(calls[0].init.headers.get("Content-Type"), "application/json");
    });
  });
}

test("non-cleanup errors retain the original response body for the shared humanized layer", async () => {
  const response = new Response(JSON.stringify({ detail: "目标笔记本不可写入" }), {
    status: 403,
    headers: {
      "Content-Type": "application/json",
      "X-User-Message": "1",
    },
  });
  await withAuthenticatedResponse(response, async () => {
    await assert.rejects(
      transferKnowhowTable("nb-1", "table-1", "nb-2", "copy"),
      (error) => {
        assert.equal(error.message, "目标笔记本不可写入");
        return true;
      },
    );
  });
});
