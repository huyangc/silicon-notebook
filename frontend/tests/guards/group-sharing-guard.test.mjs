// 群组知识共享 P1-T4 的接线守卫。
//
// 这里钉的是**只存在于 page.tsx 里的接线**——组件测试测得到 GroupsPage /
// NotebookGroupShare 自身,却测不到「它们有没有被挂上去」「群组共享的卡片有没有把
// 那个假失败的按钮收起来」。判据一律用语义结构或 onClick/条件的源码文本,不用行号。
//
// 覆盖边界(如实说明):本文件只钉下面列出的几条接线,不声称覆盖群组特性的全部界面。
import test from "node:test";
import assert from "node:assert/strict";

import {
  callsIn,
  controlFlowIn,
  findFunction,
  ifConditionsIn,
  importsFrom,
  jsxElements,
  parseModule,
  variableInitializersIn,
} from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const collectionHook = await parseModule("use-notebook-collection.ts");
const pageText = page.getFullText();

test("独立群组页面与「共享给群组」都真的挂在 page 上,不是只写了组件", () => {
  assert.deepEqual(
    importsFrom(page, "./groups-page").map((item) => item.imported),
    ["GroupsPage"],
  );
  const pages = jsxElements(page, "GroupsPage");
  assert.equal(pages.length, 1);
  assert.equal(pages[0].bindings.onBack, "showCollection");
  assert.equal(pages[0].bindings.initialGroupId, "groupNavigation.groupId");
  assert.equal(pages[0].bindings.initialTab, "groupNavigation.tab");
  // 入口在账户菜单里:回调打开 + 条件渲染页面各自被删都不会让组件测试报红。
  const menus = jsxElements(page, "AccountMenu");
  assert.equal(menus.length, 1);
  assert.equal(menus[0].bindings.onOpenGroups, '() => showGroups({}, "push")');

  const shares = jsxElements(page, "NotebookGroupShare");
  assert.equal(shares.length, 1, "「共享给群组」必须恰好挂在分享弹窗里一次");
  assert.equal(shares[0].bindings.notebookId, "currentNotebook.id");
  // ⚠ 这一条原本写的是「群组页面改的是成员/授权面,只影响列表(它不在某本笔记本的
  // 上下文里)」,只钉 `loadNotebookCollection()`。那个前提是**错的**:群组入口从工作区
  // 的顶栏也打得开,而退出群组 / 被移出 / 删组 / 撤销共享都可能把用户正站在里面的那本
  // 库从脚下抽走。只刷清单的话 `currentNotebook` 一动不动,人继续待在一本已经读不到的
  // 库里,整屏毫无反应,只能手动刷新(2026-08-20 用户反馈)。
  assert.match(pages[0].bindings.onChanged ?? "", /refreshAfterAccessChange\(\)/);
});

