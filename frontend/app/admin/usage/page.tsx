"use client";

import { Fragment, useEffect, useState } from "react";
import { fetchMe } from "../../auth.ts";
import { fetchAdminUsers, type AdminUserUsage } from "./api.ts";
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
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setState(msg === "forbidden" ? { kind: "forbidden" } : { kind: "error", message: msg });
      }
    })();
  }, []);

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
      <h1>用户使用总览</h1>
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
                  <td>{u.username}</td>
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
