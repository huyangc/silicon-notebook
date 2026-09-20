const RETURN_LOCATION_KEY = "silicon_notebook_sso_return_location";

function localReturnLocation(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u0020\u007f]/.test(value)) return "/";
  try {
    const url = new URL(value, window.location.origin);
    const path = decodeURIComponent(url.pathname);
    if (url.origin !== window.location.origin || path.startsWith("//")
      || /[\\\u0000-\u001f\u007f]/.test(path) || path.replace(/\/+$/, "") === "/auth/sso/callback") return "/";
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

/** Keep the initiating tab's invite/deep link out of the provider's URL. */
export function saveSsoReturnLocation(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(RETURN_LOCATION_KEY, localReturnLocation(
      window.location.pathname + window.location.search + window.location.hash,
    ));
  } catch { /* Storage may be unavailable; completion safely returns home. */ }
}

/** Consume before navigation; never trust a browser-storage redirect target. */
export function consumeSsoReturnLocation(): string {
  if (typeof window === "undefined") return "/";
  try {
    const value = window.sessionStorage.getItem(RETURN_LOCATION_KEY);
    window.sessionStorage.removeItem(RETURN_LOCATION_KEY);
    return localReturnLocation(value);
  } catch {
    return "/";
  }
}
