// 「这个导入的新笔记本已经发布(能打开)了吗」—— 纯判据,不依赖网络与 React。
//
// 导入作业(`notebook-transfer-api.ts`)与待确认中心的条目(`pending-actions.ts`)都按它判,
// 所以两处对同一个作业永远说同一句话:发布前占位行起就有 `notebook_id`,只有它不够,还要看
// 阶段或失败码(后端 `jobs.py::_PUBLISHED_IMPORT_PHASES`、`import_followup_*`)。

/** 导入在这几个阶段时新笔记本已经发布:它存在、归导入者,能打开。 */
export const PUBLISHED_IMPORT_PHASES: ReadonlySet<string> = new Set(["project", "reembed", "index", "done"]);

/** 这两种失败发生在发布之后:笔记本已经在了,只是导入后的后台处理没完成。 */
export const PUBLISHED_FAILURE_CODES: ReadonlySet<string> = new Set([
  "import_followup_failed", "import_followup_interrupted",
]);

/** 这一个导入的新笔记本能打开了(作业或铃铛条目的四个字段)。 */
export function importPublished(job: {
  kind?: string;
  notebook_id?: string;
  phase?: string;
  failure_code?: string;
}): boolean {
  if (job.kind !== "import" || !job.notebook_id) return false;
  return PUBLISHED_IMPORT_PHASES.has(job.phase ?? "") || PUBLISHED_FAILURE_CODES.has(job.failure_code ?? "");
}