// 「退出共享」早就有这套对账(退掉之后跳走),群组那条路直到 2026-08-20 才补上。两条
// 必须共用同一个实现:分开写会让其中一条在后续改动里悄悄退化回「只刷列表」。
test("访问权变动之后必须连当前工作区一起对账,而不只是刷清单", () => {
  const reconcile = findFunction(page, "reconcileOpenNotebook");
  assert.ok(reconcile, "缺统一的工作区对账入口 reconcileOpenNotebook");
  // 判据必须是权威的「此刻真正装载着哪一本」(`activeNotebookIdRef`),不能用渲染期从
  // state 抄下来的 `currentNotebookIdRef`——后者在一次切库落地之前还指着上一本库,于是
  // 「弹窗还在飞时用户关掉它、点开另一本」这条时序里,对账会读到刚被撤销的旧库并把用户
  // 自己发起的导航作废掉;世代闸挡不住它(导航发起在世代快照之前,两边相等)。
  // codex #529 R9 P2。
  const reconcileText = variableInitializersIn(reconcile);
  assert.ok(
    reconcileText.some((row) => /activeNotebookIdRef\.current/.test(row.initializer ?? "")),
    "对账没有读 activeNotebookIdRef",
  );
  assert.ok(
    !reconcileText.some((row) => /currentNotebookIdRef/.test(row.initializer ?? "")),
    "对账读了渲染期的 currentNotebookIdRef——切库在飞时它是陈旧的",
  );

  const insideReconcile = callsIn(reconcile);
  // 判据是「重取回来的清单里还有没有它」——同一本库可以经多条路进来(另一个群组、
  // 直接成员、everyone 边),退掉其中一条不必然失去访问。
  assert.ok(
    insideReconcile.includes("openNotebook") || insideReconcile.includes("showCollection"),
    "对账不落地:既没跳到别的库,也没退回集合页",
  );
  // 「还在清单里」≠「什么都没变」:撤掉一条边之后访问权还在但档位可能降了(组管理员
  // 只剩另一个组的只读边),工作区那份独立 state 不跟着刷就会继续亮着写入口,而每次写
  // 都在 API 上被拒(codex #529 R11 P2)。
  assert.ok(
    insideReconcile.includes("refreshActiveNotebook"),
    "库还在时不刷当前笔记本详情——授权降档后界面会继续亮着写入口",
  );

  // 兜底导航自己失败(瞬时抖动)时必须落到一个**可自恢复**的状态。`openNotebook` 一进门
  // 就把 `activeNotebookIdRef` 置成 null,而屏幕上还留着那本已经读不到的库;就地抛出去
  // 的话,之后每一次复核都在 `!openId` 上直接返回,人被永久钉在陈旧工作区里
  // (codex #529 R12 P2)。
  const fallback = controlFlowIn(reconcile)
    .find((node) => node.kind === "if" && node.condition === "firstOwned");
  assert.ok(fallback, "对账里找不到「跳到自有库」那条兜底分支");
  const attempt = fallback.then.find((node) => node.kind === "try");
  assert.ok(attempt, "兜底导航没有被 try 包住——它失败时会把人留在读不到的库里");
  assert.match(JSON.stringify(attempt.catch ?? []), /showCollection/,
    "兜底导航失败后没有退回集合页");

  const groupPath = findFunction(collectionHook, "refreshAfterAccessChange");
  assert.ok(groupPath, "缺群组侧的收口 refreshAfterAccessChange");
  const insideGroup = callsIn(groupPath);
  assert.ok(insideGroup.includes("listNotebooks"), "没重取清单");
  assert.ok(
    insideGroup.some((call) => /effectsRef\.current\.reconcileAccess/.test(call)),
    "重取了清单却不通过窄 effect 对账当前工作区",
  );
  // 两次复核会叠在一起(切回标签页一次、弹窗里的动作又一次),而先发的那次可以后回。
  // 没有请求世代闸,旧响应会把撤销前的清单盖回去——工作区已经跳走了,列表里那本读不到
  // 的库却又活过来(codex #529 R4 P2)。`navEpoch` 挡的是导航,挡不住这个。
  assert.ok(
    ifConditionsIn(groupPath).some((condition) => /listIssuedRef/.test(condition)),
    "重取清单没有过期结果闸,旧响应会把撤销前的快照盖回去",
  );
  // 发布闸必须比的是**已发布水位**而不是「发起序号是不是还等于最新」:后者「发起即占位」,
  // 一次**失败**的复核会连并发的成功加载一起作废(它自己什么都没发布),初次加载因此可能
  // 永远停在空清单上(codex #529 R13 P2)。
  assert.ok(
    ifConditionsIn(findFunction(collectionHook, "commitListSnapshot"))
      .some((condition) => /listPublishedRef/.test(condition)),
    "发布闸不是按已发布水位判的,一次失败的复核会吞掉并发的成功加载",
  );

  // 闸必须覆盖**每一个**写清单的路径。`loadNotebookCollection` 把 listNotebooks() 和更慢的
  // health/config 放在同一个 Promise.all 里,它的清单可以在撤销之前取回、却被慢请求拖到
  // 撤销之后才落地,把那张已经读不到的卡片复活(codex #529 R5 P2)。
  const collection = findFunction(page, "loadNotebookCollection");
  assert.ok(collection, "缺 loadNotebookCollection");
  assert.ok(
    callsIn(collection).includes("notebookCollection.beginListRead")
      && callsIn(collection).includes("notebookCollection.commitListSnapshot"),
    "集合刷新绕过了清单发布闸,旧响应会复活已撤销的卡片",
  );

  // 链接共享的退出走**同一个收口**,不另抄一份重取+对账:抄一份就会漏掉其中一道闸,
  // 而它恰恰漏过——请求世代闸是 R4 补的,那时它自己那份拷贝里就没有(codex #529 R4)。
  const leave = findFunction(page, "handleLeaveShared");
  assert.ok(leave, "缺 handleLeaveShared");
  assert.ok(
    callsIn(leave).includes("refreshAfterAccessChange"),
    "「退出只读共享」没复用同一个收口",
  );
  assert.ok(
    !callsIn(leave).includes("listNotebooks"),
    "「退出只读共享」又自己抄了一份重取——那份必然漏闸",
  );

  // history 只在这次 fallback 导航**真的成功**之后才写:用户在它在飞时自己点去别处,
  // epoch guard 会弃掉结果并返回 false,而无条件的 `replaceState` 仍会把地址栏改成
  // firstOwned——界面停在用户新选的库上,URL 指向另一本,两者从此对不上
  // (codex #529 R1 P2)。同文件的 `openNotebookMemory` 早就是这个写法。
  assert.ok(
    ifConditionsIn(reconcile).some(
      (condition) => /^!/.test(condition) && /await\s*openNotebook/.test(condition),
    ),
    "对账里的 replaceState 没有被 openNotebook 的返回值挡住",
  );

  // 对账还必须先问一句「用户是不是已经自己走了」。重取清单在飞的那段时间里,用户点开
  // 的新库还在等首批请求,`currentNotebookIdRef` 仍指向刚被撤销的旧库——没有这道世代闸,
  // 对账会判定「需要跳走」并发出一次新的 `openNotebook`,反过来把**用户自己**发起的
  // 导航作废掉(codex #529 R2 P2)。上面那条返回值闸救不了它:被顶掉的是先发起的那次。
  assert.ok(
    ifConditionsIn(reconcile).some((condition) => /workspaceEpochRef/.test(condition)),
    "对账没有世代闸,会打断用户自己发起的切库",
  );
});

