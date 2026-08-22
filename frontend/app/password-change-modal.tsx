"use client";

import { useState, type FormEvent } from "react";

import { changeMyPassword } from "./auth.ts";
import { toUserMessage } from "./errors.ts";
import { FloatingModalCard } from "./floating-modal-card.tsx";

type PasswordChangeModalProps = {
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
};

/**
 * 自助修改密码弹窗。骨架镜像 page.tsx 的 deleteNotebook 弹窗(utility-modal >
 * FloatingModalCard.utility-modal-card.narrow > source-modal-header + 拖动手柄)。
 * 自持表单状态,不依赖外部 page 上下文。
 */
export function PasswordChangeModal({ onClose, interactive = true, zIndex }: PasswordChangeModalProps) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (!newPassword.trim()) {
      setError("新密码不能为空");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    try {
      await changeMyPassword(oldPassword, newPassword);
      setDone(true);
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(toUserMessage(err, "修改失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="utility-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} style={{ zIndex }}>
      <FloatingModalCard storageKey="passwordChange.window" className="utility-modal-card narrow">
        {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>修改密码</h2>
            <p>修改成功后，本设备保持登录，其他设备将需要重新登录。</p>
          </div>
          <button className="icon-button" disabled={busy} onClick={onClose} title="Close">×</button>
        </div>
        {done ? (
          <>
            <div className="edit-form">
              <p className="password-change-status success">密码已修改。其他设备将需要重新登录。</p>
            </div>
            {/* modal-actions 是 header 的兄弟节点(镜像 deleteNotebook 弹窗),不嵌进
                .edit-form——后者自带 24px padding,嵌套会叠出双重内边距。 */}
            <div className="modal-actions padded">
              <button type="button" className="sort-button" onClick={onClose}>关闭</button>
            </div>
          </>
        ) : (
          <form id="password-change-form" className="edit-form" onSubmit={(event) => { void submit(event); }}>
            <label>当前密码
              <input
                type="password"
                value={oldPassword}
                autoComplete="current-password"
                disabled={busy}
                onChange={(event) => setOldPassword(event.target.value)}
              />
            </label>
            <label>新密码
              <input
                type="password"
                value={newPassword}
                autoComplete="new-password"
                disabled={busy}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </label>
            <label>确认新密码
              <input
                type="password"
                value={confirmPassword}
                autoComplete="new-password"
                disabled={busy}
                onChange={(event) => setConfirmPassword(event.target.value)}
              />
            </label>
            {error && <p className="password-change-status error">{error}</p>}
          </form>
        )}
        {!done && (
          /* 按钮行放在 .edit-form 外面(同 done 分支):.edit-form 自带 24px padding,
             嵌套 .modal-actions.padded 会叠出双重内边距;提交按钮用 form 属性关联。 */
          <div className="modal-actions padded">
            <button type="button" className="sort-button" disabled={busy} onClick={onClose}>取消</button>
            <button type="submit" form="password-change-form" className="new-pill" disabled={busy}>
              {busy ? "保存中…" : "确认修改"}
            </button>
          </div>
        )}
        </>)}
      </FloatingModalCard>
    </section>
  );
}
