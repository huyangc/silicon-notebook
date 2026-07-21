import {
  MODEL_ROLE_LABELS,
  STATUS_MODEL_ROLES,
  buildPutPayload,
  mergeModelServiceStatus,
  summarizeModelServices,
  type ModelRole,
  type ModelSettingsPatch,
  type ModelServiceStatusItem,
  type ModelServicesStatus,
  type ServiceForm,
  type StatusModelRole,
} from "./model-settings.ts";


export type ModelServiceSummaryView = {
  text: string;
  tone: "ok" | "warn" | "bad" | "connecting";
  title: string;
};


export type ModelServiceStatusState = {
  status: ModelServicesStatus | null;
  unavailable: boolean;
};


function isCompleteModelServiceStatus(status: ModelServicesStatus | null): boolean {
  return status !== null && STATUS_MODEL_ROLES.every(
    (role) => status.services.some((item) => item.service === role),
  );
}


export function acceptModelServiceStatusSnapshot(
  status: ModelServicesStatus,
): ModelServiceStatusState {
  return {
    status,
    unavailable: !isCompleteModelServiceStatus(status),
  };
}


/**
 * A one-role probe can refresh one row, but cannot establish that the other
 * effective services are known. Only a complete read/test-all response may
 * recover an unavailable or partial snapshot.
 */
export function applyModelServiceTestResult(
  current: ModelServiceStatusState,
  replacement: ModelServiceStatusItem | ModelServicesStatus,
  coverage: "single" | "all",
): ModelServiceStatusState {
  const hadCompleteSnapshot = isCompleteModelServiceStatus(current.status);
  return {
    status: mergeModelServiceStatus(current.status, replacement),
    unavailable: coverage === "all"
      ? false
      : current.unavailable || !hadCompleteSnapshot,
  };
}


const IDLE_STATUS_TEXT = new Set([
  "",
  "连接中",
  "服务正常",
  "服务正常 · 模型未配置",
  "服务连接异常",
]);


function operationalTone(text: string): ModelServiceSummaryView["tone"] {
  if (text.startsWith("正在")) return "connecting";
  if (/失败|异常|超时|错误|问题|失效/.test(text)) return "bad";
  return "warn";
}


export function deriveModelServiceSummaryView({
  apiStatus,
  statusText,
  modelStatus,
  modelStatusUnavailable,
}: {
  apiStatus: string | null;
  statusText: string;
  modelStatus: ModelServicesStatus | null;
  modelStatusUnavailable: boolean;
}): ModelServiceSummaryView {
  if (!apiStatus) {
    const text = statusText || "连接中";
    return {
      text,
      tone: text === "连接中" ? "connecting" : operationalTone(text),
      title: text === "连接中" ? "正在连接服务…" : text,
    };
  }
  if (apiStatus !== "ok") {
    return { text: "服务连接异常", tone: "bad", title: "API 服务连接异常" };
  }

  const operationalText = statusText.trim();
  if (!IDLE_STATUS_TEXT.has(operationalText)) {
    return {
      text: operationalText,
      tone: operationalTone(operationalText),
      title: operationalText,
    };
  }
  if (modelStatusUnavailable) {
    return {
      text: "API 正常 · 模型状态未知",
      tone: "warn",
      title: "API 正常；模型状态暂时不可用，打开模型服务查看",
    };
  }
  if (!modelStatus) {
    return {
      text: "API 正常 · 正在读取模型状态",
      tone: "connecting",
      title: "正在读取已保存的模型状态，不会自动测试模型",
    };
  }

  const summary = summarizeModelServices(modelStatus.services);
  const pending = modelStatus.services.filter(
    (item) => item.configured && item.status === "untested",
  );
  return {
    text: summary.text,
    tone: summary.tone,
    title: summary.abnormal.length > 0
      ? `模型异常：${summary.abnormal.map((item) =>
          `${MODEL_ROLE_LABELS[item.service]} ${item.model || "未配置"}`
        ).join("、")}`
      : pending.length > 0
        ? `待测试：${pending.map((item) =>
            `${MODEL_ROLE_LABELS[item.service]} ${item.model || "未配置"}`
          ).join("、")}`
        : "API 和已配置模型状态正常",
  };
}


