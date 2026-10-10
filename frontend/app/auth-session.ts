const TOKEN_KEY = "silicon_notebook_token";

// The token this tab last wrote or subscribed with. `storage` events fire only
// in other tabs, so this is what another tab's write is compared against.
let pageToken: string | null = null;

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
  pageToken = token;
}

/** Explicit sign-out: drops the token. */
export function clearToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  pageToken = "";
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Cross-tab session sync: when another tab replaces or clears the token (sign
 * in/out, unified-authentication sign-in), this tab's user and actor-owned
 * state are stale, so `onChange` runs (the page reloads and restores from the
 * new token). Other keys are ignored. Returns an unsubscribe.
 */
export function subscribeTokenChanges(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  try {
    pageToken = getToken();
  } catch {
    return () => undefined;
  }
  function handle(event: StorageEvent) {
    if (event.key !== TOKEN_KEY && event.key !== null) return;
    let next: string;
    try {
      next = event.key === null ? getToken() : event.newValue ?? "";
    } catch {
      return;
    }
    if (next === pageToken) return;
    pageToken = next;
    onChange();
  }
  window.addEventListener("storage", handle);
  return () => window.removeEventListener("storage", handle);
}

/** A 401 for `token` clears the session only if it is still the current one
 * (a session switched in the meantime is not cleared by it). Returns whether
 * the session was cleared. */
export function clearRejectedToken(token: string): boolean {
  if (!token || getToken() !== token) return false;
  clearToken();
  return true;
}
