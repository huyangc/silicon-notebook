export type ReleaseNoteLevel = "feature" | "change" | "fix" | "internal";

/** 级别的界面词:弹窗与更新记录页共用。 */
export const RELEASE_NOTE_LEVEL_LABELS: Record<ReleaseNoteLevel, string> = {
  feature: "新功能",
  change: "变化",
  fix: "修复",
  internal: "后台改进",
};
