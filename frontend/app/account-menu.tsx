import { useEffect, useRef, useState } from "react";
import {
  BarChart3,
  Bookmark,
  Bot,
  ChevronDown,
  KeyRound,
  LogOut,
  Puzzle,
  HeartHandshake,
  MessagesSquare,
  SlidersHorizontal,
  Users,
  Wand2,
} from "lucide-react";


type AccountMenuProps = {
  username: string;
  role: string;
  initials: string;
  memoryActive: boolean;
  showAdminUsage: boolean;
  /** 内置管理员(密码由部署配置派生)不显示「修改密码」入口——后端对它一律 409。 */
  canChangePassword: boolean;
  /** 当前是否为高级模式；开关渲染其开/关态，点击调用 onToggleAdvancedMode。 */
  advancedMode: boolean;
  /** 「我的回答偏好」入口的部署总闸（/system/config 的
   *  user_search_profile_enabled，缺失按 true）；关闭时整个菜单项不渲染。 */
  searchProfileEnabled: boolean;
  /** 管理员「提问分析」入口的部署能力位。 */
  activityViewEnabled: boolean;
  onOpenMemory: () => void;
  /** 打开独立群组管理页。任何登录用户都可用（项目组人人可建）。 */
  onOpenGroups: () => void;
  onToggleAdvancedMode: () => void;
  onOpenSearchProfile: () => void;
  onChangePassword: () => void;
  /** S1 已登录用户可在此处再次验证本地密码，发起统一身份关联。 */
  canBindIdentity: boolean;
  linkedIdentityName: string | null;
  onStartIdentityBinding: (currentPassword: string) => Promise<void>;
  onLogout: () => void | Promise<void>;
};


