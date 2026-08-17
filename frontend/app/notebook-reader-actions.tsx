"use client";

import type { MouseEvent } from "react";

import { grantedViaLabel, isGroupGranted } from "./group-api.ts";
import type { NotebookSummary } from "./workspace-model.ts";

/**
 * 「只读进来的库」在两处的呈现:工作区顶栏与笔记本卡片的操作菜单。
 *
 * 抽成组件而不是留在 `page.tsx` 里,是为了这条不变量能被**真的**测到:
 *
 *   经**群组**共享进来的库不给「退出共享」。
 *
 * 那个按钮打的是 `DELETE /notebooks/{id}/membership`,它只删 `notebook_members` 行,
 * 对群组授权边一点作用都没有——点了会弹一句「已退出」,而库还在列表里,是一个必然
 * 发生的假失败。它在 `page.tsx` 里时只能靠「源码文本里有没有那个三元」来守,而那种
 * 守卫挡不住把按钮平移出条件分支的变异(改动之后文本仍在)。搬到这里,组件测试可以
 * 直接渲染两种笔记本、断言按钮在与不在。
 *
 * 判据是 `granted_via` 非空而不是 `access` —— 只读共享同样是 reader,但它有「退出
 * 共享」这个用户自己能按的出口,群组共享没有。
 */

type ReaderNotebookBadgeProps = {
  notebook: NotebookSummary;
  leaveBusy: boolean;
  onLeave: () => void;
};

/** 工作区顶栏:只读徽章 + (仅只读共享)退出入口 / (群组共享)由谁管理的说明。 */
export function ReaderNotebookBadge({
  notebook,
  leaveBusy,
  onLeave,
}: ReaderNotebookBadgeProps) {
  const granted = isGroupGranted(notebook);
  return (
    <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
      <h1 className="notebook-title-input" style={{ margin: 0 }}>{notebook.name}</h1>
      <span className="new-pill" title="只读，无写权限">
        {granted
          ? `只读 · ${grantedViaLabel(notebook)}`
          : `只读 · 来自 ${notebook.shared_from || "他人"}`}
      </span>
      {granted ? (
        <span className="tool-hint">
          由组管理员管理；要停止访问，请联系组管理员撤销共享，或在账户菜单的「群组」里退出该群组。
        </span>
      ) : (
        <button
          className="sort-button"
          disabled={leaveBusy}
          title="退出该只读共享（仅移除你自己的访问）"
          onClick={onLeave}
        >
          {leaveBusy ? "退出中…" : "退出共享"}
        </button>
      )}
    </div>
  );
}

type NotebookMenuActionsProps = {
  notebook: NotebookSummary;
  onLeave: () => void;
  onEdit: () => void;
  onDelete: (event: MouseEvent<HTMLButtonElement>) => void;
};

/** 笔记本卡片的操作菜单:reader 只有退出(群组共享连退出都没有),owner 是编辑/删除。 */
export function NotebookMenuActions({
  notebook,
  onLeave,
  onEdit,
  onDelete,
}: NotebookMenuActionsProps) {
  if ((notebook.access ?? "owner") === "reader") {
    return isGroupGranted(notebook)
      ? <span className="notebook-menu-note">由组管理员管理</span>
      : <button className="danger" onClick={onLeave}>退出共享</button>;
  }
  return (
    <>
      <button onClick={onEdit}>编辑信息</button>
      <button className="danger" onClick={onDelete}>删除笔记本</button>
    </>
  );
}
