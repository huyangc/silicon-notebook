import { Square } from "lucide-react";

/**
 * 全站唯一的「停止」标志。
 *
 * 停止是图标键、不带字：它与发送键共用同一个位置，带字的停止键比发送键宽出一倍，
 * 小窗里会被挤成独占一行的黑块。状态（停止 / 停止中… / 取消问题理解）写在按钮的
 * `aria-label` 与 `title` 上，不写在按钮面上。
 *
 * 用法：停止键加 `STOP_CONTROL_CLASS`（浅红底 + 红描边，样式在 globals.css 的
 * `.stop-control`），内容放 `<StopGlyph />`。带字的动作行（如报告的「取消生成」）
 * 只取 `<StopGlyph />`，不取底色——那一行的外观归动作行自己。
 * 新增停止入口一律从这里取，`stop-control-guard` 守着 lucide 的 `Square` 不在别处出现。
 */
export const STOP_CONTROL_CLASS = "stop-control";

export function StopGlyph({ size = 16 }: { size?: number }) {
  return <Square className="stop-glyph" size={size} strokeWidth={2.5} aria-hidden="true" />;
}
