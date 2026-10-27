/**
 * 「取消链接分享」的确认弹窗内容：分享弹窗与「已分享」总览两个入口共用这一份，
 * 后果句、按钮文字与「取消确认就什么都不做」只有一个出处。
 *
 * 取消链接分享不可撤销：链接立即失效、重开是另一条链接；已通过链接加入的成员被移除，
 * 他们在这本笔记本里的检索心得和 Agent 记录随之清空（个人记忆不删）。所以两个入口都
 * 必须先过这一步确认，不能点一下就执行。
 *
 * 后果句刻意**不报人数**（codex #631 R5 P2）：入口手里的成员名单是打开弹窗那一刻的
 * 快照，copy 档的行后端根本不带名单，按它报数只会少说——破坏性确认最不该犯的错。
 */
import type { InfoModalAction } from "./info-modal-actions";

export const UNSHARE_LINK_LABEL = "取消链接分享";
export const KEEP_LINK_LABEL = "保留链接";

export type UnshareLinkConfirmation = {
  title: string;
  message: string;
  actions: InfoModalAction[];
};

export function unshareLinkConfirmation({
  notebookName,
  mentionGroups,
  onConfirm,
}: {
  notebookName: string;
  /** 补一句「共享给群组的部分不受影响」：入口知道这本笔记本也共享给了群组，或就摆在群组区旁边。 */
  mentionGroups: boolean;
  onConfirm: () => void;
}): UnshareLinkConfirmation {
  const consequences = [
    "链接立即失效，拿到旧链接的人不能再打开；已通过这条链接加入的只读成员会被一并移除。",
    "他们在这本笔记本里的检索心得和 Agent 记录会被清空，个人记忆不会删除。",
    "重新开启会生成另一条链接，旧链接不会恢复。",
    mentionGroups ? "共享给群组的部分不受影响。" : "",
  ].filter(Boolean);
  return {
    title: `取消「${notebookName}」的链接分享`,
    message: consequences.join(""),
    actions: [
      { label: KEEP_LINK_LABEL, action: () => {} },
      { label: UNSHARE_LINK_LABEL, danger: true, action: onConfirm },
    ],
  };
}
