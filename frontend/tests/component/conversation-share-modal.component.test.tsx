import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

// codex #522 R6 P1 的回归门:分享状态加载**非 404 失败**时,当前是否已分享/水位**未知**,
// 必须**禁用**分享并给可操作错误——绝不能让 CTA 可点而发一个空 expected,那会让服务端按
// 当前最新兜底发布(可能推进隐藏分享、或在未显示任何披露的情况下公开个人记忆)。同时钉住
// 「分享状态与会话详情各自成败」:详情失败(countsError)仍可分享,与本条正交。
//
// ask-api 被 mock(四个端点),errors.ts/披露纯逻辑/FloatingModalCard 全部保持真实实现——
// 本文件钉的正是「非 404 → 禁用」这条接线,把 httpErrorStatus / 状态位判定 mock 掉就只剩壳子。

const mocks = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getConversationShare: vi.fn(),
  shareConversation: vi.fn(),
  unshareConversation: vi.fn(),
}));

vi.mock("../../app/ask-api.ts", () => ({
  getConversation: mocks.getConversation,
  getConversationShare: mocks.getConversationShare,
  shareConversation: mocks.shareConversation,
  unshareConversation: mocks.unshareConversation,
}));

import { ConversationShareModal } from "../../app/conversation-share-modal.tsx";
import { humanizedError } from "../../app/errors.ts";
import { SHARE_DISCLOSURE_COUNTS_ERROR } from "../../app/conversation-share-disclosure.ts";

// 两轮问答,末轮引用一条个人记忆——分享披露算得出 memoryCount=1;末条 answer_id 是
// 「分享」将钉死的 expected_through_id。
const DETAIL = {
  id: "conv-1",
  notebook_id: "nb-1",
  title: "t",
  updated_at: "2026-01-01T00:00:02",
  turn_count: 2,
  turns: [
    {
      answer_id: "a1",
      question: "q1",
      response: { anchors: [], citations: [] },
      asked_at: "2026-01-01T00:00:00",
      created_at: "2026-01-01T00:00:00",
    },
    {
      answer_id: "a2",
      question: "q2",
      response: { anchors: [], citations: [{ memory_id: "m1", images: [] }] },
      asked_at: "2026-01-01T00:00:01",
      created_at: "2026-01-01T00:00:01",
    },
  ],
};

function renderModal(throughAnswerId = "") {
  return render(
    <ConversationShareModal
      notebookId="nb-1"
      conversationId="conv-1"
      title="一次问答"
      throughAnswerId={throughAnswerId}
      onClose={vi.fn()}
    />,
  );
}

beforeEach(() => {
  mocks.getConversation.mockReset();
  mocks.getConversationShare.mockReset();
  mocks.shareConversation.mockReset();
  mocks.unshareConversation.mockReset();
});

test("分享状态非 404 加载失败 → 禁用分享 CTA、显示错误、绝不发 POST", async () => {
  // 网络失败:无状态码 → httpErrorStatus 返回 undefined → 走非 404 分支。
  mocks.getConversationShare.mockRejectedValue(new Error("网络中断"));
  mocks.getConversation.mockResolvedValue(DETAIL); // 详情成功,但分享状态未知

  renderModal();

  const cta = await screen.findByRole("button", { name: /生成分享链接/ });
  // 未知分享状态下必须禁用——这是本条的核心。
  expect(cta).toBeDisabled();
  // 可操作错误上屏(toUserMessage 对无状态错误退回 fallback 文案)。
  expect(screen.getByText("分享状态加载失败，请重试")).toBeInTheDocument();

  // 点击被禁用的按钮不发请求;防御性 handler 也复查 shareStateError。
  fireEvent.click(cta);
  await Promise.resolve();
  expect(mocks.shareConversation).not.toHaveBeenCalled();
});

test("分享状态 404(未分享)+ 详情成功 → 正常可分享,expected 钉在末条", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockResolvedValue(DETAIL);
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });

  renderModal();

  const cta = await screen.findByRole("button", { name: /生成分享链接/ });
  expect(cta).not.toBeDisabled();
  // 披露算得出(不是 countsError 兜底)——末轮的个人记忆被数出来。
  expect(
    screen.getByText(/公开页会包含 1 条你引用到的个人记忆摘录/),
  ).toBeInTheDocument();

  fireEvent.click(cta);
  // 水位钉死在弹窗据以算披露的那批 turns 的末条 answer_id(a2)。
  await waitFor(() =>
    expect(mocks.shareConversation).toHaveBeenCalledWith("nb-1", "conv-1", "a2"),
  );
});

