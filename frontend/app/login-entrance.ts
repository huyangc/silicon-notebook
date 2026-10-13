const ENTRANCE_KEY = "silicon_notebook_login_entrance";

/** A tab-local visual cue, written only after SSO has actually succeeded. */
export function rememberLoginEntrance(userId: string): void {
  try { window.sessionStorage.setItem(ENTRANCE_KEY, userId); } catch { /* Animation is optional. */ }
}

/** Restoring a normal session must not replay the login animation. */
export function consumeLoginEntrance(userId?: string): boolean {
  try {
    const pending = window.sessionStorage.getItem(ENTRANCE_KEY);
    window.sessionStorage.removeItem(ENTRANCE_KEY);
    return Boolean(userId && pending === userId);
  } catch { return false; }
}
