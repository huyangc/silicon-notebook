import { requestBlob, requestJson } from "./api-client.ts";
import type { ReportDetailT, ReportSummaryT } from "./report-view.tsx";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export const createReport = (nb: string, question: string, depth: number) =>
  requestJson<{ report_id: string }>(`/notebooks/${nb}/reports`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ question, depth }),
  });

export const confirmReportIntent = (
  nb: string,
  id: string,
  payload: { resolved_question: string; answers: { id: string; answer: string }[] },
) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}/intent`, {
    ...options,
    method: "POST",
    body: JSON.stringify(payload),
  });

export const listReports = (nb: string) =>
  requestJson<ReportSummaryT[]>(`/notebooks/${nb}/reports`, options);

export const getReport = (nb: string, id: string) =>
  requestJson<ReportDetailT>(`/notebooks/${nb}/reports/${id}`, options);

export const cancelReport = (nb: string, id: string) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}/cancel`, {
    ...options,
    method: "POST",
  });

export const deleteReport = (nb: string, id: string) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}`, {
    ...options,
    method: "DELETE",
  });

export const updateReportOutline = (nb: string, id: string, sections: unknown[]) =>
  requestJson<{ status: string; sections: number }>(
    `/notebooks/${nb}/reports/${id}/outline`,
    { ...options, method: "PATCH", body: JSON.stringify({ sections }) },
  );

export const generateReport = (nb: string, id: string, depth?: number) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}/generate`, {
    ...options,
    method: "POST",
    body: JSON.stringify(depth != null ? { depth } : {}),
  });

export async function downloadReportsZip(
  nb: string,
  reportIds: string[],
): Promise<void> {
  const blob = await requestBlob(`/notebooks/${nb}/reports/export`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ report_ids: reportIds }),
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "reports.zip";
  anchor.click();
  URL.revokeObjectURL(url);
}