test("详情失败 + 分享状态成功 → 仍可分享,披露退化成不带数字的兜底(countsError)", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockRejectedValue(new Error("详情加载失败"));

  renderModal();

  const cta = await screen.findByRole("button", { name: /生成分享链接/ });
  // 详情失败只影响披露计数,绝不拦住分享本身(与 shareStateError 正交)。
  expect(cta).not.toBeDisabled();
  // 附图与个人记忆两面缺一不可的兜底文案。
  expect(screen.getByText(SHARE_DISCLOSURE_COUNTS_ERROR)).toBeInTheDocument();
});


// --- 边界模式：每条回答下面的分享按钮（T6）----------------------------------
//
// 「分享到这一条」与会话列表那个按钮共用同一个弹窗、同一个 POST，差别只在
// `expected_through_id` 钉在哪条答案上。三条钉住的性质，每条都对应一个会静默发多的
// 失败方向。

test("边界模式：expected 钉在用户点的那条,而不是会话最新那条", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockResolvedValue(DETAIL);
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1",
  });

  renderModal("a1"); // 两轮会话里点**第一轮**的分享

  const cta = await screen.findByRole("button", { name: /分享到这一条/ });
  // 抬头说清发布到第几轮——用户点的按钮在回答旁边,弹窗里必须能对上。
  expect(screen.getByText(/分享至第 1 轮回答（本会话共 2 轮）/)).toBeInTheDocument();
  // 披露按**截断后**那批算:第二轮那条个人记忆不在范围内,所以这句不该出现。
  expect(screen.queryByText(/条你引用到的个人记忆摘录/)).toBeNull();

  fireEvent.click(cta);
  await waitFor(() =>
    expect(mocks.shareConversation).toHaveBeenCalledWith("nb-1", "conv-1", "a1"),
  );
});

test("边界模式 + 详情加载失败：仍发用户点的那条 id,绝不回退成空(那会按最新发布)", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockRejectedValue(new Error("详情加载失败"));
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1",
  });

  renderModal("a1");

  const cta = await screen.findByRole("button", { name: /分享到这一条/ });
  expect(cta).not.toBeDisabled();
  expect(screen.getByText(SHARE_DISCLOSURE_COUNTS_ERROR)).toBeInTheDocument();

  fireEvent.click(cta);
  // 空 expected 会让服务端按当前最新发布 —— 界面写着「分享到这一条」,发出去却是整条会话。
  await waitFor(() =>
    expect(mocks.shareConversation).toHaveBeenCalledWith("nb-1", "conv-1", "a1"),
  );
});

test("水位已越过边界:不给发布按钮,说明现状与出路(后端水位 advance-only,收回只会 409)", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2", // 已分享到第二轮
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1"); // 却点了第一轮的分享

  await screen.findByLabelText("分享链接");
  expect(screen.getByText(/这条回答已经在链接里了/)).toBeInTheDocument();
  expect(screen.getByText(/还多包含它之后的 1 轮/)).toBeInTheDocument();
  // 发布动作一个都不给：既没有「分享到这一条」也没有「更新到这一条」。
  expect(screen.queryByRole("button", { name: /分享到这一条/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /更新到这一条/ })).toBeNull();
  // 出路仍在（撤销后重发即可钉到更早的边界）。
  expect(screen.getByRole("button", { name: /撤销分享/ })).toBeInTheDocument();
  // ⚠ 披露按**完整** turns 算:链接实际公开到 a2,少报会把第二轮那条个人记忆藏掉。
  expect(
    screen.getByText(/公开页会包含 1 条你引用到的个人记忆摘录/),
  ).toBeInTheDocument();
});

test("水位落后于边界:给「更新到这一条」,推进后 expected 仍是用户点的那条", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1", // 只分享到第一轮
  });
  mocks.getConversation.mockResolvedValue(DETAIL);
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });

  renderModal("a2"); // 点第二轮的分享

  const cta = await screen.findByRole("button", { name: /更新到这一条/ });
  // 措辞不得写成「更新到最新」——这个按钮推进到的是用户点的那条。
  expect(screen.queryByRole("button", { name: /更新到最新/ })).toBeNull();

  fireEvent.click(cta);
  await waitFor(() =>
    expect(mocks.shareConversation).toHaveBeenCalledWith("nb-1", "conv-1", "a2"),
  );
});

