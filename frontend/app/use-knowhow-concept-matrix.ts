"use client";

/**
 * use-knowhow-concept-matrix.ts
 *
 * Knowhow anchor 表「概念矩阵抽屉」的领域 owner：当前打开的概念值与其分组、引用
 * 跳转命中的高亮分支、删除概念的二次确认与进行中标记、抽屉内错误、删除分支的
 * 在途行 id，以及「删除整个概念」「删除单个分支」两个命令和「切概念/关抽屉时复位
 * 确认态」的 effect。从 knowhow-panel.tsx 原样搬出（S4-d）：state、useMemo、
 * effect 与两个命令的代码一字未改，只把面板持有的三样东西改为经 `effects` 传入
 * （loadDetail、loadTables、写表级 actionError 的原生 setter），调用顺序不变。
 *
 * 留在面板的：「+ 分支」addBranch（与添加行/+概念共用 addingRow 重入锁），以及
 * openTable / backToList / notebook 复位 / 引用跳转里对这几个 state 的写入。
 * 它们都通过本 hook 以原名暴露的 useState 原生 setter（setOpenConceptValue、
 * setHighlightRowId、setConfirmDeleteConcept、setConceptDrawerError）完成，所以
 * 网格的 `onOpenConcept={knowhowConceptMatrix.setOpenConceptValue}` 仍是原生
 * setter，身份不变。
 *
 * 调用位置（承重）：面板在 `return` 之前、原 openConceptGroup / 复位 effect 的位置
 * 调用本 hook，让这个复位 effect 仍是面板里最后一个执行的 effect。前面引用
 * `knowhowConceptMatrix` 的地方都在事件回调或 effect 体里，只在渲染之后执行，不会
 * 撞上暂时性死区。knowhow-panel-boundary 守卫钉住这个顺序。
 */

import { useEffect, useMemo, useState } from "react";

import { deleteKnowhowRow, type KnowhowTableDetail } from "./knowhow-model.ts";
import { groupRowsByAnchor } from "./knowhow-grouping-logic.ts";

/** 面板持有、本 hook 的两个删除命令要用到的三样东西。 */
export type KnowhowConceptMatrixEffects = {
  /** 带请求号守卫的表详情重拉（面板的 loadDetail）。 */
  loadDetail: (tableId: string) => Promise<KnowhowTableDetail | null>;
  /** 表清单重拉（use-knowhow-table-list.ts 的 loadTables）。 */
  loadTables: () => void;
  /** 面板的 actionError 原生 setter：关闭抽屉后网格横幅照常可见。 */
  reportActionError: (message: string | null) => void;
};

