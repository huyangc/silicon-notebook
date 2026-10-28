// 导入上传的「一串」只能在一处结束(换标识)—— notebook-import-upload.ts 的 `retireChain`。
//
// 背景:同一类缺陷前后五轮被评审找到——某条路径(预检被拒、白名单拒绝、作业列表里「没看到」、
// 到了客户端自己定的时限……)把一串结束、换了上传标识,而上一次带这个标识的请求其实还缓存在
// 代理里、或还占着服务端的预留,下一次重传就多出一本。修法是把「结束一串」收成一个只认服务端
// 凭据的函数;这份守卫钉住:删除一串、生成标识、改写标识,都不许出现在别处。
import test from "node:test";
import assert from "node:assert/strict";

import { assignmentsIn, parseModule, scopedCalls } from "../../test-support/semantic-source.mjs";

const store = await parseModule("notebook-import-upload.ts");
const calls = scopedCalls(store);

function scopesOf(target) {
  return calls.filter((call) => call.target === target).map((call) => call.scope);
}

test("删除一串只在 retireChain 里", () => {
  const scopes = scopesOf("chains.delete");
  assert.ok(scopes.length > 0, "找不到 chains.delete:守卫失效");
  assert.deepEqual([...new Set(scopes)], ["<module>.retireChain"]);
});

test("清空全部只在登出(reset)里", () => {
  assert.deepEqual([...new Set(scopesOf("chains.clear"))], ["<module>.reset"]);
});

test("生成标识、登记一串只在 chainFor 里(只为还没有一串的文件)", () => {
  assert.deepEqual([...new Set(scopesOf("newClientRequestId"))], ["<module>.chainFor"]);
  assert.deepEqual([...new Set(scopesOf("chains.set"))], ["<module>.chainFor"]);
});

test("没有任何地方改写一串的标识", () => {
  const rewrites = assignmentsIn(store).filter((assignment) => /\.uploadId$/.test(assignment.target));
  assert.deepEqual(rewrites, []);
});

test("retireChain 的每个调用都带着服务端凭据(job / server_none)", () => {
  const text = store.getText(store);
  const invocations = text.match(/retireChain\(file, \{ kind: "[a-z_]+"/g) ?? [];
  assert.ok(invocations.length > 0);
  const total = scopesOf("retireChain").length;
  assert.equal(invocations.length, calls.filter((call) => call.target === "retireChain")
    .reduce((sum, call) => sum + call.count, 0));
  assert.ok(total > 0);
  for (const invocation of invocations) {
    assert.match(invocation, /kind: "(job|server_none)"/);
  }
});
