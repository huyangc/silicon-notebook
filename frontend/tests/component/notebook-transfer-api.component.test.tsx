import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  deleteNotebookTransfer,
  downloadNotebookExport,
  getNotebookTransfer,
  importedNotebookReady,
  parseTransferJob,
  transferDownloadHref,
  transferFailureText,
  transferProgressText,
  transferRequestErrorText,
  uploadNotebookImport,
  EXPORT_START_ERRORS,
  IMPORT_UPLOAD_ERRORS,
  startNotebookExport,
  ARCHIVE_TOO_NEW_TEXT,
  DOWNLOAD_FRAME_HOLD_MS,
  UPLOAD_IN_PROGRESS_TEXT,
  uploadStillInProgress,
  REPAIR_HINT,
  FOLLOWUP_GENERIC,
  importReportNotes,
} from "../../app/notebook-transfer-api.ts";
import { CHECKUP_FIX } from "../../app/vocabulary.ts";

// 网页导出 / 导入的网络客户端:票据 → 原生下载导航、上传(带进度)、DELETE 结果、
// 错误码 → 中文。fetch 与 XMLHttpRequest 都在这里换成假的,断言真正发出去的东西。

const TICKET = "s3cr3t-ticket-value";

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function job(over: Record<string, unknown> = {}) {
  return {
    id: "xfer-1", kind: "export", state: "running", phase: "writing", notebook_id: "nb1",
    notebook_name: "封装工艺库", created_at: "2026-10-28T01:00:00+00:00", updated_at: "",
    finished_at: null, expires_at: null, progress: { bytes: 3 * 1024 * 1024 }, archive_bytes: 0,
    downloadable: false, downloaded: false, report: null, reembed: null, index: null, error: null,
    ...over,
  };
}

beforeEach(() => {
  window.localStorage.setItem("silicon_notebook_token", "tok-1");
});
afterEach(() => {
  window.localStorage.clear();
});

test("票据 URL 按 API_BASE 改写:同源 /api 与前后端分开部署两种形状", () => {
  const url = `/api/notebook-transfers/download?ticket=${TICKET}`;
  expect(transferDownloadHref(url, "/api")).toBe(`/api/notebook-transfers/download?ticket=${TICKET}`);
  expect(transferDownloadHref(url, "http://10.0.0.5:8000/api")).toBe(
    `http://10.0.0.5:8000/api/notebook-transfers/download?ticket=${TICKET}`,
  );
  expect(transferDownloadHref(url, "http://10.0.0.5:8000/api/")).toBe(
    `http://10.0.0.5:8000/api/notebook-transfers/download?ticket=${TICKET}`,
  );
  expect(transferDownloadHref(url, "/sub/api")).toBe(`/sub/api/notebook-transfers/download?ticket=${TICKET}`);
  // 形状不对绝不导航(服务端只发这一条根相对路径)。
  expect(() => transferDownloadHref("https://evil.example/x?ticket=1")).toThrow("下载没有开始");
  expect(() => transferDownloadHref("/api/notebooks/nb1")).toThrow("下载没有开始");
});

