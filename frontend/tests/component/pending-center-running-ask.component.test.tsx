import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi, afterEach, beforeEach } from "vitest";

import { PendingBell, type PendingItem } from "../../app/pending-center.tsx";

// 待确认中心的「进行中的提问」分组:用户离开页面(或刷新)之后回来找「我刚才那个
// 问题跑到哪了」,铃铛要能给出它、并把他送回那个会话。后端只带回摘要与两个 id,
// 接回逻辑由会话详情承担——这里钉的是呈现与跳转这两件铃铛自己负责的事。

const ASK: PendingItem = {
  type: "ask",
  state: "running",
  job_id: "askjob-1",
  notebook_id: "nb1",
  notebook_name: "封装工艺库",
  conversation_id: "conv-9",
  title: "封装翘曲的主要成因",
  asked_at: "2026-09-07T10:00:00Z",
};

function renderBell(items: PendingItem[], onOpenItem = vi.fn()) {
  render(
    <PendingBell
      snapshot={{ count: 0, items }}
      doneItems={[]}
      userId="u1"
      onOpenItem={onOpenItem}
      onOpenDone={vi.fn()}
      onDismissDone={vi.fn()}
    />,
  );
  return onOpenItem;
}

beforeEach(() => {
  // 「已进行多久」由组件读时钟算出;钉死时钟,断言才不会随执行时刻漂移。
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-07T10:03:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

test("进行中的提问单独成组,带库名、问题摘要与已进行时长", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  renderBell([ASK]);

  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByText("进行中的提问")).toBeInTheDocument();
  expect(screen.getByText("封装工艺库")).toBeInTheDocument();
  expect(screen.getByText("封装翘曲的主要成因 · 已进行 3 分钟")).toBeInTheDocument();
});

test("点击进行中的提问把整条待办交给上层深链(带会话 id)", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const onOpenItem = renderBell([ASK]);

  await user.click(screen.getByLabelText("待确认中心"));
  await user.click(screen.getByText("封装翘曲的主要成因 · 已进行 3 分钟"));

  expect(onOpenItem).toHaveBeenCalledTimes(1);
  const item = onOpenItem.mock.calls[0][0] as PendingItem;
  expect(item.type).toBe("ask");
  expect(item.notebook_id).toBe("nb1");
  // conversation_id 是「回到那个会话」的唯一凭据 —— 丢了它就只能打开笔记本,
  // 用户还得自己在会话列表里找回刚才那条。
  expect(item.conversation_id).toBe("conv-9");
});

test("提交时刻缺失(旧行 / 不带该字段的客户端)时只省略时长,条目照常可点", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const onOpenItem = renderBell([{ ...ASK, asked_at: "" }]);

  await user.click(screen.getByLabelText("待确认中心"));
  const row = screen.getByText("封装翘曲的主要成因");
  expect(row).toBeInTheDocument();
  await user.click(row);
  expect(onOpenItem).toHaveBeenCalledTimes(1);
});

test("进行中的提问不进未读徽标,同一帧里的其它待办照常计数", async () => {
  renderBell([
    ASK,
    { type: "index", notebook_id: "nb2", notebook_name: "NB2", state: "suggested" },
  ]);

  // 徽标只反映 index 那一条。提问是用户几秒前自己发起的,给它记未读会让铃铛
  // 每问一个问题就亮一次红点。
  expect(screen.getByText("1")).toBeInTheDocument();
});

// --- 「已进行多久」的时钟(30s 一跳) ---------------------------------------
//
// 这一段守的是 pending-center 里那条 `setInterval`。它有两个容易被改坏、又不会被
// 任何呈现类断言抓到的性质:①「关掉面板/卸载就停表」——清理函数真的 clearInterval;
// ②「面板没打开就不起表」——`!open` 那半个条件还在。两者坏掉都不改变任何一屏内容,
// 只是让一个铃铛在后台每 30 秒醒一次、永远不停。

test("面板打开时每 30 秒重算一次已进行时长", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  // 提交后 50 秒:还在「刚刚开始」那一档,再走 30 秒就跨过 1 分钟。
  vi.setSystemTime(new Date("2026-09-07T10:00:50Z"));
  renderBell([ASK]);

  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByText("封装翘曲的主要成因 · 刚刚开始")).toBeInTheDocument();

  await act(async () => { vi.advanceTimersByTime(30_000); });
  // 快照没有变(在途提问期间后端不推),变的只有这块表。
  expect(screen.getByText("封装翘曲的主要成因 · 已进行 1 分钟")).toBeInTheDocument();
});

test("关掉面板即停表:定时器被清掉,再走 60 秒不再有任何唤醒", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  vi.setSystemTime(new Date("2026-09-07T10:00:50Z"));
  renderBell([ASK]);

  await user.click(screen.getByLabelText("待确认中心"));
  expect(vi.getTimerCount()).toBeGreaterThan(0);

  await user.click(screen.getByLabelText("待确认中心"));   // 收起面板
  expect(screen.queryByText("进行中的提问")).not.toBeInTheDocument();
  // 清理函数若不 clearInterval(比如写成 `return undefined`),这里仍会留着一条
  // 每 30 秒醒一次的定时器 —— 面板关着,谁也看不见,只是白白重渲染到天荒地老。
  expect(vi.getTimerCount()).toBe(0);
  await act(async () => { vi.advanceTimersByTime(60_000); });
  expect(vi.getTimerCount()).toBe(0);
});

test("面板没打开就不起表(有在途提问也一样)", () => {
  vi.setSystemTime(new Date("2026-09-07T10:00:50Z"));
  renderBell([ASK]);

  // `open` 那半个条件被去掉的话,铃铛只要挂在页面上就开始每 30 秒醒一次 ——
  // 而在那 30 秒里屏幕上根本没有这块表。
  expect(vi.getTimerCount()).toBe(0);
});

test("卸载时清掉定时器", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  vi.setSystemTime(new Date("2026-09-07T10:00:50Z"));
  const { unmount } = render(
    <PendingBell
      snapshot={{ count: 0, items: [ASK] }}
      doneItems={[]}
      userId="u1"
      onOpenItem={vi.fn()}
      onOpenDone={vi.fn()}
      onDismissDone={vi.fn()}
    />,
  );

  await user.click(screen.getByLabelText("待确认中心"));
  expect(vi.getTimerCount()).toBeGreaterThan(0);
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});
