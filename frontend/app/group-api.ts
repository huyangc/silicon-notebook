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
  /** 请求者本人在该组里的角色;不是成员时为空串(管理员的全部群组视图会出现)。 */
  my_role: string;
  member_count: number;
  created_at: string;
};

export type GroupDetail = GroupSummary & { members: GroupMember[] };

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

/** `NotebookSummary.granted_via` 的元素:这本笔记本是经哪个群组共享给我的。 */
export type GrantedGroupRef = {
  group_id: string;
  group_name: string;
  kind: string;
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

export const renameGroup = (groupId: string, name: string): Promise<GroupDetail> =>
  requestJson(`/groups/${groupId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
    tag: TAG,
  });

export const deleteGroup = (groupId: string): Promise<void> =>
  requestVoid(`/groups/${groupId}`, { method: "DELETE", tag: TAG });

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

/** 共享给群组。P1 只发一条只读边,理由见文件顶部第 2 条。 */
export const shareNotebookToGroup = (
  notebookId: string,
  groupId: string,
): Promise<NotebookGrant> =>
  requestJson(`/notebooks/${notebookId}/grants`, {
    method: "POST",
    body: JSON.stringify({
      principal_type: "group",
      principal_id: groupId,
      role: "viewer",
    }),
    tag: TAG,
  });

export const revokeNotebookGrant = (notebookId: string, grantId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/grants/${grantId}`, { method: "DELETE", tag: TAG });

// --- 纯 helper(单测) --------------------------------------------------------

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

/** 一条已共享给群组的记录(同一个组的多条边折成一项)。 */
export type GroupShareEntry = {
  groupId: string;
  name: string;
  kind: string;
  /** 组已不存在(孤儿边):名字解析不出来,只能给用户一个删除入口。 */
  missing: boolean;
  /** 这一项背后的全部边 id——撤销时要逐条删掉。 */
  grantIds: string[];
};

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
    const existing = byPrincipal.get(grant.principal_id);
    if (existing) {
      existing.grantIds.push(grant.id);
      continue;
    }
    const entry: GroupShareEntry = {
      groupId: grant.principal_id,
      name: grant.principal_name,
      kind: grant.principal_kind,
      missing: grant.principal_kind === "missing",
      grantIds: [grant.id],
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
