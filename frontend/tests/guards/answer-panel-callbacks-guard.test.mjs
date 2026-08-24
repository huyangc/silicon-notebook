// AnswerView 交互回调的**正向**守卫 —— 防止主界面那几颗按钮静默消失。
//
// 背景:为了让只读的排障视图(dev/logs 的「活动」)不留下点了没反应的控件,
// answer-panel.tsx 把这批交互回调全改成了**可选** prop(缺省即不渲染那颗按钮,
// 见 answer-panel-readonly.component.test.tsx)。代价是对称的:page.tsx 那个**唯一
// 的生产调用点**漏传任何一个都不会 tsc 报错,只会让主界面上那颗按钮悄悄不见——
// 没有报错、没有空位、没有任何痕迹。
//
// 只读那一侧已经有回归门,这份补的是另一侧:生产调用点必须**仍然全传**。
//
// 判据用 AST 的 JSX 属性绑定作身份,**不含行号**(page.tsx 里那段 JSX 会上下漂,
// 那不是退化)。清单不手写、从 answer-panel.tsx 的 props 类型**派生**:可选回调是
// 一次加一个加出来的,手写清单只会停在写它的那天。
//
// ⚠ 覆盖边界(如实说明):本守卫只证明「传了一个非平凡的值」。传了一个真会跑、但跑
// 错事的实现,它抓不到——那类兜底是组件测试与代码评审。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { findFunction, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

// 直接用 typescript 而不是只用 semantic-source 的导出:这里要读的是**类型层**
// (property signature 上的 `?`),而 semantic-source 现有的导出都在值/JSX 层。
// 为一个守卫往共享件里加一个类型层遍历器,不如就地写这十行。
function optionalCallbackProps(sourceFile, componentName) {
  const declaration = findFunction(sourceFile, componentName);
  const [parameter] = declaration.parameters;
  const type = parameter?.type;
  if (!type || !ts.isTypeLiteralNode(type)) {
    throw new Error(`${componentName}: 第一个参数不是内联对象类型，守卫无法派生 prop 清单`);
  }
  return type.members
    .filter((member) => ts.isPropertySignature(member) && member.questionToken)
    .map((member) => member.name.getText(sourceFile))
    .filter((name) => /^on[A-Z]/.test(name))
    .sort();
}

// 派生清单**必须**覆盖到这几个已知回调。少了任何一个就说明派生走空了(找错函数、
// 类型改成了 interface、`?` 被挪走…),而一份空清单会让下面那条断言静默变成 no-op。
const KNOWN_OPTIONAL_CALLBACKS = [
  "onBuildScaleIndex",
  "onFeedback",
  "onImportGapSuggestion",
  "onOpenKnowhowRow",
  "onOpenKnowledgeGraph",
  "onOpenSource",
  "onSaveMemory",
];

// 传了但恒为空 = 没传。`disabled={false}` 那条先例的同款:形态上「写了」,行为上
// 那颗按钮照样不渲染(或渲染出来点了没反应)。
// ⚠ 刻意是**逐字**相等而不是 `.includes("undefined")`:生产调用点的
// `onTestModel={currentUser.role === "admin" ? runSystemModelTest : undefined}` 是
// 有意的权限门(非管理员本来就不该看到那颗按钮),它含 `undefined` 但不是恒空;而把
// 它改成裸 `undefined` 就该报红——那意味着谁都测不了模型。
const TRIVIALLY_EMPTY = /^(?:undefined|null|false|\([^)]*\)\s*=>\s*(?:\{\s*\}|undefined|null))$/;

test("page.tsx 的生产 <AnswerView> 仍然传齐 answer-panel 的每一个可选交互回调", async () => {
  const answerPanel = await parseModule("answer-panel.tsx");
  const required = optionalCallbackProps(answerPanel, "AnswerView");
  for (const name of KNOWN_OPTIONAL_CALLBACKS) {
    assert.ok(
      required.includes(name),
      `派生清单里没有 ${name}——answer-panel.tsx 的 props 形状变了，守卫可能已经变成空断言`,
    );
  }

  const page = await parseModule("page.tsx");
  const callSites = jsxElements(page, "AnswerView");
  // 0 = 入口被改名/删了,>1 = 多了一个可能漏传的调用点。两种都必须响亮失败,
  // 不能静默退化成「没找到就不检查」。
  assert.equal(callSites.length, 1, "page.tsx 里的 <AnswerView> 调用点数量漂移，守卫失效");
  const [callSite] = callSites;

  const offenders = [];
  for (const name of required) {
    const bound = callSite.bindings?.[name] ?? callSite.attributes?.[name];
    if (bound === undefined) {
      offenders.push(`${name}：没传 —— 主界面上那颗按钮会静默不渲染（可选 prop，tsc 不报错）`);
      continue;
    }
    if (TRIVIALLY_EMPTY.test(String(bound).trim())) {
      offenders.push(`${name}=${bound} 恒空，等于没传`);
    }
  }
  assert.deepEqual(offenders, []);
});

// 上面那条只证明「传了、且不是恒空」——分不清「传了 importGapSuggestion 本尊」
// 与「传了一个只读态会收起它的三元」。站外来源建议的「导入」是一次写入
// （POST /notebooks/{id}/sources/url），同「添加来源」弹窗既有的 `!readOnlyWorkspace`
// 口径：只读成员能看到披露，看不到导入入口。这里没有可单独渲染的组件接缝
// （AnswerView 直接嵌在 page.tsx 的生产 JSX 里，不像 dev/logs 活动详情那样另有
// 一层只读专用组件），所以按源码语义钉（同 conversation-title-limit-guard 的钉法）。
test("page.tsx 只读工作区收起 onImportGapSuggestion（导入是写操作，不能像其它可选回调一样恒传）", async () => {
  const page = await parseModule("page.tsx");
  const [callSite] = jsxElements(page, "AnswerView");
  const bound = String(callSite.bindings?.onImportGapSuggestion ?? "");
  assert.ok(
    bound.includes("readOnlyWorkspace") && bound.includes("importGapSuggestion"),
    `onImportGapSuggestion 的传值没有按 readOnlyWorkspace 三元收起——实际：${bound}`,
  );
});

// importGapSuggestionDisabledReason 不是回调（没有 `on` 前缀，上面两条派生清单都
// 不覆盖它），所以单独钉一条：page.tsx 的传值必须真的读了「添加来源」弹窗同一份
// docCapacity 判据，而不是恒 undefined——否则可写但已达文档上限的笔记本会先撞一次
// 远端 PDF 探测才拿到后端必然的容量拒绝，违反「确认上传前必须把批次计入上限，超额
// 时按钮直接置灰」那条红线。
test("page.tsx 传给 importGapSuggestionDisabledReason 的值读的是 docCapacity.atCapacity（不是恒 undefined）", async () => {
  const page = await parseModule("page.tsx");
  const [callSite] = jsxElements(page, "AnswerView");
  const bound = String(callSite.bindings?.importGapSuggestionDisabledReason ?? "");
  assert.ok(
    bound.length > 0,
    "importGapSuggestionDisabledReason 没传——可写但已达文档上限的笔记本会先撞一次远端探测才拿到容量拒绝",
  );
  assert.ok(
    bound.includes("docCapacity") && bound.includes("atCapacity"),
    `importGapSuggestionDisabledReason 的传值没有读 docCapacity.atCapacity——实际：${bound}`,
  );
  assert.doesNotMatch(
    bound.trim(),
    /^undefined$/,
    "importGapSuggestionDisabledReason 恒为 undefined，等于没传",
  );
});
