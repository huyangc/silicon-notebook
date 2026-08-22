"use client";

import { useEffect, useRef } from "react";

import {
  modelServiceDisplayName,
  type ModelServiceStatusItem,
  type ModelServicesStatus,
} from "./model-services.ts";
import { SupportIdCopy } from "./support-id-copy";
import { useFloatingWindow } from "./use-floating-window";
import { label, MODEL_SERVICE_STATUS_ERROR } from "./vocabulary";

function checkedTime(item: ModelServiceStatusItem): string {
  if (!item.checked_at) return "尚未检查";
  const timestamp = new Date(item.checked_at);
  if (!Number.isFinite(timestamp.getTime())) return "尚未检查";
  const prefix = item.trigger === "observed_failure" ? "最近失败" : "上次检查";
  return `${prefix} ${timestamp.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  })}`;
}

function waitText(milliseconds: number): string {
  return milliseconds >= 1000
    ? `${(milliseconds / 1000).toFixed(milliseconds % 1000 === 0 ? 0 : 1)}s`
    : `${milliseconds}ms`;
}

function statusPresentation(item: ModelServiceStatusItem): {
  text: string;
  tone: "ok" | "warn" | "bad" | "muted";
} {
  if (item.status === "ok") return { text: "正常", tone: "ok" };
  if (item.status === "busy") return { text: "繁忙", tone: "warn" };
  if (item.status === "circuit_open") return { text: "熔断中", tone: "bad" };
  if (item.status === "half_open") return { text: "恢复探测中", tone: "warn" };
  if (item.status === "error") return { text: "异常", tone: "bad" };
  return { text: "待检查", tone: "muted" };
}

function ModelServiceCard({
  item,
  highlighted,
  isAdmin,
  testing,
  testDisabled,
  onTest,
  register,
}: {
  item: ModelServiceStatusItem;
  highlighted: boolean;
  isAdmin: boolean;
  testing: boolean;
  testDisabled: boolean;
  onTest: (serviceId: string) => void;
  register: (element: HTMLElement | null) => void;
}) {
  const presentation = statusPresentation(item);
  const name = modelServiceDisplayName(item);
  const failure = item.code
    ? label(MODEL_SERVICE_STATUS_ERROR, item.code, "服务调用未通过")
    : "";
  return (
    <article
      ref={register}
      role="group"
      aria-label={name}
      aria-current={highlighted ? "true" : undefined}
      tabIndex={highlighted ? -1 : undefined}
      className={`model-service-status-card tone-${presentation.tone}${highlighted ? " is-highlighted" : ""}`}
    >
      <header className="model-service-status-card-header">
        <div>
          <h3>{name}</h3>
          {item.model && <p>{item.model}</p>}
        </div>
        <span className={`model-service-state tone-${presentation.tone}`}>{presentation.text}</span>
      </header>

      <div className="model-service-metrics">
        <div><small>并发</small><strong>{item.active} / {item.maximum}</strong></div>
        <div><small>队列</small><strong>排队 {item.queued}</strong></div>
        <div><small>最久等待</small><strong>{item.queued > 0 ? waitText(item.oldest_wait_ms) : "—"}</strong></div>
        <div><small>延迟</small><strong>{item.checked_at ? `${item.latency_ms}ms` : "—"}</strong></div>
      </div>

      <div className="model-service-workloads">
        <small>使用位置</small>
        <div>
          {item.workloads.length > 0
            ? item.workloads.map((workload) => <span key={workload.id}>{workload.label || "模型任务"}</span>)
            : <span>暂无任务</span>}
        </div>
      </div>

      <div className="model-service-status-meta">
        <time dateTime={item.checked_at || undefined}>{checkedTime(item)}</time>
        {failure && <span>失败类别：{failure}</span>}
        <SupportIdCopy supportId={item.support_id} className="model-service-support-id" />
      </div>

      {isAdmin && (
        <button
          type="button"
          className="sort-button model-service-test-one"
          disabled={testDisabled}
          onClick={() => onTest(item.service_id)}
        >
          {testing ? "测试中…" : "测试服务"}
        </button>
      )}
    </article>
  );
}


