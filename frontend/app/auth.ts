import {
  humanizedError,
  humanizeHttpError,
  readHttpError,
  throwHumanizedHttpError,
} from "./errors.ts";
import { performApiRequest, requestJson } from "./api-client.ts";
import { normalizeUiMode, type UiMode } from "./ui-mode.ts";
import type { SearchProfilePatchBody } from "./search-profile-model.ts";

export { API_BASE } from "./api-config.ts";
export { authHeaders, clearToken, getToken, setToken } from "./auth-session.ts";
import { clearToken } from "./auth-session.ts";
import { saveSsoReturnLocation } from "./auth-return-location.ts";

export type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  role: string;
  username: string;
  // 旧后端缺这个字段；normalizeUiMode 在每个写入点兜底成 "auto"，所以这里存的
  // 永远是已归一化的合法值，读取侧不必再判空/判非法。
  ui_mode: UiMode;
  // Agentic Memory P3（T6/T9）：检索/回答风格偏好文档。`unknown`——这是后端
  // `Dict[str, Any]` 的宽松 wire 类型（`{version, fields: {field: {value,
  // origin, updated_at}}}` 或 `null`），消费方必须经
  // `search-profile-model.ts` 的 `parseSearchProfile` 防御性解析，不能在这里
  // 假装它是已经校验过的强类型。
  search_profile: unknown;
};

export type AuthMode = "local" | "dual" | "binding_required" | "sso_only" | "retired";

/** Public, deployment-selected authentication surface.  The provider's private
 * protocol never crosses this boundary; the browser only receives a generic
 * capability and an optional display label for assistive text. */
export type AuthCapabilities = {
  mode: AuthMode;
  local_login: boolean;
  local_registration: boolean;
  sso_login: boolean;
  binding_allowed: boolean;
  provider_label: string;
};

export type IdentityInfo = {
  linked: boolean;
  local_login_name: string | null;
  external_username: string | null;
  display_name: string | null;
};

export type SsoCompletion =
  | { status: "authenticated"; token: string; user: AuthUser }
  | {
    status: "binding_required";
    pending_id: string;
    local_login_name: string;
    external_username: string;
    display_name: string;
  }
  | {
    status: "confirmation_required";
    pending_id: string;
    external_username: string;
    display_name: string;
    purpose: "enroll" | "recover" | "replace";
    target_user_id: string | null;
    target_username: string | null;
    previous_external_username?: string | null;
  };

export const LOCAL_AUTH_CAPABILITIES: AuthCapabilities = {
  mode: "local",
  local_login: true,
  local_registration: true,
  sso_login: false,
  binding_allowed: false,
  provider_label: "",
};

/** fetchMe / updateUiMode 的响应体归一化——两处都要把后端 ui_mode 收敛成合法值。 */
function normalizeAuthUser(raw: AuthUser): AuthUser {
  return { ...raw, ui_mode: normalizeUiMode((raw as { ui_mode?: unknown }).ui_mode) };
}

const USERNAME_RE = /^[a-z][0-9]{8}$/;
export function isValidUsername(username: string): boolean {
  return USERNAME_RE.test((username ?? "").trim());
}

