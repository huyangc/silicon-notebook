import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { findFunctionIn, importsIn, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const hook = await parseModule("use-report-workspace.ts");

function callCount(module, name) {
  let count = 0;
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
      && node.expression.text === name) count += 1;
    ts.forEachChild(node, visit);
  }
  visit(module);
  return count;
}

function returnedPropertyNames(module, functionName) {
  const names = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === functionName) {
      function findReturns(child) {
        if (ts.isReturnStatement(child) && child.expression
          && ts.isObjectLiteralExpression(child.expression)) {
          for (const property of child.expression.properties) {
            if (ts.isShorthandPropertyAssignment(property)
              || ts.isPropertyAssignment(property)
              || ts.isMethodDeclaration(property)) names.push(property.name.getText(module));
          }
        }
        ts.forEachChild(child, findReturns);
      }
      findReturns(node);
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(module);
  return names;
}

test("page composes one report workspace owner and no longer owns report HTTP", () => {
  assert.equal(callCount(page, "useReportWorkspace"), 1);
  assert.equal(importsIn(page).some(({ module }) => module.includes("report-api")), false);
  assert.equal(callCount(page, "createReport"), 0);
  assert.equal(callCount(page, "listReports"), 0);
  assert.equal(callCount(page, "getReport"), 0);
});

test("report hook dependency boundary is narrow and excludes other workspace domains", () => {
  const allowed = new Set([
    "react",
    "./errors",
    "./report-api",
    "./report-model",
    "./source-scope",
  ]);
  const imports = importsIn(hook).map(({ module }) => module);
  assert.deepEqual(imports.filter((module) => !allowed.has(module)), []);
  for (const forbidden of ["source-api", "ask-api", "memory", "knowledge", "notebook-api", "page", "report-view"]) {
    assert.equal(imports.some((module) => module.includes(forbidden)), false, forbidden);
  }
});

test("report hook exposes readonly views and named commands, never raw setters", () => {
  const names = returnedPropertyNames(hook, "useReportWorkspace");
  assert.ok(names.includes("reports"));
  assert.ok(names.includes("active"));
  assert.ok(names.includes("submitCreate"));
  assert.ok(names.includes("beginNotebookTransition"));
  assert.equal(names.some((name) => name.startsWith("set")), false, names.join(","));
});

test("workspace transitions and authenticated bootstrap delegate report authority", () => {
  const text = page.getText(page);
  assert.match(text, /reportWorkspace\.beginNotebookTransition\(\)/);
  assert.match(text, /reportWorkspace\.finishNotebookTransition\(reportTransition, opened\)/);
  assert.match(text, /reportWorkspace\.leaveWorkspace\(\)/);
  // reportWorkspace/askSession/sourceLibrary.activateActor 现在只经共享函数
  // activateWorkspaceOwners 间接调用；守卫 workspace-owner-transition-guard 钉住
  // 「只能从那里发出」。原断言钉三次调用紧邻 setCurrentUser(u) 的顺序邻接，现拆成
  // 两半：共享函数体内 reportWorkspace 仍紧邻 askSession 紧邻 sourceLibrary（相对
  // 顺序不变），加上两个认证站点各自 activateWorkspaceOwners(u.id) 紧邻
  // setCurrentUser(u)（站点内顺序不变）。
  assert.match(
    findFunctionIn(page, "Home", "activateWorkspaceOwners").getText(page),
    /reportWorkspace\.activateActor\(actorId\);\s*askSession\.activateActor\(actorId\);\s*sourceLibrary\.activateActor\(actorId\);/,
  );
  assert.equal(
    (text.match(/activateWorkspaceOwners\(u\.id\);\s*setCurrentUser\(u\)/g) ?? []).length,
    2,
  );
});

test("report entry remains lazy and navigation never calls report cancellation", () => {
  const text = page.getText(page);
  assert.match(text, /active:\s*chatMode === "reports"/);
  assert.equal(text.includes("reportWorkspace.requestCancel()"), false);
});
