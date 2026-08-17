// 群组知识共享 P1-T4 的接线守卫。
//
// 这里钉的是**只存在于 page.tsx 里的接线**——组件测试测得到 GroupsModal /
// NotebookGroupShare 自身,却测不到「它们有没有被挂上去」「群组共享的卡片有没有把
// 那个假失败的按钮收起来」。判据一律用语义结构或 onClick/条件的源码文本,不用行号。
//
// 覆盖边界(如实说明):本文件只钉下面列出的几条接线,不声称覆盖群组特性的全部界面。
import test from "node:test";
import assert from "node:assert/strict";

import { importsFrom, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

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
});

// 评审抓的假失败:「退出共享」打的是 `DELETE /notebooks/{id}/membership`,它只删
// notebook_members 行,对群组授权边一点作用都没有。点了会弹一句「已退出」,而库还在
// 列表里。所以两个入口都必须先按 granted_via 分流。
test("经群组共享的库不给「退出共享」,两个入口都按 granted_via 分流", () => {
  const buttons = jsxElements(page, "button");
  const workspaceLeave = buttons.filter((element) =>
    (element.bindings?.onClick ?? "").includes("handleLeaveShared("));
  assert.equal(workspaceLeave.length, 1, "工作区顶部的「退出共享」入口找不到了(改名或删除?)");
  const menuLeave = buttons.filter((element) =>
    (element.bindings?.onClick ?? "").includes("leaveNotebook(target.id)"));
  assert.equal(menuLeave.length, 1, "笔记本操作菜单里的「退出共享」入口找不到了");

  assert.ok(
    pageText.includes("isGroupGranted(currentNotebook) ? ("),
    "工作区顶部没有按 granted_via 分流——群组共享的库会看到一个必然假成功的「退出共享」",
  );
  assert.ok(
    pageText.includes('menuNotebook.access === "reader" && isGroupGranted(menuNotebook) ? ('),
    "操作菜单没有按 granted_via 分流——同一个假失败会从卡片菜单里漏出去",
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
