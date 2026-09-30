"use client";

import { useEffect, useRef, useState } from "react";
import { httpErrorStatus, logDiagnostic, toUserMessage } from "./errors";
import {
  cancelReport,
  confirmReportIntent,
  createReport,
  deleteReport,
  fetchReportsZip,
  generateReport,
  getReport,
  getReportShare,
  getReportShareDisclosure,
  listReports,
  ShareDisclosureRequired,
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

// 公开前的确认条状态。`count` 是服务端数出来的「公开页可能包含来自几条作者本人个人记忆的
// 内容」(取数失败、靠 409 才拿到确数之前为 null);`added` 非 null 表示这是 409 带回的新数字
// (值为比确认时多出的条数),条上先说「条数有变化」;`refusal` 非 null 表示这份报告不能公开
// (引用了其他成员的个人记忆,或服务端拒绝了这次公开)——那句中文原因就地显示,不再给
// 「确认公开」。
/** 披露端点说报告引用了其他成员的个人记忆时,不发请求、就地显示的原因(与服务端 403 同句)。 */
export const FOREIGN_MEMORY_REFUSAL = "报告引用了其他成员的个人记忆，不能公开";
/** 以前就已公开、但引用了其他成员个人记忆的报告:公开链接已打不开时,作者这里看到的说明。 */
export const SHARED_LINK_REFUSED = "报告引用了其他成员的个人记忆，公开链接已无法打开，可以取消分享";

export type ReportShareConfirmState = {
  count: number | null;
  added: number | null;
  refusal: string | null;
};

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
  const [shareConfirm, setShareConfirm] = useState<ReportShareConfirmState | null>(null);
  const [sharedRefusal, setSharedRefusal] = useState<string | null>(null);
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
    setShareConfirm(null);
    setSharedRefusal(null);
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

  // 确认条属于「打开的那一份报告」:换报告、回列表都得收起,别让上一份的条数挂在下一份上。
  useEffect(() => {
    setShareConfirm(null);
  }, [activeReport?.id]);

  // 旧数据:以前就已公开、但引用了其他成员个人记忆的报告,公开页现在打不开(与撤销的链接一样)。
  // 作者这里不能还显示成「已公开、可复制链接」——打开这样一份已公开的报告时读一次披露,引用了
  // 别人的记忆就改成就地说明链接已失效,只留「取消分享」。
  useEffect(() => {
    setSharedRefusal(null);
    const owner = currentOwner();
    const report = activeReport;
    if (
      !owner || !policyRef.current.canManageReports || !report
      || !report.shared || report.status !== "done"
    ) return undefined;
    let cancelled = false;
    getReportShareDisclosure(owner.notebookId, report.id).then((disclosure) => {
      if (cancelled || !owns(owner) || activeReportRef.current?.id !== report.id) return;
      const others = disclosure?.foreign_memory_count;
      if (typeof others === "number" && others > 0) setSharedRefusal(SHARED_LINK_REFUSED);
    }).catch((error) => logDiagnostic("report", error));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeReport?.id, activeReport?.shared, activeReport?.status]);

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

  // 发一次公开请求并处理它的四种结局。调用方已占好 "share" 操作令牌与 shareBusy。
  // 返回「链接有没有进剪贴板」;null = 没有走到复制(被拒、需要确认、切库/换报告失效)。
  const publishReport = async (
    owner: ReportOwner,
    report: ReportDetailT,
    acknowledged: number | undefined,
  ): Promise<boolean | null> => {
    try {
      const { share_token: token } = await shareReport(owner.notebookId, report.id, acknowledged);
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
      setShared(true);
      setShareConfirm(null);
      return await effectsRef.current.announceShareLink(token);
    } catch (error) {
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
      if (error instanceof ShareDisclosureRequired) {
        // 服务端此刻的确数与作者确认的不等:确认条就地换成确数,由作者重新决定。作者已经
        // 看过一个数字时,条上先说「条数有变化」;取披露失败、这是第一次拿到数字时不说。
        const { memoryCount, newMemoryCount } = error;
        setShareConfirm((prev) => ({
          count: memoryCount,
          added: prev?.count != null ? newMemoryCount : null,
          refusal: null,
        }));
      } else if (httpErrorStatus(error) === 403) {
        // 服务端拒绝公开(非作者、或引用了其他成员的个人记忆):那句原因就地显示在确认条里。
        const refusal = toUserMessage(error, "只有作者可以公开分享这份报告");
        setShareConfirm((prev) => ({ count: prev?.count ?? null, added: null, refusal }));
      } else {
        // 请求没有拿到回答(断网、超时):服务端可能已经公开了。重读一次分享状态,如实报告——
        // 已公开就按公开成功处理(结果落在按钮上),否则才报「分享操作失败」。
        if (httpErrorStatus(error) === undefined) {
          try {
            const { share_token: token } = await getReportShare(owner.notebookId, report.id);
            if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
            if (token) {
              setShared(true);
              setShareConfirm(null);
              return await effectsRef.current.announceShareLink(token);
            }
          } catch (readError) {
            logDiagnostic("report", readError);
            if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
          }
        }
        surfaceError(error, "分享操作失败");
      }
      return null;
    }
  };

  // 分享 / 取消分享。返回值只在「这一次公开成功」时有意义(链接有没有进剪贴板),其余 null。
  const toggleShare = async (): Promise<boolean | null> => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    if (!owner || !policyRef.current.canManageReports || !report || shareBusy) return null;
    const operation = beginOperation("share");
    const wasShared = shared;
    setShareBusy(true);
    try {
      if (wasShared) {
        await unshareReport(owner.notebookId, report.id);
        if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
        setShared(false);
        effectsRef.current.notify("已取消分享，原链接立即失效");
        return null;
      }
      // 先取披露:公开页可能包含来自几条作者本人个人记忆的内容,以及有没有引用其他成员的
      // 个人记忆。引用了别人的 → 不发 POST,确认条就地说明不能公开;0 条 → 直接公开(与从前
      // 逐字节相同的那一发 POST);>0 → 展开确认条等作者决定。取数失败不拦公开:POST 不带
      // 确认值,服务端 409 / 403 会把确数或原因带回来。
      let count: number | undefined;
      let foreign = 0;
      try {
        const disclosure = await getReportShareDisclosure(owner.notebookId, report.id);
        const value = disclosure?.memory_count;
        count = Number.isInteger(value) && value >= 0 ? value : undefined;
        const others = disclosure?.foreign_memory_count;
        foreign = typeof others === "number" && Number.isInteger(others) && others > 0 ? others : 0;
      } catch (error) {
        logDiagnostic("report", error);
      }
      if (!owns(owner) || activeReportRef.current?.id !== report.id) return null;
      if (foreign > 0) {
        setShareConfirm({ count: null, added: null, refusal: FOREIGN_MEMORY_REFUSAL });
        return null;
      }
      if (count !== undefined && count > 0) {
        setShareConfirm({ count, added: null, refusal: null });
        return null;
      }
      return await publishReport(owner, report, undefined);
    } catch (error) {
      if (owns(owner) && activeReportRef.current?.id === report.id) {
        surfaceError(error, "分享操作失败");
      }
      return null;
    } finally {
      if (owns(owner) && ownsOperation("share", operation)) setShareBusy(false);
    }
  };

  // 确认条上的「确认公开」:把作者看到的那个数字原样交给服务端核对。
  const confirmShare = async (): Promise<boolean | null> => {
    const owner = currentOwner();
    const report = activeReportRef.current;
    const pending = shareConfirm;
    if (
      !owner || !policyRef.current.canManageReports || !report || shareBusy
      || !pending || pending.count === null || pending.refusal !== null
    ) return null;
    const operation = beginOperation("share");
    setShareBusy(true);
    try {
      return await publishReport(owner, report, pending.count);
    } finally {
      if (owns(owner) && ownsOperation("share", operation)) setShareBusy(false);
    }
  };

  // 「取消」:收起确认条,什么请求都不发。请求在飞时不可取消(结果马上落地)。
  const cancelShareConfirm = () => {
    if (shareBusy) return;
    setShareConfirm(null);
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
    shareConfirm: visible ? shareConfirm : null,
    sharedRefusal: visible && shared ? sharedRefusal : null,
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
    confirmShare,
    cancelShareConfirm,
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
