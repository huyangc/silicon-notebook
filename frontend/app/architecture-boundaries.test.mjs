import test from "node:test";
import assert from "node:assert/strict";

import {
  appSourceModules,
  declarations,
  importsFrom,
  parseModule,
} from "./test/semantic-source.mjs";


const page = await parseModule("page.tsx");
const answerPanel = await parseModule("answer-panel.tsx");
const workspaceModel = await parseModule("workspace-model.ts");
const kgTypeMark = await parseModule("kg-type-mark.tsx");


function names(module, kind) {
  return new Set(
    declarations(module)
      .filter((finding) => finding.kind === kind)
      .map((finding) => finding.name),
  );
}


test("workspace orchestrator imports answer UI instead of reimplementing it", () => {
  assert.deepEqual(
    importsFrom(page, "./answer-panel").map((item) => item.imported),
    ["AnswerView", "LatexText", "ReasoningTracePanel"],
  );
  assert.equal(names(page, "function").has("AnswerView"), false);
  assert.equal(names(page, "function").has("ReasoningTracePanel"), false);
  assert.equal(names(answerPanel, "function").has("AnswerView"), true);
  assert.equal(names(answerPanel, "function").has("ReasoningTracePanel"), true);
});


test("workspace API models have one shared module", () => {
  assert.ok(importsFrom(page, "./workspace-model").length > 20);
  assert.equal(names(page, "type").has("NotebookSummary"), false);
  assert.equal(names(page, "type").has("AskResponse"), false);
  assert.equal(names(workspaceModel, "type").has("NotebookSummary"), true);
  assert.equal(names(workspaceModel, "type").has("AskResponse"), true);
});


test("KG type marks are shared by graph and answer panels", () => {
  const expected = new Set(["KG_TYPE_STYLE", "KgTypeMark", "kgTypeLabel"]);
  assert.deepEqual(
    new Set(importsFrom(page, "./kg-type-mark").map((item) => item.imported)),
    expected,
  );
  assert.deepEqual(
    new Set(importsFrom(answerPanel, "./kg-type-mark").map((item) => item.imported)),
    new Set(["KgTypeMark", "kgTypeLabel"]),
  );
  assert.equal(names(kgTypeMark, "function").has("KgTypeMark"), true);
  assert.equal(names(page, "function").has("KgTypeMark"), false);
});


test("frontend has no retired personal model configuration contract", async () => {
  const forbidden = [
    "/me/model-settings",
    "/me/model-settings/test",
    "fetchModelSettings",
    "saveModelSettings",
    "testModelService",
    "baseUrlDirty",
    "keyDirty",
    "测试未保存设置",
    "编辑个人设置",
  ];
  const violations = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === "architecture-boundaries.test.mjs") continue;
    const source = module.getFullText();
    for (const value of forbidden) {
      if (source.includes(value)) violations.push(`${path}: ${value}`);
    }
  }
  assert.deepEqual(violations, []);
});
