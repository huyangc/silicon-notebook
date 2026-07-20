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
      return {
        label: "模型服务异常，正在停止本次分析…",
        detail: "正在等待已开始的请求安全退出，已完成内容会保留",
        tone: "warning",
        actionLabel: null,
      };
    }
    return {
      label: `正在分析 ${job.completed_sources}/${job.total_sources}`,
      detail: job.failed_sources > 0
        ? `${job.failed_sources} 篇未完成；其余来源继续处理`
        : "已完成内容会即时保留",
      tone: "neutral",
      actionLabel: null,
    };
  }

  if (job.status === "failed") {
    return {
      label:
        `分析已中断 · 已完成 ${job.completed_sources}/${job.total_sources}`,
      detail: job.user_message || "已完成内容已保留，可继续分析未完成内容。",
      tone: "error",
      actionLabel:
        pendingSources > 0 ? "继续分析未完成内容" : null,
    };
  }

  if (job.failed_sources > 0 || pendingSources > 0) {
    const unfinished = Math.max(job.failed_sources, pendingSources);
    return {
      label: `分析完成 · ${unfinished} 篇未完成`,
      detail: "已完成内容已保留，可继续处理未完成来源",
      tone: "warning",
      actionLabel:
        pendingSources > 0 ? "继续分析未完成内容" : null,
    };
  }

  return {
    label: "知识图谱分析完成",
    detail: `已完成 ${job.completed_sources}/${job.total_sources}`,
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
    return `知识图谱分析完成，但有 ${job.failed_sources} 篇未完成`;
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
