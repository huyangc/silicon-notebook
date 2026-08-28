import { vi } from "vitest";

import type { ReportWorkspace } from "../../app/use-report-workspace";

export function reportWorkspaceFixture(
  overrides: Partial<ReportWorkspace> = {},
): ReportWorkspace {
  return {
    reports: [],
    active: null,
    question: "",
    depthIndex: 1,
    creating: false,
    actionBusy: false,
    intentBusy: false,
    outlineBusy: false,
    shareBusy: false,
    shared: false,
    confirmDelete: false,
    confirmDeleteId: null,
    deletingId: null,
    downloadingId: null,
    selectMode: false,
    selectedIds: new Set<string>(),
    zipBusy: false,
    activateActor: vi.fn(),
    beginNotebookTransition: vi.fn(),
    finishNotebookTransition: vi.fn(),
    leaveWorkspace: vi.fn(),
    focusReport: vi.fn(),
    updateQuestion: vi.fn(),
    selectDepth: vi.fn(),
    submitCreate: vi.fn(),
    openReport: vi.fn(),
    backToList: vi.fn(),
    requestCancel: vi.fn(),
    requestRetry: vi.fn(),
    confirmIntent: vi.fn(),
    confirmOutline: vi.fn(),
    toggleShare: vi.fn(),
    // 默认 resolve(null)=「这一次没走到复制」。不能用裸 vi.fn():它返回 undefined,而
    // 调用方要 `.then(...)` 读复制结果,undefined 上取 then 当场抛。
    copyShareLink: vi.fn(async () => null),
    requestDelete: vi.fn(),
    deleteById: vi.fn(),
    chooseDeleteConfirmation: vi.fn(),
    downloadOne: vi.fn(),
    toggleSelectMode: vi.fn(),
    toggleSelected: vi.fn(),
    downloadSelected: vi.fn(),
    ...overrides,
  };
}
