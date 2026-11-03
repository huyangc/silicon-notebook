import test from "node:test";
import assert from "node:assert/strict";

import { fetchAnswerMemoryLinks } from "../../app/ask-api.ts";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
test.after(() => { globalThis.fetch = originalFetch; globalThis.window = originalWindow; });

test("answer-memory-links keeps legacy links and supplies empty states for an old backend", async () => {
  const calls = [];
  globalThis.window = { localStorage: { getItem: () => "answer-memory-session" } };
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ links: { a1: "m1" } }));
  };
  const controller = new AbortController();
  assert.deepEqual(await fetchAnswerMemoryLinks("nb1", ["a1", "a2"], controller.signal), {
    links: { a1: "m1" }, states: {},
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:8000/api/notebooks/nb1/answer-memory-links");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.signal, controller.signal);
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer answer-memory-session");
  assert.deepEqual(JSON.parse(calls[0].init.body), { answer_ids: ["a1", "a2"] });
});

test("answer-memory-links carries saved and suggested states in the same single request", async () => {
  const payload = {
    links: { a1: "m1" },
    states: {
      a1: { memory_id: "m1", status: "confirmed", suggested: true },
      a2: { memory_id: "m2", status: "candidate", suggested: true },
      a3: { memory_id: "m3", status: "rejected", suggested: true },
    },
  };
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return new Response(JSON.stringify(payload));
  };
  assert.deepEqual(await fetchAnswerMemoryLinks("nb1", ["a1", "a2", "a3"]), payload);
  assert.equal(requests, 1);
});
