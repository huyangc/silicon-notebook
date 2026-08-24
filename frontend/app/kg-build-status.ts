import type { KgBuildJobStatus } from "./workspace-model";

export type KgBuildTone = "neutral" | "warning" | "error" | "success";

export type KgBuildPresentation = {
  label: string;
  detail: string;
  tone: KgBuildTone;
  actionLabel: string | null;
};

export type KgBuildRequestOwner = {
  notebookId: string;
  workspaceEpoch: number;
  requestEpoch: number;
};

export function canContinueKgBuild(
  actionLabel: string | null,
  buildingKg: boolean,
  // 无写权即隐藏「继续分析」——page.tsx 传 `readOnlyWorkspace`(= !canWriteNotebook)。
  // ⚠ P2-T2 起**不是** `isReader`:继续构建 KG 是内容写(kg:write=admin),组管理员
  // 有权(readOnlyWorkspace 为 false),按 isReader 判会把他挡掉。
  readOnly: boolean,
): boolean {
  return Boolean(actionLabel) && !buildingKg && !readOnly;
}

export function ownsKgBuildRequest(
  owner: KgBuildRequestOwner,
  currentNotebookId: string | null | undefined,
  currentWorkspaceEpoch: number,
  currentRequestEpoch: number,
): boolean {
  return (
    owner.notebookId === currentNotebookId
    && owner.workspaceEpoch === currentWorkspaceEpoch
    && owner.requestEpoch === currentRequestEpoch
  );
}

export function kgBuildPresentation(
  job: KgBuildJobStatus | null | undefined,
  pendingSources: number,
  ready: boolean,
): KgBuildPresentation {
  if (!job) {
    if (!ready) {
      return {
        label: "尚未整理知识图谱",
        detail: "从来源分析知识对象与关系",
        tone: "neutral",
        actionLabel: null,
      };
    }
    if (pendingSources > 0) {
      return {
        label: `${pendingSources} 篇来源待分析`,
        detail: "已完成内容可继续使用；可分析新增或未完成内容",
        tone: "warning",
        actionLabel: null,
      };
    }
    return {
      label: "知识图谱已就绪",
      detail: "全部可分析来源均已完成",
      tone: "success",
      actionLabel: null,
    };
  }

  if (job.status === "running") {
    if (job.stage === "probing") {
      return {
        label: "正在连接模型服务…",
        detail: "连接成功后才会开始分析来源",
        tone: "neutral",
        actionLabel: null,
      };
    }
    if (job.stage === "stopping") {
      // 人工中断（Ctrl-C / 终止信号，离线批量分析的常规结束方式）也会经过「正在
      // 停止」，此时断言「模型服务异常」就是不实文案——停止原因只能按 error_code 判。
      return {
        label: job.error_code === "worker_interrupted"
          ? "正在停止本次分析…"
          : "模型服务异常，正在停止本次分析…",
        detail: "正在等待已开始的请求安全退出，已完成内容会保留",
        tone: "warning",
        actionLabel: null,
      };
    }
    return {
      label: `正在分析 ${job.completed_sources}/${job.total_sources} 项内容`,
      detail: job.failed_sources > 0
        ? `${job.failed_sources} 项内容未完成；其余内容继续处理`
        : "正在提取知识对象与关系",
      tone: "neutral",
      actionLabel: null,
    };
  }

  if (job.status === "failed") {
    const responseInvalid = job.error_code === "model_response_invalid";
    return {
      label: responseInvalid
        ? `模型响应不可用 · 已完成 ${job.completed_sources}/${job.total_sources} 项内容`
        : `分析已中断 · 已完成 ${job.completed_sources}/${job.total_sources} 项内容`,
      detail: job.user_message || "已完成内容已保留，可继续分析未完成内容。",
      tone: "error",
      actionLabel:
        pendingSources > 0 ? "继续分析未完成内容" : null,
    };
  }

  if (job.failed_sources > 0 || pendingSources > 0) {
    const unfinished = Math.max(job.failed_sources, pendingSources);
    return {
      label: `分析完成 · ${unfinished} 项内容未完成`,
      detail: "已完成内容已保留，可继续处理未完成来源",
      tone: "warning",
      actionLabel:
        pendingSources > 0 ? "继续分析未完成内容" : null,
    };
  }

  return {
    label: "知识图谱分析完成",
    detail: `已完成 ${job.completed_sources}/${job.total_sources} 项内容`,
    tone: "success",
    actionLabel: null,
  };
}

export function kgBuildTerminalToast(
  job: KgBuildJobStatus | null | undefined,
): string | null {
  if (!job || job.status === "running") return null;
  if (job.status === "failed") {
    return job.user_message || "知识图谱分析已中断；已完成内容已保留。";
  }
  if (job.failed_sources > 0) {
    return `知识图谱分析完成，但有 ${job.failed_sources} 项内容未完成`;
  }
  return "知识图谱分析完成 ✓";
}

export function isTrackedKgTerminal(
  trackedJobId: string | null | undefined,
  job: KgBuildJobStatus | null | undefined,
): boolean {
  return Boolean(
    trackedJobId
      && job
      && trackedJobId === job.job_id
      && job.status !== "running",
  );
}

/**
 * Notebook summaries expose the latest durable KG job. A different id therefore
 * means another tab started a newer job after the locally tracked one ended.
 * Adopt a newer running job, or accept its terminal state, so polling cannot
 * remain pinned forever to an older id.
 */
export function reconcileTrackedKgPoll(
  trackedJobId: string | null | undefined,
  latestJob: KgBuildJobStatus | null | undefined,
): { terminal: boolean; trackedJobId: string | null } {
  if (!trackedJobId || !latestJob) {
    return {
      terminal: false,
      trackedJobId: trackedJobId ?? null,
    };
  }
  if (latestJob.status === "running") {
    return {
      terminal: false,
      trackedJobId: latestJob.job_id,
    };
  }
  return {
    terminal: true,
    trackedJobId: null,
  };
}
