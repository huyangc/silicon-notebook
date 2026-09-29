import { useRef, useState, type FormEvent } from "react";

import { toUserMessage } from "./errors.ts";

type IdentityMigrationFormProps = {
  onSubmit: (loginName: string, password: string) => Promise<void>;
  onClose: () => void;
};

/**
 * 自动开户账号迁回旧账号的表单。提交中只锁输入与提交键；取消键始终可用，
 * 关掉后在途请求的结果不再写回这里（成功时的会话切换由调用方负责）。
 */
export function IdentityMigrationForm({ onSubmit, onClose }: IdentityMigrationFormProps) {
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const closedRef = useRef(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!loginName.trim() || !password) {
      setError("请输入旧账号登录名和密码");
      return;
    }
    setError("");
    setStatus("");
    setBusy(true);
    try {
      await onSubmit(loginName.trim(), password);
      if (!closedRef.current) setStatus("迁移完成，正在切换到旧账号…");
    } catch (err) {
      if (!closedRef.current) {
        setError(toUserMessage(err, "迁移失败，请稍后重试"));
        setBusy(false);
      }
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
        <input autoComplete="username" value={loginName} disabled={busy}
          onChange={(event) => setLoginName(event.target.value)} />
      </label>
      <label>旧账号密码
        <input type="password" autoComplete="current-password" value={password} disabled={busy}
          onChange={(event) => setPassword(event.target.value)} />
      </label>
      {error && <p className="identity-binding-error" role="alert">{error}</p>}
      {status && <p className="identity-linked-status" role="status">{status}</p>}
      <div className="identity-binding-actions">
        <button type="button" onClick={close}>取消</button>
        <button type="submit" disabled={busy}>{busy ? "正在迁移…" : "迁移"}</button>
      </div>
    </form>
  );
}
