"use client";

import { FormEvent, useState } from "react";
import {
  type AuthCapabilities,
  isValidUsername,
  loginUser,
  registerUser,
  setToken,
  startSsoLogin,
  type AuthUser,
} from "./auth";
import { toUserMessage } from "./errors.ts";

type AuthGateProps = { capabilities: AuthCapabilities; onAuthenticated: (user: AuthUser) => void };

/** 统一认证的按钮文案：部署给了提供方名称就用它，否则用通用说法。 */
export function ssoLoginLabel(providerLabel: string): string {
  const label = providerLabel.trim();
  return label ? `使用${label}登录` : "使用统一认证登录";
}

/** The pre-workspace authentication surface.  With unified authentication on,
 * the only way in is the provider button; passwords are asked for only when an
 * older account is linked on the callback page. */
export function AuthGate({ capabilities, onAuthenticated }: AuthGateProps) {
  if (capabilities.sso_login) return <SsoLoginGate providerLabel={capabilities.provider_label} />;
  return <LocalLoginGate capabilities={capabilities} onAuthenticated={onAuthenticated} />;
}

function SsoLoginGate({ providerLabel }: { providerLabel: string }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function redirectToSso() {
    setError("");
    setBusy(true);
    try {
      window.location.assign(await startSsoLogin());
    } catch (err) {
      setError(toUserMessage(err, "统一认证暂时不可用，请稍后重试"));
      setBusy(false);
    }
  }

  return (
    <div className="auth-gate">
      <div className="auth-card">
        <div className="auth-brand">silicon-notebook</div>
        <button className="auth-sso-submit" type="button" disabled={busy} aria-busy={busy} onClick={() => { void redirectToSso(); }}>
          {busy ? "正在跳转…" : ssoLoginLabel(providerLabel)}
        </button>
        {error && <div className="auth-error" role="alert">{error}</div>}
      </div>
    </div>
  );
}

function LocalLoginGate({ capabilities, onAuthenticated }: AuthGateProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const canUseLocal = capabilities.local_login;
  const canRegister = capabilities.local_registration;
  const usernameHint = username && !isValidUsername(username)
    ? "用户名须为「单个小写字母 + 八位数字」，如 a12345678" : "";

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
      onAuthenticated(result.user);
    } catch (err) {
      setError(toUserMessage(err, mode === "login" ? "登录失败，请稍后重试" : "注册失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-gate">
      <form className="auth-card" onSubmit={(event) => { void submitLocal(event); }}>
        <div className="auth-brand">silicon-notebook</div>
        {canUseLocal && <>
          {canRegister && <div className="auth-tabs">
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
          <button className="auth-submit" type="submit" disabled={busy}>{busy ? "请稍候…" : mode === "login" ? "本地登录" : "注册并进入"}</button>
        </>}
        {!canUseLocal && <p className="auth-error" role="alert">当前登录方式不可用，请联系管理员。</p>}
        {error && <div className="auth-error" role="alert">{error}</div>}
      </form>
    </div>
  );
}
