import { test } from "node:test";
import assert from "node:assert/strict";

import {
  adminGroups,
  buildGroupInviteLink,
  canDropManage,
  confersManage,
  creatableGroupKinds,
  foldGroupShares,
  manageGrantIds,
  grantedViaLabel,
  GROUP_INPUT_LIMITS,
  groupLengthHint,
  groupKindLabel,
  groupRoleLabel,
  isGroupAdmin,
  isGroupGranted,
  isPlainMember,
  partitionByGrant,
  parseGroupInviteToken,
  requestableGroups,
  revocationOrder,
  shareableGroups,
  shareRequestStatusLabel,
  visibleMyShareRequests,
} from "../../app/group-api.ts";

test("群组邀请链接编码 token，并能在登录前后从 query 中恢复", () => {
  const link = buildGroupInviteLink("gri-a_b-c", "https://sn.example");
  assert.equal(link, "https://sn.example/?group_invite=gri-a_b-c");
  assert.equal(parseGroupInviteToken("?group_invite=gri-a_b-c"), "gri-a_b-c");
  assert.equal(parseGroupInviteToken("?x=1&group_invite=gri-a%2Fb&y=2"), "gri-a/b");
  assert.equal(parseGroupInviteToken("?x=1"), null);
});

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
  assert.deepEqual(entries[0].grants, [
    { id: "gr1", role: "viewer" },
    { id: "gr2", role: "admin" },
  ]);
  // 带 group_admins 边 → 标注可管理(两条边任意顺序都成立)。
  assert.equal(entries[0].manage, true);
});

