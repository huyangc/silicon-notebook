// 独立页面的会话门守卫。
//
// 事故：`fetchMe()` 刻意不走全局的「401 清凭证并刷新」，因为工作区（`/`）的启动流程
// 自己处理被拒会话。可用户总览、更新记录、许愿墙、Agent 接入等独立页面也直接调它，
// 401 时只把「登录状态已失效，请重新登录」摆在页面上：凭证不清、不进登录页，刷新也没用，
// 用户只能自己点回主页。独立页面一律经 `page-user.ts` 的 `fetchPageUser()` 取当前用户——
// 它清掉被拒的那个凭证、带着本页去登录页，登录后再回来。直接用 `fetchMe` 的只能是工作区
// 自己（它的启动流程处理 401）和这个共用入口。
import test from "node:test";
import assert from "node:assert/strict";

import {
  appSourceModules,
  callsIn,
  findFunction,
  importsIn,
  propertyAccesses,
} from "../../test-support/semantic-source.mjs";

const SHARED_ENTRY = "page-user.ts";
const WORKSPACE = "page.tsx";
const DEFINITION = "auth.ts";

test("only the workspace and the shared entry read the user through fetchMe", async () => {
  const offenders = [];
  for (const item of await appSourceModules()) {
    if (item.path === DEFINITION || item.path === SHARED_ENTRY || item.path === WORKSPACE) continue;
    const imported = importsIn(item.module).some((entry) => entry.imported === "fetchMe");
    const accessed = propertyAccesses(item.module).some((access) => /(^|\.)fetchMe$/.test(access));
    if (imported || accessed) offenders.push(item.path);
  }
  assert.deepEqual(offenders, [], "standalone pages read the user with fetchPageUser() from page-user.ts");
});

test("the shared entry still turns a rejected session into sign-in and nothing else", async () => {
  const owner = (await appSourceModules()).find((item) => item.path === SHARED_ENTRY);
  assert.ok(owner, "page-user.ts is missing");
  const calls = callsIn(findFunction(owner.module, "fetchPageUser"));
  for (const required of ["fetchMe", "httpErrorStatus", "clearRejectedToken", "replacePageLocation", "signInLocation"]) {
    assert.ok(calls.includes(required), `fetchPageUser must call ${required}`);
  }
  assert.ok(!calls.includes("clearToken"), "only the rejected token may be cleared, never a switched-in one");
});

test("the workspace clears a rejected session and returns signed-in users to where they came from", async () => {
  const workspace = (await appSourceModules()).find((item) => item.path === WORKSPACE);
  assert.ok(workspace, "page.tsx is missing");
  const restore = callsIn(findFunction(workspace.module, "restoreAuthentication"));
  for (const required of ["fetchMe", "clearRejectedToken", "takeSignInReturnLocation", "replacePageLocation"]) {
    assert.ok(restore.includes(required), `restoreAuthentication must call ${required}`);
  }
  assert.ok(
    callsIn(workspace.module).filter((call) => call === "takeSignInReturnLocation").length >= 2,
    "both the restored session and the sign-in form must honour the return target",
  );
});