test("下载:取一张票据,在隐藏 iframe 里导航过去(绝不离开当前页),不读成 Blob,也不把票据写进日志", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => (
    json({ url: `/api/notebook-transfers/download?ticket=${TICKET}`, expires_at: "2026-10-28T01:01:00+00:00" })
  ));
  const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
  const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");
  const before = window.location.href;
  const blob = vi.spyOn(Response.prototype, "blob");

  await downloadNotebookExport("xfer-1");

  expect(fetchMock).toHaveBeenCalledOnce();
  const [requested, init] = fetchMock.mock.calls[0];
  expect(String(requested)).toBe("http://127.0.0.1:8000/api/notebook-transfers/xfer-1/download-ticket");
  expect(init?.method).toBe("POST");
  const frames = document.querySelectorAll("iframe");
  expect(frames).toHaveLength(1);
  expect(frames[0].getAttribute("src")).toBe(
    `http://127.0.0.1:8000/api/notebook-transfers/download?ticket=${TICKET}`,
  );
  expect(frames[0].style.display).toBe("none");
  expect(frames[0].getAttribute("aria-hidden")).toBe("true");
  // 当前页不导航:不点锚点、不改 location。
  expect(anchorClick).not.toHaveBeenCalled();
  expect(window.location.href).toBe(before);
  expect(blob).not.toHaveBeenCalled();
  for (const call of debug.mock.calls) expect(call.join(" ")).not.toContain(TICKET);
  // 被拒的那一页加载完,iframe 连同地址一起拿掉。
  frames[0].dispatchEvent(new Event("load"));
  expect(document.querySelector("iframe")).toBeNull();
  // 新的一次下载不拿掉上一次的(那可能正在下载):每个只在自己加载完或到点时拿掉自己。
  await downloadNotebookExport("xfer-1");
  vi.advanceTimersByTime(60_000);
  await downloadNotebookExport("xfer-1");
  expect(document.querySelectorAll("iframe")).toHaveLength(2);
  expect(DOWNLOAD_FRAME_HOLD_MS).toBeGreaterThanOrEqual(5 * 60_000);
  vi.advanceTimersByTime(DOWNLOAD_FRAME_HOLD_MS - 60_000 + 1);
  expect(document.querySelectorAll("iframe")).toHaveLength(1);
  vi.advanceTimersByTime(60_000);
  expect(document.querySelector("iframe")).toBeNull();
  vi.useRealTimers();
});

test("下载:票据请求失败时不导航,错误带状态码", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "导出还没有完成，暂时不能下载。" }, 409, { "X-User-Message": "1" }));
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const error = await downloadNotebookExport("xfer-1").catch((caught: unknown) => caught);
  expect(document.querySelector("iframe")).toBeNull();
  expect(transferRequestErrorText(error, {}, "x")).toBe("导出还没有完成，暂时不能下载。");
});

type FakeXhr = {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
  upload: { onprogress: ((event: ProgressEvent) => void) | null };
  respond: (status: number, body: string, headers?: string) => void;
  fail: () => void;
  aborted: boolean;
};

function installFakeXhr(): FakeXhr[] {
  const created: FakeXhr[] = [];
  class Xhr {
    method = "";
    url = "";
    headers: Record<string, string> = {};
    body: unknown = null;
    status = 0;
    responseText = "";
    rawHeaders = "";
    aborted = false;
    upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    constructor() {
      created.push(this as unknown as FakeXhr);
    }
    open(method: string, url: string) { this.method = method; this.url = url; }
    setRequestHeader(name: string, value: string) { this.headers[name] = value; }
    getAllResponseHeaders() { return this.rawHeaders; }
    send(body: unknown) { this.body = body; }
    abort() { this.aborted = true; this.onabort?.(); }
    respond(status: number, body: string, headers = "content-type: application/json\r\n") {
      this.status = status;
      this.responseText = body;
      this.rawHeaders = headers;
      this.onload?.();
    }
    fail() { this.onerror?.(); }
  }
  vi.stubGlobal("XMLHttpRequest", Xhr);
  return created;
}

test("上传:原始 application/zip 请求体、带会话头、名字进 query、进度回调、202 拿回作业", async () => {
  const xhrs = installFakeXhr();
  const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
  const progress = vi.fn();
  const file = new File([new Uint8Array(10)], "库.snbk");
  const pending = uploadNotebookImport(file, { name: "  新名字 ", uploadId: "up-test-0001", onProgress: progress });
  const xhr = xhrs[0];
  expect(xhr.method).toBe("POST");
  expect(xhr.url).toBe(`http://127.0.0.1:8000/api/notebook-imports?name=${encodeURIComponent("新名字").replace(/%20/g, "+")}`);
  expect(xhr.headers["Content-Type"]).toBe("application/zip");
  expect(xhr.headers.Authorization).toBe("Bearer tok-1");
  // 上传标识走请求头(不进 query、不进日志)。
  expect(xhr.headers["X-Upload-Id"]).toBe("up-test-0001");
  expect(xhr.body).toBe(file);
  xhr.upload.onprogress?.({ loaded: 4, total: 10, lengthComputable: true } as ProgressEvent);
  expect(progress).toHaveBeenCalledWith(4, 10);
  xhr.respond(202, JSON.stringify(job({ kind: "import", state: "queued", phase: "queued", notebook_id: "" })));
  const created = await pending;
  expect(created.kind).toBe("import");
  expect(created.state).toBe("queued");
  // 日志里只有路径和 query 的键,没有名字。
  expect(debug.mock.calls.map((call) => call.join(" ")).join("\n")).toContain("/notebook-imports?name=*");
  expect(debug.mock.calls.map((call) => call.join(" ")).join("\n")).not.toContain("新名字");
});

