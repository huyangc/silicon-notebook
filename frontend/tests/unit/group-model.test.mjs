import { test } from "node:test";
import assert from "node:assert/strict";

import {
  adminGroups,
  creatableGroupKinds,
  foldGroupShares,
  grantedViaLabel,
  groupKindLabel,
  groupRoleLabel,
  isGroupAdmin,
  isGroupGranted,
  partitionByGrant,
  shareableGroups,
} from "../../app/group-api.ts";

test("群组分类与角色都翻成界面词,未知值退中性词而不是吐后端的英文 id", () => {
  assert.equal(groupKindLabel("project"), "项目");
  assert.equal(groupKindLabel("department"), "部门");
  assert.equal(groupKindLabel("domain"), "领域");
  assert.equal(groupKindLabel("guild"), "群组");
  assert.equal(groupRoleLabel("admin"), "组管理员");
  assert.equal(groupRoleLabel("member"), "成员");
  assert.equal(groupRoleLabel("editor"), "成员");
});

test("普通用户只能建项目组;系统管理员三类都能建", () => {
  assert.deepEqual(creatableGroupKinds("user"), ["project"]);
  assert.deepEqual(creatableGroupKinds(""), ["project"]);
  assert.deepEqual(creatableGroupKinds("admin"), ["project", "department", "domain"]);
});

test("「共享给群组」只列我担任组管理员的组——别的组点一次必然拿 403", () => {
  const groups = [
    { id: "g1", my_role: "admin" },
    { id: "g2", my_role: "member" },
    { id: "g3", my_role: "" },
  ];
  assert.ok(isGroupAdmin(groups[0]));
  assert.ok(!isGroupAdmin(groups[1]));
  assert.deepEqual(adminGroups(groups).map((g) => g.id), ["g1"]);
});

test("授权边只留群组主体:只读共享(user)与公共知识库(everyone)各有自己的界面表达", () => {
  const entries = foldGroupShares([
    { id: "gr1", principal_type: "user", principal_id: "u1", role: "viewer", principal_name: "", principal_kind: "", created_at: "" },
    { id: "gr2", principal_type: "everyone", principal_id: "", role: "viewer", principal_name: "", principal_kind: "", created_at: "" },
    { id: "gr3", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "封装项目", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(entries.map((e) => e.groupId), ["g1"]);
  assert.equal(entries[0].name, "封装项目");
  assert.equal(entries[0].kind, "project");
  assert.equal(entries[0].missing, false);
});

test("同一个组的两条边(成员只读 / 组管理员可管)折成一项,撤销时两条一起删", () => {
  const entries = foldGroupShares([
    { id: "gr1", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "封装项目", principal_kind: "project", created_at: "" },
    { id: "gr2", principal_type: "group_admins", principal_id: "g1", role: "admin", principal_name: "封装项目", principal_kind: "project", created_at: "" },
  ]);
  assert.equal(entries.length, 1);
  assert.deepEqual(entries[0].grantIds, ["gr1", "gr2"]);
});

test("孤儿边(组已不存在)标成 missing,让库主看得懂并且删得掉", () => {
  const entries = foldGroupShares([
    { id: "gr9", principal_type: "group", principal_id: "gone", role: "viewer", principal_name: "", principal_kind: "missing", created_at: "" },
  ]);
  assert.equal(entries[0].missing, true);
  assert.deepEqual(entries[0].grantIds, ["gr9"]);
});

test("已经共享过的组不再出现在选择器里(重复发边后端会 409)", () => {
  const groups = [
    { id: "g1", my_role: "admin" },
    { id: "g2", my_role: "admin" },
    { id: "g3", my_role: "member" },
  ];
  const shared = foldGroupShares([
    { id: "gr1", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "已共享", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(shareableGroups(groups, shared).map((g) => g.id), ["g2"]);
});

test("granted_via 非空即「经群组共享」;标注点名全部来源群组", () => {
  assert.equal(isGroupGranted({}), false);
  assert.equal(isGroupGranted({ granted_via: [] }), false);
  const one = { granted_via: [{ group_id: "g1", group_name: "封装项目", kind: "project" }] };
  assert.equal(isGroupGranted(one), true);
  assert.equal(grantedViaLabel(one), "来自群组《封装项目》");
  assert.equal(grantedViaLabel({}), "");
  const two = {
    granted_via: [
      { group_id: "g1", group_name: "封装项目", kind: "project" },
      { group_id: "g2", group_name: "工艺部", kind: "department" },
    ],
  };
  // 只写第一个会让「退出了 A 组还看得见」变成一件说不通的事。
  assert.equal(grantedViaLabel(two), "来自群组《封装项目》《工艺部》");
});

test("列表分区按 granted_via 判,不按 access —— 只读共享仍留在主区(它有自己的退出出口)", () => {
  const entries = [
    { notebook: { id: "a", access: "owner" } },
    { notebook: { id: "b", access: "reader" } },                       // 只读共享
    { notebook: { id: "c", access: "reader", granted_via: [] } },      // 空数组也算只读共享
    { notebook: { id: "d", access: "reader", granted_via: [{ group_id: "g1", group_name: "组", kind: "project" }] } },
  ];
  const { personal, group } = partitionByGrant(entries);
  assert.deepEqual(personal.map((e) => e.notebook.id), ["a", "b", "c"]);
  assert.deepEqual(group.map((e) => e.notebook.id), ["d"]);
});
