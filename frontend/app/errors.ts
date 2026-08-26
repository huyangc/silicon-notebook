// 错误人话层:把后端的 HTTP 状态码 / detail、以及任意 catch 到的异常,翻成给
// 用户看的中文。
//
// 核心不变量:**deny by default,信任来自出处,不是形态。**
//
// 早先这里用「4xx 且 detail 含中文就原样穿透」来判断什么能给用户看。那是形态
// 检查,不是信任边界——后端 40 处 `detail=str(exc)` 和 20 处刻意写给用户的中文
// 文案都是普通字符串,结构上区分不开,于是 `403 "访问被拒绝 — nginx/1.25
// upstream=10.0.0.7:8000"` 会连内网地址一起上屏。
//
// 现在出处由后端显式声明,共两条通道:
//
//   ① 可展示通道(给用户):只有**同时**满足三条才把 detail 原样给用户——
//      (a) 响应带 `X-User-Message: 1`,即后端经 user_error() 明确声明「这是
//          写给终端用户的文案」(见 backend/app/api/deps.py);
//      (b) 它还得像一句文案(isDisplayableUserText:不多行、不带标签、不带
//          花括号、不超长);
//      (c) 它还得符合产品文案规范(中文,见 isCompliantUserCopy)。
//      (a) 是**信任闸**,(b)(c) 是**展示策略闸**,两道串联、职责不能互换,
//      详见 humanizeHttpError 上方的长注释。没有 (a) 的一律不给用户看,哪怕
//      整段都是中文。
//      裸 `HTTPException(detail=str(exc))` 永远不带标记 → 用户只看到按状态码
//      泛化的通用中文。
//
//   ② 诊断通道(给开发者 / MCP):状态码 + statusText + 原始正文 + X-Request-Id,
//      统一截断后只进 console.error。见 pickDiagnostic() / readHttpError()。
//
// toUserMessage() 同理:它**只认品牌**(见 HUMANIZED),不认形态。只有本模块
// 亲手翻译过的错误才原样返回;后端塞在 job.error / error_message / stream
// event 里的任意字符串,不管长什么样,一律换成兜底文案 + 原文进 console。
//
// 用法:
//   - 走 fetch 的失败响应 → throwHumanizedHttpError(res, tag)
//   - catch 到的任意异常(fetch 自身 reject、流式 error 事件、后台 job 的
//     error 字段……)→ toUserMessage(error, fallback)
//   - 渲染期拿到的后端诊断字段(不该上屏,但排查要留)→ logDiagnostic(tag, value)
// 裸抛 `new Error(`${res.status} ...`)`、以及在用户可见位置直出 `err.message` /
// `.error` / `.error_message`,都由 errors-guard.test.mjs 拦住。

// 后端为用户写的文案都是短句(「用户名已被占用」「格子定位不合法」)。超长、
// 带换行、带标签的东西不是文案,是正文 / 堆栈 / 错误页——即使后端给它盖了章,
// 也不给用户看。这是①通道在「出处标记」之外的第二道闸,防的是后端把
// user_error() 用在了一个拼了异常原文的串上。
const USER_TEXT_MAX_CHARS = 200;

// 原始诊断进 console 前统一截断,防把整页 HTML 错误页 / 超长异常灌进控制台。
// HTTP 正文和 catch 到的任意异常共用这一个上限。
const DIAGNOSTIC_MAX_CHARS = 500;

// 后端 user_error() 挂的出处标记(backend/app/api/deps.py)。
// ⚠它同时登记在后端 CORS 的 expose_headers 里;跨源部署下没有 expose 就读不到,
// 后果是所有后端中文文案被压平成通用文案(后端 test_user_error.py 有守卫)。
const USER_MESSAGE_HEADER = "X-User-Message";

// 兜底文案:说清「没成功」和「能重试」,不暴露任何技术细节。
export const GENERIC_USER_ERROR = "操作没成功，请稍后重试";

