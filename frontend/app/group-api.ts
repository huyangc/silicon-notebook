// 群组与「共享给群组」的客户端(群组知识共享 P1-T4)。
//
// 传输一律经 api-client(生产代码里裸 fetch 只允许出现在那一个模块,守卫见
// tests/guards/api-boundary.test.mjs);本模块不依赖 React,纯 helper 在
// tests/unit/group-model.test.mjs 里单测。
//
// 两条与后端口径有关、容易在前端写反的事实:
//
//  1. **群组的「看不见」是 404,不是 403**。非成员访问一个群组与「组不存在」返回
//     完全一样的响应,所以界面不能按错误码区分这两种情形,也不该把「你不是成员」
//     写成文案——我们根本分不出来。唯一的例外在 `createGrant`:那里的 403 明确
//     是「你不是这个群组的组管理员」。
//  2. **P1 的共享只发一条 `viewer` 边**(已定裁决 4)。组管理员的写权限(第二条
//     `group_admins` 边)随 P2 一起上——现在发出去只会是一条当前没有任何效果的
//     授权,所以 `shareNotebookToGroup` 不接受角色参数。

import { requestJson, requestVoid } from "./api-client.ts";
import { GROUP_KIND, GROUP_ROLE, label } from "./vocabulary.ts";

// --- 类型(后端 app/models/groups.py 的镜像) --------------------------------

export type UserRef = {
  id: string;
  username: string;
  display_name?: string;
};

export type GroupMember = UserRef & {
  role: string;
  added_at?: string;
};

export type GroupSummary = {
  id: string;
  name: string;
  kind: string;
  description: string;
  /** 当前唯一 owner；created_by 仅留在服务端作不可变创建审计。 */
  owner_id: string;
  /** 请求者本人在该组里的角色;不是成员时为空串(管理员的全部群组视图会出现)。 */
  my_role: string;
  member_count: number;
  created_at: string;
};

export type GroupDetail = GroupSummary & { members: GroupMember[] };

export type GroupInviteState = {
  active: boolean;
  token: string;
  created_at?: string | null;
};

/** 一条授权边的只读投影。`principal_kind === "missing"` 是孤儿边(组已不存在)。 */
export type NotebookGrant = {
  id: string;
  principal_type: string;
  principal_id: string;
  role: string;
  principal_name: string;
  principal_kind: string;
  created_at: string;
};

export type GroupSharedNotebook = {
  notebook_id: string;
  name: string;
  owner_username: string;
  roles: string[];
};

/** 一条共享申请的只读投影(后端 app/models/groups.py::ShareRequestItem 的镜像)。
 *
 * 状态机 **pending → approved / rejected 单向**;撤回是删整行、不是第四个状态。
 * `decided_at` 只在已决定的行上是 ISO 时间戳,pending 时为 `null`(绝不是空串)。 */
export type ShareRequest = {
  id: string;
  notebook_id: string;
  notebook_name: string;
  group_id: string;
  group_name: string;
  requested_by: string;
  requested_by_username: string;
  status: string;
  decided_by?: string | null;
  decided_at?: string | null;
  created_at: string;
};

/** `NotebookSummary.granted_via` 的元素:这本笔记本是经哪个群组共享给我的。 */
export type GrantedGroupRef = {
  group_id: string;
  group_name: string;
  kind: string;
};

export type GroupPageTab = "notebooks" | "members" | "requests" | "settings";

export const groupsHash = (groupId = "", tab: GroupPageTab = "notebooks"): string => {
  const parts = ["groups"];
  if (groupId) parts.push(`group=${encodeURIComponent(groupId)}`);
  if (groupId && tab !== "notebooks") parts.push(`tab=${encodeURIComponent(tab)}`);
  return `#${parts.join("&")}`;
};

