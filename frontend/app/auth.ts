import {
  humanizedError,
  humanizeHttpError,
  readHttpError,
} from "./errors.ts";
import { performApiRequest, requestJson } from "./api-client.ts";
import { normalizeUiMode, type UiMode } from "./ui-mode.ts";

export { API_BASE } from "./api-config.ts";
export { authHeaders, clearToken, getToken, setToken } from "./auth-session.ts";
import { clearToken } from "./auth-session.ts";

export type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  role: string;
  username: string;
  // 旧后端缺这个字段；normalizeUiMode 在每个写入点兜底成 "auto"，所以这里存的
  // 永远是已归一化的合法值，读取侧不必再判空/判非法。
  ui_mode: UiMode;
};

/** fetchMe / updateUiMode 的响应体归一化——两处都要把后端 ui_mode 收敛成合法值。 */
function normalizeAuthUser(raw: AuthUser): AuthUser {
  return { ...raw, ui_mode: normalizeUiMode((raw as { ui_mode?: unknown }).ui_mode) };
}

const USERNAME_RE = /^[a-z]00\d{6}$/;
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
): Promise<{ token: string; user: AuthUser }> {
  const result = await authFetch<{ token: string; user: AuthUser }>("/auth/login", { username, password });
  return { ...result, user: normalizeAuthUser(result.user) };
}

export async function logoutUser(): Promise<void> {
  try {
    await performApiRequest("/auth/logout", { method: "POST", tag: "auth" });
  } catch {
    // Logout is intentionally fail-open: the locally held credential must still be discarded.
  } finally {
    clearToken();
  }
}

export async function fetchMe(): Promise<AuthUser> {
  const user = await requestJson<AuthUser>("/me", { tag: "auth" });
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
