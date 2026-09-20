"use client";

import { useEffect, useRef, useState } from "react";
import { fetchMe } from "../../auth";
import { PageHeader } from "../../components/PageHeader";
import { toUserMessage } from "../../errors";
import {
  fetchAuthAccounts, fetchAuthAudit, fetchAuthMigration, fetchAuthPolicy, issueAuthGrant,
  prepareAuthProviderMaintenance, updateAuthAccountStatus, updateAuthPolicy,
  type AuthAccount, type AuthAccountPage, type AuthAuditPage, type AuthGrant, type AuthMigrationPreflight, type AuthPolicy,
} from "./api";

const MODES: Array<{ value: AuthPolicy["mode"]; label: string }> = [
  { value: "local", label: "仅本地登录" }, { value: "dual", label: "本地与统一登录并存" },
  { value: "binding_required", label: "仅允许本地账号完成关联" }, { value: "sso_only", label: "仅统一登录" },
  { value: "retired", label: "退役本地凭据" },
];

const AUDIT_PAGE_SIZE = 100;
const AUDIT_ACTION_LABEL: Readonly<Record<string, string>> = {
  "grant_issued:enroll": "已签发新建账号凭证",
  "grant_issued:recover": "已签发恢复账号凭证",
  "grant_issued:replace": "已签发更换统一账号凭证",
  "grant_started:enroll": "已开始新建账号验证",
  "grant_started:recover": "已开始恢复账号验证",
  "grant_started:replace": "已开始更换统一账号验证",
  "grant_completed:enroll": "已完成新建账号",
  "grant_completed:recover": "已完成恢复账号",
  "grant_completed:replace": "已完成更换统一账号",
  identity_bound: "已关联统一身份",
  identity_renamed: "已更新统一身份名称",
  "account_status:active": "已启用账号",
  "account_status:disabled": "已停用账号",
};

function auditActionLabel(action: string): string { return AUDIT_ACTION_LABEL[action] ?? "其他账号操作"; }

const ACTION_FEEDBACK_MS = 5000;
type ActionResult = { text: string; failed?: boolean; accountId?: string };

function useActionResult() {
  const [result, setResult] = useState<ActionResult | null>(null);
  useEffect(() => {
    if (!result) return;
    const timer = window.setTimeout(() => setResult(null), ACTION_FEEDBACK_MS);
    return () => window.clearTimeout(timer);
  }, [result]);
  return [result, setResult] as const;
}

function ActionFeedback({ result }: { result: ActionResult | null }) {
  return result && <p className={result.failed ? "admin-auth-error" : "admin-auth-notice"} role={result.failed ? "alert" : "status"}>{result.text}</p>;
}

