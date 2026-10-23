/**
 * report-scope-hint.tsx
 *
 * 深度报告输入框锁住（检索范围为空）时，输入框旁那一句。输入框的 placeholder 只能是
 * 纯文字，所以出路写在这里，说到的动作就是可点的字：
 *
 *  - 高级模式下范围被清空、但其实有东西可选 →「全选」（与来源栏「全选」同一个动作：本库
 *    来源与参考库一起全选）；
 *  - 没有任何可检索的内容 →「添加来源」/「挂载参考库」，当前用户做不了的那一半不说。
 *
 * 只是呈现：命令经回调交回宿主。点「全选」后这句会消失，焦点由 ReportsPanel 交给输入框。
 */
"use client";


export type ReportScopeHintProps = {
  /** 高级模式：范围是用户自己勾的，可以一键全选回来。 */
  advanced: boolean;
  /** 有没有东西可全选（本库有来源，或挂了参考库）。 */
  canSelectAll: boolean;
  canAddSources: boolean;
  canMountBases: boolean;
  /** 镜像库：内容随源环境同步，不是这里的谁添加的。 */
  mirrored?: boolean;
  onSelectAll: () => void;
  onAddSources: () => void;
  onMountBases: () => void;
};


export function ReportScopeHint({
  advanced,
  canSelectAll,
  canAddSources,
  canMountBases,
  mirrored = false,
  onSelectAll,
  onAddSources,
  onMountBases,
}: ReportScopeHintProps) {
  if (advanced && canSelectAll) {
    return (
      <>
        当前检索范围为空，
        <button type="button" className="link-button inline-action" onClick={onSelectAll}>
          全选
        </button>
        来源与参考库，或在来源栏勾选要用的来源，再开始生成报告
      </>
    );
  }
  const addSources = canAddSources && (
    <button type="button" className="link-button inline-action" onClick={onAddSources}>
      添加来源
    </button>
  );
  const mountBases = canMountBases && (
    <button type="button" className="link-button inline-action" onClick={onMountBases}>
      挂载参考库
    </button>
  );
  if (!addSources && !mountBases) {
    return mirrored
      ? <>这本笔记本还没有可检索的内容，内容随源环境同步过来之后就能生成报告</>
      : <>这本笔记本还没有可检索的内容，由库主或可编辑的成员添加之后就能生成报告</>;
  }
  return (
    <>
      请先
      {addSources}
      {addSources && mountBases && "或"}
      {mountBases}
      ，再开始生成报告
    </>
  );
}
