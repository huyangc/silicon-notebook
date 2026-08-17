import test from "node:test";
import assert from "node:assert/strict";

import {
  doneItemDestination,
  notebookRoleText,
  workspaceCapabilities,
  workspaceRequestIsCurrent,
} from "../../app/workspace-transitions.ts";
import {
  callSitesIn,
  declarations,
  importsFrom,
  jsxElements,
  jsxTextValues,
  parseModule,
} from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");


test("workspace composes executable Ask and account components", () => {
  assert.deepEqual(
    importsFrom(page, "./ask-composer").map((item) => item.imported),
    ["AskComposer"],
  );
  assert.deepEqual(
    importsFrom(page, "./account-menu").map((item) => item.imported),
    ["AccountMenu"],
  );
  assert.deepEqual(
    importsFrom(page, "./ask-session-header").map((item) => item.imported),
    ["AskSessionHeaderActions"],
  );
  assert.equal(jsxElements(page, "AskComposer").length, 1);
  assert.equal(jsxElements(page, "AccountMenu").length, 1);
  const sessionHeaders = jsxElements(page, "AskSessionHeaderActions");
  assert.equal(sessionHeaders.length, 1);
  assert.deepEqual(sessionHeaders[0].bindings, {
    sessionCount: "sessions.length",
    sessionPanelOpen: "sessionPanelOpen",
    onToggleSessionPanel: "() => setSessionPanelOpen(open => !open)",
    onStartNewSession: "startNewSession",
  });
  const pageFunctions = new Set(
    declarations(page)
      .filter((finding) => finding.kind === "function")
      .map((finding) => finding.name),
  );
  assert.equal(pageFunctions.has("AskComposer"), false);
  assert.equal(pageFunctions.has("AccountMenu"), false);
});


test("Ask session controls occupy one header row", () => {
  assert.equal(
    jsxElements(page, "div").some(
      ({ attributes }) => attributes.className === "chat-session-context",
    ),
    false,
  );
  assert.ok(
    jsxElements(page, "div").some(
      ({ attributes }) => (
        attributes.id === "ask-session-manager"
        && attributes.className === "chat-session-popover"
        && attributes.role === "dialog"
        && attributes["aria-label"] === "会话管理"
      ),
    ),
  );
});


test("workspace has no retired Studio panel and keeps a labelled exit", () => {
  const classes = [
    ...jsxElements(page, "div"),
    ...jsxElements(page, "section"),
  ].map(({ attributes }) => attributes.className);
  assert.equal(classes.includes("workspace-panel studio-panel"), false);
  assert.ok(
    jsxElements(page, "button")
      .some(({ attributes }) => attributes.className === "notebook-home"),
  );
  assert.ok(jsxTextValues(page).includes("返回主页"));
});


test("source actions remain available by accessible meaning", () => {
  const buttons = jsxElements(page, "button");
  const links = jsxElements(page, "a");
  assert.ok(buttons.some(({ attributes }) => attributes.title === "删除来源"));
  assert.ok(links.some(({ attributes }) => attributes["aria-label"] === "打开原始链接"));
});


test("降级解析提示提供显式重新解析与删除操作", () => {
  const warnings = jsxElements(page, "section").filter(
    ({ attributes }) => attributes["aria-label"] === "降级解析提示",
  );
  assert.equal(warnings.length, 1);
  const text = jsxTextValues(page);
  assert.ok(text.includes("当前内容由本地解析器生成"));
  const buttons = jsxElements(page, "button");
  assert.ok(buttons.some(({ attributes }) => attributes["aria-label"] === "重新解析降级来源"));
  assert.ok(buttons.some(({ attributes }) => attributes["aria-label"] === "删除降级来源"));
});


// 评审 P2:「修改密码」的两截接线(菜单回调打开 + 条件渲染弹窗)各自被删都不会
// 让任何组件测试报红——组件测试只测 AccountMenu / PasswordChangeModal 自身。
// 这条把 page.tsx 的接线钉住:回调绑定、内置管理员隐藏入口的谓词、弹窗恰好
// 渲染一次且 onClose 关掉同一个 state。
test("修改密码弹窗在 page 接线:菜单回调打开、内置管理员隐藏、onClose 复位", () => {
  assert.deepEqual(
    importsFrom(page, "./password-change-modal").map((item) => item.imported),
    ["PasswordChangeModal"],
  );
  const modals = jsxElements(page, "PasswordChangeModal");
  assert.equal(modals.length, 1);
  assert.deepEqual(modals[0].bindings, {
    onClose: "() => setPasswordModalOpen(false)",
  });
  const menus = jsxElements(page, "AccountMenu");
  assert.equal(menus.length, 1);
  assert.equal(menus[0].bindings.onChangePassword, "() => setPasswordModalOpen(true)");
  assert.equal(menus[0].bindings.canChangePassword, 'currentUser.id !== "user-local"');
});


