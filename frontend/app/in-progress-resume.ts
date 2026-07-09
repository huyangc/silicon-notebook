// 进行中动作的「刷新重连」判定 —— 纯逻辑，单测于 in-progress-resume.test.mjs。
// 这些后台动作离开页面后仍在后端跑；下列谓词让前端在 mount/切库时读后端真相、
// 决定是否把进度接回并续上既有 6s 轮询，而不是无条件复位丢掉进行中状态。

export type MergeReviewJobLike = { status: string };
export type ScaleIndexStatusLike = { building?: boolean; state?: string };
export type NotebookKgBuildLike = { kg_building?: boolean };

/** 「全部预审」是否应在 mount/切库时接回轮询（后端 merge-review job 仍 running）。 */
export function shouldResumeReviewAll(job: MergeReviewJobLike | null | undefined): boolean {
  return !!job && job.status === "running";
}

/** 检索索引：后端报 building，或 state==="queued"（已排队、空闲时建、尚未进入 building）
 *  即应接回轮询——否则「已排队」徽章在刷新后卡死，直到用户手动操作才会发现其实已建完。 */
export function shouldResumeScaleIndex(status: ScaleIndexStatusLike | null | undefined): boolean {
  return !!status && (status.building === true || status.state === "queued");
}

/** KG 构建/重抽：后端内存标志 kg_building 为真即应接回轮询。 */
export function shouldResumeKgBuild(nb: NotebookKgBuildLike | null | undefined): boolean {
  return !!nb && nb.kg_building === true;
}

/** KG 轮询的停止条件：改看 kg_building（而非 kg_ready）——
 *  重抽已建库时 kg_ready 恒为真，用它会一上来就误判「完成」。
 *  空值（null/undefined）视为「未在构建」→ true。 */
export function kgBuildFinished(nb: NotebookKgBuildLike | null | undefined): boolean {
  return !nb || !nb.kg_building;
}
