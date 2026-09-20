"use client";

import { useEffect, useRef, useState } from "react";
import {
  cancelIdentityBinding,
  completeSsoLogin,
  confirmIdentityBinding,
  getToken,
  setToken,
  type SsoCompletion,
} from "../../../auth";
import { toUserMessage } from "../../../errors";
import { IdentityBindingConfirmation } from "../../../identity-binding-confirmation";
import { consumeSsoReturnLocation } from "../../../auth-return-location";

type CallbackState =
  | { kind: "working" }
  | { kind: "error"; copy: string }
  | { kind: "preview"; pending: Exclude<SsoCompletion, { status: "authenticated" }> };

function callbackErrorMessage(key: string | null): string {
  if (key === "cancelled") return "已取消统一登录，请返回后重试。";
  return "统一登录未完成，请返回后重试。";
}

/** The URL may contain only the server-issued one-time handoff code.  It is
 * consumed immediately and removed before any network completion can render. */
export default function SsoCallbackPage() {
  const [state, setState] = useState<CallbackState>({ kind: "working" });
  const [busy, setBusy] = useState(false);
  const completionStarted = useRef(false);

  useEffect(() => {
    if (completionStarted.current) return;
    completionStarted.current = true;
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const error = params.get("error");
    window.history.replaceState(null, "", "/auth/sso/callback");
    if (error || !code) {
      setState({ kind: "error", copy: callbackErrorMessage(error) });
      return;
    }
    void completeSsoLogin(code)
      .then((result) => {
        if (result.status === "authenticated") {
          setToken(result.token);
          window.location.replace(consumeSsoReturnLocation());
          return;
        }
        setState({ kind: "preview", pending: result });
      })
      .catch((err) => setState({ kind: "error", copy: toUserMessage(err, "统一登录未完成，请返回后重试。") }));
  }, []);

  async function confirm() {
    if (state.kind !== "preview") return;
    if (state.pending.status === "binding_required" && !getToken()) {
      setState({ kind: "error", copy: "本地验证已失效，请返回后重新开始关联。" });
      return;
    }
    setBusy(true);
    try {
      const result = await confirmIdentityBinding(state.pending.pending_id);
      setToken(result.token);
      window.location.replace(consumeSsoReturnLocation());
    } catch (err) {
      setState({ kind: "error", copy: toUserMessage(err, "关联未完成，请返回后重试。") });
      setBusy(false);
    }
  }

  async function cancel() {
    if (state.kind !== "preview") return;
    setBusy(true);
    try {
      await cancelIdentityBinding(state.pending.pending_id);
      window.location.replace(consumeSsoReturnLocation());
    } catch (err) {
      setState({ kind: "error", copy: toUserMessage(err, "取消关联失败，请稍后重试。") });
      setBusy(false);
    }
  }

  return (
    <main className="auth-gate">
      <section className="auth-card auth-callback-card" aria-live="polite">
        <div className="auth-brand">silicon-notebook</div>
        {state.kind === "working" && <><h1 className="auth-title">正在完成统一登录</h1><p className="auth-copy">请稍候。</p></>}
        {state.kind === "error" && <>
          <h1 className="auth-title">统一登录未完成</h1>
          <p className="auth-error" role="alert">{state.copy}</p>
          <a className="auth-return" href="/" onClick={(event) => {
            event.preventDefault();
            window.location.replace(consumeSsoReturnLocation());
          }}>返回登录页</a>
        </>}
        {state.kind === "preview" && <>
          <IdentityBindingConfirmation pending={state.pending} busy={busy} onConfirm={() => { void confirm(); }} onCancel={() => { void cancel(); }} />
        </>}
      </section>
    </main>
  );
}
