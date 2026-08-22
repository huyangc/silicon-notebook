import { StrictMode, useState } from "react";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/command-catalog-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/command-catalog-api.ts")>();
  return {
    ...actual,
    fetchCommandCatalogPreview: vi.fn(),
    startCommandCatalog: vi.fn(),
    fetchCommandCatalogJob: vi.fn(),
    cancelCommandCatalog: vi.fn(),
    fetchCommandCatalogCandidates: vi.fn(),
    applyCommandCatalog: vi.fn(),
    dismissCommandCatalog: vi.fn(),
  };
});

import {
  applyCommandCatalog,
  cancelCommandCatalog,
  dismissCommandCatalog,
  fetchCommandCatalogCandidates,
  fetchCommandCatalogJob,
  fetchCommandCatalogPreview,
  startCommandCatalog,
  type CommandCatalogApplyResult,
  type CommandCatalogCandidate,
  type CommandCatalogDismissResult,
  type CommandCatalogJob,
  type CommandCatalogProgress,
} from "../../app/command-catalog-api.ts";
import { humanizedError } from "../../app/errors.ts";
import {
  CommandCatalogReview,
  CommandCatalogSection,
  type CatalogConfirmRequest,
  type CatalogReviewRequest,
} from "../../app/command-catalog-panel.tsx";

type JobOverrides = Partial<Omit<CommandCatalogJob, "progress">> & {
  progress?: Partial<CommandCatalogProgress>;
};

function job(overrides: JobOverrides = {}): CommandCatalogJob {
  return {
    id: "job-1",
    notebook_id: "nb-1",
    source_id: "src-1",
    status: "running",
    failure_reason: "",
    created_at: "",
    updated_at: "",
    finished_at: "",
    ...overrides,
    progress: {
      sections_total: 0,
      sections_done: 0,
      entries: 0,
      rejected: 0,
      uncovered: 0,
      pending_candidates: 0,
      ...(overrides.progress ?? {}),
    },
  };
}

function candidate(overrides: Partial<CommandCatalogCandidate> = {}): CommandCatalogCandidate {
  return {
    id: "c1",
    position: 1,
    section_path: "第 3 章 > set_db",
    command_name: "set_db",
    state: "candidate",
    syntax: "set_db <name> <value>",
    description: "设置数据库属性",
    args: [{ name: "-name", required: true, desc: "属性名", default: "" }],
    examples: [],
    anchors: [],
    excerpt: "set_db 用于设置属性…",
    suspect_related: false,
    rejections: [],
    dismiss_reason: "",
    rejections_overflow: 0,
    desc_overflow: 0,
    ...overrides,
  };
}

const preview = {
  source_id: "src-1",
  source_title: "工具手册",
  estimated_windows: 12,
  estimated_calls: 18,
  windows_in_prefix: 8,
  skipped_windows_in_prefix: 3,
  sampled: true,
  element_limit: 4000,
};

let confirmed: CatalogConfirmRequest[] = [];
const onOpenTable = vi.fn();
const onToast = vi.fn();

type SectionProps = Parameters<typeof CommandCatalogSection>[0];
type HarnessProps = Partial<SectionProps> & {
  reviewIsCurrent?: () => boolean;
};

/**
 * P0 修复后,审阅弹窗(`CommandCatalogReview`)不再是 `CommandCatalogSection` 自己
 * 渲染的子树——它被提升到调用方(生产代码里是 page.tsx)根层,`CommandCatalogSection`
 * 只经 `onOpenReview` 请求打开。这个测试专用的 harness 组件复刻 page.tsx 那一小段
 * 「持有开关状态 + 根层渲染 CommandCatalogReview」的接线,好让既有的「点开审阅弹窗
 * 再操作」用例不用改断言逻辑,只需换一个更贴近生产接线形状的挂载点。
 */
function Harness({ reviewIsCurrent, ...props }: HarnessProps = {}) {
  const [review, setReview] = useState<CatalogReviewRequest | null>(null);
  // R8:page.tsx 那条 reviewSeq/onReviewed 接线也复刻进来——它是「确认完最后一条
  // 候选、关掉弹窗,重新识别就该可点」的唯一实现路径,harness 少接这一根线,那条
  // 用例就永远绿。
  const [reviewSeq, setReviewSeq] = useState(0);
  const canEdit = props.canEdit ?? true;
  return (
    <>
      <CommandCatalogSection
        notebookId="nb-1"
        sourceId="src-1"
        sourceTitle="工具手册"
        canEdit={canEdit}
        onConfirm={(request) => { confirmed.push(request); }}
        reviewSeq={reviewSeq}
        {...props}
        onOpenReview={setReview}
      />
      {review && (
        <CommandCatalogReview
          notebookId={review.notebookId}
          sourceId={review.sourceId}
          sourceTitle={review.sourceTitle}
          jobId={review.jobId}
          canEdit={canEdit}
          onClose={() => setReview(null)}
          onOpenTable={onOpenTable}
          onToast={onToast}
          isCurrent={reviewIsCurrent}
          onReviewed={() => setReviewSeq((seq) => seq + 1)}
        />
      )}
    </>
  );
}

function mount(props: HarnessProps = {}) {
  return render(<Harness {...props} />);
}

