// 错误人话层:把后端的 HTTP 状态码 / detail、以及任意 catch 到的异常,翻成给
// 用户看的中文。
//
// 两条通道,严格分开——这是本模块的核心不变量:
//
//   ① 可展示通道(给用户):唯一来源是后端结构化 JSON 里的 `detail` / `message`
//      字符串字段(以及 FastAPI 422 的 `[{msg}]` 数组)。后端有一批 4xx 的
//      detail 是刻意写成中文的可操作文案(注册的「用户名已被占用」、knowhow 的
//      「格子定位不合法」、治理的「仅管理员可设置基准库」……),比按状态码泛化
//      的话有用得多,原样透传。判据是「有没有汉字」:中文 detail = 后端写给
//      用户的;英文 detail = 写给 MCP / 日志的,只按状态码泛化。
//      **非 JSON 的原始正文永远不进这条通道**——网关的 `<html>访问被拒绝 —
//      nginx request id=req-1</html>` 里混着标签、上游名和 request id,含一个
//      汉字就整段甩给用户是泄漏,不是人话。见 pickUserDetail()。
//
//   ② 诊断通道(给开发者 / MCP):状态码 + statusText + 原始正文 + X-Request-Id,
//      统一截断后只进 console.error。见 pickDiagnostic() / readHttpError()。
//
// 用法:
//   - 走 fetch 的失败响应 → throwHumanizedHttpError(res, tag)
//   - catch 到的任意异常(fetch 自身 reject、流式 error 事件、后台 job 的
//     error 字段……)→ toUserMessage(error, fallback)
// 裸抛 `new Error(`${res.status} ...`)`、以及在用户可见位置直出 `err.message`,
// 都由 errors-guard.test.mjs 拦住。

// 中日韩统一表意文字。用来区分「后端写给用户的中文文案」与「写给 MCP / 日志的
// 英文技术串」。
const CJK_RE = /[一-鿿]/;

// 后端为用户写的文案都是短句(「用户名已被占用」「格子定位不合法」)。超长、
// 带换行、带标签的东西不是文案,是正文 / 堆栈 / 错误页——哪怕里面有汉字也不给
// 用户看。这是①通道在 pickUserDetail() 之外的第二道闸:任何绕过结构化提取直接
// 调 humanizeHttpError() 的将来调用点,也漏不出网关正文。
const USER_TEXT_MAX_CHARS = 200;

// 原始诊断进 console 前统一截断,防把整页 HTML 错误页灌进控制台。
const DIAGNOSTIC_MAX_CHARS = 500;

// 兜底文案:说清「没成功」和「能重试」,不暴露任何技术细节。
export const GENERIC_USER_ERROR = "操作没成功，请稍后重试";

// 一段文本是否够格直接展示给用户。
function isDisplayableUserText(text: string): boolean {
  if (!text || text.length > USER_TEXT_MAX_CHARS) return false;
  if (/[\r\n]/.test(text)) return false; // 多行 = 正文 / 堆栈,不是文案
  if (/<[a-zA-Z/!]/.test(text)) return false; // <html> / </p> / <!doctype:标记语言正文
  if (/[{}]/.test(text)) return false; // 花括号 = JSON / 序列化结构,不是文案
  return CJK_RE.test(text); // 英文 = 写给 MCP / 日志的,泛化掉
}

