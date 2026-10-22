import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

// 分享之后，有一轮引用的库分享者已读不了（链接本身仍能打开）。
//
// 场景是一条 30 轮的会话，分享到第 20 轮；第 25 轮用的库，分享者已失去读权。后端在这个
// 场景下的实际回执（两个分支都在真后端上跑过）如下：
//   · 会话详情 `GET /global-ask/conversations/{id}` → 404「部分笔记本已无法访问」。接入确数端点
//     之前，弹窗翻页取轮次时就会撞上这个 404，然后走兜底；
//   · 确数端点 → 同一个 404：它数到的窗口里有那一轮；
//   · 个人记忆披露：截到第 20 轮 200，截到更后面 404；更新 POST → 404。
// 下面钉住的界面，与接入确数端点之前的弹窗**逐字相同**（两个分支同一个用例对拍过）：
//   · 会话列表入口：保留链接、「内容截至…」和兜底披露；没有「更新到最新」，也没有偏小的数字；
//   · 回答下的入口：范围文案按「无法确认」说，「更新到这一条」仍可点，点了之后那句读权失败的
//     原因就地出现。

const net = vi.hoisted(() => ({ requestJson: vi.fn(), requestVoid: vi.fn(), perform: vi.fn() }));
vi.mock("../../app/api-client.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/api-client.ts")>(),
  requestJson: net.requestJson, requestVoid: net.requestVoid, performApiRequest: net.perform,
}));

import { ConversationShareModal } from "../../app/conversation-share-modal.tsx";
import { globalConversationShareApi } from "../../app/global-ask-api.ts";
import { humanizedError } from "../../app/errors.ts";

const LOST = "部分笔记本已无法访问，请重新选择范围。";

function wire() {
  net.requestJson.mockImplementation(async (path: string) => {
    if (path.endsWith("/share")) {
      return { share_token: "gshr-x", shared_through_at: "2026-01-01T00:00:19+00:00", shared_through_id: "j19" };
    }
    if (path.includes("/share/disclosure?")) {
      if (new URLSearchParams(path.split("?")[1]).get("through_id") === "j19") return { memory_count: 0, new_memory_count: 0 };
      throw humanizedError(LOST, 404);
    }
    if (path.includes("/share/counts?")) throw humanizedError(LOST, 404);
    if (path.startsWith("/global-ask/conversations/gconv-lost?")) throw humanizedError(LOST, 404);
    throw new Error(`unexpected ${path}`);
  });
  net.perform.mockImplementation(async () => new Response(JSON.stringify({ detail: LOST }), {
    status: 404, headers: { "Content-Type": "application/json", "X-User-Message": "1" },
  }));
}

async function open(throughAnswerId: string) {
  wire();
  const view = render(
    <ConversationShareModal api={globalConversationShareApi("gconv-lost")} title="会话" throughAnswerId={throughAnswerId} onClose={() => {}} />,
  );
  await waitFor(() => expect(screen.queryByText("正在加载…")).toBeNull());
  await screen.findByLabelText("分享链接");
  const lines = () => Array.from(view.container.querySelectorAll(".conversation-share-body p"))
    .map((p) => (p.textContent ?? "").replace(/^内容截至 .*$/, "内容截至 <时刻>"));
  const buttons = () => Array.from(view.container.querySelectorAll("button")).map((b) => b.textContent?.trim());
  return { lines, buttons };
}

test("会话列表入口：链接照旧，不给数字、不给「更新到最新」——与接入确数端点之前逐字相同", async () => {
  const modal = await open("");
  expect(modal.lines()).toEqual([
    "发布成一条免登录的只读快照。分享后新问的问题不会自动出现，需要再点一次「更新到最新」。撤销即刻失效。",
    "内容截至 <时刻>",
    "公开页可能包含引用到的附图与个人记忆摘录（本次未能统计数量）。",
  ]);
  expect(modal.buttons()).toEqual(["×", "复制", "撤销分享"]);
});

test("回答下的入口：「更新到这一条」仍在，点了之后读权失败的那句话就地出现", async () => {
  const modal = await open("j29");
  expect(modal.lines()).toEqual([
    "无法确认当前链接的范围——它可能已在别处被推进到这里看不到的轮次。现在复制发出去的内容可能多于这条回答，请刷新后重新查看。",
    "内容截至 <时刻>",
    "「更新到这一条」会公开新增轮次，其中可能包含新引用的附图与个人记忆摘录（本次未能统计数量）。",
    "公开页可能包含引用到的附图与个人记忆摘录（本次未能统计数量）。",
  ]);
  expect(modal.buttons()).toEqual(["×", "复制", "更新到这一条", "撤销分享"]);

  fireEvent.click(screen.getByRole("button", { name: /更新到这一条/ }));
  expect(await screen.findByText(LOST)).toBeInTheDocument();
  expect(net.perform).toHaveBeenCalledTimes(1);
});
