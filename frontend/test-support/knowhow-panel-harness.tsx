// Characterization harness for `KnowhowPanel` (debt-fix S4, C0).
//
// The panel is about to be split into several modules (styles, three modals,
// list / grid / row-detail / concept-matrix slices) with **zero** behaviour
// change. This harness gives the C0 characterization suites three observation
// points that do not depend on how the code is organised:
//
//  1. The network contract, observed at the one place every request crosses:
//     `globalThis.fetch`. Each request is recorded as `{ method, path, body }` in
//     issue order, so a suite can pin the exact request sequence (URL, method,
//     body, order) of a scripted scenario. This golden is permanent: it is what
//     the backend sees, and no refactor is allowed to change it.
//  2. React commit counts, from a `<Profiler>` wrapped around the panel. This is
//     a split-period baseline only (see `knowhow-panel-golden.component.test.tsx`).
//  3. The stylesheet text of the panel's single global style block, read from
//     the DOM (never from the source file). Split-period baseline as well.
//
// The fake server answers every request with an in-memory response whose body
// methods resolve on microtasks only, so a scripted scenario is deterministic:
// no wall-clock timer, socket or stream pump decides how React batches updates.
// Unrouted requests are recorded and fail the test in `afterEach` instead of
// silently turning into a component-level error branch.
import { createHash } from "node:crypto";

import { cleanup, getConfig, render } from "@testing-library/react";
import { Profiler, StrictMode, type ReactElement } from "react";
import { afterEach, beforeEach, expect, vi } from "vitest";

import { API_BASE } from "../app/api-config.ts";
import { KnowhowPanel, type KnowhowPanelProps } from "../app/knowhow-panel.tsx";
import type { KnowhowRow, KnowhowTableDetail, KnowhowTableSummary } from "../app/knowhow-model.ts";

// ---------------------------------------------------------------------------
// Wire fixtures
// ---------------------------------------------------------------------------

export function wireRow(row: KnowhowRow) {
  return { id: row.id, position: row.position, projection_status: row.projectionStatus, cells: row.cells };
}

export function wireDetail(detail: KnowhowTableDetail) {
  return {
    id: detail.id,
    title: detail.title,
    description: detail.description,
    anchor_column_id: detail.anchorColumnId,
    columns: detail.columns.map((column) => ({
      id: column.id, name: column.name, position: column.position, kind: column.role,
    })),
    rows: detail.rows.map(wireRow),
  };
}

export function wireSummary(table: Partial<KnowhowTableSummary> & { id: string; title: string }) {
  return {
    id: table.id,
    title: table.title,
    description: table.description ?? "",
    row_count: table.rowCount ?? 0,
    projection_pending: table.projectionPending ?? 0,
    projection_failed: table.projectionFailed ?? 0,
    stale_code_count: table.staleCodeCount ?? 0,
    last_activity_at: table.lastActivityAt ?? "2026-10-01T00:00:00Z",
  };
}

export function wirePatchResult(rowId: string, columnId: string, contentMd: string) {
  return { row_id: rowId, column_id: columnId, content_md: contentMd, projection_status: "pending" };
}

