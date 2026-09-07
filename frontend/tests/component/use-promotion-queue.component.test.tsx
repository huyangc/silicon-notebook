// 「内容审核」队列 owner 单独的行为覆盖（PR-5 分片 1）。没有 `Home`、没有真的协调器——
// 只给一个假的窄命令面，判三条契约：开窗走冻结票据、决策落定后刷新两处、票据过期后
// 一个字都不许写回去（原本这三条只能靠 root-modal-boundary 的源码守卫近似钉住）。
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { PromotionCandidate } from "../../app/promotion-queue";

const promotionApi = vi.hoisted(() => ({
  approvePromotion: vi.fn(),
  fetchPromotionQueue: vi.fn(),
  rejectPromotion: vi.fn(),
}));

vi.mock("../../app/promotion-queue", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/promotion-queue")>()),
  ...promotionApi,
}));

import { usePromotionQueue, type PromotionQueueModals } from "../../app/use-promotion-queue";

const candidate = { id: "cand-1", status: "proposed" } as unknown as PromotionCandidate;

// 一个可以被"作废"的假票据面：`current` 为 false 时 publish/owns 都拒绝，模拟请求
// 在途期间用户切了用户/笔记本。
function fakeModals() {
  const lease = { slot: "promotion-queue", owner: { kind: "actor" } } as never;
  const state = { current: true, hasActive: true, issued: 0 };
  const modals = {
    captureActorOwner: () => ({ kind: "actor" }) as never,
    issue: () => { state.issued += 1; return lease; },
    publish: () => state.current,
    leaseIsCurrent: () => state.current,
    owns: () => state.current,
    activeLease: () => (state.hasActive ? lease : null),
  } as unknown as PromotionQueueModals;
  return { modals, state };
}

function harness(overrides: { refreshCollection?: () => Promise<void> } = {}) {
  const notify = vi.fn();
  const refreshCollection = overrides.refreshCollection ?? vi.fn(async () => {});
  const { modals, state } = fakeModals();
  const rendered = renderHook(() => usePromotionQueue({
    modals,
    effects: { notify, refreshCollection },
  }));
  return { ...rendered, notify, refreshCollection, state };
}

test("开窗先发冻结票据再取数，publish 通过才把队列画出来", async () => {
  promotionApi.fetchPromotionQueue.mockResolvedValue([candidate]);
  const { result, state } = harness();

  expect(result.current.view.candidates).toEqual([]);
  await act(async () => { await result.current.openPromoQueue(); });

  expect(state.issued).toBe(1);
  expect(result.current.view.candidates).toEqual([candidate]);
});

test("请求在途期间票据作废：数据不落进新窗口，错误也不再抛给调用方", async () => {
  const { result, state } = harness();
  promotionApi.fetchPromotionQueue.mockImplementation(async () => {
    state.current = false;
    return [candidate];
  });

  await act(async () => { await result.current.openPromoQueue(); });
  expect(result.current.view.candidates).toEqual([]);

  promotionApi.fetchPromotionQueue.mockRejectedValue(new Error("boom"));
  await act(async () => { await expect(result.current.openPromoQueue()).resolves.toBeUndefined(); });
});

test("批准：调用 API、给出合并说明、重取队列并刷新笔记本集合", async () => {
  promotionApi.approvePromotion.mockResolvedValue({ merged_into: "abcdefgh1234" });
  promotionApi.fetchPromotionQueue.mockResolvedValue([]);
  const { result, notify, refreshCollection } = harness();

  await act(async () => { await result.current.decidePromotion("cand-1", "approve"); });

  expect(promotionApi.approvePromotion).toHaveBeenCalledWith("cand-1");
  expect(notify).toHaveBeenCalledWith("已批准收录（与 abcdefgh 合并），内容已加入公共知识库");
  expect(promotionApi.fetchPromotionQueue).toHaveBeenCalled();
  expect(refreshCollection).toHaveBeenCalledTimes(1);
  expect(result.current.view.busy).toBe(false);
});

test("拒绝：带 reason 调用 API，文案说明个人内容不变", async () => {
  promotionApi.rejectPromotion.mockResolvedValue(undefined);
  promotionApi.fetchPromotionQueue.mockResolvedValue([]);
  const { result, notify } = harness();

  await act(async () => { await result.current.decidePromotion("cand-1", "reject", "证据不足"); });

  expect(promotionApi.rejectPromotion).toHaveBeenCalledWith("cand-1", "证据不足");
  expect(notify).toHaveBeenCalledWith("贡献未采纳，个人内容保持不变");
});

test("写入期间 busy 置位，同一时刻不许发第二笔（防同一候选被批准两次）", async () => {
  let release: () => void = () => {};
  promotionApi.approvePromotion.mockImplementation(() => new Promise((resolve) => {
    release = () => resolve({ merged_into: "" });
  }));
  promotionApi.fetchPromotionQueue.mockResolvedValue([]);
  const { result } = harness();

  let inFlight: Promise<void> = Promise.resolve();
  act(() => { inFlight = result.current.decidePromotion("cand-1", "approve"); });
  await waitFor(() => expect(result.current.view.busy).toBe(true));

  await act(async () => { await result.current.decidePromotion("cand-1", "reject"); });
  expect(promotionApi.rejectPromotion).not.toHaveBeenCalled();

  await act(async () => { release(); await inFlight; });
  expect(result.current.view.busy).toBe(false);
});

// 关闭弹窗 ≠ 那笔 HTTP 请求结束：clearQueue 只丢可见载荷，单飞闸与 busy 保持原样。
test("clearQueue 只清可见载荷，不释放在飞的单飞闸", async () => {
  let release: () => void = () => {};
  promotionApi.approvePromotion.mockImplementation(() => new Promise((resolve) => {
    release = () => resolve({ merged_into: "" });
  }));
  promotionApi.fetchPromotionQueue.mockResolvedValue([candidate]);
  const { result } = harness();

  await act(async () => { await result.current.openPromoQueue(); });
  expect(result.current.view.candidates).toEqual([candidate]);

  let inFlight: Promise<void> = Promise.resolve();
  act(() => { inFlight = result.current.decidePromotion("cand-1", "approve"); });
  await waitFor(() => expect(result.current.view.busy).toBe(true));

  act(() => { result.current.clearQueue(); });
  expect(result.current.view.candidates).toEqual([]);
  expect(result.current.view.busy).toBe(true);

  // clearQueue 没放开单飞闸：关掉弹窗后再点一次决策，仍必须被 promoOperationRef
  // 挡住——API 依旧只调过一次，不会让同一候选被批准两次。
  await act(async () => { await result.current.decidePromotion("cand-1", "reject"); });
  expect(promotionApi.approvePromotion).toHaveBeenCalledTimes(1);
  expect(promotionApi.rejectPromotion).not.toHaveBeenCalled();

  await act(async () => { release(); await inFlight; });
});