beforeEach(() => {
  confirmed = [];
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({ job: null });
  vi.mocked(fetchCommandCatalogPreview).mockResolvedValue(preview);
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [], next_cursor: 0, has_more: false, counts: {},
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("发起前先弹成本预告：约数 + 采样口径 + 跳过闸提示，确认才真的开始", async () => {
  const user = userEvent.setup();
  vi.mocked(startCommandCatalog).mockResolvedValue({
    status: "started",
    job: job({ status: "queued" }),
  });
  mount();

  const start = await screen.findByRole("button", { name: "识别命令目录" });
  await user.click(start);

  await waitFor(() => expect(confirmed).toHaveLength(1));
  // 预告弹出的那一刻还没有花任何模型的钱 —— 确认之前绝不发起。
  expect(startCommandCatalog).not.toHaveBeenCalled();
  const [request] = confirmed;
  expect(request.title).toBe("识别命令目录");
  // 这份夹具是 `sampled: true`：段数是下界（写「至少约」），而调用数只对已读的
  // 那 8 段成立 —— 其余段落后端刻意不报价。
  expect(request.body).toContain("全文至少约 12 段");
  expect(request.body).toContain("按开头 8 段估算需约 18 次模型调用");
  expect(request.body).toContain("其余段落视内容而定");
  expect(request.body).toContain("其中纯叙述部分不消耗调用。");
  expect(request.body).not.toContain("实际可能更多");
  expect(request.sections).toEqual([]);
  expect(request.confirmLabel).toBe("开始识别");

  await act(async () => { request.onConfirm(); });
  expect(startCommandCatalog).toHaveBeenCalledWith("nb-1", "src-1");
});

test("确认弹窗活得比卡片长：关掉来源详情后再点「开始识别」不发起无人看管的付费调用", async () => {
  const user = userEvent.setup();
  vi.mocked(startCommandCatalog).mockResolvedValue({ status: "started", job: job() });
  const view = mount();

  await user.click(await screen.findByRole("button", { name: "识别命令目录" }));
  await waitFor(() => expect(confirmed).toHaveLength(1));

  // 用户在预告弹出后关掉了来源详情 —— 卡片卸载，弹窗还挂在工作区上。
  view.unmount();
  await act(async () => { confirmed[0].onConfirm(); });

  expect(startCommandCatalog).not.toHaveBeenCalled();
});

// StrictMode 的 dev 双调用会在初次挂载时 mount → unmount(跑 cleanup) → mount 一遍
// （组件本身并未真的从树上移除）。aliveRef 的 cleanup 若直接把 ref 置 false、
// remount 时不重新置 true，这个 ref 会永久停在 false —— 组件明明还挂着，
// 「开始识别」却从此静默不发起任何请求（对齐 knowhow-import.tsx 的 mountedRef
// 同款修法与同款注释）。这里不 mock React,直接用真实 StrictMode 包裹渲染树来
// 触发双调用,而不是手工模拟 unmount/remount 时序 —— 后者测的是我们对 StrictMode
// 行为的想象,不是 StrictMode 真实做的事。
test("StrictMode 的双调用不会让「开始识别」永久哑火", async () => {
  const user = userEvent.setup();
  vi.mocked(startCommandCatalog).mockResolvedValue({ status: "started", job: job() });
  render(
    <StrictMode>
      <Harness />
    </StrictMode>,
  );

  await user.click(await screen.findByRole("button", { name: "识别命令目录" }));
  await waitFor(() => expect(confirmed).toHaveLength(1));
  await act(async () => { confirmed[0].onConfirm(); });

  expect(startCommandCatalog).toHaveBeenCalledWith("nb-1", "src-1");
});

// P1 竞态修复:确认弹窗的 onConfirm 回调是 requestRun() 那次渲染闭包捕获的
// beginRun,绑死在预告弹出那一刻的来源。组件实例复用(同一处 JSX 位置换了
// sourceId props,不是卸载重挂载)时 aliveRef 仍为 true,scoped() 又只拦响应
// 回填拦不住请求本身——不加这道前置校验,点确认会对旧来源真的发起一次付费识别,
// 而界面此刻显示的是新来源。
test("确认弹窗打开后切来源(组件实例复用)：确认回调静默放弃，不对旧来源发起付费识别", async () => {
  const user = userEvent.setup();
  vi.mocked(startCommandCatalog).mockResolvedValue({ status: "started", job: job() });
  const view = mount({ sourceId: "src-1" });

  await user.click(await screen.findByRole("button", { name: "识别命令目录" }));
  await waitFor(() => expect(confirmed).toHaveLength(1));

  // 用户在预告弹出后、点确认前切到了另一个来源。
  view.rerender(<Harness sourceId="src-2" />);
  await act(async () => { confirmed[0].onConfirm(); });

  expect(startCommandCatalog).not.toHaveBeenCalled();
});

// R3 P2 修复:挂载时的首拉(「打开来源就取一次最近任务」那个 effect)可能在用户
// 确认、startCommandCatalog 真的把新任务建起来之后才回来。不加请求代际,这份迟到
// 的响应会用无条件 setJob(latest) 把刚建好的新 job 覆盖成旧值/null —— 轮询与取消
// 入口跟着消失,而识别本身仍在后台跑、继续向模型付费。
test("挂载时的首拉响应若拖到启动之后才到达，不覆盖新启动的 job（请求代际守卫）", async () => {
  const user = userEvent.setup();
  let resolveInitialFetch!: (value: { job: CommandCatalogJob | null }) => void;
  vi.mocked(fetchCommandCatalogJob).mockReturnValue(
    new Promise((resolve) => { resolveInitialFetch = resolve; }),
  );
  vi.mocked(startCommandCatalog).mockResolvedValue({
    status: "started",
    job: job({ status: "queued" }),
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "识别命令目录" }));
  await waitFor(() => expect(confirmed).toHaveLength(1));
  await act(async () => { confirmed[0].onConfirm(); });

  // 启动已经把新 job 落进 state —— 取消入口应该已经在了。
  expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();

  // 姗姗来迟的挂载期首拉响应现在才回来，它带的是「还没识别过」的旧快照。
  await act(async () => { resolveInitialFetch({ job: null }); });

  expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "识别命令目录" })).not.toBeInTheDocument();
});

// R4 P2 修复:代际计数器是跨来源共用的一个 ref,而 A 的启动 POST 可能在用户切到
// 来源 B 之后才回来。旧版在 scoped() 之前就把它 +1,于是 A 那次「已经被 scoped()
// 丢掉、一个 setState 都没做」的响应,反而作废了 B 正在飞的首拉 —— B 明明有个在跑
// 的任务,界面却显示成「还没识别过」,取消入口一并消失。
test("换来源后 A 的启动响应迟到，不作废 B 正在飞的首拉（代际按来源隔离）", async () => {
  const user = userEvent.setup();
  const jobOfB = job({ id: "job-b", source_id: "src-2", status: "running" });
  let resolveFetchOfB!: (value: { job: CommandCatalogJob | null }) => void;
  const fetchOfB = new Promise<{ job: CommandCatalogJob | null }>((resolve) => {
    resolveFetchOfB = resolve;
  });
  let callsForB = 0;
  vi.mocked(fetchCommandCatalogJob).mockImplementation((_notebookId, sourceId) => {
    if (sourceId !== "src-2") return Promise.resolve({ job: null });
    callsForB += 1;
    // 只有 B 的那次首拉是「在飞」的;之后的轮询照常拿到快照,免得测试尾巴上挂着
    // 一个永远不 resolve 的请求。
    return callsForB === 1 ? fetchOfB : Promise.resolve({ job: jobOfB });
  });
  let resolveStartOfA!: (value: { status: string; job: CommandCatalogJob }) => void;
  vi.mocked(startCommandCatalog).mockReturnValue(
    new Promise((resolve) => { resolveStartOfA = resolve; }),
  );
  const view = mount({ sourceId: "src-1" });

  await user.click(await screen.findByRole("button", { name: "识别命令目录" }));
  await waitFor(() => expect(confirmed).toHaveLength(1));
  // A 的 POST 已经发出(此刻还在 src-1,前置 scope 校验放行),但还没回来。
  await act(async () => { confirmed[0].onConfirm(); });
  expect(startCommandCatalog).toHaveBeenCalledWith("nb-1", "src-1");

  // 用户切到来源 B —— B 的首拉发出去了,同样还没回来。
  view.rerender(<Harness sourceId="src-2" />);
  await act(async () => {});

  // A 的启动响应现在才回来。它已经不属于当前来源,scoped() 会丢掉它的 setState;
  // 它也不该顺手把 B 的首拉判成过期。
  await act(async () => {
    resolveStartOfA({ status: "started", job: job({ id: "job-a" }) });
  });

  // B 的首拉这才回来,带着 B 真正在跑的那个任务。
  await act(async () => { resolveFetchOfB({ job: jobOfB }); });

  expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "识别命令目录" })).not.toBeInTheDocument();
});

