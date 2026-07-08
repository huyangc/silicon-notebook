export const TODAY_VALUE = ""; // 空 = 让后端按其本地时区兜底为「今天」，避开浏览器/服务器时区错位
export function dayLabel(v: string): string {
  if (v === TODAY_VALUE) return "今天";
  if (v === "legacy") return "历史(未分天)";
  return v;
}
