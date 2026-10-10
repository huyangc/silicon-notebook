"use client";

import { useState, type FormEvent } from "react";

import {
  cancelSsoChoice,
  createSsoAccount,
  linkSsoAccount,
  setToken,
  type AuthUser,
  type SsoChoice,
} from "./auth";
import { consumeSsoReturnLocation } from "./auth-return-location";
import { httpErrorStatus, toUserMessage } from "./errors";

type ChoiceDeps = {
  link?: typeof linkSsoAccount;
  create?: typeof createSsoAccount;
  cancel?: typeof cancelSsoChoice;
  /** Leaves the callback page for `location`; replaced in tests. */
  navigate?: (location: string) => void;
};

type Busy = "link" | "create" | "cancel" | null;

/**
 * 统一认证回来、工号对不上任何本站用户名时的选择页：关联老账号（验证老账号密码），
 * 或不关联、以工号新建账号。三条动作互斥：任一条在途时其余按钮停用，结果就近显示
 * 在发起它的表单或按钮旁。关联失败不消耗本次选择，可以直接重输。
 */
export function SsoAccountChoice({
  pending,
  link = linkSsoAccount,
  create = createSsoAccount,
  cancel = cancelSsoChoice,
  navigate = (location) => window.location.replace(location),
}: { pending: SsoChoice } & ChoiceDeps) {
  const [busy, setBusy] = useState<Busy>(null);
  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [linkError, setLinkError] = useState("");
  const [createError, setCreateError] = useState("");
  const [done, setDone] = useState(false);
  // 后端 410：本次待选已作废（过期/已使用/状态变化），再提交也不会成功，只能回登录重来。
  const [voided, setVoided] = useState(false);

  function signIn(result: { token: string; user: AuthUser }) {
    setToken(result.token);
    setDone(true);
    navigate(consumeSsoReturnLocation());
  }

  async function submitLink(event: FormEvent) {
    event.preventDefault();
    if (busy || done || voided) return;
    if (!loginName.trim() || !password) {
      setLinkError("请输入老账号的用户名和密码");
      return;
    }
    setLinkError("");
    setCreateError("");
    setBusy("link");
    try {
      signIn(await link(pending.pending_id, loginName.trim(), password));
    } catch (err) {
      setPassword("");
      setLinkError(toUserMessage(err, "关联未完成，请重试"));
      if (httpErrorStatus(err) === 410) setVoided(true);
      setBusy(null);
    }
  }

  async function submitCreate() {
    if (busy || done || voided) return;
    setLinkError("");
    setCreateError("");
    setBusy("create");
    try {
      signIn(await create(pending.pending_id));
    } catch (err) {
      setCreateError(toUserMessage(err, "新建账号未完成，请重试"));
      if (httpErrorStatus(err) === 410) setVoided(true);
      setBusy(null);
    }
  }

  async function returnToLogin() {
    if (busy || done) return;
    setBusy("cancel");
    try {
      await cancel(pending.pending_id);
    } catch {
      // 放弃是尽力而为：未取消的选择会按有效期自行失效，不能把用户卡在这一页。
    }
    setDone(true);
    navigate(consumeSsoReturnLocation());
  }

  const returnLocked = busy !== null || done;
  const locked = returnLocked || voided;
  return <>
    <h1 className="auth-title">统一认证账号 {pending.external_username} 在本站还没有对应账号</h1>
    {pending.display_name && <p className="auth-copy">姓名：{pending.display_name}</p>}

    <form className="sso-choice-option" aria-label="关联老账号" onSubmit={(event) => { void submitLink(event); }}>
      <h2 className="sso-choice-heading">关联老账号</h2>
      <p className="auth-copy">输入你在本站原来使用的用户名和密码。关联后该账号的用户名改为 {pending.external_username}，原有数据保持不变。</p>
      <label className="auth-label">老账号用户名
        <input className="auth-input" autoComplete="username" value={loginName} disabled={locked}
          onChange={(event) => setLoginName(event.target.value)} />
      </label>
      <label className="auth-label">老账号密码
        <input className="auth-input" type="password" autoComplete="current-password" value={password} disabled={locked}
          onChange={(event) => setPassword(event.target.value)} />
      </label>
      {linkError && <p className="auth-error" role="alert">{linkError}</p>}
      <button className="auth-submit" type="submit" disabled={locked} aria-busy={busy === "link"}>
        {busy === "link" ? "正在关联…" : "关联并登录"}
      </button>
    </form>

    <section className="sso-choice-option" aria-label="不关联，使用新账号">
      <h2 className="sso-choice-heading">不关联，使用新账号</h2>
      <p className="auth-copy">将以 {pending.external_username} 新建账号，老账号的数据不会出现在新账号里。选定后无法自行改为关联老账号，需要时请联系管理员。</p>
      {createError && <p className="auth-error" role="alert">{createError}</p>}
      <button className="auth-sso-submit" type="button" disabled={locked} aria-busy={busy === "create"} onClick={() => { void submitCreate(); }}>
        {busy === "create" ? "正在新建…" : "使用新账号"}
      </button>
    </section>

    <button className="auth-cancel" type="button" disabled={returnLocked} aria-busy={busy === "cancel"} onClick={() => { void returnToLogin(); }}>
      {busy === "cancel" ? "正在返回…" : "返回登录"}
    </button>
  </>;
}
