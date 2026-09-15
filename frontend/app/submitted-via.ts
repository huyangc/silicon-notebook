// 提问/深度报告的提交入口，与后端 `submitted_via` 字段逐字对应
// （backend/app/models/ask.py::SubmittedVia）。空串表示未记录：该字段上线前的历史行，
// 或不经网页、MCP 这两个入口的内部调用。

export type SubmittedVia = "web" | "mcp";

const LABELS: Record<SubmittedVia, string> = { web: "网页", mcp: "MCP" };

/** 已记录的入口返回界面词；未记录或认不出的值返回空串，绝不把原值上屏。 */
export function recordedSubmittedViaLabel(value: string | undefined): string {
  return value === "web" || value === "mcp" ? LABELS[value] : "";
}

export function submittedViaLabel(value: string | undefined): string {
  return recordedSubmittedViaLabel(value) || "未记录";
}
