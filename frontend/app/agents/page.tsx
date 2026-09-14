"use client";

// 「Agent 接入」独立页——账户菜单直达。壳层同许愿墙/管理页:`fetchMe` 登录门
// (加载中 / 加载失败 / 就绪)+ PageHeader;页内全部交互由 AgentAccessManager 负责。
import { Bot } from "lucide-react";
import { useEffect, useState } from "react";

import { AgentAccessManager } from "../agent-access-manager";
import { fetchMe } from "../auth.ts";
import { PageHeader } from "../components/PageHeader.tsx";
import { toUserMessage } from "../errors.ts";
import "./agents.css";

type State =
  | { kind: "loading" }
  | { kind: "error"; notice: string }
  | { kind: "ready" };

export default function AgentAccessPage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  // 页面级会话信号:独立页没有「退出登录时一并中止」的外层编排,组件卸载时
  // AgentAccessManager 会自行中止在途请求,这里只需一个稳定、不会被中止的信号。
  const [session] = useState(() => new AbortController());

  useEffect(() => {
    let cancelled = false;
    fetchMe()
      .then(() => { if (!cancelled) setState({ kind: "ready" }); })
      .catch((error) => {
        if (!cancelled) setState({ kind: "error", notice: toUserMessage(error, "加载失败，请稍后重试") });
      });
    return () => { cancelled = true; };
  }, []);

  return (
    <>
      <PageHeader title="Agent 接入" />
      <main className="agent-access-page">
        <section className="agent-access-hero">
          <div className="agent-access-hero-icon"><Bot size={24} /></div>
          <div>
            <h1>Agent 接入</h1>
            <p>
              为 Claude Code、Codex 等客户端签发最小权限 token，并随时回来调整它能访问的笔记本、权限和过期时间。
              配置方法见
              <a href="/api/agent-mcp/onboarding" target="_blank" rel="noreferrer">Agent MCP 接入说明</a>。
            </p>
          </div>
        </section>
        {state.kind === "loading" && <p className="agent-access-page-state">加载中…</p>}
        {state.kind === "error" && <p className="agent-access-page-state error" role="alert">{state.notice}</p>}
        {state.kind === "ready" && <AgentAccessManager sessionSignal={session.signal} />}
      </main>
    </>
  );
}