/** A record-style table (no anchor column): two rows, the first fully filled. */
export function recordDetail(overrides: Partial<KnowhowTableDetail> = {}): KnowhowTableDetail {
  return {
    id: "t-rec",
    title: "排障记录",
    description: "电源相关",
    anchorColumnId: null,
    columns: [
      { id: "c-title", name: "问题", role: "attribute", position: 0 },
      { id: "c-fix", name: "处理方法", role: "procedure", position: 1 },
      { id: "c-tool", name: "工具", role: "entity", position: 2 },
    ],
    rows: [
      { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-title": "供电异常", "c-fix": "检查电源", "c-tool": "万用表" } },
      { id: "r2", position: 1, projectionStatus: "failed", cells: { "c-title": "时钟漂移", "c-fix": "", "c-tool": "" } },
    ],
    ...overrides,
  };
}

/** An anchor (concept) table: concept 时钟 has two branches sharing 共同方法. */
export function anchorDetail(overrides: Partial<KnowhowTableDetail> = {}): KnowhowTableDetail {
  return {
    id: "t-anc",
    title: "概念表",
    description: "",
    anchorColumnId: "c-anchor",
    columns: [
      { id: "c-anchor", name: "概念", role: "anchor", position: 0 },
      { id: "c-shared", name: "共同方法", role: "procedure", position: 1 },
      { id: "c-own", name: "分支备注", role: "attribute", position: 2 },
    ],
    rows: [
      { id: "a1", position: 0, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-shared": "检查时钟源", "c-own": "主板" } },
      { id: "a2", position: 1, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-shared": "检查时钟源", "c-own": "" } },
      { id: "a3", position: 2, projectionStatus: "synced", cells: { "c-anchor": "电源", "c-shared": "量电压", "c-own": "单板" } },
    ],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Fake transport
// ---------------------------------------------------------------------------

export type RecordedRequest = { method: string; path: string; body?: unknown };

export type FakeReply =
  | { status?: number; json?: unknown; text?: string }
  /** A request-task NDJSON stream that ends with one `final` frame. */
  | { stream: unknown };

type Responder = FakeReply | ((request: RecordedRequest) => FakeReply);

type Deferred = {
  promise: Promise<FakeReply>;
  resolve: (reply: FakeReply) => void;
};

// The same base the API client resolves against (NEXT_PUBLIC_API_BASE_URL or its
// default), so a developer shell that exports it does not unroute every request.
const API_PREFIX = API_BASE.replace(/\/+$/, "");

function relativePath(url: string): string {
  return url.startsWith(API_PREFIX) ? url.slice(API_PREFIX.length) : url;
}

function recordedBody(body: unknown): unknown {
  if (body === undefined || body === null) return undefined;
  if (typeof body === "string") {
    try {
      return JSON.parse(body);
    } catch {
      return body;
    }
  }
  if (typeof FormData !== "undefined" && body instanceof FormData) {
    return { formData: [...body.keys()] };
  }
  return String(body);
}

function abortError(): DOMException {
  return new DOMException("The operation was aborted.", "AbortError");
}

function fakeResponse(reply: FakeReply): Response {
  const status = "stream" in reply ? 200 : reply.status ?? 200;
  const text = "stream" in reply
    ? `${JSON.stringify({ event: "final", stage: "done", result: reply.stream })}\n`
    : reply.text ?? (reply.json === undefined ? "" : JSON.stringify(reply.json));
  const build = (): Response => {
    let delivered = false;
    const reader = {
      read: async () => {
        if (delivered) return { value: undefined, done: true };
        delivered = true;
        return { value: new TextEncoder().encode(text), done: false };
      },
    };
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      headers: new Headers(),
      json: async () => JSON.parse(text),
      text: async () => text,
      blob: async () => new Blob([text]),
      body: { getReader: () => reader },
      clone: () => build(),
    } as unknown as Response;
  };
  return build();
}

export type FakeKnowhowServer = {
  /** Every request in issue order. */
  log: RecordedRequest[];
  /** Requests no route answered (asserted empty after each test). */
  unrouted: RecordedRequest[];
  /** Persistent answer for `METHOD path` (exact path, query included). */
  on: (method: string, path: string, responder: Responder) => void;
  /** One-shot answer, consumed before the persistent one. */
  once: (method: string, path: string, responder: Responder) => void;
  /** One-shot answer that stays pending until the returned resolver is called. */
  hold: (method: string, path: string) => (reply: FakeReply) => void;
  /** Forget the log (routes stay). */
  clearLog: () => void;
  /** `log` as `"METHOD path"` strings — handy for order assertions. */
  lines: () => string[];
};

export function installFakeKnowhowServer(): FakeKnowhowServer {
  const log: RecordedRequest[] = [];
  const unrouted: RecordedRequest[] = [];
  const persistent = new Map<string, Responder>();
  const oneShot = new Map<string, Array<Responder | Deferred>>();

  const key = (method: string, path: string) => `${method.toUpperCase()} ${path}`;

  const fetchImpl = (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init.method ?? "GET").toUpperCase();
    const request: RecordedRequest = { method, path: relativePath(url) };
    const body = recordedBody(init.body);
    if (body !== undefined) request.body = body;
    log.push(request);
    const signal = init.signal ?? undefined;
    if (signal?.aborted) return Promise.reject(abortError());

    const queue = oneShot.get(key(method, request.path));
    const next = queue && queue.length > 0 ? queue.shift()! : persistent.get(key(method, request.path));
    if (!next) {
      unrouted.push(request);
      return Promise.resolve(fakeResponse({ status: 599, json: { detail: "unrouted in test" } }));
    }
    if (typeof next === "object" && "promise" in next) {
      return new Promise<Response>((resolve, reject) => {
        signal?.addEventListener("abort", () => reject(abortError()), { once: true });
        void next.promise.then((reply) => {
          if (signal?.aborted) return;
          resolve(fakeResponse(reply));
        });
      });
    }
    const reply = typeof next === "function" ? next(request) : next;
    return Promise.resolve(fakeResponse(reply));
  };

  vi.stubGlobal("fetch", vi.fn(fetchImpl));

  return {
    log,
    unrouted,
    on: (method, path, responder) => {
      persistent.set(key(method, path), responder);
    },
    once: (method, path, responder) => {
      const k = key(method, path);
      oneShot.set(k, [...(oneShot.get(k) ?? []), responder]);
    },
    hold: (method, path) => {
      let resolve!: (reply: FakeReply) => void;
      const promise = new Promise<FakeReply>((settle) => {
        resolve = settle;
      });
      const k = key(method, path);
      oneShot.set(k, [...(oneShot.get(k) ?? []), { promise, resolve }]);
      return resolve;
    },
    clearLog: () => {
      log.length = 0;
    },
    lines: () => log.map((entry) => `${entry.method} ${entry.path}`),
  };
}

/** Routes the read endpoints every panel scenario needs. */
export function routeTable(
  server: FakeKnowhowServer,
  notebookId: string,
  detail: KnowhowTableDetail,
  summaries: ReturnType<typeof wireSummary>[] = [wireSummary({ id: detail.id, title: detail.title, rowCount: detail.rows.length })],
): void {
  server.on("GET", `/notebooks/${notebookId}/knowhow`, { json: summaries });
  server.on("GET", `/notebooks/${notebookId}/knowhow/${detail.id}`, { json: wireDetail(detail) });
  for (const row of detail.rows) server.on("GET", `/agent/knowhow/rows/${row.id}`, { json: { code: [] } });
}

// ---------------------------------------------------------------------------
// Console hygiene
// ---------------------------------------------------------------------------

// Without the Next.js styled-jsx transform, `<style jsx global>` reaches React as
// two boolean attributes and React warns once per attribute. That warning is the
// only console.error the panel is allowed to produce in these suites (same
// allowance as knowhow-cell-restore / knowhow-reformat-origin).
function isKnownStyledJsxAttributeWarning(args: unknown[]): boolean {
  const [format, value, attribute] = args;
  return (
    typeof format === "string"
    && format.startsWith("Received `%s` for a non-boolean attribute `%s`.")
    && value === true
    && (attribute === "jsx" || attribute === "global")
  );
}

// The shared error layer (errors.ts readHttpError) writes one diagnostic line per
// failed HTTP response. Scenarios that script a failing response expect it; the
// failing request itself is already pinned by the request-sequence assertion.
function isHttpFailureDiagnostic(args: unknown[]): boolean {
  return typeof args[0] === "string" && /^\[[\w-]+\] [45]\d\d /.test(args[0]);
}

export function captureConsole(): { unexpectedErrors: unknown[][] } {
  const state = { unexpectedErrors: [] as unknown[][] };
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    if (!isKnownStyledJsxAttributeWarning(args) && !isHttpFailureDiagnostic(args)) state.unexpectedErrors.push(args);
  });
  // performApiRequest logs one debug line per request.
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  return state;
}

/**
 * Registers the per-test plumbing every panel characterization suite shares:
 * fresh storages (floating-window geometry and cell drafts live there), a fresh
 * fake server, and the after-test checks (no unrouted request, no unexpected
 * console.error).
 */
export function useKnowhowPanelSuite(): { server: () => FakeKnowhowServer } {
  let server: FakeKnowhowServer;
  let consoleState: { unexpectedErrors: unknown[][] };
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    consoleState = captureConsole();
    server = installFakeKnowhowServer();
  });
  afterEach(() => {
    try {
      expect(server.unrouted).toEqual([]);
      expect(consoleState.unexpectedErrors).toEqual([]);
    } finally {
      cleanup();
      vi.restoreAllMocks();
      vi.unstubAllGlobals();
    }
  });
  return { server: () => server };
}