async function authFetch<T>(path: string, body: unknown): Promise<T> {
  const res = await performApiRequest(path, {
    auth: "none",
    tag: "auth",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // 原始诊断(状态码 + detail + requestId)统一走 errors.ts 进 console。
    const { status, userDetail, trusted } = await readHttpError(res, "auth");
    // 登录 401 特化:后端 detail 是「用户名或密码错误」,登录表单上说「不对」
    // 更自然。其余交给共享映射——auth 路由的这些 4xx 都经后端 user_error()
    // 盖了章(「用户名已被占用」「密码不能为空」「用户名须为…」),
    // humanizeHttpError 凭 trusted 原样保留,不需要在这里再写一遍特例。
    throw humanizedError(
      status === 401 ? "用户名或密码不对" : humanizeHttpError(status, userDetail, trusted)
    );
  }
  return res.json();
}

export async function registerUser(
  username: string,
  password: string
): Promise<{ token: string; user: AuthUser }> {
  const result = await authFetch<{ token: string; user: AuthUser }>("/auth/register", { username, password });
  return { ...result, user: normalizeAuthUser(result.user) };
}

export async function loginUser(
  username: string,
  password: string
): Promise<{ token: string; user: AuthUser; migration_required?: boolean }> {
  const result = await authFetch<{ token: string; user: AuthUser; migration_required?: boolean }>("/auth/login", { username, password });
  return { ...result, user: normalizeAuthUser(result.user) };
}

export async function fetchAuthCapabilities(): Promise<AuthCapabilities> {
  const result = await requestJson<AuthCapabilities>("/auth/capabilities", {
    auth: "none",
    tag: "auth",
    credentials: "include",
  });
  const modes: readonly AuthMode[] = ["local", "dual", "binding_required", "sso_only", "retired"];
  if (!modes.includes(result.mode)
    || typeof result.local_login !== "boolean"
    || typeof result.local_registration !== "boolean"
    || typeof result.sso_login !== "boolean"
    || typeof result.binding_allowed !== "boolean"
    || typeof result.provider_label !== "string") {
    throw new TypeError("authentication capabilities response is invalid");
  }
  return result;
}

async function ssoStart(path: string, body: unknown): Promise<string> {
  saveSsoReturnLocation();
  const result = await requestJson<{ authorization_url: string }>(path, {
    tag: "auth",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  if (!result.authorization_url) throw new TypeError("authentication redirect is missing");
  return result.authorization_url;
}

/** Starts a browser-bound external sign-in.  It deliberately returns only an
 * authorization URL; identity tokens remain in the server/plugin boundary. */
export function startSsoLogin(): Promise<string> {
  return ssoStart("/auth/sso/start", {});
}

export function startIdentityBinding(currentPassword: string): Promise<string> {
  return ssoStart("/me/identity-binding/start", { current_password: currentPassword });
}

/** A deployment-issued, scoped one-time grant permits only enrollment or
 * recovery.  The browser never submits a target user ID. */
export function startSsoGrant(grantToken: string, purpose: "enroll" | "recover" | "replace"): Promise<string> {
  return ssoStart("/auth/sso/grant/start", { grant_token: grantToken, purpose });
}

export async function completeSsoLogin(code: string): Promise<SsoCompletion> {
  const result = await requestJson<SsoCompletion>("/auth/sso/complete", {
    auth: "none",
    tag: "auth",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
    credentials: "include",
  });
  if (result.status === "authenticated") {
    return { ...result, user: normalizeAuthUser(result.user) };
  }
  return result;
}

export async function confirmIdentityBinding(pendingId: string): Promise<{ token: string; user: AuthUser }> {
  const result = await requestJson<{ token: string; user: AuthUser }>("/me/identity-binding/confirm", {
    tag: "auth",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pending_id: pendingId }),
    credentials: "include",
  });
  return { ...result, user: normalizeAuthUser(result.user) };
}

export async function cancelIdentityBinding(pendingId: string): Promise<void> {
  const res = await performApiRequest("/me/identity-binding/cancel", {
    tag: "auth",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pending_id: pendingId }),
    credentials: "include",
  });
  if (!res.ok) await throwHumanizedHttpError(res, "auth");
}

export function fetchMyIdentities(): Promise<IdentityInfo> {
  return requestJson<IdentityInfo>("/me/identities", { tag: "auth", credentials: "include", unauthorized: "preserve" });
}

export async function logoutUser(): Promise<void> {
  try {
    await performApiRequest("/auth/logout", { method: "POST", tag: "auth", credentials: "include" });
  } catch {
    // Logout is intentionally fail-open: the locally held credential must still be discarded.
  } finally {
    clearToken();
  }
}

/** 自助修改密码;成功后当前会话保留,其他会话被吊销。错误一律走人话层
 * (与 authFetch 不同,这里不特化 401,直接复用共享的 throwHumanizedHttpError)。 */
export async function changeMyPassword(oldPassword: string, newPassword: string): Promise<void> {
  const res = await performApiRequest("/me/password", {
    tag: "auth",
    method: "PATCH",
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  if (!res.ok) await throwHumanizedHttpError(res, "auth");
}

export async function fetchMe(): Promise<AuthUser> {
  // Startup needs to distinguish an S2 migration token from an expired SSO
  // session before the global 401 policy clears browser state.
  const user = await requestJson<AuthUser>("/me", { tag: "auth", unauthorized: "preserve" });
  return normalizeAuthUser(user);
}

/** PATCH /me/ui-mode——切换自动/高级界面模式，返回更新后的完整用户档案。 */
export async function updateUiMode(mode: UiMode): Promise<AuthUser> {
  const user = await requestJson<AuthUser>("/me/ui-mode", {
    tag: "auth",
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ui_mode: mode }),
  });
  return normalizeAuthUser(user);
}

/** PATCH /me/search-profile——自助编辑「我的回答偏好」，返回更新后的完整用户
 * 档案（同 updateUiMode 同格）。`body` 由 `search-profile-model.ts` 的
 * `buildSearchProfilePatch` 构造，未触碰的字段整体不出现在 JSON 里；总闸关闭
 * 时后端回 409，由调用方经既有人话层展示。 */
export async function patchSearchProfile(body: SearchProfilePatchBody): Promise<AuthUser> {
  const user = await requestJson<AuthUser>("/me/search-profile", {
    tag: "auth",
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return normalizeAuthUser(user);
}
