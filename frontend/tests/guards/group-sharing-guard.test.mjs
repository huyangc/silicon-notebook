// 群组知识共享 P1-T4 的接线守卫。
//
// 这里钉的是**只存在于 page.tsx 里的接线**——组件测试测得到 GroupsModal /
// NotebookGroupShare 自身,却测不到「它们有没有被挂上去」「群组共享的卡片有没有把
// 那个假失败的按钮收起来」。判据一律用语义结构或 onClick/条件的源码文本,不用行号。
//
// 覆盖边界(如实说明):本文件只钉下面列出的几条接线,不声称覆盖群组特性的全部界面。
import test from "node:test";
import assert from "node:assert/strict";

import { callsIn, findFunction, importsFrom, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const pageText = page.getFullText();

test("群组管理弹窗与「共享给群组」都真的挂在 page 上,不是只写了组件", () => {
  assert.deepEqual(
    importsFrom(page, "./groups-panel").map((item) => item.imported),
    ["GroupsModal"],
  );
  const modals = jsxElements(page, "GroupsModal");
  assert.equal(modals.length, 1);
  assert.equal(modals[0].bindings.onClose, "() => setGroupsOpen(false)");
  // 入口在账户菜单里:两截接线(回调打开 + 条件渲染弹窗)各自被删都不会让组件测试报红。
  const menus = jsxElements(page, "AccountMenu");
  assert.equal(menus.length, 1);
  assert.equal(menus[0].bindings.onOpenGroups, "() => setGroupsOpen(true)");

  const shares = jsxElements(page, "NotebookGroupShare");
  assert.equal(shares.length, 1, "「共享给群组」必须恰好挂在分享弹窗里一次");
  assert.equal(shares[0].bindings.notebookId, "currentNotebook.id");
  // 共享面变了要让笔记本清单重取:「已分享」徽标的口径含群组共享,不重取就会看到
  // 一本刚共享出去的库仍然没有徽标。
  assert.match(shares[0].bindings.onChanged ?? "", /loadNotebookCollection\(\)/);
  assert.match(modals[0].bindings.onChanged ?? "", /loadNotebookCollection\(\)/);
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
  assert.equal(jsxElements(page, "NotebookMenuActions").length, 1);

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
    ["grantedViaLabel", "isGroupGranted", "partitionByGrant"],
  );
  const lists = jsxElements(page, "NotebookList");
  assert.equal(lists.length, 2, "列表视图应当分成两段:自有/只读共享 与 群组");
  const entries = lists.map((element) => element.bindings.entries);
  assert.ok(entries.includes("notebookPartition.personal"));
  assert.ok(entries.includes("notebookPartition.group"));
  const groupList = lists.find((element) => element.bindings.entries === "notebookPartition.group");
  assert.equal(groupList.attributes.roleText, "群组成员");
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
