// 生产事故的回归：`crypto.randomUUID` 是 Secure Context 限定 API，http://<IP> 部署里不存在。
import test from "node:test";
import assert from "node:assert/strict";

import { newClientRequestId } from "../../app/client-request-id.ts";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");

function withCrypto(value, run) {
  Object.defineProperty(globalThis, "crypto", { configurable: true, value });
  try { return run(); } finally { Object.defineProperty(globalThis, "crypto", descriptor); }
}

test("a secure context uses the platform UUID", () => {
  assert.match(newClientRequestId(), UUID_V4);
});

test("an insecure context (no randomUUID) still yields a well-formed UUID v4 from getRandomValues", () => {
  const real = descriptor.get ? descriptor.get.call(globalThis) : descriptor.value;
  const insecure = { getRandomValues: (array) => real.getRandomValues(array) };
  const ids = withCrypto(insecure, () => Array.from({ length: 200 }, () => newClientRequestId()));
  for (const id of ids) assert.match(id, UUID_V4);
  assert.equal(new Set(ids).size, ids.length);
});

test("no crypto at all, or a crypto that throws, never throws out of the generator", () => {
  const bare = withCrypto(undefined, () => [newClientRequestId(), newClientRequestId()]);
  assert.ok(bare.every((id) => typeof id === "string" && id.length >= 10));
  const hostile = { randomUUID() { throw new TypeError("not a function, effectively"); }, getRandomValues() { throw new Error("no entropy"); } };
  assert.ok(withCrypto(hostile, () => newClientRequestId()).length >= 10);
});
