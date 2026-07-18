// 错误人话层:把后端的 HTTP 状态码 / detail 翻成给用户看的中文。
//
// 设计约定(PR C):
//   - 后端 detail 大多是英文——供 MCP agent / 日志 / 排查,前端不改后端。英文
//     detail 永远不进用户文案,只按 HTTP 状态码泛化成中文。
//   - 但后端有一批 4xx 的 detail 是刻意写成中文的可操作文案(注册的「用户名
//     已被占用」、knowhow 的「格子定位不合法」、治理的「仅管理员可设置基准
//     库」……)。这些比按状态码泛化的话有用得多,原样透传给用户。
//     判据就是「detail 里有没有汉字」:中文 detail = 后端写给用户看的;英文
//     detail = 写给 MCP / 日志看的。5xx 一律泛化,不把内部错误漏出去。
//   - 原始诊断(状态码 + statusText + 后端 detail + requestId)一律进
//     console.error 供排查,由 readHttpError() 统一收口。
//
// 所有走 fetch 的调用点都应该用 throwHumanizedHttpError();裸抛
// `new Error(`${res.status} ...`)` 由 errors-guard.test.mjs 拦住。

// 中日韩统一表意文字。用来区分「后端写给用户的中文 detail」与「写给
// MCP / 日志的英文 detail」。
const CJK_RE = /[一-鿿]/;

export function humanizeHttpError(status: number, detail?: string): string {
  // 4xx 的中文 detail 是后端刻意写给用户的可操作文案,比泛化文案有用 → 透传。
  // 英文 detail(如 "notebook owner required")只进 console,不给用户看。
  const trimmed = (detail ?? "").trim();
  if (status < 500 && trimmed && CJK_RE.test(trimmed)) return trimmed;
  switch (status) {
    case 401:
      return "登录状态已失效，请重新登录";
    case 403:
      return "没有权限进行这个操作";
    case 404:
      return "没找到，可能已被删除";
    case 409:
      return "操作有冲突，请刷新后重试";
    case 413:
      return "文件太大";
    case 422:
      return "提交的内容有误";
    default:
      if (status >= 500) return "服务暂时不可用，请稍后再试";
      return "操作失败，请重试";
  }
}

// 把任意 catch 到的异常转成能给用户看的文案。
//
// fetch 层抛出来的 message 已经是人话(中文),直接展示——保住 401/403/404/409
// 之间的语义差别,别再压平成一句通用文案(否则用户分不清「登录失效 / 没权限 /
// 已删除 / 冲突」,还会对权限和已删除问题反复重试)。
// 非 fetch 层的异常(如 "TypeError: Failed to fetch"、JSON 解析错)是英文技术
// 文本,不给用户看 → 用调用方的兜底文案。判据同上:有没有中文。
export function toUserMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message.trim() : "";
  return message && CJK_RE.test(message) ? message : fallback;
}

// 一次 HTTP 失败的原始诊断。detail 是抠出来的后端错误文本(FastAPI 的
// {"detail": ...} / {"message": ...} / 422 的错误数组 / 非 JSON 原文)。
export type HttpErrorDiagnostic = {
  status: number;
  detail: string;
  requestId: string;
};

// 从已经读出来的 body 原文里抠后端 detail。
// FastAPI 的几种形状都覆盖:{"detail": "..."} / {"detail": [{"msg": ...}]} /
// 非 JSON 的纯文本(网关 502 的 HTML 之类)。JSON 里没有 detail/message 时返回
// 空串——整坨 JSON 不是给用户看的文案。
function pickDetail(raw: string): string {
  const text = raw.trim();
  if (!text) return "";
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return text; // 非 JSON:原文本身就是 detail(诊断用)。
  }
  if (!body || typeof body !== "object") return text;
  const found =
    (body as { detail?: unknown; message?: unknown }).detail ??
    (body as { message?: unknown }).message;
  if (typeof found === "string") return found;
  if (Array.isArray(found)) {
    // FastAPI 422:[{loc, msg, type}, ...]
    return found
      .map((item: { msg?: unknown }) => String(item?.msg ?? ""))
      .filter(Boolean)
      .join("；");
  }
  if (found != null) return JSON.stringify(found);
  return "";
}

// 读一次失败响应的 body + 取 X-Request-Id,并把原始诊断写进 console.error。
// 返回诊断供调用侧做场景化文案(如 auth.ts 的登录 401 特化)。
//
// ⚠会消费 res 的 body(res.text() 只能读一次)。调用点在此之后不应再读 body
// ——所有调用点都是「读完就抛」。
export async function readHttpError(res: Response, tag: string): Promise<HttpErrorDiagnostic> {
  const raw = await res.text().catch(() => "");
  const detail = pickDetail(raw);
  const requestId = res.headers.get("X-Request-Id") || "";
  // console 里宁可多给:detail 抠不出来时退回 body 原文(截断,防把整页 HTML
  // 错误页灌进 console)。
  const diagnostic = detail || raw.trim().slice(0, 500);
  console.error(
    `[${tag}] ${res.status} ${res.statusText}${diagnostic ? ` - ${diagnostic}` : ""}${requestId ? ` [${requestId}]` : ""}`
  );
  return { status: res.status, detail, requestId };
}

// 失败响应的统一出口:原始诊断进 console,面向用户抛人话。
export async function throwHumanizedHttpError(res: Response, tag: string): Promise<never> {
  const { status, detail } = await readHttpError(res, tag);
  throw new Error(humanizeHttpError(status, detail));
}
