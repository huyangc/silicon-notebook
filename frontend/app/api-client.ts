import { API_BASE } from "./api-config.ts";
import { authHeaders, clearRejectedToken, getToken } from "./auth-session.ts";
import { throwHumanizedHttpError } from "./errors.ts";
import { reloadPage } from "./page-reload.ts";

export type ApiAuth = "required" | "none";

export type ApiRequestOptions = RequestInit & {
  auth?: ApiAuth;
  tag: string;
  unauthorized?: "preserve" | "clear-and-reload";
  /**
   * What the debug log prints instead of the real path when a path segment carries
   * content or a credential (a source-derived concept name, a share or invite token,
   * a schema type name): the route with those segments as placeholders, for example
   * `/notebooks/nb-1/kg-analysis/findings/F4/{subject_key}/dismissal`. The request
   * itself always goes to the real path. Query values are never logged either way.
   */
  logPath?: string;
};

/**
 * The request path as the debug log may print it (AGENTS.md: no content in logs): the
 * caller's content-free `logPath` when it has one, and never a query value -- search
 * terms, usernames and the like travel in queries -- only the query keys.
 */
export function loggableRequestPath(pathOrUrl: string, logPath?: string): string {
  const path = logPath ?? pathOrUrl;
  const queryStart = path.indexOf("?");
  if (queryStart < 0) return path;
  const keys = path.slice(queryStart + 1)
    .split("&")
    .filter(Boolean)
    .map((pair) => `${pair.split("=", 1)[0]}=*`);
  return `${path.slice(0, queryStart)}?${keys.join("&")}`;
}

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
    unauthorized = "clear-and-reload",
    headers: inputHeaders,
    logPath,
    ...init
  } = options;
  const headers = new Headers(inputHeaders);
  // A 401 only speaks for the token that was sent: a session switched in the
  // meantime is not cleared by it.
  const sentToken = auth === "required" ? getToken() : null;
  if (auth === "required") {
    for (const [name, value] of Object.entries(authHeaders())) headers.set(name, value);
  }
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (init.body !== undefined && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const started = globalThis.performance.now();
  const response = await fetch(resolveApiUrl(pathOrUrl), { ...init, headers });
  settleResponse(response, {
    method: init.method || "GET", pathOrUrl, logPath, started, auth, unauthorized, sentToken,
  });
  void tag;
  return response;
}

/** The debug line and the 401 policy every transport in this module applies. */
function settleResponse(
  response: Response,
  sent: {
    method: string;
    pathOrUrl: string;
    logPath?: string;
    started: number;
    auth: ApiAuth;
    unauthorized: "preserve" | "clear-and-reload";
    sentToken: string | null;
  },
): void {
  const elapsed = Math.round(globalThis.performance.now() - sent.started);
  const requestId = response.headers.get("X-Request-Id") || "";
  console.debug(
    `[api] ${sent.method.toUpperCase()} ${loggableRequestPath(sent.pathOrUrl, sent.logPath)} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`,
  );
  if (
    sent.auth === "required"
    && sent.unauthorized === "clear-and-reload"
    && response.status === 401
    && sent.sentToken
    && clearRejectedToken(sent.sentToken)
    && typeof window !== "undefined"
  ) {
    reloadPage();
  }
}

export type ApiUploadOptions = {
  tag: string;
  /** The request body is sent as it is, with this `Content-Type`. */
  contentType: string;
  signal?: AbortSignal;
  /** Bytes handed to the network so far, and the total (when the browser knows it). */
  onProgress?: (sent: number, total: number) => void;
};

/**
 * POST a large body with upload progress — the one place `fetch` cannot serve
 * (it has no upload progress event), so it goes through `XMLHttpRequest` while
 * keeping everything else of `performApiRequest`: the URL stays under
 * `API_BASE`, the session header, the debug line (query values never logged)
 * and the 401 policy. Resolves with an ordinary `Response` (any status) so the
 * caller reads failures through `throwHumanizedHttpError` like every other
 * request; rejects with an `AbortError` when `signal` aborts and a `TypeError`
 * when the network fails.
 */
export function performApiUpload(
  path: string,
  body: Blob,
  options: ApiUploadOptions,
): Promise<Response> {
  const { tag, contentType, signal, onProgress } = options;
  const url = resolveApiUrl(path);
  const sentToken = getToken();
  return new Promise<Response>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("The upload was aborted", "AbortError"));
      return;
    }
    const xhr = new XMLHttpRequest();
    const started = globalThis.performance.now();
    const onAbort = () => xhr.abort();
    const done = () => signal?.removeEventListener("abort", onAbort);
    xhr.open("POST", url);
    for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);
    xhr.setRequestHeader("Content-Type", contentType);
    if (onProgress) {
      xhr.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : body.size);
    }
    xhr.onload = () => {
      done();
      const headers = new Headers();
      for (const line of xhr.getAllResponseHeaders().trim().split(/[\r\n]+/)) {
        const colon = line.indexOf(":");
        if (colon > 0) headers.append(line.slice(0, colon).trim(), line.slice(colon + 1).trim());
      }
      // A null-body status may not carry a body, not even an empty string.
      const nullBody = [101, 204, 205, 304].includes(xhr.status);
      const response = new Response(nullBody ? null : xhr.responseText, { status: xhr.status, headers });
      settleResponse(response, {
        method: "POST", pathOrUrl: path, started, auth: "required",
        unauthorized: "clear-and-reload", sentToken,
      });
      resolve(response);
    };
    xhr.onerror = () => {
      done();
      reject(new TypeError("Network request failed"));
    };
    xhr.onabort = () => {
      done();
      reject(new DOMException("The upload was aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort);
    void tag;
    xhr.send(body);
  });
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
