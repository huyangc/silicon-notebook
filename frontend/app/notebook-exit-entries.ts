// 只读进来的库(以及点名编辑者)在顶栏与卡片菜单上的「退出」入口 —— 纯逻辑,组件测试与
// 单测都从这里取。分享编辑与协作 P1,规格 §4.4「既是只读成员又是编辑者时的两个『退出』」。
//
// 一个人可以经几条路读到同一本库:成员行(经链接加入)、点名授权(`collaborator_role`:
// editor,或同步/历史留下的 viewer / admin)、群组授权(`granted_via`)。每条出口只删自己
// 那一条,互不代删;最后一条读权被删时才触发 Memory 告知与删除(两个端点同一份合同,见
// notebook-exit-api.ts)。入口按下面的逐格表给:
//
// | collaborator_role | 成员行 | 入口 |
// | --- | --- | --- |
// | ""(无点名授权) | — | 非群组共享:「退出共享」→ 成员关系;群组共享:无(由组管理员管理) |
// | editor | 无 | 「退出协作」→ 点名授权(还经群组可读时写「退出协作（保留只读）」) |
// | editor | 有 | 「退出协作（保留只读）」→ 点名授权;「退出共享」→ 成员关系,附「你仍是这本笔记本的编辑者」 |
// | viewer / admin | 无 | 「退出共享」→ 点名授权 |
// | viewer / admin | 有 | 「退出共享」→ 先成员关系、再点名授权(两条都是「共享给我」,用户要的是都退) |
//
// 最后一格是唯一的两步计划。不拆成两颗按钮:两条读权对用户都叫「共享」,拆开就得给其中一颗
// 起一个用户分不清的名字;合成一步又会让第一条退掉之后报「已退出」而库还在。两步各自走完整
// 的告知→确认→DELETE,前一步确认退成才进下一步(use-notebook-exit.ts),不会假成功。

import { isGroupGranted } from "./group-api.ts";
import { MEMBERSHIP_EXIT, type ExitRoute } from "./notebook-exit-api.ts";
import { STILL_EDITOR, STILL_MEMBER, STILL_NAMED } from "./notebook-exit-copy.ts";
import type { NotebookSummary } from "./workspace-model.ts";

/**
 * 一次「退出」要走哪几条出口、入口叫什么。
 */
export type ExitPlan = {
  steps: readonly ExitRoute[];
  /** 入口与确认面板上的动作名:「退出共享」「退出协作」「退出协作（保留只读）」。 */
  label: string;
  /** 退出没有完成时仍然成立的身份(「你仍是成员」「你仍是编辑者」…)。 */
  still: string;
  /** 成功提示的补充句(例如「你仍是这本笔记本的编辑者」);空串不加。 */
  leftNote: string;
};

export const MEMBERSHIP_EXIT_PLAN: ExitPlan = {
  steps: [MEMBERSHIP_EXIT],
  label: "退出共享",
  still: STILL_MEMBER,
  leftNote: "",
};

/** 仍是编辑者时「退出共享」旁的那句话(规格 §4.4)。 */
export const STILL_EDITOR_NOTE = "你仍是这本笔记本的编辑者";
/** 退出协作之后仍经成员行或群组可读时的成功补充句。 */
export const KEEPS_READING_NOTE = "你仍可以阅读这本笔记本";

export const LEAVE_COLLABORATION_LABEL = "退出协作";
export const LEAVE_COLLABORATION_KEEP_READING_LABEL = "退出协作（保留只读）";
export const LEAVE_SHARE_LABEL = "退出共享";

export type ExitEntry = {
  /** 稳定身份(React key、测试)。 */
  key: "collaboration" | "share";
  label: string;
  plan: ExitPlan;
  /** 入口旁的补充说明(空串 = 不写)。 */
  note: string;
};

/**
 * 这本库给当前用户的退出入口。`selfUserId` 是当前登录用户的 id——点名授权的出口地址里要它;
 * 拿不到时(还没加载完 /me)不给点名授权那几颗,宁可少一个入口也不发一条打不中的 DELETE。
 * owner 没有退出入口。
 */
export function readerExitEntries(
  notebook: Pick<NotebookSummary, "access" | "collaborator_role" | "collaborator_is_member" | "granted_via">,
  selfUserId: string,
): ExitEntry[] {
  if ((notebook.access ?? "owner") !== "reader") return [];
  const role = notebook.collaborator_role ?? "";
  const member = Boolean(role && notebook.collaborator_is_member);
  const granted = isGroupGranted(notebook);
  if (!role) {
    return granted ? [] : [{ key: "share", label: LEAVE_SHARE_LABEL, plan: MEMBERSHIP_EXIT_PLAN, note: "" }];
  }
  if (!selfUserId) return [];
  const named: ExitRoute = { kind: "collaborator", userId: selfUserId };
  if (role === "editor") {
    const keepsReading = member || granted;
    const label = keepsReading ? LEAVE_COLLABORATION_KEEP_READING_LABEL : LEAVE_COLLABORATION_LABEL;
    const entries: ExitEntry[] = [{
      key: "collaboration",
      label,
      plan: { steps: [named], label, still: STILL_EDITOR, leftNote: keepsReading ? KEEPS_READING_NOTE : "" },
      note: "",
    }];
    if (member) {
      entries.push({
        key: "share",
        label: LEAVE_SHARE_LABEL,
        plan: { steps: [MEMBERSHIP_EXIT], label: LEAVE_SHARE_LABEL, still: STILL_MEMBER, leftNote: STILL_EDITOR_NOTE },
        note: STILL_EDITOR_NOTE,
      });
    }
    return entries;
  }
  return [{
    key: "share",
    label: LEAVE_SHARE_LABEL,
    plan: {
      steps: member ? [MEMBERSHIP_EXIT, named] : [named],
      label: LEAVE_SHARE_LABEL,
      still: STILL_NAMED,
      leftNote: "",
    },
    note: "",
  }];
}

/**
 * 徽章上的身份词与来路(「可编辑 · 来自 张三」)。
 *
 * 身份词按权限档:管理权(owner 之外 = admin 授权)→「可管理」,编辑权 →「可编辑」,其余「只读」。
 * 来路:点名编辑者写「来自 {库主}」(他是被库主点名的,即便同时在某个群组里);其余群组共享
 * 写群组名,链接/点名只读写库主。
 */
export function readerBadgeWords(
  notebook: Pick<NotebookSummary, "can_manage_content" | "can_manage_notebook" | "collaborator_role" | "granted_via">,
): { accessWord: string; viaGroup: boolean } {
  const accessWord = notebook.can_manage_notebook
    ? "可管理"
    : notebook.can_manage_content ? "可编辑" : "只读";
  const editor = notebook.collaborator_role === "editor" && !notebook.can_manage_notebook;
  return { accessWord, viaGroup: isGroupGranted(notebook) && !editor };
}
