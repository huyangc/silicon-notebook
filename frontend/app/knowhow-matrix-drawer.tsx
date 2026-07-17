/**
 * knowhow-matrix-drawer.tsx
 *
 * C 概念矩阵抽屉（Task 7，spec §4.3）：点主网格某个概念（G2 合并格）→ 打开
 * 「属性 × 分支」矩阵——属性为行、分支为列，是主网格 G2（属性为列、概念为
 * 行）的转置视图，最贴合用户原 Excel 观感。共享属性（如现象，全分支同值）
 * 跨分支列合并成一格（`MatrixAttrRow.sharedSpan`）；`highlightRowId` 标记
 * ask 引用命中的那个分支列（spec §4.5，Task 11 接线）。
 *
 * 矩阵构造本身（属性行/共享判定）是纯函数 `buildConceptMatrix`，已在 Phase 2
 * 的 knowhow-grouping-logic.ts 交付 + 单测覆盖；本文件只做渲染，不新增可测
 * 纯函数、不另立 *-logic.ts。
 *
 * 样式（`.kh-matrix*` 一整套）登记在 knowhow-panel.tsx 顶层的
 * `<style jsx global>`，不在本文件另开 `<style jsx>`——同 knowhow-cell-
 * editor.tsx / knowhow-code.tsx 的既有约定：styled-jsx 的 global 样式注入
 * 绑定「声明该 <style jsx global> 的组件是否渲染过」，KnowhowPanel 是本特性
 * 唯一保证任何时候都已挂载的样式容器，本组件不能自己声明一份（谁先渲染谁
 * 才有样式，会导致偶发裸 DOM）。外壳（overlay/card/header/breadcrumb/
 * icon-button）复用既有 kh-modal-*，与格子浮窗/代码浮层同一套视觉语言。
 *
 * 本 task（Task 7）只定义组件本身；「点概念打开」的接线（打开态状态管理 +
 * 从主网格触发）是 Task 8 的范围，见 KnowhowPanel 消费处（未来）。
 *
 * 已知遗留（有意不在本 task 做，留给接线抽屉打开态的后续 task）：本抽屉
 * 目前没有 Esc 关闭监听器——不是遗漏，是刻意等接线阶段一起做。同类
 * kh-modal-* 抽屉（KnowhowRowDrawer 等）都有 Esc 监听器，但那些监听器都
 * 带一个 cellModalOpen 短路条件（见 knowhow-panel.tsx KnowhowRowDrawer 的
 * cellModalOpen 参数注释）：本抽屉打开时点格子会在其上层再堆一个格子浮窗
 * （Task 8 起 onEditCell 接 openCellAuto），若现在就无条件加 Esc 监听器，
 * 按一次 Esc 会同时关掉格子浮窗和本抽屉（两个独立 window keydown 监听器
 * 都会响应同一次按键）——这正是 KnowhowRowDrawer 那段注释记录的、已经修过
 * 一次的 bug。本组件当前的 props 里还没有能表达"上层是否有浮窗"的信号，
 * 硬加等价的 cellModalOpen prop 会与 Task 8 计划里已经写好、不带该 prop 的
 * 调用点（`<KnowhowMatrixDrawer group=... onEditCell=... onClose=... />`）
 * 对不上；留给知道 KnowhowPanel 状态形状的后续 task 与 Esc 监听器一起加，
 * 比现在加一个可能被忽略、从而重新引入该 bug 的半成品更安全。
 */
"use client";