export function useKnowhowConceptMatrix({
  notebookId,
  selectedTableId,
  detail,
  effects,
}: {
  notebookId: string;
  selectedTableId: string | null;
  detail: KnowhowTableDetail | null;
  effects: KnowhowConceptMatrixEffects;
}) {
  // Task 8（点概念打开矩阵抽屉，规格 §4.3）：当前打开的概念值——null=未打开；
  // 非空时对应 anchor 列某个分组的 anchorValue，驱动下方渲染
  // KnowhowMatrixDrawer（与 cellModal/codeModal 平级的另一个顶层 modal 状态，
  // 堆叠在网格之上）。
  const [openConceptValue, setOpenConceptValue] = useState<string | null>(null);

  // Task 11（引用命中→跳概念矩阵抽屉+高亮，规格 §4.5）：ask 引用跳转命中的
  // 分支（行 id）——null=无高亮；非空时传给 KnowhowMatrixDrawer 的
  // highlightRowId prop，抽屉据此高亮该分支列（表头 + 该行全部格子）。只在
  // 「有 anchor 的表被引用跳转命中」时非空，其余打开方式（点概念格/加分支/
  // 加概念）不设置，恒为 null——与 openConceptValue 同生命周期但不完全同步：
  // openConceptValue 变化时不逐一清空这个值（那样会清掉刚设好的高亮，见下方
  // 引用跳转 effect 的注释），只在关闭抽屉/切表/返回列表等「离开当前概念」
  // 的位置与 openConceptValue 并列清零。
  const [highlightRowId, setHighlightRowId] = useState<string | null>(null);

  // Task 10（删概念）：矩阵抽屉「删除整个概念」二次确认态——镜像表级
  // confirmDelete/deleting 那一对状态（KnowhowTableGrid 消费的同名 prop），
  // 只是作用对象从"这张表"换成"当前打开的概念组"。openConceptValue 变化
  // （切概念/关抽屉）时下面紧跟的 effect 统一重置，不必在每个改
  // openConceptValue 的地方各自补一行重置代码。
  const [confirmDeleteConcept, setConfirmDeleteConcept] = useState(false);
  const [deletingConcept, setDeletingConcept] = useState(false);
  // 复审 Important 修复：矩阵抽屉内操作（加分支/删概念）失败的错误——与
  // actionError 分开维护，因为 actionError 只在 KnowhowTableGrid 渲染成横幅，
  // 被抽屉的 .kh-modal-overlay（z-index 65）盖住，抽屉开着时用户看不到。
  // addBranch/deleteConcept 失败时仍然并行写 actionError（不改变既有行为：
  // 关闭抽屉后主网格横幅照常出现），同时把同一条文案写进这里传给
  // KnowhowMatrixDrawer 就地渲染。用独立 state 而不是直接透传 actionError，
  // 是为了避免一个无关操作（改名/重新投影/下载模板等）留下的陈旧 actionError
  // 在用户打开一个全新的矩阵抽屉时被误显示——openConceptValue 变化（切概念/
  // 关抽屉）时随 confirmDeleteConcept/deletingConcept 一并重置，保证每次
  // 打开都是干净状态。
  const [conceptDrawerError, setConceptDrawerError] = useState<string | null>(null);

  // Follow-up B（删分支，复审 Important 修复）：单个分支删除请求进行中的
  // 目标 rowId——null=当前没有删除在途。镜像 addingRow 的既有重入防护用法
  // （deleteBranch 顶部据此短路重入调用），但用 rowId 而非布尔值：删除按
  // 具体某一行发起，用 rowId 既能在 deleteBranch 内部短路重入（避免并发的
  // 第二次调用读到同一份 stale openConceptGroup.rows.length、两次都误判"非
  // 最后一支"、最终留下一个悬挂指向已删概念的 openConceptValue——详见
  // deleteBranch 函数体注释），也原样传给 KnowhowMatrixDrawer 驱动"这一列"
  // 表头确认按钮的 disabled + "删除中…"文案，不影响其余分支列的入口。
  const [deletingBranchRowId, setDeletingBranchRowId] = useState<string | null>(null);

  // Task 10（删概念）：矩阵抽屉「删除整个概念」——概念组内的物理行全部归属
  // 这一个概念，删概念即删组内每一行，Promise.all 并发触发（前端并发触发、
  // 非后端单事务的取舍——不同于 handleCellSave 的合并格批量写：那一路
  // followup A 已经换成后端单事务的 batchPatchKnowhowCells，这里的批量删除
  // 目前仍是各行独立 DELETE，不在本次改动范围内）：任一路失败就不当整批
  // 成功处理，不关抽屉、不刷新——失败的那几行在服务端可能已经被删除，下一次
  // loadDetail 会照实反映服务端的（可能部分删除后的）真实状态，这次不用
  // 本地乐观更新去掩盖它。
  async function deleteConcept() {
    if (!selectedTableId || !openConceptGroup) return;
    setDeletingConcept(true);
    effects.reportActionError(null);
    setConceptDrawerError(null);
    try {
      await Promise.all(openConceptGroup.rows.map((row) => deleteKnowhowRow(notebookId, selectedTableId, row.id)));
      setDeletingConcept(false);
      setOpenConceptValue(null);
      setHighlightRowId(null); // Task 11：概念连同其全部分支已删除，没有可高亮的行了
      effects.loadDetail(selectedTableId);
      effects.loadTables();
    } catch {
      // 复审修复：抽屉删除失败时抽屉仍开着（不像成功路径会关闭），必须让
      // 错误在抽屉内可见——conceptDrawerError 与 actionError 同文案双写，
      // 理由同 addBranch 的 catch 分支注释。
      effects.reportActionError("删除失败，请重试");
      setConceptDrawerError("删除失败，请重试");
      setConfirmDeleteConcept(false);
      setDeletingConcept(false);
    }
  }

  // Follow-up B（删分支，spec §4.4）：矩阵抽屉表头「删除该分支」——删除概念组
  // 里单独一个物理行（一个分支），与上面 deleteConcept「删整个概念（组内全部
  // 行）」不同粒度。二次确认态本身在 KnowhowMatrixDrawer 组件内部持有
  // （confirmDeleteBranchRowId 本地 state，见该组件），这里只负责确认后的真正
  // 删除请求——错误处理与 deleteConcept 同一套双写渠道（actionError 供关闭
  // 抽屉后主网格横幅可见 + conceptDrawerError 供抽屉开着时就地显示）。
  //
  // 删最后一个分支的边界：openConceptGroup 是调用这一刻（删除请求发出前）捕获
  // 的旧引用，await 期间不会因为其它渲染变化——在发起删除前先判定
  // `rows.length <= 1`（= 正在删的就是这个概念组仅剩的最后一个分支），删除
  // 成功后据此关闭抽屉（setOpenConceptValue(null)），否则该组变空后
  // KnowhowMatrixDrawer 会尝试渲染一个 branchRowIds 为空的矩阵。非最后一个
  // 分支时抽屉照常开着，loadDetail 拿到少一行的新 detail 后 openConceptGroup
  // 会自动重算成少一个分支的矩阵，不需要额外处理。
  //
  // 复审 Important 修复：上面这段"isLastBranch 判定"的前提是 openConceptGroup
  // 这份旧引用在函数调用的一瞬间是权威的——但若同一个概念组里还有另一个分支
  // 在这次请求 await 落地（+ loadDetail 刷新）之前也被确认删除，第二次调用会
  // 读到同一份 stale rows.length，两次都判定 isLastBranch=false；两个分支都
  // 真删掉后概念组剩 0 行，却没有一次调用触发 setOpenConceptValue(null)——
  // openConceptValue 悬挂指向一个已不存在的概念（openConceptGroup 的 useMemo
  // 找不到匹配组会变回 null，抽屉不再渲染，不会崩，但状态本身是脏的：万一之
  // 后有同锚点文本的行被加回/导入，抽屉会诡异地自动弹出）。同一个分支被快速
  // 二次点击确认同理会打两次 DELETE，第二次因行已不存在而 400，误报"删除
  // 失败"。deletingBranchRowId（镜像 addingRow 的既有重入防护）在函数最顶部
  // 短路重入调用，保证同一时刻至多一路删除请求在途，堵住这两条窗口；配合
  // KnowhowMatrixDrawer 侧不再提前收起 confirmDeleteBranchRowId（见该组件
  // handleConfirmDeleteBranch 注释），在制造出第二次点击之前先让按钮转
  // disabled + "删除中…"。
  async function deleteBranch(rowId: string) {
    if (deletingBranchRowId) return;
    if (!selectedTableId || !openConceptGroup) return;
    const isLastBranch = openConceptGroup.rows.length <= 1;
    setDeletingBranchRowId(rowId);
    effects.reportActionError(null);
    setConceptDrawerError(null);
    try {
      await deleteKnowhowRow(notebookId, selectedTableId, rowId);
      if (isLastBranch) {
        setOpenConceptValue(null);
        setHighlightRowId(null); // 同 deleteConcept：没有可高亮的行了
      }
      effects.loadDetail(selectedTableId);
      effects.loadTables();
    } catch {
      effects.reportActionError("删除失败，请重试");
      setConceptDrawerError("删除失败，请重试");
    } finally {
      setDeletingBranchRowId(null);
    }
  }

  // Task 8：openConceptValue 对应的 anchor 分组——按 detail.rows 全量分组
  // （不受网格搜索 query 过滤影响，抽屉展示该概念完整的分支集合，不因为
  // 搜索命中了其中一支就漏显示其余分支）；表已刷新/该概念值不再存在时
  // 找不到、为 null，抽屉不渲染（见下方 KnowhowMatrixDrawer 挂载条件）。
  const openConceptGroup = useMemo(() => {
    if (!detail?.anchorColumnId || openConceptValue === null) return null;
    const groups = groupRowsByAnchor(detail.rows, detail.anchorColumnId);
    return groups.find((g) => g.anchorValue === openConceptValue) ?? null;
  }, [detail, openConceptValue]);

  // Task 10（删概念）：openConceptValue 一变（切到另一个概念、或关闭抽屉）就
  // 收起二次确认态——不然上一个概念点了「删除」没确认就关抽屉，下次（同一个
  // 或另一个）概念一开就已经是确认态，等于半自动帮用户点了一半删除流程。
  useEffect(() => {
    setConfirmDeleteConcept(false);
    setDeletingConcept(false);
    setConceptDrawerError(null); // 复审修复：同上，切概念/关抽屉不留旧错误
  }, [openConceptValue]);

  return {
    view: {
      openConceptGroup,
      highlightRowId,
      confirmDeleteConcept,
      deletingConcept,
      conceptDrawerError,
      deletingBranchRowId,
    },
    setOpenConceptValue,
    setHighlightRowId,
    setConfirmDeleteConcept,
    setConceptDrawerError,
    deleteConcept,
    deleteBranch,
  };
}
