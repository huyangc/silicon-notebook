const TOKEN_KEY = "silicon_notebook_token";
// Shared with every tab through the same storage as the token itself: while one
// tab migrates this session, the server revokes it before the response installs
// the new token, and no tab may treat the resulting 401s as a logout.
const HANDOFF_KEY = "silicon_notebook_session_handoff";
// api-client has no request timeout; the migration request is one short
// transaction (a PBKDF2 check plus a few row updates). One minute bounds how
// long a crashed tab's marker can keep suppressing a genuine 401.
export const SESSION_HANDOFF_MAX_MS = 60_000;

type SessionHandoff = { token: string; expiresAt: number };

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (typeof window !== "undefined") window.localStorage.setItem(TOKEN_KEY, token);
}

/** Explicit sign-out: drops the token and any handoff, so an in-flight
 * migration result is discarded rather than installed over the logout. */
export function clearToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  try {
    window.localStorage.removeItem(HANDOFF_KEY);
  } catch {
    // The token itself is gone; a stale marker expires on its own.
  }
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function readHandoff(): SessionHandoff | null {
  try {
    const raw = window.localStorage.getItem(HANDOFF_KEY);
    const parsed = raw ? JSON.parse(raw) as Partial<SessionHandoff> : null;
    if (parsed && typeof parsed.token === "string" && typeof parsed.expiresAt === "number") {
      return { token: parsed.token, expiresAt: parsed.expiresAt };
    }
  } catch {
    // Unreadable storage protects nothing: callers fall back to the plain 401 rule.
  }
  return null;
}

export function beginSessionHandoff(token: string): void {
  if (typeof window === "undefined" || !token) return;
  try {
    const marker: SessionHandoff = { token, expiresAt: Date.now() + SESSION_HANDOFF_MAX_MS };
    window.localStorage.setItem(HANDOFF_KEY, JSON.stringify(marker));
  } catch {
    // Without storage the handoff is simply unprotected (the previous behaviour).
  }
}

/** Removes the marker only while it still belongs to this token. */
export function endSessionHandoff(token: string): void {
  if (typeof window === "undefined") return;
  if (readHandoff()?.token !== token) return;
  try {
    window.localStorage.removeItem(HANDOFF_KEY);
  } catch {
    // Expiry retires it.
  }
}

export function sessionHandoffActive(token: string): boolean {
  if (typeof window === "undefined" || !token) return false;
  const marker = readHandoff();
  return marker !== null && marker.token === token && marker.expiresAt > Date.now();
}

/** A 401 for `token` clears the session only if it is still the current one
 * and no tab is handing it over. Returns whether the session was cleared. */
export function clearRejectedToken(token: string): boolean {
  if (!token || getToken() !== token || sessionHandoffActive(token)) return false;
  clearToken();
  return true;
}
