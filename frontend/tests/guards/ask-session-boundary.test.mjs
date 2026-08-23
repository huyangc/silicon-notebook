import assert from "node:assert/strict";
import test from "node:test";

import ts from "typescript";

import {
  callsIn,
  findFunction,
  findFunctionIn,
  importsIn,
  parseModule,
} from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");
const hook = await parseModule("use-ask-session.ts");


test("page composes one Ask-session owner and no longer retains its state machine", () => {
  const text = page.getText(page);
  assert.equal((text.match(/useAskSession\(/g) ?? []).length, 1);
  for (const legacyOwner of [
    "const [question, setQuestion]",
    "const [turns, setTurns]",
    "const [sessions, setSessions]",
    "const [asking, setAsking]",
    "const askAbortRef",
    "const askJobIdRef",
    "const reconnectJob",
    "function executeAsk(",
    "function loadSessions(",
    "function applySessionDetail(",
    "function deleteSession(",
    "function commitRenameSession(",
  ]) {
    assert.equal(text.includes(legacyOwner), false, `page still owns ${legacyOwner}`);
  }

  const askImports = importsIn(page)
    .filter((item) => item.module === "./ask-api")
    .map((item) => item.imported);
  assert.deepEqual(askImports, [
    "askQuestionLimitHint",
    "fetchAnswerMemoryLinks",
  ]);
});


test("Ask-session hook has a narrow dependency boundary", () => {
  const allowed = new Set([
    "react",
    "./ask-api.ts",
    "./ask-intent-model.ts",
    "./ask-intent-trace.ts",
    "./ask-modes.ts",
    "./ask-reconnect.ts",
    "./ask-retrieval-effort.ts",
    "./ask-session-state.ts",
    "./ask-stream.ts",
    "./conversation-cleanup.ts",
    "./errors.ts",
    "./source-scope.ts",
    "./workspace-model.ts",
    "./workspace-transitions.ts",
  ]);
  const modules = importsIn(hook).map((item) => item.module);
  assert.deepEqual(modules.filter((module) => !allowed.has(module)), []);
  assert.deepEqual(
    modules.filter((module) => /(?:source|knowledge|report|memory)-api/.test(module)),
    [],
  );
});


function unwrap(expression) {
  let current = expression;
  while (
    ts.isAsExpression(current)
    || ts.isSatisfiesExpression(current)
    || ts.isParenthesizedExpression(current)
  ) {
    current = current.expression;
  }
  return current;
}


function publicHookProperties() {
  const useHook = findFunction(hook, "useAskSession");
  const candidates = [];
  function visit(node) {
    if (ts.isReturnStatement(node) && node.expression) {
      const expression = unwrap(node.expression);
      if (
        ts.isObjectLiteralExpression(expression)
        && expression.properties.some((property) => property.name?.getText(hook) === "beginNotebookTransition")
      ) {
        candidates.push(expression);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(useHook);
  assert.equal(candidates.length, 1, "useAskSession should have one public return object");
  return candidates[0].properties.map((property) => property.name?.getText(hook)).filter(Boolean);
}


test("Ask-session public surface exposes readonly views and named commands, never raw setters", () => {
  const properties = publicHookProperties();
  assert.deepEqual(properties.filter((name) => /^set[A-Z]/.test(name)), []);
  for (const command of [
    "beginNotebookTransition",
    "finishNotebookTransition",
    "restoreNotebook",
    "leaveWorkspace",
    "abortForLogout",
    "openSession",
    "startNewSession",
    "submit",
    "confirmIntent",
    "cancelIntent",
    "abort",
    "deleteSession",
    "bulkCleanup",
    "submitFeedback",
  ]) {
    assert.ok(properties.includes(command), `missing named Ask command ${command}`);
  }
});


test("workspace transitions delegate Ask ownership and authenticated bootstrap activates identity first", () => {
  // 结构项 F3：打开笔记本的编排收成了 `notebook-transition.ts` 的单一 transition，
  // 每个 owner 的 begin / commit / settle 只在 `notebookTransitionSteps` 这一份 step
  // 列表里声明（`openNotebook` 自己不再直接触达任何 owner hook 生命周期——
  // notebook-transition-guard 钉住这条）。Ask 的三个调用因此改在这里断言。
  const openCalls = callsIn(findFunctionIn(page, "Home", "notebookTransitionSteps"));
  for (const call of [
    "askSession.beginNotebookTransition",
    "askSession.restoreNotebook",
    "askSession.finishNotebookTransition",
  ]) {
    assert.ok(openCalls.includes(call), `notebookTransitionSteps is missing ${call}`);
  }
  // askSession.leaveWorkspace/abortForLogout/activateActor 现在只经共享函数
  // leaveWorkspaceOwners/leaveActorOwners/activateWorkspaceOwners 间接调用；守卫
  // workspace-owner-transition-guard 钉住「只能从那里发出」。
  assert.ok(
    callsIn(findFunction(page, "showCollection")).includes("leaveWorkspaceOwners"),
    "collection transition must call the shared leaveWorkspaceOwners sink",
  );
  assert.ok(
    callsIn(findFunctionIn(page, "Home", "leaveWorkspaceOwners")).includes("askSession.leaveWorkspace"),
    "leaveWorkspaceOwners must synchronously detach the Ask owner",
  );
  assert.ok(
    callsIn(findFunction(page, "handleLogout")).includes("leaveActorOwners"),
    "logout must call the shared leaveActorOwners sink",
  );
  assert.ok(
    callsIn(findFunctionIn(page, "Home", "leaveActorOwners")).includes("askSession.abortForLogout"),
    "leaveActorOwners must explicitly tear down Ask work",
  );
  assert.ok(
    callsIn(findFunctionIn(page, "Home", "activateWorkspaceOwners")).includes("askSession.activateActor"),
    "activateWorkspaceOwners must activate the Ask owner",
  );
  // 原断言钉「askSession → sourceLibrary → setCurrentUser」顺序邻接；现拆成两半：
  // 共享函数体内 askSession 仍紧邻 sourceLibrary（相对顺序不变），加上两个认证站点
  // 各自 activateWorkspaceOwners(u.id) 紧邻 setCurrentUser(u)（站点内顺序不变）。
  assert.match(
    findFunctionIn(page, "Home", "activateWorkspaceOwners").getText(page),
    /askSession\.activateActor\(actorId\);\s*sourceLibrary\.activateActor\(actorId\);/,
    "activateWorkspaceOwners must keep askSession before sourceLibrary",
  );
  assert.equal(
    (page.getText(page).match(/activateWorkspaceOwners\(u\.id\);\s*setCurrentUser\(u\)/g) ?? []).length,
    2,
    "authenticated bootstrap must activate hook identities before publishing currentUser",
  );
  assert.match(
    page.getText(page),
    /window\.addEventListener\("popstate", onPopState\)[\s\S]*?\}, \[authChecked, currentUser\?\.id\]\);/,
    "browser-history navigation must rebind after the authenticated actor changes",
  );
});