test("v2 不做形状检测，入口从不因为文档形状被禁用；未采样时不提示采样口径", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogPreview).mockResolvedValue({
    ...preview,
    sampled: false,
    skipped_windows_in_prefix: 0,
  });
  mount();

  const start = await screen.findByRole("button", { name: "识别命令目录" });
  expect(start).toBeEnabled();
  await user.click(start);

  await waitFor(() => expect(confirmed).toHaveLength(1));
  expect(confirmed[0].body).toContain("预计约 18 次模型调用");
  expect(confirmed[0].body).not.toContain("实际可能更多");
  expect(confirmed[0].body).not.toContain("纯叙述部分");
  // 未采样时前缀就是全文，段数是后端真跑分段的精确值 —— 不写「至少约」。
  expect(confirmed[0].body).toContain("将通读全文（约 12 段）");
  expect(confirmed[0].body).not.toContain("至少约");
});

// 这个用例走假时钟(要驱动轮询),所以刻意只用 fireEvent + act:userEvent 自带延时、
// findBy*/waitFor 自带轮询,它们与假时钟互相等待会直接把用例挂死(实测超时)。
// act 里 await 一次即可把 fetch 的 microtask 冲干净。
test("确认后按钮立刻禁用并换成「识别中…」，落终态才解除", async () => {
  vi.useFakeTimers();
  vi.mocked(startCommandCatalog).mockResolvedValue({
    status: "started",
    job: job({ status: "running", progress: { sections_total: 12, sections_done: 3, entries: 5 } }),
  });
  mount();
  await act(async () => {});

  fireEvent.click(screen.getByRole("button", { name: "识别命令目录" }));
  await act(async () => {});
  expect(confirmed).toHaveLength(1);
  await act(async () => { confirmed[0].onConfirm(); });

  const busy = screen.getByRole("button", { name: "识别中…" });
  expect(busy).toBeDisabled();
  expect(screen.getByText("已处理 3/12 段 · 已识别 5 条")).toBeInTheDocument();
  // 取消也是长任务按钮:点下去到 worker 真的停之间不能再点。
  expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();

  // 轮询拿到终态 —— 这是解除忙碌位的**唯一**证据(不是「POST 返回了」)。
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 12, sections_done: 12, entries: 20, rejected: 3, uncovered: 0 },
    }),
  });
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });

  expect(screen.getByRole("button", { name: "查看识别结果" })).toBeEnabled();
  expect(screen.getByText("已识别 20 条命令 · 3 条未通过")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
});

// R3 P2 修复:轮询窗口一关不再兜底放开按钮——那种猜测曾经把取消按钮(只在忙碌态
// 渲染)一起藏起来，而长时间跑着烧钱的任务恰恰在这时候最需要能取消。现在按钮的
// 可点性与「运行中」展示只认最后一次已知的真实 job 状态，pollExpired 只停自动
// 刷新、换出一条提示 + 手动刷新入口。
test("轮询窗口过期后不再兜底放开按钮：取消入口与「识别中」展示照旧跟随真实 job 状态", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "running", progress: { sections_total: 8, sections_done: 2, entries: 1 } }),
  });
  mount();
  await act(async () => {});
  expect(screen.getByRole("button", { name: "识别中…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();

  await act(async () => { await vi.advanceTimersByTimeAsync(31 * 60 * 1000); });

  // 主按钮与取消按钮都还照实反映「仍在跑」——不再假装没在跑。
  expect(screen.getByRole("button", { name: "识别中…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  // 已经拿到的进度不擦掉 —— 只是不再刷新。
  expect(screen.getByText("已处理 2/8 段 · 已识别 1 条")).toBeInTheDocument();
  // 不再兜底猜测，而是老实说清「自动刷新停了」并给一颗手动刷新入口。
  expect(screen.getByText(/已停止自动刷新/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled();
  // 建议修 #5:置 pollExpired 的同时必须把 pollUntil 一并归零，否则轮询用的
  // setInterval 不会被清掉，会在窗口过期后继续每 4s 空转一次。窗口过期后不该再
  // 有任何常驻定时器。
  expect(vi.getTimerCount()).toBe(0);
});

test("轮询过期后手动点「刷新」拿到终态：取消入口消失、按钮恢复常态", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "running", progress: { sections_total: 8, sections_done: 2, entries: 1 } }),
  });
  mount();
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(31 * 60 * 1000); });
  expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled();

  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 8, sections_done: 8, entries: 5, rejected: 0 },
    }),
  });
  // 假时钟下不用 userEvent(它自带延时，会跟假时钟互相等待挂死)，用 fireEvent。
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await act(async () => {});

  expect(screen.getByRole("button", { name: "查看识别结果" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
  expect(screen.queryByText(/已停止自动刷新/)).not.toBeInTheDocument();
});

// R11 P2 修复:轮询窗口过期后点了取消——worker 还没到下一个分片边界,cancel 响应
// 里 job 仍是 running,cancelling=true 本该由轮询接手落终态,但窗口已经不再自动
// 刷新。用户手动点「刷新」拿到 worker 真落的终态时,refreshNow 必须像轮询路径/
// 立即取消路径一样清掉 cancelling——否则这面标志会一直卡着,直到同一来源上重新
// 发起识别,新任务的取消按钮会被这面陈旧标志冻结成禁用的「取消中…」。
test("轮询过期→取消未即时落终态→手动刷新拿到终态→同来源再识别：取消按钮不再卡「取消中…」", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "running", progress: { sections_total: 8, sections_done: 2, entries: 1 } }),
  });
  mount();
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(31 * 60 * 1000); });
  expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled();

  // 点取消:worker 还没到下一个分片边界,cancel 响应里 job 仍是 running。
  let settleCancel!: (value: { status: string; job: CommandCatalogJob | null }) => void;
  vi.mocked(cancelCommandCatalog).mockReturnValue(new Promise((resolve) => { settleCancel = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await act(async () => {});
  await act(async () => {
    settleCancel({
      status: "cancelling",
      job: job({ status: "running", progress: { sections_total: 8, sections_done: 2, entries: 1 } }),
    });
  });
  expect(screen.getByRole("button", { name: "取消中…" })).toBeDisabled();

  // worker 真的落了终态(取消成功),但轮询窗口已经过期，没有自动轮询去发现它——
  // 用户手动点「刷新」。
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "cancelled", failure_reason: "已取消。" }),
  });
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await act(async () => {});
  expect(screen.getByText("已取消，已识别的结果仍可查看")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "取消中…" })).not.toBeInTheDocument();

  // 同来源重新发起识别:成本预告确认后建起一个全新的 job。
  fireEvent.click(screen.getByRole("button", { name: "识别命令目录" }));
  await act(async () => {});
  expect(confirmed).toHaveLength(1);
  vi.mocked(startCommandCatalog).mockResolvedValue({
    status: "started",
    job: job({ id: "job-2", status: "running", progress: { sections_total: 5, sections_done: 0, entries: 0 } }),
  });
  await act(async () => { confirmed[0].onConfirm(); });

  // 新任务的取消按钮必须是干净的「取消」，不能被上一轮那面没清干净的 cancelling
  // 标志卡死成禁用的「取消中…」。
  const newCancel = screen.getByRole("button", { name: "取消" });
  expect(newCancel).toBeEnabled();
  expect(screen.queryByRole("button", { name: "取消中…" })).not.toBeInTheDocument();
});

