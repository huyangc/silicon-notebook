import { DEFAULT_SUPPORTED_SOURCE_EXTENSIONS } from "./system-api.ts";
import type { SourceSummary } from "./workspace-model.ts";

// 标题清理需要在系统配置返回前可用，因此使用与后端注册表配套的兼容默认值；上传
// 校验、accept 与可见格式列表则使用 /system/config 下发的权威注册表投影。
export const SUPPORTED_SOURCE_EXT_GROUP = DEFAULT_SUPPORTED_SOURCE_EXTENSIONS.join("|");

export function compactSourceTitle(source: SourceSummary): string {
  const rawTitle = (source.title || source.file_name || "Untitled source").trim();
  const withoutExtension = rawTitle.replace(new RegExp(`\\.(${SUPPORTED_SOURCE_EXT_GROUP})$`, "i"), "");
  return withoutExtension || rawTitle;
}
