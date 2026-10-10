"use client";

import { useEffect, useRef, useState } from "react";
import { completeSsoLogin, setToken, type SsoChoice } from "../../../auth";
import { toUserMessage } from "../../../errors";
import { consumeSsoReturnLocation } from "../../../auth-return-location";
import { SsoAccountChoice } from "../../../sso-account-choice";

type CallbackState =
  | { kind: "working" }
  | { kind: "error"; copy: string }
  | { kind: "choice"; pending: SsoChoice };

function callbackErrorMessage(key: string | null): string {
  if (key === "cancelled") return "已取消统一认证登录，请返回后重试。";
  return "统一认证登录未完成，请返回后重试。";
}

/** The URL may contain only the server-issued one-time handoff code.  It is
 * consumed immediately and removed before any network completion can render. */
export default function SsoCallbackPage() {
  const [state, setState] = useState<CallbackState>({ kind: "working" });
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
        if (result.status === "choice_required") {
          setState({ kind: "choice", pending: result });
          return;
        }
        setState({ kind: "error", copy: callbackErrorMessage(null) });
      })
      .catch((err) => setState({ kind: "error", copy: toUserMessage(err, "统一认证登录未完成，请返回后重试。") }));
  }, []);

  return (
    <main className="auth-gate">
      <section className="auth-card auth-callback-card" aria-live="polite">
        <div className="auth-brand">silicon-notebook</div>
        {state.kind === "working" && <><h1 className="auth-title">正在完成统一认证登录</h1><p className="auth-copy">请稍候。</p></>}
        {state.kind === "error" && <>
          <h1 className="auth-title">统一认证登录未完成</h1>
          <p className="auth-error" role="alert">{state.copy}</p>
          <a className="auth-return" href="/" onClick={(event) => {
            event.preventDefault();
            window.location.replace(consumeSsoReturnLocation());
          }}>返回登录页</a>
        </>}
        {state.kind === "choice" && <SsoAccountChoice pending={state.pending} />}
      </section>
    </main>
  );
}
