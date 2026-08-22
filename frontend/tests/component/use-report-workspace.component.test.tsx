import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ReportDetailT, ReportSummaryT } from "../../app/report-model";

const api = vi.hoisted(() => ({
  cancelReport: vi.fn(),
  confirmReportIntent: vi.fn(),
  createReport: vi.fn(),
  deleteReport: vi.fn(),
  downloadReportsZip: vi.fn(),
  generateReport: vi.fn(),
  getReport: vi.fn(),
  getReportShare: vi.fn(),
  listReports: vi.fn(),
  shareReport: vi.fn(),
  unshareReport: vi.fn(),
  updateReportOutline: vi.fn(),
}));

vi.mock("../../app/report-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/report-api.ts")>()),
  ...api,
}));

import { useReportWorkspace } from "../../app/use-report-workspace";

type HookValue = ReturnType<typeof useReportWorkspace>;
type HookOptions = Parameters<typeof useReportWorkspace>[0];

const defaultPolicy: HookOptions["policy"] = {
  advanced: true,
  canManageReports: true,
  creationDisabled: false,
  sourceScope: { mode: "include", source_ids: ["source-a"] },
  baseScope: { mode: "include", notebook_ids: ["base-a"] },
};

const effects: HookOptions["effects"] = {
  notify: vi.fn(),
  downloadMarkdown: vi.fn(),
  announceShareLink: vi.fn(),
};

let value: HookValue | null = null;

function Harness({
  actorId = "user-a",
  notebookId = "notebook-a",
  active = true,
  policy = defaultPolicy,
}: {
  actorId?: string | null;
  notebookId?: string | null;
  active?: boolean;
  policy?: HookOptions["policy"];
}) {
  value = useReportWorkspace({ actorId, notebookId, active, policy, effects });
  return <div>{value.active?.id ?? "list"}:{value.reports?.map((row) => row.id).join(",") ?? "loading"}</div>;
}

function summary(id: string, status = "done"): ReportSummaryT {
  return {
    id,
    question: id,
    status,
    progress: "",
    section_count: 1,
    created_at: "2026-08-22T00:00:00Z",
    created_by: "user-a",
  };
}

function detail(id: string, status = "done"): ReportDetailT {
  return {
    ...summary(id, status),
    outline: [{ title: "one", scope: "scope", sub_queries: ["query"] }],
    sections: [],
    section_status: [],
    gaps: [],
    content_md: status === "done" ? "body" : "",
    references: [],
    understanding: {},
    error: "",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  value = null;
  vi.clearAllMocks();
  api.listReports.mockResolvedValue([]);
  api.getReport.mockResolvedValue(detail("report-a"));
  api.createReport.mockResolvedValue({ report_id: "report-new" });
  api.confirmReportIntent.mockResolvedValue({ status: "planning" });
  api.updateReportOutline.mockResolvedValue({ status: "ok", sections: 1 });
  api.generateReport.mockResolvedValue({ status: "generating" });
  api.cancelReport.mockResolvedValue({ status: "cancelled" });
  api.deleteReport.mockResolvedValue({ status: "deleted" });
  api.shareReport.mockResolvedValue({ share_token: "token" });
  api.getReportShare.mockResolvedValue({ share_token: "token" });
  api.unshareReport.mockResolvedValue(undefined);
  api.downloadReportsZip.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

test("hidden report tab performs zero report I/O and entering performs one list read", async () => {
  const { rerender } = render(<Harness active={false} />);
  expect(api.listReports).not.toHaveBeenCalled();

  rerender(<Harness active />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledTimes(1));
  expect(api.listReports).toHaveBeenCalledWith("notebook-a");
});

test("a notebook transition suspends report reads and a failed transition resumes the prior view", async () => {
  render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledTimes(1));

  let transition!: ReturnType<HookValue["beginNotebookTransition"]>;
  act(() => { transition = value!.beginNotebookTransition(); });
  await act(async () => Promise.resolve());
  expect(api.listReports).toHaveBeenCalledTimes(1);
  expect(value!.reports).toBeNull();

  act(() => value!.finishNotebookTransition(transition, false));
  await waitFor(() => expect(api.listReports).toHaveBeenCalledTimes(2));
  expect(api.listReports).toHaveBeenLastCalledWith("notebook-a");
});

test("a successful transition stays suspended until finish then loads only the new notebook", async () => {
  const { rerender } = render(<Harness active={false} />);
  let transition!: ReturnType<HookValue["beginNotebookTransition"]>;
  act(() => { transition = value!.beginNotebookTransition(); });
  rerender(<Harness notebookId="notebook-b" active />);
  await act(async () => Promise.resolve());
  expect(api.listReports).not.toHaveBeenCalled();

  act(() => value!.finishNotebookTransition(transition, true));
  await waitFor(() => expect(api.listReports).toHaveBeenCalledTimes(1));
  expect(api.listReports).toHaveBeenCalledWith("notebook-b");
});

test("focus waits for the single entry list and then reads exactly one detail", async () => {
  const pendingList = deferred<ReportSummaryT[]>();
  api.listReports.mockReturnValueOnce(pendingList.promise);
  render(<Harness />);

  act(() => value!.focusReport("report-focus"));
  expect(api.getReport).not.toHaveBeenCalled();
  await act(async () => pendingList.resolve([]));
  await waitFor(() => expect(api.getReport).toHaveBeenCalledTimes(1));
  expect(api.getReport).toHaveBeenCalledWith("notebook-a", "report-focus");
});

test("actor replacement rejects a deferred detail commit", async () => {
  const pendingDetail = deferred<ReportDetailT>();
  api.getReport.mockReturnValueOnce(pendingDetail.promise);
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());

  let opening!: Promise<void>;
  act(() => { opening = value!.openReport("report-old"); });
  rerender(<Harness actorId="user-b" />);
  await act(async () => pendingDetail.resolve(detail("report-old")));
  await opening;
  expect(value!.active).toBeNull();
});