export type ModelTestTicket = {
  id: number;
  epoch: number;
  kind: "one" | "all" | "draft";
  service?: StatusModelRole;
  draftRevision?: number;
};


export type ModelTestActivity = {
  roles: Partial<Record<StatusModelRole, boolean>>;
  drafts: Partial<Record<ModelRole, boolean>>;
  all: boolean;
};


export function prepareModelSettingsSave(
  forms: Record<ModelRole, ServiceForm>,
): ModelSettingsPatch | null {
  const payload = buildPutPayload(forms);
  return Object.keys(payload).length > 0 ? payload : null;
}


export class ModelTestCoordinator {
  private epoch = 0;
  private nextId = 1;
  private allTicketId: number | null = null;
  private roleTicketIds = new Map<StatusModelRole, number>();
  private draftTicketIds = new Map<ModelRole, number>();
  private draftRevisions = new Map<ModelRole, number>();

  beginOne(service: StatusModelRole): ModelTestTicket | null {
    if (
      this.allTicketId !== null
      || this.roleTicketIds.has(service)
      || this.draftTicketIds.has(service as ModelRole)
    ) return null;
    const ticket = {
      id: this.nextId++,
      epoch: this.epoch,
      kind: "one" as const,
      service,
    };
    this.roleTicketIds.set(service, ticket.id);
    return ticket;
  }

  beginAll(): ModelTestTicket | null {
    if (
      this.allTicketId !== null
      || this.roleTicketIds.size > 0
      || this.draftTicketIds.size > 0
    ) return null;
    const ticket = {
      id: this.nextId++,
      epoch: this.epoch,
      kind: "all" as const,
    };
    this.allTicketId = ticket.id;
    return ticket;
  }

  beginDraft(service: ModelRole): ModelTestTicket | null {
    if (
      this.allTicketId !== null
      || this.roleTicketIds.has(service)
      || this.draftTicketIds.has(service)
    ) return null;
    const revision = this.draftRevisions.get(service) ?? 0;
    this.draftRevisions.set(service, revision);
    const ticket = {
      id: this.nextId++,
      epoch: this.epoch,
      kind: "draft" as const,
      service,
      draftRevision: revision,
    };
    this.draftTicketIds.set(service, ticket.id);
    return ticket;
  }

  invalidateDraft(service: ModelRole): void {
    this.draftRevisions.set(service, (this.draftRevisions.get(service) ?? 0) + 1);
  }

  invalidateDrafts(): void {
    for (const service of this.draftRevisions.keys()) this.invalidateDraft(service);
  }

  invalidateConfiguration(): void {
    this.epoch += 1;
  }

  isCurrent(ticket: ModelTestTicket): boolean {
    if (ticket.epoch !== this.epoch) return false;
    if (ticket.kind === "all") return this.allTicketId === ticket.id;
    if (!ticket.service) return false;
    if (ticket.kind === "draft") {
      return this.draftTicketIds.get(ticket.service as ModelRole) === ticket.id
        && (this.draftRevisions.get(ticket.service as ModelRole) ?? 0) === ticket.draftRevision;
    }
    return this.roleTicketIds.get(ticket.service) === ticket.id;
  }

  finish(ticket: ModelTestTicket): void {
    if (ticket.kind === "all") {
      if (this.allTicketId === ticket.id) this.allTicketId = null;
      return;
    }
    if (ticket.kind === "draft") {
      const service = ticket.service as ModelRole | undefined;
      if (service && this.draftTicketIds.get(service) === ticket.id) {
        this.draftTicketIds.delete(service);
      }
      return;
    }
    if (ticket.service && this.roleTicketIds.get(ticket.service) === ticket.id) {
      this.roleTicketIds.delete(ticket.service);
    }
  }

  hasInFlight(): boolean {
    return this.allTicketId !== null
      || this.roleTicketIds.size > 0
      || this.draftTicketIds.size > 0;
  }

  snapshot(): ModelTestActivity {
    return {
      roles: Object.fromEntries(
        [...this.roleTicketIds.keys()].map((service) => [service, true]),
      ) as Partial<Record<StatusModelRole, boolean>>,
      drafts: Object.fromEntries(
        [...this.draftTicketIds.keys()].map((service) => [service, true]),
      ) as Partial<Record<ModelRole, boolean>>,
      all: this.allTicketId !== null,
    };
  }
}
