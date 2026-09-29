"use client";

import { useState } from "react";

import type { SsoCompletion } from "./auth";

type PendingIdentityConfirmation = Exclude<SsoCompletion, { status: "authenticated" }>;

type ConfirmationProps = {
  pending: PendingIdentityConfirmation;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /** Only read for auto_enroll: whether the old account can still sign in with
   * its local password (then it can bind itself instead of being migrated). */
  localLoginAllowed?: boolean | null;
};

/** 自动开户前的确认：先说清新账号是空的，并给已有旧账号的人一条不新建的路。 */
function AutoEnrollConfirmation({ pending, busy, onConfirm, onCancel, localLoginAllowed }: ConfirmationProps) {
  const [legacy, setLegacy] = useState(false);
  if (legacy) {
    return <>
      <h1 className="auth-title">使用本站旧账号</h1>
      {localLoginAllowed
        ? <p className="auth-copy">请用本站旧账号登录，登录后在右上角账户菜单选择「关联统一身份」完成关联。你原有的笔记本等数据都在旧账号里，不会新建账号。</p>
        : <p className="auth-copy" role="status">本站密码登录已停用，请联系管理员迁移旧账号。</p>}
      <div className="auth-actions">
        <button type="button" className="auth-cancel" onClick={() => setLegacy(false)}>返回</button>
        {localLoginAllowed && <button type="button" className="auth-submit" disabled={busy} onClick={onCancel}>{busy ? "处理中…" : "去本站登录"}</button>}
      </div>
    </>;
  }
  return <>
    <h1 className="auth-title">确认新建账号</h1>
    <p className="auth-copy">系统中没有与工号 {pending.external_username} 关联的账号。继续将为你新建一个本站账号，你之前在本站的笔记本等数据不会出现在新账号里。</p>
    <dl className="identity-preview">
      <div><dt>工号</dt><dd>{pending.external_username}</dd></div>
      <div><dt>显示姓名</dt><dd>{pending.display_name || "未提供"}</dd></div>
    </dl>
    <div className="auth-actions">
      <button type="button" className="auth-cancel" disabled={busy} onClick={onCancel}>取消</button>
      <button type="button" className="auth-cancel" disabled={busy} onClick={() => setLegacy(true)}>我已有本站旧账号</button>
      <button type="button" className="auth-submit" disabled={busy} onClick={onConfirm}>{busy ? "处理中…" : "确认新建账号"}</button>
    </div>
  </>;
}

export function IdentityBindingConfirmation(props: ConfirmationProps) {
  const { pending, busy, onConfirm, onCancel } = props;
  if (pending.status === "confirmation_required" && pending.purpose === "auto_enroll") {
    return <AutoEnrollConfirmation {...props} />;
  }
  const binding = pending.status === "binding_required";
  return <>
    <h1 className="auth-title">{binding ? "确认关联身份" : pending.purpose === "replace" ? "更换统一账号" : "确认统一身份"}</h1>
    <p className="auth-copy">{binding
      ? "确认后，统一身份用户名将成为本站显示的正式用户名；原本地登录名只在并存期用于本地登录。"
      : pending.purpose === "replace"
        ? "确认后将更换统一账号。管理员已限定原账号与新身份；你的用户 ID、业务数据和角色保持不变，旧统一身份和会话将不能继续使用。"
        : pending.purpose === "recover"
        ? "确认后将恢复原有账号及其业务数据。"
        : "确认后将创建一个新的本站账号。"}</p>
    <dl className="identity-preview">
      {pending.status === "binding_required" && <div><dt>本地登录名</dt><dd>{pending.local_login_name}</dd></div>}
      {pending.status === "confirmation_required" && pending.target_user_id && <div><dt>原账号</dt><dd>{pending.target_username || pending.target_user_id}</dd></div>}
      <div><dt>统一身份用户名</dt><dd>{pending.external_username}</dd></div>
      <div><dt>显示姓名</dt><dd>{pending.display_name || "未提供"}</dd></div>
      {pending.status === "confirmation_required" && pending.purpose === "replace" && pending.previous_external_username && <div><dt>旧统一身份</dt><dd>{pending.previous_external_username}</dd></div>}
    </dl>
    {binding && <p className="auth-copy">你的用户 ID、笔记本、角色、群组和历史数据不会改变。</p>}
    <div className="auth-actions">
      <button type="button" className="auth-cancel" disabled={busy} onClick={onCancel}>取消</button>
      <button type="button" className="auth-submit" disabled={busy} onClick={onConfirm}>{busy ? "处理中…" : binding ? "确认关联" : pending.purpose === "replace" ? "确认更换" : "确认"}</button>
    </div>
  </>;
}