test("重开来源时后台仍在跑的任务会重新把按钮锁住（不是显示成「还没识别过」）", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "running", progress: { sections_total: 8, sections_done: 2, entries: 1 } }),
  });
  mount();

  const busy = await screen.findByRole("button", { name: "识别中…" });
  expect(busy).toBeDisabled();
  expect(screen.getByText("已处理 2/8 段 · 已识别 1 条")).toBeInTheDocument();
});

test("重复发起的 409 只带文案不带任务，前端自己把在跑的那个捡回来", async () => {
  const user = userEvent.setup();
  vi.mocked(startCommandCatalog).mockRejectedValue(
    humanizedError("该来源已有命令目录任务正在运行，请等待或先取消。", 409),
  );
  vi.mocked(fetchCommandCatalogJob)
    .mockResolvedValueOnce({ job: null })
    .mockResolvedValue({
      job: job({ id: "job-running", status: "running", progress: { sections_total: 9, sections_done: 4, entries: 6 } }),
    });
  mount();

  await user.click(await screen.findByRole("button", { name: "识别命令目录" }));
  await waitFor(() => expect(confirmed).toHaveLength(1));
  await act(async () => { confirmed[0].onConfirm(); });

  // 后端文案原样上屏(它带 X-User-Message，出处可信)，同时界面接上真实进度。
  expect(await screen.findByText("该来源已有命令目录任务正在运行，请等待或先取消。")).toBeInTheDocument();
  expect(await screen.findByText("已处理 4/9 段 · 已识别 6 条")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "识别中…" })).toBeDisabled();
});

test("失败原样展示后端文案；零产出指向未通过条目", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "failed", failure_reason: "模型服务未配置或不可用。" }),
  });
  const view = mount();
  expect(await screen.findByText("模型服务未配置或不可用。")).toBeInTheDocument();
  view.unmount();

  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 6, sections_done: 6, entries: 0, rejected: 7 } }),
  });
  mount();
  expect(
    await screen.findByText("未识别出命令，有 7 条未通过校验，可在结果里查看原因"),
  ).toBeInTheDocument();
});

test("取消点下去立刻禁用并换成「取消中…」", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "running", progress: { sections_total: 8, sections_done: 2 } }),
  });
  let settle!: (value: { status: string; job: CommandCatalogJob | null }) => void;
  vi.mocked(cancelCommandCatalog).mockReturnValue(new Promise((resolve) => { settle = resolve; }));
  mount();

  const cancel = await screen.findByRole("button", { name: "取消" });
  await user.click(cancel);
  const busyCancel = await screen.findByRole("button", { name: "取消中…" });
  expect(busyCancel).toBeDisabled();

  await act(async () => {
    settle({ status: "cancelled", job: job({ status: "cancelled", failure_reason: "已取消。" }) });
  });
  // 取消不复述后端那句同义文案(那是带句号的整句)，卡片自己写一句更干净的。
  expect(await screen.findByText("已取消，已识别的结果仍可查看")).toBeInTheDocument();
});

test("只读成员看得到结果，但拿不到发起/确认能力", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 3 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  const user = userEvent.setup();
  mount({ canEdit: false });

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  expect(within(dialog).getByText("set_db")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "确认全部待审阅" })).not.toBeInTheDocument();
  expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重新识别" })).not.toBeInTheDocument();
});

test("只读成员遇到从没识别过的来源时整块不渲染，不摆一张无从行动的空卡片", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({ job: null });
  mount({ canEdit: false });
  await act(async () => {});
  expect(screen.queryByText("命令目录")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "识别命令目录" })).not.toBeInTheDocument();
});

// R5 P2:上一轮成功但还有候选没审阅完时，「重新识别」不能直接把它们顶掉——
// `.../job` 只返回最近一次任务，旧候选会永远够不着（后端同一条判据的 409 是
// 第二道防线，见 test_command_catalog_job.py）。
test("重新识别被待审阅候选拦住：禁用 + 说明文案 + 点不动（不发起新的成本预告）", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 4, sections_done: 4, entries: 3, pending_candidates: 3 },
    }),
  });
  mount();

  const restart = await screen.findByRole("button", { name: "重新识别" });
  expect(restart).toBeDisabled();
  expect(
    screen.getByText("仍有 3 条待审阅候选，请先确认或跳过后再重新识别。"),
  ).toBeInTheDocument();

  // disabled 的按钮真实 DOM 不派发 click——顺手证明"点不动"，不只是属性对了
  // (同文件其余长任务按钮用例的既有风格)。
  await user.click(restart);
  expect(fetchCommandCatalogPreview).not.toHaveBeenCalled();
  // 「查看识别结果」这颗主按钮完全不受影响——被拦住的只是「重新识别」。
  expect(screen.getByRole("button", { name: "查看识别结果" })).toBeEnabled();
});

// R6 P1:codex 评审推翻了 R5「失败/取消不拦」的结论——`INTERRUPTED_MESSAGE`
// 那句「可重新发起识别」的承诺兑现不了（`.../job` 只认最近一次任务，立刻重新
// 发起同样会把候选永久孤儿化）。失败/取消这两个状态走的是主按钮「识别命令
// 目录」本身（不是「重新识别」——那颗按钮只在 succeeded 时渲染），所以这里
// 断言的是 new-pill 分支被拦住，而不是 sort-button 分支。
test("失败后主按钮「识别命令目录」被待审阅候选拦住：禁用 + 说明文案 + 点不动", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "failed",
      failure_reason: "服务中断导致命令目录识别未完成；已生成的候选已保留，请先在审阅面板确认或跳过，再重新发起识别。",
      progress: { sections_total: 4, sections_done: 2, entries: 1, pending_candidates: 1 },
    }),
  });
  mount();

  const primary = await screen.findByRole("button", { name: "识别命令目录" });
  expect(primary).toBeDisabled();
  expect(
    screen.getByText("仍有 1 条待审阅候选，请先确认或跳过后再重新识别。"),
  ).toBeInTheDocument();
  // 没有「重新识别」这颗按钮——它只在 succeeded 时渲染，被拦住的是主按钮本身。
  expect(screen.queryByRole("button", { name: "重新识别" })).not.toBeInTheDocument();

  await user.click(primary);
  expect(fetchCommandCatalogPreview).not.toHaveBeenCalled();
  // 「查看识别结果」不受影响——候选还在，只是发起识别的入口被拦。
  expect(screen.getByRole("button", { name: "查看识别结果" })).toBeEnabled();
});

test("取消后主按钮同样被待审阅候选拦住", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "cancelled",
      progress: { sections_total: 4, sections_done: 2, entries: 1, pending_candidates: 2 },
    }),
  });
  mount();

  const primary = await screen.findByRole("button", { name: "识别命令目录" });
  expect(primary).toBeDisabled();
  expect(
    screen.getByText("仍有 2 条待审阅候选，请先确认或跳过后再重新识别。"),
  ).toBeInTheDocument();
});

test("失败但候选都已审完（0 条待审）时主按钮照常可发起新一轮识别", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "failed",
      failure_reason: "识别没能完成。",
      progress: { sections_total: 4, sections_done: 2, entries: 1, pending_candidates: 0 },
    }),
  });
  mount();

  const primary = await screen.findByRole("button", { name: "识别命令目录" });
  expect(primary).toBeEnabled();
  expect(screen.queryByText(/仍有 \d+ 条待审阅候选/)).not.toBeInTheDocument();
});

