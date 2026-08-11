"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { fetchMe } from "../../auth.ts";
import { PageHeader } from "../../components/PageHeader.tsx";
import { toUserMessage } from "../../errors.ts";
import {
  fetchAdminUsers,
  fetchOnlineIds,
  fetchUploadLimitDefault,
  FORBIDDEN_SENTINEL,
  resetAdminUserPassword,
  updateAdminUserRole,
  updateAdminUserUploadLimit,
  updateUploadLimitDefault,
  type AdminUserRole,
  type AdminUserUsage,
} from "./api.ts";
import {
  formatLastActive,
  logsDrillHref,
  parseUploadLimit,
  sortAdminUsers,
  type AdminUserSortKey,
  type SortDirection,
} from "./format.ts";
import { fetchUserNotebooks, notebookStatusLabel, type AdminUserNotebook } from "./notebooks.ts";
import "./usage.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: AdminUserUsage[] };

type NotebookCacheEntry = AdminUserNotebook[] | "loading" | "error";

const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

type SortableHeaderProps = {
  label: string;
  sortKey: AdminUserSortKey;
  activeKey: AdminUserSortKey;
  direction: SortDirection;
  onSort: (key: AdminUserSortKey) => void;
};

function SortableHeader({ label, sortKey, activeKey, direction, onSort }: SortableHeaderProps) {
  const active = activeKey === sortKey;
  const ariaSort = active ? (direction === "asc" ? "ascending" : "descending") : "none";
  return (
    <th aria-sort={ariaSort}>
      <button
        type="button"
        className={`usage-sort-button${active ? " usage-sort-button-active" : ""}`}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span className="usage-sort-indicator" aria-hidden="true">
          {active ? (direction === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

export default function AdminUsagePage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [nbCache, setNbCache] = useState<Record<string, NotebookCacheEntry>>({});
  const [onlineIds, setOnlineIds] = useState<Set<string>>(new Set());
  const [currentUserId, setCurrentUserId] = useState("");
  const [confirmingRole, setConfirmingRole] = useState<{ userId: string; role: AdminUserRole } | null>(null);
  const [rolePendingId, setRolePendingId] = useState("");
  const [roleNotice, setRoleNotice] = useState<{ kind: "ok" | "error"; message: string } | null>(null);
  const [uploadLimitDefault, setUploadLimitDefault] = useState<number | null>(null);
  const [defaultInput, setDefaultInput] = useState("");
  const [defaultSaving, setDefaultSaving] = useState(false);
  const [editingLimitId, setEditingLimitId] = useState("");
  const [limitInput, setLimitInput] = useState("");
  const [limitPendingId, setLimitPendingId] = useState("");
  const [limitNotice, setLimitNotice] = useState<{ kind: "ok" | "error"; message: string } | null>(null);
  const [resettingId, setResettingId] = useState("");
  const [resetInput, setResetInput] = useState("");
  const [resetPendingId, setResetPendingId] = useState("");
  const [resetNotice, setResetNotice] = useState<{ kind: "ok" | "error"; message: string } | null>(null);
  const [sortKey, setSortKey] = useState<AdminUserSortKey>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZE_OPTIONS[0]);

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        if (me.role !== "admin") {
          setState({ kind: "forbidden" });
          return;
        }
        setCurrentUserId(me.id);
        const [rows, limitDefault] = await Promise.all([fetchAdminUsers(), fetchUploadLimitDefault()]);
        setState({ kind: "ready", rows });
        setUploadLimitDefault(limitDefault);
        setDefaultInput(String(limitDefault));
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

  async function submitRoleChange(target: AdminUserUsage, role: AdminUserRole) {
    setRolePendingId(target.id);
    setRoleNotice(null);
    try {
      const updated = await updateAdminUserRole(target.id, role);
      setState((previous) => previous.kind === "ready"
        ? {
            kind: "ready",
            rows: previous.rows.map((row) => row.id === updated.id
              ? { ...row, role: updated.role }
              : row),
          }
        : previous);
      setConfirmingRole(null);
      setRoleNotice({
        kind: "ok",
        message: role === "admin"
          ? `已授予 ${updated.username} 管理员权限`
          : `已撤销 ${updated.username} 的管理员权限`,
      });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setRoleNotice({ kind: "error", message: toUserMessage(error, "权限更新失败，请稍后重试") });
    } finally {
      setRolePendingId("");
    }
  }

  async function saveDefault() {
    const parsed = parseUploadLimit(defaultInput);
    if (parsed === null) {
      setLimitNotice({ kind: "error", message: "请输入 1 到 100000 之间的整数" });
      return;
    }
    setDefaultSaving(true);
    setLimitNotice(null);
    try {
      const saved = await updateUploadLimitDefault(parsed);
      setUploadLimitDefault(saved);
      setDefaultInput(String(saved));
      // 没有单独设置的用户,有效上限跟随全局默认——就地刷新表格,免整表重取。
      setState((previous) => previous.kind === "ready"
        ? {
            kind: "ready",
            rows: previous.rows.map((row) => row.upload_limit_overridden ? row : { ...row, upload_limit: saved }),
          }
        : previous);
      setLimitNotice({ kind: "ok", message: `已将默认文档上限设为 ${saved}` });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setLimitNotice({ kind: "error", message: toUserMessage(error, "默认文档上限更新失败，请稍后重试") });
    } finally {
      setDefaultSaving(false);
    }
  }

  // limit=null 清除覆盖(回落全局默认);其余是显式覆盖值。镜像 submitRoleChange 的
  // 哨兵分流 + 就地更新对应行 + 人话层错误。
  async function submitLimitChange(target: AdminUserUsage, limit: number | null) {
    setLimitPendingId(target.id);
    setLimitNotice(null);
    try {
      const updated = await updateAdminUserUploadLimit(target.id, limit);
      setState((previous) => previous.kind === "ready"
        ? {
            kind: "ready",
            rows: previous.rows.map((row) => row.id === updated.id
              ? { ...row, upload_limit: updated.upload_limit, upload_limit_overridden: updated.upload_limit_overridden }
              : row),
          }
        : previous);
      setEditingLimitId("");
      setLimitNotice({
        kind: "ok",
        message: updated.upload_limit_overridden
          ? `已将 ${updated.username} 的文档上限设为 ${updated.upload_limit}`
          : `已恢复 ${updated.username} 的文档上限为默认值（${updated.upload_limit}）`,
      });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setLimitNotice({ kind: "error", message: toUserMessage(error, "文档上限更新失败，请稍后重试") });
    } finally {
      setLimitPendingId("");
    }
  }

  function saveLimit(target: AdminUserUsage) {
    const parsed = parseUploadLimit(limitInput);
    if (parsed === null) {
      setLimitNotice({ kind: "error", message: "请输入 1 到 100000 之间的整数" });
      return;
    }
    void submitLimitChange(target, parsed);
  }

  // 镜像 submitRoleChange 的哨兵分流 + 人话层错误;成功后目标用户全部会话被
  // 吊销(纯后端行为,这里只负责收起编辑态并给出提示)。
  async function submitResetPassword(target: AdminUserUsage) {
    if (!resetInput.trim()) {
      setResetNotice({ kind: "error", message: "请输入新密码" });
      return;
    }
    setResetPendingId(target.id);
    setResetNotice(null);
    try {
      const updated = await resetAdminUserPassword(target.id, resetInput);
      setResettingId("");
      setResetInput("");
      setResetNotice({
        kind: "ok",
        message: `已重置 ${updated.username} 的密码，该用户需用新密码重新登录`,
      });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setResetNotice({ kind: "error", message: toUserMessage(error, "密码重置失败，请稍后重试") });
    } finally {
      setResetPendingId("");
    }
  }

  function resetRowInteractions() {
    setExpanded(null);
    setConfirmingRole(null);
    setEditingLimitId("");
    setResettingId("");
    // 收起编辑态时同步清掉已输入的明文新密码,别让凭据在 state 里过夜。
    setResetInput("");
  }

  function changeSort(nextKey: AdminUserSortKey) {
    if (sortKey === nextKey) {
      setSortDirection((previous) => previous === "asc" ? "desc" : "asc");
    } else {
      setSortKey(nextKey);
      setSortDirection("asc");
    }
    setPage(1);
    resetRowInteractions();
  }

  const readyRows = state.kind === "ready" ? state.rows : [];
  const sortedRows = useMemo(
    () => sortAdminUsers(readyRows, sortKey, sortDirection),
    [readyRows, sortKey, sortDirection],
  );
  const pageCount = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = sortedRows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (state.kind === "loading") return <main className="usage-page">加载中…</main>;
  if (state.kind === "forbidden")
    return <main className="usage-page usage-empty">无权限:仅管理员可查看用户使用总览。</main>;
  if (state.kind === "error")
    return <main className="usage-page usage-empty">加载失败:{state.message}</main>;

  function changePage(nextPage: number) {
    setPage(Math.max(1, Math.min(nextPage, pageCount)));
    resetRowInteractions();
  }

  return (
    <main className="usage-page">
      <PageHeader title="用户使用总览" />
      <p className="usage-description">查看用户用量，配置文档数量上限，并授予或撤销管理员权限。</p>
      <div className="usage-settings-bar">
        <label className="usage-settings-label" htmlFor="upload-limit-default">普通用户默认文档上限</label>
        <input
          id="upload-limit-default"
          className="usage-limit-input"
          type="number"
          min={1}
          max={100000}
          value={defaultInput}
          disabled={uploadLimitDefault === null || defaultSaving}
          onChange={(event) => setDefaultInput(event.target.value)}
        />
        <button
          type="button"
          className="usage-role-button usage-role-button-confirm"
          disabled={uploadLimitDefault === null || defaultSaving}
          onClick={() => void saveDefault()}
        >
          {defaultSaving ? "保存中…" : "保存"}
        </button>
        <span className="usage-settings-hint">管理员的笔记本不受限；为某位用户单独设置后，以其设置为准。</span>
      </div>
      {roleNotice && (
        <div className={`usage-role-notice usage-role-notice-${roleNotice.kind}`} role="status">
          {roleNotice.message}
        </div>
      )}
      {limitNotice && (
        <div className={`usage-role-notice usage-role-notice-${limitNotice.kind}`} role="status">
          {limitNotice.message}
        </div>
      )}
      {resetNotice && (
        <div className={`usage-role-notice usage-role-notice-${resetNotice.kind}`} role="status">
          {resetNotice.message}
        </div>
      )}
      <div className="usage-table-wrap">
        <table className="usage-table">
          <thead>
            <tr>
              <th className="usage-expand-col"></th>
              <SortableHeader label="用户名" sortKey="username" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="角色" sortKey="role" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="注册时间" sortKey="created_at" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="笔记本" sortKey="notebooks" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="来源" sortKey="sources" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="提问" sortKey="questions" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="报告" sortKey="reports" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="最近活跃" sortKey="last_active" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <th>日志</th>
              <SortableHeader label="文档上限" sortKey="upload_limit" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <th>密码</th>
              <th>权限管理</th>
            </tr>
          </thead>
          <tbody>
          {visibleRows.map((u) => {
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
                  <td>{u.questions}</td>
                  <td>{u.reports}</td>
                  <td>{formatLastActive(u.last_active)}</td>
                  <td><a href={logsDrillHref(u.id)}>查看日志</a></td>
                  <td className="usage-limit-cell">
                    {u.role === "admin" ? (
                      // 管理员的笔记本豁免(写路径 owner-only ⇒ owner 即当前 admin),显示「不限」不可编辑。
                      <span className="usage-role-locked">不限</span>
                    ) : editingLimitId === u.id ? (
                      <span className="usage-limit-edit">
                        <input
                          className="usage-limit-input"
                          type="number"
                          min={1}
                          max={100000}
                          value={limitInput}
                          disabled={limitPendingId === u.id}
                          aria-label={`${u.username} 的文档上限`}
                          onChange={(event) => setLimitInput(event.target.value)}
                        />
                        <button
                          type="button"
                          className="usage-role-button usage-role-button-confirm"
                          disabled={limitPendingId === u.id}
                          onClick={() => saveLimit(u)}
                        >
                          {limitPendingId === u.id ? "保存中…" : "保存"}
                        </button>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={limitPendingId === u.id}
                          title="恢复为默认文档上限"
                          onClick={() => void submitLimitChange(u, null)}
                        >重置默认</button>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={limitPendingId === u.id}
                          onClick={() => setEditingLimitId("")}
                        >取消</button>
                      </span>
                    ) : (
                      <span className="usage-limit-view">
                        <span className="usage-limit-value">{u.upload_limit}</span>
                        <span className={`usage-limit-tag${u.upload_limit_overridden ? " usage-limit-tag-custom" : ""}`}>
                          {u.upload_limit_overridden ? "自定义" : "默认"}
                        </span>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={Boolean(limitPendingId) || Boolean(editingLimitId)}
                          onClick={() => { setEditingLimitId(u.id); setLimitInput(String(u.upload_limit)); setLimitNotice(null); }}
                        >编辑</button>
                      </span>
                    )}
                  </td>
                  <td className="usage-limit-cell">
                    {u.id === "user-local" ? (
                      <span className="usage-role-locked" title="内置管理员密码由部署配置决定">受保护</span>
                    ) : u.id === currentUserId ? (
                      <span className="usage-role-locked" title="请在主界面头像菜单中修改自己的密码">本人</span>
                    ) : resettingId === u.id ? (
                      <span className="usage-limit-edit">
                        <input
                          className="usage-limit-input"
                          type="password"
                          autoComplete="new-password"
                          value={resetInput}
                          disabled={resetPendingId === u.id}
                          aria-label={`${u.username} 的新密码`}
                          onChange={(event) => setResetInput(event.target.value)}
                        />
                        <button
                          type="button"
                          className="usage-role-button usage-role-button-confirm"
                          disabled={resetPendingId === u.id}
                          onClick={() => void submitResetPassword(u)}
                        >
                          {resetPendingId === u.id ? "重置中…" : "确认重置"}
                        </button>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={resetPendingId === u.id}
                          onClick={() => { setResettingId(""); setResetInput(""); }}
                        >取消</button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="usage-role-button"
                        disabled={Boolean(resetPendingId) || Boolean(resettingId)}
                        onClick={() => { setResettingId(u.id); setResetInput(""); setResetNotice(null); }}
                      >重置密码</button>
                    )}
                  </td>
                  <td>
                    {!u.role_mutable ? (
                      <span className="usage-role-locked">
                        {u.id === currentUserId ? "当前账户" : "受保护"}
                      </span>
                    ) : confirmingRole?.userId === u.id ? (
                      <span className="usage-role-confirm">
                        <button
                          type="button"
                          className="usage-role-button usage-role-button-confirm"
                          disabled={rolePendingId === u.id}
                          onClick={() => void submitRoleChange(u, confirmingRole.role)}
                        >
                          {rolePendingId === u.id ? "更新中…" : "确认"}
                        </button>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={rolePendingId === u.id}
                          onClick={() => setConfirmingRole(null)}
                        >取消</button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="usage-role-button"
                        disabled={Boolean(rolePendingId)}
                        onClick={() => setConfirmingRole({
                          userId: u.id,
                          role: u.role === "admin" ? "user" : "admin",
                        })}
                      >
                        {u.role === "admin" ? "撤销管理员" : "设为管理员"}
                      </button>
                    )}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="usage-subrow">
                    <td colSpan={13}>
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
                              <th>来源</th><th>提问</th><th>报告</th>
                              <th>创建</th><th>最近更新</th>
                            </tr>
                          </thead>
                          <tbody>
                            {entry.map((nb) => (
                              <tr key={nb.id}>
                                <td>{nb.name}</td>
                                <td>{notebookStatusLabel(nb.status)}</td>
                                <td>{nb.sources}</td>
                                <td>{nb.questions}</td>
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
      </div>
      <nav className="usage-pagination" aria-label="用户列表分页">
        <span className="usage-pagination-summary">
          第 {currentPage} / {pageCount} 页，共 {sortedRows.length} 位用户
        </span>
        <label className="usage-page-size-label">
          每页
          <select
            className="usage-page-size-select"
            aria-label="每页用户数"
            value={pageSize}
            onChange={(event) => {
              setPageSize(Number(event.target.value));
              setPage(1);
              resetRowInteractions();
            }}
          >
            {PAGE_SIZE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
          条
        </label>
        <div className="usage-pagination-actions">
          <button type="button" disabled={currentPage === 1} onClick={() => changePage(1)}>首页</button>
          <button type="button" disabled={currentPage === 1} onClick={() => changePage(currentPage - 1)}>上一页</button>
          <button type="button" disabled={currentPage === pageCount} onClick={() => changePage(currentPage + 1)}>下一页</button>
          <button type="button" disabled={currentPage === pageCount} onClick={() => changePage(pageCount)}>末页</button>
        </div>
      </nav>
    </main>
  );
}
