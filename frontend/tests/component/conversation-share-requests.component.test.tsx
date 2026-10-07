import { afterEach, beforeEach, expect, test, vi } from "vitest";

// 会话分享的两个**真实**请求（笔记本内 `shareConversation` / `getConversationShareDisclosure`，
// 全局 `globalConversationShareApi`）打到 fetch 这一层：URL、方法、请求体原文、409 / 403 的
// 解析。弹窗测试里这些函数被 mock 掉了，这里钉的是它们自己——零条个人记忆时请求体与接入
// 披露之前逐字节相同，确认值只在作者确认过一个确数时才出现。

import { getConversationShareDisclosure, shareConversation } from "../../app/ask-api.ts";
import { globalConversationShareApi } from "../../app/global-ask-api.ts";
import { httpErrorStatus, toUserMessage } from "../../app/errors.ts";
import { ShareDisclosureRequired } from "../../app/share-failure.ts";

type Call = { method: string; url: string; body: string | null };
let calls: Call[] = [];
let respond: (call: Call) => Response;

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const SHARED = { share_token: "tok", shared_through_at: "2026-01-01T00:00:00", shared_through_id: "a2" };

beforeEach(() => {
  calls = [];
  respond = () => json(200, SHARED);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call = {
      method: String(init?.method || "GET").toUpperCase(),
      url: String(input),
      body: typeof init?.body === "string" ? init.body : null,
    };
    calls.push(call);
    return respond(call);
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// 去掉 API 前缀（`/api`），留下后端路由本身。
const path = (call: Call) => new URL(call.url, "http://localhost").pathname.replace(/^\/api(?=\/)/, "");

test("笔记本：不带确认值的 POST，请求体原文与接入披露之前逐字节相同", async () => {
  // 与笔记本侧工厂的调用形态一致：没有确认值时第四个参数显式是 undefined。
  await shareConversation("nb-1", "conv-1", "a2", undefined);
  expect(calls).toHaveLength(1);
  expect(calls[0].method).toBe("POST");
  expect(path(calls[0])).toBe("/notebooks/nb-1/conversations/conv-1/share");
  expect(calls[0].body).toBe('{"expected_through_id":"a2"}');
});

test("笔记本：带确认值时，acknowledged_memory_count 紧随 expected_through_id", async () => {
  await shareConversation("nb-1", "conv-1", "a2", 3);
  expect(calls[0].body).toBe('{"expected_through_id":"a2","acknowledged_memory_count":3}');
});

test("全局：同样两种请求体，端点是全局那一组", async () => {
  const api = globalConversationShareApi("conv-g");
  await api.share("job-2");
  await api.share("job-2", 4);
  expect(calls.map((call) => [call.method, path(call), call.body])).toEqual([
    ["POST", "/global-ask/conversations/conv-g/share", '{"expected_through_id":"job-2"}'],
    ["POST", "/global-ask/conversations/conv-g/share", '{"expected_through_id":"job-2","acknowledged_memory_count":4}'],
  ]);
});

test("披露端点：笔记本与全局都是 GET .../share/disclosure?through_id=<范围>", async () => {
  respond = () => json(200, { memory_count: 2, new_memory_count: 1 });
  const notebook = await getConversationShareDisclosure("nb-1", "conv-1", "a 2");
  const global = await globalConversationShareApi("conv-g").loadDisclosure("job-2");
  expect(notebook).toEqual({ memory_count: 2, new_memory_count: 1 });
  expect(global).toEqual({ memory_count: 2, new_memory_count: 1 });
  expect(calls.map((call) => [call.method, call.url.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api(?=\/)/, "")])).toEqual([
    ["GET", "/notebooks/nb-1/conversations/conv-1/share/disclosure?through_id=a%202"],
    ["GET", "/global-ask/conversations/conv-g/share/disclosure?through_id=job-2"],
  ]);
});

test("409 share_disclosure_required（detail 形态）→ ShareDisclosureRequired，带服务端确数", async () => {
  respond = () => json(409, { detail: { code: "share_disclosure_required", memory_count: 3, new_memory_count: 2 } });
  const error = await shareConversation("nb-1", "conv-1", "a2").catch((reason) => reason);
  expect(error).toBeInstanceOf(ShareDisclosureRequired);
  expect([error.memoryCount, error.newMemoryCount]).toEqual([3, 2]);
});

test("409 share_disclosure_required（根级形态）同样被接受", async () => {
  respond = () => json(409, { code: "share_disclosure_required", memory_count: 5, new_memory_count: 0 });
  const error = await globalConversationShareApi("conv-g").share("job-2", 4).catch((reason) => reason);
  expect(error).toBeInstanceOf(ShareDisclosureRequired);
  expect([error.memoryCount, error.newMemoryCount]).toEqual([5, 0]);
});

test("别的 409（水位过期、零可分享回答）仍是普通人话错误，不被当成披露确认", async () => {
  respond = () => json(409, { detail: "这条会话已有变化，请刷新后重新分享。" }, { "X-User-Message": "1" });
  const error = await shareConversation("nb-1", "conv-1", "a2").catch((reason) => reason);
  expect(error).not.toBeInstanceOf(ShareDisclosureRequired);
  expect(httpErrorStatus(error)).toBe(409);
  expect(toUserMessage(error, "分享失败")).toBe("这条会话已有变化，请刷新后重新分享。");
});

test("403：服务端的中文原因（X-User-Message）原样成为错误文案，状态码可读回", async () => {
  respond = () => json(403, { detail: "这条会话引用了其他成员的个人记忆，不能公开分享。" }, { "X-User-Message": "1" });
  const error = await shareConversation("nb-1", "conv-1", "a2", 1).catch((reason) => reason);
  expect(httpErrorStatus(error)).toBe(403);
  expect(toUserMessage(error, "暂时无法公开分享")).toBe("这条会话引用了其他成员的个人记忆，不能公开分享。");
});

test("自相矛盾的 409（新增大于总数）不被当成披露确认，落回普通错误", async () => {
  respond = () => json(409, { detail: { code: "share_disclosure_required", memory_count: 1, new_memory_count: 3 } });
  const error = await shareConversation("nb-1", "conv-1", "a2").catch((reason) => reason);
  expect(error).not.toBeInstanceOf(ShareDisclosureRequired);
  expect(httpErrorStatus(error)).toBe(409);
});