test("上传失败:后端写给用户的中文(409 文档上限 / 名额满)原样给出;拿不到出处头时按状态码给清楚的一句", async () => {
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const file = new File([new Uint8Array(3)], "a.snbk");
  const cases: Array<[number, string, string, string]> = [
    [409, "这个文件的文档超过每本笔记本最多 50 篇的上限；请联系管理员用命令行导入。", "x-user-message: 1\r\n",
      "这个文件的文档超过每本笔记本最多 50 篇的上限；请联系管理员用命令行导入。"],
    [409, "你已有 2 个导出或导入正在进行，请等其中一个完成后再试。", "x-user-message: 1\r\n",
      "你已有 2 个导出或导入正在进行，请等其中一个完成后再试。"],
    [409, "anything", "", IMPORT_UPLOAD_ERRORS[409] ?? ""],
    [413, "anything", "", IMPORT_UPLOAD_ERRORS[413] ?? ""],
    [415, "anything", "", IMPORT_UPLOAD_ERRORS[415] ?? ""],
    [400, "anything", "", IMPORT_UPLOAD_ERRORS[400] ?? ""],
  ];
  for (const [status, detail, headers, expected] of cases) {
    const xhrs = installFakeXhr();
    const pending = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001" }).catch((caught: unknown) => caught);
    xhrs[0].respond(status, JSON.stringify({ detail }), headers);
    const error = await pending;
    expect(transferRequestErrorText(error, IMPORT_UPLOAD_ERRORS, "fallback")).toBe(expected);
  }
  expect(IMPORT_UPLOAD_ERRORS[409]).toContain("请联系管理员用命令行导入");
  expect(IMPORT_UPLOAD_ERRORS[409]).toContain("请等其中一个完成");
});

test("上传:更新版本的部署写的文件(400 + archive_too_new)说「请先升级本部署」,不说文件坏了", async () => {
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const file = new File([new Uint8Array(3)], "a.snbk");
  const xhrs = installFakeXhr();
  const newer = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001" }).catch((caught: unknown) => caught);
  xhrs[0].respond(400, JSON.stringify({ detail: { code: "archive_too_new", message: "x" } }));
  expect(transferRequestErrorText(await newer, IMPORT_UPLOAD_ERRORS)).toBe(ARCHIVE_TOO_NEW_TEXT);
  expect(ARCHIVE_TOO_NEW_TEXT).toContain("请先升级本部署再导入");
  // 普通 400(文件坏了)仍是坏文件那一句,不提升级。
  const xhrs2 = installFakeXhr();
  const broken = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001" }).catch((caught: unknown) => caught);
  xhrs2[0].respond(400, JSON.stringify({ detail: "x" }));
  const brokenText = transferRequestErrorText(await broken, IMPORT_UPLOAD_ERRORS);
  expect(brokenText).toBe(IMPORT_UPLOAD_ERRORS[400]);
  expect(brokenText).not.toContain("升级");
});

test("失败码:更新版本 → 升级;被收割 → 可以重新导入、不说文件坏了;后台处理没完成 → 不点任何按钮的名", () => {
  const failed = (code: string) => parseTransferJob(job({ kind: "import", state: "failed", error: { code, message: "" } }))!;
  expect(transferFailureText(failed("archive_too_new"))).toBe(ARCHIVE_TOO_NEW_TEXT);
  const reaped = transferFailureText(failed("import_reaped"));
  expect(reaped).toBe("导入中断了，请重新导入。");
  expect(reaped).not.toMatch(/损坏|不是笔记本导出文件/);
  expect(REPAIR_HINT).toContain(`「${CHECKUP_FIX.backfill_vectors}」`);
  // 发布之后的失败说不准缺的是哪一块(知识图谱、经验表同步……):不承诺一颗可能不在的按钮。
  for (const code of ["import_followup_failed", "import_followup_interrupted"]) {
    const text = transferFailureText(failed(code));
    expect(text).toContain("可以删除这本笔记本后重新导入");
    expect(text).not.toContain(CHECKUP_FIX.backfill_vectors);
  }
});