test("候选已审完（pending_candidates 为 0）时「重新识别」照常可发起新一轮识别", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 4, sections_done: 4, entries: 3, pending_candidates: 0 },
    }),
  });
  mount();

  expect(
    screen.queryByText(/仍有 \d+ 条待审阅候选/),
  ).not.toBeInTheDocument();
  const restart = await screen.findByRole("button", { name: "重新识别" });
  expect(restart).toBeEnabled();

  await user.click(restart);
  await waitFor(() => expect(fetchCommandCatalogPreview).toHaveBeenCalledWith("nb-1", "src-1"));
});

// R5 P2:`reject_info["overflow"]`/`["desc_overflow"]` 此前落了库却在响应模型被
// 丢弃——审阅面板因此没有任何办法知道「拦截记录被截了」这件事本身。
test("展开候选能看到拦截账目的溢出小字（另有 N 条未显示 / M 段说明被截）", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate({ rejections_overflow: 2, desc_overflow: 5 })],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");
  // 收起状态下不渲染——展开面板之外的行高必须保持可扫(同文件既有惯例)。
  expect(
    within(dialog).queryByText("另有 2 条拦截记录未显示；有 5 段参数描述因超长被截"),
  ).not.toBeInTheDocument();

  await user.click(within(dialog).getByRole("button", { name: "展开" }));
  expect(
    within(dialog).getByText("另有 2 条拦截记录未显示；有 5 段参数描述因超长被截"),
  ).toBeInTheDocument();
});

test("溢出计数为 0 时不渲染多余的小字", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate({ rejections_overflow: 0, desc_overflow: 0 })],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await user.click(within(dialog).getByRole("button", { name: "展开" }));
  expect(within(dialog).queryByText(/条拦截记录未显示/)).not.toBeInTheDocument();
  expect(within(dialog).queryByText(/段参数描述因超长被截/)).not.toBeInTheDocument();
});

test("审阅两页签：候选可展开看参数表与原文；未通过页签给中文原因", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 1, rejected: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockImplementation(async (_nb, _src, opts) => (
    opts?.state === "rejected"
      ? {
        items: [candidate({
          id: "r1",
          command_name: "",
          state: "rejected",
          syntax: "",
          args: [],
          rejections: [{
            field: "arg",
            value: "density",
            reason: "dash_stripped",
            window: "[-density target_density]",
          }],
        })],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 1, applied: 0, dismissed: 0 },
      }
      : {
        items: [candidate({ suspect_related: true })],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 1, applied: 0, dismissed: 0 },
      }
  ));
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  expect(await within(dialog).findByText("set_db")).toBeInTheDocument();
  expect(within(dialog).getByText("第 3 章 > set_db")).toBeInTheDocument();
  expect(within(dialog).getByText("参数 1")).toBeInTheDocument();
  expect(within(dialog).getByText("疑似仅被提及")).toBeInTheDocument();
  expect(within(dialog).getByText("set_db <name> <value>")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("button", { name: "展开" }));
  expect(within(dialog).getByText("设置数据库属性")).toBeInTheDocument();
  expect(within(dialog).getByRole("cell", { name: "-name" })).toBeInTheDocument();
  expect(within(dialog).getByText("set_db 用于设置属性…")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("tab", { name: "未通过（1）" }));
  expect(
    await within(dialog).findByText("参数「density」：少了前面的短横（原文写作 -xxx）"),
  ).toBeInTheDocument();
});

// R2 P2:示例此前只进了库、没有任何 UI 可见面——一条候选最有说服力的部分(怎么调用
// 这条命令)在审阅时看不到。它同时是**刻意不接地**的模型自撰内容,所以必须连同那句
// 说明一起出现,否则用户会把它当成从原文抄来的。
test("展开候选能看到示例与「未经原文校验」说明；超过三条折在「显示全部」后面", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate({
      examples: [
        "set_db -name a",
        "set_db -name b",
        "set_db -name c",
        "set_db -name d",
      ],
    })],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  // 收起状态下不渲染示例——展开面板之外的行高必须保持可扫。
  expect(within(dialog).queryByText("set_db -name a")).not.toBeInTheDocument();

  await user.click(within(dialog).getByRole("button", { name: "展开" }));
  expect(within(dialog).getByText("示例")).toBeInTheDocument();
  expect(within(dialog).getByText("示例为模型生成，未经原文校验")).toBeInTheDocument();
  expect(within(dialog).getByText("set_db -name a")).toBeInTheDocument();
  expect(within(dialog).getByText("set_db -name c")).toBeInTheDocument();
  expect(within(dialog).queryByText("set_db -name d")).not.toBeInTheDocument();

  await user.click(within(dialog).getByRole("button", { name: "显示全部 4 条示例" }));
  expect(within(dialog).getByText("set_db -name d")).toBeInTheDocument();
  expect(
    within(dialog).queryByRole("button", { name: /显示全部/ }),
  ).not.toBeInTheDocument();
});

// 后端在**通过**的候选上也记逐字段落空(R2:这一批问了却没回来的参数)。上方那份
// 列表只在「未通过」页签渲染,所以不在展开面板里补一份,这条账目在界面上根本没有
// 落点——而它恰恰是「这行能用,但少了什么」的唯一线索。
test("待审阅候选展开后能看到逐字段的落空原因，不必切到未通过页签", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate({
      rejections: [
        { field: "arg", value: "-pad_right", reason: "arg_not_returned", window: "" },
      ],
    })],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  // 收起时不显示:待审阅页签的行首先要能扫。
  expect(
    within(dialog).queryByText("参数「-pad_right」：原文里有，但模型没有提取"),
  ).not.toBeInTheDocument();

  await user.click(within(dialog).getByRole("button", { name: "展开" }));
  expect(within(dialog).getByText("未采纳的内容")).toBeInTheDocument();
  expect(
    within(dialog).getByText("参数「-pad_right」：原文里有，但模型没有提取"),
  ).toBeInTheDocument();
});

test("没有示例的候选展开后不渲染空的示例区块", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate({ examples: [] })],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await user.click(within(dialog).getByRole("button", { name: "展开" }));
  // 参数表照常在,只有示例整块不渲染——一句「未经原文校验」的免责小字挂在没有
  // 内容的区块上,读起来像是有东西被藏起来了。
  expect(within(dialog).getByRole("cell", { name: "-name" })).toBeInTheDocument();
  expect(within(dialog).queryByText("示例")).not.toBeInTheDocument();
  expect(
    within(dialog).queryByText("示例为模型生成，未经原文校验"),
  ).not.toBeInTheDocument();
});

