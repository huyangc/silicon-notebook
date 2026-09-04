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
    // 批 3·W3(D3):大库上「重试当前管线」= 非内建目标,服务端恒 409;
    // 「切回内建」是被豁免的唯一自助出口,保留。按钮与文案出自同一真值,
    // 不给必然失败的点击。
    if (projection.large_library_locked === true) {
      return {
        tone: "warning",
        detail:
          "索引管线重建失败；旧索引仍可读取，新写入会暂时被阻止。这本笔记本规模较大，暂不支持重试自定义管线；可切回内建管线恢复写入。",
        canRevert: true,
        canRetry: false,
      };
    }
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

export function indexingPipelineOptionLocked(
  projection: IndexingPipelineResponse | null,
  optionId: string | null | undefined,
): boolean {
  // 批 3·W3(D3):大库只锁**非内建**目标——切回内建是服务端豁免的恢复
  // 出口,radio 必须保持可点;内建选项在锁定库上照常可选(变更为内建 →
  // 放行;无变化 → 前端本就不发 PATCH)。
  return (
    projection?.large_library_locked === true
    && normalizeIndexingPipelineId(optionId) !== ""
  );
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
  // 只有 summary、没有 options 投影时拿不到 descriptor 的 label——上通用界面词,
  // 不把 `acme.fast` 这种内部 id 当文案上屏(词汇守卫扫不到运行时数据,这里自守)。
  const label = pipelineId ? `部署插件提供的索引管线 · v${version}` : `内建管线 · v${version}`;
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