// 「本模块翻译过」的品牌。toUserMessage 只认它,不认文本形态。
//
// 为什么是 Symbol.for 而不是模块内 `Symbol()` 或 `class HumanizedError`:
// 后两者的身份都绑在**模块实例**上,而 Next.js 会把同一个模块同时打进 server
// 和 client bundle,于是同一个概念出现两份互不相等的身份,`instanceof` /
// 私有 Symbol 比对会出现假阴性——盖过章的错误被判成没盖章,用户拿到兜底文案,
// 401/403/404/409 的区分度白丢。全局注册表的 Symbol 跨模块实例稳定。
//
// 「可伪造性」不在威胁模型里:要挡的是**后端来的字符串**,而字符串永远带不上
// Symbol 属性——JSON 里没有 Symbol,序列化会把它丢掉。能调 humanizedError()
// 的只有本仓库的代码,那不是攻击面。
const HUMANIZED: unique symbol = Symbol.for("silicon-notebook.errors.humanized") as never;

// 原始 HTTP 状态码,挂在带品牌的 Error 上(非枚举属性)。humanizeHttpError 把
// detail 压平成一句中文文案就丢了状态码,可有些 catch 侧要按状态码分流——并发
// 保存里 409(他人已改 → 跳过该格)必须和其他失败(真报错)区分开。用与 HUMANIZED
// 同样的 Symbol.for 全局注册表(跨 server/client bundle 稳定,见上)。JSON 里没有
// Symbol,后端来的字符串永远带不上它,故只有本仓库亲手 throw 的错误才有状态码。
const HTTP_STATUS: unique symbol = Symbol.for("silicon-notebook.errors.httpStatus") as never;

// 一段文本是否够格直接展示给用户(**形态**闸,不是信任闸)。
function isDisplayableUserText(text: string): boolean {
  if (!text || text.length > USER_TEXT_MAX_CHARS) return false;
  if (/[\r\n]/.test(text)) return false; // 多行 = 正文 / 堆栈,不是文案
  if (/<[a-zA-Z/!]/.test(text)) return false; // <html> / </p> / <!doctype:标记语言正文
  if (/[{}]/.test(text)) return false; // 花括号 = JSON / 序列化结构,不是文案
  return true;
}

// 产品文案规范:面向用户的文案一律中文(AGENTS.md)。要求「含中文」而不是
// 「纯中文」——真实文案里混着标识符是正常的(「用户名须为『单个小写字母+00+
// 六位数字』,如 a00123456」)。
const CJK_RE = /[一-鿿]/;

// 已通过出处验证的文案,是否符合产品文案规范。
//
// ⚠⚠ 这**不是**「用形态判断信任」——那个模型(「4xx 且 detail 含中文就透传」)
// 在第三轮评审已经被否掉,它错在拿形态当**信任**判据,于是后端
// `detail=str(exc)` 里碰巧带中文的异常串(「解析失败:不支持的文件类型」)就
// 上了屏。别把这个函数读成那条回退。
//
// 区别在**作用位置**:本函数只在闸1(出处 / X-User-Message)已经放行之后才跑。
// 到这里为止,「这句是写给终端用户的」已经由后端显式声明过了,不再有疑问;
// 本函数判的不是「能不能信」,而是「符不符合规范」。也就是:
//
//     闸1 出处 → 决定**信任**(能不能给用户看)。形态在这一层没有发言权。
//     闸2 规范 → 决定**展示**(这句合不合格)。作用在已可信的文案上。
//
// 两道串联,顺序和职责都不能互换,也不要合并成一道。判据方向可以自查:放宽
// 闸2 永远不会让**不可信**的文本上屏(闸1 早就拦掉了),而放宽闸1 会——这就是
// 两者不对称、必须分开的原因。
//
// 闸2 兜的是一类**产品**缺陷:后端有人用 user_error() 写了英文用户文案。
// 那种串盖过章、也确实是写给用户的,但用户读不懂,不如按状态码给的通用中文。
function isCompliantUserCopy(text: string): boolean {
  return CJK_RE.test(text);
}

// 造一个「已翻译」的错误——本模块之外没有第二个地方盖这个章。可选 `status`:
// HTTP 失败经此抛出时挂上原始状态码,供 httpErrorStatus 读回做状态码分流。
// 扩展引擎的 stream/job 失败码 → 固定中文文案。住在本模块是红线要求(「前端翻译
// 只在 errors.ts」):它匹配的是**我们自己后端**异常序列化出的稳定形状
// `AskPluginEngineError: <code>`,原文永不上屏——未知码只落通用文案。调用方
// (ask-api.ts 的 stream error 分支)拿到译文后仍经 humanizedError 打品牌。
export function pluginEngineFailureMessage(raw: string): string | null {
  const match = /^AskPluginEngineError: ([a-z0-9_]+)$/.exec(raw.trim());
  if (!match) return null;
  if (match[1] === "plugin_engine_unverified_citation") {
    return "扩展引擎返回了无法核验的引用";
  }
  if (match[1] === "plugin_engine_unavailable") {
    return "所选扩展引擎当前不可用，请切换引擎后重试";
  }
  if ([
    "plugin_engine_citation_limit",
    "plugin_engine_model_call_limit",
    "plugin_engine_search_call_limit",
    "plugin_engine_kg_call_limit",
  ].includes(match[1])) {
    return "扩展引擎超过了本次调用预算，请重试或切换引擎";
  }
  return "扩展引擎没能完成回答，请重试或切换引擎";
}

