import test from "node:test";
import assert from "node:assert/strict";

import {
  appSourceModules,
  declarations,
  importsFrom,
  parseModule,
} from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");
const answerPanel = await parseModule("answer-panel.tsx");
// 引用小卡片（`.cite-popover` + `.cite-detail-card`）从 answer-panel 抽成了自己的
// 模块：全局问答与笔记本内问答共用同一份实现。KG 类型标记跟着调用点走，所以它也
// 是 kg-type-mark 的消费方之一。
const citationCard = await parseModule("citation-card.tsx");
const workspaceModel = await parseModule("workspace-model.ts");
const kgTypeMark = await parseModule("kg-type-mark.tsx");
// 知识图谱视图从 page.tsx 搬到自己的模块（PR-5 分片 3）：画布绘制用的 KG_TYPE_STYLE
// 与图例/类型过滤的 KgTypeMark 都跟着走，page 只剩知识浏览器那两处标记。
const kgGraphView = await parseModule("kg-graph-view.tsx");


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
  // 判据不变：形状/配色/类型名只有 kg-type-mark 一个定义，谁都不许自己再实现一份。
  // 只换「谁来 import 全套」——canvas 绘制用的 KG_TYPE_STYLE 随 drawKgNode 一起搬进
  // 了知识图谱视图模块，page.tsx 里剩下的知识浏览器只用 KgTypeMark / kgTypeLabel。
  const expected = new Set(["KG_TYPE_STYLE", "KgTypeMark", "kgTypeLabel"]);
  assert.deepEqual(
    new Set(importsFrom(kgGraphView, "./kg-type-mark").map((item) => item.imported)),
    expected,
  );
  assert.deepEqual(
    new Set(importsFrom(page, "./kg-type-mark").map((item) => item.imported)),
    new Set(["KgTypeMark", "kgTypeLabel"]),
  );
  // 答案面的两个消费方合起来仍然只用这两个名字，且各自只 import 自己真正用到的
  // 那个：清单卡里的跨库条目只摆标记（KgTypeMark），引用小卡片还要写出类型名
  // （kgTypeLabel）。并集断言而不是逐文件写死，才不会在下一次拆分时变成一份
  // 「哪个文件该 import 哪个」的手抄账。
  assert.deepEqual(
    new Set([
      ...importsFrom(answerPanel, "./kg-type-mark").map((item) => item.imported),
      ...importsFrom(citationCard, "./kg-type-mark").map((item) => item.imported),
    ]),
    new Set(["KgTypeMark", "kgTypeLabel"]),
  );
  assert.equal(names(kgTypeMark, "function").has("KgTypeMark"), true);
  assert.equal(names(page, "function").has("KgTypeMark"), false);
  assert.equal(names(kgGraphView, "function").has("KgTypeMark"), false);
  assert.equal(names(answerPanel, "function").has("KgTypeMark"), false);
  assert.equal(names(citationCard, "function").has("KgTypeMark"), false);
});


// 引用小卡片只有**一份**实现：全局问答与笔记本内问答 import 同一个模块。复制一份
// 近似实现（两边各画一张卡）正是这条守卫要挡的事——两个面的引用呈现一旦分家，
// 修一边就只修一半，而界面上看不出来。
test("citation popover has one implementation shared by both ask surfaces", async () => {
  const globalAsk = await parseModule("ask/global-ask-workspace.tsx");
  assert.equal(names(citationCard, "function").has("CitationPopover"), true);
  assert.equal(names(citationCard, "function").has("SelectedReferenceDetail"), true);
  for (const [module, specifier, label] of [
    [answerPanel, "./citation-card", "answer-panel.tsx"],
    [globalAsk, "../citation-card", "ask/global-ask-workspace.tsx"],
  ]) {
    assert.ok(
      importsFrom(module, specifier).some((item) => item.imported === "CitationPopover"),
      `${label} 没有从 citation-card import CitationPopover——引用卡可能被复制了一份`,
    );
    assert.equal(names(module, "function").has("CitationPopover"), false);
    assert.equal(names(module, "function").has("SelectedReferenceDetail"), false);
  }
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