test("补全检索数据没完成:只有没补完的全是段/内容块那两条腿时,才点「补齐向量」的名", () => {
  const done = { state: "done", embedded: 3, remaining: 0, detail: "" };
  const broken = { state: "failed", embedded: 0, remaining: 5, detail: "RuntimeError" };
  const imported = (reembed: Record<string, unknown>) => parseTransferJob(job({
    kind: "import", state: "succeeded", phase: "done", notebook_id: "nb", reembed,
  }))!;
  const chunksOnly = importReportNotes(imported({
    state: "failed", legs: ["chunks", "elements", "kg_objects"],
    progress: { chunks: broken, elements: done, kg_objects: done },
  }));
  expect(chunksOnly).toEqual([`后台补全检索数据没有完成，提问可能找不全相关内容；${REPAIR_HINT}`]);
  for (const reembed of [
    // 知识图谱那条腿没补完:看板那颗按钮补不上它。
    { state: "failed", legs: ["chunks", "kg_relations"], progress: { chunks: broken, kg_relations: broken } },
    { state: "failed", legs: ["kg_objects"], progress: { kg_objects: broken } },
    // 作业没留下各条腿的进度(提交就失败了):说不准。
    { state: "failed", legs: ["chunks"], error: "RuntimeError" },
  ]) {
    const notes = importReportNotes(imported(reembed));
    expect(notes).toEqual([FOLLOWUP_GENERIC]);
    expect(notes.join("")).not.toContain(CHECKUP_FIX.backfill_vectors);
  }
});

test("上传:同一个上传标识的上一次上传还在处理(409 upload_in_progress)能被认出来,与名额满分开", async () => {
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const file = new File([new Uint8Array(3)], "a.snbk");
  const xhrs = installFakeXhr();
  const racing = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001" }).catch((caught: unknown) => caught);
  xhrs[0].respond(409, JSON.stringify({ detail: { code: "upload_in_progress", message: "x" } }));
  const error = await racing;
  expect(uploadStillInProgress(error)).toBe(true);
  expect(transferRequestErrorText(error, IMPORT_UPLOAD_ERRORS)).toBe(UPLOAD_IN_PROGRESS_TEXT);
  const xhrs2 = installFakeXhr();
  const busy = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001" }).catch((caught: unknown) => caught);
  xhrs2[0].respond(409, JSON.stringify({ detail: "你已有 2 个导出或导入正在进行，请等其中一个完成后再试。" }), "x-user-message: 1\r\n");
  expect(uploadStillInProgress(await busy)).toBe(false);
});

test("作业解析带回 client_upload_id", () => {
  expect(parseTransferJob(job({ kind: "import", client_upload_id: "up-xyz-123" }))?.client_upload_id).toBe("up-xyz-123");
  expect(parseTransferJob(job())?.client_upload_id).toBe("");
});

test("上传:中止得到 AbortError,网络失败得到兜底文案", async () => {
  const xhrs = installFakeXhr();
  const controller = new AbortController();
  const file = new File([new Uint8Array(3)], "a.snbk");
  const aborted = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001", signal: controller.signal }).catch((caught: unknown) => caught);
  controller.abort();
  const abortError = await aborted;
  expect(xhrs[0].aborted).toBe(true);
  expect((abortError as DOMException).name).toBe("AbortError");

  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const failed = uploadNotebookImport(file, { name: "", uploadId: "up-test-0001" }).catch((caught: unknown) => caught);
  xhrs[1].fail();
  expect(transferRequestErrorText(await failed, IMPORT_UPLOAD_ERRORS, "上传没有完成")).toBe("上传没有完成");
});