// 加/撤群组授权、开启/取消链接分享都会翻转「未共享门」——本笔记本一旦被共享出去,
// 它**借来的**参考库当场失效(设计文档 §6.1)。只刷集合列表的话
// `currentNotebook.base_notebooks` 还是旧的:检索范围控件继续列出并允许勾选一个这轮
// 取不到的参考库,Ask 与深度报告随之提交一份无效(甚至空)的范围,直到重开笔记本。
//
// 所以三个共享回调必须走**同一个**刷新入口,而那个入口必须同时刷列表与当前笔记本详情。
test("共享面变更的三个回调都刷新当前笔记本详情,而不只是集合列表", () => {
  const refresher = findFunction(page, "handleSharingChanged");
  assert.ok(refresher, "缺统一的共享变更刷新入口 handleSharingChanged");
  const inside = callsIn(refresher);
  assert.ok(inside.includes("loadNotebookCollection"), "共享变更后没刷集合列表");
  assert.ok(
    inside.includes("refreshActiveNotebook"),
    "共享变更后没重取当前笔记本详情 —— 借入参考库失效后检索范围控件仍显示旧的挂载集",
  );

  // 三个入口逐个钉:少接一个,那条路径上的失效就只有重开笔记本才能被发现。
  const share = jsxElements(page, "NotebookGroupShare")[0];
  assert.match(
    share.bindings.onChanged ?? "",
    /handleSharingChanged\(\)/,
    "「共享给群组」的回调没接统一刷新",
  );
  for (const name of ["enableShareLink", "handleUnshare"]) {
    const fn = findFunction(page, name);
    assert.ok(fn, `${name} 找不到了(改名或删除?)`);
    assert.ok(
      callsIn(fn).includes("handleSharingChanged"),
      `${name} 没接统一刷新 —— 它同样翻转未共享门`,
    );
  }

  // 刷新走的是既有的单库取数路径,不是新造的第二条。
  const refreshActive = findFunction(page, "refreshActiveNotebook");
  assert.ok(refreshActive && callsIn(refreshActive).includes("getNotebook"));
  assert.ok(
    callsIn(findFunction(page, "revalidateAskAvailability")).includes("refreshActiveNotebook"),
    "单库刷新分叉成了两条路径",
  );
});

