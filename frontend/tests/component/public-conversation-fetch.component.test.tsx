import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../../app/api-client.ts", () => ({ requestJson: mocks.requestJson }));

import { humanizedError } from "../../app/errors";
import { fetchPublicConversation } from "../../app/public-conversation";

beforeEach(() => mocks.requestJson.mockReset());

test("404 读作 missing，410 读作 expired，其余错误照常抛出", async () => {
  mocks.requestJson.mockRejectedValueOnce(humanizedError("不存在", 404));
  await expect(fetchPublicConversation("t")).resolves.toEqual({ kind: "missing" });

  mocks.requestJson.mockRejectedValueOnce(humanizedError("链接已过期，请重新提问", 410));
  await expect(fetchPublicConversation("t")).resolves.toEqual({ kind: "expired" });

  const boom = humanizedError("服务异常", 500);
  mocks.requestJson.mockRejectedValueOnce(boom);
  await expect(fetchPublicConversation("t")).rejects.toBe(boom);
});

test("成功体归一为 ready，turns 缺省字段补齐并保留 expires_at", async () => {
  mocks.requestJson.mockResolvedValueOnce({
    title: "T", created_at: "", shared_at: "", truncated_turns: false,
    expires_at: "2030-01-02T03:04:05Z",
    turns: [{ question: "q", answer_md: "a" }],
  });
  const result = await fetchPublicConversation("t");
  expect(result.kind).toBe("ready");
  if (result.kind !== "ready") return;
  expect(result.conversation.expires_at).toBe("2030-01-02T03:04:05Z");
  expect(result.conversation.turns[0].references).toEqual([]);
  expect(result.conversation.turns[0].images).toEqual([]);
});

test("空口令不发请求，直接 missing", async () => {
  await expect(fetchPublicConversation("  ")).resolves.toEqual({ kind: "missing" });
  expect(mocks.requestJson).not.toHaveBeenCalled();
});