export default function AdminAuthPage() {
  const [policy, setPolicy] = useState<AuthPolicy | null>(null);
  const [preflight, setPreflight] = useState<AuthMigrationPreflight | null>(null);
  const [accounts, setAccounts] = useState<AuthAccountPage | null>(null);
  const [accountsBusy, setAccountsBusy] = useState(false);
  const [accountsError, setAccountsError] = useState("");
  const accountReadRef = useRef(0);
  const accountReadInFlight = useRef(false);
  const failedAccountOffset = useRef(0);
  const [audit, setAudit] = useState<AuthAuditPage | null>(null);
  const [auditError, setAuditError] = useState("");
  const [auditBusy, setAuditBusy] = useState(false);
  const [mode, setMode] = useState<AuthPolicy["mode"]>("local");
  const [retiredConfirmation, setRetiredConfirmation] = useState("");
  const [allowRollback, setAllowRollback] = useState(false);
  const [maintenanceGeneration, setMaintenanceGeneration] = useState("");
  const [grantPurpose, setGrantPurpose] = useState<AuthGrant["purpose"]>("enroll");
  const [grantSubject, setGrantSubject] = useState("");
  const [grantTarget, setGrantTarget] = useState("");
  const [selectedGrantAccount, setSelectedGrantAccount] = useState<AuthAccount | null>(null);
  const [grant, setGrant] = useState<AuthGrant | null>(null);
  const [policyResult, setPolicyResult] = useActionResult();
  const [maintenanceResult, setMaintenanceResult] = useActionResult();
  const [grantResult, setGrantResult] = useActionResult();
  const [accountResult, setAccountResult] = useActionResult();
  const [selectionResult, setSelectionResult] = useActionResult();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mutationInFlight = useRef(false);

  function beginMutation() {
    if (mutationInFlight.current) return false;
    mutationInFlight.current = true;
    setBusy(true);
    return true;
  }

  function finishMutation() { mutationInFlight.current = false; setBusy(false); }

  async function reload(offset = accounts?.offset ?? 0) {
    // A post-mutation refresh supersedes any older pagination read.
    const read = ++accountReadRef.current;
    accountReadInFlight.current = true;
    setAccountsBusy(true); setAccountsError("");
    try {
      const [nextPolicy, nextPreflight, nextAccounts] = await Promise.all([
        fetchAuthPolicy(), fetchAuthMigration(), fetchAuthAccounts(offset),
      ]);
      if (read !== accountReadRef.current) return;
      setPolicy(nextPolicy); setPreflight(nextPreflight); setAccounts(nextAccounts); setMode(nextPolicy.mode);
      setMaintenanceGeneration(nextPolicy.config_generation);
    } finally {
      if (read === accountReadRef.current) {
        accountReadInFlight.current = false;
        setAccountsBusy(false);
      }
    }
  }

  async function loadAccountsPage(offset: number) {
    // The ref closes the gap before React paints the disabled controls.
    if (busy || accountReadInFlight.current) return;
    const read = ++accountReadRef.current;
    accountReadInFlight.current = true;
    failedAccountOffset.current = offset;
    setAccountsBusy(true); setAccountsError("");
    try {
      const next = await fetchAuthAccounts(offset);
      if (read === accountReadRef.current) setAccounts(next);
    } catch (cause) {
      if (read === accountReadRef.current) setAccountsError(toUserMessage(cause, "加载账号列表失败，请重试。"));
    } finally {
      if (read === accountReadRef.current) {
        accountReadInFlight.current = false;
        setAccountsBusy(false);
      }
    }
  }

  async function reloadAudit(offset = audit?.offset ?? 0) {
    setAuditBusy(true); setAuditError("");
    try { setAudit(await fetchAuthAudit(offset, AUDIT_PAGE_SIZE)); }
    catch (cause) { setAuditError(toUserMessage(cause, "加载认证审计记录失败，请稍后重试。")); }
    finally { setAuditBusy(false); }
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await fetchMe();
        if (cancelled) return;
        if (me.role !== "admin") { setError("无权限：仅管理员可管理认证迁移。"); return; }
        await reload(0);
        if (cancelled) return;
        await reloadAudit(0);
      } catch (cause) { if (!cancelled) setError(toUserMessage(cause, "加载认证迁移状态失败，请稍后重试。")); }
    })();
    return () => { cancelled = true; accountReadRef.current += 1; accountReadInFlight.current = false; };
  }, []);

  async function changePolicy() {
    if (!policy) return;
    if (mode === "retired" && retiredConfirmation !== "退役本地凭据") {
      setPolicyResult({ failed: true, text: "请输入“退役本地凭据”后才能继续。此操作会关闭本地凭据入口。" });
      return;
    }
    if (!beginMutation()) return;
    setPolicyResult(null);
    try {
      const updated = await updateAuthPolicy(mode, policy.revision, allowRollback);
      setPolicy(updated); setMode(updated.mode); setRetiredConfirmation("");
      setPolicyResult({ text: `认证策略已更新为“${MODES.find((item) => item.value === updated.mode)?.label}”。` });
      try { await reload(accounts?.offset ?? 0); }
      catch { setPolicyResult({ failed: true, text: "认证策略已更新，但状态刷新失败，请刷新页面。" }); }
    } catch (cause) { setPolicyResult({ failed: true, text: toUserMessage(cause, "认证策略更新失败，请刷新后重试。") }); }
    finally { finishMutation(); }
  }

  async function prepareMaintenance() {
    if (!policy || !maintenanceGeneration.trim() || /\s/.test(maintenanceGeneration)) { setMaintenanceResult({ failed: true, text: "配置代次不能为空且不能包含空白字符。" }); return; }
    if (!beginMutation()) return;
    setMaintenanceResult(null);
    try {
      const updated = await prepareAuthProviderMaintenance(policy.revision, maintenanceGeneration);
      setPolicy(updated); setMaintenanceResult({ text: "已进入认证提供方维护准备状态；重启并加载该代次后再恢复新的统一登录。" });
    } catch (cause) { setMaintenanceResult({ failed: true, text: toUserMessage(cause, "维护准备失败，请刷新后重试。") }); }
    finally { finishMutation(); }
  }

  async function createGrant() {
    if (!grantSubject.trim()) { setGrantResult({ failed: true, text: "请填写迁移凭证的使用人标识。" }); return; }
    if (grantPurpose === "replace" && !grantTarget.trim()) { setGrantResult({ failed: true, text: "更换统一账号必须指定原账号 ID。" }); return; }
    if (!beginMutation()) return;
    setGrantResult(null); setGrant(null);
    try {
      setGrant(await issueAuthGrant(grantPurpose, grantSubject.trim(), grantPurpose === "enroll" ? undefined : grantTarget.trim() || undefined));
      setGrantResult({ text: "已签发迁移凭证，请保存下方凭证并通过受控渠道交给使用人。" });
    }
    catch (cause) { setGrantResult({ failed: true, text: toUserMessage(cause, "签发迁移凭证失败，请稍后重试。") }); }
    finally { finishMutation(); }
  }

  function selectGrantAccount(account: AuthAccount) {
    if (mutationInFlight.current) return;
    setGrantTarget(account.id);
    setSelectedGrantAccount(account);
    if (grantPurpose === "enroll") setGrantPurpose(account.identity_status === "active" ? "replace" : "recover");
    setGrantResult(null);
    setSelectionResult({ accountId: account.id, text: "已选择为原账号，请在上方凭证表单核对并确认签发。" });
  }

  function changeGrantPurpose(purpose: AuthGrant["purpose"]) {
    setGrantPurpose(purpose);
    setGrantResult(null);
    if (purpose === "enroll") {
      setGrantTarget("");
      setSelectedGrantAccount(null);
      setSelectionResult(null);
    }
  }

  async function setAccountStatus(account: AuthAccount) {
    if (!beginMutation()) return;
    setAccountResult(null);
    try {
      await updateAuthAccountStatus(account.id, account.status === "active" ? "disabled" : "active");
      const text = `已${account.status === "active" ? "停用" : "启用"}该账号。`;
      setAccountResult({ accountId: account.id, text });
      try { await reload(accounts?.offset ?? 0); }
      catch { setAccountResult({ accountId: account.id, failed: true, text: `${text}但列表刷新失败，请刷新页面。` }); }
    } catch (cause) { setAccountResult({ accountId: account.id, failed: true, text: toUserMessage(cause, "账号状态更新失败，请稍后重试。") }); }
    finally { finishMutation(); }
  }

  if (!policy || !preflight || !accounts) return <main className="admin-auth-page"><PageHeader title="认证迁移" />{error ? <p className="admin-auth-error">{error}</p> : <p>加载中…</p>}</main>;
  const priorOffset = Math.max(0, accounts.offset - accounts.limit);
  const nextOffset = accounts.offset + accounts.limit;
  return <main className="admin-auth-page">
    <PageHeader title="认证迁移" />
    <p className="admin-auth-intro">此页只显示部署认证策略和迁移状态。切换、维护准备、账号停用和凭证签发都需要明确点击，不会自动执行。</p>
    {error && <p className="admin-auth-error" role="alert">{error}</p>}
    <section className="admin-auth-card"><h2>切换预检</h2><dl className="admin-auth-metrics">
      <div><dt>活跃用户</dt><dd>{preflight.active_users}</dd></div><div><dt>未完成关联</dt><dd>{preflight.unready_users}</dd></div><div><dt>已就绪管理员</dt><dd>{preflight.ready_admins}</dd></div><div><dt>可切换</dt><dd>{preflight.ready ? "是" : "否"}</dd></div>
    </dl></section>
    <section className="admin-auth-card"><h2>认证策略</h2><p>当前模式：{MODES.find((item) => item.value === policy.mode)?.label}。策略版本 {policy.revision}，提供方配置代次 {policy.config_generation}。</p>
      <label>目标模式<select value={mode} disabled={busy} onChange={(event) => setMode(event.target.value as AuthPolicy["mode"])}>{MODES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
      {mode !== policy.mode && <label><input type="checkbox" checked={allowRollback} disabled={busy} onChange={(event) => setAllowRollback(event.target.checked)} /> 这是受控回退；允许后端按部署策略执行回退</label>}
      {mode === "retired" && <label>确认退役<input value={retiredConfirmation} disabled={busy} onChange={(event) => setRetiredConfirmation(event.target.value)} placeholder="输入：退役本地凭据" /></label>}
      <button type="button" disabled={busy || mode === policy.mode} onClick={() => { void changePolicy(); }}>{busy ? "处理中…" : "更新策略"}</button>
      <ActionFeedback result={policyResult} />
    </section>
    <section className="admin-auth-card"><h2>认证提供方维护</h2><p>准备维护会使在途统一登录失效，并在插件按指定配置代次重启前拒绝新的统一登录；当前有效统一登录会话不受影响。</p>
      <label>准备配置代次<input value={maintenanceGeneration} disabled={busy} onChange={(event) => setMaintenanceGeneration(event.target.value)} /></label>
      <button type="button" disabled={busy} onClick={() => { void prepareMaintenance(); }}>准备维护</button>
      <ActionFeedback result={maintenanceResult} />
    </section>
    <section className="admin-auth-card"><h2>签发迁移凭证</h2><p>凭证只显示一次。请通过受控渠道交给获准使用人。</p>
      <label>用途<select value={grantPurpose} disabled={busy} onChange={(event) => changeGrantPurpose(event.target.value as AuthGrant["purpose"])}><option value="enroll">新建账号</option><option value="recover">恢复账号</option><option value="replace">更换统一账号</option></select></label>
      <label>使用人标识<input value={grantSubject} disabled={busy} onChange={(event) => setGrantSubject(event.target.value)} /></label>
      {grantPurpose !== "enroll" && <>
        <p>可在下方账号列表点击“选择为原账号”，无需手动输入账号 ID。核对原账号与使用人标识后，点击“签发凭证”确认。</p>
        <label>目标账号 ID（必填）<input value={grantTarget} disabled={busy} onChange={(event) => { setGrantTarget(event.target.value); setSelectedGrantAccount(null); setSelectionResult(null); }} /></label>
        {selectedGrantAccount && <p>已选原账号：{selectedGrantAccount.display_name || selectedGrantAccount.username}（用户名：{selectedGrantAccount.username}；统一身份：{selectedGrantAccount.subject || "未关联"}；账号 ID：<code>{selectedGrantAccount.id}</code>）。选择账号不会签发凭证。</p>}
      </>}
      {grantPurpose === "replace" && <p>更换凭证只能用于该目标账号。用户确认新统一身份后，原用户 ID、数据和角色保留，旧统一身份与旧统一登录会话将失效。</p>}
      <button type="button" disabled={busy} onClick={() => { void createGrant(); }}>签发凭证</button>
      <ActionFeedback result={grantResult} />
      {grant && <p className="admin-auth-grant"><strong>一次性迁移凭证：</strong><code>{grant.grant_token}</code>，有效期 {grant.expires_in} 秒。</p>}
    </section>
    <section className="admin-auth-card"><h2>账号状态</h2><table><thead><tr><th>用户</th><th>统一身份</th><th>状态</th><th>操作</th></tr></thead><tbody>{accounts.items.map((account) => <tr key={account.id}><td>{account.display_name || account.username}</td><td>{account.identity_status === "active" ? account.subject || "已关联" : "未关联"}</td><td>{account.status === "active" ? "启用" : "已停用"}</td><td>
      <button type="button" disabled={busy} onClick={() => selectGrantAccount(account)}>选择为原账号</button>
      <ActionFeedback result={selectionResult?.accountId === account.id ? selectionResult : null} />
      <button type="button" disabled={busy} onClick={() => { void setAccountStatus(account); }}>{account.status === "active" ? "停用" : "启用"}</button><ActionFeedback result={accountResult?.accountId === account.id ? accountResult : null} /></td></tr>)}</tbody></table>
      <div className="admin-auth-pagination"><button type="button" disabled={busy || accountsBusy || accounts.offset === 0} onClick={() => { void loadAccountsPage(priorOffset); }}>上一页</button><span role="status">{accountsBusy ? "正在加载账号列表…" : `第 ${Math.floor(accounts.offset / accounts.limit) + 1} 页`}</span><button type="button" disabled={busy || accountsBusy || (accounts.total !== undefined && nextOffset >= accounts.total)} onClick={() => { void loadAccountsPage(nextOffset); }}>下一页</button></div>
      {accountsError && <div><p className="admin-auth-error" role="alert">{accountsError}</p><button type="button" disabled={busy || accountsBusy} onClick={() => { void loadAccountsPage(failedAccountOffset.current); }}>重试加载账号</button></div>}
    </section>
    <section className="admin-auth-card"><h2>认证审计</h2><p>只显示账号迁移与身份操作记录，不展示凭证或凭证引用。</p>
      {auditError && <p className="admin-auth-error" role="alert">{auditError}</p>}
      {!audit ? <p>{auditBusy ? "加载中…" : "暂无审计记录。"}</p> : <><table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>目标账号</th><th>身份源</th></tr></thead><tbody>{audit.items.map((item) => <tr key={item.id}><td>{item.created_at || "未知"}</td><td>{auditActionLabel(item.action)}</td><td>{item.actor_id || "系统"}</td><td>{item.target_user_id || "—"}</td><td>{item.provider_namespace || "—"}</td></tr>)}</tbody></table>
        <div className="admin-auth-pagination"><button type="button" disabled={auditBusy || audit.offset === 0} onClick={() => { void reloadAudit(Math.max(0, audit.offset - audit.limit)); }}>上一页</button><span>第 {Math.floor(audit.offset / audit.limit) + 1} 页</span><button type="button" disabled={auditBusy || audit.offset + audit.limit >= audit.total} onClick={() => { void reloadAudit(audit.offset + audit.limit); }}>下一页</button></div></>}
      {auditError && <button type="button" disabled={auditBusy} onClick={() => { void reloadAudit(); }}>{auditBusy ? "重试中…" : "重试"}</button>}
    </section>
  </main>;
}