export function humanizedError(message: string, status?: number): Error {
  const error = new Error(message);
  Object.defineProperty(error, HUMANIZED, { value: true, enumerable: false });
  if (status !== undefined) {
    Object.defineProperty(error, HTTP_STATUS, { value: status, enumerable: false });
  }
  return error;
}

function isHumanized(error: unknown): error is Error {
  return (
    error instanceof Error &&
    (error as unknown as Record<symbol, unknown>)[HUMANIZED] === true
  );
}

// 读回一个错误上的原始 HTTP 状态码(只有 throwHumanizedHttpError 抛出的错误才
// 有)。非 HTTP 错误(fetch 自身 reject、纯 Error、humanizedError 无状态、非 Error
// 值)一律返回 undefined。用途:catch 侧按状态码分流(如 409 → 内容已被他人改动,
// 走「跳过该格」而非「保存失败」),不必读 .message 做脆弱的文案匹配。
export function httpErrorStatus(error: unknown): number | undefined {
  if (!(error instanceof Error)) return undefined;
  const status = (error as unknown as Record<symbol, unknown>)[HTTP_STATUS];
  return typeof status === "number" ? status : undefined;
}

// 状态码 → 中文。`trusted` 才允许 detail 原样穿透。
//
// ⚠ trusted 只该来自 readHttpError() 读到的 `X-User-Message` 头。手工传 true
// 等于绕过整条信任链。
export function humanizeHttpError(status: number, detail?: string, trusted: boolean = false): string {
  const trimmed = (detail ?? "").trim();
  // 闸1(信任):出处 + 5xx 排除 + 形态兜底。
  // 5xx 一律泛化:user_error() 是给「客户端能纠正的 4xx」用的,5xx 意味着
  // 「我们坏了」,通用文案才是对的,也避免上游/网关伪造标记把内部错误顶上屏。
  if (trusted && status < 500 && isDisplayableUserText(trimmed)) {
    // 闸2(展示策略):盖了章、但不符合产品文案规范 → 退回状态码通用文案。
    if (isCompliantUserCopy(trimmed)) return trimmed;
    // 非中文用户文案是**写码时**的产品缺陷,不是运行时故障:用户读不懂它,
    // 但它本身不含敏感信息(已过闸1)。所以原样喊进 console 给开发者,界面退
    // 通用文案。console 用户看不到,喊出来才有人去把它改成中文。
    console.error(
      `[user-copy] 后端 user_error() 给了非中文用户文案,界面已退回通用文案(请改成中文):`,
      truncateDiagnostic(trimmed)
    );
  }
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
// 只认品牌:本模块翻译过的(humanizedError / throwHumanizedHttpError)原样返回
// ——保住 401/403/404/409 之间的语义差别,别压平成一句通用文案(否则用户分不清
// 「登录失效 / 没权限 / 已删除 / 冲突」,还会对权限和已删除问题反复重试)。
//
// 其余一律兜底,不看内容长什么样:fetch 自身 reject 的 "TypeError: Failed to
// fetch"、JSON 解析错、后端塞在 job.error / error_message / stream event 里的
// 串(`RuntimeError: 模型调用失败 upstream timeout` 这种「中文技术串」正是旧
// 形态判据漏掉的一类)、以及非 Error 值。
//
// 被丢掉的原始值一律进 console.error——用户看人话,排查看原文,两边都不丢。
export function toUserMessage(error: unknown, fallback: string = GENERIC_USER_ERROR): string {
  // 透传路径无损,不重复记日志(HTTP 错误的原始诊断 readHttpError 已经记过了)。
  if (isHumanized(error)) return error.message;
  logDiagnostic("error", error);
  return fallback;
}

// 一次 HTTP 失败的原始诊断。
export type HttpErrorDiagnostic = {
  status: number;
  // 后端 JSON 里的 detail/message 原文。**能不能给用户看由 trusted 决定**,
  // 不要绕过 humanizeHttpError() 直接展示它。
  userDetail: string;
  // 后端是否用 user_error() 声明「这句是写给用户的」。
  trusted: boolean;
  requestId: string;
};

// ①可展示通道:从响应正文里抠出后端的 detail 文本。
// 覆盖 FastAPI 的几种形状:{"detail": "..."} / {"detail": [{loc,msg,type}, ...]} /
// {"message": "..."}。除此以外一律返回空串:
//   - 非 JSON(网关 HTML、代理错误页、纯文本堆栈)→ ""(只进诊断通道)
//   - JSON 但没有 detail/message,或它不是字符串/字符串数组 → ""
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

// 诊断值统一压成单行 + 截断。HTTP 正文和 catch 到的异常共用,避免一边截断
// 一边不截断(旧版 toUserMessage 直接把整个 error 丢进 console,无上限)。
function truncateDiagnostic(text: string): string {
  const oneLine = text.trim().replace(/\s+/g, " ");
  return oneLine.length > DIAGNOSTIC_MAX_CHARS
    ? `${oneLine.slice(0, DIAGNOSTIC_MAX_CHARS)}…[已截断]`
    : oneLine;
}

// ②诊断通道:原始正文压成单行并统一截断。这里不做任何「可展示」判断——原样
// (截断后)进 console 就是它的价值。
function pickDiagnostic(raw: string): string {
  return truncateDiagnostic(raw);
}

// 把任意值描述成一行可读的诊断文本(同样受 DIAGNOSTIC_MAX_CHARS 约束)。
function describeForDiagnostic(value: unknown): string {
  if (value instanceof Error) return truncateDiagnostic(`${value.name}: ${value.message}`);
  if (typeof value === "string") return truncateDiagnostic(value);
  if (value === null || value === undefined) return String(value);
  try {
    return truncateDiagnostic(JSON.stringify(value) ?? String(value));
  } catch {
    return truncateDiagnostic(String(value));
  }
}

// 把「不给用户看的原始诊断」写进 console(统一截断)。
//
// 用在渲染期拿到后端诊断字段、但不该上屏的地方(report.error、
// model_errors[].message、readiness 的 error……)。
// ⚠别在 render body 里直接调——会随重渲染刷屏;放进 useEffect 或事件回调。
export function logDiagnostic(tag: string, value: unknown): void {
  console.error(`[${tag}] 未翻译的原始错误(界面已用兜底文案代替):`, describeForDiagnostic(value));
}

// 读一次失败响应的 body + 取 X-Request-Id / X-User-Message,把原始诊断写进
// console.error,返回诊断供调用侧做场景化文案(如 auth.ts 的登录 401 特化)。
//
// ⚠会消费 res 的 body(res.text() 只能读一次)。调用点在此之后不应再读 body
// ——所有调用点都是「读完就抛」。
export async function readHttpError(res: Response, tag: string): Promise<HttpErrorDiagnostic> {
  const raw = await res.text().catch(() => "");
  const userDetail = pickUserDetail(raw);
  const trusted = res.headers.get(USER_MESSAGE_HEADER) === "1";
  const requestId = res.headers.get("X-Request-Id") || "";
  // console 里给原始正文(不是抠出来的 detail):detail 抠不出来的时候恰恰最需要
  // 看原文,而抠得出来的时候原文本来就含着它。截断在 pickDiagnostic 里统一做。
  const diagnostic = pickDiagnostic(raw);
  console.error(
    `[${tag}] ${res.status} ${res.statusText}${diagnostic ? ` - ${diagnostic}` : ""}${requestId ? ` [${requestId}]` : ""}`
  );
  return { status: res.status, userDetail, trusted, requestId };
}

// 失败响应的统一出口:原始诊断进 console,面向用户抛人话(带品牌)。
export async function throwHumanizedHttpError(res: Response, tag: string): Promise<never> {
  const { status, userDetail, trusted } = await readHttpError(res, tag);
  // 状态码随品牌一并挂上:文案被 humanizeHttpError 压平后,catch 侧仍能按状态码
  // 分流(httpErrorStatus)。
  throw humanizedError(humanizeHttpError(status, userDetail, trusted), status);
}
