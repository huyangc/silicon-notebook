import test from "node:test";
import assert from "node:assert/strict";

import {
  doneItemDestination,
  notebookIsMirror,
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
const sourceListPanel = await parseModule("source-list-panel.tsx");


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
    onToggleSessionPanel: "askSession.toggleSessionPanel",
    onStartNewSession: "startNewAskSession",
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
      .some(({ attributes }) => attributes.className === "back-home-button"),
  );
  assert.ok(jsxTextValues(page).includes("返回主页"));
});


// 来源行的删除/外链动作 PR-5 分片 2 起住在 source-list-panel.tsx;判据不变,只换模块。
test("source actions remain available by accessible meaning", () => {
  const buttons = jsxElements(sourceListPanel, "button");
  const links = jsxElements(sourceListPanel, "a");
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
    onClose: '() => rootModals.requestClose("password-change", "button")',
    interactive: 'rootModals.view("password-change").topmost',
    zIndex: 'rootModals.view("password-change").zIndex',
  });
  const menus = jsxElements(page, "AccountMenu");
  assert.equal(menus.length, 1);
  assert.equal(
    menus[0].bindings.onChangePassword,
    '() => { rootModals.open("password-change", rootModals.captureActorOwner()); }',
  );
  assert.equal(menus[0].bindings.canChangePassword, '(authCapabilities?.mode === "local" || authCapabilities?.mode === "dual") && currentUser.id !== "user-local"');
});


