import test from "node:test";
import assert from "node:assert/strict";

import {
  callSitesIn,
  findFunctionIn,
  ifConditionsIn,
  importsIn,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const hook = await parseModule("use-root-modal-coordinator.ts");
const modelPanel = await parseModule("model-service-panel.tsx");
const passwordModal = await parseModule("password-change-modal.tsx");
const searchProfileModal = await parseModule("search-profile-modal.tsx");
const memoryPanel = await parseModule("memory-panel.tsx");
const catalogPanel = await parseModule("command-catalog-panel.tsx");
const conversationShare = await parseModule("conversation-share-modal.tsx");
const pageText = page.getText(page);
const hookText = hook.getText(hook);

test("page composes one typed root-modal coordinator and has no legacy modal booleans", () => {
  assert.equal((pageText.match(/useRootModalCoordinator\(/g) ?? []).length, 1);
  for (const legacy of [
    "passwordModalOpen", "searchProfileModalOpen", "sourceModalOpen",
    "modelPanelOpen", "sharedByMeOpen", "promoOpen", "edgeReviewOpen",
    "understandingOpen",
  ]) assert.equal(pageText.includes(legacy), false, legacy);
  for (const slot of ["notebook-editor", "notebook-delete", "source-detail", "kg-schema", "kg-analysis"]) {
    assert.match(pageText, new RegExp(`rootModals\\.view\\(\\"${slot}\\"\\)`), slot);
  }
  // 与标识符无关的判据(取代上面三条已随 editingNotebook/deleteNotebook/
  // schemaModalOpen 被删除而永真的 doesNotMatch):page.tsx 里除 kg-view(知识图谱
  // 主视图,见下方说明)外,任何 role="dialog" 曲面的 aria-modal 都不许是静态字面量
  // "true"/"false",必须是 rootModals.view("<slot>").topmost 表达式。这样不论回潮
  // 时用的是哪个已删的旧布尔变量名(不局限于上面三个),都会被这条钉住。
  //
  // kg-view 不是 RootModalSlot(见 use-root-modal-coordinator.ts 的 RootModalSlot
  // 联合类型,其中没有 "kg-view")——它是知识图谱主视图,由 kgGraph.open 直接控制,
  // 独立于本文件描述的 rootModals 协调器边界之外,不参与「被覆盖时退出交互树」的
  // inert/aria-hidden 契约,是先于本次 root-modal-boundary 工作就存在的既有设计。
  // 用它自己的结构标记(className="kg-view")整体挖掉后,残留文本里不该再出现任何
  // 静态 aria-modal 字面量。
  const pageTextWithoutKgView = pageText.replace(
    /<section className="kg-view" role="dialog" aria-modal="true">/,
    "",
  );
  assert.doesNotMatch(pageTextWithoutKgView, /aria-modal="(?:true|false)"/);
  assert.match(pageText, /<SourceDetailWindow[\s\S]{0,240}interactive=\{rootModals\.view\("source-detail"\)\.topmost\}/);
  assert.match(pageText, /<KgAnalysisView[\s\S]{0,300}interactive=\{rootModals\.view\("kg-analysis"\)\.topmost\}/);
});

test("coordinator is presentation-only: React is its sole dependency and it owns no I/O or timer", () => {
  assert.deepEqual([...new Set(importsIn(hook).map(({ module }) => module))], ["react"]);
  assert.doesNotMatch(hookText, /\b(fetch|setTimeout|setInterval|XMLHttpRequest|AbortController)\b/);
  assert.doesNotMatch(hookText, /\b(Api|Repository|Service|Busy|Payload|setCurrentNotebook)\b/);
});

test("workspace transitions invalidate root slots before old-domain work and close source intake through one sink", () => {
  // 结构项 F3：打开笔记本的编排收成了单一 transition，root-modal 的 begin/settle 只在
  // `notebookTransitionSteps` 的 step 列表里声明（`openNotebook` 自己不再直接触达任何
  // owner hook 生命周期——notebook-transition-guard 钉住这条）。root-modal 必须排在
  // 列表**第一位**：它同步撤销旧的 source-add lease，其 close sink 是暂存文件 /
  // bundle 勾选 resolver / 迟到解包世代的唯一清理路径。
  const openCalls = callSitesIn(findFunctionIn(page, "Home", "notebookTransitionSteps"))
    .map(({ target }) => target);
  assert.ok(openCalls.includes("rootModals.beginWorkspaceTransition"));
  assert.ok(openCalls.includes("rootModals.finishWorkspaceTransition"));
  // rootModals.leaveWorkspace 现在只经共享函数 leaveWorkspaceOwners 间接调用；
  // 守卫 workspace-owner-transition-guard 钉住「只能从那里发出」。
  const collectionCalls = callSitesIn(findFunctionIn(page, "Home", "showCollection")).map(({ target }) => target);
  assert.ok(collectionCalls.includes("leaveWorkspaceOwners"));
  const leaveWorkspaceOwnersCalls = callSitesIn(findFunctionIn(page, "Home", "leaveWorkspaceOwners")).map(({ target }) => target);
  assert.ok(leaveWorkspaceOwnersCalls.includes("rootModals.leaveWorkspace"));
  const closeCalls = callSitesIn(findFunctionIn(page, "Home", "handleRootModalClosed")).map(({ target }) => target);
  assert.ok(closeCalls.includes("resetStagedIntake"));
  assert.ok(closeCalls.includes("setLinkSectionOpen"));
  const closeText = findFunctionIn(page, "Home", "handleRootModalClosed").getText(page);
  assert.match(
    closeText,
    /case "source-add":[\s\S]*?resetStagedIntake\(\);[\s\S]*?setLinkSectionOpen\(false\);[\s\S]*?sourceModalDismissedRef\.current = true;[\s\S]*?return;/,
  );
  assert.match(closeText, /case "info":[\s\S]*?setInfoModal\(null\);[\s\S]*?return;/);
  assert.doesNotMatch(closeText, /case "info":[\s\S]*?closeInfoModal\(\);[\s\S]*?return;/);
});

test("authenticated bootstrap activates modal authority before publishing the user", () => {
  // rootModals.activateActor/leaveActor 现在只经共享函数
  // activateWorkspaceOwners/leaveActorOwners 间接调用；守卫
  // workspace-owner-transition-guard 钉住「只能从那里发出」。
  const activateOwnersCalls = callSitesIn(findFunctionIn(page, "Home", "activateWorkspaceOwners")).map(({ target }) => target);
  assert.ok(activateOwnersCalls.includes("rootModals.activateActor"));
  assert.equal(
    (pageText.match(/activateWorkspaceOwners\(u\.id\);[\s\S]{0,220}setCurrentUser\(u\)/g) ?? []).length,
    2,
  );
  const logoutCalls = callSitesIn(findFunctionIn(page, "Home", "handleLogout")).map(({ target }) => target);
  assert.ok(logoutCalls.includes("leaveActorOwners"));
  const leaveActorOwnersCalls = callSitesIn(findFunctionIn(page, "Home", "leaveActorOwners")).map(({ target }) => target);
  assert.ok(leaveActorOwnersCalls.includes("rootModals.leaveActor"));
  assert.ok(logoutCalls.includes("logoutUser"));
});

test("deferred root openers publish frozen tickets instead of opening from live state", () => {
  for (const name of ["openShareModal", "openSharedByMe", "openPromoQueue", "openEdgeReviewQueue"]) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    assert.ok(calls.includes("rootModals.issue"), name);
    assert.ok(calls.includes("rootModals.publish"), name);
  }
  assert.match(pageText, /previewShared\(token\)[\s\S]*rootModals\.publish\(modalLease\)/);
  for (const name of ["presentNotebookEditor", "presentNotebookDelete"]) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    assert.ok(calls.includes("rootModals.issue"), name);
    assert.ok(calls.includes("rootModals.publish"), name);
  }
  const sourceCalls = callSitesIn(findFunctionIn(page, "Home", "openSourceDetailById")).map(({ target }) => target);
  assert.ok(sourceCalls.includes("rootModals.issue"));
  assert.ok(sourceCalls.includes("rootModals.publish"));
});

