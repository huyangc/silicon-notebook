import { takeNdjsonLines } from "./ndjson.ts";

/**
 * 读完一条 NDJSON 响应体：取一段字节、解码、按行切开、逐行交给 `consume`。
 *
 * 抽成一处而不是每个传输各抄一遍，是因为这段循环里有三个**只在边界上显形**的细节，
 * 抄一次就要正确一次：
 *   · `decode(value, { stream: true })`——一个 UTF-8 字符可能被拆在相邻两段字节之间，
 *     漏了这个标志就会在中文轨迹里随机吐出替换字符；
 *   · 流结束后的 `decode()` 收尾 + 残段补发——后端最后一行不带换行时（终态帧正是
 *     最后一行），少了这两句整条终态就悄悄丢了，调用方只会看到「提前结束」；
 *   · 逐行 `await`——`consume` 要更新界面，按序 await 才不会把两帧的顺序颠倒。
 *
 * 保活用的空行由 `takeNdjsonLines` 滤掉，所以 `consume` 永远拿到非空的一行；
 * 解析、协议与错误语义都留在调用方——这里只负责「把字节变成行」。
 */
export async function readNdjsonStream(
  body: ReadableStream<Uint8Array>,
  consume: (line: string) => void | Promise<void>,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = takeNdjsonLines(buffer);
    buffer = parsed.remainder;
    for (const line of parsed.lines) await consume(line);
  }
  buffer += decoder.decode();
  if (buffer.trim()) await consume(buffer.trim());
}


const PAINT_YIELD_FALLBACK_MS = 50;

/**
 * 两帧之间让浏览器画一次，轨迹才是一步一步出现而不是攒成一坨。
 *
 * ⚠ 必须带定时器兜底：后台标签页里浏览器**暂停** `requestAnimationFrame`，只等它的
 * 读取循环会原地卡死——后端早已答完，页面却停在「启动检索」，直到用户切回来。笔记本
 * 内问答的流曾经就是这样（只有 `requestTaskStream` 带了兜底）；三条 NDJSON 传输现在
 * 共用这一份。
 */
export function yieldToPaint(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
      resolve();
      return;
    }
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(fallback);
      resolve();
    };
    const fallback = setTimeout(finish, PAINT_YIELD_FALLBACK_MS);
    window.requestAnimationFrame(finish);
  });
}
