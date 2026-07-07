"use client";

import { useEffect, useState } from "react";
import { fetchMe } from "../../auth.ts";
import { fetchAdminUsers, type AdminUserUsage } from "./api.ts";
import { formatLastActive, logsDrillHref } from "./format.ts";
import "./usage.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: AdminUserUsage[] };

export default function AdminUsagePage() {
  const [state, setState] = useState<State>({ kind: "loading" });

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
            <th>用户名</th><th>角色</th><th>注册时间</th>
            <th>笔记本</th><th>来源</th><th>对话</th><th>报告</th>
            <th>最近活跃</th><th>日志</th>
          </tr>
        </thead>
        <tbody>
          {state.rows.map((u) => (
            <tr key={u.id}>
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
          ))}
        </tbody>
      </table>
    </main>
  );
}