test("开始导出:413 的中文原样给,泛化文案换成按状态码写好的一句", async () => {
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const fetchMock = vi.spyOn(globalThis, "fetch");
  fetchMock.mockResolvedValueOnce(json({ detail: "这本笔记本的原文件已超过网页导出上限（512 MB），请联系管理员用命令行导出。" }, 413, { "X-User-Message": "1" }));
  const trusted = await startNotebookExport("nb1").catch((caught: unknown) => caught);
  expect(transferRequestErrorText(trusted, EXPORT_START_ERRORS)).toBe(
    "这本笔记本的原文件已超过网页导出上限（512 MB），请联系管理员用命令行导出。",
  );
  fetchMock.mockResolvedValueOnce(json({ detail: "x" }, 413));
  const generic = await startNotebookExport("nb1").catch((caught: unknown) => caught);
  expect(transferRequestErrorText(generic, EXPORT_START_ERRORS)).toBe(EXPORT_START_ERRORS[413]);
});

test("DELETE 的四种结果原样带回;404 当作已经删掉;认不出的结果不冒充成功", async () => {
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  const fetchMock = vi.spyOn(globalThis, "fetch");
  for (const outcome of ["deleted", "cancelled", "pending"] as const) {
    fetchMock.mockResolvedValueOnce(json({ id: "xfer-1", outcome, kept_notebook_id: null }));
    expect(await deleteNotebookTransfer("xfer-1")).toEqual({ outcome, kept_notebook_id: null });
  }
  fetchMock.mockResolvedValueOnce(json({ id: "xfer-1", outcome: "kept", kept_notebook_id: "nb-new" }));
  expect(await deleteNotebookTransfer("xfer-1")).toEqual({ outcome: "kept", kept_notebook_id: "nb-new" });
  expect(fetchMock.mock.calls.at(-1)?.[1]?.method).toBe("DELETE");
  fetchMock.mockResolvedValueOnce(json({ detail: "Transfer job not found" }, 404));
  expect(await deleteNotebookTransfer("xfer-1")).toEqual({ outcome: "deleted", kept_notebook_id: null });
  fetchMock.mockResolvedValueOnce(json({ id: "xfer-1", outcome: "exploded" }));
  await expect(deleteNotebookTransfer("xfer-1")).rejects.toThrow("暂时无法确认结果");
});

test("读一个作业:404 是「不在了」(null),不是错误", async () => {
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ detail: "Transfer job not found" }, 404));
  expect(await getNotebookTransfer("xfer-1")).toBeNull();
});

test("作业解析:形状不对的丢掉;失败只取稳定码并查表,未知码退中性兜底", () => {
  expect(parseTransferJob({ id: "x", kind: "other", state: "queued" })).toBeNull();
  expect(parseTransferJob({ id: "x", kind: "export", state: "weird" })).toBeNull();
  const failed = parseTransferJob(job({ state: "failed", error: { code: "too_large", message: "<b>raw</b>" } }));
  expect(failed?.failure_code).toBe("too_large");
  expect(transferFailureText(failed!)).toBe("导出文件超过网页导出上限，请联系管理员用命令行导出这本笔记本。");
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const unknown = parseTransferJob(job({ kind: "import", state: "failed", error: { code: "future_code", message: "x" } }));
  expect(transferFailureText(unknown!)).toBe("导入没有完成，请稍后重试。");
});

test("进度句与「新笔记本能不能打开」", () => {
  expect(transferProgressText(parseTransferJob(job())!)).toBe("正在写入导出文件（已写入 3.0 MB）");
  const rows = parseTransferJob(job({ kind: "import", phase: "rows", progress: { rows: 40, rows_total: 100 } }))!;
  expect(transferProgressText(rows)).toBe("正在写入内容（40 / 100）");
  // 占位行起就有 notebook_id,发布之前它不能打开。
  expect(importedNotebookReady(rows)).toBe(false);
  expect(importedNotebookReady(parseTransferJob(job({ kind: "import", phase: "reembed", notebook_id: "nb2" }))!)).toBe(true);
  expect(importedNotebookReady(parseTransferJob(job({ kind: "import", phase: "reembed", notebook_id: "" }))!)).toBe(false);
  expect(importedNotebookReady(parseTransferJob(job({
    kind: "import", state: "failed", phase: "publish", notebook_id: "nb2",
    error: { code: "import_followup_interrupted", message: "" },
  }))!)).toBe(true);
});
