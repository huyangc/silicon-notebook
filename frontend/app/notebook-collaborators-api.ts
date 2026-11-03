// 「点名共享」的客户端(分享编辑与协作 P1-T13)。
//
// 后端合同见 `backend/app/api/notebook_collaborator_routes.py`:
//
//  * `GET  /notebooks/{id}/collaborators`:库主拿到全部点名授权(`view:"owner"`,
//    `items`),持有 editor 授权的人只拿到编辑者名字(`view:"editor"`,`editors`,
//    决策 D17),其他人 404。
//  * `PUT  /notebooks/{id}/collaborators/{user_id}`:库主把这个人**设为**编辑者——
//    已有只读点名授权的升为编辑者,遗留的管理权限点名授权**降为**编辑者。幂等:
//    本来就是编辑者时 `changed=false`。
//  * `DELETE /notebooks/{id}/collaborators/{user_id}`:库主移除别人的点名授权(不论
//    角色)。删自己是「退出协作」,走另一套带 Memory 告知的合同,不在本模块。
//
// 传输一律经 api-client;本模块不依赖 React,纯 helper 由组件测试覆盖。

import { requestJson, requestVoid } from "./api-client.ts";
import type { MountedBase } from "./notebook-bases.ts";
import { COLLABORATOR_ROLE, label } from "./vocabulary.ts";

const TAG = "collaborators";

/** 库主视角的一条点名授权。`role` 可能是 editor / viewer / 遗留的 admin。 */
export type CollaboratorItem = {
  id: string;
  user_id: string;
  username: string;
  display_name: string;
  role: string;
  granted_by_username: string;
  created_at: string;
};

/** 决策 D17:编辑者看得到的其他编辑者。 */
export type EditorName = { username: string; display_name: string };

export type CollaboratorList = {
  view: "owner" | "editor";
  items: CollaboratorItem[];
  editors: EditorName[];
};

export type CollaboratorPutResult = {
  grant: CollaboratorItem;
  changed: boolean;
  previous_role: string | null;
};

const collaboratorsPath = (notebookId: string) =>
  `/notebooks/${encodeURIComponent(notebookId)}/collaborators`;

export const listCollaborators = (notebookId: string): Promise<CollaboratorList> =>
  requestJson(collaboratorsPath(notebookId), { tag: TAG });

export const addEditor = (notebookId: string, userId: string): Promise<CollaboratorPutResult> =>
  requestJson(`${collaboratorsPath(notebookId)}/${encodeURIComponent(userId)}`, {
    method: "PUT",
    tag: TAG,
  });

/** 库主移除**别人**的点名授权。本人退出协作不走这里。 */
export const removeCollaborator = (notebookId: string, userId: string): Promise<void> =>
  requestVoid(`${collaboratorsPath(notebookId)}/${encodeURIComponent(userId)}`, {
    method: "DELETE",
    tag: TAG,
  });

/** 名单与徽章上的角色词。查不到的历史值给中性兜底,不把原值上屏。 */
export const collaboratorRoleLabel = (role: string): string =>
  label(COLLABORATOR_ROLE, role, "其他权限");

/** 「张三（zhangsan）」;没有显示名时只给用户名。 */
export const collaboratorName = (person: { username: string; display_name?: string }): string => {
  const display = (person.display_name ?? "").trim();
  return display && display !== person.username ? `${display}（${person.username}）` : person.username;
};

/**
 * 添加编辑者之后会对所有人失效的参考库(决策 D13)。判据只认后端算好的
 * `closes_when_shared`——「此刻有效、且只靠借入臂有效」,前端不按 tier / active 自己猜。
 */
export const basesClosedBySharing = (bases: readonly MountedBase[]): MountedBase[] =>
  bases.filter((base) => base.closes_when_shared === true);
