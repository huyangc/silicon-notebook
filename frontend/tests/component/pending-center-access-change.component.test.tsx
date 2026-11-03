import { renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

// 分享编辑 P1:库主把我添加为编辑者时,铃铛推送 `notebook_collaborator_granted`。它改变的是
// 「我能看到哪些笔记本、能做什么」,所以收到即交给宿主的访问对账(page.tsx 接
// refreshAfterAccessChange),同时在铃铛里留一句可点的话。

const transport = vi.hoisted(() => ({ performApiRequest: vi.fn() }));
vi.mock("../../app/api-client.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/api-client.ts")>()),
  ...transport,
}));
vi.mock("../../app/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/auth")>()),
  getToken: () => "token",
}));

const { usePendingActions } = await import("../../app/pending-center.tsx");

function streamOf(lines: string[]): Response {
  const encoder = new TextEncoder();
  let sent = false;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (sent) return new Promise<void>(() => undefined); // 保持连接,不触发重连
      sent = true;
      controller.enqueue(encoder.encode(lines.map((line) => `${line}\n`).join("")));
      return undefined;
    },
  });
  return new Response(body, { status: 200 });
}

test("收到「被添加为编辑者」:调用宿主的访问对账,并在铃铛里留一条", async () => {
  transport.performApiRequest.mockImplementation(async (path: string) => {
    if (path === "/me/pending-actions/stream") {
      return streamOf([JSON.stringify({ kind: "event", event: "notebook_collaborator_granted", notebook_id: "nb-9" })]);
    }
    return new Response(JSON.stringify({ count: 0, items: [] }), { status: 200 });
  });
  const onAccessChanged = vi.fn();
  const { result, unmount } = renderHook(() => usePendingActions(true, onAccessChanged));

  await waitFor(() => expect(onAccessChanged).toHaveBeenCalledWith("nb-9"));
  await waitFor(() => expect(result.current.doneItems).toHaveLength(1));
  expect(result.current.doneItems[0]).toMatchObject({
    notebook_id: "nb-9",
    kind: "notebook_collaborator_granted",
    text: "你被添加为一本笔记本的编辑者，点击查看",
  });
  unmount();
});

test("其他推送事件不触发访问对账", async () => {
  transport.performApiRequest.mockImplementation(async (path: string) => {
    if (path === "/me/pending-actions/stream") {
      return streamOf([JSON.stringify({ kind: "event", event: "index_done", notebook_id: "nb-1", notebook_name: "库" })]);
    }
    return new Response(JSON.stringify({ count: 0, items: [] }), { status: 200 });
  });
  const onAccessChanged = vi.fn();
  const { result, unmount } = renderHook(() => usePendingActions(true, onAccessChanged));
  await waitFor(() => expect(result.current.doneItems).toHaveLength(1));
  expect(onAccessChanged).not.toHaveBeenCalled();
  unmount();
});