// 打开「分享」是一次**查看**,不该有持久副作用。此前 openShareModal 无条件
// `POST .../share`,于是只想共享给群组的用户会顺带被铸出一条分享链接。
test("打开分享弹窗只读状态,链接由显式动作生成", async () => {
  const share = await parseModule("notebook-share.ts");
  assert.ok(findFunction(share, "getShareState"), "缺只读的分享状态读取入口");

  const opener = findFunction(page, "openShareModal");
  assert.ok(opener, "openShareModal 找不到了(改名或删除?)");
  const openCalls = callsIn(opener);
  assert.ok(openCalls.includes("getShareState"), "打开弹窗没走只读端点");
  assert.ok(
    !openCalls.includes("shareNotebook"),
    "打开弹窗仍会 POST —— 一次查看动作铸出了一条分享链接",
  );
  // POST 只留给显式的「开启链接分享」。
  const enabler = findFunction(page, "enableShareLink");
  assert.ok(enabler && callsIn(enabler).includes("shareNotebook"), "缺显式开启链接的入口");
});

// 评审抓的假失败:「退出共享」打的是 `DELETE /notebooks/{id}/membership`,它只删
// notebook_members 行,对群组授权边一点作用都没有。点了会弹一句「已退出」,而库还在
// 列表里。
//
// ⚠ 这条**不再**用源码文本断言那个三元还在:文本断言挡不住「把按钮平移出条件分支」
// 的变异(改完文本仍在,守卫照绿——移动变异实测)。两个入口已经抽成
// `notebook-reader-actions.tsx` 的组件,不变量由 `notebook-reader-actions.component
// .test.tsx` 真的渲染两种笔记本来断言;这里只钉「page 确实用的是那两个组件」——
// 把逻辑抄回 page.tsx 就等于绕开那份组件测试。
test("两个只读入口都由 notebook-reader-actions 提供,page 不自己再写一份", () => {
  assert.deepEqual(
    importsFrom(page, "./notebook-reader-actions").map((item) => item.imported).sort(),
    ["NotebookMenuActions", "ReaderNotebookBadge"],
  );
  assert.equal(jsxElements(page, "ReaderNotebookBadge").length, 1);
  const menuActions = jsxElements(page, "NotebookMenuActions");
  assert.equal(menuActions.length, 1);
  assert.match(menuActions[0].bindings.onLeave ?? "", /leaveNotebook\(target\.id\)/);
  assert.match(
    menuActions[0].bindings.onLeave ?? "",
    /loadNotebookCollection\(\)/,
    "集合卡片退出共享必须保留既有 composite 刷新，不能降成 access-only list",
  );

  // 反向:page.tsx 里不该再出现自己渲染的「退出共享」按钮。
  const strays = jsxElements(page, "button").filter((element) =>
    (element.bindings?.onClick ?? "").includes("handleLeaveShared(")
    || (element.bindings?.onClick ?? "").includes("leaveNotebook(target.id)"));
  assert.deepEqual(
    strays.map((element) => element.bindings.onClick),
    [],
    "page.tsx 又自己渲染了退出共享按钮 —— 它绕开了组件测试守着的那条分流",
  );
});

test("笔记本列表有独立的「群组」分区,且那一区的角色列不写「所有者」", () => {
  assert.deepEqual(
    importsFrom(page, "./group-api").map((item) => item.imported).sort(),
    [
      "GroupPageTab",
      "grantedViaLabel",
      "groupsHash",
      "isGroupGranted",
      "joinGroupInvite",
      "parseGroupInviteToken",
      "parseGroupsHash",
      "partitionByGrant",
    ],
  );
  const lists = jsxElements(page, "NotebookList");
  assert.equal(lists.length, 2, "列表视图应当分成两段:自有/只读共享 与 群组");
  const entries = lists.map((element) => element.bindings.entries);
  assert.ok(entries.includes("notebookPartition.personal"));
  assert.ok(entries.includes("notebookPartition.group"));
  const groupList = lists.find((element) => element.bindings.entries === "notebookPartition.group");
  assert.equal(groupList.attributes.roleText, "群组成员");
});

