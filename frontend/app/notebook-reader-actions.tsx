"use client";

import { useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent } from "react";

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

/**
 * 组管理员的行内改名(群组知识共享 P2-T2 评审 P2-4)。
 *
 * 改名走 PATCH /notebooks/{id}（notebook:manage,组管理员 ✓,后端实测 200)——**不是**
 * 那个 fused 编辑器(它连挂载配置一起拉,是 owner-only 的 notebook:configure)。所以
 * owner 的顶栏输入框与组管理员的这个共用同一个 PATCH-only handler,只是渲染位置不同:
 * owner 走 page.tsx 自己的 <input>,组管理员走**徽章内**的这个,好保住「可管理·来自
 * 群组《X》」的身份标注。纯只读成员不传它(下面按 can_manage_content 才启用)。
 */
type BadgeRenameProps = {
  value: string;
  saving: boolean;
  onChange: (value: string) => void;
  /** blur / Enter:提交改名。 */
  onCommit: () => void;
  /** Escape:丢弃草稿、复位。 */
  onReset: () => void;
};

type ReaderNotebookBadgeProps = {
  notebook: NotebookSummary;
  leaveBusy: boolean;
  onLeave: () => void;
  /** 组管理员改名(可选);仅当 `notebook.can_manage_content` 时真正渲染成可编辑。 */
  rename?: BadgeRenameProps;
};

/**
 * 身份说明只进 tooltip,不进顶栏正文。
 *
 * 它原本是一句 `.tool-hint` 长文,和标题、徽章挤在同一个 `.tag-row` 里。那个类
 * `flex-wrap: wrap`,而 `.workspace-header` 是固定 72px 单行——三样东西排成三行、
 * 在 72px 里居中,结果标题被推到可视区**之上**(实测 innerH 141 > 72,标题整个看不见),
 * 说明文字漏到标题栏外面盖住下方内容。用户反馈的「点进来完全看不到 notebook 名字」
 * 就是它。版式已由 `.reader-badge-row` 收口,但那句话本身也不该占着顶栏:
 *
 *   「要停止访问,请联系组管理员撤销共享」——用户明确说不需要这条(2026-08-20)。
 *
 * 访问的开关本来就不在这个人手里(群组共享没有自助退出,那正是下面 `!granted` 才渲染
 * 「退出共享」的原因),把一条他按不动的操作指引常驻在标题旁边,既占地方又帮不上忙。
 */
function badgeHint(granted: boolean, canManage: boolean): string {
  if (!granted) return "只读，无写权限";
  return canManage
    ? "你可以管理这本笔记本的内容，但它属于原作者"
    : "只读，无写权限。这本笔记本由组管理员共享给群组";
}

/**
 * 工作区顶栏:库名 + 身份徽章 + (仅链接共享)退出入口。
 *
 * 三种形态:
 * - 只读共享(分享链接):`只读 · 来自 X` + 「退出共享」按钮;
 * - 群组共享、无管理权:`只读 · 来自群组《X》`;
 * - 群组共享、有管理权:`可管理 · 来自群组《X》`,库名就地可改。
 *
 * 恒单行:版式约定见 `globals.css` 的 `.reader-badge-row`。
 */
export function ReaderNotebookBadge({
  notebook,
  leaveBusy,
  onLeave,
  rename,
}: ReaderNotebookBadgeProps) {
  const granted = isGroupGranted(notebook);
  const canManage = Boolean(notebook.can_manage_content);
  const accessWord = canManage ? "可管理" : "只读";
  const badgeLabel = granted
    ? `${accessWord} · ${grantedViaLabel(notebook)}`
    : `${accessWord} · 来自 ${notebook.shared_from || "他人"}`;
  // Escape 之后紧跟的那次 blur **不提交**。`onReset()` 只是排一次 state 更新,而同一个
  // 事件里立刻调用的 `blur()` 会同步触发 onBlur——它读到的仍是本次渲染的**旧**编辑值,
  // 于是「取消」反而把已被撤销的标题 PATCH 了出去(codex #519 R1 P2)。用 ref 而不是
  // state 抑制:它必须在同一个事件循环里立即生效,state 更新做不到。
  const cancelledRef = useRef(false);
  return (
    <div className="reader-badge-row">
      {canManage && rename ? (
        <input
          className="notebook-title-input reader-badge-title"
          value={rename.value}
          disabled={rename.saving}
          aria-label="笔记本名称"
          maxLength={80}
          onChange={(event) => rename.onChange(event.target.value)}
          onBlur={() => {
            if (cancelledRef.current) {
              cancelledRef.current = false;
              return;
            }
            rename.onCommit();
          }}
          onKeyDown={(event: ReactKeyboardEvent<HTMLInputElement>) => {
            if (event.key === "Enter") event.currentTarget.blur();
            if (event.key === "Escape") {
              cancelledRef.current = true;
              rename.onReset();
              event.currentTarget.blur();
            }
          }}
        />
      ) : (
        // 只读成员没有改名权,所以**不**戴 `.notebook-title-input`——那个类带着 hover/
        // focus 的输入框态,套在一个点不动的标题上是在承诺一个不存在的交互。
        <h1 className="reader-badge-title" title={notebook.name}>{notebook.name}</h1>
      )}
      {/* 徽章有界(见 `.reader-badge-chip` 的 max-width),长组名会被省略,所以完整文本
          必须和身份说明一起进 tooltip——否则读者看不出这本库到底来自哪个组。 */}
      <span
        className="reader-badge-chip"
        title={`${badgeLabel}\n${badgeHint(granted, canManage)}`}
      >
        {badgeLabel}
      </span>
      {!granted && (
        <button
          className="sort-button reader-badge-action"
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
 * 笔记本卡片(主页列表)的操作菜单。
 *
 * - owner:编辑信息 + 删除笔记本;
 * - 组管理员 / 群组共享的只读成员(reader + `granted_via`):只有「由组管理员管理」
 *   说明,**没有编辑信息、没有删除、没有退出**。⚠ 组管理员的「编辑信息」刻意**不在
 *   这里**(P2-T2 评审 P2-4 修正):卡片菜单的「编辑信息」打开的是那个 fused 编辑器
 *   (`openNotebookEditor` 连挂载配置一起拉,是 owner-only 的 notebook:configure),
 *   给组管理员画出来点了会在 listMountable 上 404。组管理员改名走**工作区顶栏**的
 *   行内输入框(PATCH-only,见 ReaderNotebookBadge.rename)——那条不碰挂载配置;
 * - 只读共享(分享链接、非群组):退出共享。
 */
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
