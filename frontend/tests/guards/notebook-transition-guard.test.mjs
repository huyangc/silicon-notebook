// 打开笔记本的编排接线守卫（结构项 F3）。
//
// `openNotebook` 曾经手写一整串 begin/commit/rollback：开头一串 begin、中间「谁拒绝了
// 就整体放弃」那段分支里的一次 finish(false)、成功路径上的 commit，以及 `finally` 里带
// `opened` 布尔的另一次 finish。同一个 owner 分散在四处，新增一个 owner 要在四处各补
// 一笔——漏掉任意一处都是**静默**失败（owner 永远停在 suspended 态，或者一次被顶替的
// 切换把状态提交给了新工作区，两者都不报错）。
//
// 现在这条流程收在 `app/notebook-transition.ts` 的单一 transition 里，每个 owner 只在
// `page.tsx::notebookTransitionSteps` 这**一份 step 列表**里声明自己的 begin / 可选
// commit / settle。本守卫钉住这条单点性：
//
//   ① 打开编排的五个函数（openNotebook 与它的四个相位 helper）里**不得直接出现**任何
//      owner hook 的生命周期调用——它们只能出现在 step 列表里；
//   ② step 列表确实覆盖全部六个 owner，且每个 owner 至少声明 begin + settle；
//   ③ openNotebook 恰好经 runNotebookTransition 消费这份列表一次；
//   ④ 编排模块本身是纯逻辑：不 import React、不 import 任何 app 模块。
//
// 判据全部是语义的（AST 上调用点的最近命名函数作用域与实参形状），不含行号，也不用
// `.getStart()`/`.getEnd()` 之类的位置查询（见 static-source-policy 守卫）。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import {
  findFunctionIn,
  importsIn,
  parseModule,
  scopedCalls,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const orchestrator = await parseModule("notebook-transition.ts");
const calls = scopedCalls(page);

const STEPS_SCOPE = "<module>.Home.notebookTransitionSteps";

// 打开笔记本的编排面：`openNotebook` 本身，加上它交给编排器的四个相位 helper。
// 这五个作用域里出现 owner 生命周期调用，就等于绕开了 step 列表这个单点。
const ORCHESTRATION_SCOPES = [
  "<module>.Home.openNotebook",
  "<module>.Home.enterNotebookTransition",
  "<module>.Home.openNotebookSnapshot",
  "<module>.Home.applyOpenedNotebook",
  "<module>.Home.concludeOpenNotebook",
];

const OWNER_HOOKS = [
  "rootModals",
  "workspaceExtensions",
  "sourceLibrary",
  "reportWorkspace",
  "kgWorkspace",
  "askSession",
  "notebookCollection",
];

// 生命周期动词。`sourceLibrary.deleteGeneration` 这类**读**不在其中——它是取数相位
// 合法的输入，不是把 owner 的生命周期挪动一格。
const LIFECYCLE = /^(begin|finish|commit|settle|restore|leave|activate|adopt)/;

function isLifecycleCall(target) {
  const [receiver, member, ...rest] = target.split(".");
  if (rest.length > 0) return false;
  return OWNER_HOOKS.includes(receiver) && Boolean(member) && LIFECYCLE.test(member);
}

/** step 列表里每个 `transitionStep({...})` 声明的 name → 它声明了哪几个相位。 */
function declaredSteps() {
  const declared = [];
  function visit(node) {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "transitionStep"
      && node.arguments.length === 1
      && ts.isObjectLiteralExpression(node.arguments[0])
    ) {
      const phases = [];
      let name = null;
      for (const property of node.arguments[0].properties) {
        if (!ts.isPropertyAssignment(property) || !ts.isIdentifier(property.name)) continue;
        const key = property.name.text;
        if (key === "name" && ts.isStringLiteral(property.initializer)) {
          name = property.initializer.text;
          continue;
        }
        phases.push(key);
      }
      declared.push({ name, phases });
    }
    ts.forEachChild(node, visit);
  }
  visit(findFunctionIn(page, "Home", "notebookTransitionSteps"));
  return declared;
}