test("source detail uses the dedicated draggable window shell", () => {
  assert.deepEqual(
    importsFrom(page, "./source-detail-window").map((item) => item.imported),
    ["SourceDetailWindow"],
  );
  const windows = jsxElements(page, "SourceDetailWindow");
  assert.equal(windows.length, 1);
  assert.deepEqual(windows[0].bindings, {
    // PR-2 T6: 关闭来源详情也要清掉 highlightedElementId——否则 Ask 清单卡「查看
    // 来源」跳转设置的高亮目标会残留到下一次经普通来源列表打开的、无关的来源上
    // （该状态与目标元素同一个 getElementById 效果消费，参见 highlightedElementId
    // 声明处的效果与 openSourceById 的注释）。
    onClose: '() => { sourceDetailRequestGenerationRef.current += 1; setSourceDetail(null); setHighlightedElementId(""); setSourceElementsLoading(false); }',
  });
  assert.equal(
    importsFrom(page, "lucide-react").some(({ imported }) => imported === "PanelRightClose"),
    false,
  );
});


// 双评审 P2-6: 来源详情「查看来源」跳转能不能真的定位到目标元素,取决于两处
// 独立代码是否仍在用同一个 sourceElementDomId(...) 变换互相对应——评审实测:
// 删掉元素卡的 id 属性,现有测试(上面那条只钉 onClose 绑定)全绿。这条测试把
// 两处绑到一起:元素卡必须把 id 设成 sourceElementDomId(element.id),滚动 effect
// 必须用同一个函数把 highlightedElementId 变换成同一种 id 去 getElementById。
// 任一处被删除或被"移动"(换成不调用 sourceElementDomId 的等价写法)都会报红。
test("来源详情的元素卡片 DOM id 与滚动 effect 消费同一个 sourceElementDomId(...)", () => {
  const sourceCards = jsxElements(page, "article").filter(
    (element) => element.bindings?.id === "sourceElementDomId(element.id)",
  );
  assert.equal(
    sourceCards.length,
    1,
    "元素卡片未绑定 id={sourceElementDomId(element.id)}(被删除,或改了绑定表达式)",
  );

  const scrollEffect = callSitesIn(page).find(
    (call) => call.target === "useEffect"
      && call.arguments[1] === "[highlightedElementId, sourceDetail, sourceElements]",
  );
  assert.ok(
    scrollEffect,
    "highlightedElementId 滚动 effect 未找到(依赖数组已改变,或整段被删)",
  );
  assert.match(
    scrollEffect.arguments[0],
    /sourceElementDomId\(highlightedElementId\)/,
    "滚动 effect 不再调用 sourceElementDomId(highlightedElementId)(被改写成了不经过它的等价逻辑)",
  );
});


test("background responses require the same workspace and notebook", () => {
  assert.equal(
    workspaceRequestIsCurrent(false, 3, 3, "nb-1", "nb-1"),
    true,
  );
  assert.equal(
    workspaceRequestIsCurrent(true, 3, 3, "nb-1", "nb-1"),
    false,
  );
  assert.equal(
    workspaceRequestIsCurrent(false, 2, 3, "nb-1", "nb-1"),
    false,
  );
  assert.equal(
    workspaceRequestIsCurrent(false, 3, 3, "nb-1", "nb-2"),
    false,
  );
});


test("workspace capabilities separate notebook type ownership from the global baseline", () => {
  // 只读成员的 canManageReports 为 **true**（群组知识共享 P1）：报告按创建者行级
  // 隔离，列表里出现的每一份都是当前用户自己建的，可操作性恒成立。其余四项仍跟着
  // 写权走——这条断言同时钉住「只放开了报告面」。
  assert.deepEqual(workspaceCapabilities("reader", "user"), {
    canWriteNotebook: false,
    canGovernKnowledge: false,
    canManageReports: true,
    canManageNotebookSchemas: false,
    canManageGlobalSchemas: false,
  });
  assert.deepEqual(workspaceCapabilities("owner", "user"), {
    canWriteNotebook: true,
    canGovernKnowledge: true,
    canManageReports: true,
    canManageNotebookSchemas: true,
    canManageGlobalSchemas: false,
  });
  assert.deepEqual(workspaceCapabilities("owner", "admin"), {
    canWriteNotebook: true,
    canGovernKnowledge: true,
    canManageReports: true,
    canManageNotebookSchemas: true,
    canManageGlobalSchemas: true,
  });
});


test("completed paper metadata opens sources while index work opens KG", () => {
  assert.equal(doneItemDestination("paper_meta_done"), "sources");
  assert.equal(doneItemDestination("index_done"), "kg");
  assert.equal(doneItemDestination(undefined), "kg");
});


// 「角色」列此前整列写死 "Owner",连只读共享进来的库也被标成所有者——与挂载选择器把
// 别人的库标成「我的笔记本」是同一类事实错误的标签。
test("笔记本列表的角色文案按 access 判,群组分区可显式覆盖", () => {
  assert.equal(notebookRoleText({ access: "owner" }), "Owner");
  assert.equal(notebookRoleText({}), "Owner");                 // 缺字段按 owner(向后兼容)
  assert.equal(notebookRoleText({ access: "reader" }), "只读成员");
  assert.equal(notebookRoleText({ access: "reader" }, "群组成员"), "群组成员");
  assert.equal(notebookRoleText({ access: "owner" }, "群组成员"), "群组成员");
});
