"use client";

import { FileText, MessageCircleQuestion, Search, Users } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchMe } from "../../auth.ts";
import { PageHeader } from "../../components/PageHeader.tsx";
import { toUserMessage } from "../../errors.ts";
import { fetchSystemConfiguration } from "../../system-api.ts";
import { fetchAdminUsers, type AdminUserUsage } from "../usage/api.ts";
import {
  ADMIN_QUESTIONS_QUERY_MAX_CHARS,
  fetchAdminQuestions,
  type AdminQuestionKind,
  type AdminQuestionsPage,
} from "./api.ts";
import "./questions.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "unavailable" }
  | { kind: "error"; notice: string }
  | { kind: "ready"; page: AdminQuestionsPage };

function formattedTime(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "等待中", running: "进行中", generating: "生成中",
    completed: "已完成", done: "已完成", failed: "失败", cancelled: "已取消",
    interrupted: "已中断", planning: "规划中",
    intent_ready: "待确认问题", outline_ready: "待确认大纲",
  };
  return labels[status] ?? "状态未知";
}

export default function AdminQuestionsPage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [authorized, setAuthorized] = useState<boolean | null>(null);
  const [users, setUsers] = useState<AdminUserUsage[]>([]);
  const [kind, setKind] = useState<AdminQuestionKind | "">("");
  const [userId, setUserId] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [searchNotice, setSearchNotice] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const requestGeneration = useRef(0);

  const initialize = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const me = await fetchMe();
      if (me.role !== "admin") {
        setAuthorized(false);
        setState({ kind: "forbidden" });
        return;
      }
      const config = await fetchSystemConfiguration();
      if (!config.user_activity_view_enabled) {
        setAuthorized(false);
        setState({ kind: "unavailable" });
        return;
      }
      const allUsers = await fetchAdminUsers();
      setUsers(allUsers);
      setAuthorized(true);
    } catch (error) {
      setState({ kind: "error", notice: toUserMessage(error, "提问分析加载失败，请重试") });
    }
  }, []);

  const load = useCallback(async () => {
    if (!authorized) return;
    const generation = ++requestGeneration.current;
    setState({ kind: "loading" });
    try {
      const page = await fetchAdminQuestions({
        kind: kind || undefined,
        userId: userId || undefined,
        query,
        offset,
      });
      if (generation !== requestGeneration.current) return;
      setState({ kind: "ready", page });
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      setState({ kind: "error", notice: toUserMessage(error, "提问分析加载失败，请重试") });
    }
  }, [authorized, kind, userId, query, offset]);

  useEffect(() => { void initialize(); }, [initialize]);
  useEffect(() => { void load(); }, [load]);

  function applySearch() {
    const nextQuery = searchDraft.trim();
    if (codePointLength(nextQuery) > ADMIN_QUESTIONS_QUERY_MAX_CHARS) {
      setSearchNotice(`搜索内容不能超过 ${ADMIN_QUESTIONS_QUERY_MAX_CHARS} 个字符`);
      return;
    }
    setSearchNotice("");
    if (nextQuery === query) return;
    ++requestGeneration.current;
    setOffset(0);
    setQuery(nextQuery);
  }

  function changeKind(next: AdminQuestionKind | "") {
    if (next === kind) return;
    ++requestGeneration.current;
    setOffset(0);
    setKind(next);
  }

  function changeUser(next: string) {
    if (next === userId) return;
    ++requestGeneration.current;
    setOffset(0);
    setUserId(next);
  }

  function changeOffset(next: number) {
    if (next === offset) return;
    ++requestGeneration.current;
    setOffset(next);
  }

  return (
    <>
      <PageHeader title="提问分析" />
      <main className="questions-page">
        <div className="questions-heading">
          <div>
            <h1>提问分析</h1>
            <p>跨用户查看问答与深度报告中的问题，了解大家真正关注的内容。</p>
          </div>
        </div>

        {state.kind === "forbidden" && <div className="questions-state">仅管理员可以查看全局提问分析。</div>}
        {state.kind === "unavailable" && <div className="questions-state">当前部署未开启提问分析。</div>}
        {state.kind === "error" && <div className="questions-state error"><span>{state.notice}</span><button type="button" onClick={() => { void (authorized ? load() : initialize()); }}>重试</button></div>}

        {authorized === true && (
          <section className="questions-filters" aria-label="提问筛选">
            <div className="questions-kind-tabs" role="group" aria-label="提问来源">
              <button type="button" className={kind === "" ? "active" : ""} onClick={() => changeKind("")}>全部</button>
              <button type="button" className={kind === "ask" ? "active" : ""} onClick={() => changeKind("ask")}>问答</button>
              <button type="button" className={kind === "report" ? "active" : ""} onClick={() => changeKind("report")}>深度报告</button>
            </div>
            <label>用户
              <select value={userId} onChange={(event) => changeUser(event.target.value)}>
                <option value="">全部用户</option>
                {users.map((user) => <option key={user.id} value={user.id}>{user.username}</option>)}
              </select>
            </label>
            <div className="questions-search">
              <Search size={16} />
              <input aria-label="搜索提问内容" value={searchDraft} aria-invalid={codePointLength(searchDraft.trim()) > ADMIN_QUESTIONS_QUERY_MAX_CHARS} placeholder="搜索提问内容" onChange={(event) => { setSearchDraft(event.target.value); setSearchNotice(""); }} onKeyDown={(event) => { if (event.key === "Enter") applySearch(); }} />
              <button type="button" onClick={applySearch}>搜索</button>
              {searchNotice && <small role="alert">{searchNotice}</small>}
            </div>
          </section>
        )}

        {state.kind === "loading" && <div className="questions-state">正在汇总提问…</div>}
        {state.kind === "ready" && (
          <>
            <section className="questions-stats" aria-label="提问汇总">
              <article><MessageCircleQuestion size={19} /><div><strong>{state.page.stats.total}</strong><span>全部提问</span></div></article>
              <article><MessageCircleQuestion size={19} /><div><strong>{state.page.stats.asks}</strong><span>问答</span></div></article>
              <article><FileText size={19} /><div><strong>{state.page.stats.reports}</strong><span>深度报告</span></div></article>
              <article><Users size={19} /><div><strong>{state.page.stats.active_users}</strong><span>活跃用户</span></div></article>
            </section>

            <section className="questions-results">
              <div className="questions-results-head"><h2>提问内容</h2><span>共 {state.page.total} 条</span></div>
              {state.page.items.length === 0 ? (
                <div className="questions-empty">没有符合当前条件的提问。</div>
              ) : (
                <div className="questions-table-wrap">
                  <table className="questions-table">
                    <thead><tr><th>来源</th><th>提问内容</th><th>用户</th><th>笔记本</th><th>状态</th><th>时间</th></tr></thead>
                    <tbody>
                      {state.page.items.map((item) => (
                        <tr key={`${item.type}-${item.id}`}>
                          <td><span className={`questions-kind questions-kind-${item.type}`}>{item.type === "ask" ? "问答" : "深度报告"}</span></td>
                          <td className="questions-question">{item.question}</td>
                          <td>{item.username}</td>
                          <td>{item.notebook_name || "已删除的笔记本"}</td>
                          <td><span className="questions-status">{statusLabel(item.status)}</span></td>
                          <td className="questions-time">{formattedTime(item.created_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="questions-pagination">
                <span>第 {Math.floor(state.page.offset / state.page.limit) + 1} 页</span>
                <button type="button" disabled={state.page.offset === 0} onClick={() => changeOffset(Math.max(0, offset - state.page.limit))}>上一页</button>
                <button type="button" disabled={state.page.offset + state.page.items.length >= state.page.total} onClick={() => changeOffset(offset + state.page.limit)}>下一页</button>
              </div>
            </section>
          </>
        )}
      </main>
    </>
  );
}