export function ModelServiceSummaryButton({
  text, tone, title, onOpen,
}: {
  text: string;
  tone: "ok" | "warn" | "bad" | "connecting";
  title: string;
  onOpen: () => void;
}) {
  return (
    <button type="button" className={`status status-${tone}`} title={title} onClick={onOpen}>
      <span className={`status-dot ${tone === "ok" ? "" : tone}`} aria-hidden="true" />
      <span>{text}</span>
    </button>
  );
}


export function ModelServicePanel({
  status,
  highlightedServiceId,
  isAdmin,
  onTestOne,
  onTestAll,
  onClose,
  testingServiceIds,
  allTesting,
  interactive = true,
  zIndex,
}: {
  status: ModelServicesStatus | null;
  highlightedServiceId: string | null;
  isAdmin: boolean;
  onTestOne: (serviceId: string) => Promise<void>;
  onTestAll: () => Promise<void>;
  onClose: (reason?: "button" | "backdrop" | "escape") => void;
  testingServiceIds: Record<string, boolean>;
  allTesting: boolean;
  interactive?: boolean;
  zIndex?: number;
}) {
  const serviceRefs = useRef<Record<string, HTMLElement | null>>({});
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const floating = useFloatingWindow({ storageKey: "modelService.window", resizable: false });
  const anyTesting = Object.values(testingServiceIds).some(Boolean);

  // Focus the highlighted card (or the close button) when the dialog opens, when
  // the highlight target changes, or once status first loads (so the target card
  // exists to receive focus). Gate on `statusLoaded` (a boolean), NOT the whole
  // `status` object: while the panel is open it re-polls every 2s, and depending
  // on `status` would re-steal keyboard focus / reset scroll on every poll.
  const statusLoaded = status !== null;
  useEffect(() => {
    if (!interactive) return;
    const highlighted = highlightedServiceId ? serviceRefs.current[highlightedServiceId] : null;
    if (highlighted) {
      highlighted.focus();
      highlighted.scrollIntoView?.({ block: "center", behavior: "smooth" });
    } else {
      closeButtonRef.current?.focus();
    }
  }, [highlightedServiceId, statusLoaded, interactive]);

  useEffect(() => {
    if (!interactive) return;
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose("escape");
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ));
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !focusable.includes(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, interactive]);

  async function runOne(serviceId: string) {
    if (allTesting || testingServiceIds[serviceId]) return;
    try { await onTestOne(serviceId); } catch { /* page owns the user-facing error */ }
  }

  async function runAll() {
    if (allTesting || anyTesting) return;
    try { await onTestAll(); } catch { /* page owns the user-facing error */ }
  }

  return (
    <section
      ref={dialogRef}
      className="utility-modal model-service-modal"
      role="dialog"
      tabIndex={-1}
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      aria-labelledby="model-service-title"
      style={{ zIndex }}
      onClick={(event) => { if (event.currentTarget === event.target) onClose("backdrop"); }}
    >
      <div ref={floating.cardRef} className="utility-modal-card model-service-card" style={floating.style}>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2 id="model-service-title">模型服务状态</h2>
            <p>模型由系统统一管理；此处展示实时并发和最近检查结果</p>
          </div>
          <div className="model-service-header-actions">
            {isAdmin && (
              <button
                type="button"
                className="sort-button model-service-test-all"
                disabled={allTesting || anyTesting}
                onClick={runAll}
              >
                {allTesting ? "测试全部中…" : "测试全部"}
              </button>
            )}
            <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭模型服务" onClick={() => onClose("button")}>×</button>
          </div>
        </div>
        <div className="source-detail-body model-service-body">
          {!status ? (
            <p className="tool-hint">正在读取模型状态…</p>
          ) : status.services.length === 0 ? (
            <p className="tool-hint">暂无模型服务</p>
          ) : (
            <div className="model-service-status-list">
              {status.services.map((item) => (
                <ModelServiceCard
                  key={item.service_id}
                  item={item}
                  highlighted={highlightedServiceId === item.service_id}
                  isAdmin={isAdmin}
                  testing={allTesting || Boolean(testingServiceIds[item.service_id])}
                  testDisabled={allTesting || anyTesting}
                  onTest={(serviceId) => { void runOne(serviceId); }}
                  register={(element) => { serviceRefs.current[item.service_id] = element; }}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
