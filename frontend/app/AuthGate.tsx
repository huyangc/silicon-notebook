"use client";
import { FormEvent, useState } from "react";
import { isValidUsername, loginUser, registerUser, setToken, type AuthUser } from "./auth";

export function AuthGate({ onAuthenticated }: { onAuthenticated: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const usernameHint = username && !isValidUsername(username)
    ? "用户名须为「单个字母 + 00 + 六位数字」，如 a00123456" : "";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (mode === "register" && !isValidUsername(username)) {
      setError("用户名须为「单个字母 + 00 + 六位数字」，如 a00123456");
      return;
    }
    if (!password) { setError("请输入密码"); return; }
    setBusy(true);
    try {
      const fn = mode === "login" ? loginUser : registerUser;
      const { token, user } = await fn(username.trim(), password);
      setToken(token);
      onAuthenticated(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-gate">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">silicon-notebook</div>
        <div className="auth-tabs">
          <button type="button" className={mode === "login" ? "active" : ""}
            onClick={() => { setMode("login"); setError(""); }}>登录</button>
          <button type="button" className={mode === "register" ? "active" : ""}
            onClick={() => { setMode("register"); setError(""); }}>注册</button>
        </div>
        <label className="auth-label">用户名
          <input className="auth-input" value={username} autoFocus
            onChange={(e) => setUsername(e.target.value)} placeholder="a00123456" />
        </label>
        {mode === "register" && usernameHint && <div className="auth-hint">{usernameHint}</div>}
        <label className="auth-label">密码
          <input className="auth-input" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="请输入密码" />
        </label>
        {error && <div className="auth-error">{error}</div>}
        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? "请稍候…" : mode === "login" ? "登录" : "注册并进入"}
        </button>
      </form>
    </div>
  );
}