test("非边界模式（会话列表那个按钮）措辞与行为逐字不变", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1",
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal(); // 空边界

  expect(await screen.findByRole("button", { name: /更新到最新/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /更新到这一条/ })).toBeNull();
  expect(screen.queryByText(/分享至第/)).toBeNull();
});


// ⚠ 上面那条 ahead 用例的 `newCount` 恰好是 0（水位就在末轮），所以它**证不到**
// `!watermarkAhead` 这一半守卫——去掉它那条用例照样绿。这一条补上区分度：水位越过边界
// **且**水位之后还有新轮，此时 newCount>0，只有 ahead 判据能拦住那颗按钮。按下去服务端
// 只会 409（边界 a1 排在已发布水位 a2 之前），而按钮上写的是「更新」。
const DETAIL3 = {
  ...DETAIL,
  turn_count: 3,
  turns: [
    ...DETAIL.turns,
    {
      answer_id: "a3",
      question: "q3",
      response: { anchors: [], citations: [] },
      asked_at: "2026-01-01T00:00:02",
      created_at: "2026-01-01T00:00:02",
    },
  ],
};

test("水位越过边界且其后还有新轮:仍然一个发布按钮都不给（newCount>0 拦不住它，ahead 才行）", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2", // 已分享到第二轮，第三轮还没发布
  });
  mocks.getConversation.mockResolvedValue(DETAIL3);

  renderModal("a1"); // 点第一轮的分享

  await screen.findByLabelText("分享链接");
  // 确认这一支确实有「未包含的新增轮」——即 newCount>0，单靠它拦不住按钮。
  expect(screen.getByText(/之后新增 1 轮未包含/)).toBeInTheDocument();
  expect(screen.getByText(/这条回答已经在链接里了/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /更新到这一条/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /更新到最新/ })).toBeNull();
  expect(mocks.shareConversation).not.toHaveBeenCalled();
});


// --- codex #530 R1 的两条回归门 ----------------------------------------------

test("codex #530 R1 P1：水位越过边界时，绝不承诺「只包含这条回答之前」", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1");

  // 复制按钮就在这句话下面：读完它直接复制发出去，发的会是比承诺更多的轮次。
  await screen.findByLabelText("分享链接");
  expect(screen.queryByText(/只包含这条回答以及它之前的问答/)).toBeNull();
  expect(screen.getByText(/覆盖的范围比你点的这条回答更靠后/)).toBeInTheDocument();
});

test("codex #530 R1 P2：边界模式的推进回执说「已更新到这一条」，不说「已更新到最新」", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1",
  });
  mocks.getConversation.mockResolvedValue(DETAIL3); // a1..a3，推进到 a2 时 a3 仍未公开
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });

  renderModal("a2");

  fireEvent.click(await screen.findByRole("button", { name: /更新到这一条/ }));
  expect(await screen.findByText("已更新到这一条")).toBeInTheDocument();
  expect(screen.queryByText("已更新到最新")).toBeNull();
});

test("非边界模式的两条回执逐字不变", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockResolvedValue(DETAIL);
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });

  renderModal();

  fireEvent.click(await screen.findByRole("button", { name: /生成分享链接/ }));
  expect(await screen.findByText("已生成分享链接")).toBeInTheDocument();
});


// --- codex #530 R2 的两条回归门 ----------------------------------------------
//
// 两条都是「链接与复制按钮当场可用，而上方那句话说的是别的范围」。

test("codex #530 R2 P2：链接停在更早一轮时，说清这条回答**还没**进链接", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1", // 链接只到第一轮
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a2"); // 用户点的是第二轮

  await screen.findByLabelText("分享链接");
  // 照着「只包含这条回答以及它之前的问答」复制，发出去的快照缺了这条回答。
  expect(screen.queryByText(/只包含这条回答以及它之前的问答/)).toBeNull();
  expect(screen.getByText(/还不包含这条回答/)).toBeInTheDocument();
  // 出路仍在同屏。
  expect(screen.getByRole("button", { name: /更新到这一条/ })).toBeInTheDocument();
});

test("codex #530 R2 P1：水位指向本地看不到的答案时，范围声明为未知而不是更窄", async () => {
  // 读完详情之后，另一个标签页把分享推进到了 turns 里没有的一轮。
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:09",
    shared_through_id: "a9-not-loaded-here",
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1");

  await screen.findByLabelText("分享链接");
  expect(screen.queryByText(/只包含这条回答以及它之前的问答/)).toBeNull();
  expect(screen.getByText(/无法确认当前链接的范围/)).toBeInTheDocument();
  // 数字答不上来就不给数字：退化成同时点名附图与个人记忆的兜底文案。
  expect(screen.getByText(SHARE_DISCLOSURE_COUNTS_ERROR)).toBeInTheDocument();
});

