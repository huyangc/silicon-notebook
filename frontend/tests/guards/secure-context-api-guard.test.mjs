// Secure Context 限定 API 的守卫。
//
// 事故（2026-09-21）：生产前端挂在 http://<内网 IP>:3000。`crypto.randomUUID` 只在 HTTPS /
// localhost 下存在，全局问答的提交路径裸调了它，于是生产上每一次提问都在发请求之前同步抛
// TypeError，而本机（localhost）与 CI（Node / jsdom 都有 randomUUID）永远是绿的。
// 能拦住这类问题的只有「不许裸调」本身：生成 id 一律走 `app/client-request-id.ts`。
import test from "node:test";
import assert from "node:assert/strict";

import { appSourceModules, propertyAccesses } from "../../test-support/semantic-source.mjs";

const OWNER = "client-request-id.ts";

test("crypto.randomUUID is only ever touched by the one generator that has a fallback", async () => {
  const offenders = [];
  for (const item of await appSourceModules()) {
    if (item.path === OWNER) continue;
    if (propertyAccesses(item.module).some((access) => /(^|\.)crypto\.randomUUID$/.test(access))) offenders.push(item.path);
  }
  assert.deepEqual(offenders, [], "use newClientRequestId() from client-request-id.ts");
});

test("the generator itself still guards the call", async () => {
  const owner = (await appSourceModules()).find((item) => item.path === OWNER);
  assert.ok(owner, "client-request-id.ts is missing");
  const accesses = propertyAccesses(owner.module);
  assert.ok(accesses.includes("crypto.randomUUID"));
  assert.ok(accesses.includes("crypto.getRandomValues"), "the fallback must not depend on a secure context");
});