// ---------------------------------------------------------------------------
// Rendering with a commit counter
// ---------------------------------------------------------------------------

export type PanelProps = KnowhowPanelProps;

export const BASE_PROPS: PanelProps = {
  notebookId: "nb-1",
  apiBase: "http://api.test",
  canEdit: true,
  onClose: () => undefined,
};

export type RenderedPanel = ReturnType<typeof render> & {
  /** Number of React commits of the panel subtree so far. */
  commits: () => number;
  /** Re-render with new props (same tree shape, same Profiler). */
  update: (props: PanelProps) => void;
};

export function renderPanel(props: Partial<PanelProps> = {}, options: { strict?: boolean } = {}): RenderedPanel {
  let commitCount = 0;
  const onRender = () => {
    commitCount += 1;
  };
  const tree = (current: PanelProps): ReactElement => {
    const profiled = (
      <Profiler id="knowhow-panel" onRender={onRender}>
        <KnowhowPanel {...current} />
      </Profiler>
    );
    return options.strict ? <StrictMode>{profiled}</StrictMode> : profiled;
  };
  const result = render(tree({ ...BASE_PROPS, ...props }));
  return {
    ...result,
    commits: () => commitCount,
    update: (next) => result.rerender(tree(next)),
  };
}

/**
 * Yield to the event loop a fixed number of macrotask turns (8 by default) so
 * queued microtasks, passive effects and React scheduler tasks get to run.
 * Runs inside Testing Library's async wrapper (the same one `waitFor` uses), so
 * React schedules work exactly as it does between real user events.
 *
 * It does not detect quiescence; it relies on every scripted step finishing
 * within a few turns, which holds because fake responses resolve on microtasks
 * and no scripted flow sets state from a wall-clock timer. The margin was
 * measured (S4-a quality review): the suites pass unchanged with 1, 2, 4, 8 or
 * 40 turns and with an extra 120 ms sleep, so which step a commit is attributed
 * to does not depend on timing.
 */
export async function settle(turns = 8): Promise<void> {
  await getConfig().asyncWrapper(async () => {
    for (let index = 0; index < turns; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });
}

// ---------------------------------------------------------------------------
// Stylesheet fingerprint
// ---------------------------------------------------------------------------

/** The panel's single global style block, read from the rendered DOM. */
export function panelStyleElement(container: HTMLElement): HTMLStyleElement {
  const section = container.querySelector("section.knowhow-view");
  expect(section).not.toBeNull();
  const styles = section!.querySelectorAll(":scope > style");
  expect(styles).toHaveLength(1);
  return styles[0] as HTMLStyleElement;
}

export function sha256Hex(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}
