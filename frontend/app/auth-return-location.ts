const RETURN_LOCATION_KEY = "silicon_notebook_sso_return_location";
// The sign-in page (`/`) carries the page a rejected session came from here.
const SIGN_IN_RETURN_PARAM = "next";

function localReturnLocation(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u0020\u007f]/.test(value)) return "/";
  try {
    const url = new URL(value, window.location.origin);
    const path = decodeURIComponent(url.pathname);
    if (url.origin !== window.location.origin || path.startsWith("//")
      || /[\\\u0000-\u001f\u007f]/.test(path) || path.replace(/\/{2,}/g, "/").replace(/\/+$/, "") === "/auth/sso/callback") return "/";
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

/** The sign-in page for this page's rejected session, carrying this page (path,
 * query and hash) so that signing in, locally or through unified
 * authentication, comes back here. */
export function signInLocation(): string {
  const here = localReturnLocation(window.location.pathname + window.location.search + window.location.hash);
  return here === "/" ? "/" : `/?${SIGN_IN_RETURN_PARAM}=${encodeURIComponent(here)}`;
}

/** Once signed in on the sign-in page: the same-site page it was reached from,
 * or "" to stay in the workspace. A missing, external or otherwise unusable
 * target is dropped from the address bar, so the workspace URL never keeps it. */
export function takeSignInReturnLocation(): string {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get(SIGN_IN_RETURN_PARAM);
  if (requested === null) return "";
  const target = localReturnLocation(requested);
  if (target !== "/") return target;
  params.delete(SIGN_IN_RETURN_PARAM);
  const query = params.toString();
  window.history.replaceState(window.history.state, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
  return "";
}

/** Leaves this page for a sign-in or return location without keeping it in
 * history. The one navigation seam of the sign-in round trip (jsdom cannot
 * navigate, so tests replace it). */
export function replacePageLocation(location: string): void {
  window.location.replace(location);
}
