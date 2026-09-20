"use client";

import { FormEvent, useState } from "react";
import {
  type AuthCapabilities,
  isValidUsername,
  loginUser,
  registerUser,
  setToken,
  startIdentityBinding,
  startSsoGrant,
  startSsoLogin,
  type AuthUser,
} from "./auth";
import { toUserMessage } from "./errors.ts";

type AuthGateProps = { capabilities: AuthCapabilities; migrationSession?: boolean; onAuthenticated: (user: AuthUser) => void };

/** The pre-workspace authentication surface.  A migration session deliberately
 * never calls onAuthenticated, so it cannot load any business workspace. */
export function AuthGate({ capabilities, migrationSession = false, onAuthenticated }: AuthGateProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [migrationReady, setMigrationReady] = useState(migrationSession);
  const [grantOpen, setGrantOpen] = useState(false);
  const [grantToken, setGrantToken] = useState("");
  const [grantPurpose, setGrantPurpose] = useState<"enroll" | "recover" | "replace">("enroll");
  const canUseLocal = capabilities.local_login && capabilities.mode !== "retired";
  const canRegister = capabilities.local_registration && capabilities.mode !== "retired";
  const canUseSso = capabilities.sso_login;
  const canUseGrant = capabilities.sso_login;
  const bindingOnly = migrationSession || migrationReady;
  const usernameHint = username && !isValidUsername(username)
    ? "用户名须为「单个小写字母 + 八位数字」，如 a12345678" : "";

  async function redirectToSso(start: () => Promise<string>) {
    setError("");
    setBusy(true);
    try {
      window.location.assign(await start());
    } catch (err) {
      setError(toUserMessage(err, "统一登录暂时不可用，请稍后重试"));
      setBusy(false);
    }
  }

  async function submitLocal(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (mode === "register" && !isValidUsername(username)) {
      setError("用户名须为「单个小写字母 + 八位数字」，如 a12345678");
      return;
    }
    if (!password) { setError("请输入密码"); return; }
    setBusy(true);
    try {
      const result = mode === "login"
        ? await loginUser(username.trim(), password)
        : await registerUser(username.trim(), password);
      setToken(result.token);
      if (capabilities.mode === "binding_required") {
        const verifiedPassword = password;
        setPassword("");
        await redirectToSso(() => startIdentityBinding(verifiedPassword));
        return;
      }
      if ("migration_required" in result && result.migration_required) {
        setPassword("");
        setMigrationReady(true);
        return;
      }
      onAuthenticated(result.user);
    } catch (err) {
      setError(toUserMessage(err, mode === "login" ? "登录失败，请稍后重试" : "注册失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  }

  async function startGrant() {
    if (!grantToken.trim()) {
      setError("请输入管理员提供的一次性迁移凭证");
      return;
    }
    await redirectToSso(() => startSsoGrant(grantToken.trim(), grantPurpose));
  }

  if (bindingOnly) return (
    <div className="auth-gate">
      <form className="auth-card" onSubmit={(event) => { event.preventDefault(); void redirectToSso(() => startIdentityBinding(password)); }}>
        <div className="auth-brand">silicon-notebook</div>
        <h1 className="auth-title">关联统一身份</h1>
        <p className="auth-copy">请先验证当前本地密码，再使用统一登录确认是同一位本人。关联完成前，无法进入笔记本。</p>
        <label className="auth-label">当前密码
          <input className="auth-input" type="password" value={password} autoFocus autoComplete="current-password"
            disabled={busy} onChange={(event) => setPassword(event.target.value)} placeholder="请输入当前密码" />
        </label>
        {error && <div className="auth-error" role="alert">{error}</div>}
        <button className="auth-submit" type="submit" disabled={busy || !password}>{busy ? "正在跳转…" : "验证并关联统一身份"}</button>
        {canUseSso && <button className="auth-sso-submit" type="button" disabled={busy} onClick={() => { void redirectToSso(startSsoLogin); }}>{busy ? "正在跳转…" : "统一登录"}</button>}
      </form>
    </div>
  );

  return (
    <div className="auth-gate">
      <form className="auth-card" onSubmit={(event) => { void submitLocal(event); }}>
        <div className="auth-brand">silicon-notebook</div>
        {canUseLocal && <>
          {canRegister && capabilities.mode !== "binding_required" && <div className="auth-tabs">
            <button type="button" className={mode === "login" ? "active" : ""} disabled={busy} onClick={() => { setMode("login"); setError(""); }}>登录</button>
            <button type="button" className={mode === "register" ? "active" : ""} disabled={busy} onClick={() => { setMode("register"); setError(""); }}>注册</button>
          </div>}
          <label className="auth-label">用户名
            <input className="auth-input" value={username} autoFocus disabled={busy} onChange={(event) => setUsername(event.target.value.toLowerCase())} placeholder="a12345678" />
          </label>
          {mode === "register" && usernameHint && <div className="auth-hint">{usernameHint}</div>}
          <label className="auth-label">密码
            <input className="auth-input" type="password" value={password} autoComplete={mode === "login" ? "current-password" : "new-password"} disabled={busy} onChange={(event) => setPassword(event.target.value)} placeholder="请输入密码" />
          </label>
          <button className="auth-submit" type="submit" disabled={busy}>{busy ? "请稍候…" : capabilities.mode === "binding_required" ? "验证并关联统一身份" : mode === "login" ? "本地登录" : "注册并进入"}</button>
        </>}
        {canUseSso && <button className="auth-sso-submit" type="button" disabled={busy} onClick={() => { void redirectToSso(startSsoLogin); }}>{busy ? "正在跳转…" : "统一登录"}</button>}
        {canUseGrant && <div className="auth-migration-help">
          <button type="button" disabled={busy} aria-expanded={grantOpen} onClick={() => { setGrantOpen((value) => !value); setError(""); }}>需要迁移帮助？</button>
          {grantOpen && <div className="auth-grant-form">
            <p>请使用管理员签发的一次性迁移凭证。该凭证只能用于你获准的操作。</p>
            <label className="auth-label">迁移凭证
              <input className="auth-input" value={grantToken} disabled={busy} onChange={(event) => setGrantToken(event.target.value)} autoComplete="off" />
            </label>
            <fieldset disabled={busy}>
              <legend>操作</legend>
              <label><input type="radio" checked={grantPurpose === "enroll"} onChange={() => setGrantPurpose("enroll")} /> 新建账号</label>
              <label><input type="radio" checked={grantPurpose === "recover"} onChange={() => setGrantPurpose("recover")} /> 恢复账号</label>
              <label><input type="radio" checked={grantPurpose === "replace"} onChange={() => setGrantPurpose("replace")} /> 更换统一账号</label>
            </fieldset>
            <button className="auth-sso-submit" type="button" disabled={busy} onClick={() => { void startGrant(); }}>{busy ? "正在跳转…" : "验证凭证并继续"}</button>
          </div>}
        </div>}
        {!canUseLocal && !canUseSso && <p className="auth-error" role="alert">当前登录方式不可用，请联系管理员。</p>}
        {error && <div className="auth-error" role="alert">{error}</div>}
      </form>
    </div>
  );
}