// R1 P2:apply 产生冲突后落 dismissed 的候选此前没有任何 UI 可见面。三页签各自
// 独立取数(复用既有 epoch 语义),互不串台——切到「已跳过」必须看到该候选与
// 中文原因,而不是「候选」或「未通过」页签的内容。
test("已跳过页签能列出冲突候选与原因；三页签互不串台", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockImplementation(async (_nb, _src, opts) => {
    if (opts?.state === "dismissed") {
      return {
        items: [candidate({
          id: "d1",
          command_name: "report_timing",
          state: "dismissed",
          dismiss_reason: "conflict_existing_row",
        })],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 0, applied: 1, dismissed: 1 },
      };
    }
    if (opts?.state === "rejected") {
      return { items: [], next_cursor: 0, has_more: false, counts: { candidate: 1, rejected: 0, applied: 1, dismissed: 1 } };
    }
    return {
      items: [candidate()],
      next_cursor: 1,
      has_more: false,
      counts: { candidate: 1, rejected: 0, applied: 1, dismissed: 1 },
    };
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  expect(await within(dialog).findByText("set_db")).toBeInTheDocument();
  expect(within(dialog).getByText("已跳过 1")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("tab", { name: "已跳过（1）" }));
  expect(await within(dialog).findByText("report_timing")).toBeInTheDocument();
  expect(within(dialog).getByText("已存在同名行")).toBeInTheDocument();
  // 已跳过页签是只读的——没有勾选框,复用「候选」页签独有的交互。
  expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  expect(within(dialog).queryByText("set_db")).not.toBeInTheDocument();

  await user.click(within(dialog).getByRole("tab", { name: "待审阅（1）" }));
  expect(await within(dialog).findByText("set_db")).toBeInTheDocument();
  expect(within(dialog).queryByText("report_timing")).not.toBeInTheDocument();
});

// R7:已跳过页签的两个写者(apply 冲突自动跳过 / dismiss 端点人工跳过)必须显示
// 两条不同的中文原因——审阅者要能分清「这条我自己不要」和「这条已经在表里了」。
test("已跳过页签区分「已存在同名行」与「人工跳过」两种原因", async () => {
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 2 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockImplementation(async (_nb, _src, opts) => (
    opts?.state === "dismissed"
      ? {
        items: [
          candidate({
            id: "d1", command_name: "report_timing", state: "dismissed",
            dismiss_reason: "conflict_existing_row",
          }),
          candidate({
            id: "d2", command_name: "reset_path", state: "dismissed",
            dismiss_reason: "user_dismissed",
          }),
        ],
        next_cursor: 2,
        has_more: false,
        counts: { candidate: 0, rejected: 0, applied: 1, dismissed: 2 },
      }
      : { items: [], next_cursor: 0, has_more: false, counts: { candidate: 0, rejected: 0, applied: 1, dismissed: 2 } }
  ));
  const user = userEvent.setup();
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await user.click(within(dialog).getByRole("tab", { name: "已跳过（2）" }));

  expect(await within(dialog).findByText("已存在同名行")).toBeInTheDocument();
  expect(within(dialog).getByText("人工跳过")).toBeInTheDocument();
});

test("确认所选：报出新增/冲突/剩余三件事，剩余可以接着确认", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 2 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate(), candidate({ id: "c2", position: 2, command_name: "report_timing" })],
    next_cursor: 2,
    has_more: false,
    counts: { candidate: 2, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(applyCommandCatalog).mockResolvedValue({
    table_id: "t-9",
    table_title: "命令目录：工具手册",
    created: true,
    applied: ["c1"],
    rows_added: 1,
    conflicts: [{ candidate_id: "c2", command_name: "report_timing" }],
    pending_remaining: 4,
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  expect(within(dialog).getByText(/已选 1 条/)).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "确认所选" }));

  await waitFor(() => expect(applyCommandCatalog).toHaveBeenCalledWith(
    "nb-1", "src-1", { candidate_ids: ["c1"] }, "job-1",
  ));
  expect(await within(dialog).findByText("已新建《命令目录：工具手册》并写入 1 条命令")).toBeInTheDocument();
  expect(
    within(dialog).getByText("1 条已存在同名行，未改动，已跳过，不再重复确认：report_timing"),
  ).toBeInTheDocument();
  // 一次最多确认一页 —— 剩余必须显式提示，否则用户以为做完了。
  expect(within(dialog).getByText("还有 4 条待审阅，可以继续确认。")).toBeInTheDocument();
  expect(onToast).toHaveBeenCalledWith("已新建《命令目录：工具手册》并写入 1 条命令");

  await user.click(within(dialog).getByRole("button", { name: "去看这张表" }));
  expect(onOpenTable).toHaveBeenCalledWith("t-9");
});

test("确认响应迟到且来源弹窗 lease 已失效时，不向替代工作区 toast 或触发入口刷新", async () => {
  const user = userEvent.setup();
  let leaseCurrent = true;
  let settleApply!: (value: CommandCatalogApplyResult) => void;
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(applyCommandCatalog).mockReturnValue(new Promise((resolve) => { settleApply = resolve; }));
  mount({ reviewIsCurrent: () => leaseCurrent });

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");
  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  await user.click(within(dialog).getByRole("button", { name: "确认所选" }));
  await waitFor(() => expect(applyCommandCatalog).toHaveBeenCalledTimes(1));
  const candidateReads = vi.mocked(fetchCommandCatalogCandidates).mock.calls.length;
  const jobReads = vi.mocked(fetchCommandCatalogJob).mock.calls.length;

  await act(async () => {
    leaseCurrent = false;
    settleApply({
      table_id: "t-late",
      table_title: "旧来源目录",
      created: true,
      applied: ["c1"],
      rows_added: 1,
      conflicts: [],
      pending_remaining: 0,
    });
  });

  expect(onToast).not.toHaveBeenCalled();
  expect(fetchCommandCatalogCandidates).toHaveBeenCalledTimes(candidateReads);
  expect(fetchCommandCatalogJob).toHaveBeenCalledTimes(jobReads);
});

