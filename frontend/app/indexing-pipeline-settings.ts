import type {
  IndexingPipelineOption,
  IndexingPipelineResponse,
} from "./notebook-api.ts";
import type { NotebookSummary } from "./workspace-model.ts";

export function normalizeIndexingPipelineId(
  value: string | null | undefined,
): string {
  return typeof value === "string" ? value : "";
}

export function indexingPipelineIdsEqual(
  left: string | null | undefined,
  right: string | null | undefined,
): boolean {
  return normalizeIndexingPipelineId(left) === normalizeIndexingPipelineId(right);
}

export function selectedIndexingPipelineOption(
  projection: IndexingPipelineResponse | null,
  pipelineId: string | null | undefined = projection?.pipeline_id,
): IndexingPipelineOption | null {
  if (!projection) return null;
  const normalized = normalizeIndexingPipelineId(pipelineId);
  return projection.options.find((option) =>
    normalizeIndexingPipelineId(option.pipeline_id) === normalized) ?? null;
}

export function describeIndexingPipelineState(
  projection: IndexingPipelineResponse | null,
): { tone: "warning" | "status"; detail: string; canRevert: boolean; canRetry: boolean } | null {
  if (!projection) return null;
  if (projection.missing) {
    return {
      tone: "warning",
      detail:
        "已选索引管线当前缺席；旧索引仍可读取，新写入会暂时被阻止。可切回内建管线并重建全库索引以恢复写入。",
      canRevert: true,
      canRetry: false,
    };
  }
  if (!projection.available && normalizeIndexingPipelineId(projection.pipeline_id)) {
    return {
      tone: "warning",
      detail:
        "已选索引管线当前不可用；旧索引仍可读取，新写入会暂时被阻止。可切回内建管线并重建全库索引。",
      canRevert: true,
      canRetry: false,
    };
  }
  if (projection.rebuild_status === "failed") {
    return {
      tone: "warning",
      detail:
        "索引管线重建失败；旧索引仍可读取，新写入会暂时被阻止。重试当前管线或切回内建都会重建全库索引。",
      canRevert: true,
      canRetry: true,
    };
  }
  if (projection.pending || projection.rebuild_status === "pending") {
    return {
      tone: "status",
      detail:
        "索引管线切换已提交，正在重建全库索引。重建完成前旧索引继续可读，新写入会暂时被阻止。",
      canRevert: false,
      canRetry: false,
    };
  }
  return null;
}

export function indexingPipelineConfirmMessage(
  option: Pick<IndexingPipelineOption, "label"> | null,
): string {
  const label = option?.label?.trim() || "所选索引管线";
  return `切换到“${label}”将重建全库索引。重建完成前旧索引继续可读，新写入会暂时被阻止。是否继续？`;
}

export function indexingPipelineReadOnlySummary(
  projection: IndexingPipelineResponse | null,
): { label: string; detail: string } {
  const selected = selectedIndexingPipelineOption(projection);
  const notice = describeIndexingPipelineState(projection);
  return {
    label: selected
      ? `${selected.label} · v${selected.version}`
      : `内建管线 · v${projection?.version ?? "builtin-v1"}`,
    detail: notice?.detail ?? "当前索引构建使用该管线。",
  };
}

export function notebookIndexingPipelineReadOnlySummary(
  notebook: Pick<
    NotebookSummary,
    | "indexing_pipeline_id"
    | "indexing_pipeline_version"
    | "indexing_pipeline_available"
    | "indexing_pipeline_missing"
    | "indexing_pipeline_pending"
  > | null,
): { label: string; detail: string } {
  const pipelineId = notebook?.indexing_pipeline_id ?? "";
  const version = notebook?.indexing_pipeline_version ?? "builtin-v1";
  const label = pipelineId ? `${pipelineId} · v${version}` : `内建管线 · v${version}`;
  if (notebook?.indexing_pipeline_missing) {
    return {
      label,
      detail: "当前选中的索引管线缺席；旧索引仍可读取，新写入会暂时被阻止。",
    };
  }
  if (pipelineId && notebook?.indexing_pipeline_available === false) {
    return {
      label,
      detail: "当前选中的索引管线不可用；旧索引仍可读取，新写入会暂时被阻止。",
    };
  }
  if (notebook?.indexing_pipeline_pending) {
    return {
      label,
      detail: "索引管线切换已提交，正在重建全库索引。重建完成前旧索引继续可读。",
    };
  }
  return {
    label,
    detail: "当前索引构建使用该管线。",
  };
}