export function AccountMenu({
  username,
  role,
  initials,
  memoryActive,
  showAdminUsage,
  canChangePassword,
  advancedMode,
  searchProfileEnabled,
  activityViewEnabled,
  onOpenMemory,
  onOpenGroups,
  onToggleAdvancedMode,
  onOpenSearchProfile,
  onChangePassword,
  canBindIdentity,
  linkedIdentityName,
  onStartIdentityBinding,
  onLogout,
}: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const [bindingOpen, setBindingOpen] = useState(false);
  const [bindingPassword, setBindingPassword] = useState("");
  const [bindingError, setBindingError] = useState("");
  const [bindingBusy, setBindingBusy] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;

    function close() {
      setOpen(false);
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && menuRef.current?.contains(target)) {
        return;
      }
      close();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") close();
    }

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const accountRole = role === "admin" ? "管理员" : "用户";

  async function beginIdentityBinding() {
    if (!bindingPassword) {
      setBindingError("请输入当前密码");
      return;
    }
    setBindingError("");
    setBindingBusy(true);
    try {
      await onStartIdentityBinding(bindingPassword);
    } catch {
      setBindingError("无法发起关联，请稍后重试");
      setBindingBusy(false);
    }
  }

  return (
    <div className="user-menu" ref={menuRef}>
      <button
        aria-label="账户菜单"
        className="user-menu-trigger"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        title="账户菜单"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="user-avatar">{initials}</span>
        <span className="user-name">
          {username}{role === "admin" ? "（管理员）" : ""}
        </span>
        <ChevronDown size={14} className="user-menu-chevron" />
      </button>
      {open && (
        <div className="user-menu-popover" role="menu" aria-label="账户菜单">
          <div className="user-menu-profile">
            <span className="user-avatar large">{initials}</span>
            <div>
              <strong>{username}</strong>
              <small>{accountRole}</small>
            </div>
          </div>
          <button
            className={`user-logout ${memoryActive ? "active" : ""}`}
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onOpenMemory();
            }}
          >
            <Bookmark size={16} />
            <span>私有记忆</span>
          </button>
          <a
            className="user-logout"
            role="menuitem"
            href="/agents"
            title="为 Claude Code、Codex 等客户端签发 MCP token，或修改已签发 token 的权限"
          >
            <Bot size={16} />
            <span>Agent 接入</span>
          </a>
          <a
            className="user-logout"
            role="menuitem"
            href="/wishes"
            title="提交问题、功能需求或查看更新计划"
          >
            <HeartHandshake size={16} />
            <span>许愿墙</span>
          </a>
          <button
            className="user-logout"
            type="button"
            role="menuitem"
            title="管理群组成员，并查看共享给群组的知识库"
            onClick={() => {
              setOpen(false);
              onOpenGroups();
            }}
          >
            <Users size={16} />
            <span>群组</span>
          </button>
          <button
            className={`user-logout user-menu-toggle ${advancedMode ? "active" : ""}`}
            type="button"
            role="menuitemcheckbox"
            aria-checked={advancedMode}
            title="高级模式会显示引擎切换、检索档位、研究深度与来源范围勾选等完整配置项"
            onClick={() => {
              setOpen(false);
              onToggleAdvancedMode();
            }}
          >
            <SlidersHorizontal size={16} />
            <span>高级模式</span>
            <span className="user-menu-state">{advancedMode ? "已开启" : "已关闭"}</span>
          </button>
          {searchProfileEnabled && (
            <button
              className="user-logout"
              type="button"
              role="menuitem"
              title="设置回答的语言、组织方式、详略与常用术语"
              onClick={() => {
                setOpen(false);
                onOpenSearchProfile();
              }}
            >
              <Wand2 size={16} />
              <span>我的回答偏好</span>
            </button>
          )}
          {canChangePassword && (
            <button
              className="user-logout"
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onChangePassword();
              }}
            >
              <KeyRound size={16} />
              <span>修改密码</span>
            </button>
          )}
          {canBindIdentity && (
            <div className="identity-binding-menu-item">
              <button
                className="user-logout"
                type="button"
                role="menuitem"
                aria-expanded={bindingOpen}
                onClick={() => {
                  setBindingOpen((value) => !value);
                  setBindingError("");
                }}
              >
                <KeyRound size={16} />
                <span>关联统一身份</span>
              </button>
              {bindingOpen && (
                <form className="identity-binding-form" onSubmit={(event) => { event.preventDefault(); void beginIdentityBinding(); }}>
                  <p>请验证当前密码，随后将跳转到统一登录页面确认身份。</p>
                  <label>当前密码
                    <input type="password" autoComplete="current-password" value={bindingPassword} disabled={bindingBusy}
                      onChange={(event) => setBindingPassword(event.target.value)} />
                  </label>
                  {bindingError && <p className="identity-binding-error" role="alert">{bindingError}</p>}
                  <div className="identity-binding-actions">
                    <button type="button" disabled={bindingBusy} onClick={() => { setBindingOpen(false); setBindingPassword(""); setBindingError(""); }}>取消</button>
                    <button type="submit" disabled={bindingBusy}>{bindingBusy ? "正在跳转…" : "验证并继续"}</button>
                  </div>
                </form>
              )}
            </div>
          )}
          {linkedIdentityName && (
            <div className="identity-linked-status" role="status">已关联统一身份：{linkedIdentityName}</div>
          )}
          {showAdminUsage && (
            <a
              className="user-logout"
              role="menuitem"
              href="/admin/usage"
              title="用户使用总览"
            >
              <BarChart3 size={16} />
              <span>用户总览</span>
            </a>
          )}
          {showAdminUsage && (
            <a className="user-logout" role="menuitem" href="/admin/auth" title="查看认证迁移预检、策略、账号与迁移凭证">
              <KeyRound size={16} />
              <span>认证迁移</span>
            </a>
          )}
          {showAdminUsage && activityViewEnabled && (
            <a
              className="user-logout"
              role="menuitem"
              href="/admin/questions"
              title="跨用户查看问答与深度报告中的提问"
            >
              <MessagesSquare size={16} />
              <span>提问分析</span>
            </a>
          )}
          {/* 管理员页面共用 showAdminUsage / canSeeAdminUsage 角色判据；提问分析
              还需匹配后端 USER_ACTIVITY_VIEW_ENABLED 能力位。 */}
          {showAdminUsage && (
            <a
              className="user-logout"
              role="menuitem"
              href="/admin/extensions"
              title="查看这个服务已经装入的扩展"
            >
              <Puzzle size={16} />
              <span>已加载的扩展</span>
            </a>
          )}
          <button
            className="user-logout"
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              void onLogout();
            }}
          >
            <LogOut size={16} />
            <span>退出登录</span>
          </button>
        </div>
      )}
    </div>
  );
}
