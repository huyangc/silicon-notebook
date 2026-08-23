// Owner hook 生命周期扇出的接线守卫 —— 钉住 page.tsx 里 activateActor /
// leaveWorkspace / leaveActor / abortForLogout 只能从三个指定的集中函数发出：
//
//  - activateWorkspaceOwners：登录成功（挂载认证 / AuthGate 两个调用点）把
//    actor 接上全部 owner hook。
//  - leaveWorkspaceOwners：回集合页（showCollection）——放弃 workspace、
//    保留 actor。
//  - leaveActorOwners：登出（handleLogout）——放弃 actor 本身。
//
// 新增第八个 owner hook、或把某个 hook 的生命周期调用挪出这三个函数（哪怕只是
// 移到另一个函数里），都必须在这里现形——判据全部是语义的（AST 上调用点的最
// 近命名函数作用域与实参），不含行号、不用 .getStart()/.getEnd() 之类的位置查询。
import test from "node:test";
import assert from "node:assert/strict";

import { findFunctionIn, parseModule, scopedCalls } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const calls = scopedCalls(page);

const ACTIVATE_SCOPE = "<module>.Home.activateWorkspaceOwners";
const LEAVE_WORKSPACE_SCOPE = "<module>.Home.leaveWorkspaceOwners";
const LEAVE_ACTOR_SCOPE = "<module>.Home.leaveActorOwners";

test("三个扇出函数都存在且可被语义定位（空转保护）", () => {
  findFunctionIn(page, "Home", "activateWorkspaceOwners");
  findFunctionIn(page, "Home", "leaveWorkspaceOwners");
  findFunctionIn(page, "Home", "leaveActorOwners");
});

test("activateActor 只能从 activateWorkspaceOwners 发出，且覆盖 >=7 个 owner hook", () => {
  const activateCalls = calls.filter((entry) => entry.target.endsWith(".activateActor"));

  const rogue = activateCalls.filter((entry) => entry.scope !== ACTIVATE_SCOPE);
  assert.deepEqual(
    rogue,
    [],
    `activateActor 被从 activateWorkspaceOwners 之外调用：${JSON.stringify(rogue)}`,
  );

  const receivers = new Set(
    activateCalls
      .filter((entry) => entry.scope === ACTIVATE_SCOPE)
      .map((entry) => entry.target.replace(/\.activateActor$/, "")),
  );
  assert.ok(
    receivers.size >= 7,
    `activateWorkspaceOwners 应覆盖 >=7 个不同 owner hook 的 activateActor，实测 ${receivers.size}：${[...receivers].join(", ")}`,
  );

  const totalCalls = activateCalls
    .filter((entry) => entry.scope === ACTIVATE_SCOPE)
    .reduce((sum, entry) => sum + entry.count, 0);
  assert.ok(totalCalls >= 7, `activateActor 调用总数应 >=7，实测 ${totalCalls}`);
});

test("activateWorkspaceOwners 至少有两个调用点（挂载认证 + AuthGate）", () => {
  const invocationTotal = calls
    .filter((entry) => entry.target === "activateWorkspaceOwners")
    .reduce((sum, entry) => sum + entry.count, 0);
  assert.ok(
    invocationTotal >= 2,
    `activateWorkspaceOwners(...) 调用点应 >=2，实测 ${invocationTotal}`,
  );
});

test("leaveWorkspace 只能从 leaveWorkspaceOwners 或 leaveActorOwners 发出", () => {
  const leaveWorkspaceCalls = calls.filter((entry) => entry.target.endsWith(".leaveWorkspace"));
  const allowedScopes = new Set([LEAVE_WORKSPACE_SCOPE, LEAVE_ACTOR_SCOPE]);

  const rogue = leaveWorkspaceCalls.filter((entry) => !allowedScopes.has(entry.scope));
  assert.deepEqual(
    rogue,
    [],
    `leaveWorkspace 被从 leaveWorkspaceOwners/leaveActorOwners 之外调用：${JSON.stringify(rogue)}`,
  );

  const totalCalls = leaveWorkspaceCalls.reduce((sum, entry) => sum + entry.count, 0);
  assert.ok(totalCalls >= 5, `leaveWorkspace 调用总数应 >=5，实测 ${totalCalls}`);
});

test("leaveActor / abortForLogout 只能从 leaveActorOwners 发出", () => {
  const leaveActorCalls = calls.filter((entry) => entry.target.endsWith(".leaveActor"));
  const abortCalls = calls.filter((entry) => entry.target.endsWith(".abortForLogout"));

  const rogueLeaveActor = leaveActorCalls.filter((entry) => entry.scope !== LEAVE_ACTOR_SCOPE);
  assert.deepEqual(
    rogueLeaveActor,
    [],
    // 现状 showCollection 里没有调用过 leaveActor，因此这里不需要为它单独放行；
    // 一旦将来出现，须在这条断言旁登记理由，而不是悄悄放宽 allowedScopes。
    `leaveActor 被从 leaveActorOwners 之外调用：${JSON.stringify(rogueLeaveActor)}`,
  );

  const rogueAbort = abortCalls.filter((entry) => entry.scope !== LEAVE_ACTOR_SCOPE);
  assert.deepEqual(
    rogueAbort,
    [],
    `abortForLogout 被从 leaveActorOwners 之外调用：${JSON.stringify(rogueAbort)}`,
  );

  assert.ok(leaveActorCalls.length >= 1, "leaveActorOwners 应包含至少一个 leaveActor 调用");
  assert.ok(abortCalls.length >= 1, "leaveActorOwners 应包含至少一个 abortForLogout 调用");
});

test("showCollection 与 handleLogout 分别只经集中函数触达 owner hook", () => {
  const showCollectionScope = "<module>.Home.showCollection";
  const handleLogoutScope = "<module>.Home.handleLogout";

  const showCollectionLeaveWorkspaceOwnersCalls = calls.filter(
    (entry) => entry.scope === showCollectionScope && entry.target === "leaveWorkspaceOwners",
  );
  assert.ok(
    showCollectionLeaveWorkspaceOwnersCalls.length >= 1,
    "showCollection 必须调用 leaveWorkspaceOwners()",
  );

  const handleLogoutLeaveActorOwnersCalls = calls.filter(
    (entry) => entry.scope === handleLogoutScope && entry.target === "leaveActorOwners",
  );
  assert.ok(
    handleLogoutLeaveActorOwnersCalls.length >= 1,
    "handleLogout 必须调用 leaveActorOwners()",
  );
});
