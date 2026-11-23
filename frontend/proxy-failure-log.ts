// 同源反代失败时，Next 内置的转发代码会执行
// `console.error("Failed to proxy <上游完整地址>", err)`，地址里带着请求的查询串——
// 检索词等用户输入就在查询串里。Next 没有关闭或替换这条日志的配置，rewrite 规则也
// 管不到它，所以这里只改写这一条消息：保留定位故障要用的上游协议、主机和端口，去掉
// 路径、查询串、片段和凭据。其余日志原样透传；转发本身不经过这里。
//
// 路径整段不写：请求带来的部分里有分享口令、邀请口令，它们本身就是凭据；而
// BACKEND_PROXY_TARGET 可以自带路径前缀，所以也没有哪一段能保证只来自转发规则。
// 要知道具体是哪个请求，看上游自己的日志。
//
// 随消息一起打印的错误对象也不原样写：上游返回畸形响应时，Node 会把收到的原始字节挂在
// 错误对象上（rawPacket）。这里只写出几个形状受限的字段（错误名、错误码、系统调用、
// 对端地址和端口）。Next 随后还会把同一个错误对象不带前缀地再打印一次，所以记住见过的
// 错误对象，再次出现时同样处理。

const PROXY_FAILURE_PREFIX = "Failed to proxy ";
const UNREADABLE_TARGET = "[upstream address withheld]";
// scheme://authority，authority 到第一个 / 为止，其中最后一个 @ 之前是凭据。
const HTTP_TARGET = /^(https?:\/\/)([^/]*)/;

/** 上游地址里可以进日志的部分；认不出形状时整段不写，不回退到原文。 */
export function loggableProxyTarget(target: string): string {
  const queryOrFragment = target.search(/[?#]/);
  const withoutQuery = queryOrFragment === -1 ? target : target.slice(0, queryOrFragment);
  const match = HTTP_TARGET.exec(withoutQuery);
  if (!match) return UNREADABLE_TARGET;
  const [, scheme, authority] = match;
  const host = authority.slice(authority.lastIndexOf("@") + 1);
  if (host === "") return UNREADABLE_TARGET;
  return `${scheme}${host}`;
}

type ProxyErrorSummary = {
  name?: string;
  code?: string;
  syscall?: string;
  address?: string;
  port?: number;
};

/** 错误对象里可以进日志的字段；每个字段都要符合固定形状，否则不写。 */
export function loggableProxyError(error: object): ProxyErrorSummary {
  const summary: ProxyErrorSummary = {};
  try {
    const { name, code, syscall, address, port } = error as Record<string, unknown>;
    if (typeof name === "string" && /^[A-Za-z]{1,40}$/.test(name)) summary.name = name;
    if (typeof code === "string" && /^[A-Z][A-Z0-9_]{0,60}$/.test(code)) summary.code = code;
    if (typeof syscall === "string" && /^[a-z]{1,20}$/.test(syscall)) summary.syscall = syscall;
    if (typeof address === "string" && /^[0-9A-Fa-f:.]{1,45}$/.test(address)) summary.address = address;
    if (typeof port === "number" && Number.isInteger(port) && port >= 0 && port <= 65535) {
      summary.port = port;
    }
  } catch {
    // 读取字段本身抛错的对象：已经确认的字段照写，其余不写。
  }
  return summary;
}

// 随转发失败消息出现过的错误对象；弱引用，不妨碍回收。
const proxyErrors = new WeakSet<object>();

const isObject = (value: unknown): value is object =>
  (typeof value === "object" && value !== null) || typeof value === "function";

/** 只改写 Next 的转发失败消息及其错误对象，其它参数列表原样返回。 */
export function redactProxyFailureLog(args: readonly unknown[]): readonly unknown[] {
  const [message, ...rest] = args;
  if (typeof message === "string" && message.startsWith(PROXY_FAILURE_PREFIX)) {
    const target = message.slice(PROXY_FAILURE_PREFIX.length);
    const details = rest.map((value) => {
      if (!isObject(value)) return value;
      proxyErrors.add(value);
      return loggableProxyError(value);
    });
    return [`${PROXY_FAILURE_PREFIX}${loggableProxyTarget(target)}`, ...details];
  }
  if (!args.some((value) => isObject(value) && proxyErrors.has(value))) return args;
  return args.map((value) =>
    isObject(value) && proxyErrors.has(value) ? loggableProxyError(value) : value,
  );
}

const REDACTING = Symbol.for("silicon-notebook.proxy-failure-log-redaction");

type ErrorLog = ((...args: unknown[]) => void) & { [REDACTING]?: true };

/** 给 `target.error` 套上改写；对同一个对象重复安装不叠加。 */
export function installProxyFailureLogRedaction(target: { error: ErrorLog }): void {
  const inner = target.error;
  if (inner[REDACTING]) return;
  const redacting: ErrorLog = function (this: unknown, ...args: unknown[]) {
    return inner.apply(this, [...redactProxyFailureLog(args)]);
  };
  redacting[REDACTING] = true;
  target.error = redacting;
}