test("auto creation freezes current scopes and forces the default depth", async () => {
  const policy: HookOptions["policy"] = {
    ...defaultPolicy,
    advanced: false,
    sourceScope: { mode: "include", source_ids: ["source-frozen"] },
    baseScope: { mode: "include", notebook_ids: ["base-frozen"] },
  };
  render(<Harness policy={policy} />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());
  act(() => value!.updateQuestion("  compare  "));
  await act(async () => value!.submitCreate());

  expect(api.createReport).toHaveBeenCalledWith(
    "notebook-a",
    "compare",
    2,
    { mode: "include", source_ids: ["source-frozen"] },
    { mode: "include", notebook_ids: ["base-frozen"] },
    true,
  );
});

test("live report management permission is rechecked by every write command", async () => {
  const readonlyPolicy: HookOptions["policy"] = { ...defaultPolicy, canManageReports: false };
  api.listReports.mockResolvedValue([summary("report-a", "failed")]);
  api.getReport.mockResolvedValue(detail("report-a", "failed"));
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());
  await act(async () => value!.openReport("report-a"));
  rerender(<Harness policy={readonlyPolicy} />);

  act(() => value!.updateQuestion("new report"));
  await act(async () => {
    await value!.submitCreate();
    await value!.requestCancel();
    await value!.requestRetry();
    await value!.confirmIntent({ resolved_question: "resolved", answers: [] });
    await value!.confirmOutline({ sections: [{ title: "one", scope: "scope", sub_queries: ["query"] }] });
    await value!.toggleShare();
    await value!.deleteById("report-a");
  });

  expect(api.createReport).not.toHaveBeenCalled();
  expect(api.cancelReport).not.toHaveBeenCalled();
  expect(api.generateReport).not.toHaveBeenCalled();
  expect(api.confirmReportIntent).not.toHaveBeenCalled();
  expect(api.updateReportOutline).not.toHaveBeenCalled();
  expect(api.shareReport).not.toHaveBeenCalled();
  expect(api.deleteReport).not.toHaveBeenCalled();
});

test("successful delete tombstone converges after A to B to A", async () => {
  const pendingDelete = deferred<{ status: string }>();
  api.deleteReport.mockReturnValueOnce(pendingDelete.promise);
  api.listReports.mockImplementation(async (notebook: string) => (
    notebook === "notebook-a" ? [summary("report-delete")] : [summary("report-b")]
  ));
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(value!.reports?.map((row) => row.id)).toEqual(["report-delete"]));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.deleteById("report-delete"); });
  rerender(<Harness notebookId="notebook-b" />);
  await waitFor(() => expect(value!.reports?.map((row) => row.id)).toEqual(["report-b"]));
  rerender(<Harness notebookId="notebook-a" />);
  await waitFor(() => expect(value!.reports?.map((row) => row.id)).toEqual(["report-delete"]));
  expect(value!.deletingId).toBe("report-delete");
  await act(async () => value!.deleteById("report-delete"));
  expect(api.deleteReport).toHaveBeenCalledTimes(1);
  await act(async () => pendingDelete.resolve({ status: "deleted" }));
  await deleting;
  expect(value!.reports).toEqual([]);
});

test("list and detail polling are mutually exclusive and stop on terminal state", async () => {
  vi.useFakeTimers();
  api.listReports.mockResolvedValue([summary("running", "generating")]);
  render(<Harness />);
  await act(async () => Promise.resolve());
  expect(api.listReports).toHaveBeenCalledTimes(1);

  api.listReports.mockResolvedValue([summary("done")]);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(api.listReports).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(18000); });
  expect(api.listReports).toHaveBeenCalledTimes(2);
  expect(api.getReport).not.toHaveBeenCalled();
});