test("空转保护：编排入口、step 列表与四个相位 helper 都可被语义定位", () => {
  findFunctionIn(page, "Home", "openNotebook");
  findFunctionIn(page, "Home", "notebookTransitionSteps");
  findFunctionIn(page, "Home", "enterNotebookTransition");
  findFunctionIn(page, "Home", "openNotebookSnapshot");
  findFunctionIn(page, "Home", "applyOpenedNotebook");
  findFunctionIn(page, "Home", "concludeOpenNotebook");
  // 空转保护的另一半：判据本身必须能认出至少一个真实的生命周期调用，否则下面那条
  // 「openNotebook 里一个都没有」会因为 LIFECYCLE 正则失效而恒真。
  const recognized = calls.filter((entry) => isLifecycleCall(entry.target));
  assert.ok(
    recognized.length >= 8,
    `LIFECYCLE 判据没有认出足够多的真实调用：${recognized.length}`,
  );
});


test("owner 生命周期调用只能出现在 step 列表里，不得直接写在打开编排的函数体内", () => {
  const rogue = calls.filter((entry) => (
    ORCHESTRATION_SCOPES.includes(entry.scope) && isLifecycleCall(entry.target)
  ));
  assert.deepEqual(
    rogue,
    [],
    "打开笔记本的编排函数体内直接触达了 owner hook 的生命周期。新增 owner 只能在 "
      + `notebookTransitionSteps 的 step 列表里加一项：${JSON.stringify(rogue)}`,
  );
});


test("step 列表覆盖全部六个 owner，每项至少声明 begin + settle", () => {
  const stepped = new Set(
    calls
      .filter((entry) => entry.scope === STEPS_SCOPE && isLifecycleCall(entry.target))
      .map((entry) => entry.target.split(".")[0]),
  );
  for (const owner of [
    "rootModals",
    "workspaceExtensions",
    "sourceLibrary",
    "reportWorkspace",
    "kgWorkspace",
    "askSession",
  ]) {
    assert.ok(stepped.has(owner), `step 列表漏掉了 owner: ${owner}`);
  }

  const declared = declaredSteps();
  assert.equal(declared.length, 6, `step 数量应为 6，实测 ${declared.length}`);
  const names = declared.map((step) => step.name);
  assert.equal(new Set(names).size, names.length, `step 名称必须唯一：${names.join(", ")}`);
  for (const step of declared) {
    assert.ok(step.name, "每个 step 必须有 name（诊断用，refusedBy 会带上它）");
    assert.ok(step.phases.includes("begin"), `${step.name} 缺少 begin`);
    assert.ok(step.phases.includes("settle"), `${step.name} 缺少 settle`);
    for (const phase of step.phases) {
      assert.ok(
        ["begin", "commit", "settle"].includes(phase),
        `${step.name} 声明了未知相位 ${phase}`,
      );
    }
  }

  // root-modal 必须排在第一位：它同步撤销旧的 source-add lease，其 close sink
  // （resetStagedIntake）是暂存文件 / bundle 勾选 resolver / 迟到解包世代的唯一清理
  // 路径。挪到后面会让这些清理发生在别的 owner 已经开始换代之后。
  assert.equal(names[0], "root-modals", `root-modal 必须是第一个 begin：${names.join(", ")}`);
});


test("openNotebook 恰好经 runNotebookTransition 消费这份 step 列表一次", () => {
  const inOpen = calls.filter((entry) => entry.scope === "<module>.Home.openNotebook");
  const runs = inOpen
    .filter((entry) => entry.target === "runNotebookTransition")
    .reduce((sum, entry) => sum + entry.count, 0);
  assert.equal(runs, 1, `openNotebook 应恰好调用一次 runNotebookTransition，实测 ${runs}`);
  const buildsSteps = inOpen
    .filter((entry) => entry.target === "notebookTransitionSteps")
    .reduce((sum, entry) => sum + entry.count, 0);
  assert.equal(buildsSteps, 1, `openNotebook 应恰好构造一次 step 列表，实测 ${buildsSteps}`);
});


test("编排模块是纯逻辑：不 import React，也不 import 任何 app 模块", () => {
  const modules = importsIn(orchestrator).map((item) => item.module);
  assert.deepEqual(
    modules,
    [],
    `notebook-transition.ts 必须零依赖（纯逻辑）：${modules.join(", ")}`,
  );
  const text = orchestrator.getText(orchestrator);
  for (const forbidden of ["useState", "useRef", "useEffect", "window.", "document.", "fetch("]) {
    assert.equal(
      text.includes(forbidden),
      false,
      `notebook-transition.ts 不得触达 React/DOM/网络：${forbidden}`,
    );
  }
});
