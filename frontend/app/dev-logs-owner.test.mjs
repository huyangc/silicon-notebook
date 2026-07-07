import test from "node:test";
import assert from "node:assert/strict";

import { usernameForOwner } from "./dev/logs/owner.ts";

const users = [
  { id: "user-aaa", username: "a00000001" },
  { id: "user-bbb", username: "b00000002" },
];

test("命中 owner 返回其用户名", () => {
  assert.equal(usernameForOwner(users, "user-bbb", "self"), "b00000002");
});

test("owner 为空返回自身名", () => {
  assert.equal(usernameForOwner(users, "", "myself"), "myself");
});

test("未知 owner 回退 owner 原值", () => {
  assert.equal(usernameForOwner(users, "user-zzz", "self"), "user-zzz");
});