// R7:显式跳过——「重新识别」被待审阅候选拦住时唯一的放弃出口(apply 只在候选
// 与目标表冲突时才自动 dismiss)。
test("跳过所选：报出跳过了几条与还剩几条待审阅，跳过后从头重拉列表", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 2 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate(), candidate({ id: "c2", position: 2, command_name: "report_timing" })],
    next_cursor: 2,
    has_more: false,
    counts: { candidate: 2, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(dismissCommandCatalog).mockResolvedValue({
    dismissed: ["c1"],
    pending_remaining: 1,
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  await user.click(within(dialog).getByRole("button", { name: "跳过所选" }));

  await waitFor(() => expect(dismissCommandCatalog).toHaveBeenCalledWith(
    "nb-1", "src-1", { candidate_ids: ["c1"] }, "job-1",
  ));
  // apply 绝没被叫到——跳过与确认是两条独立的写路径。
  expect(applyCommandCatalog).not.toHaveBeenCalled();
  expect(await within(dialog).findByText("已跳过 1 条候选")).toBeInTheDocument();
  expect(
    within(dialog).getByText("还有 1 条待审阅，可以继续确认或跳过。"),
  ).toBeInTheDocument();
  expect(onToast).toHaveBeenCalledWith("已跳过 1 条候选");
  // 跳过从不写表——不该出现「去看这张表」入口。
  expect(within(dialog).queryByRole("button", { name: "去看这张表" })).not.toBeInTheDocument();
});

test("跳过请求在审阅弹窗关闭后落地时，不向新界面 toast 或触发入口刷新", async () => {
  const user = userEvent.setup();
  let settleDismiss!: (value: CommandCatalogDismissResult) => void;
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 1, sections_done: 1, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(dismissCommandCatalog).mockReturnValue(new Promise((resolve) => { settleDismiss = resolve; }));
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");
  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  await user.click(within(dialog).getByRole("button", { name: "跳过所选" }));
  await waitFor(() => expect(dismissCommandCatalog).toHaveBeenCalledTimes(1));
  const candidateReads = vi.mocked(fetchCommandCatalogCandidates).mock.calls.length;
  const jobReads = vi.mocked(fetchCommandCatalogJob).mock.calls.length;
  await user.click(within(dialog).getByRole("button", { name: "关闭识别结果" }));

  await act(async () => {
    settleDismiss({ dismissed: ["c1"], pending_remaining: 0 });
  });

  expect(onToast).not.toHaveBeenCalled();
  expect(fetchCommandCatalogCandidates).toHaveBeenCalledTimes(candidateReads);
  expect(fetchCommandCatalogJob).toHaveBeenCalledTimes(jobReads);
});

test("跳过所选/跳过全部待审阅在飞时立刻禁用并换成「跳过中…」，且与确认互斥", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  let settleDismiss!: (value: CommandCatalogDismissResult) => void;
  vi.mocked(dismissCommandCatalog).mockReturnValue(new Promise((resolve) => { settleDismiss = resolve; }));
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  // 点击前捕获元素引用——「跳过所选」与「跳过全部待审阅」在飞时都会换成
  // 「跳过中…」,两颗按钮同名,点击后不能再靠 name 精确定位其中一颗。
  const dismissSelectedButton = within(dialog).getByRole("button", { name: "跳过所选" });
  await user.click(dismissSelectedButton);

  expect(dismissSelectedButton).toBeDisabled();
  expect(dismissSelectedButton).toHaveTextContent("跳过中…");
  // 确认所选/确认全部待审阅在跳过飞行期间也不可点——同一批候选不能被两个写
  // 路径同时处理。
  expect(within(dialog).getByRole("button", { name: "确认所选" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "确认全部待审阅" })).toBeDisabled();

  await act(async () => {
    settleDismiss({ dismissed: ["c1"], pending_remaining: 0 });
  });

  // 落地后重拉列表并清空选中集,「跳过所选」文案回到常态——它此刻仍会因
  // 「没有选中任何一条」而 disabled,那是选择态的常态,与「在飞」是两回事,
  // 所以这里只断言忙碌文案已经消失,不断言可点性。
  expect(dismissSelectedButton).toHaveTextContent("跳过所选");
  // 「确认全部待审阅」的可点性只看 pendingCount(不受选中集清空影响),
  // dismissing 落地后必须恢复可点——证明互斥只在飞行期间生效,不是永久锁死。
  expect(within(dialog).getByRole("button", { name: "确认全部待审阅" })).toBeEnabled();
});

test("跳过全部待审阅走 all_pending，跳过后拦截守卫解除（pending_remaining 归零）", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 2 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate(), candidate({ id: "c2", position: 2, command_name: "report_timing" })],
    next_cursor: 2,
    has_more: false,
    counts: { candidate: 2, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(dismissCommandCatalog).mockResolvedValue({
    dismissed: ["c1", "c2"],
    pending_remaining: 0,
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("button", { name: "跳过全部待审阅" }));

  await waitFor(() => expect(dismissCommandCatalog).toHaveBeenCalledWith(
    "nb-1", "src-1", { all_pending: true }, "job-1",
  ));
  expect(await within(dialog).findByText("已跳过 2 条候选")).toBeInTheDocument();
  // pending_remaining 归零——没有「还有 N 条待审阅」的提示残留。
  expect(within(dialog).queryByText(/还有 \d+ 条待审阅/)).not.toBeInTheDocument();
});

// 页签竞态(建议修 #4):runApply 落地时用它开始那一刻捕获的 tab 闭包重拉列表；
// 若这期间用户已经切到另一个页签,重拉会用旧页签的结果覆盖新页签正在看的内容
// ——串台。最简修法是 apply 在飞时整段禁用页签切换,这里验证禁用状态精确跟随
// `applying`,而不是验证具体的串台内容(串台本身是异步时序竞态,直接断言
// disabled 更稳定,且已经是覆盖住这条回退的充分条件——页签点不动,时序竞态
// 就无从发生)。
test("apply 在飞时页签禁用，落地后恢复——防止 runApply 用旧页签闭包重拉时把新页签的列表覆盖掉", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 1, rejected: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockImplementation(async (_nb, _src, opts) => (
    opts?.state === "rejected"
      ? {
        items: [candidate({ id: "r1", state: "rejected" })],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 1, applied: 0, dismissed: 0 },
      }
      : {
        items: [candidate()],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 1, applied: 0, dismissed: 0 },
      }
  ));
  let settleApply!: (value: CommandCatalogApplyResult) => void;
  vi.mocked(applyCommandCatalog).mockReturnValue(new Promise((resolve) => { settleApply = resolve; }));
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  await user.click(within(dialog).getByRole("button", { name: "确认所选" }));

  const candidateTab = within(dialog).getByRole("tab", { name: "待审阅（1）" });
  const rejectedTab = within(dialog).getByRole("tab", { name: "未通过（1）" });
  expect(candidateTab).toBeDisabled();
  expect(rejectedTab).toBeDisabled();
  // disabled 的按钮真实 DOM 不派发 click——顺手证明"切不动"，不只是属性对了。
  await user.click(rejectedTab);
  expect(within(dialog).getByRole("tab", { name: "待审阅（1）", selected: true })).toBeInTheDocument();

  await act(async () => {
    settleApply({
      table_id: "t-9", table_title: "命令目录：工具手册", created: true, applied: ["c1"], rows_added: 1,
      conflicts: [], pending_remaining: 0,
    });
  });

  expect(candidateTab).toBeEnabled();
  expect(rejectedTab).toBeEnabled();
});

// 建议修 #9:确认结果卡是上一次确认动作的摘要，与当前页签内容无关——切走页签后
// 它不该继续挂着，那看起来像是刚才那次确认发生在新页签里。
test("切页签时确认结果卡不残留——它是上一次确认动作的摘要，不是当前页签的内容", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 1, rejected: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockImplementation(async (_nb, _src, opts) => (
    opts?.state === "rejected"
      ? {
        items: [candidate({ id: "r1", state: "rejected" })],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 1, applied: 0, dismissed: 0 },
      }
      : {
        items: [candidate()],
        next_cursor: 1,
        has_more: false,
        counts: { candidate: 1, rejected: 1, applied: 0, dismissed: 0 },
      }
  ));
  vi.mocked(applyCommandCatalog).mockResolvedValue({
    table_id: "t-9", table_title: "命令目录：工具手册", created: true, applied: ["c1"], rows_added: 1,
    conflicts: [], pending_remaining: 0,
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("checkbox", { name: "选择 set_db" }));
  await user.click(within(dialog).getByRole("button", { name: "确认所选" }));
  expect(await within(dialog).findByText("已新建《命令目录：工具手册》并写入 1 条命令")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("tab", { name: "未通过（1）" }));
  expect(within(dialog).queryByText("已新建《命令目录：工具手册》并写入 1 条命令")).not.toBeInTheDocument();
});