import { useEffect, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { buildConceptMatrix, type AnchorGroup } from "./knowhow-grouping-logic.ts";
import { KnowhowMarkdown } from "./knowhow-cell-editor.tsx";
import type { KnowhowColumn } from "./knowhow-model.ts";

export function KnowhowMatrixDrawer({
  group,
  columns,
  anchorColumnId,
  notebookId,
  apiBase,
  canEdit,
  highlightRowId,
  onEditCell,
  onClose,
  error,
  onAddBranch,
  addingBranch,
  confirmDeleteConcept,
  onRequestDeleteConcept,
  onCancelDeleteConcept,
  onConfirmDeleteConcept,
  deletingConcept,
  onDeleteBranch,
  deletingBranchRowId,
}: {
  group: AnchorGroup;
  columns: KnowhowColumn[];
  anchorColumnId: string;
  notebookId: string;
  apiBase: string;
  /** 只读成员没有编辑入口，但填了内容的格子仍可点开看全（见下方 isClickable）。 */
  canEdit: boolean;
  /** ask 引用跳转命中的分支（行 id）：对应表头列 + 该行所有格子高亮
   * （spec §4.5，Task 11 接线；未命中/非引用打开时为空）。 */
  highlightRowId?: string | null;
  onEditCell: (rowId: string, columnId: string) => void;
  onClose: () => void;
  /** 复审 Important 修复：加分支/删概念失败时的错误文案——这两个操作都从
   * 抽屉内部触发，但它们原本仅有的落地渠道（panel 级 actionError）只在主
   * 网格 KnowhowTableGrid 里渲染成横幅，被本抽屉的 .kh-modal-overlay
   * （z-index 65，见 knowhow-panel.tsx 挂载处注释）整个盖住——抽屉开着时
   * 用户看不到任何失败提示（加分支失败只是按钮静默复原，删概念失败后抽屉
   * 还留在原地却无法得知原因）。KnowhowPanel 额外把同一条文案通过这个 prop
   * 传进来，在 header 下方就地渲染（.kh-inline-error，与 KnowhowCellEditor
   * 的 saveError/uploadError 同一既有模式，不新开 CSS）。null/undefined/
   * 空字符串均不渲染。 */
  error?: string | null;
  /** Task 10（加分支）：底部「+ 分支」——在当前概念组下新建一个物理行（anchor
   * 列预填该概念值，其余列留空待填）。可选，只有 canEdit 时 KnowhowPanel 才
   * 传入，本组件据此决定是否渲染底部 footer。 */
  onAddBranch?: () => void;
  /** 加分支请求进行中——从按下按钮到新行落地这段时间禁用按钮，防止连点造出
   * 多个空分支。 */
  addingBranch?: boolean;
  /** Task 10（删概念）：header「删除整个概念」二次确认——镜像
   * KnowhowTableGrid 表级 confirmDelete/onRequestDelete/onCancelDelete/
   * onConfirmDelete/deleting 那一组既有 prop 形状（同一套内联确认模式），
   * 状态仍归 KnowhowPanel 持有，本组件只负责按这些 prop 渲染。 */
  confirmDeleteConcept?: boolean;
  onRequestDeleteConcept?: () => void;
  onCancelDeleteConcept?: () => void;
  onConfirmDeleteConcept?: () => void;
  deletingConcept?: boolean;
  /** Follow-up B（删分支，spec §4.4）：表头「删除该分支」——删掉这一个物理行
   * （一个分支），与上面 onRequestDeleteConcept 那组「删整个概念」不同粒度。
   * 二次确认态本身是本组件内部的 confirmDeleteBranchRowId 本地 state（见下方
   * 组件体），不像删概念那样把确认态提升到 KnowhowPanel——每个分支都是独立
   * 物理行，确认目标天然按 rowId 区分，组件自己就能管，不需要外部持有一份
   * "哪个分支在确认中"。真正的删除请求（deleteKnowhowRow + 刷新 + 最后一个
   * 分支时关抽屉）仍在 KnowhowPanel 完成，这个 prop 只是确认后的那一下触发。
   * 只读成员没有这个入口（渲染处 canEdit 门控）；未传入时表头只显示纯文本
   * 「分支 N」，不出现删除图标——KnowhowPanel 只在 canEdit 时才会传。 */
  onDeleteBranch?: (rowId: string) => void;
  /** 复审 Important 修复：单个分支删除请求进行中的 rowId（对应 KnowhowPanel
   * 的 deletingBranchRowId state）——null/undefined=当前没有删除在途。驱动
   * 下方确认按钮的 disabled + "删除中…" 文案，镜像 header「删除整个概念」
   * 确认按钮 disabled={deletingConcept} 那一套既有模式。 */
  deletingBranchRowId?: string | null;
}) {
  const matrix = buildConceptMatrix(group, columns, anchorColumnId);

  // Follow-up B（删分支）：哪个分支的表头正在二次确认删除——null=都没有；点
  // 某个分支的删除图标就把它设成该分支的 rowId，再点另一个分支的删除图标会
  // 直接把这个值换成新目标（单个 string，不是 Set，天然不会叠加出两个同时
  // 敞开的确认态，符合"点另一个分支的删除应切换确认目标，不叠加"）。抽屉整
  // 体关闭（KnowhowPanel 卸载本组件）时随组件一并清空，不需要额外 effect；
  // 但抽屉保持挂载、只是 group 换到另一个概念（ask 引用跳转 A→B 直接换
  // openConceptValue，不经过"关闭再打开"，见 knowhow-panel.tsx highlightRowId
  // 声明处注释）这一种情况下组件不会重新挂载，必须靠下面这个 effect 显式
  // 收起——否则上一个概念里点了「删除该分支」还没确认的按钮会带进下一个
  // 概念，显示成"确认删除"扣在一个此刻完全不相关的分支列表头上（复审 Important
  // 修复）。
  const [confirmDeleteBranchRowId, setConfirmDeleteBranchRowId] = useState<string | null>(null);

  useEffect(() => {
    setConfirmDeleteBranchRowId(null);
  }, [group.anchorValue]);

  // 格子是否可点：镜像 knowhow-panel.tsx 主网格 G2 的既有判据
  // `clickable = filled || canEdit`（Task 6）——填了内容的格子任何人都能点开
  // 看全（spec §4.3「点开看全」不分身份），canEdit 只额外解锁"点空格子去
  // 填写"。Task 8 计划把 onEditCell 接到 openCellAuto，其内部同样只在
  // "空内容 + 只读"时才短路不开——两处判据必须一致，否则只读成员会点不开
  // 已经填了内容的格子，看不到 spec 要求的全文。
  function isClickable(text: string): boolean {
    return canEdit || Boolean(text.trim());
  }

  // 可点格子的可聚焦/键盘属性。格子内容是 KnowhowMarkdown（可能渲染
  // <a>/<img>/<p> 等块级或交互元素），不能像 G2 网格
  // （knowhow-panel.tsx 的 .knowhow-cell-open）那样用 <button> 包一层——
  // button 内嵌块级/交互元素是非法 HTML。这里改为把可点的 <td> 本身做成
  // role="button" 的可聚焦控件：Enter/Space 触发与鼠标 onClick 相同的
  // onEditCell(rowId, columnId)，两处共用同一个 activate 闭包，不用各写一遍
  // 该格的 (rowId, columnId)。不可点的格子（isClickable 为假）返回空对象，
  // 不挂 role/tabIndex/事件——保持"不可点=纯展示、不出现在 tab 顺序里"。
  function clickableCellProps(clickable: boolean, rowId: string, columnId: string) {
    if (!clickable) return {};
    const activate = () => onEditCell(rowId, columnId);
    return {
      role: "button" as const,
      tabIndex: 0,
      onClick: activate,
      onKeyDown: (event: ReactKeyboardEvent<HTMLTableCellElement>) => {
        if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
          event.preventDefault();
          activate();
        }
      },
    };
  }

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) onClose();
  }

  // Follow-up B：表头确认按钮——把真正的删除请求交给 KnowhowPanel。
  //
  // 复审 Important 修复：改动前这里会先把 confirmDeleteBranchRowId 收起、
  // 表头随即掉回一个可点的删除图标，请求仍在 await 中——这段窗口期给了两条
  // 路子把同一次删除打两遍：①同一分支被快速二次点击（表头是可点的图标，第
  // 二次点击直接又发起一次 handleConfirmDeleteBranch）；②概念组还有另一个
  // 分支时，用户在这段窗口期确认了那一个分支的删除，两次删除都读到同一份
  // stale openConceptGroup.rows.length，都判定"不是最后一支"，最终两行都被
  // 删掉、组里 0 行剩却没有一次调用清空 openConceptValue（见
  // knowhow-panel.tsx deleteBranch 的 Important 修复注释）。现在不再主动收起
  // confirmDeleteBranchRowId：KnowhowPanel 新增的 deletingBranchRowId prop
  // （=== rowId 时）会让下面这个确认按钮转 disabled + "删除中…"，继续挂着这份
  // "确认中"视觉直到请求真正落地——删除成功后这一列随 matrix.branchRowIds
  // 重算直接从表头消失（确认态随之无处渲染，不用手动清）；删除失败则按钮
  // 恢复可点，用户能直接原地重试而不必先关掉确认框再重新点删除图标。
  function handleConfirmDeleteBranch(rowId: string) {
    onDeleteBranch?.(rowId);
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        className="kh-modal-card kh-matrix-card"
        role="dialog"
        aria-modal="true"
        aria-label={`概念 ${matrix.anchorValue}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header">
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              {/* 徽章文案用域中立的"概念"而非 spec 背景例子里的"违例概念"
                  ——anchor 列可以是任何域的分组概念（故障类型/组件/……），
                  硬编码时序违例领域的措辞会在其它领域的表里显示错误标签
                  （本文件其余部分、knowhow-model.ts 的 kind 词表都已经是
                  域中立设计，这里不应该开倒车）。复用既有
                  knowhow-status-badge--info（蓝底）配色，Step 2 无需为此
                  再新开一个 .concept-badge 类。 */}
              <span className="knowhow-status-badge knowhow-status-badge--info">概念</span>
              <span className="kh-modal-row-title">{matrix.anchorValue}</span>
              <span className="kh-modal-sep">·</span>
              <span>{matrix.branchRowIds.length} 个分支</span>
            </div>
            <div className="kh-modal-header-actions">
              {/* Task 10（删概念）：整组一次性删除——二次确认沿用
                  KnowhowTableGrid 表删除的既有内联确认样式
                  （.knowhow-confirm/-yes/-no），只换文案，不新开一套确认 UI。
                  只读成员看不到这个入口。 */}
              {canEdit && onRequestDeleteConcept && (
                confirmDeleteConcept ? (
                  <span className="knowhow-confirm">
                    <span>删除整个概念？其下 {matrix.branchRowIds.length} 个分支将一并删除</span>
                    <button
                      type="button"
                      className="knowhow-confirm-yes"
                      disabled={deletingConcept}
                      onClick={onConfirmDeleteConcept}
                    >
                      {deletingConcept ? "删除中…" : "确认删除"}
                    </button>
                    <button type="button" className="knowhow-confirm-no" onClick={onCancelDeleteConcept}>
                      取消
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="icon-button"
                    title="删除整个概念"
                    onClick={onRequestDeleteConcept}
                  >
                    <Trash2 size={18} />
                  </button>
                )
              )}
              <button type="button" className="icon-button" title="关闭" onClick={onClose}>
                <X size={18} />
              </button>
            </div>
          </div>
          {/* 复审修复：加分支/删概念失败提示——放在 header 内、body 之上，
              抽屉开着就能看到，不必等关闭抽屉才在主网格看到 actionError 横幅。
              复用 .kh-modal-header 自带的 padding/gap，不新开 CSS。 */}
          {error && <p className="kh-inline-error">{error}</p>}
        </header>
        <div className="kh-modal-body">
          <table className="kh-matrix">
            <thead>
              <tr>
                <th className="kh-matrix-corner"></th>
                {matrix.branchRowIds.map((rid, i) => (
                  <th key={rid} className={rid === highlightRowId ? "kh-matrix-branch--hi" : undefined}>
                    {/* Follow-up B（删分支，spec §4.4）：二次确认态下这一格换成
                        确认 UI——沿用 header「删除整个概念」那套既有内联确认
                        样式（.knowhow-confirm/-yes/-no），只是确认目标换成这一
                        个分支而非整组。canEdit 为假或调用方没接 onDeleteBranch
                        时只渲染纯文本「分支 N」，不出现删除入口。 */}
                    {canEdit && onDeleteBranch && confirmDeleteBranchRowId === rid ? (
                      <span className="knowhow-confirm">
                        <span>删除该分支？</span>
                        <button
                          type="button"
                          className="knowhow-confirm-yes"
                          disabled={deletingBranchRowId === rid}
                          onClick={() => handleConfirmDeleteBranch(rid)}
                        >
                          {deletingBranchRowId === rid ? "删除中…" : "确认删除"}
                        </button>
                        <button
                          type="button"
                          className="knowhow-confirm-no"
                          disabled={deletingBranchRowId === rid}
                          onClick={() => setConfirmDeleteBranchRowId(null)}
                        >
                          取消
                        </button>
                      </span>
                    ) : (
                      <span className="kh-matrix-branch-head">
                        <span>分支 {i + 1}</span>
                        {canEdit && onDeleteBranch && (
                          <button
                            type="button"
                            className="kh-matrix-branch-delete"
                            title="删除该分支"
                            disabled={!!deletingBranchRowId}
                            onClick={() => setConfirmDeleteBranchRowId(rid)}
                          >
                            <Trash2 size={14} />
                          </button>
                        )}
                      </span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.attrRows.map((attr) => (
                <tr key={attr.columnId}>
                  <td className="kh-matrix-rowhead">{attr.columnName}</td>
                  {attr.sharedSpan ? (
                    <td
                      className="kh-matrix-shared"
                      colSpan={matrix.branchRowIds.length}
                      {...clickableCellProps(isClickable(attr.values[0]), matrix.branchRowIds[0], attr.columnId)}
                    >
                      <KnowhowMarkdown md={attr.values[0]} notebookId={notebookId} apiBase={apiBase} />
                    </td>
                  ) : (
                    matrix.branchRowIds.map((rid, i) => {
                      const value = attr.values[i] ?? "";
                      return (
                        <td
                          key={rid}
                          className={rid === highlightRowId ? "kh-matrix-cell--hi" : undefined}
                          {...clickableCellProps(isClickable(value), rid, attr.columnId)}
                        >
                          <KnowhowMarkdown md={value} notebookId={notebookId} apiBase={apiBase} />
                        </td>
                      );
                    })
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {/* Task 10（加分支）：底部固定入口，随时给这个概念追加一个新分支
            （物理行，anchor 列预填当前概念值），不必先关抽屉回到主网格找
            「添加行」再手填概念名。只读成员不出现。 */}
        {canEdit && onAddBranch && (
          <footer className="kh-modal-footer">
            <div className="kh-footer-actions">
              <button type="button" onClick={onAddBranch} disabled={addingBranch}>
                <Plus size={14} /> {addingBranch ? "添加中…" : "分支"}
              </button>
            </div>
          </footer>
        )}
      </div>
    </div>
  );
}