test("撤销顺序把给管理权的边排到最后——先删它就把自己的删除权删掉了", () => {
  // 标准共享模板**先发** (group_admins, admin) **后发** (group, viewer),所以后端按
  // created_at 返回时 admin 边天然排在前:这是主路径,不是边角情形(codex #519 R7 P2)。
  const [entry] = foldGroupShares([
    { id: "gr-admin", principal_type: "group_admins", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
    { id: "gr-view", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(entry.grants.map((g) => g.id), ["gr-admin", "gr-view"]);  // 原始返回序
  assert.deepEqual(revocationOrder(entry), ["gr-view", "gr-admin"]);          // 管理边最后

  // 已经是「管理边在后」的输入不被打乱;单边、零边都不炸。
  const [reversed] = foldGroupShares([
    { id: "gr-view", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
    { id: "gr-admin", principal_type: "group", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(revocationOrder(reversed), ["gr-view", "gr-admin"]);
  assert.deepEqual(revocationOrder({ grants: [{ id: "only", role: "viewer" }] }), ["only"]);
  assert.deepEqual(revocationOrder({ grants: [] }), []);
});

test("取消管理权只删授予管理权的边,读权边留着——判据同样是 confersManage", () => {
  // 标准模板:(group, viewer) + (group_admins, admin)。
  const [standard] = foldGroupShares([
    { id: "gr-view", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
    { id: "gr-admin", principal_type: "group_admins", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(manageGrantIds(standard), ["gr-admin"]);   // 只删它
  assert.ok(canDropManage(standard));                          // 删完还剩只读边

  // ⚠ (group, admin) 也是管理边(整组每个成员都拿到管理权)——按**主体类型**挑就会
  // 漏掉它、按钮点了没反应。判据必须是 role。
  const [byRole] = foldGroupShares([
    { id: "gr-a", principal_type: "group", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
    { id: "gr-v", principal_type: "group_admins", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(manageGrantIds(byRole), ["gr-a"]);
  assert.ok(canDropManage(byRole));

  // 孤零零一条 (group, admin):它**既是**读权又是管理权,删了这个组什么都看不到了
  // ——那不叫「取消管理权」,所以界面不给这个入口(改走「撤销共享」)。
  const [onlyAdmin] = foldGroupShares([
    { id: "gr-solo", principal_type: "group", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.equal(onlyAdmin.manage, true);
  assert.equal(canDropManage(onlyAdmin), false);
  // 纯只读共享:没有管理边可删。
  const [readOnly] = foldGroupShares([
    { id: "gr-r", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.deepEqual(manageGrantIds(readOnly), []);
});

test("排序判据与「可管理」标注是同一个 confersManage——只看 role,不看主体类型", () => {
  // 两个消费者共用一份判据:分成两份写法迟早会出现「标着可管理、却没被排到最后」的边。
  assert.equal(confersManage({ role: "admin" }), true);
  assert.equal(confersManage({ role: "viewer" }), false);
  // (group, admin) 也是管理边 —— 它把管理权给了整组每个成员,撤销时同样必须最后删。
  const [entry] = foldGroupShares([
    { id: "gr-a", principal_type: "group", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
    { id: "gr-b", principal_type: "group_admins", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.equal(entry.manage, true);
  assert.deepEqual(revocationOrder(entry), ["gr-b", "gr-a"]);
});

test("管理标注只看 role,两种群组主体一视同仁(四格:主体 × role)", () => {
  // 与后端 `_admin_principal_match_expr` 逐字对齐:group / group_admins 两条臂都只要求
  // role='admin'。所以 (group, admin) 也是管理权——它把管理权给了整组每个成员。
  const edge = (principal_type, role, id = "gr") => ({
    id, principal_type, principal_id: "g1", role,
    principal_name: "组", principal_kind: "project", created_at: "",
  });
  const manageOf = (grant) => foldGroupShares([grant])[0].manage;

  assert.equal(manageOf(edge("group", "admin")), true);          // ← R4 修的那一格
  assert.equal(manageOf(edge("group_admins", "admin")), true);
  assert.equal(manageOf(edge("group", "viewer")), false);
  assert.equal(manageOf(edge("group_admins", "viewer")), false); // ← R1 修的那一格
});

test("只有一条只读边时 manage 为假;group_admins 边先出现也照样标可管理", () => {
  const readOnly = foldGroupShares([
    { id: "gr1", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.equal(readOnly[0].manage, false);
  // group_admins 边先返回、group 边后返回,manage 仍为真(顺序无关)。
  const adminFirst = foldGroupShares([
    { id: "gr2", principal_type: "group_admins", principal_id: "g1", role: "admin", principal_name: "组", principal_kind: "project", created_at: "" },
    { id: "gr1", principal_type: "group", principal_id: "g1", role: "viewer", principal_name: "组", principal_kind: "project", created_at: "" },
  ]);
  assert.equal(adminFirst.length, 1);
  assert.equal(adminFirst[0].manage, true);
});

test("普通成员的组走申请路径:排除已共享的组与已有 pending 申请的组,但保留已驳回的", () => {
  const groups = [
    { id: "g1", my_role: "admin" },   // 组管理员 → 走直接共享,不在申请路径
    { id: "g2", my_role: "member" },  // 普通成员 → 可申请
    { id: "g3", my_role: "member" },  // 普通成员,但已共享 → 排除
    { id: "g4", my_role: "member" },  // 普通成员,但已有 pending → 排除
    { id: "g5", my_role: "member" },  // 普通成员,曾被驳回 → 仍可重新申请
    { id: "g6", my_role: "" },        // 非成员 → 都不在
  ];
  assert.ok(isPlainMember(groups[1]));
  assert.ok(!isPlainMember(groups[0]));
  const shared = foldGroupShares([
    { id: "s3", principal_type: "group", principal_id: "g3", role: "viewer", principal_name: "已共享", principal_kind: "project", created_at: "" },
  ]);
  const requests = [
    { id: "r4", group_id: "g4", status: "pending" },
    { id: "r5", group_id: "g5", status: "rejected" },
  ];
  assert.deepEqual(
    requestableGroups(groups, shared, requests).map((g) => g.id),
    ["g2", "g5"],
  );
});

test("申请状态翻成界面词,未知值退中性词;回显只留待审批与已驳回(已批准已变成共享)", () => {
  assert.equal(shareRequestStatusLabel("pending"), "待审批");
  assert.equal(shareRequestStatusLabel("approved"), "已批准");
  assert.equal(shareRequestStatusLabel("rejected"), "已驳回");
  assert.equal(shareRequestStatusLabel("weird"), "申请");
  const visible = visibleMyShareRequests([
    { id: "a", group_id: "g1", status: "pending" },
    { id: "b", group_id: "g2", status: "approved" },
    { id: "c", group_id: "g3", status: "rejected" },
  ]);
  assert.deepEqual(visible.map((r) => r.id), ["a", "c"]);
});

test("孤儿边(组已不存在)标成 missing,让库主看得懂并且删得掉", () => {
  const entries = foldGroupShares([
    { id: "gr9", principal_type: "group", principal_id: "gone", role: "viewer", principal_name: "", principal_kind: "missing", created_at: "" },
  ]);
  assert.equal(entries[0].missing, true);
  assert.deepEqual(entries[0].grants, [{ id: "gr9", role: "viewer" }]);
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

test("长度护栏与后端同值,余量提示只在接近上限时出声", () => {
  // 与 backend/app/api/group_routes.py 的 _MAX_GROUP_NAME_CHARS /
  // _MAX_GROUP_DESCRIPTION_CHARS 同值 —— 改一侧就要改另一侧。
  assert.equal(GROUP_INPUT_LIMITS.nameMaxChars, 120);
  assert.equal(GROUP_INPUT_LIMITS.descriptionMaxChars, 1000);

  assert.equal(groupLengthHint("", 120), "");
  assert.equal(groupLengthHint("x".repeat(100), 120), "");     // 还早,不给计数噪音
  assert.equal(groupLengthHint("x".repeat(108), 120), "还可输入 12 个字");
  assert.equal(groupLengthHint("x".repeat(119), 120), "还可输入 1 个字");
  assert.equal(groupLengthHint("x".repeat(120), 120), "已达上限 120 个字");
  // 超出(粘贴时浏览器 maxLength 之外的路径)同样说「已达上限」,不报负数。
  assert.equal(groupLengthHint("x".repeat(130), 120), "已达上限 120 个字");
});
