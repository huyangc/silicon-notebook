"use client";

import { useEffect, useMemo, useRef } from "react";

import {
  MODEL_ROLES,
  MODEL_ROLE_LABELS,
  type ModelRole,
  type ModelServicesStatus,
  type ServiceForm,
  type StatusModelRole,
} from "./model-settings.ts";
import { label, MODEL_SERVICE_STATUS_ERROR } from "./vocabulary";


const BASE_URL_PLACEHOLDERS: Record<ModelRole, string> = {
  llm: "https://api.openai.com/v1",
  reasoning_llm: "https://api.deepseek.com/v1",
  rewrite_llm: "https://api.openai.com/v1",
  kg_llm: "https://api.openai.com/v1",
  rerank: "https://dashscope.aliyuncs.com/api/v1",
};


function checkedTime(
  iso: string,
  trigger: ModelServicesStatus["services"][number]["trigger"] | "",
): string {
  if (!iso) return "尚未测试";
  const timestamp = new Date(iso);
  if (!Number.isFinite(timestamp.getTime())) return "尚未测试";
  const prefix = trigger === "observed_failure" ? "最近失败" : "上次测试";
  return `${prefix} ${timestamp.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })}`;
}


function statusPresentation(item: ModelServicesStatus["services"][number] | undefined): {
  text: string;
  tone: "ok" | "warn" | "bad" | "muted";
} {
  if (!item) return { text: "状态未知", tone: "muted" };
  if (!item.configured || item.status === "unconfigured") return { text: "未配置", tone: "muted" };
  if (item.status === "ok") return { text: `正常 ${item.latency_ms}ms`, tone: "ok" };
  if (item.status === "error") {
    return {
      text: `异常 · ${label(MODEL_SERVICE_STATUS_ERROR, item.code, "连接未通过")}`,
      tone: "bad",
    };
  }
  return { text: "待测试", tone: "warn" };
}


function EffectiveStatusRow({
  role,
  status,
  disabled,
  onTest,
}: {
  role: StatusModelRole;
  status: ModelServicesStatus["services"][number] | undefined;
  disabled: boolean;
  onTest: (role: StatusModelRole) => void;
}) {
  const presentation = statusPresentation(status);
  return (
    <div className={`model-service-status-row tone-${presentation.tone}`}>
      <div>
        <small>当前使用</small>
        <strong>{status?.model || "尚未配置"}</strong>
        <span>{status?.source === "user" ? "个人设置" : status?.source === "system" ? "系统设置" : "未配置"}</span>
      </div>
      <div className="model-service-status-result">
        <strong>{presentation.text}</strong>
        <time dateTime={status?.checked_at || undefined}>
          {checkedTime(status?.checked_at || "", status?.trigger || "")}
        </time>
      </div>
      <button type="button" disabled={disabled} onClick={() => onTest(role)}>
        {disabled ? "测试中…" : "测试当前使用"}
      </button>
    </div>
  );
}


