import { fetchMe, type AuthUser } from "./auth.ts";
import { clearRejectedToken, getToken } from "./auth-session.ts";
import { replacePageLocation, signInLocation } from "./auth-return-location.ts";
import { httpErrorStatus } from "./errors.ts";

/**
 * The signed-in user for every session check except the workspace's own startup
 * at `/`, which restores its session itself; the global Q&A window embedded in
 * the workspace uses it too, and returns to the workspace location after
 * signing in. A rejected session (401: expired, revoked, or a local session
 * after unified authentication was switched on) clears only the token that was
 * sent, so a session switched in another tab meanwhile is kept, and leaves for
 * the sign-in page, which returns here once signed in. The promise then never
 * settles: the page keeps its loading state while it navigates away instead of
 * showing the rejection as an error. Any other failure (5xx, network) is
 * rethrown for the page's own error and retry, and leaves the credential alone.
 */
export async function fetchPageUser(): Promise<AuthUser> {
  let sentToken = "";
  try {
    sentToken = getToken();
  } catch {
    // Blocked storage holds no session; the request reports whatever it meets.
  }
  try {
    return await fetchMe();
  } catch (error) {
    if (httpErrorStatus(error) !== 401) throw error;
    clearRejectedToken(sentToken);
    replacePageLocation(signInLocation());
    return new Promise<never>(() => undefined);
  }
}

/** A read a page starts alongside its session check: its failure surfaces only
 * once `session` has succeeded. A rejected session fails every read at once, and
 * the page should go to sign-in without first showing a read's own rejection. */
export function behindSessionCheck<T>(session: Promise<AuthUser>, read: Promise<T>): Promise<T> {
  return read.catch(async (error: unknown) => {
    await session;
    throw error;
  });
}
