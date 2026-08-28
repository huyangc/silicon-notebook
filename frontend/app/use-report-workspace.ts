"use client";

import { useEffect, useRef, useState } from "react";
import { logDiagnostic, toUserMessage } from "./errors";
import {
  cancelReport,
  confirmReportIntent,
  createReport,
  deleteReport,
  fetchReportsZip,
  generateReport,
  getReport,
  getReportShare,
  listReports,
  shareReport,
  unshareReport,
  updateReportOutline,
} from "./report-api";
import {
  isReportActive,
  REPORT_DEFAULT_DEPTH_INDEX,
  REPORT_DEPTHS,
  REPORT_POLL_INTERVAL_MS,
  type ReportDetailT,
  type ReportFrameT,
  type ReportOutlineSectionT,
  type ReportSummaryT,
} from "./report-model";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope";

type ReportOwner = {
  actorId: string;
  notebookId: string;
  generation: number;
  viewGeneration: number;
};

export type ReportWorkspaceTransition = { generation: number };

type ReportPolicy = {
  advanced: boolean;
  canManageReports: boolean;
  creationDisabled: boolean;
  sourceScope: SourceScopePayload;
  baseScope: BaseScopePayload;
};

type ReportEffects = {
  notify: (message: string) => void;
  downloadMarkdown: (report: ReportDetailT) => void;
  downloadArchive: (blob: Blob) => void;
  // 返回值是「链接有没有真的进剪贴板」。报告工具栏那颗「复制链接」要把结果画在自己
  // 身上,而复制发生在这一层之外(跨域 presentation effect),所以结果得原路带回来。
  announceShareLink: (token: string) => Promise<boolean>;
};

export type UseReportWorkspaceOptions = {
  actorId: string | null;
  notebookId: string | null;
  active: boolean;
  policy: ReportPolicy;
  effects: ReportEffects;
};

const copySourceScope = (scope: SourceScopePayload): SourceScopePayload => ({
  ...scope,
  source_ids: [...scope.source_ids],
});

const copyBaseScope = (scope: BaseScopePayload): BaseScopePayload => ({
  ...scope,
  notebook_ids: [...scope.notebook_ids],
});

const ownerKey = (owner: Pick<ReportOwner, "actorId" | "notebookId">): string =>
  `${owner.actorId}\0${owner.notebookId}`;

const sameOwner = (left: ReportOwner | null, right: ReportOwner): boolean => Boolean(
  left
  && left.actorId === right.actorId
  && left.notebookId === right.notebookId
  && left.generation === right.generation
  && left.viewGeneration === right.viewGeneration,
);

const sameIdentity = (
  left: Pick<ReportOwner, "actorId" | "notebookId"> | null,
  right: Pick<ReportOwner, "actorId" | "notebookId">,
): boolean => Boolean(
  left && left.actorId === right.actorId && left.notebookId === right.notebookId,
);

const optimisticGenerating = (
  report: ReportDetailT,
  progress: string,
): ReportDetailT => ({
  ...report,
  status: "generating",
  progress,
  error: "",
  sections: [],
  section_status: [],
  gaps: [],
  content_md: "",
  references: [],
  understanding: { ...report.understanding, credibility: undefined },
});

export type ReportWorkspace = ReturnType<typeof useReportWorkspace>;

