"use client";

// 多领域基准库 A3:内容审核弹窗里,批准/拒绝这一行按钮 + 缺目标库时的原地提示,
// 从 page.tsx 的候选卡片渲染里抽出来,便于单独做组件测试(page.tsx 整体不可
// 直接渲染测试)。只接最小的展示态 props,不拿 PromotionCandidate 整个对象——
// 调用方(page.tsx)已经算好了 hasTargetBase。
export function PromotionCandidateActions({
  hasTargetBase,
  busy,
  onApprove,
  onReject,
}: {
  hasTargetBase: boolean;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  return (
    <>
      {/* A3:多领域基准库合入前的存量候选没有 target_base_id,批准会被后端
          400 拒绝——置灰批准按钮并紧邻按钮行原地指路补救脚本,拒绝仍可点
          (拒绝不需要目标库,是合法操作)。 */}
      {!hasTargetBase && (
        <p className="tool-hint">
          未指定目标公共知识库，需先运行 scripts/backfill_promotion_targets.py 指定目标库
        </p>
      )}
      <div className="modal-actions">
        <button className="sort-button" disabled={busy} onClick={onReject}>
          拒绝
        </button>
        <button
          className="new-pill"
          disabled={busy || !hasTargetBase}
          title={hasTargetBase ? undefined : "未指定目标公共知识库，暂不能批准"}
          onClick={onApprove}
        >
          批准收录
        </button>
      </div>
    </>
  );
}
