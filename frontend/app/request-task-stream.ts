import {
  performApiRequest,
  type ApiRequestOptions,
} from "./api-client.ts";
import {
  humanizedError,
  logDiagnostic,
  throwHumanizedHttpError,
} from "./errors.ts";
import { takeNdjsonLines } from "./ndjson.ts";
import { yieldToPaint } from "./ndjson-stream.ts";

type TaskStreamEvent<T> =
  | { event: "started"; stage: string; elapsed_ms: number }
  | { event: "heartbeat"; stage: string; elapsed_ms: number }
  | { event: "final"; stage: string; result: T }
  | { event: "cancelled"; stage: string }
  | { event: "error"; stage: string; error: string };

export type TaskStreamCallbacks = {
  onHeartbeat?: (elapsedMs: number, stage: string) => void | Promise<void>;
  fallbackMessage?: string;
};

/** Consume the shared request-local started/heartbeat/final NDJSON protocol. */
export async function requestTaskStream<T>(
  path: string,
  options: ApiRequestOptions,
  callbacks: TaskStreamCallbacks = {},
): Promise<T> {
  const response = await performApiRequest(path, options);
  if (!response.ok) await throwHumanizedHttpError(response, options.tag);
  if (!response.body) throw humanizedError(callbacks.fallbackMessage ?? "请求没有返回可读取的结果，请重试");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: T | undefined;
  let hasFinal = false;

  const consume = async (line: string) => {
    let event: TaskStreamEvent<T>;
    try {
      event = JSON.parse(line) as TaskStreamEvent<T>;
    } catch {
      throw humanizedError(callbacks.fallbackMessage ?? "响应格式异常，请重试");
    }
    if (event.event === "started" || event.event === "heartbeat") {
      await callbacks.onHeartbeat?.(event.elapsed_ms, event.stage);
      await yieldToPaint();
      return;
    }
    if (event.event === "final") {
      finalResult = event.result;
      hasFinal = true;
      return;
    }
    if (event.event === "cancelled") {
      throw new DOMException("操作已中断", "AbortError");
    }
    if (event.event === "error") {
      logDiagnostic(`task-stream:${event.stage}`, event.error);
      throw humanizedError(callbacks.fallbackMessage ?? "操作没能完成，请重试");
    }
    const exhaustive: never = event;
    void exhaustive;
    throw humanizedError(callbacks.fallbackMessage ?? "响应格式异常，请重试");
  };

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
  if (!hasFinal) {
    throw humanizedError(callbacks.fallbackMessage ?? "连接提前结束，请重试");
  }
  return finalResult as T;
}