export function useReportWorkspace({
  actorId,
  notebookId,
  active: reportTabActive,
  policy,
  effects,
}: UseReportWorkspaceOptions) {
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const effectsRef = useRef(effects);
  effectsRef.current = effects;
  const actorIdRef = useRef(actorId);
  actorIdRef.current = actorId;
  const notebookIdRef = useRef(notebookId);
  notebookIdRef.current = notebookId;
  const tabActiveRef = useRef(reportTabActive);
  tabActiveRef.current = reportTabActive;

  const generationRef = useRef(0);
  const viewGenerationRef = useRef(0);
  const ownerRef = useRef<ReportOwner | null>(null);
  const tombstonesRef = useRef(new Map<string, Set<string>>());
  const pendingDeletesRef = useRef(new Map<string, Set<string>>());
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const focusRef = useRef<{ id: string; actorId: string; notebookId: string } | null>(null);
  const operationTokensRef = useRef(new Map<string, object>());
  const transitionSuspendedRef = useRef(false);
  const transitionGenerationRef = useRef(0);
  // Hidden-state fallback for the export selection must be a **stable
  // reference** (same rule as the frozen `NO_*` arrays in the sibling
  // hooks): a fresh `new Set()` per render makes every consumer dependency
  // "change" each render. A module-level `Set` can't be frozen shut (unlike
  // an array/object literal, `Object.freeze` doesn't stop `.add`/`.delete`
  // on a Set/Map), so a stray write from anywhere in the module would leak
  // across every hook instance and every actor/notebook. A per-instance ref
  // keeps the same stable-reference guarantee but shrinks that blast radius
  // to this one hook instance — it is never mutated, only ever replaced via
  // `setSelectedIds(new Set(...))`.
  const hiddenSelectedIdsRef = useRef(new Set<string>());
  const beginOperation = (kind: string): object => {
    const token = {};
    operationTokensRef.current.set(kind, token);
    return token;
  };
  const ownsOperation = (kind: string, token: object): boolean =>
    operationTokensRef.current.get(kind) === token;

  const [reports, setReports] = useState<ReportSummaryT[] | null>(null);
  const [activeReport, setActiveReport] = useState<ReportDetailT | null>(null);
  const activeReportRef = useRef<ReportDetailT | null>(null);
  activeReportRef.current = activeReport;
  const [question, setQuestionState] = useState("");
  const [depthIndex, setDepthIndexState] = useState(REPORT_DEFAULT_DEPTH_INDEX);
  const [creating, setCreating] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [intentBusy, setIntentBusy] = useState(false);
  const [outlineBusy, setOutlineBusy] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);
  const [shared, setShared] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmDeleteId, setConfirmDeleteIdState] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [zipBusy, setZipBusy] = useState(false);
  const [ownerVersion, forceOwnerRender] = useState(0);

  const currentOwner = (): ReportOwner | null => {
    const owner = ownerRef.current;
    if (!owner || !tabActiveRef.current) return null;
    if (!actorIdRef.current || owner.actorId !== actorIdRef.current) return null;
    if (!notebookIdRef.current || owner.notebookId !== notebookIdRef.current) return null;
    return owner;
  };

  const owns = (owner: ReportOwner): boolean => Boolean(
    tabActiveRef.current
    && actorIdRef.current === owner.actorId
    && notebookIdRef.current === owner.notebookId
    && sameOwner(ownerRef.current, owner),
  );

  const ownsIdentity = (owner: Pick<ReportOwner, "actorId" | "notebookId">): boolean => Boolean(
    actorIdRef.current === owner.actorId
    && notebookIdRef.current === owner.notebookId
    && sameIdentity(ownerRef.current, owner),
  );

  const deletedIds = (owner: Pick<ReportOwner, "actorId" | "notebookId">): Set<string> =>
    tombstonesRef.current.get(ownerKey(owner)) ?? new Set<string>();

  const filterReports = (
    owner: Pick<ReportOwner, "actorId" | "notebookId">,
    rows: ReportSummaryT[],
  ): ReportSummaryT[] => {
    const deleted = deletedIds(owner);
    return deleted.size === 0 ? rows : rows.filter((row) => !deleted.has(row.id));
  };

  const surfaceError = (error: unknown, fallback = "报告操作没成功，请稍后重试") => {
    effectsRef.current.notify(toUserMessage(error, fallback));
  };

  const clearVisibleState = () => {
    setReports(null);
    setActiveReport(null);
    setQuestionState("");
    setDepthIndexState(REPORT_DEFAULT_DEPTH_INDEX);
    setCreating(false);
    setActionBusy(false);
    setIntentBusy(false);
    setOutlineBusy(false);
    setShareBusy(false);
    setShared(false);
    setConfirmDelete(false);
    setConfirmDeleteIdState(null);
    setDeletingId(null);
    setDownloadingId(null);
    setSelectMode(false);
    setSelectedIds(new Set());
    setZipBusy(false);
  };

  const invalidate = () => {
    generationRef.current += 1;
    viewGenerationRef.current += 1;
    ownerRef.current = null;
    listRequestRef.current += 1;
    detailRequestRef.current += 1;
    focusRef.current = null;
    clearVisibleState();
    forceOwnerRender((value) => value + 1);
  };

  const loadReportsFor = async (
    owner: ReportOwner,
    options: { surface?: boolean; clearOnError?: boolean } = {},
  ): Promise<ReportSummaryT[] | null> => {
    const requestId = ++listRequestRef.current;
    try {
      const rows = filterReports(owner, await listReports(owner.notebookId));
      if (!owns(owner) || requestId !== listRequestRef.current) return null;
      setReports(rows);
      return rows;
    } catch (error) {
      if (owns(owner) && requestId === listRequestRef.current) {
        if (options.clearOnError !== false) setReports([]);
        if (options.surface !== false) surfaceError(error);
      }
      return null;
    }
  };

  const loadDetailFor = async (
    owner: ReportOwner,
    reportId: string,
    options: { surface?: boolean } = {},
  ): Promise<ReportDetailT | null> => {
    const requestId = ++detailRequestRef.current;
    try {
      const detail = await getReport(owner.notebookId, reportId);
      if (!owns(owner) || requestId !== detailRequestRef.current) return null;
      if (deletedIds(owner).has(detail.id) || detail.id !== reportId) return null;
      setActiveReport(detail);
      return detail;
    } catch (error) {
      if (options.surface !== false && owns(owner)) surfaceError(error);
      return null;
    }
  };

  useEffect(() => {
    if (!reportTabActive || !actorId || !notebookId) {
      if (ownerRef.current) invalidate();
      return;
    }
    if (transitionSuspendedRef.current) return;
    const current = ownerRef.current;
    if (current && current.actorId === actorId && current.notebookId === notebookId) return;
    const owner: ReportOwner = {
      actorId,
      notebookId,
      generation: ++generationRef.current,
      viewGeneration: ++viewGenerationRef.current,
    };
    ownerRef.current = owner;
    clearVisibleState();
    setDeletingId(pendingDeletesRef.current.get(ownerKey(owner))?.values().next().value ?? null);
    void loadReportsFor(owner);
    // This is the single lazy entry read. Hidden tabs and notebook opening do zero report I/O.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actorId, notebookId, reportTabActive, ownerVersion]);

  useEffect(() => {
    const owner = currentOwner();
    const focus = focusRef.current;
    if (!owner || reports === null || !focus
      || focus.actorId !== owner.actorId || focus.notebookId !== owner.notebookId) return;
    focusRef.current = null;
    void loadDetailFor(owner, focus.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reports]);

  const hasLiveReports = (reports ?? []).some((report) => isReportActive(report.status));
  useEffect(() => {
    const owner = currentOwner();
    if (!owner || !hasLiveReports || activeReport !== null) return;
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (stopped || inFlight || !owns(owner)) return;
      inFlight = true;
      try {
        await loadReportsFor(owner, { surface: false, clearOnError: false });
      } finally {
        inFlight = false;
      }
    }, REPORT_POLL_INTERVAL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [hasLiveReports, activeReport]);

  const activeId = activeReport?.id ?? null;
  const activeLive = activeReport ? isReportActive(activeReport.status) : false;
  useEffect(() => {
    const owner = currentOwner();
    if (!owner || !activeId || !activeLive) return;
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (stopped || inFlight || !owns(owner)) return;
      inFlight = true;
      try {
        const detail = await loadDetailFor(owner, activeId, { surface: false });
        if (detail && !isReportActive(detail.status)) {
          await loadReportsFor(owner, { surface: false, clearOnError: false });
        }
      } finally {
        inFlight = false;
      }
    }, REPORT_POLL_INTERVAL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [activeId, activeLive]);

  useEffect(() => {
    if (activeReport?.status === "failed" && activeReport.error) {
      logDiagnostic("report", activeReport.error);
    }
  }, [activeReport?.status, activeReport?.error]);

  useEffect(() => {
    setShared(Boolean(activeReport?.shared));
  }, [activeReport?.id, activeReport?.shared]);

  useEffect(() => {
    if (!confirmDelete) return;
    const timer = window.setTimeout(() => setConfirmDelete(false), 4000);
    return () => window.clearTimeout(timer);
  }, [confirmDelete]);

  const beginNotebookTransition = (): ReportWorkspaceTransition => {
    transitionSuspendedRef.current = true;
    const transition = { generation: ++transitionGenerationRef.current };
    invalidate();
    return transition;
  };
  const finishNotebookTransition = (
    transition: ReportWorkspaceTransition,
    _succeeded: boolean,
  ) => {
    if (transition.generation !== transitionGenerationRef.current) return;
    transitionSuspendedRef.current = false;
    forceOwnerRender((value) => value + 1);
  };
  const leaveWorkspace = () => invalidate();
  const activateActor = (nextActorId: string) => {
    if (!nextActorId || (actorIdRef.current && actorIdRef.current !== nextActorId)) invalidate();
    actorIdRef.current = nextActorId;
  };

  const focusReport = (reportId: string) => {
    const owner = currentOwner();
    const focusActor = owner?.actorId ?? actorIdRef.current;
    const focusNotebook = owner?.notebookId ?? notebookIdRef.current;
    if (!focusActor || !focusNotebook || !reportId) return;
    focusRef.current = { id: reportId, actorId: focusActor, notebookId: focusNotebook };
    if (owner && reports !== null) {
      focusRef.current = null;
      void loadDetailFor(owner, reportId);
    }
  };

  const updateQuestion = (value: string) => { if (currentOwner()) setQuestionState(value); };
  const selectDepth = (value: number) => {
    if (currentOwner() && Number.isInteger(value) && value >= 0 && value < REPORT_DEPTHS.length) {
      setDepthIndexState(value);
    }
  };

  const submitCreate = async () => {
    const owner = currentOwner();
    const currentPolicy = policyRef.current;
    const trimmed = question.trim();
    if (!owner || !currentPolicy.canManageReports || currentPolicy.creationDisabled || !trimmed || creating) return;
    const operation = beginOperation("create");
    const sourceScope = copySourceScope(currentPolicy.sourceScope);
    const baseScope = copyBaseScope(currentPolicy.baseScope);
    setCreating(true);
    try {
      const depth = currentPolicy.advanced
        ? REPORT_DEPTHS[depthIndex]
        : REPORT_DEPTHS[REPORT_DEFAULT_DEPTH_INDEX];
      await createReport(owner.notebookId, trimmed, depth, sourceScope, baseScope, !currentPolicy.advanced);
      if (!owns(owner) || !ownsOperation("create", operation)) return;
      setQuestionState("");
      effectsRef.current.notify(currentPolicy.advanced
        ? "正在理解研究问题，完成后请先确认或补充关键信息"
        : "正在理解研究问题；若存在需要补充的关键信息会先请你确认，否则将自动完成大纲并生成报告");
      await loadReportsFor(owner, { surface: false, clearOnError: false });
    } catch (error) {
      if (owns(owner)) surfaceError(error);
    } finally {
      if (owns(owner) && ownsOperation("create", operation)) setCreating(false);
    }
  };

  const openReport = async (reportId: string) => {
    const owner = currentOwner();
    if (!owner || deletedIds(owner).has(reportId)) return;
    setConfirmDelete(false);
    await loadDetailFor(owner, reportId);
  };

  const backToList = () => {
    const owner = currentOwner();
    if (!owner) return;
    detailRequestRef.current += 1;
    setActiveReport(null);
    setConfirmDelete(false);
    void loadReportsFor(owner);
  };

  const requestCancel = async () => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || actionBusy) return;
    const operation = beginOperation("action");
    setActionBusy(true);
    try {
      const result = await cancelReport(owner.notebookId, report.id);
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return;
      effectsRef.current.notify(result.status === "cancelled"
        ? "已请求取消，报告将停在当前进度"
        : "报告已进入终态，无需再取消");
      await loadDetailFor(owner, report.id, { surface: false });
    } catch (error) {
      if (owns(owner)) surfaceError(error);
    } finally {
      if (owns(owner) && ownsOperation("action", operation)) setActionBusy(false);
    }
  };

  const requestRetry = async () => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || actionBusy
      || report.status !== "failed" || report.outline.length === 0) return;
    const operation = beginOperation("action");
    setActionBusy(true);
    try {
      await generateReport(owner.notebookId, report.id, report.depth);
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return;
      setActiveReport(optimisticGenerating(report, "准备生成"));
      effectsRef.current.notify("已按原确认问题和大纲重新生成");
      void loadReportsFor(owner, { surface: false, clearOnError: false });
    } catch (error) {
      if (owns(owner)) surfaceError(error);
    } finally {
      if (owns(owner) && ownsOperation("action", operation)) setActionBusy(false);
    }
  };

  const confirmIntent = async (payload: {
    resolved_question: string;
    answers: { id: string; answer: string }[];
  }) => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || intentBusy) return;
    const operation = beginOperation("intent");
    setIntentBusy(true);
    try {
      await confirmReportIntent(owner.notebookId, report.id, payload);
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return;
      setActiveReport({ ...report, status: "planning", progress: "按已确认问题规划中" });
      effectsRef.current.notify("问题理解已确认，开始检查语料并规划大纲");
      void loadReportsFor(owner, { surface: false, clearOnError: false });
    } catch (error) {
      if (owns(owner)) surfaceError(error, "问题确认没能提交，请稍后重试");
    } finally {
      if (owns(owner) && ownsOperation("intent", operation)) setIntentBusy(false);
    }
  };

  const confirmOutline = async (payload: {
    sections: ReportOutlineSectionT[];
    frame?: ReportFrameT;
  }) => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || outlineBusy) return;
    const operation = beginOperation("outline");
    setOutlineBusy(true);
    try {
      await updateReportOutline(owner.notebookId, report.id, payload);
      if (!owns(owner) || !policyRef.current.canManageReports
        || activeReportRef.current?.id !== report.id) return;
      await generateReport(owner.notebookId, report.id);
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return;
      setActiveReport(optimisticGenerating(report, `章节 0/${payload.sections.length} 完成`));
      effectsRef.current.notify("已确认大纲，开始生成完整报告");
      void loadReportsFor(owner, { surface: false, clearOnError: false });
    } catch (error) {
      if (owns(owner)) surfaceError(error, "报告没能生成完，可以重试");
    } finally {
      if (owns(owner) && ownsOperation("outline", operation)) setOutlineBusy(false);
    }
  };

  const toggleShare = async () => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || shareBusy) return;
    const operation = beginOperation("share");
    const wasShared = shared;
    setShareBusy(true);
    try {
      if (wasShared) {
        await unshareReport(owner.notebookId, report.id);
        if (!owns(owner) || activeReportRef.current?.id !== report.id) return;
        setShared(false);
        effectsRef.current.notify("已取消分享，原链接立即失效");
      } else {
        const { share_token: token } = await shareReport(owner.notebookId, report.id);
        if (!owns(owner) || activeReportRef.current?.id !== report.id) return;
        setShared(true);
        await effectsRef.current.announceShareLink(token);
      }
    } catch (error) {
      if (owns(owner) && activeReportRef.current?.id === report.id) {
        surfaceError(error, "分享操作失败");
      }
    } finally {
      if (owns(owner) && ownsOperation("share", operation)) setShareBusy(false);
    }
  };

  // 返回「链接有没有进剪贴板」;null = 这一次压根没走到复制(前置守卫不通过、切库/换
  // 报告导致失效、或取回链接本身失败)。调用方据此决定要不要在按钮上画结果——把 null
  // 也当失败会在用户什么都没等到的时候闪一下「复制失败」。
  const copyShareLink = async (): Promise<boolean | null> => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || shareBusy) return null;
    const operation = beginOperation("share");
    setShareBusy(true);
    try {
      const { share_token: token } = await getReportShare(owner.notebookId, report.id);
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
      return await effectsRef.current.announceShareLink(token);
    } catch (error) {
      if (owns(owner) && activeReportRef.current?.id === report.id) {
        surfaceError(error, "取回分享链接失败");
      }
      return null;
    } finally {
      if (owns(owner) && ownsOperation("share", operation)) setShareBusy(false);
    }
  };

  const recordDelete = (owner: ReportOwner, reportId: string) => {
    const key = ownerKey(owner);
    const next = new Set(tombstonesRef.current.get(key) ?? []);
    next.add(reportId);
    tombstonesRef.current.set(key, next);
    if (!ownsIdentity(owner)) return;
    setReports((rows) => rows?.filter((row) => row.id !== reportId) ?? rows);
    setSelectedIds((ids) => {
      const copy = new Set(ids);
      copy.delete(reportId);
      return copy;
    });
    setActiveReport((current) => current?.id === reportId ? null : current);
  };

  const deleteById = async (reportId: string) => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canManageReports || actionBusy) return;
    const key = ownerKey(owner);
    const pending = new Set(pendingDeletesRef.current.get(key) ?? []);
    if (pending.has(reportId) || pending.size > 0) return;
    pending.add(reportId);
    pendingDeletesRef.current.set(key, pending);
    const operation = beginOperation("delete");
    setDeletingId(reportId);
    if (activeReportRef.current?.id === reportId) setActionBusy(true);
    try {
      await deleteReport(owner.notebookId, reportId);
      recordDelete(owner, reportId);
      if (owns(owner)) {
        setConfirmDelete(false);
        setConfirmDeleteIdState(null);
        effectsRef.current.notify("报告已删除");
        void loadReportsFor(owner, { surface: false, clearOnError: false });
      }
    } catch (error) {
      if (owns(owner)) surfaceError(error);
    } finally {
      const remaining = new Set(pendingDeletesRef.current.get(key) ?? []);
      remaining.delete(reportId);
      if (remaining.size === 0) pendingDeletesRef.current.delete(key);
      else pendingDeletesRef.current.set(key, remaining);
      if (ownsIdentity(owner)) {
        setDeletingId(remaining.values().next().value ?? null);
      }
      if (owns(owner) && ownsOperation("delete", operation)) {
        setActionBusy(false);
      }
    }
  };

  const requestDelete = () => {
    const report = activeReportRef.current;
    if (!report || !policyRef.current.canManageReports) return;
    if (!confirmDelete) { setConfirmDelete(true); return; }
    void deleteById(report.id);
  };

  const chooseDeleteConfirmation = (reportId: string | null) => {
    if (currentOwner() && policyRef.current.canManageReports) setConfirmDeleteIdState(reportId);
  };

  const downloadOne = async (reportId: string) => {
    const owner = currentOwner();
    if (!owner || downloadingId || deletedIds(owner).has(reportId)) return;
    setDownloadingId(reportId);
    try {
      const detail = await getReport(owner.notebookId, reportId);
      if (!owns(owner) || detail.id !== reportId) return;
      if (!detail.content_md) {
        effectsRef.current.notify("该报告没有正文内容，无法下载");
        return;
      }
      effectsRef.current.downloadMarkdown(detail);
    } catch (error) {
      if (owns(owner)) surfaceError(error);
    } finally {
      if (owns(owner)) setDownloadingId(null);
    }
  };

  const toggleSelectMode = () => {
    if (!currentOwner()) return;
    setSelectMode((enabled) => {
      if (enabled) setSelectedIds(new Set());
      return !enabled;
    });
  };

  const toggleSelected = (reportId: string) => {
    if (!currentOwner()) return;
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(reportId)) next.delete(reportId); else next.add(reportId);
      return next;
    });
  };

  const downloadSelected = async () => {
    const owner = currentOwner();
    if (!owner || zipBusy || selectedIds.size === 0) return;
    setZipBusy(true);
    try {
      const blob = await fetchReportsZip(owner.notebookId, Array.from(selectedIds));
      if (!owns(owner)) return;
      effectsRef.current.downloadArchive(blob);
      setSelectMode(false);
      setSelectedIds(new Set());
    } catch (error) {
      if (owns(owner)) surfaceError(error);
    } finally {
      if (owns(owner)) setZipBusy(false);
    }
  };

  const visible = Boolean(currentOwner());
  return {
    reports: visible ? reports : null,
    active: visible ? activeReport : null,
    question: visible ? question : "",
    depthIndex: visible ? depthIndex : REPORT_DEFAULT_DEPTH_INDEX,
    creating: visible && creating,
    actionBusy: visible && actionBusy,
    intentBusy: visible && intentBusy,
    outlineBusy: visible && outlineBusy,
    shareBusy: visible && shareBusy,
    shared: visible && shared,
    confirmDelete: visible && confirmDelete,
    confirmDeleteId: visible ? confirmDeleteId : null,
    deletingId: visible ? deletingId : null,
    downloadingId: visible ? downloadingId : null,
    selectMode: visible && selectMode,
    selectedIds: visible ? selectedIds : hiddenSelectedIdsRef.current,
    zipBusy: visible && zipBusy,
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    leaveWorkspace,
    focusReport,
    updateQuestion,
    selectDepth,
    submitCreate,
    openReport,
    backToList,
    requestCancel,
    requestRetry,
    confirmIntent,
    confirmOutline,
    toggleShare,
    copyShareLink,
    requestDelete,
    deleteById,
    chooseDeleteConfirmation,
    downloadOne,
    toggleSelectMode,
    toggleSelected,
    downloadSelected,
  };
}