export function humanizeHttpError(status: number, detail?: string): string {
  // ⚠detail 必须来自 pickUserDetail()(结构化 JSON 字段),不能是响应原始正文。
  // 4xx 的中文 detail 是后端刻意写给用户的可操作文案 → 透传;英文 detail
  // (如 "notebook owner required")只进 console,不给用户看。
  const trimmed = (detail ?? "").trim();
  if (status < 500 && isDisplayableUserText(trimmed)) return trimmed;
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

// 把任意 catch 到的异常转成能给用户看的文案。**所有 catch 分支的唯一出口**。
//
// fetch 层抛出来的 message 已经是人话(中文),直接展示——保住 401/403/404/409
// 之间的语义差别,别再压平成一句通用文案(否则用户分不清「登录失效 / 没权限 /
// 已删除 / 冲突」,还会对权限和已删除问题反复重试)。
// 其余一律兜底:fetch 自身 reject 的 "TypeError: Failed to fetch"(它根本进不了
// throwHumanizedHttpError)、JSON 解析错、后端塞在 job.error 里的英文技术串、
// 以及非 Error 值。判据同上:有没有中文 + 够不够像一句文案。
//
// 被丢掉的原始值一律进 console.error——用户看人话,排查看原文,两边都不丢。
export function toUserMessage(error: unknown, fallback: string = GENERIC_USER_ERROR): string {
  const message = error instanceof Error ? error.message.trim() : "";
  // 透传路径无损,不重复记日志(HTTP 错误的原始诊断 readHttpError 已经记过了)。
  if (isDisplayableUserText(message)) return message;
  console.error("[error] 未翻译的原始错误(已用兜底文案代替):", error);
  return fallback;
}

// 一次 HTTP 失败的原始诊断。
export type HttpErrorDiagnostic = {
  status: number;
  // 可展示的后端文案。**只可能来自 JSON 的 detail/message 字段**;非 JSON 的
  // 原始正文恒为 ""(它只进诊断通道)。可以安全地喂给 humanizeHttpError()。
  userDetail: string;
  requestId: string;
};

// ①可展示通道:从响应正文里抠出「后端写给用户的文案」。
// 覆盖 FastAPI 的几种形状:{"detail": "..."} / {"detail": [{loc,msg,type}, ...]} /
// {"message": "..."}。除此以外一律返回空串:
//   - 非 JSON(网关 HTML、代理错误页、纯文本堆栈)→ ""(只进诊断通道)
//   - JSON 但没有 detail/message,或它不是字符串/字符串数组 → ""
// 整坨 JSON、标记语言正文都不是给用户看的文案。
function pickUserDetail(raw: string): string {
  const text = raw.trim();
  if (!text) return "";
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return ""; // 非 JSON 原始正文永不直出给用户。
  }
  if (!body || typeof body !== "object") return "";
  const found =
    (body as { detail?: unknown; message?: unknown }).detail ??
    (body as { message?: unknown }).message;
  if (typeof found === "string") return found.trim();
  if (Array.isArray(found)) {
    // FastAPI 422:[{loc, msg, type}, ...]
    return found
      .map((item: { msg?: unknown }) => (typeof item?.msg === "string" ? item.msg.trim() : ""))
      .filter(Boolean)
      .join("；");
  }
  return "";
}

// ②诊断通道:原始正文压成单行并统一截断。这里不做任何「可展示」判断——原样
// (截断后)进 console 就是它的价值。
function pickDiagnostic(raw: string): string {
  const text = raw.trim().replace(/\s+/g, " ");
  return text.length > DIAGNOSTIC_MAX_CHARS
    ? `${text.slice(0, DIAGNOSTIC_MAX_CHARS)}…[已截断]`
    : text;
}

// 读一次失败响应的 body + 取 X-Request-Id,把原始诊断写进 console.error,
// 返回可展示的 userDetail 供调用侧做场景化文案(如 auth.ts 的登录 401 特化)。
//
// ⚠会消费 res 的 body(res.text() 只能读一次)。调用点在此之后不应再读 body
// ——所有调用点都是「读完就抛」。
export async function readHttpError(res: Response, tag: string): Promise<HttpErrorDiagnostic> {
  const raw = await res.text().catch(() => "");
  const userDetail = pickUserDetail(raw);
  const requestId = res.headers.get("X-Request-Id") || "";
  // console 里给原始正文(不是抠出来的 detail):detail 抠不出来的时候恰恰最需要
  // 看原文,而抠得出来的时候原文本来就含着它。截断在 pickDiagnostic 里统一做。
  const diagnostic = pickDiagnostic(raw);
  console.error(
    `[${tag}] ${res.status} ${res.statusText}${diagnostic ? ` - ${diagnostic}` : ""}${requestId ? ` [${requestId}]` : ""}`
  );
  return { status: res.status, userDetail, requestId };
}

// 失败响应的统一出口:原始诊断进 console,面向用户抛人话。
export async function throwHumanizedHttpError(res: Response, tag: string): Promise<never> {
  const { status, userDetail } = await readHttpError(res, tag);
  throw new Error(humanizeHttpError(status, userDetail));
}
