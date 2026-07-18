import { humanizeHttpError } from "./errors.ts";

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
    let detail = "";
    try { detail = (await res.json())?.detail ?? ""; } catch { /* noop */ }
    // 原始诊断进 console;面向用户抛人话。
    console.error(`[auth] ${path} -> ${res.status}${detail ? ` ${detail}` : ""}`);
    // auth 路由的 4xx detail 恒为中文且可操作(注册的「用户名已被占用」「密码不能为空」
    // 「用户名须为…」等),优先透传;仅 5xx/空 detail 才泛化。登录 401 特化。
    // ⚠此「透传 detail」只适用 auth.ts(路由 detail 恒中文);page.tsx 的 api() 不可照做
    // (那里多数路由 detail 是英文,透传会把英文漏回给用户)。
    const human =
      res.status === 401
        ? "用户名或密码不对"
        : res.status < 500 && typeof detail === "string" && detail.trim()
          ? detail
          : humanizeHttpError(res.status, detail);
    throw new Error(human);
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
  if (!res.ok) {
    console.error(`[auth] /me -> ${res.status}`);
    throw new Error(humanizeHttpError(res.status));
  }
  return res.json();
}