export function ModelServiceSummaryButton({
  text,
  tone,
  title,
  onOpen,
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
  forms,
  status,
  highlightedRole,
  draftTestResults,
  onFormChange,
  onTestDraft,
  onTestCurrent,
  onTestAll,
  onClose,
  onSave,
  testingRoles,
  draftTestingRoles,
  allTesting,
  saving = false,
  returnFocusTo = null,
}: {
  forms: Record<ModelRole, ServiceForm>;
  status: ModelServicesStatus | null;
  highlightedRole: StatusModelRole | null;
  draftTestResults: Partial<Record<ModelRole, string>>;
  onFormChange: (role: ModelRole, form: ServiceForm) => void;
  onTestDraft: (role: ModelRole) => void;
  onTestCurrent: (role: StatusModelRole) => Promise<void>;
  onTestAll: () => Promise<void>;
  onClose: () => void;
  onSave: () => void;
  testingRoles: Partial<Record<StatusModelRole, boolean>>;
  draftTestingRoles: Partial<Record<ModelRole, boolean>>;
  allTesting: boolean;
  saving?: boolean;
  returnFocusTo?: HTMLElement | null;
}) {
  const roleRefs = useRef<Partial<Record<StatusModelRole, HTMLFieldSetElement | null>>>({});
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const statusByRole = useMemo(
    () => new Map((status?.services ?? []).map((item) => [item.service, item])),
    [status],
  );
  const anyRoleTesting = Object.values(testingRoles).some(Boolean);
  const anyDraftTesting = Object.values(draftTestingRoles).some(Boolean);

  useEffect(() => {
    if (highlightedRole) {
      const fieldset = roleRefs.current[highlightedRole];
      if (!fieldset) return;
      fieldset.focus();
      fieldset.scrollIntoView?.({ block: "center", behavior: "smooth" });
      return;
    }
    closeButtonRef.current?.focus();
  }, [highlightedRole]);

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !saving) onClose();
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
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
  }, [onClose, saving]);

  useEffect(() => () => {
    returnFocusTo?.focus();
  }, [returnFocusTo]);

  async function runCurrentTest(role: StatusModelRole) {
    if (allTesting || testingRoles[role]) return;
    try {
      await onTestCurrent(role);
    } catch {
      // The page-level orchestrator reports the humanized error and owns the lock.
    }
  }

  async function runAllTests() {
    if (allTesting || anyRoleTesting) return;
    try {
      await onTestAll();
    } catch {
      // The page-level orchestrator reports the humanized error and owns the lock.
    }
  }

  function fieldsetProps(role: StatusModelRole) {
    const highlighted = highlightedRole === role;
    return {
      ref: (element: HTMLFieldSetElement | null) => { roleRefs.current[role] = element; },
      className: `edit-form model-service-fieldset${highlighted ? " is-highlighted" : ""}`,
      tabIndex: highlighted ? -1 : undefined,
      "aria-current": highlighted ? ("true" as const) : undefined,
    };
  }

  return (
    <section
      className="utility-modal model-service-modal"
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="model-service-title"
      onClick={(event) => { if (event.currentTarget === event.target && !saving) onClose(); }}
    >
      <div className="utility-modal-card model-service-card">
        <div className="source-modal-header">
          <div>
            <h2 id="model-service-title">模型服务</h2>
            <p>编辑个人设置，或查看当前实际使用的模型；API Key 只写不回显</p>
          </div>
          <button
            type="button"
            ref={closeButtonRef}
            className="icon-button"
            aria-label="关闭模型服务"
            disabled={saving}
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <div className="source-detail-body model-service-body">
          <p className="tool-hint model-service-hint">
            Base URL 只需填到服务根地址（通常以 <code>/v1</code> 结尾），系统会自动拼接接口路径。
            下方“当前使用”来自已保存设置；修改表单后可先测试未保存设置，再保存。
          </p>
          <div className="model-service-toolbar">
            <button
              type="button"
              className="sort-button model-service-test-all"
              disabled={allTesting || anyRoleTesting || anyDraftTesting || saving}
              onClick={runAllTests}
            >
              {allTesting ? "正在测试全部模型…" : "测试当前使用的全部模型"}
            </button>
          </div>
          {MODEL_ROLES.map((role) => {
            const form = forms[role];
            const roleTesting = allTesting
              || saving
              || Boolean(testingRoles[role])
              || Boolean(draftTestingRoles[role]);
            return (
              <fieldset key={role} {...fieldsetProps(role)}>
                <legend>{MODEL_ROLE_LABELS[role]}</legend>
                <EffectiveStatusRow
                  role={role}
                  status={statusByRole.get(role)}
                  disabled={roleTesting}
                  onTest={runCurrentTest}
                />
                <div className="model-service-draft-grid">
                  <label>Base URL
                    <input
                      value={form.base_url}
                      disabled={saving}
                      placeholder={BASE_URL_PLACEHOLDERS[role]}
                      onChange={(event) => onFormChange(role, { ...form, base_url: event.target.value })}
                    />
                  </label>
                  <label>Model
                    <input
                      value={form.model}
                      disabled={saving}
                      onChange={(event) => onFormChange(role, { ...form, model: event.target.value })}
                    />
                  </label>
                  <label>API Key
                    <input
                      type="password"
                      placeholder="未改动则保留原 key"
                      value={form.api_key}
                      disabled={saving}
                      onChange={(event) => onFormChange(role, {
                        ...form,
                        api_key: event.target.value,
                        keyDirty: true,
                      })}
                    />
                  </label>
                </div>
                <div className="model-service-draft-actions">
                  <button
                    type="button"
                    className="sort-button"
                    disabled={roleTesting}
                    onClick={() => onTestDraft(role)}
                  >
                    测试未保存设置
                  </button>
                  <span role="status">{draftTestResults[role] || ""}</span>
                </div>
              </fieldset>
            );
          })}
          <fieldset {...fieldsetProps("embedding")}>
            <legend>{MODEL_ROLE_LABELS.embedding}</legend>
            <EffectiveStatusRow
              role="embedding"
              status={statusByRole.get("embedding")}
              disabled={allTesting || saving || Boolean(testingRoles.embedding)}
              onTest={runCurrentTest}
            />
            <p className="model-service-admin-guidance">由管理员维护系统配置</p>
          </fieldset>
          <div className="modal-actions model-service-footer">
            <button type="button" className="sort-button" disabled={saving} onClick={onClose}>取消</button>
            <button
              type="button"
              className="new-pill"
              disabled={saving || allTesting || anyRoleTesting || anyDraftTesting}
              onClick={onSave}
            >
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
