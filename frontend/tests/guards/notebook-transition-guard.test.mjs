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
  callSitesIn,
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

  // step 顺序整体钉住，不只是「root-modal 排第一」：顺序同时决定 begin 顺序、commit
  // 顺序（编排器按声明序逐个 await 有 Promise 返回值的 commit）与 settle 顺序（逆序）。
  // root-modal 必须最先 begin——它同步撤销旧的 source-add lease，其 close sink
  // （resetStagedIntake）是暂存文件 / bundle 勾选 resolver / 迟到解包世代的唯一清理
  // 路径，挪到后面会让这些清理发生在别的 owner 已经开始换代之后。source-library 必须
  // 排在 ask-session 之前——它的 commit（commitNotebookSnapshot）是同步的，
  // ask-session 的 commit（restoreNotebook）返回 Promise、会被 await；commit 循环
  // 按声明序执行，source-library 排在 ask-session 之前保证快照的同步提交先发生，不
  // 会被 ask-session 那次 await 插入的微任务窗口截胡——挪到之后，快照提交就会跨过一
  // 个可被顶替的窗口。
  assert.deepEqual(
    names,
    [
      "root-modals",
      "workspace-extensions",
      "source-library",
      "report-workspace",
      "kg-workspace",
      "ask-session",
    ],
    `step 顺序必须整体钉住：${names.join(", ")}`,
  );
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