export const parseGroupsHash = (
  hash: string,
): { groupId: string; tab: GroupPageTab } | null => {
  const raw = hash.replace(/^#/, "");
  if (raw !== "groups" && !raw.startsWith("groups&")) return null;
  const params = new URLSearchParams(raw === "groups" ? "" : raw.slice(7));
  const rawTab = params.get("tab");
  const tab: GroupPageTab = rawTab === "members" || rawTab === "requests" || rawTab === "settings"
    ? rawTab
    : "notebooks";
  return { groupId: params.get("group") || "", tab };
};

// --- 传输 -------------------------------------------------------------------

const TAG = "groups";

export const listGroups = (scope: "mine" | "all" = "mine"): Promise<GroupSummary[]> =>
  requestJson(`/groups?scope=${scope}`, { tag: TAG });

export const createGroup = (
  name: string,
  kind: string,
  description = "",
): Promise<GroupDetail> =>
  requestJson(`/groups`, {
    method: "POST",
    body: JSON.stringify({ name, kind, description }),
    tag: TAG,
  });

export const getGroup = (groupId: string): Promise<GroupDetail> =>
  requestJson(`/groups/${groupId}`, { tag: TAG });

/** 改名 + 改说明。`kind` 刻意不可改(后端 `extra="forbid"` 直接 422),分类要变就重建。 */
export const updateGroup = (
  groupId: string,
  name: string,
  description: string,
): Promise<GroupDetail> =>
  requestJson(`/groups/${groupId}`, {
    method: "PATCH",
    body: JSON.stringify({ name, description }),
    tag: TAG,
  });

export const deleteGroup = (groupId: string): Promise<void> =>
  requestVoid(`/groups/${groupId}`, { method: "DELETE", tag: TAG });

/** 转让唯一 owner。目标必须是现有成员，服务端会原子提升为管理员；原 owner 保留管理员。 */
export const transferGroupOwner = (
  groupId: string,
  newOwnerId: string,
): Promise<GroupDetail> =>
  requestJson(`/groups/${groupId}/transfer`, {
    method: "POST",
    body: JSON.stringify({ new_owner_id: newOwnerId }),
    tag: TAG,
  });

export const putGroupMember = (
  groupId: string,
  userId: string,
  role: string,
): Promise<GroupDetail> =>
  requestJson(`/groups/${groupId}/members/${userId}`, {
    method: "PUT",
    body: JSON.stringify({ role }),
    tag: TAG,
  });

export const removeGroupMember = (groupId: string, userId: string): Promise<void> =>
  requestVoid(`/groups/${groupId}/members/${userId}`, { method: "DELETE", tag: TAG });

/** 自助退出。唯一的组管理员退出会被后端 409 挡住,文案直接上屏。 */
export const leaveGroup = (groupId: string): Promise<void> =>
  requestVoid(`/groups/${groupId}/membership`, { method: "DELETE", tag: TAG });

export const getGroupInvite = (groupId: string): Promise<GroupInviteState> =>
  requestJson(`/groups/${groupId}/invite-link`, { tag: TAG });

export const createGroupInvite = (groupId: string): Promise<GroupInviteState> =>
  requestJson(`/groups/${groupId}/invite-link`, { method: "POST", tag: TAG });

export const rotateGroupInvite = (groupId: string): Promise<GroupInviteState> =>
  requestJson(`/groups/${groupId}/invite-link/rotate`, { method: "POST", tag: TAG });

export const revokeGroupInvite = (groupId: string): Promise<void> =>
  requestVoid(`/groups/${groupId}/invite-link`, { method: "DELETE", tag: TAG });

/** Redeem is intentionally outside the group-id namespace: the token itself is authority. */
export const joinGroupInvite = (token: string): Promise<GroupDetail> =>
  requestJson(`/group-invites/${encodeURIComponent(token)}/join`, {
    method: "POST",
    tag: TAG,
  });

/** 按用户名**精确**查人。查不到是 404,由错误层翻成后端写好的文案。 */
export const resolveUser = (username: string): Promise<UserRef> =>
  requestJson(`/users/resolve?username=${encodeURIComponent(username)}`, { tag: TAG });

export const listGroupSharedNotebooks = (groupId: string): Promise<GroupSharedNotebook[]> =>
  requestJson(`/groups/${groupId}/shared-notebooks`, { tag: TAG });

/** 组管理员视角撤销:删掉这本库上指向本组的**全部**边。 */
export const revokeGroupSharedNotebook = (
  groupId: string,
  notebookId: string,
): Promise<void> =>
  requestVoid(`/groups/${groupId}/shared-notebooks/${notebookId}`, {
    method: "DELETE",
    tag: TAG,
  });

export const listNotebookGrants = (notebookId: string): Promise<NotebookGrant[]> =>
  requestJson(`/notebooks/${notebookId}/grants`, { tag: TAG });

/** 共享给群组。默认只发一条只读边(`group`/`viewer`)。
 *
 * `manage=true` 时**追加**一条 `group_admins`/`admin` 边(P2 的「组管理员可管理」勾选):
 * 组管理员因此获得这本库的内容管理权。两条边分两次 POST——先发只读边,成功后再发管理边
 * (顺序无所谓,权限由两条边各自的谓词独立生效),这样即使第二条失败,库主也至少完成了
 * 只读共享,不会两条都回滚成「什么都没共享」。 */
export const shareNotebookToGroup = (
  notebookId: string,
  groupId: string,
  opts: { manage?: boolean } = {},
): Promise<NotebookGrant> => {
  const viewer = requestJson<NotebookGrant>(`/notebooks/${notebookId}/grants`, {
    method: "POST",
    body: JSON.stringify({
      principal_type: "group",
      principal_id: groupId,
      role: "viewer",
    }),
    tag: TAG,
  });
  if (!opts.manage) return viewer;
  return viewer.then(async (grant) => {
    await grantGroupAdminsManage(notebookId, groupId);
    return grant;
  });
};

/**
 * 给一个**已经共享过**的群组补上「组管理员可管理」。
 *
 * 与 `shareNotebookToGroup({manage:true})` 发的是同一条边,所以抽成一个函数由两处共用:
 * 新建路径先发只读边再调它,已有共享则直接调它(那条只读边已经在库里了,重发会 409)。
 * 请求体只写一处,免得两个入口对同一件事有两种拼法。
 */
export const grantGroupAdminsManage = (
  notebookId: string,
  groupId: string,
): Promise<NotebookGrant> =>
  requestJson(`/notebooks/${notebookId}/grants`, {
    method: "POST",
    body: JSON.stringify({
      principal_type: "group_admins",
      principal_id: groupId,
      role: "admin",
    }),
    tag: TAG,
  });

export const revokeNotebookGrant = (notebookId: string, grantId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/grants/${grantId}`, { method: "DELETE", tag: TAG });

// --- 成员贡献审批流(群组知识共享 P2-T3) ------------------------------------

/** 提交共享申请:把这本库贡献给一个我只是普通成员的群组,由组管理员审批。 */
export const submitShareRequest = (
  notebookId: string,
  groupId: string,
): Promise<ShareRequest> =>
  requestJson(`/notebooks/${notebookId}/share-requests`, {
    method: "POST",
    body: JSON.stringify({ group_id: groupId }),
    tag: TAG,
  });

/** 我对这本库发起过的申请(弹窗回显待审批 / 已驳回)。只回本人发起的。 */
export const listMyShareRequests = (notebookId: string): Promise<ShareRequest[]> =>
  requestJson(`/notebooks/${notebookId}/share-requests`, { tag: TAG });

/** 撤回一条**待审批**申请(删整行)。已决定的会被后端 409 挡住,文案直接上屏。 */
export const withdrawShareRequest = (
  notebookId: string,
  requestId: string,
): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/share-requests/${requestId}`, {
    method: "DELETE",
    tag: TAG,
  });

/**
 * 我发起的、仍**待审批**的全部申请 —— 跨笔记本的**全局**入口,只要求登录。
 *
 * 与 `listMyShareRequests(notebookId)` 的区别是**授权轴**:那条挂在笔记本维度、要
 * `notebook:manage`,申请人一失去管理权就打不开;这条唯一的谓词是「这条申请是你提的」,
 * 与撤回端点逐字相同。裁决 P2-7 特意让撤回不要求笔记本权限(否则失权申请人的申请既批不了
 * 也撤不掉),而没有这条清单他连申请 id 都拿不回来 —— 那个口子在它唯一存在意义的场景里
 * 就是够不着的(codex #519 R11 P1)。
 */
export const listMyPendingShareRequests = (): Promise<ShareRequest[]> =>
  requestJson(`/me/share-requests`, { tag: TAG });

/** 组管理员的审核队列:共享给本组的待审批申请。 */
export const listGroupShareRequests = (groupId: string): Promise<ShareRequest[]> =>
  requestJson(`/groups/${groupId}/share-requests`, { tag: TAG });

/** 批准:同事务写 `(group, viewer)` 边并把申请标 approved。 */
export const approveShareRequest = (
  groupId: string,
  requestId: string,
): Promise<ShareRequest> =>
  requestJson(`/groups/${groupId}/share-requests/${requestId}/approve`, {
    method: "POST",
    tag: TAG,
  });

/** 驳回:把申请标 rejected,不发任何边。 */
export const rejectShareRequest = (
  groupId: string,
  requestId: string,
): Promise<ShareRequest> =>
  requestJson(`/groups/${groupId}/share-requests/${requestId}/reject`, {
    method: "POST",
    tag: TAG,
  });

// --- 输入护栏 ---------------------------------------------------------------

/**
 * 组名 / 组说明的长度上限。
 *
 * **与 `backend/app/api/group_routes.py` 的 `_MAX_GROUP_NAME_CHARS` /
 * `_MAX_GROUP_DESCRIPTION_CHARS` 同值**,改一侧就要改另一侧。
 *
 * 两侧都要有,是「数值上限与截断」红线的要求:用户编辑的数据不得静默截断——前端显示
 * 同一护栏(输入框直接敲不进去),API 超限**明确拒绝**(后端 400,不裁短了存)。少了
 * 前端这半,用户会敲完一长串才在提交时吃一个 400,而且不知道边界在哪。
 */
export const GROUP_INPUT_LIMITS = {
  nameMaxChars: 120,
  descriptionMaxChars: 1000,
} as const;

/** 接近上限时才出现的余量提示;还早的时候返回空串(不给一个常驻的计数噪音)。 */
export const groupLengthHint = (value: string, max: number): string => {
  const used = value.length;
  if (used >= max) return `已达上限 ${max} 个字`;
  if (used >= max - Math.max(1, Math.round(max * 0.1))) return `还可输入 ${max - used} 个字`;
  return "";
};

// --- 纯 helper(单测) --------------------------------------------------------

export const buildGroupInviteLink = (token: string, origin: string): string =>
  `${origin}/?group_invite=${encodeURIComponent(token)}`;

export const parseGroupInviteToken = (search: string): string | null => {
  const value = new URLSearchParams((search ?? "").replace(/^\?/, "")).get("group_invite");
  return value || null;
};

/** 群组分类的界面词。未知分类退成中性词,绝不把后端的英文 id 吐给用户。 */
export const groupKindLabel = (kind: string): string => label(GROUP_KIND, kind, "群组");

/** 组内角色的界面词。 */
export const groupRoleLabel = (role: string): string => label(GROUP_ROLE, role, "成员");

/** 我在这个组里是不是组管理员——「共享给群组」与组管理动作的唯一判据。 */
export const isGroupAdmin = (group: { my_role: string }): boolean =>
  group.my_role === "admin";

/**
 * 「共享给群组」的可选项 = 我担任组管理员的组。
 *
 * 按 `my_role` 过滤而不是全给:后端要求发边的人同时是那个组的组管理员,列出普通
 * 成员的组只会让用户点一次拿一个 403。这不是权限判定(权威判定在后端的写事务里,
 * 见 group_routes.create_notebook_grant_route),只是不给一个必然失败的入口。
 */
export const adminGroups = <T extends { my_role: string }>(groups: readonly T[]): T[] =>
  groups.filter((group) => isGroupAdmin(group));

/**
 * 我能建哪几类群组。项目组人人可建;部门/领域仅系统管理员可建(已定决策 2)。
 *
 * 前端只是不显示那两个选项——后端 `POST /groups` 上有同一道闸(403),这里的过滤
 * 是界面礼貌,不是安全边界。
 */
export const creatableGroupKinds = (role: string): string[] =>
  role === "admin" ? ["project", "department", "domain"] : ["project"];

/** 折叠项背后的一条边:撤销要按 id 逐条删,而排序要看 role,两个字段缺一不可。 */
export type GroupShareGrantRef = { id: string; role: string };

/** 一条已共享给群组的记录(同一个组的多条边折成一项)。 */
export type GroupShareEntry = {
  groupId: string;
  name: string;
  kind: string;
  /** 组已不存在(孤儿边):名字解析不出来,只能给用户一个删除入口。 */
  missing: boolean;
  /** 有一条 `group_admins`/`admin` 边——组管理员可管理这本库(标注「可管理」)。 */
  manage: boolean;
  /** 这一项背后的全部边——撤销时要逐条删掉,**顺序**见 `revocationOrder`。 */
  grants: GroupShareGrantRef[];
};

/**
 * 这条边给不给管理权。**只看 `role`**,对 `group` 与 `group_admins` 两种主体一视同仁
 * ——与后端 `_admin_principal_match_expr` 逐字对齐:那三条臂(`user`/`group`/
 * `group_admins`)都只要求 `role='admin'`。
 *
 * 所以一条 `(group, role='admin')` 边把管理权给了**整组每个成员**,把它标成「不可管理」
 * 会让用户看到的权限比实际**小**(codex #519 R4 P2);反过来一条 `(group_admins, viewer)`
 * 边一点管理权都不给,只看主体类型又会标大(codex #519 R1 P2)。两次修的是同一处:
 * R1 补了 role 判定但把类型判定留窄了。
 *
 * ⚠ 提成模块级共用函数,是因为它现在有**两个**消费者,而它们必须是同一个判据:
 * `foldGroupShares` 用它算「要不要标注可管理」,`revocationOrder` 用它决定「哪条边最后删」。
 * 分成两份写法,总有一天会出现「标着可管理、却没被排到最后」的边——那正是 R7 P2 的形态。
 */
export const confersManage = (grant: { role: string }): boolean => grant.role === "admin";

/**
 * 撤销顺序:**给管理权的边(`role === "admin"`)排到最后**(codex #519 R7 P2)。
 *
 * 撤销是逐条 `DELETE`,而这三个 grant 端点的守卫都是 `notebook:manage`(admin 档)——
 * 也就是说**组管理员**(非库主)也能进这个面板。他对这本库的管理权恰恰来自那条
 * `(group_admins, admin)` 边:先删它,他当场就失去了继续删的权限,第二次 DELETE 拿 404,
 * 于是 `(group, viewer)` 边**仍然生效**、整组人照样读得到,而界面已经报了「撤销」。
 *
 * 而「先发 admin 边、后发 viewer 边」正是 `shareNotebookToGroup` 的**默认顺序**,所以
 * 后端按 `created_at` 返回时 admin 边天然排在前——这不是边角情形,是主路径。
 *
 * 库主不受影响(owner 臂恒成立),但顺序对他也无害:同一份顺序两种人都对。
 */
export const revocationOrder = (entry: {
  grants: readonly GroupShareGrantRef[];
}): string[] => [
  ...entry.grants.filter((grant) => !confersManage(grant)).map((grant) => grant.id),
  ...entry.grants.filter(confersManage).map((grant) => grant.id),
];

/**
 * 「取消组管理员管理」要删的那几条边 —— **只**删授予管理权的,读权边原样保留。
 *
 * 判据同样是 `confersManage`(不是「主体类型是不是 `group_admins`」):一条
 * `(group, admin)` 边把管理权给了整组每个成员,按主体类型挑就会漏掉它、点了没反应。
 */
export const manageGrantIds = (entry: {
  grants: readonly GroupShareGrantRef[];
}): string[] => entry.grants.filter(confersManage).map((grant) => grant.id);

/**
 * 取消管理权之后**还剩得下读权**吗?
 *
 * 标准模板(`(group_admins, admin)` + `(group, viewer)`)为真:删掉管理边,只读边还在。
 * 但一条孤零零的 `(group, admin)` 边**既是**读权又是管理权,删了它这个组就什么都看不到
 * 了——那不叫「取消管理权」,那叫撤销共享。这种形态界面因此不给「取消管理权」入口,
 * 用户要收回就走「撤销共享」(语义诚实),想改成只读则撤销后重新共享。
 *
 * 界面自己创建的共享永远是标准模板(先发只读边、再按勾选补管理边),批准共享申请写的也是
 * `(group, viewer)`,所以这个分支只可能来自 API 直调或历史数据。
 */
export const canDropManage = (entry: {
  grants: readonly GroupShareGrantRef[];
}): boolean => entry.grants.some((grant) => !confersManage(grant));

/**
 * 授权边清单 → 界面上的「已共享给群组」条目。
 *
 * 两件事在这里发生,都不能挪到渲染里各写一遍:
 *
 * 1. **只留群组主体**。后端如实返回四类主体(那份清单是库主「谁能看我的库」的完整
 *    答案),但 `user` 主体是只读共享、`everyone` 是公共知识库,两者各有自己的界面
 *    表达;把它们混进「共享给群组」会给用户一个删不掉、也看不懂的条目。
 * 2. **同组两条边折成一项**。设计上「成员只读 + 组管理员可管」是两行
 *    (`group` / `group_admins`),但对用户是一件事:这个库共享给了这个组。
 */
export const foldGroupShares = (grants: readonly NotebookGrant[]): GroupShareEntry[] => {
  const out: GroupShareEntry[] = [];
  const byPrincipal = new Map<string, GroupShareEntry>();
  for (const grant of grants) {
    if (grant.principal_type !== "group" && grant.principal_type !== "group_admins") continue;
    // 管理权判据见 `confersManage`(与后端 `_admin_principal_match_expr` 逐字对齐)。
    // 两条边任意顺序返回都成立(OR 累积)。
    const grantsManage = confersManage(grant);
    const existing = byPrincipal.get(grant.principal_id);
    if (existing) {
      // 原样按返回序累积;撤销那一侧要的顺序由 `revocationOrder` 现算,不在这里排
      // ——这份数组还要服务「这一项背后有哪几条边」这个与顺序无关的用途。
      existing.grants.push({ id: grant.id, role: grant.role });
      existing.manage = existing.manage || grantsManage;
      continue;
    }
    const entry: GroupShareEntry = {
      groupId: grant.principal_id,
      name: grant.principal_name,
      kind: grant.principal_kind,
      missing: grant.principal_kind === "missing",
      manage: grantsManage,
      grants: [{ id: grant.id, role: grant.role }],
    };
    byPrincipal.set(grant.principal_id, entry);
    out.push(entry);
  }
  return out;
};

/** 已经共享过的组不再出现在选择器里——重复发边后端会 409。 */
export const shareableGroups = <T extends { id: string; my_role: string }>(
  groups: readonly T[],
  shared: readonly GroupShareEntry[],
): T[] => {
  const taken = new Set(shared.map((entry) => entry.groupId));
  return adminGroups(groups).filter((group) => !taken.has(group.id));
};

/** 我在这个组里只是**普通成员**(不是组管理员,也不是非成员)。 */
export const isPlainMember = (group: { my_role: string }): boolean =>
  group.my_role === "member";

/**
 * 「提交共享申请」的可选项 = 我只是普通成员的组,且既没共享过、也没有正在待审批的申请。
 *
 * 与 `shareableGroups`(我担任组管理员的组、可直接发边)**互斥**:组管理员不必申请,
 * 直接发边;普通成员发不了边,只能申请。已共享的组从两个入口都消失(共享是终态);
 * 已有 pending 申请的组也不再出现(重复提交后端会幂等返回,但界面不该给一个「再申请」
 * 的入口——那让人以为能提交第二份)。已驳回(rejected)**不**挡在这里:被驳回后用户可以
 * 重新申请,所以只按 pending 过滤。
 */
export const requestableGroups = <T extends { id: string; my_role: string }>(
  groups: readonly T[],
  shared: readonly GroupShareEntry[],
  requests: readonly ShareRequest[],
): T[] => {
  const taken = new Set(shared.map((entry) => entry.groupId));
  const pending = new Set(
    requests.filter((r) => r.status === "pending").map((r) => r.group_id),
  );
  return groups.filter(
    (group) => isPlainMember(group) && !taken.has(group.id) && !pending.has(group.id),
  );
};

/** 共享申请状态的界面词。未知状态退成中性词,绝不吐后端的英文 id。 */
export const shareRequestStatusLabel = (status: string): string =>
  status === "pending" ? "待审批" : status === "approved" ? "已批准" : status === "rejected" ? "已驳回" : "申请";

/** 弹窗里要回显的申请 = 尚未完成的(待审批)或刚被驳回的;已批准的已经变成共享条目,不再单列。 */
export const visibleMyShareRequests = (
  requests: readonly ShareRequest[],
): ShareRequest[] => requests.filter((r) => r.status === "pending" || r.status === "rejected");

// --- 笔记本列表侧的纯 helper -------------------------------------------------

/** 结构性最小形状:只要求读得到 `granted_via`,不绑死整个 NotebookSummary。 */
type MaybeGranted = { granted_via?: GrantedGroupRef[] };

/** 这本笔记本是不是**经群组**共享进来的。 */
export const isGroupGranted = (notebook: MaybeGranted): boolean =>
  (notebook.granted_via ?? []).length > 0;

/**
 * 卡片上的来源标注:「来自群组《X》」。
 *
 * 多个组同时共享同一本库时全部点名——用户要知道是哪几层关系让他看到这本库,只写
 * 第一个会让「退出了 A 组还看得见」变成一件说不通的事。
 */
export const grantedViaLabel = (notebook: MaybeGranted): string => {
  const groups = notebook.granted_via ?? [];
  if (groups.length === 0) return "";
  return `来自群组《${groups.map((group) => group.group_name).join("》《")}》`;
};

/**
 * 列表分区:经群组共享进来的库单独成一区(设计决策 10)。
 *
 * 判据是 `granted_via` 非空而不是 `access === "reader"`:只读共享(分享链接)进来
 * 的库同样是 reader,但它有「退出共享」这个用户自己能按的出口,而群组共享没有
 * (那个按钮打的是成员表,对授权边一点作用都没有)。两者必须分开。
 */
export const partitionByGrant = <T extends { notebook: MaybeGranted }>(
  entries: readonly T[],
): { personal: T[]; group: T[] } => ({
  personal: entries.filter((entry) => !isGroupGranted(entry.notebook)),
  group: entries.filter((entry) => isGroupGranted(entry.notebook)),
});
