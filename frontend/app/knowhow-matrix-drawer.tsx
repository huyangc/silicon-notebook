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

import { type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
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
}) {
  const matrix = buildConceptMatrix(group, columns, anchorColumnId);

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
                    分支 {i + 1}
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