test("邀请兑换成功后集合刷新失败也必须进入群组,不能误报兑换失败", () => {
  const start = pageText.indexOf("joinGroupInvite(token)");
  const end = pageText.indexOf("}, [authChecked, currentUser]", start);
  assert.ok(start >= 0 && end > start, "缺邀请兑换 effect");
  const redemption = pageText.slice(start, end);

  assert.match(redemption, /\.then\(\(group\) => \{/);
  assert.match(redemption, /setToast\(`已加入群组/);
  assert.match(redemption, /showGroups\(\{ groupId: group\.id, tab: "members" \}, "replace"\)/);
  assert.match(
    redemption,
    /void loadNotebookCollection\(\)\.catch\(\(\) => \{\}\)/,
    "兑换后的集合刷新必须独立 fail-open，不能落入 joinGroupInvite 的失败提示",
  );
  assert.doesNotMatch(
    redemption,
    /await loadNotebookCollection\(\)/,
    "集合刷新失败会冒泡到外层 catch，并把已成功入组误报为失败",
  );
});

// P1-T2 规格评审登记的那条事实错误的标签:mountable 现在含别人 owner 的库,只按
// tier 分两组会把它们标成「我的笔记本」。
test("挂载选择器分三档,第三档是「共享给我的」", () => {
  assert.ok(pageText.includes('render("公共知识库", groups.public, "public")'));
  assert.ok(pageText.includes('render("我的笔记本", groups.mine, "mine")'));
  assert.ok(
    pageText.includes('render("共享给我的", groups.shared, "shared")'),
    "缺第三档:共享/群组授权进来的库会被标成「我的笔记本」",
  );
});


// 本地那条对账只覆盖「在这台浏览器上发起」的改动。被别人移出群组、组被别的管理员删掉、
// 库主撤销共享——都发生在别处,本浏览器收不到任何事件,人会继续坐在一本读不到的库里,
// 直到某次交互撞上 403(codex #529 R3 P1)。没有推送通道,所以退而求其次:标签页重新
// 可见时复用**同一条**对账路径。这条钉的正是「同一条」——另开一个只刷列表的旁路,
// 用户照样被留在那本库里。
test("远端撤销:标签页重新可见时复用同一条对账路径", () => {
  const revalidator = findFunction(collectionHook, "revalidate");
  assert.ok(revalidator, "缺 visibilitychange 复核入口 revalidate");

  // 由 hook 内部直接调用同一个 actor-owned 对账命令，页面不再保存陈旧 closure。
  assert.ok(
    callsIn(revalidator).includes("refreshAfterAccessChange"),
    "复核没有走同一个 refreshAfterAccessChange",
  );
  // 节流:密集 alt-tab 不该每切回一次就发一次 listNotebooks()。
  assert.ok(
    ifConditionsIn(revalidator).some(
      (condition) => /ACCESS_REVALIDATE_MIN_INTERVAL_MS/.test(condition),
    ),
    "复核没有节流",
  );

  // ⚠ 监听必须只装在**已登录**的页面上。没存过 token 时 `authChecked` 照样会被置真而
  // `currentUser` 仍是 null:只看 authChecked 的话,监听会装在登录页上,用户切回标签页就
  // 发一次 listNotebooks(),而它是 `unauthorized: "clear-and-reload"`——401 把整页重载,
  // 未登录用户每切回来一次就被刷一次(codex #529 R8 P2)。
  assert.ok(
    ifConditionsIn(collectionHook).some((condition) => /!actorId/.test(condition)),
    "访问权复核的监听没有同时门控已登录用户,会装在登录页上并触发 401 重载",
  );
});