test("source detail uses the dedicated draggable window shell", () => {
  assert.deepEqual(
    importsFrom(page, "./source-detail-window").map((item) => item.imported),
    ["SourceDetailWindow"],
  );
  const windows = jsxElements(page, "SourceDetailWindow");
  assert.equal(windows.length, 1);
  assert.deepEqual(windows[0].bindings, {
    // 关闭经 root coordinator 的 source-detail close sink 回到 source owner，仍由
    // sourceLibrary.closeSourceDetail 清 highlightedElementId；同时 topmost 决定
    // aria-modal/inert，防止上层确认框出现时后台详情继续接收键盘输入。
    onClose: '() => rootModals.requestClose("source-detail", "button")',
    interactive: 'rootModals.view("source-detail").topmost',
    zIndex: 'rootModals.view("source-detail").zIndex',
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
test("来源详情的元素卡片 DOM id 与滚动 effect 消费同一个 sourceElementDomId(...)", async () => {
  const hook = await parseModule("use-source-library.ts");
  const sourceCards = jsxElements(page, "article").filter(
    (element) => element.bindings?.id === "sourceElementDomId(element.id)",
  );
  assert.equal(
    sourceCards.length,
    1,
    "元素卡片未绑定 id={sourceElementDomId(element.id)}(被删除,或改了绑定表达式)",
  );

  const scrollEffect = callSitesIn(hook).find(
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
    mirrored: false,
    mirrorOrigin: "",
    mirrorHidesNotebookManage: false,
    canWriteNotebook: false,
    canGovernKnowledge: false,
    canRebuildIndexes: false,
    canGrantAccess: false,
    canConfigureNotebook: false,
    canMountBases: false,
    canManageNotebook: false,
    canDeleteNotebook: false,
    canManageReports: true,
    canManageNotebookSchemas: false,
    canManageGlobalSchemas: false,
  });
  assert.deepEqual(workspaceCapabilities("owner", "user"), {
    mirrored: false,
    mirrorOrigin: "",
    mirrorHidesNotebookManage: false,
    canWriteNotebook: true,
    canGovernKnowledge: true,
    canRebuildIndexes: true,
    canGrantAccess: true,
    canConfigureNotebook: true,
    canMountBases: true,
    canManageNotebook: true,
    canDeleteNotebook: true,
    canManageReports: true,
    canManageNotebookSchemas: true,
    canManageGlobalSchemas: false,
  });
  assert.deepEqual(workspaceCapabilities("owner", "admin"), {
    mirrored: false,
    mirrorOrigin: "",
    mirrorHidesNotebookManage: false,
    canWriteNotebook: true,
    canGovernKnowledge: true,
    canRebuildIndexes: true,
    canGrantAccess: true,
    canConfigureNotebook: true,
    canMountBases: true,
    canManageNotebook: true,
    canDeleteNotebook: true,
    canManageReports: true,
    canManageNotebookSchemas: true,
    canManageGlobalSchemas: true,
  });
});


test("group admins get the content-management bits on a notebook that is still `reader`", () => {
  // 群组知识共享 P2:后端把六个内容管理能力从 owner-only 翻成「owner ∪ 组管理边」,
  // 而 `access` 刻意仍是 "reader"（权限档没有新增枚举值，裁决 P2-3）。只看 access 的
  // 界面会让组管理员对着一个 API 全部允许、按钮全部藏起来的只读工作区。
  assert.deepEqual(workspaceCapabilities("reader", "user", true), {
    mirrored: false,
    mirrorOrigin: "",
    mirrorHidesNotebookManage: false,
    canWriteNotebook: true,
    canGovernKnowledge: true,
    canRebuildIndexes: true,
    canGrantAccess: true,
    // ⚠ 挂载配置(notebook:mount)与链接分享(notebook:configure)**都恒 owner**
    // (P2-T2 评审 P0;跨环境同步 §5 把它们拆成两个能力名,级别一个字没变):组管理员
    // 有内容管理权,但 access 仍是 reader → canConfigureNotebook 为 **false**。
    canConfigureNotebook: false,
    // 挂载同样恒 owner,组管理员拿不到（与 canConfigureNotebook 同级别,只是围栏相反）。
    canMountBases: false,
    // 改名/tier 是 notebook:manage(admin 档):组管理员**有**。
    canManageNotebook: true,
    // 删库恒 owner。
    canDeleteNotebook: false,
    canManageReports: true,
    canManageNotebookSchemas: true,
    // 全局图谱类型基线仍只认系统管理员——组管理员在**这本库**里有权，不是全站有权。
    canManageGlobalSchemas: false,
  });
  // 显式 false 与省略第三个参数必须逐位相同:旧后端不发 can_manage_content,缺省
  // 一律取收的那一侧（画多了按钮 = 点进一个必然 404 的动作）。
  assert.deepEqual(
    workspaceCapabilities("reader", "user", false),
    workspaceCapabilities("reader", "user"),
  );
  assert.equal(workspaceCapabilities("reader", "user", false).canWriteNotebook, false);
  // owner 那一侧不受这个参数影响（它本来就为真，false 也不该把它按下去）。
  assert.equal(workspaceCapabilities("owner", "user", false).canWriteNotebook, true);
  // 系统管理员这一维与内容管理权正交:组管理员不因此获得全局基线写权。
  assert.equal(
    workspaceCapabilities("reader", "admin", true).canManageGlobalSchemas,
    true,
  );
});


test("canConfigureNotebook is owner-only — content-management权 never unlocks it", () => {
  // P2-T2 评审 P0:挂载配置(参考库增删)与链接分享是 owner 对本库检索范围/对外处置的
  // 配置,后端 notebook:mount / notebook:configure 都恒 owner,不随内容管理权翻给组
  // 管理员。这一个前端标志刻意覆盖两格——它们级别相同,界面上也是同一批入口。
  // 判据只看 access。
  assert.equal(workspaceCapabilities("owner", "user").canConfigureNotebook, true);
  // 组管理员(reader + can_manage_content=true)有内容写权,但配置权仍为 false。
  assert.equal(workspaceCapabilities("reader", "user", true).canConfigureNotebook, false);
  assert.equal(workspaceCapabilities("reader", "user", false).canConfigureNotebook, false);
  // canWrite 放宽了(组管理员为真),canConfigure 没有——两者刻意分开。
  assert.equal(workspaceCapabilities("reader", "user", true).canWriteNotebook, true);
});


// ------------------------------------------------- 跨环境同步:目标端写入围栏 §5
//
// `sync_origin` 非空 = 这本库是从别的环境同步来的镜像。后端按能力名逐格决定「这个
// 端点会不会改写同步层内容」(`_CAPABILITY_MIRROR_FENCE`),前端这一层必须**逐格**跟
// 着那张表走,而不是笼统地「镜像 = 只读」——镜像的 owner 权限一点没少,他仍然能分享、
// 能授权、能提问,还**必须**能重建自己的检索索引。

test("镜像逐格映射围栏表:内容写被挡,索引/链接分享/报告照常", () => {
  const local = workspaceCapabilities("owner", "user");
  const mirror = workspaceCapabilities("owner", "user", false, "site-a");

  assert.equal(mirror.mirrored, true);
  assert.equal(mirror.mirrorOrigin, "site-a");
  assert.equal(local.mirrored, false);
  assert.equal(local.mirrorOrigin, "");

  // 挡(True 的那几格):内容写、图谱类型、改名/tier、挂载、删库。
  assert.equal(mirror.canWriteNotebook, false);
  assert.equal(mirror.canGovernKnowledge, false);
  assert.equal(mirror.canManageNotebookSchemas, false);
  assert.equal(mirror.canManageNotebook, false);
  assert.equal(mirror.canMountBases, false);
  assert.equal(mirror.canDeleteNotebook, false);

  // 放行(False 的那几格):检索索引重建、链接分享、报告。
  assert.equal(
    mirror.canRebuildIndexes,
    true,
    "scale_index:write 在围栏里是 False——镜像必须能重建自己的索引,否则索引永远停在导入那一刻",
  );
  assert.equal(mirror.canConfigureNotebook, true, "share_token 是目标端自有列,不随同步走");
  assert.equal(
    mirror.canGrantAccess,
    true,
    "notebook:grant 在围栏里是 False——目标端自己的可见性由目标端管理(§5 明文放行)",
  );
  assert.equal(mirror.canManageReports, true);

  // 本地库那一侧一格都没动(缺省 syncOrigin = "" 与显式空串逐位相同)。
  assert.deepEqual(workspaceCapabilities("owner", "user", false, ""), local);
});

test("镜像 × 只读成员:收的那一侧不会被围栏放宽", () => {
  const mirror = workspaceCapabilities("reader", "user", false, "site-a");
  // 只读成员在镜像上仍然是只读——围栏只会更收,不会更放。
  assert.equal(mirror.canWriteNotebook, false);
  assert.equal(mirror.canRebuildIndexes, false, "他对本地库也没有索引重建权,镜像不会给他");
  assert.equal(mirror.canConfigureNotebook, false, "链接分享恒 owner");
  assert.equal(mirror.canMountBases, false);
  assert.equal(mirror.canManageNotebook, false);
  assert.equal(mirror.canDeleteNotebook, false);
  assert.deepEqual(
    mirror,
    { ...workspaceCapabilities("reader", "user"), mirrored: true, mirrorOrigin: "site-a" },
    "只读成员这一行除了两个标注位,镜像与本地逐位相同",
  );
});

test("镜像 × 组管理员:内容写与改名被挡,索引重建仍在", () => {
  const mirror = workspaceCapabilities("reader", "user", true, "site-a");
  assert.equal(mirror.canWriteNotebook, false);
  assert.equal(mirror.canGovernKnowledge, false);
  assert.equal(mirror.canManageNotebookSchemas, false);
  // notebook:manage 是 admin 档,他本来有;围栏把它挡掉。
  assert.equal(mirror.canManageNotebook, false);
  // scale_index:write 也是 admin 档,但围栏放行 → 组管理员在镜像上照样能重建索引。
  assert.equal(mirror.canRebuildIndexes, true);
  // 恒 owner 的那两格与镜像无关,本来就是 false。
  assert.equal(mirror.canConfigureNotebook, false);
  assert.equal(mirror.canMountBases, false);
  assert.equal(mirror.canDeleteNotebook, false);
  // 系统管理员那一维与镜像正交。
  assert.equal(workspaceCapabilities("reader", "admin", true, "site-a").canManageGlobalSchemas, true);
});

// 「控件确实被收走了」与「他没有这个权限」是两件事:就地说明只该出现在前者。
test("mirrorHidesNotebookManage 只对本来握有 notebook:manage 的人为真", () => {
  // owner / 组管理员:本地库上有改名控件,镜像把它收走了 → 要解释。
  assert.equal(workspaceCapabilities("owner", "user", false, "site-a").mirrorHidesNotebookManage, true);
  assert.equal(workspaceCapabilities("reader", "user", true, "site-a").mirrorHidesNotebookManage, true);
  // 纯只读成员:他本来就没有那个控件,一句「镜像的名称…」是在解释他从未见过的事。
  assert.equal(workspaceCapabilities("reader", "user", false, "site-a").mirrorHidesNotebookManage, false);
  // 本地库一律为假(没有任何东西被收走)。
  assert.equal(workspaceCapabilities("owner", "user").mirrorHidesNotebookManage, false);
  assert.equal(workspaceCapabilities("reader", "user", true).mirrorHidesNotebookManage, false);
  // ⚠ 判据不是 `!canManageNotebook`:那一位对只读成员恒假,正是要避开的那一格。
  assert.equal(workspaceCapabilities("reader", "user", false, "site-a").canManageNotebook, false);
});

test("notebookIsMirror 只看 sync_origin,空串/缺失都是本地库", () => {
  assert.equal(notebookIsMirror({ sync_origin: "site-a" }), true);
  assert.equal(notebookIsMirror({ sync_origin: "" }), false);
  assert.equal(notebookIsMirror({}), false);
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