test("a slow list poll stays single-flight and cannot overwrite a newer manual refresh", async () => {
  vi.useFakeTimers();
  const stalePoll = deferred<ReportSummaryT[]>();
  api.listReports
    .mockResolvedValueOnce([summary("running", "generating")])
    .mockReturnValueOnce(stalePoll.promise)
    .mockResolvedValueOnce([summary("fresh", "generating")])
    .mockResolvedValueOnce([summary("next", "generating")]);
  render(<Harness />);
  await act(async () => Promise.resolve());

  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(api.listReports).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
  expect(api.listReports).toHaveBeenCalledTimes(2);

  act(() => value!.backToList());
  await act(async () => Promise.resolve());
  expect(value!.reports?.map((row) => row.id)).toEqual(["fresh"]);
  await act(async () => stalePoll.resolve([summary("stale", "generating")]));
  expect(value!.reports?.map((row) => row.id)).toEqual(["fresh"]);

  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(api.listReports).toHaveBeenCalledTimes(4);
  expect(value!.reports?.map((row) => row.id)).toEqual(["next"]);
});

test("navigation detaches active jobs without calling cancel", async () => {
  api.listReports.mockResolvedValue([summary("running", "generating")]);
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());
  rerender(<Harness active={false} />);
  expect(api.cancelReport).not.toHaveBeenCalled();
  expect(value!.reports).toBeNull();
});

test("intent and outline commands preserve the existing request sequence", async () => {
  api.listReports.mockResolvedValue([summary("report-a", "intent_ready")]);
  api.getReport.mockResolvedValue(detail("report-a", "intent_ready"));
  render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());
  await act(async () => value!.openReport("report-a"));
  await act(async () => value!.confirmIntent({ resolved_question: "resolved", answers: [] }));
  expect(api.confirmReportIntent).toHaveBeenCalledTimes(1);

  api.getReport.mockResolvedValue(detail("report-a", "outline_ready"));
  await act(async () => value!.openReport("report-a"));
  await act(async () => value!.confirmOutline({
    sections: [{ title: "one", scope: "scope", sub_queries: ["query"] }],
  }));
  expect(api.updateReportOutline).toHaveBeenCalledTimes(1);
  expect(api.generateReport).toHaveBeenCalledTimes(1);
  expect(api.updateReportOutline.mock.invocationCallOrder[0])
    .toBeLessThan(api.generateReport.mock.invocationCallOrder[0]);
});

test("outline confirmation rechecks owner and live permission between PATCH and generate", async () => {
  const pendingPatch = deferred<{ status: string; sections: number }>();
  api.listReports.mockResolvedValue([summary("report-a", "outline_ready")]);
  api.getReport.mockResolvedValue(detail("report-a", "outline_ready"));
  api.updateReportOutline.mockReturnValueOnce(pendingPatch.promise);
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());
  await act(async () => value!.openReport("report-a"));

  let confirming!: Promise<void>;
  act(() => {
    confirming = value!.confirmOutline({
      sections: [{ title: "one", scope: "scope", sub_queries: ["query"] }],
    });
  });
  rerender(<Harness policy={{ ...defaultPolicy, canManageReports: false }} />);
  await act(async () => pendingPatch.resolve({ status: "ok", sections: 1 }));
  await confirming;
  expect(api.generateReport).not.toHaveBeenCalled();
});

test("a successful create keeps the existing list when its derived refresh fails", async () => {
  api.listReports
    .mockResolvedValueOnce([summary("existing")])
    .mockRejectedValueOnce(new Error("refresh failed"));
  render(<Harness />);
  await waitFor(() => expect(value!.reports?.map((row) => row.id)).toEqual(["existing"]));
  act(() => value!.updateQuestion("new report"));
  await act(async () => value!.submitCreate());

  expect(value!.reports?.map((row) => row.id)).toEqual(["existing"]);
  expect(effects.notify).toHaveBeenCalledTimes(1);
  expect(effects.notify).toHaveBeenCalledWith(expect.stringContaining("正在理解"));
});

test("cancel reports a sticky terminal response instead of claiming cancellation", async () => {
  api.listReports.mockResolvedValue([summary("report-a", "generating")]);
  api.getReport.mockResolvedValue(detail("report-a", "done"));
  api.cancelReport.mockResolvedValueOnce({ status: "done" });
  render(<Harness />);
  await waitFor(() => expect(api.listReports).toHaveBeenCalledOnce());
  await act(async () => value!.openReport("report-a"));
  await act(async () => value!.requestCancel());

  expect(effects.notify).toHaveBeenCalledWith("报告已进入终态，无需再取消");
  expect(effects.notify).not.toHaveBeenCalledWith(expect.stringContaining("已请求取消"));
});
