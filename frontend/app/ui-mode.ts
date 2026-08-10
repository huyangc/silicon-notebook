// 自动/高级界面模式的单一判据。所有隐藏/显示分支、请求侧 scope 强制都只读这里
// 派生的值，不散落第二份布尔——那会让「自动模式隐藏了勾选框，但请求还是发出了
// 上次高级模式下收窄的 scope」这类串台在多处独立地发生。

export type UiMode = "auto" | "advanced";

/**
 * 缺省(旧后端 /me 没有 ui_mode 字段)或非法值一律落回 "auto"——自动模式是产品
 * 默认，字段缺失不能被解释成「用户选了高级」。
 */
export function normalizeUiMode(v: unknown): UiMode {
  return v === "advanced" ? "advanced" : "auto";
}

export function isAdvanced(mode: UiMode): boolean {
  return mode === "advanced";
}

export function isAutoMode(mode: UiMode): boolean {
  return mode === "auto";
}

/**
 * 自动模式下、问答被硬锁时的 placeholder 文案：如果笔记本里存在解析未到终态
 * （非 extracted/failed）的来源，提示还在处理、说明会自动解锁；否则沿用调用方
 * 传入的既有兜底文案（例如「请先添加来源...」）。
 *
 * `pendingCount` 为 `null` 表示调用方手上的来源列表不可信（来源搜索框非空，或
 * 分页未加载全部来源）——这时不能报出一个可能算错的数字，也不能因为「按当前
 * 这页数出来是 0」就误判成没有来源在处理、退回「请先添加来源」这类更强的否定
 * 结论，因此显示不带数字的通用处理中文案。
 *
 * 纯函数，不读任何 React 状态——由 page.tsx 传入已经算好的处理中来源数（或不可信
 * 时传 null）。
 */
export function autoModeAskPlaceholder(pendingCount: number | null, fallback: string): string {
  if (pendingCount === null) return "文档处理中，完成后即可提问";
  return pendingCount > 0
    ? `${pendingCount} 篇文档处理中，完成后即可提问`
    : fallback;
}
