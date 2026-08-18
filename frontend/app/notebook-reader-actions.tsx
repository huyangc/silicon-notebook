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
 *
 * 群组知识共享 P2 之后还有第二条不变量:
 *
 *   `access === "reader"` 不再等于「只读」。
 *
 * 组管理员打开被共享进本组的库时 `access` 仍是 reader(权限档没有新增枚举值,裁决
 * P2-3),但他有内容管理权。所以徽章文案按 `can_manage_content` 分岔:写着「只读」
 * 而整屏写入口都亮着,是一句当场自相矛盾的话。他仍然**不是** owner——删库仍恒 owner,
 * 所以卡片菜单只给「编辑信息」,不给「删除笔记本」。
 */

type ReaderNotebookBadgeProps = {
  notebook: NotebookSummary;
  leaveBusy: boolean;
  onLeave: () => void;
};

/**
 * 工作区顶栏:身份徽章 + (仅只读共享)退出入口 / (群组共享)由谁管理的说明。
 *
 * 三种形态:
 * - 只读共享(分享链接):`只读 · 来自 X` + 「退出共享」按钮;
 * - 群组共享、无管理权:`只读 · 来自群组《X》` + 由组管理员管理的说明;
 * - 群组共享、有管理权:`可管理 · 来自群组《X》` + 说明改成「你是该群组的管理员」。
 */
export function ReaderNotebookBadge({
  notebook,
  leaveBusy,
  onLeave,
}: ReaderNotebookBadgeProps) {
  const granted = isGroupGranted(notebook);
  const canManage = Boolean(notebook.can_manage_content);
  const accessWord = canManage ? "可管理" : "只读";
  return (
    <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
      <h1 className="notebook-title-input" style={{ margin: 0 }}>{notebook.name}</h1>
      <span
        className="new-pill"
        title={canManage ? "你可以管理这本笔记本的内容，但它不属于你" : "只读，无写权限"}
      >
        {granted
          ? `${accessWord} · ${grantedViaLabel(notebook)}`
          : `${accessWord} · 来自 ${notebook.shared_from || "他人"}`}
      </span>
      {granted ? (
        <span className="tool-hint">
          {canManage
            ? "你是该群组的管理员，可以管理这本笔记本的内容；它仍属于原作者，删除笔记本只有作者本人可以做。"
            : "由组管理员管理；要停止访问，请联系组管理员撤销共享，或在账户菜单的「群组」里退出该群组。"}
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

/**
 * 笔记本卡片的操作菜单。
 *
 * - owner:编辑信息 + 删除笔记本;
 * - 组管理员(reader + `can_manage_content`):编辑信息 + 由组管理员管理的说明,
 *   **没有删除**——`notebook:delete` 恒 owner(裁决 P2-1),画出来只会 404;
 *   也没有「退出共享」(他的访问来自授权边,那个按钮打的是成员表,点了是假成功);
 * - 群组共享的纯只读成员:只有说明;
 * - 只读共享(分享链接):退出共享。
 */
export function NotebookMenuActions({
  notebook,
  onLeave,
  onEdit,
  onDelete,
}: NotebookMenuActionsProps) {
  if ((notebook.access ?? "owner") === "reader") {
    if (!isGroupGranted(notebook)) {
      return <button className="danger" onClick={onLeave}>退出共享</button>;
    }
    return notebook.can_manage_content ? (
      <>
        <button onClick={onEdit}>编辑信息</button>
        <span className="notebook-menu-note">由组管理员管理</span>
      </>
    ) : (
      <span className="notebook-menu-note">由组管理员管理</span>
    );
  }
  return (
    <>
      <button onClick={onEdit}>编辑信息</button>
      <button className="danger" onClick={onDelete}>删除笔记本</button>
    </>
  );
}
