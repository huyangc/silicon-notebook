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
    // 原始诊断进 console;面向用户抛人话。登录/注册的 401 特化为「用户名或密码不对」。
    console.error(`[auth] ${path} -> ${res.status}${detail ? ` ${detail}` : ""}`);
    throw new Error(res.status === 401 ? "用户名或密码不对" : humanizeHttpError(res.status, detail));
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
