import { humanizeHttpError, readHttpError, throwHumanizedHttpError } from "./errors.ts";

export const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

const TOKEN_KEY = "silicon_notebook_token";

export type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  role: string;
  username: string;
};

const USERNAME_RE = /^[a-z]00\d{6}$/;
export function isValidUsername(username: string): boolean {
  return USERNAME_RE.test((username ?? "").trim());
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}
export function setToken(token: string): void {
  if (typeof window !== "undefined") window.localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  if (typeof window !== "undefined") window.localStorage.removeItem(TOKEN_KEY);
}
export function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function authFetch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // 原始诊断(状态码 + detail + requestId)统一走 errors.ts 进 console。
    const { status, userDetail } = await readHttpError(res, "auth");
    // 登录 401 特化:后端 detail 是「用户名或密码错误」,登录表单上说「不对」
    // 更自然。其余交给共享映射——auth 路由的 4xx detail 恒为中文可操作文案
    // (「用户名已被占用」「密码不能为空」「用户名须为…」),humanizeHttpError
    // 的「中文 detail 透传」规则会原样保留,不需要在这里再写一遍特例。
    throw new Error(status === 401 ? "用户名或密码不对" : humanizeHttpError(status, userDetail));
  }
  return res.json();
}

export async function registerUser(
  username: string,
  password: string
): Promise<{ token: string; user: AuthUser }> {
  return authFetch("/auth/register", { username, password });
}

export async function loginUser(
  username: string,
  password: string
): Promise<{ token: string; user: AuthUser }> {
  return authFetch("/auth/login", { username, password });
}

export async function logoutUser(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, {
    method: "POST",
    headers: authHeaders(),
  }).catch(() => undefined);
  clearToken();
}

export async function fetchMe(): Promise<AuthUser> {
  const res = await fetch(`${API_BASE}/me`, { headers: authHeaders() });
  if (!res.ok) await throwHumanizedHttpError(res, "auth");
  return res.json();
}
