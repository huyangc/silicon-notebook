import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import {
  callSitesIn,
  findFunction,
  findFunctionIn,
  ifConditionsIn,
  importsIn,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";

// `handleRootModalClosed`'s switch has one CaseClause per slot, each ending in
// its own `return` (no fallthrough) — so a CaseClause node's own statements
// are exactly that slot's close-sink body. Used below to pin the
// promotion-queue/edge-review case blocks to *exactly* `clearQueue()`: the
// existing `closeCalls.includes("promotionQueue.clearQueue")` check only
// proves that call is *somewhere* in the whole switch, so a second call
// (e.g. a `releaseAfterClose()` slipped in next to `clearQueue()`) would not
// be caught by it.
function caseClauseIn(functionNode, label) {
  let match;
  function visit(node) {
    if (match) return;
    if (
      ts.isCaseClause(node)
      && ts.isStringLiteral(node.expression)
      && node.expression.text === label
    ) {
      match = node;
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(functionNode);
  if (!match) {
    throw new Error(`case clause not found: "${label}"`);
  }
  return match;
}

const page = await parseModule("page.tsx");
const hook = await parseModule("use-root-modal-coordinator.ts");
const modelPanel = await parseModule("model-service-panel.tsx");
const passwordModal = await parseModule("password-change-modal.tsx");
const searchProfileModal = await parseModule("search-profile-modal.tsx");
const releaseNotesModal = await parseModule("release-notes-modal.tsx");
const memoryPanel = await parseModule("memory-panel.tsx");
const catalogPanel = await parseModule("command-catalog-panel.tsx");
const conversationShare = await parseModule("conversation-share-modal.tsx");
// PR-5 分片 1：晋升队列与关系审核队列从 page.tsx 搬到各自的 owner hook + 弹窗组件。
// 下面凡是原本按「函数住在 page.tsx 的 Home 里」认身份的断言，只改「去哪个模块找」，
// 判据本身一条不变。
const promotionQueueHook = await parseModule("use-promotion-queue.ts");
const promotionQueueModal = await parseModule("promotion-queue-modal.tsx");
const edgeReviewHook = await parseModule("use-edge-review-queue.ts");
const edgeReviewModal = await parseModule("edge-review-modal.tsx");
// 知识图谱主视图搬进了自己的模块（PR-5 分片 3）；page.tsx 仍持有它的开合门与
// 「图谱分析」弹窗的 rootModals 编排（作为 children 原位渲染在 .kg-view 内）。
const kgGraphView = await parseModule("kg-graph-view.tsx");
// 网页导出 / 导入的两个根弹窗(notebook-export / notebook-import)。
const transferPanel = await parseModule("notebook-transfer-panel.tsx");
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
  // schemaModalOpen 被删除而永真的 doesNotMatch):page.tsx 里任何 role="dialog"
  // 曲面的 aria-modal 都不许是静态字面量 "true"/"false",必须是
  // rootModals.view("<slot>").topmost 表达式。这样不论回潮时用的是哪个已删的旧布尔
  // 变量名(不局限于上面三个),都会被这条钉住。
  //
  // kg-view 曾是这条的唯一例外(靠 className="kg-view" 挖洞豁免);它整块搬去
  // kg-graph-view.tsx 之后,page 侧不再需要例外——判据因此比原来**更紧**。kg-view
  // 自身的形状(它不是 RootModalSlot,见 use-root-modal-coordinator.ts 的
  // RootModalSlot 联合类型,其中没有 "kg-view";它是由 kgGraph.open 直接控制的知识
  // 图谱主视图,不参与「被覆盖时退出交互树」的 inert/aria-hidden 契约,是先于
  // root-modal-boundary 工作就存在的既有设计)在新模块上原样钉住,并要求它是那里
  // 唯一的静态 aria-modal。
  assert.doesNotMatch(pageText, /aria-modal="(?:true|false)"/);
  const kgViewText = kgGraphView.getText(kgGraphView);
  assert.match(kgViewText, /<section className="kg-view" role="dialog" aria-modal="true">/);
  assert.doesNotMatch(
    kgViewText.replace(/<section className="kg-view" role="dialog" aria-modal="true">/, ""),
    /aria-modal="(?:true|false)"/,
  );
  // 防回填:视图曲面不许再长回 page.tsx。
  assert.doesNotMatch(pageText, /className="kg-view"/);
  assert.doesNotMatch(pageText, /<ForceGraph2D/);
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
  for (const name of ["openShareModal", "openSharedByMe"]) {
    const calls = callSitesIn(findFunctionIn(page, "Home", name)).map(({ target }) => target);
    assert.ok(calls.includes("rootModals.issue"), name);
    assert.ok(calls.includes("rootModals.publish"), name);
  }
  // 两个治理队列的开窗搬进了各自的 owner hook；协调器在那里是注入进来的窄命令面
  // `modals`，所以调用目标从 `rootModals.*` 变成 `modals.*`，判据（issue → publish 的
  // 冻结票据）不变。
  for (const [module, parent, name] of [
    [promotionQueueHook, "usePromotionQueue", "openPromoQueue"],
    [edgeReviewHook, "useEdgeReviewQueue", "openEdgeReviewQueue"],
  ]) {
    const calls = callSitesIn(findFunctionIn(module, parent, name)).map(({ target }) => target);
    assert.ok(calls.includes("modals.issue"), name);
    assert.ok(calls.includes("modals.publish"), name);
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

// primary 之间的共存例外只给「往下钻」的那几对:来源详情 ↔ 命令目录审阅(原有),以及
// 图谱分析的问题清单「打开来源核对」带出来的两对——来源详情叠在分析面板上层、来源详情
// 再开命令目录审阅时面板仍在。多一对就会有别的弹窗不再互斥,少一对面板就会被自己带出来的
// 下一层关掉(关掉来源详情后也就回不到原来那一条问题)。行为在协调器组件测试里钉。
test("primary coexistence is limited to the directed drill-down pairs", () => {
  const block = hookText.match(/const COEXISTING_PRIMARY_PAIRS[^=]*= \[([\s\S]*?)\];/);
  assert.ok(block, "COEXISTING_PRIMARY_PAIRS 不见了");
  const pairs = [...block[1].matchAll(/\["([\w-]+)", "([\w-]+)"\]/g)].map((match) => [match[1], match[2]]);
  // [已开着的, 正要开的]。分析面板只作为「已开着的」一方出现:它画在知识图谱视图的层叠
  // 上下文里,来源详情已开时再开它必须照旧把来源详情冲突关掉。
  assert.deepEqual(pairs, [
    ["source-detail", "catalog-review"],
    ["catalog-review", "source-detail"],
    ["kg-analysis", "source-detail"],
    ["kg-analysis", "catalog-review"],
  ]);
  // 函数体就是这一条有向查表,不许在后面再或上别的例外。
  const coexist = findFunction(hook, "primarySlotsMayCoexist").getText(hook).replace(/\s+/g, " ");
  assert.equal(
    coexist,
    "function primarySlotsMayCoexist(open: RootModalSlot, opening: RootModalSlot): boolean { return COEXISTING_PRIMARY_PAIRS.some(([kept, incoming]) => open === kept && opening === incoming); }",
  );
  // 面板里的「打开来源核对」走既有的冻结票据开窗,且不关面板。
  const calls = callSitesIn(findFunctionIn(page, "Home", "openSourceFromAnalysis")).map(({ target }) => target);
  assert.deepEqual(calls, ["openSourceDetailById"]);
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
    // promotion-queue / edge-review 不再内联在 page.tsx——它们的 section 随 PR-5 分片 1
    // 搬进了 PromotionQueueModal / EdgeReviewModal，改由下面 componentBindings 那张表钉住
    // （与 memory-save、kg-analysis 等既有组件化弹窗同一判据）。
    "kg-schema", "understanding", "promotion-target",
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
    ["ReleaseNotesModal", "release-notes"],
    ["SearchProfileModal", "search-profile"],
    ["MemorySaveDialog", "memory-save"],
    ["CommandCatalogReview", "catalog-review"],
    ["ConversationShareModal", "conversation-share"],
    ["SourceDetailWindow", "source-detail"],
    ["KgAnalysisView", "kg-analysis"],
    ["ImagePreviewModal", "answer-image-preview"],
    ["PromotionQueueModal", "promotion-queue"],
    ["EdgeReviewModal", "edge-review"],
    ["NotebookExportModal", "notebook-export"],
    ["NotebookImportModal", "notebook-import"],
  ];
  for (const [component, slot] of componentBindings) {
    const view = `rootModals\\.view\\("${slot}"\\)`;
    assert.match(
      pageText,
      new RegExp(`<${component}[\\s\\S]{0,700}interactive=\\{${view}\\.topmost\\}[\\s\\S]{0,160}zIndex=\\{${view}\\.zIndex\\}`),
      component,
    );
  }
  for (const source of [
    passwordModal, searchProfileModal, releaseNotesModal, memoryPanel, catalogPanel, conversationShare,
    promotionQueueModal, edgeReviewModal, transferPanel,
  ]) {
    const text = source.getText(source);
    assert.match(text, /aria-hidden=\{!interactive\}/);
    assert.match(text, /inert=\{interactive \? undefined : true\}/);
  }
  // 一个模块里住着两个根弹窗(导出 / 导入):上面的 match 只证明「至少有一处」,必须逐个都有。
  const transferText = transferPanel.getText(transferPanel);
  assert.equal((transferText.match(/aria-hidden=\{!interactive\}/g) ?? []).length, 2);
  assert.equal((transferText.match(/inert=\{interactive \? undefined : true\}/g) ?? []).length, 2);
});

test("presentation close never releases an in-flight domain operation", () => {
  const close = findFunctionIn(page, "Home", "handleRootModalClosed");
  const text = close.getText(page);
  assert.doesNotMatch(text, /OperationRef\.current\s*=\s*null/);
  assert.doesNotMatch(text, /set(?:Share|Promo|Edge)Busy\(false\)/);
  for (const [name, operation] of [
    ["openShareModal", "shareOperationRef.current"],
    ["openSharedByMe", "shareOperationRef.current"],
  ]) {
    assert.ok(
      ifConditionsIn(findFunctionIn(page, "Home", name))
        .some((condition) => condition.includes(operation)),
      `${name} must not replace an in-flight domain operation`,
    );
  }
  // 两个治理队列的 close sink 现在只调 owner hook 的 clearQueue（page 侧那两条 case 就是
  // 它）。同一条「关闭 ≠ 请求结束」的判据因此下沉一层：clearQueue 只许清可见载荷，不许
  // 把单飞闸置空、也不许复位 busy——否则关掉弹窗就能让同一候选被批准两次。
  for (const [module, parent, opener, operation] of [
    [promotionQueueHook, "usePromotionQueue", "openPromoQueue", "promoOperationRef.current"],
    [edgeReviewHook, "useEdgeReviewQueue", "openEdgeReviewQueue", "edgeOperationRef.current"],
  ]) {
    assert.ok(
      ifConditionsIn(findFunctionIn(module, parent, opener))
        .some((condition) => condition.includes(operation)),
      `${opener} must not replace an in-flight domain operation`,
    );
    const clearText = findFunctionIn(module, parent, "clearQueue").getText(module);
    assert.doesNotMatch(clearText, /OperationRef\.current\s*=\s*null/, parent);
    assert.doesNotMatch(clearText, /set(?:Promo|Edge)Busy\(false\)/, parent);
  }
  const closeCalls = callSitesIn(close).map(({ target }) => target);
  assert.ok(closeCalls.includes("promotionQueue.clearQueue"));
  assert.ok(closeCalls.includes("edgeReview.clearQueue"));
  // 逐字比对整个 case 块的调用清单(不是"清单里有没有它"),这样紧挨着
  // clearQueue() 多塞一条 releaseAfterClose() 之类的调用(把 operationRef 置空
  // /把 busy 复位)也会被抓到——不再只靠上面两条 includes。
  for (const [label, expectedCalls] of [
    ["promotion-queue", ["promotionQueue.clearQueue"]],
    ["edge-review", ["edgeReview.clearQueue"]],
  ]) {
    const clauseCalls = callSitesIn(caseClauseIn(close, label)).map(({ target }) => target);
    assert.deepEqual(clauseCalls, expectedCalls, label);
  }
});

test("modal mutations suppress errors after their frozen lease becomes stale", () => {
  for (const name of [
    "enableShareLink",
    "handleUnshare",
    "handleUnshareFromOverview",
  ]) {
    const text = findFunctionIn(page, "Home", name).getText(page);
    assert.match(text, /catch \(error\)[\s\S]*rootModals\.owns\(modalLease\)[\s\S]*throw error/, name);
  }
  // 同一条判据，随两个治理队列搬到各自的 owner hook；协调器在那里叫 `modals`。
  {
    const text = findFunctionIn(edgeReviewHook, "useEdgeReviewQueue", "decideEdge").getText(edgeReviewHook);
    assert.match(text, /catch \(error\)[\s\S]*modals\.owns\(modalLease\)[\s\S]*throw error/, "decideEdge");
  }
  // 内容审核的决策失败不再抛给顶部状态栏，而是把原因落在被点的那一条上(按钮
  // 结果落在按钮旁的仓库规则)。判据不变：票据作废后什么都不写——catch 里先
  // `if (!modals.owns(modalLease)) return;`，重取队列之后再核一次，才写队列与原因。
  {
    const text = findFunctionIn(promotionQueueHook, "usePromotionQueue", "decidePromotion").getText(promotionQueueHook);
    const handler = text.slice(text.indexOf("catch (error)"));
    assert.match(handler, /^catch \(error\) \{\s*if \(!modals\.owns\(modalLease\)\) return;/, "decidePromotion");
    assert.match(
      handler,
      /await fetchPromotionQueue\(\)[\s\S]*if \(!modals\.owns\(modalLease\)\) return;[\s\S]*setPromoQueue[\s\S]*setPromoNotices/,
      "decidePromotion",
    );
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

test("release-notes slot: policy is the actor-owned announcement shape and its close sink stays presentation-only", () => {
  // conflictGroup 为 null:登录后晚到的公告不许挤掉用户此刻已开的 primary 弹窗。
  assert.match(
    hookText,
    /"release-notes": \{ ownerKinds: \["actor"\], conflictGroup: null, layer: 60, backdrop: false, escape: false \}/,
  );
  const sink = caseClauseIn(findFunctionIn(page, "Home", "handleRootModalClosed"), "release-notes").getText(page);
  assert.match(sink, /releaseNotes\.clear\(\);\s*return;/);
  assert.doesNotMatch(sink, /markReleaseNotesSeen|fetch/);
  // 上报「已看」在弹窗自己的关闭函数里,× 与「知道了」共用一条,且两个按钮都不带 disabled。
  const modalText = releaseNotesModal.getText(releaseNotesModal);
  assert.match(modalText, /markReleaseNotesSeen\(build\.ordinal\)/);
  assert.doesNotMatch(modalText, /disabled=/);
});

// 全局问答窗口里的引用配图走的是笔记本页**同一格** answer-image-preview，作为子层叠在
// 窗口之上（`openAbove` + 策略表的 `stacksOver`）。组件测试用的是与 page.tsx 同构的宿主
// 替身，所以 page.tsx 这一侧的接线由这里钉住：断了任何一环，浮窗里的放大按钮就会消失
// （没有承接方）或者点开时把窗口顶掉（不经子层）。
test("global ask hands its citation images to the shared preview slot as a stacked child", async () => {
  const launchers = jsxElements(page, "GlobalAskLauncher");
  assert.equal(launchers.length, 1);
  assert.equal(launchers[0].bindings?.onPreviewImage, "openGlobalAnswerImagePreview");
  assert.equal(launchers[0].bindings?.imagePreviewOpen, 'rootModals.view("answer-image-preview").open');
  const opener = findFunctionIn(page, "Home", "openGlobalAnswerImagePreview").getText(page);
  assert.match(opener, /rootModals\.openAbove\("answer-image-preview", "global-ask"\)/);
  assert.match(hookText, /"answer-image-preview": \{[^}]*stacksOver: \["global-ask"\] \}/);
  // 预览按每一张图自己的取图库挂载与取图：全局问答没有当前笔记本，挂载条件或 URL 只要
  // 退回 `currentNotebookId`，集合页上的预览就永远渲染不出来、或去错的库取图。
  // 判据看的是挂载点到根的**全部**祖先条件（`&&` 的左侧、三元的条件、外层 if），不只
  // 最内层那条 `&&` 链：外面再套一层 `{currentNotebookId && <>…</>}` 一样会让集合页上的
  // 预览永远渲染不出来。page.tsx 里声明的变量别名按初始化式展开（抽变量是合理重构）。
  // 正向：某个祖先条件确实读 `answerImagePreviewImage.notebookId`（`.` 或 `?.`）。
  // 反向：任何祖先条件都不许引用当前笔记本 / 工作区相关的标识——按名字认（含
  // notebook / workspace 的标识符，属性名除外），`Boolean(currentNotebookId)`、
  // `currentNotebook?.id`、`isWorkspace` 这类换个写法绕回当前库的都挡住。
  const homeConsts = new Map();
  (function collect(node) {
    // 只展开「条件别名」：初始化式是表达式而不是一次调用（`rootModals = useRootModalCoordinator(…)`
    // 这类句柄的构造参数与挂载条件无关）。
    if (
      ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer
      && !ts.isCallExpression(node.initializer) && !ts.isNewExpression(node.initializer)
    ) {
      homeConsts.set(node.name.text, node.initializer);
    }
    ts.forEachChild(node, collect);
  })(page);
  const previewConditions = [];
  (function visit(node) {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(page) === "ImagePreviewModal") {
      const conditions = [];
      let child = node;
      for (let cursor = node.parent; cursor; child = cursor, cursor = cursor.parent) {
        if (
          ts.isBinaryExpression(cursor)
          && cursor.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
          && cursor.right === child
        ) conditions.push(cursor.left);
        if (ts.isConditionalExpression(cursor) && cursor.condition !== child) conditions.push(cursor.condition);
        if (ts.isIfStatement(cursor) && cursor.expression !== child) conditions.push(cursor.expression);
      }
      previewConditions.push(conditions);
    }
    ts.forEachChild(node, visit);
  })(page);
  assert.equal(previewConditions.length, 1, "page.tsx 应恰好挂一处 ImagePreviewModal");
  const conditionTexts = [];
  const conditionNames = [];
  const expand = (expression, depth) => {
    conditionTexts.push(expression.getText(page));
    (function names(node) {
      if (ts.isIdentifier(node) && !(ts.isPropertyAccessExpression(node.parent) && node.parent.name === node)) {
        conditionNames.push(node.text);
        const alias = homeConsts.get(node.text);
        if (alias && depth < 3) expand(alias, depth + 1);
      }
      ts.forEachChild(node, names);
    })(expression);
  };
  for (const condition of previewConditions[0]) expand(condition, 0);
  assert.ok(
    conditionTexts.some((text) => /answerImagePreviewImage\??\.notebookId/.test(text)),
    `挂载条件要读这张图自己的取图库：${conditionTexts.join(" ‖ ")}`,
  );
  const currentLibraryNames = conditionNames.filter((name) => /notebook|workspace/i.test(name));
  assert.deepEqual(currentLibraryNames, [], `挂载条件不许依赖当前笔记本 / 工作区：${conditionTexts.join(" ‖ ")}`);
  const previewImages = jsxElements(page, "AuthedImage")
    .filter((element) => (element.bindings?.url ?? "").includes("answerImagePreviewImage"));
  assert.equal(previewImages.length, 1);
  assert.equal(
    previewImages[0].bindings.url,
    "sourceImageAssetUrl(API_BASE, answerImagePreviewImage.notebookId, answerImagePreviewImage.assetId)",
  );
  // 笔记本内那条入口仍是顶层 workspace primary，不经子层。
  const inNotebook = findFunctionIn(page, "Home", "openAnswerImagePreview").getText(page);
  assert.match(inNotebook, /rootModals\.open\("answer-image-preview", rootModals\.captureWorkspaceOwner\(\)\)/);

  // Esc 分层是协调器的通用规则（最上层先吃），全局窗口一侧不许为预览写特判。
  const launcher = await parseModule("ask/global-ask-launcher.tsx");
  const workspace = await parseModule("ask/global-ask-workspace.tsx");
  for (const source of [launcher, workspace]) {
    assert.doesNotMatch(source.getText(source), /answer-image-preview|openAbove/);
  }
  const dialogs = jsxElements(launcher, "dialog");
  assert.equal(dialogs.length, 1);
  for (const handler of ["onCancel", "onKeyDown"]) {
    assert.ok(dialogs[0].bindings?.[handler], `${handler} 不见了`);
    assert.doesNotMatch(dialogs[0].bindings[handler], /preview/i, handler);
  }
});


test("导出 / 导入弹窗没有 Escape 与遮罩退路,它们的「×」任何时候都不禁用", () => {
  // 与上面 notebook-editor / notebook-delete 同一前提:两条退路都关着(导入弹窗里有选好、
  // 还没上传的文件,误触遮罩不该把它丢掉),那么「×」被忙碌位禁用就把用户锁在弹窗里——
  // 而上传、导出、取消时的 30 秒收尾都是会持续很久的在飞状态。作业在服务端接着跑,关掉
  // 弹窗从来不该等它们。
  for (const slot of ["notebook-export", "notebook-import"]) {
    assert.match(
      hookText,
      new RegExp(`"${slot}": \\{[^}]*backdrop: false, escape: false`),
      `${slot} 的策略不再是「无遮罩关闭 + 无 Escape」，请重新评估本守卫的前提`,
    );
    assert.ok(
      pageText.includes(`onClose={() => rootModals.requestClose("${slot}", "button")}`),
      `${slot}: page.tsx 没有把关闭接到 requestClose`,
    );
  }
  const closers = jsxElements(transferPanel, "button")
    .filter((element) => (element.bindings?.onClick ?? element.attributes?.onClick ?? "") === "onClose");
  assert.equal(closers.length, 2, "导出与导入弹窗各有一个「×」");
  for (const closer of closers) {
    assert.equal(closer.bindings?.disabled ?? closer.attributes?.disabled, undefined, "「×」带上了 disabled");
  }
});
