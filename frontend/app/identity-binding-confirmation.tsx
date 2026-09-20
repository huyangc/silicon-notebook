"use client";

import type { SsoCompletion } from "./auth";

type PendingIdentityConfirmation = Exclude<SsoCompletion, { status: "authenticated" }>;

export function IdentityBindingConfirmation({
  pending,
  busy,
  onConfirm,
  onCancel,
}: {
  pending: PendingIdentityConfirmation;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
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
