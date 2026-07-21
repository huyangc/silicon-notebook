"use client";

import { useEffect, useMemo, useRef, useState } from "react";

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


function checkedTime(iso: string): string {
  if (!iso) return "尚未测试";
  const timestamp = new Date(iso);
  if (!Number.isFinite(timestamp.getTime())) return "尚未测试";
  return `上次测试 ${timestamp.toLocaleString("zh-CN", {
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
        <time dateTime={status?.checked_at || undefined}>{checkedTime(status?.checked_at || "")}</time>
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
  saving = false,
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
  saving?: boolean;
}) {
  const roleRefs = useRef<Partial<Record<StatusModelRole, HTMLFieldSetElement | null>>>({});
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const testingRolesRef = useRef(new Set<StatusModelRole>());
  const allTestingRef = useRef(false);
  const [testingRoles, setTestingRoles] = useState<Partial<Record<StatusModelRole, boolean>>>({});
  const [allTesting, setAllTesting] = useState(false);
  const statusByRole = useMemo(
    () => new Map((status?.services ?? []).map((item) => [item.service, item])),
    [status],
  );
  const anyRoleTesting = Object.values(testingRoles).some(Boolean);

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
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, saving]);

  async function runCurrentTest(role: StatusModelRole) {
    if (allTestingRef.current || testingRolesRef.current.has(role)) return;
    testingRolesRef.current.add(role);
    setTestingRoles((current) => ({ ...current, [role]: true }));
    try {
      await onTestCurrent(role);
    } finally {
      testingRolesRef.current.delete(role);
      setTestingRoles((current) => ({ ...current, [role]: false }));
    }
  }

  async function runAllTests() {
    if (allTestingRef.current || testingRolesRef.current.size > 0) return;
    allTestingRef.current = true;
    setAllTesting(true);
    try {
      await onTestAll();
    } catch {
      // The orchestrator reports the humanized error; this panel only unlocks.
    } finally {
      allTestingRef.current = false;
      setAllTesting(false);
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
              disabled={allTesting || anyRoleTesting || saving}
              onClick={runAllTests}
            >
              {allTesting ? "正在测试全部模型…" : "测试当前使用的全部模型"}
            </button>
          </div>
          {MODEL_ROLES.map((role) => {
            const form = forms[role];
            const roleTesting = allTesting || saving || Boolean(testingRoles[role]);
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
                      placeholder={BASE_URL_PLACEHOLDERS[role]}
                      onChange={(event) => onFormChange(role, { ...form, base_url: event.target.value })}
                    />
                  </label>
                  <label>Model
                    <input
                      value={form.model}
                      onChange={(event) => onFormChange(role, { ...form, model: event.target.value })}
                    />
                  </label>
                  <label>API Key
                    <input
                      type="password"
                      placeholder="未改动则保留原 key"
                      value={form.api_key}
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
                    disabled={allTesting || saving || draftTestResults[role] === "测试中…"}
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
              disabled={saving || allTesting || anyRoleTesting}
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