test("collection and KG payload owners are coordinated only through typed presentation adapters", () => {
  const expected = new Map([
    ["openKgSchemas", ["rootModals.open", "kgWorkspace.openSchemas"]],
    ["openKgAnalysis", ["rootModals.open", "kgWorkspace.openAnalysis"]],
    ["closeKgSchemas", ["rootModals.requestClose"]],
    ["closeKgAnalysis", ["rootModals.requestClose"]],
  ]);
  for (const [name, targets] of expected) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    for (const target of targets) assert.ok(calls.includes(target), `${name}: ${target}`);
  }
  const closeCalls = callSitesIn(findFunctionIn(page, "Home", "handleRootModalClosed"))
    .map(({ target }) => target);
  for (const target of [
    "notebookCollection.closeEditor",
    "notebookCollection.closeDelete",
    "kgWorkspace.closeSchemas",
    "kgWorkspace.closeAnalysis",
  ]) assert.ok(closeCalls.includes(target), target);
});

test("only the coordinator top layer drives the model focus trap", () => {
  assert.match(pageText, /interactive=\{rootModals\.view\("model-service"\)\.topmost\}/);
  assert.match(modelPanel.getText(modelPanel), /if \(!interactive\) return;/);
  assert.match(modelPanel.getText(modelPanel), /onClose\("escape"\)/);
});