test("链接正好停在这条回答上：如实说「就到这条回答为止」", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a2");

  await screen.findByLabelText("分享链接");
  expect(screen.getByText(/链接的内容就到这条回答为止/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /更新到这一条/ })).toBeNull();
});

test("非边界模式的介绍语逐字不变", async () => {
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1",
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal();

  expect(
    await screen.findByText(/分享后新问的问题不会自动出现，需要再点一次「更新到最新」/),
  ).toBeInTheDocument();
});


// --- codex #530 R3：弹窗打开之后的漂移 ---------------------------------------
//
// token 是稳定的，所以另一个标签页推进同一条分享时，这边的链接当场就指向更多轮次，
// 而范围文案还停在初次加载那一刻。复制是那句声明变成行动的一刻，闸放在那里。

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
    writable: true,
  });
  return writeText;
}

test("codex #530 R3 P1：复制前复核水位，别处已推进则刷新并拦下这次复制", async () => {
  const writeText = stubClipboard();
  mocks.getConversationShare
    .mockResolvedValueOnce({
      share_token: "tok-1",
      shared_through_at: "2026-01-01T00:00:00",
      shared_through_id: "a1",
    })
    // 弹窗打开期间，另一个标签页把同一条分享推到了 a2。
    .mockResolvedValue({
      share_token: "tok-1",
      shared_through_at: "2026-01-01T00:00:01",
      shared_through_id: "a2",
    });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1");

  // 初次加载：链接正好停在这条回答上。
  expect(await screen.findByText(/链接的内容就到这条回答为止/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /复制/ }));

  await waitFor(() =>
    expect(screen.getByText(/分享范围已在别处变化/)).toBeInTheDocument(),
  );
  // 这一次不复制：那句范围声明已经不成立了。
  expect(writeText).not.toHaveBeenCalled();
  // 刷新后如实改口成「已越过这条回答」。
  expect(screen.getByText(/这条回答已经在链接里了/)).toBeInTheDocument();
});

test("水位没变时复制照常，只是多了一次复核", async () => {
  const writeText = stubClipboard();
  mocks.getConversationShare.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:00",
    shared_through_id: "a1",
  });
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1");

  fireEvent.click(await screen.findByRole("button", { name: /复制/ }));

  await waitFor(() => expect(screen.getByText("分享链接已复制")).toBeInTheDocument());
  expect(writeText).toHaveBeenCalledTimes(1);
});

test("复核时发现已在别处撤销：拦下复制，界面回到未分享", async () => {
  const writeText = stubClipboard();
  mocks.getConversationShare
    .mockResolvedValueOnce({
      share_token: "tok-1",
      shared_through_at: "2026-01-01T00:00:00",
      shared_through_id: "a1",
    })
    .mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1");

  fireEvent.click(await screen.findByRole("button", { name: /复制/ }));

  await waitFor(() =>
    expect(screen.getByText(/分享范围已在别处变化/)).toBeInTheDocument(),
  );
  expect(writeText).not.toHaveBeenCalled();
  expect(await screen.findByRole("button", { name: /分享到这一条/ })).toBeInTheDocument();
});


test("codex #530 R4 P2：复核读不到时说「读不到」，绝不谎称范围已变化", async () => {
  const writeText = stubClipboard();
  mocks.getConversationShare
    .mockResolvedValueOnce({
      share_token: "tok-1",
      shared_through_at: "2026-01-01T00:00:00",
      shared_through_id: "a1",
    })
    .mockRejectedValue(new Error("网络中断")); // 非 404 的瞬时失败
  mocks.getConversation.mockResolvedValue(DETAIL);

  renderModal("a1");

  fireEvent.click(await screen.findByRole("button", { name: /复制/ }));

  await waitFor(() =>
    expect(screen.getByText(/暂时无法确认分享范围/)).toBeInTheDocument(),
  );
  // 什么都没变，也什么都没刷新——不能说成「已在别处变化」。
  expect(screen.queryByText(/分享范围已在别处变化/)).toBeNull();
  // 拦下复制仍是刻意的 fail-closed：读不到就证明不了那句范围声明还成立。
  expect(writeText).not.toHaveBeenCalled();
  // 界面确实没被改动：范围文案仍是初次加载那一句。
  expect(screen.getByText(/链接的内容就到这条回答为止/)).toBeInTheDocument();
});