test("确认全部待审阅走 all_pending，并在确认后从头重拉列表（旧游标已失效）", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 1 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(applyCommandCatalog).mockResolvedValue({
    table_id: "t-9", table_title: "命令目录：工具手册", created: false, applied: ["c1"], rows_added: 1,
    conflicts: [], pending_remaining: 0,
  });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");
  const before = vi.mocked(fetchCommandCatalogCandidates).mock.calls.length;

  await user.click(within(dialog).getByRole("button", { name: "确认全部待审阅" }));
  await waitFor(() => expect(applyCommandCatalog).toHaveBeenCalledWith(
    "nb-1", "src-1", { all_pending: true }, "job-1",
  ));
  await waitFor(() => {
    const calls = vi.mocked(fetchCommandCatalogCandidates).mock.calls;
    expect(calls.length).toBeGreaterThan(before);
    expect(calls.at(-1)?.[2]).toMatchObject({ cursor: 0, state: "candidate" });
  });
  expect(await within(dialog).findByText("已向《命令目录：工具手册》新增 1 条命令")).toBeInTheDocument();
});

test("加载更多用 keyset 游标接着取，不是页码", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({ status: "succeeded", progress: { sections_total: 4, sections_done: 4, entries: 2 } }),
  });
  vi.mocked(fetchCommandCatalogCandidates)
    .mockResolvedValueOnce({
      items: [candidate()],
      next_cursor: 7,
      has_more: true,
      counts: { candidate: 2, rejected: 0, applied: 0, dismissed: 0 },
    })
    .mockResolvedValue({
      items: [candidate({ id: "c2", position: 8, command_name: "report_timing" })],
      next_cursor: 8,
      has_more: false,
      counts: { candidate: 2, rejected: 0, applied: 0, dismissed: 0 },
    });
  mount();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  await user.click(within(dialog).getByRole("button", { name: "加载更多" }));
  expect(await within(dialog).findByText("report_timing")).toBeInTheDocument();
  expect(vi.mocked(fetchCommandCatalogCandidates).mock.calls.at(-1)?.[2]).toMatchObject({ cursor: 7 });
  // 取完了就不再给「加载更多」，避免点出一页空的。
  expect(within(dialog).queryByRole("button", { name: "加载更多" })).not.toBeInTheDocument();
});

// R8(codex PR #412 评审 P1):审阅弹窗提升到根层之后,它与入口卡片之间没有任何
// 共享状态,而卡片的 job 快照里带着「重新识别」的拦截判据 pending_candidates。
// 审阅者确认掉最后一条候选、关掉弹窗,卡片手里仍是那份带旧计数的快照——按钮会
// 一直禁用到用户重开来源详情为止。这条用例钉的就是那条回调线。
test("确认掉最后一条候选、关掉弹窗后，「重新识别」重新可点（父卡片重读了 job）", async () => {
  const user = userEvent.setup();
  // 打开时:1 条待审阅 → 「重新识别」被拦住。
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 4, sections_done: 4, entries: 1, pending_candidates: 1 },
    }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(applyCommandCatalog).mockResolvedValue({
    table_id: "t-9",
    table_title: "命令目录：工具手册",
    created: true,
    applied: ["c1"],
    rows_added: 1,
    conflicts: [],
    pending_remaining: 0,
  });
  mount();

  expect(await screen.findByRole("button", { name: "重新识别" })).toBeDisabled();
  expect(
    screen.getByText("仍有 1 条待审阅候选，请先确认或跳过后再重新识别。"),
  ).toBeInTheDocument();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  // 确认之后后端那份 job 的 pending 已经归零 —— 卡片必须重新去读，才看得到。
  // 换 mock 必须在弹窗已经拉过一页之后，否则弹窗打开时就是空的、点不到确认。
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 4, sections_done: 4, entries: 1, pending_candidates: 0 },
    }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [], next_cursor: 0, has_more: false,
    counts: { candidate: 0, rejected: 0, applied: 1, dismissed: 0 },
  });

  await user.click(within(dialog).getByRole("button", { name: "确认全部待审阅" }));
  await waitFor(() => expect(applyCommandCatalog).toHaveBeenCalled());

  await user.click(within(dialog).getByRole("button", { name: "关闭识别结果" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "命令目录识别结果" })).not.toBeInTheDocument(),
  );

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "重新识别" })).toBeEnabled(),
  );
  expect(screen.queryByText(/仍有 \d+ 条待审阅候选/)).not.toBeInTheDocument();
  // 而且真的可以发起 —— 不只是 disabled 属性掉了。
  await user.click(screen.getByRole("button", { name: "重新识别" }));
  await waitFor(() =>
    expect(fetchCommandCatalogPreview).toHaveBeenCalledWith("nb-1", "src-1"),
  );
});

test("跳过完最后一条候选同样让父卡片重读 job（跳过与确认走同一条回调）", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 2, sections_done: 2, entries: 1, pending_candidates: 1 },
    }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(dismissCommandCatalog).mockResolvedValue({
    dismissed: ["c1"],
    pending_remaining: 0,
  });
  mount();

  expect(await screen.findByRole("button", { name: "重新识别" })).toBeDisabled();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 2, sections_done: 2, entries: 1, pending_candidates: 0 },
    }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [], next_cursor: 0, has_more: false,
    counts: { candidate: 0, rejected: 0, applied: 0, dismissed: 1 },
  });

  await user.click(within(dialog).getByRole("button", { name: "跳过全部待审阅" }));
  await waitFor(() => expect(dismissCommandCatalog).toHaveBeenCalled());

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "重新识别" })).toBeEnabled(),
  );
});

// R8:失败路径也要通知父层。apply/dismiss 的 409 里有一种(来源已重新解析)后端
// 会顺手把剩下的候选整批置过期——服务端状态变了、拦截守卫已经解除,只是这次请求
// 没成功。只在成功时回调,那一种恰好就是卡片永远不知道的情况。
test("确认失败(来源已重新解析的 409)后父卡片仍重读 job，拦截守卫随之解除", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 2, sections_done: 2, entries: 1, pending_candidates: 1 },
    }),
  });
  vi.mocked(fetchCommandCatalogCandidates).mockResolvedValue({
    items: [candidate()],
    next_cursor: 1,
    has_more: false,
    counts: { candidate: 1, rejected: 0, applied: 0, dismissed: 0 },
  });
  vi.mocked(applyCommandCatalog).mockRejectedValue(
    humanizedError("来源已重新解析，本次识别结果已过期，请重新识别。"),
  );
  mount();

  expect(await screen.findByRole("button", { name: "重新识别" })).toBeDisabled();

  await user.click(await screen.findByRole("button", { name: "查看识别结果" }));
  const dialog = await screen.findByRole("dialog", { name: "命令目录识别结果" });
  await within(dialog).findByText("set_db");

  // 后端在拒绝这次确认的同一次调用里把候选整批作废了。
  vi.mocked(fetchCommandCatalogJob).mockResolvedValue({
    job: job({
      status: "succeeded",
      progress: { sections_total: 2, sections_done: 2, entries: 1, pending_candidates: 0 },
    }),
  });

  await user.click(within(dialog).getByRole("button", { name: "确认全部待审阅" }));

  // 后端文案原样上屏(它带 X-User-Message)。
  expect(
    await within(dialog).findByText("来源已重新解析，本次识别结果已过期，请重新识别。"),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "重新识别" })).toBeEnabled(),
  );
});