// page.tsx 不再把每个 slot 的 view() 摊平成局部别名，每个使用点都直接
// `rootModals.view("<slot>")`——下面两张表因此改按 slot id 生成正则，而不是按
// 别名标识符。
test("every coordinated root surface leaves the interaction tree when it is covered", () => {
  const inlineSlots = [
    "notebook-share", "shared-preview", "shared-by-me", "source-add",
    "notebook-editor", "notebook-delete", "info", "analytics",
    "kg-schema", "understanding", "promotion-queue", "promotion-target",
    "edge-review",
  ];
  for (const slot of inlineSlots) {
    const view = `rootModals\\.view\\("${slot}"\\)`;
    assert.match(
      pageText,
      new RegExp(`aria-modal=\\{${view}\\.topmost\\}[\\s\\S]{0,180}aria-hidden=\\{!${view}\\.topmost\\}[\\s\\S]{0,180}inert=\\{${view}\\.topmost \\? undefined : true\\}`),
      slot,
    );
  }
  const componentBindings = [
    ["PasswordChangeModal", "password-change"],
    ["SearchProfileModal", "search-profile"],
    ["MemorySaveDialog", "memory-save"],
    ["CommandCatalogReview", "catalog-review"],
    ["ConversationShareModal", "conversation-share"],
    ["SourceDetailWindow", "source-detail"],
    ["KgAnalysisView", "kg-analysis"],
    ["ImagePreviewModal", "answer-image-preview"],
  ];
  for (const [component, slot] of componentBindings) {
    const view = `rootModals\\.view\\("${slot}"\\)`;
    assert.match(
      pageText,
      new RegExp(`<${component}[\\s\\S]{0,700}interactive=\\{${view}\\.topmost\\}[\\s\\S]{0,160}zIndex=\\{${view}\\.zIndex\\}`),
      component,
    );
  }
  for (const source of [passwordModal, searchProfileModal, memoryPanel, catalogPanel, conversationShare]) {
    const text = source.getText(source);
    assert.match(text, /aria-hidden=\{!interactive\}/);
    assert.match(text, /inert=\{interactive \? undefined : true\}/);
  }
});

test("presentation close never releases an in-flight domain operation", () => {
  const close = findFunctionIn(page, "Home", "handleRootModalClosed");
  const text = close.getText(page);
  assert.doesNotMatch(text, /OperationRef\.current\s*=\s*null/);
  assert.doesNotMatch(text, /set(?:Share|Promo|Edge)Busy\(false\)/);
  for (const [name, operation] of [
    ["openShareModal", "shareOperationRef.current"],
    ["openSharedByMe", "shareOperationRef.current"],
    ["openPromoQueue", "promoOperationRef.current"],
    ["openEdgeReviewQueue", "edgeOperationRef.current"],
  ]) {
    assert.ok(
      ifConditionsIn(findFunctionIn(page, "Home", name))
        .some((condition) => condition.includes(operation)),
      `${name} must not replace an in-flight domain operation`,
    );
  }
});

