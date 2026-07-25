export type ReportCoverage = {
  hits: number;
  base_hits: number;
  element_hits: number;
  source_hits: number;
};

export function parseReportSubQueries(value: string): string[] {
  return value.split("\n");
}

export function formatReportCoverage(coverage: ReportCoverage): string {
  const base = coverage.base_hits ? ` · 公共库 ${coverage.base_hits}` : "";
  return `原文 ${coverage.element_hits || 0} · 知识 ${coverage.hits || 0}${base}`;
}
