import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { importsIn, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const hook = await parseModule("use-kg-workspace.ts");

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

test("page composes one KG workspace owner and no longer owns Knowledge or graph HTTP", () => {
  assert.equal(callCount(page, "useKgWorkspace"), 1);
  assert.equal(importsIn(page).some(({ module }) => module === "./knowledge-api"), false);
  for (const name of [
    "listKnowledge",
    "listKnowledgeTypes",
    "fetchUnifiedGraph",
    "fetchPendingMerges",
    "fetchNodeContext",
    "fetchKgSearch",
    "fetchConceptDetail",
    "fetchKgNeighbors",
    "fetchMergeReviewJob",
    "reviewMerges",
    "confirmMerge",
    "rejectMerge",
    "rebuildUnifiedKg",
    "relinkKg",
    "buildKg",
    "rebuildKg",
  ]) assert.equal(callCount(page, name), 0, name);
});

test("KG hook has a positive dependency allowlist and no cross-workspace owner", () => {
  const allowed = new Set([
    "react",
    "./errors",
    "./kg-focus",
    "./kg-workspace-model",
    "./knowledge-api",
    "./kg-merge-model",
    "./in-progress-resume",
    "./schema-manager",
    "../features/kg-maintenance/kg-api",
    "../features/kg-maintenance/kg-rebuild-status",
    "../features/kg-maintenance/kg-relink-status",
    "./kg-build-status",
    "./workspace-model",
  ]);
  const imports = importsIn(hook).map(({ module }) => module);
  assert.deepEqual(imports.filter((module) => !allowed.has(module)), []);
  for (const forbidden of [
    "source-api",
    "ask-api",
    "report-api",
    "memory",
    "notebook-api",
    "page",
    "use-source-library",
    "use-ask-session",
    "use-report-workspace",
  ]) assert.equal(imports.some((module) => module.includes(forbidden)), false, forbidden);
});

test("KG hook exposes readonly views and named commands, never raw setters", () => {
  const names = returnedPropertyNames(hook, "useKgWorkspace");
  for (const name of [
    "knowledge",
    "schema",
    "graph",
    "enterKnowledge",
    "openSchemas",
    "openGraph",
    "beginNotebookTransition",
    "finishNotebookTransition",
    "leaveWorkspace",
  ]) assert.ok(names.includes(name), name);
  assert.equal(names.some((name) => name.startsWith("set")), false, names.join(","));
});

test("workspace transition and both authentication paths bind KG authority", () => {
  const text = page.getText(page);
  assert.match(text, /kgWorkspace\.beginNotebookTransition\(\)/);
  assert.match(text, /kgWorkspace\.finishNotebookTransition\(kgTransition, opened \? openedNotebook : null\)/);
  assert.match(text, /kgWorkspace\.leaveWorkspace\(\)/);
  assert.equal((text.match(/kgWorkspace\.activateActor\(u\.id\);/g) ?? []).length, 2);
  assert.match(text, /kgWorkspace\.activateActor\(u\.id\);[\s\S]{0,220}setCurrentUser\(u\)/);
  assert.match(
    text,
    /listBases\(notebookId\)[\s\S]{0,260}workspaceActorIdRef\.current === actorId[\s\S]{0,160}activeNotebookIdRef\.current === notebookId/,
  );
});

test("Knowledge, schemas, and unified graph remain explicit lazy commands", () => {
  const text = hook.getText(hook);
  assert.match(text, /const enterKnowledge = async \(\) =>/);
  assert.match(text, /const openSchemas = \(\) =>/);
  assert.match(text, /const openGraph = async \(/);
  const ownerEffect = text.slice(text.indexOf("useEffect(() => {"), text.indexOf("const beginNotebookTransition"));
  for (const forbidden of ["listKnowledge(", "listKnowledgeTypes(", "listNotebookObjectSchemas(", "fetchUnifiedGraph("]) {
    assert.equal(ownerEffect.includes(forbidden), false, forbidden);
  }
});

test("visible commits compare live actor and notebook before exact owner generation", () => {
  const text = hook.getText(hook);
  assert.match(
    text,
    /actorIdRef\.current === owner\.actorId[\s\S]{0,180}notebookIdRef\.current === owner\.notebookId[\s\S]{0,180}sameKgOwner\(ownerRef\.current, owner\)/,
  );
  assert.match(text, /pendingNotebookSnapshotRef/);
  assert.match(text, /notebookSnapshot\?\.id === notebookId/);
});

test("read-only review recovery and every provider-side write are policy gated", () => {
  const text = hook.getText(hook);
  assert.match(text, /if \(policyRef\.current\.canWriteKg\) \{[\s\S]{0,180}fetchMergeReviewJob/);
  for (const name of ["reviewPendingMerges", "reviewAllMerges", "decideMerge", "startRelink", "launchRebuild", "startKgBuild"]) {
    const at = text.indexOf(`const ${name}`);
    assert.ok(at >= 0, name);
    assert.match(text.slice(at, at + 900), /policyRef\.current\.canWriteKg/);
  }
  const pageText = page.getText(page);
  assert.match(pageText, /!readOnlyWorkspace && \([\s\S]{0,400}onClick=\{reviewPendingMerges\}/);
  assert.match(pageText, /!readOnlyWorkspace && <span className="kg-merge-actions">/);
});