test("modal mutations suppress errors after their frozen lease becomes stale", () => {
  for (const name of [
    "enableShareLink",
    "handleUnshare",
    "handleUnshareFromOverview",
    "decidePromotion",
    "decideEdge",
  ]) {
    const text = findFunctionIn(page, "Home", name).getText(page);
    assert.match(text, /catch \(error\)[\s\S]*rootModals\.owns\(modalLease\)[\s\S]*throw error/, name);
  }
  const promotion = findFunctionIn(page, "Home", "submitPromotion").getText(page);
  assert.match(promotion, /const actorId = currentUser\?\.id/);
  assert.match(promotion, /const workspaceEpoch = workspaceEpochRef\.current/);
  assert.match(promotion, /if \(isCurrent\(\)\) setToast/);
  assert.match(promotion, /catch \(error\)[\s\S]*if \(isCurrent\(\)\) throw error/);
  assert.match(pageText, /const \[catalogReviewLease, setCatalogReviewLease\] = useState<RootModalLease<"catalog-review"> \| null>/);
  assert.match(pageText, /<CommandCatalogReview[\s\S]{0,700}isCurrent=\{\(\) => rootModals\.owns\(catalogReviewLease\)\}/);
  const catalogText = catalogPanel.getText(catalogPanel);
  assert.match(catalogText, /const operationIsCurrent = \(\) => reviewAliveRef\.current && isCurrent\(\)/);
  assert.match(catalogText, /const result = await applyCommandCatalog[\s\S]{0,180}if \(!operationIsCurrent\(\)\) return/);
  assert.match(catalogText, /const result = await dismissCommandCatalog[\s\S]{0,180}if \(!operationIsCurrent\(\)\) return/);
});

// 无退路弹窗守卫 —— 「忙碌时把关闭入口一起 disable」会把用户锁死在弹窗里。
//
// 背景：notebook-editor / notebook-delete 这两个 slot 在 ROOT_MODAL_POLICIES 里
// escape 与 backdrop 都是 false，而 `.utility-modal` 又是 `inset: 0` 的全屏遮罩——
// 页面背后点不到、Esc 没反应、点遮罩不关。这种形态下，关闭入口是弹窗**唯一**的出口。
// 一旦它跟着「保存中/删除中」一起被禁用，一个永不 settle 的请求（客户端没有超时）就只剩
// 刷新页面一条路，用户已填好的表单内容也随之丢掉。
//
// 判据：凡 onClick 打的是这两个 slot 的 requestClose 的 <button>，都不许带 disabled。
// 按 onClick 源码文本认身份，不认行号也不认按钮文案，按钮在文件里挪动或改文案都不报红。
//
// ⚠ 这条**不是**说「所有弹窗按钮都不该禁用」：同一个弹窗里的「保存 / 确认」照旧必须
// 带 disabled（那是防重复提交，由 long-task-button-guard 与既有组件测试覆盖），被钉住
// 的只有「离开这个弹窗」这一个动作。
test("无 Escape / 无遮罩关闭的弹窗，其关闭入口不得被忙碌位禁用", () => {
  const buttons = jsxElements(page, "button");
  for (const slot of ["notebook-editor", "notebook-delete"]) {
    // 前提：这两个 slot 确实两条退路都没有。哪天给它们开了 Escape，这条断言会先红，
    // 提醒重新判断下面那条「关闭入口是唯一出口」还成不成立。
    assert.match(
      hookText,
      new RegExp(`"${slot}": \\{[^}]*backdrop: false, escape: false`),
      `${slot} 的策略不再是「无遮罩关闭 + 无 Escape」，请重新评估本守卫的前提`,
    );
    const dismissals = buttons.filter((element) => (
      (element.bindings?.onClick ?? element.attributes?.onClick ?? "")
        .includes(`rootModals.requestClose("${slot}", "button")`)
    ));
    assert.ok(dismissals.length >= 2, `${slot}: 期望至少「×」与「取消」两个关闭入口`);
    for (const dismissal of dismissals) {
      const disabled = dismissal.bindings?.disabled ?? dismissal.attributes?.disabled;
      assert.equal(
        disabled,
        undefined,
        `${slot}: 关闭入口带上了 disabled=${disabled}，忙碌时弹窗将没有任何出口`,
      );
    }
  }
});
