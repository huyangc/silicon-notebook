import { API_BASE } from "./api-config.ts";
import { authHeaders, clearToken, getToken } from "./auth-session.ts";
import { throwHumanizedHttpError } from "./errors.ts";

export type ApiAuth = "required" | "none";

export type ApiRequestOptions = RequestInit & {
  auth?: ApiAuth;
  tag: string;
  unauthorized?: "preserve" | "clear-and-reload";
};

function resolveApiUrl(pathOrUrl: string): string {
  try {
    let base: URL;
    try {
      base = new URL(API_BASE);
    } catch {
      if (typeof window === "undefined" || !window.location.origin) throw new TypeError();
      base = new URL(API_BASE, window.location.origin);
    }
    const basePath = base.pathname === "/" ? "/" : base.pathname.replace(/\/+$/, "");
    const baseDirectory = new URL(base);
    baseDirectory.pathname = basePath === "/" ? "/" : `${basePath}/`;
    baseDirectory.search = "";
    baseDirectory.hash = "";
    const candidate = pathOrUrl.startsWith("/")
      ? new URL(pathOrUrl.slice(1), baseDirectory)
      : new URL(pathOrUrl);
    const isBasePath = candidate.pathname === basePath;
    const isBaseChild = basePath === "/" || candidate.pathname.startsWith(`${basePath}/`);
    if (candidate.origin === base.origin && (isBasePath || isBaseChild)) return candidate.toString();
  } catch {
    // Invalid candidates are confined by the same public TypeError below.
  }
  throw new TypeError("authenticated API requests must stay under API_BASE");
}

export async function performApiRequest(
  pathOrUrl: string,
  options: ApiRequestOptions,
): Promise<Response> {
  const {
    auth = "required",
    tag,
    unauthorized = "preserve",
    headers: inputHeaders,
    ...init
  } = options;
  const headers = new Headers(inputHeaders);
  if (auth === "required") {
    for (const [name, value] of Object.entries(authHeaders())) headers.set(name, value);
  }
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (init.body !== undefined && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const started = globalThis.performance.now();
  const response = await fetch(resolveApiUrl(pathOrUrl), { ...init, headers });
  const elapsed = Math.round(globalThis.performance.now() - started);
  const requestId = response.headers.get("X-Request-Id") || "";
  console.debug(
    `[api] ${(init.method || "GET").toUpperCase()} ${pathOrUrl} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`,
  );
  if (
    auth === "required"
    && unauthorized === "clear-and-reload"
    && response.status === 401
    && getToken()
  ) {
    clearToken();
    if (typeof window !== "undefined") window.location.reload();
  }
  void tag;
  return response;
}

async function checked(path: string, options: ApiRequestOptions): Promise<Response> {
  const response = await performApiRequest(path, options);
  if (!response.ok) await throwHumanizedHttpError(response, options.tag);
  return response;
}

export async function requestJson<T>(path: string, options: ApiRequestOptions): Promise<T> {
  const response = await checked(path, options);
  return response.json() as Promise<T>;
}

export async function requestVoid(path: string, options: ApiRequestOptions): Promise<void> {
  await checked(path, options);
}

export async function requestBlob(path: string, options: ApiRequestOptions): Promise<Blob> {
  return (await checked(path, options)).blob();
}