test("plan 的四个相位钉在具名 helper 上，不是内联展开的临时逻辑", () => {
  const openNotebook = findFunctionIn(page, "Home", "openNotebook");
  const runCall = callSitesIn(openNotebook).find(
    (call) => call.target === "runNotebookTransition",
  );
  assert.ok(runCall, "openNotebook 必须调用 runNotebookTransition");
  assert.equal(runCall.arguments.length, 1, "runNotebookTransition 只接一个 plan 参数");
  const [plan] = runCall.arguments;

  assert.match(
    plan,
    /\benter: enterNotebookTransition\b/,
    `enter 的初始值必须直接引用 enterNotebookTransition（不得内联展开成另一段逻辑）：${plan}`,
  );
  assert.match(
    plan,
    /\bload: \(\) => openNotebookSnapshot\(/,
    `load 的箭头函数体必须调用 openNotebookSnapshot：${plan}`,
  );
  assert.match(
    plan,
    /\bapply: [^,]*=> applyOpenedNotebook\(/,
    `apply 必须调用 applyOpenedNotebook：${plan}`,
  );
  assert.match(
    plan,
    /\bconclude: \(\) => concludeOpenNotebook\(/,
    `conclude 必须调用 concludeOpenNotebook：${plan}`,
  );
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


// ---------------------------------------------------------------------------
// deny-by-default：上面「owner 生命周期调用只能出现在 step 列表里」那条只查了五个
// 编排作用域体内——一个新函数从 openNotebook 里调出去、在自己体内直接触达某个 owner
// 的生命周期，五个编排作用域一个都不覆盖它，会完全放行。下面这条把同一个
// isLifecycleCall 判据铺到**整个 page.tsx**：每一个匹配到的调用点，它所在的最近命名
// 作用域都必须出现在这份显式 allowlist 里，否则判为 rogue。
//
// allowlist 是跑一遍真实扫描后逐条核实写下的（node ./probe.mjs 列出
// isLifecycleCall 命中的全部作用域，再对每一条判断是否与「打开笔记本」这条
// transition 无关、是否属于其它已审阅过的功能）：
const OWNER_LIFECYCLE_SCOPE_ALLOWLIST = {
  // 打开笔记本 transition 的 step 列表——本文件要保护的那个单点。
  "<module>.Home.notebookTransitionSteps":
    "打开笔记本 transition 的 step 列表本身。",
  // 三个 owner-hook 扇出入口（actor / workspace 生命周期，与打开哪一本笔记本无关）。
  // workspace-owner-transition-guard.test.mjs 已经从另一个角度钉死这三个函数是
  // activateActor/leaveWorkspace/leaveActor/abortForLogout 的唯一发出点；这里只是
  // 承认它们也会命中本文件更宽的 LIFECYCLE 前缀判据。
  "<module>.Home.activateWorkspaceOwners":
    "登录成功后把 actor 接上全部 owner hook（activateActor）。",
  "<module>.Home.leaveWorkspaceOwners":
    "回集合页——放弃 workspace、保留 actor（leaveWorkspace）。",
  "<module>.Home.leaveActorOwners":
    "登出——放弃 actor 本身（leaveActor/leaveWorkspace）。",
  // notebookCollection 自己的取数生命周期：管的是笔记本清单的读取快照，不是打开
  // 单本笔记本。
  "<module>.Home.loadNotebookCollection":
    "notebookCollection.beginListRead/commitListSnapshot 管笔记本清单快照，"
      + "与打开单本笔记本的 transition 无关。",
  // sourceLibrary.beginTransition() 是幂等的「重置来源库状态」调用，不携带有意义的
  // ticket、没有对应的 settle。下面两处各自独立调用它清理来源库状态，不是
  // notebookTransitionSteps 那条 transition 的一部分。
  "<module>.Home.showCollection": "离开笔记本回集合页时重置来源库状态。",
  "<module>.Home.handleLogout": "登出时重置来源库状态。",
  // 切换 / 新建 Ask 会话时用 rootModals 的 begin/finishWorkspaceTransition 保护
  // source-add lease，是与「打开笔记本」平行的另一条 workspace transition，不经过
  // notebookTransitionSteps。
  "<module>.Home.openAskSession": "切换 Ask 会话时保护 source-add lease。",
  "<module>.Home.startNewAskSession": "新建 Ask 会话时保护 source-add lease。",
  // 上传 / URL 导入来源的提交，是 sourceLibrary 自己的「新增来源」写入路径，与打开
  // 笔记本无关。applyImportedUrlSources 是 URL 导入成功半的抽出函数(X9 PR-A T3)——
  // submitUrlSources(粘贴链接框)与 importGapSuggestion(站外来源建议的「导入」按钮,
  // ask.gap_consult)共用同一份提交逻辑，实际调用 sourceLibrary.commitUrlSources 的
  // 作用域因此从 submitUrlSources 移到了这里。
  "<module>.Home.confirmUploadInner": "文件上传成功后提交新来源。",
  "<module>.Home.applyImportedUrlSources":
    "URL 导入成功后提交新来源(submitUrlSources 与 importGapSuggestion 共用)。",
  // 会话重命名按钮直接内联写在 JSX 事件处理器里，不在任何具名函数体内，最近命名
  // 作用域因而就是 Home 本身；askSession.beginRenameSession/commitRenameSession 是
  // 会话重命名这个独立小功能的生命周期，与打开笔记本无关。
  "<module>.Home": "会话重命名按钮（内联 JSX handler）。",
};

test("owner 生命周期调用（OWNER_HOOKS × LIFECYCLE）整份 page.tsx deny-by-default", () => {
  const rogue = calls.filter((entry) => (
    isLifecycleCall(entry.target)
      && !Object.prototype.hasOwnProperty.call(OWNER_LIFECYCLE_SCOPE_ALLOWLIST, entry.scope)
  ));
  assert.deepEqual(
    rogue,
    [],
    "出现了未登记的 owner 生命周期调用作用域。新增调用点要么挪进 "
      + "notebookTransitionSteps，要么在 OWNER_LIFECYCLE_SCOPE_ALLOWLIST 补一条并写明"
      + `理由：${JSON.stringify(rogue)}`,
  );
});


test("allowlist 没有失效条目：每一项都至少命中一次真实的生命周期调用", () => {
  const matchedScopes = new Set(
    calls.filter((entry) => isLifecycleCall(entry.target)).map((entry) => entry.scope),
  );
  for (const scope of Object.keys(OWNER_LIFECYCLE_SCOPE_ALLOWLIST)) {
    assert.ok(
      matchedScopes.has(scope),
      `allowlist 条目 ${scope} 没有命中任何真实调用，应删除（否则将来会掩盖一次本该报红的挪动）`,
    );
  }
});
