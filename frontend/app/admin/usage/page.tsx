"use client";

import { Fragment, useEffect, useState } from "react";
import { fetchMe } from "../../auth.ts";
import { PageHeader } from "../../components/PageHeader.tsx";
import { toUserMessage } from "../../errors.ts";
import { fetchAdminUsers, fetchOnlineIds, FORBIDDEN_SENTINEL, type AdminUserUsage } from "./api.ts";
import { formatLastActive, logsDrillHref } from "./format.ts";
import { fetchUserNotebooks, notebookStatusLabel, type AdminUserNotebook } from "./notebooks.ts";
import "./usage.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: AdminUserUsage[] };

type NotebookCacheEntry = AdminUserNotebook[] | "loading" | "error";

export default function AdminUsagePage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [nbCache, setNbCache] = useState<Record<string, NotebookCacheEntry>>({});
  const [onlineIds, setOnlineIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        if (me.role !== "admin") {
          setState({ kind: "forbidden" });
          return;
        }
        const rows = await fetchAdminUsers();
        setState({ kind: "ready", rows });
        setOnlineIds(new Set(rows.filter((r) => r.is_online).map((r) => r.id)));
      } catch (e) {
        // 哨兵先判(分流到专用无权限视图),其余一律过人话层——此前这里直出
        // e.message,断网时页面上会写「加载失败:Failed to fetch」。
        if (e instanceof Error && e.message === FORBIDDEN_SENTINEL) {
          setState({ kind: "forbidden" });
          return;
        }
        setState({ kind: "error", message: toUserMessage(e, "请稍后重试") });
      }
    })();
  }, []);

  useEffect(() => {
    if (state.kind !== "ready") return;
    const timer = setInterval(async () => {
      try {
        setOnlineIds(new Set(await fetchOnlineIds()));
      } catch {
        // 忽略瞬时失败,下个周期重试
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [state.kind]);

  async function toggleExpand(userId: string) {
    if (expanded === userId) {
      setExpanded(null);
      return;
    }
    setExpanded(userId);
    if (nbCache[userId] !== undefined) return; // 已缓存,不重复请求
    setNbCache((prev) => ({ ...prev, [userId]: "loading" }));
    try {
      const notebooks = await fetchUserNotebooks(userId);
      setNbCache((prev) => ({ ...prev, [userId]: notebooks }));
    } catch {
      setNbCache((prev) => ({ ...prev, [userId]: "error" }));
    }
  }

  if (state.kind === "loading") return <main className="usage-page">加载中…</main>;
  if (state.kind === "forbidden")
    return <main className="usage-page usage-empty">无权限:仅管理员可查看用户使用总览。</main>;
  if (state.kind === "error")
    return <main className="usage-page usage-empty">加载失败:{state.message}</main>;

  return (
    <main className="usage-page">
      <PageHeader title="用户使用总览" />
      <table className="usage-table">
        <thead>
          <tr>
            <th className="usage-expand-col"></th>
            <th>用户名</th><th>角色</th><th>注册时间</th>
            <th>笔记本</th><th>来源</th><th>对话</th><th>报告</th>
            <th>最近活跃</th><th>日志</th>
          </tr>
        </thead>
        <tbody>
          {state.rows.map((u) => {
            const isOpen = expanded === u.id;
            const entry = nbCache[u.id];
            const isOnline = onlineIds.has(u.id);
            return (
              <Fragment key={u.id}>
                <tr className={isOpen ? "usage-row-expanded" : undefined}>
                  <td className="usage-expand-col">
                    <button
                      type="button"
                      className="usage-expand-btn"
                      aria-expanded={isOpen}
                      aria-label={isOpen ? "收起笔记本列表" : "展开笔记本列表"}
                      onClick={() => toggleExpand(u.id)}
                    >
                      {isOpen ? "▾" : "▸"}
                    </button>
                  </td>
                  <td>
                    <span
                      className={`usage-dot ${isOnline ? "usage-dot-online" : "usage-dot-offline"}`}
                      role="img"
                      aria-label={isOnline ? "在线" : "离线"}
                      title={isOnline ? "在线" : "离线"}
                    />
                    {u.username}
                  </td>
                  <td>{u.role === "admin" ? "管理员" : "用户"}</td>
                  <td>{formatLastActive(u.created_at)}</td>
                  <td>{u.notebooks}</td>
                  <td>{u.sources}</td>
                  <td>{u.conversations}</td>
                  <td>{u.reports}</td>
                  <td>{formatLastActive(u.last_active)}</td>
                  <td><a href={logsDrillHref(u.id)}>查看日志</a></td>
                </tr>
                {isOpen && (
                  <tr className="usage-subrow">
                    <td colSpan={9}>
                      {entry === "loading" && (
                        <div className="usage-subtable-status">加载中…</div>
                      )}
                      {entry === "error" && (
                        <div className="usage-subtable-status usage-subtable-error">加载失败,请重试。</div>
                      )}
                      {Array.isArray(entry) && entry.length === 0 && (
                        <div className="usage-subtable-status">该用户暂无笔记本。</div>
                      )}
                      {Array.isArray(entry) && entry.length > 0 && (
                        <table className="usage-subtable">
                          <thead>
                            <tr>
                              <th>笔记本</th><th>状态</th>
                              <th>来源</th><th>对话</th><th>报告</th>
                              <th>创建</th><th>最近更新</th>
                            </tr>
                          </thead>
                          <tbody>
                            {entry.map((nb) => (
                              <tr key={nb.id}>
                                <td>{nb.name}</td>
                                <td>{notebookStatusLabel(nb.status)}</td>
                                <td>{nb.sources}</td>
                                <td>{nb.conversations}</td>
                                <td>{nb.reports}</td>
                                <td>{formatLastActive(nb.created_at)}</td>
                                <td>{formatLastActive(nb.updated_at)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </main>
  );
}
