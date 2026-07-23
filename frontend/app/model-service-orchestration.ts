import {
  mergeModelServiceStatus,
  modelServiceDisplayName,
  summarizeModelServices,
  type ModelServiceStatusItem,
  type ModelServicesStatus,
} from "./model-services.ts";


export type ModelServiceSummaryView = {
  text: string;
  tone: "ok" | "warn" | "bad" | "connecting";
  title: string;
};

export type ModelServiceStatusState = {
  status: ModelServicesStatus | null;
  unavailable: boolean;
};


export function acceptModelServiceStatusSnapshot(
  status: ModelServicesStatus,
): ModelServiceStatusState {
  return { status, unavailable: false };
}

export function applyModelServiceTestResult(
  current: ModelServiceStatusState,
  replacement: ModelServiceStatusItem | ModelServicesStatus,
  coverage: "single" | "all",
): ModelServiceStatusState {
  return {
    status: mergeModelServiceStatus(current.status, replacement),
    unavailable: coverage === "all" ? false : current.unavailable,
  };
}


const IDLE_STATUS_TEXT = new Set([
  "", "连接中", "服务正常", "服务正常 · 模型服务不可用", "服务连接异常",
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
    return { text: operationalText, tone: operationalTone(operationalText), title: operationalText };
  }
  if (modelStatusUnavailable) {
    return {
      text: "API 正常 · 模型状态未知",
      tone: "warn",
      title: "API 正常；模型状态暂时不可用，查看模型状态以了解详情",
    };
  }
  if (!modelStatus) {
    return {
      text: "API 正常 · 正在读取模型状态",
      tone: "connecting",
      title: "正在读取模型状态，不会自动测试模型",
    };
  }
  const summary = summarizeModelServices(modelStatus.services);
  const pending = modelStatus.services.filter((item) => item.status === "untested");
  return {
    text: summary.text,
    tone: summary.tone,
    title: summary.abnormal.length > 0
      ? `模型异常：${summary.abnormal.map((item) => modelServiceDisplayName(item)).join("、")}`
      : pending.length > 0
        ? `待检查：${pending.map((item) => modelServiceDisplayName(item)).join("、")}`
        : "API 和模型服务状态正常",
  };
}


export type ModelTestTicket = {
  id: number;
  kind: "one" | "all";
  serviceId?: string;
};

export type ModelTestActivity = {
  services: Record<string, boolean>;
  all: boolean;
};

export class ModelTestCoordinator {
  private nextId = 1;
  private allTicketId: number | null = null;
  private serviceTicketIds = new Map<string, number>();

  beginOne(serviceId: string): ModelTestTicket | null {
    if (this.allTicketId !== null || this.serviceTicketIds.has(serviceId)) return null;
    const ticket = { id: this.nextId++, kind: "one" as const, serviceId };
    this.serviceTicketIds.set(serviceId, ticket.id);
    return ticket;
  }

  beginAll(): ModelTestTicket | null {
    if (this.allTicketId !== null || this.serviceTicketIds.size > 0) return null;
    const ticket = { id: this.nextId++, kind: "all" as const };
    this.allTicketId = ticket.id;
    return ticket;
  }

  isCurrent(ticket: ModelTestTicket): boolean {
    if (ticket.kind === "all") return this.allTicketId === ticket.id;
    return Boolean(ticket.serviceId && this.serviceTicketIds.get(ticket.serviceId) === ticket.id);
  }

  finish(ticket: ModelTestTicket): void {
    if (ticket.kind === "all") {
      if (this.allTicketId === ticket.id) this.allTicketId = null;
      return;
    }
    if (ticket.serviceId && this.serviceTicketIds.get(ticket.serviceId) === ticket.id) {
      this.serviceTicketIds.delete(ticket.serviceId);
    }
  }

  hasInFlight(): boolean {
    return this.allTicketId !== null || this.serviceTicketIds.size > 0;
  }

  snapshot(): ModelTestActivity {
    return {
      services: Object.fromEntries([...this.serviceTicketIds.keys()].map((id) => [id, true])),
      all: this.allTicketId !== null,
    };
  }
}
