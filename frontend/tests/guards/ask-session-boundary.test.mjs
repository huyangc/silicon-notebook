import assert from "node:assert/strict";
import test from "node:test";

import ts from "typescript";

import {
  callsIn,
  findFunction,
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
    "searchNotebooksBounded",
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
  const openCalls = callsIn(findFunction(page, "openNotebook"));
  for (const call of [
    "askSession.beginNotebookTransition",
    "askSession.restoreNotebook",
    "askSession.finishNotebookTransition",
  ]) {
    assert.ok(openCalls.includes(call), `openNotebook is missing ${call}`);
  }
  assert.ok(
    callsIn(findFunction(page, "showCollection")).includes("askSession.leaveWorkspace"),
    "collection transition must synchronously detach the Ask owner",
  );
  assert.ok(
    callsIn(findFunction(page, "handleLogout")).includes("askSession.abortForLogout"),
    "logout must explicitly tear down Ask work",
  );
  assert.match(
    page.getText(page),
    /askSession\.activateActor\(u\.id\);\s*sourceLibrary\.activateActor\(u\.id\);\s*setCurrentUser\(u\)/,
    "authenticated bootstrap must activate hook identities before publishing currentUser",
  );
});
