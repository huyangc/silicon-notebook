import { useRef, useState, type FormEvent } from "react";

import { toUserMessage } from "./errors.ts";

/** applied: the page is switching to the old account; discarded: the page's
 * session changed while the request was in flight, so its result was not
 * installed; in-flight: another submission already owns the migration. */
export type IdentityMigrationOutcome = "applied" | "discarded" | "in-flight";

type IdentityMigrationFormProps = {
  /** Owned by the page, not this form: closing the form must not release it. */
  inFlight: boolean;
  onSubmit: (loginName: string, password: string) => Promise<IdentityMigrationOutcome>;
  onClose: () => void;
};

/**
 * 自动开户账号迁回旧账号的表单。进行中状态由页面持有，关掉再打开仍是「正在迁移」；
 * 取消键始终可用，关掉后在途请求的结果不再写回这里（会话切换由页面负责）。
 */
export function IdentityMigrationForm({ inFlight, onSubmit, onClose }: IdentityMigrationFormProps) {
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const closedRef = useRef(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (inFlight) return;
    if (!loginName.trim() || !password) {
      setError("请输入旧账号登录名和密码");
      return;
    }
    setError("");
    setStatus("");
    try {
      const outcome = await onSubmit(loginName.trim(), password);
      if (closedRef.current) return;
      if (outcome === "applied") setStatus("迁移完成，正在切换到旧账号…");
      else if (outcome === "discarded") setStatus("当前页面的登录状态已变化，本次结果未应用到此页面，请刷新页面后查看。");
    } catch (err) {
      if (!closedRef.current) setError(toUserMessage(err, "迁移失败，请稍后重试"));
    }
  }

  function close() {
    closedRef.current = true;
    onClose();
  }

  return (
    <form className="identity-binding-form" aria-label="迁移旧账号" onSubmit={(event) => { void submit(event); }}>
      <p>输入原本站账号的登录名和密码。迁移后统一登录将进入旧账号，旧账号的数据保持不变；当前自动开通的账号会被停用。</p>
      <label>旧账号登录名
        <input autoComplete="username" value={loginName} disabled={inFlight}
          onChange={(event) => setLoginName(event.target.value)} />
      </label>
      <label>旧账号密码
        <input type="password" autoComplete="current-password" value={password} disabled={inFlight}
          onChange={(event) => setPassword(event.target.value)} />
      </label>
      {error && <p className="identity-binding-error" role="alert">{error}</p>}
      {status && <p className="identity-linked-status" role="status">{status}</p>}
      <div className="identity-binding-actions">
        <button type="button" onClick={close}>取消</button>
        <button type="submit" disabled={inFlight}>{inFlight ? "正在迁移…" : "迁移"}</button>
      </div>
    </form>
  );
}
