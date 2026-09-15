import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchUserActivity: vi.fn(),
  fetchUserAskDetail: vi.fn(),
  fetchUserReportDetail: vi.fn(),
  fetchUserNotebookSource: vi.fn(),
  fetchUserNotebookSources: vi.fn(),
  fetchUserNotebooks: vi.fn(),
}));

vi.mock("../../app/dev/logs/activity/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchUserActivity: mocks.fetchUserActivity,
  fetchUserAskDetail: mocks.fetchUserAskDetail,
  fetchUserReportDetail: mocks.fetchUserReportDetail,
  fetchUserNotebookSource: mocks.fetchUserNotebookSource,
  fetchUserNotebookSources: mocks.fetchUserNotebookSources,
}));
vi.mock("../../app/admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));

import { ActivityView } from "../../app/dev/logs/activity/ActivityView";
import type {
  ActivityAsk,
  ActivityItem,
  ActivityReport,
  ActivitySource,
  ReportDetail,
} from "../../app/dev/logs/activity/types";

const NOW = new Date(2026, 7, 4, 12, 0);

const NOTEBOOKS = [
  {
    id: "nb-1",
    name: "笔记本一",
    status: "ready",
    sources: 2,
    conversations: 1,
    questions: 3,
    reports: 0,
    created_at: "2026-08-01T09:00:00",
    updated_at: "2026-08-04T09:00:00",
  },
  {
    id: "nb-2",
    name: "笔记本二",
    status: "ready",
    sources: 1,
    conversations: 0,
    questions: 1,
    reports: 1,
    created_at: "2026-08-02T09:00:00",
    updated_at: "2026-08-04T09:00:00",
  },
];

function ask(id: string, question: string, notebookId = "nb-1"): ActivityAsk {
  return {
    type: "ask",
    id,
    notebook_id: notebookId,
    created_at: "2026-08-04T10:30:00",
    asked_at: "2026-08-04T10:29:00",
    conversation_id: `conv-${id}`,
    question,
    mode: "reasoning",
    status: "done",
    answer_id: `ans-${id}`,
    error: "",
  };
}

function report(id: string, question: string, notebookId = "nb-1"): ActivityReport {
  return {
    type: "report",
    id,
    notebook_id: notebookId,
    created_at: "2026-08-04T10:30:00",
    updated_at: "2026-08-04T10:40:00",
    question,
    depth: 4,
    status: "done",
    generation_started_at: "2026-08-04T10:31:00",
  };
}

// fetchUserReportDetail 的成功响应,字段逐字对应后端 ReportActivityDetail
// （types.ts::ReportDetail）。测试各自用 `{ ...reportDetail(...), 字段: 值 }` 覆盖。
function reportDetail(overrides: Partial<ReportDetail> = {}): ReportDetail {
  return {
    report_id: "r1",
    notebook_id: "nb-1",
    question: "这次报告问了什么",
    depth: 4,
    status: "done",
    created_at: "2026-08-04T10:30:00",
    updated_at: "2026-08-04T10:40:00",
    generation_started_at: "2026-08-04T10:31:00",
    error: "",
    content_md: "",
    references: [],
    ...overrides,
  };
}

function page(items: ActivityItem[], hasMore = false) {
  return {
    items,
    has_more: hasMore,
    next_cursor: hasMore
      ? { ts: items[items.length - 1]?.created_at ?? "", id: items[items.length - 1]?.id ?? "" }
      : null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

// 来源清单接口的一行。`title` 是原始列、`display_title` 是后端按
// source_display_title 合成的显示名——左栏必须读后者(见下面那条显示名用例)。
function failedSource() {
  return {
    id: "src-1",
    notebook_id: "nb-1",
    title: "季度报告",
    display_title: "季度报告",
    type: "pdf",
    status: "failed",
    parse_status: "failed",
    summary: "",
    element_count: 0,
    file_name: "q3.pdf",
    file_size: 10,
    // ⚠ 这两个字段刻意指向**不同的日子**:`created_at` 是权威的原始时间戳(右栏按
    // 浏览器本地时区渲染),`created_label` 是服务端按服务端日历日算好的旧字符串。
    // 左栏若退回读 created_label,下面那条「两个入口时间逐字相同」会立刻报红。
    created_at: "2026-08-04T09:00:00",
    created_label: "2026-08-01",
    parse_failed: true,
  };
}

// 同一份来源在中栏活动流里的形状(toActivitySource 的目标形状)。左右两栏点的是
// 同一件东西,右栏必须显示同一个时刻。
function streamSource(): ActivitySource {
  const source = failedSource();
  return {
    type: "source",
    id: source.id,
    notebook_id: source.notebook_id,
    created_at: source.created_at,
    display_title: source.display_title,
    file_name: source.file_name,
    source_type: source.type,
    parse_status: source.parse_status,
    status: source.status,
    parse_failed: true,
    extraction_warning: "",
    parse_quality_warning: false,
    paper_meta_status: "",
  };
}

function sourcePage(items: ReturnType<typeof failedSource>[], total = 2) {
  return { items, total_count: total, offset: 0, limit: 50 };
}

function view() {
  return render(<ActivityView now={NOW} scopeKey='["activity","user-1",""]' userId="user-1" />);
}


test("解析问题深链通过管理员只读端点精确打开来源详情", async () => {
  window.history.replaceState(
    {},
    "",
    "/dev/logs?view=activity&owner=user-1&activity_type=source&notebook_id=nb-1&source_id=src-1",
  );
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSource.mockResolvedValue(streamSource());

  view();

  expect(await screen.findByRole("heading", { name: "季度报告" })).toBeInTheDocument();
  expect(mocks.fetchUserNotebookSource).toHaveBeenCalledWith(
    "user-1", "nb-1", "src-1",
  );
  expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
    "user-1",
    expect.objectContaining({ activityType: "source", notebookId: "nb-1" }),
  );
  window.history.replaceState({}, "", "/dev/logs");
});


test("解析问题深链的迟到详情不会跨到另一个笔记本范围", async () => {
  const user = userEvent.setup();
  const stale = deferred<ActivitySource>();
  window.history.replaceState(
    {},
    "",
    "/dev/logs?view=activity&owner=user-1&activity_type=source&notebook_id=nb-1&source_id=src-1",
  );
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSource.mockReturnValue(stale.promise);

  view();

  await waitFor(() => expect(mocks.fetchUserNotebookSource).toHaveBeenCalledTimes(1));
  await user.click(await screen.findByRole("button", { name: /^笔记本二/ }));
  stale.resolve(streamSource());

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
      "user-1", expect.objectContaining({ notebookId: "nb-2" }),
    );
  });
  expect(screen.queryByRole("heading", { name: "季度报告" })).not.toBeInTheDocument();
  window.history.replaceState({}, "", "/dev/logs");
});


test("解析问题深链详情失败后可就地重试", async () => {
  const user = userEvent.setup();
  window.history.replaceState(
    {},
    "",
    "/dev/logs?view=activity&owner=user-1&activity_type=source&notebook_id=nb-1&source_id=src-1",
  );
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSource
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce(streamSource());

  view();

  expect(await screen.findByText("来源详情加载失败，请重试")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByRole("heading", { name: "季度报告" })).toBeInTheDocument();
  expect(mocks.fetchUserNotebookSource).toHaveBeenCalledTimes(2);
  window.history.replaceState({}, "", "/dev/logs");
});


test("左栏列出该用户的笔记本与界面词计数（提问用 questions，不是会话容器数）", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  view();

  const row = await screen.findByRole("button", { name: /^笔记本一/ });
  expect(row.textContent).toContain("来源 2");
  expect(row.textContent).toContain("提问 3"); // questions=3，不是 conversations=1
  expect(row.textContent).toContain("报告 0");
});

// F3:提问分析页签用「允许的活动类型子集」(activityTypeOptions)取代此前固定死的
// fixedActivityType="ask"——只出现两个按钮(不是默认四选一),点「深度报告」按新
// 类型重新取数并把 activity_type 写回 URL。
test("嵌入提问分析页时只渲染子集里的活动类型按钮，点击后按新类型重新取数", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockImplementation((_userId, params) => (
    params.activityType === "report"
      ? Promise.resolve(page([report("r1", "分析报告")]))
      : Promise.resolve(page([ask("ask-1", "分析问题")]))
  ));
  window.history.replaceState(null, "", "/admin/usage?sheet=questions");
  render(
    <ActivityView
      activityTypeOptions={[
        { value: "ask", label: "问答" },
        { value: "report", label: "深度报告" },
      ]}
      now={NOW}
      scopeKey='["admin-usage-questions","user-1"]'
      userId="user-1"
    />,
  );

  expect(await screen.findByText("分析问题")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenCalledWith(
    "user-1", expect.objectContaining({ activityType: "ask" }),
  );
  const filter = screen.getByRole("group", { name: "按活动类型筛选" });
  expect(within(filter).getAllByRole("button")).toHaveLength(2);
  expect(within(filter).getByRole("button", { name: "问答" })).toBeInTheDocument();
  expect(within(filter).getByRole("button", { name: "深度报告" })).toBeInTheDocument();

  await user.click(within(filter).getByRole("button", { name: "深度报告" }));

  expect(await screen.findByText("分析报告")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
    "user-1", expect.objectContaining({ activityType: "report" }),
  );
  expect(window.location.search).toContain("activity_type=report");
  window.history.replaceState({}, "", "/dev/logs");
});


// 子集页深链:URL 的 activity_type 在子集内就直接采用。
test("嵌入子集页时，URL activity_type 若在子集内则首个请求就用它", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockImplementation((_userId, params) => (
    params.activityType === "report"
      ? Promise.resolve(page([report("r1", "分析报告")]))
      : Promise.resolve(page([ask("ask-1", "分析问题")]))
  ));
  window.history.replaceState(null, "", "/admin/usage?sheet=questions&activity_type=report");
  render(
    <ActivityView
      activityTypeOptions={[
        { value: "ask", label: "问答" },
        { value: "report", label: "深度报告" },
      ]}
      now={NOW}
      scopeKey='["admin-usage-questions","user-1"]'
      userId="user-1"
    />,
  );

  expect(await screen.findByText("分析报告")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(1);
  expect(mocks.fetchUserActivity).toHaveBeenCalledWith(
    "user-1", expect.objectContaining({ activityType: "report" }),
  );
  const filter = screen.getByRole("group", { name: "按活动类型筛选" });
  expect(within(filter).getByRole("button", { name: "深度报告" })).toHaveAttribute("aria-pressed", "true");
  window.history.replaceState({}, "", "/dev/logs");
});


// 子集页深链的另一半:URL 的 activity_type 不在子集内时(这里是子集页压根不提供
// 的 "source"),退回子集第一项,不落到「全部」——子集页本来就不给「全部」这个选项。
test("嵌入子集页时，URL activity_type 若不在子集内则退回子集第一项", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockImplementation((_userId, params) => (
    params.activityType === "report"
      ? Promise.resolve(page([report("r1", "分析报告")]))
      : Promise.resolve(page([ask("ask-1", "分析问题")]))
  ));
  window.history.replaceState(null, "", "/admin/usage?sheet=questions&activity_type=source");
  render(
    <ActivityView
      activityTypeOptions={[
        { value: "ask", label: "问答" },
        { value: "report", label: "深度报告" },
      ]}
      now={NOW}
      scopeKey='["admin-usage-questions","user-1"]'
      userId="user-1"
    />,
  );

  expect(await screen.findByText("分析问题")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenCalledWith(
    "user-1", expect.objectContaining({ activityType: "ask" }),
  );
  const filter = screen.getByRole("group", { name: "按活动类型筛选" });
  expect(within(filter).getByRole("button", { name: "问答" })).toHaveAttribute("aria-pressed", "true");
  window.history.replaceState({}, "", "/dev/logs");
});


test("展开笔记本取回该库的来源清单，异常小字同样经 AnomalyBadge 渲染", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSources.mockResolvedValue(sourcePage([failedSource()]));
  const { container } = view();

  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));

  expect(await screen.findByText("季度报告")).toBeInTheDocument();
  expect(mocks.fetchUserNotebookSources).toHaveBeenCalledWith("user-1", "nb-1", { limit: 50 });
  const badge = container.querySelector(".activity-src .anomaly-badge");
  expect(badge).toHaveClass("anomaly-badge--integrity");
  // 只取回了 2 个里的 1 个,清单必须如实说出边界而不是假装列全了。
  expect(screen.getByText("已显示 1 / 2 个来源")).toBeInTheDocument();
});

test("来源清单超过一页时「加载更多来源」按已取回条数取下一页并追加", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  const second = { ...failedSource(), id: "src-2", title: "年度报告", display_title: "年度报告" };
  const more = deferred<ReturnType<typeof sourcePage>>();
  mocks.fetchUserNotebookSources
    .mockResolvedValueOnce(sourcePage([failedSource()]))
    .mockReturnValueOnce(more.promise);
  view();

  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));
  expect(await screen.findByText("季度报告")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "加载更多来源" }));
  expect(mocks.fetchUserNotebookSources).toHaveBeenLastCalledWith(
    "user-1", "nb-1", { offset: 1, limit: 50 },
  );
  // 追加页在途:按钮禁用防连点,已列出的来源不被清掉。
  expect(screen.getByRole("button", { name: "加载中…" })).toBeDisabled();
  expect(screen.getByText("季度报告")).toBeInTheDocument();

  more.resolve(sourcePage([second]));
  expect(await screen.findByText("年度报告")).toBeInTheDocument();
  expect(screen.getByText("季度报告")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "加载更多来源" })).not.toBeInTheDocument();
  expect(screen.queryByText(/已显示/)).not.toBeInTheDocument();
});

test("追加页在途时换了被查看用户，迟到的追加页被丢弃", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  const late = deferred<ReturnType<typeof sourcePage>>();
  mocks.fetchUserNotebookSources
    .mockResolvedValueOnce(sourcePage([failedSource()]))
    .mockReturnValueOnce(late.promise)
    .mockResolvedValue(sourcePage([failedSource()], 1));
  const { rerender } = view();

  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));
  await screen.findByText("季度报告");
  await user.click(screen.getByRole("button", { name: "加载更多来源" }));

  rerender(<ActivityView now={NOW} scopeKey='["activity","user-2",""]' userId="user-2" />);
  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));
  expect(await screen.findByText("季度报告")).toBeInTheDocument();

  late.resolve(sourcePage([{ ...failedSource(), id: "src-late", title: "上一位用户的来源", display_title: "上一位用户的来源" }]));
  await new Promise((settle) => setTimeout(settle, 0));
  expect(screen.queryByText("上一位用户的来源")).not.toBeInTheDocument();
});

test("追加页失败只在按钮旁报错，已列出的来源保留且可以重试", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSources
    .mockResolvedValueOnce(sourcePage([failedSource()]))
    .mockRejectedValueOnce(new Error("boom"));
  view();

  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));
  await screen.findByText("季度报告");
  await user.click(screen.getByRole("button", { name: "加载更多来源" }));

  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.getByText("季度报告")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "加载更多来源" })).toBeEnabled();
});

function manyNotebooks(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    ...NOTEBOOKS[0],
    id: `nb-${index + 1}`,
    name: `笔记本${String(index + 1).padStart(2, "0")}`,
  }));
}

test("深链选中的笔记本在左栏后面一页时，左栏翻到它所在的页", async () => {
  window.history.replaceState(
    {},
    "",
    "/dev/logs?view=activity&owner=user-1&activity_type=source&notebook_id=nb-23&source_id=src-1",
  );
  mocks.fetchUserNotebooks.mockResolvedValue(manyNotebooks(25));
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSource.mockResolvedValue({ ...streamSource(), notebook_id: "nb-23" });
  try {
    view();

    const selected = await screen.findByRole("button", { name: /^笔记本23/ });
    expect(selected).toHaveClass("selected");
    expect(screen.getByRole("navigation", { name: "笔记本清单分页" })).toHaveTextContent("21–25 / 25");
    expect(screen.queryByRole("button", { name: /^笔记本01/ })).not.toBeInTheDocument();
  } finally {
    window.history.replaceState({}, "", "/dev/logs");
  }
});

test("笔记本超过一页时左栏分页", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(manyNotebooks(25));
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  view();

  expect(await screen.findByRole("button", { name: /^笔记本01/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^笔记本21/ })).not.toBeInTheDocument();
  const pager = screen.getByRole("navigation", { name: "笔记本清单分页" });
  expect(pager).toHaveTextContent("1–20 / 25");

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(screen.getByRole("button", { name: /^笔记本23/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^笔记本01/ })).not.toBeInTheDocument();
});


// ⚠ `docs/product-and-api.md` 契约:所有为用户命名来源的路径共用 source_display_title 这一个真源。
// 已接地的论文 title 仍是上传文件名(1706.03762.pdf),显示名在后端合成好的
// display_title 里。左栏改回读 `source.title` 时,这条会读到文件名而报红——那正是
// 「同一篇论文在同一屏里有两个名字」(左栏文件名 / 中栏论文标题)的原形。
test("左栏来源名用后端合成的显示名，不是原始 title 列", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSources.mockResolvedValue(sourcePage([{
    ...failedSource(),
    id: "src-paper",
    title: "1706.03762.pdf",
    display_title: "Attention Is All You Need",
    file_name: "1706.03762.pdf",
    status: "extracted",
    parse_status: "extracted",
  }], 1));
  view();

  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));

  expect(await screen.findByText("Attention Is All You Need")).toBeInTheDocument();
  expect(screen.queryByText("1706.03762.pdf")).not.toBeInTheDocument();
});


// 正向对照:没有这条,下面那条「旧响应不被拼进来」可能因为「加载更多压根没生效」
// 而假绿。
test("加载更多把下一页追加到当前列表", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity
    .mockResolvedValueOnce(page([ask("a1", "第一页的提问")], true))
    .mockResolvedValueOnce(page([ask("a2", "第二页的提问")]));
  view();

  expect(await screen.findByText("第一页的提问")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "加载更多" }));

  expect(await screen.findByText("第二页的提问")).toBeInTheDocument();
  expect(screen.getByText("第一页的提问")).toBeInTheDocument();
});


// 竞态红线:mergeActivityPages 是纯函数、看不见 scope,拦截必须发生在调用点。
// 对笔记本一点「加载更多」、响应未回时切到笔记本二,那一页绝不能被拼进来。
test("切换笔记本后，上一个笔记本的迟到分页不被拼进列表", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  const stale = deferred<ReturnType<typeof page>>();
  mocks.fetchUserActivity
    .mockResolvedValueOnce(page([ask("a1", "笔记本一的提问")], true))
    .mockReturnValueOnce(stale.promise)
    .mockResolvedValueOnce(page([ask("b1", "笔记本二的提问", "nb-2")]));
  view();

  expect(await screen.findByText("笔记本一的提问")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "加载更多" }));

  await user.click(await screen.findByRole("button", { name: /^笔记本二/ }));
  expect(await screen.findByText("笔记本二的提问")).toBeInTheDocument();

  stale.resolve(page([ask("a2", "笔记本一的迟到分页")]));
  await waitFor(() => {
    expect(screen.getByText("笔记本二的提问")).toBeInTheDocument();
  });
  expect(screen.queryByText("笔记本一的迟到分页")).not.toBeInTheDocument();
  expect(screen.queryByText("笔记本一的提问")).not.toBeInTheDocument();
});


// reload() 的 finally 靠 generation + scopeKey 双重校验才清 loading:旧范围的响应
// 迟到时,它自己的 finally 必须认出自己已经过期,不能提前把新范围仍在进行的加载态
// 清掉(那会让类型筛选按钮在新请求还没回来时就被误判为"可点")。
test("切换笔记本时，上一个笔记本迟到的响应不会提前清掉新笔记本仍在进行的加载态", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  const first = deferred<ReturnType<typeof page>>();
  const second = deferred<ReturnType<typeof page>>();
  mocks.fetchUserActivity
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  view();

  await waitFor(() => expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(1));
  await user.click(await screen.findByRole("button", { name: /^笔记本二/ }));
  await waitFor(() => expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(2));
  const sourceFilter = screen.getByRole("button", { name: "来源" });
  expect(sourceFilter).toBeDisabled();

  // 笔记本一(已经切走的旧范围)这时才姗姗来迟地返回。
  first.resolve(page([ask("a1", "笔记本一的迟到提问")]));
  await waitFor(() => expect(sourceFilter).toBeDisabled());
  expect(screen.queryByText("笔记本一的迟到提问")).not.toBeInTheDocument();

  second.resolve(page([]));
  await waitFor(() => expect(sourceFilter).toBeEnabled());
});


test("切换笔记本会把 notebook_id 下推给活动流端点", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  view();

  await user.click(await screen.findByRole("button", { name: /^笔记本二/ }));
  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
      notebookId: "nb-2",
      since: undefined,
      until: undefined,
      limit: 50,
    });
  });
});


test("日期区间原样下推，不在前端过滤", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  render(
    <ActivityView
      now={NOW}
      scopeKey='["activity","user-1","2026-08-04"]'
      since="2026-08-04T00:00:00"
      until="2026-08-05T00:00:00"
      userId="user-1"
    />,
  );

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenCalledWith("user-1", {
      notebookId: undefined,
      since: "2026-08-04T00:00:00",
      until: "2026-08-05T00:00:00",
      limit: 50,
    });
  });
});


test("提问概览把筛选下推给服务端，并保留完整问题文本", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockImplementation((_userId, params) => (
    params.activityType === "ask"
      ? Promise.resolve(page([ask("focus-1", "封装噪声为什么比仿真高？")]))
      : Promise.resolve(page([]))
  ));
  window.history.replaceState(null, "", "/dev/logs");
  view();

  await user.click(screen.getByRole("button", { name: "提问" }));

  expect(await screen.findByText("封装噪声为什么比仿真高？")).toBeInTheDocument();
  expect(screen.getByText("集中查看用户提出的问题，了解当前关注点。")).toBeInTheDocument();
  expect(screen.getByText("全部提问（含共享笔记本）")).toBeInTheDocument();
  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
      activityType: "ask",
      notebookId: undefined,
      since: undefined,
      until: undefined,
      limit: 50,
    });
  });
  expect(window.location.search).toContain("activity_type=ask");
  expect(screen.getByRole("group", { name: "按活动类型筛选" })).toBeInTheDocument();
  window.history.replaceState(null, "", "/dev/logs");
});


// F3:report 分支复用同一套中栏标题/全部笔记本文案覆盖——与上面 ask 分支对称。
test("报告概览把筛选下推给服务端，中栏标题与全部笔记本文案随之切换", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockImplementation((_userId, params) => (
    params.activityType === "report"
      ? Promise.resolve(page([report("report-1", "这份报告问了什么")]))
      : Promise.resolve(page([]))
  ));
  window.history.replaceState(null, "", "/dev/logs");
  view();

  await user.click(screen.getByRole("button", { name: "报告" }));

  expect(await screen.findByText("这份报告问了什么")).toBeInTheDocument();
  expect(screen.getByText("报告概览")).toBeInTheDocument();
  expect(screen.getByText("全部报告（含共享笔记本）")).toBeInTheDocument();
  // 「提问」类型专属的聚焦提示不该串到「报告」分支。
  expect(screen.queryByText("集中查看用户提出的问题，了解当前关注点。")).not.toBeInTheDocument();
  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
      activityType: "report",
      notebookId: undefined,
      since: undefined,
      until: undefined,
      limit: 50,
    });
  });
  expect(window.location.search).toContain("activity_type=report");
  window.history.replaceState(null, "", "/dev/logs");
});


test("提问概览深链在首个请求就下推 ask 筛选", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  window.history.replaceState(null, "", "/dev/logs?view=activity&activity_type=ask");

  view();

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenCalledWith("user-1", {
      activityType: "ask",
      notebookId: undefined,
      since: undefined,
      until: undefined,
      limit: 50,
    });
  });
  expect(screen.getByText("提问概览")).toBeInTheDocument();
  window.history.replaceState(null, "", "/dev/logs");
});


test("活动流重载期间禁用类型筛选，避免重复发起并发查询", async () => {
  const user = userEvent.setup();
  const first = deferred<ReturnType<typeof page>>();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockReturnValue(first.promise);
  window.history.replaceState(null, "", "/dev/logs");

  view();

  const sourceFilter = screen.getByRole("button", { name: "来源" });
  await waitFor(() => expect(sourceFilter).toBeDisabled());
  expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(1);

  await user.click(sourceFilter);
  expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(1);

  first.resolve(page([]));
  await waitFor(() => expect(sourceFilter).toBeEnabled());
});


test("类型筛选失败后在筛选旁显示结果并可就地重试", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce(page([ask("retry-1", "重试后看到的问题")]));
  window.history.replaceState(null, "", "/dev/logs");

  view();

  const feedback = await screen.findByRole("alert");
  expect(feedback).toHaveTextContent("活动记录加载失败，请重试");
  expect(feedback.closest(".activity-stream-head")).not.toBeNull();
  await user.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByText("重试后看到的问题")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("活动记录加载失败，请重试")).not.toBeInTheDocument();
});


test("选中一条提问时取回它的详情，并复用既有推理轨迹面板", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([ask("a1", "这次问了什么")]));
  mocks.fetchUserAskDetail.mockResolvedValue({
    job_id: "a1",
    notebook_id: "nb-1",
    conversation_id: "conv-a1",
    question: "这次问了什么",
    mode: "reasoning",
    status: "done",
    asked_at: "2026-08-04T10:29:00",
    answered_at: "2026-08-04T10:31:00",
    error: "",
    trace: [{ step_type: "memory", summary: "找到 2 条相关记忆", detail: {}, duration_ms: 120 }],
    answer: null,
  });
  const { container } = view();

  await user.click(await screen.findByText("这次问了什么"));

  await waitFor(() => {
    expect(mocks.fetchUserAskDetail).toHaveBeenCalledWith("user-1", "a1");
  });
  expect(await screen.findByText("找到 2 条相关记忆")).toBeInTheDocument();
  expect(container.querySelector(".reasoning-trace-panel")).not.toBeNull();
});


// F2:选中一条报告时取回它的只读详情正文,复用 report-view.tsx 的 ReportMarkdown。
test("选中一条报告时取回它的详情，报告正文渲染出来", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockResolvedValue(reportDetail({
    content_md: "这是报告正文内容。",
  }));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  await waitFor(() => {
    expect(mocks.fetchUserReportDetail).toHaveBeenCalledWith("user-1", "r1");
  });
  expect(await screen.findByText("这是报告正文内容。")).toBeInTheDocument();
});


test("报告详情 403 落到固定的无权限态，不给重试暗示", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockRejectedValue(new Error("forbidden"));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  expect(await screen.findByText("没有权限查看这位用户的活动记录。")).toBeInTheDocument();
  expect(screen.queryByText(/请重试/)).not.toBeInTheDocument();
});


test("报告详情加载失败时右栏显示错误文案", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockRejectedValue(new Error("boom"));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  expect(await screen.findByText("报告详情加载失败，请重试")).toBeInTheDocument();
});


// 失败原因区块与下面的空态互补:failed 且有错误原文才显示「失败原因：」。
test("报告状态为 failed 且有错误原文时显示失败原因", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockResolvedValue(reportDetail({
    status: "failed",
    error: "模型服务超时",
  }));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  expect(await screen.findByText("失败原因：")).toBeInTheDocument();
  expect(screen.getByText("模型服务超时")).toBeInTheDocument();
  expect(screen.queryByText("这份报告还没有生成正文")).not.toBeInTheDocument();
});


// 回归门:失败原因区块的显示条件与下面空态的显示条件曾经不互补——「状态还没推进
// 到 failed,但已经带着一条错误原文」这种边界会让两边条件都不满足,右栏空白一片。
// 现在两者按 `!(failed && failure)` 互补,这种边界必须落到空态,而不是两边都不显示。
test("报告状态非 failed 但带错误原文时，右栏落到空态而不是两边都空白", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockResolvedValue(reportDetail({
    status: "running",
    error: "残留的错误原文",
  }));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  expect(await screen.findByText("这份报告还没有生成正文")).toBeInTheDocument();
  expect(screen.queryByText("失败原因：")).not.toBeInTheDocument();
  expect(screen.queryByText("残留的错误原文")).not.toBeInTheDocument();
});


test("报告还没有生成正文时右栏显示空态文案", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockResolvedValue(reportDetail({ status: "running" }));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  expect(await screen.findByText("这份报告还没有生成正文")).toBeInTheDocument();
});


// 与 AskDetailPane 同一条规则:详情端点的 notebook_deleted_at 是权威,合并进
// RetainedActivityNotice;此时不再显示「还没有生成正文」的空态(避免两句互相矛盾)。
test("报告所在笔记本已删除时右栏显示留存说明，不再显示空态文案", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockResolvedValue(reportDetail({
    content_md: "",
    notebook_name: "笔记本一",
    notebook_deleted_at: "2026-08-10T00:00:00",
    retained_until: "2026-09-10T00:00:00",
  }));
  view();

  await user.click(await screen.findByText("这次报告问了什么"));

  expect(await screen.findByText(/原笔记本《笔记本一》已删除/)).toBeInTheDocument();
  expect(screen.queryByText("这份报告还没有生成正文")).not.toBeInTheDocument();
});


// 竞态红线:报告详情与 ask 详情共用同一套 detailGenerationRef + streamScopeRef。
// 选中第一份报告后(请求进行中)切到第二份报告,第一份的迟到响应绝不能覆盖已经
// 换到的第二份报告的正文。
test("切换选中的报告后，上一条报告的迟到详情不会覆盖新选中报告的正文", async () => {
  const user = userEvent.setup();
  const stale = deferred<ReportDetail>();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([
    report("r1", "第一份报告"),
    report("r2", "第二份报告"),
  ]));
  mocks.fetchUserReportDetail
    .mockReturnValueOnce(stale.promise)
    .mockResolvedValueOnce(reportDetail({
      report_id: "r2",
      question: "第二份报告",
      content_md: "第二份报告的正文",
    }));
  view();

  await user.click(await screen.findByText("第一份报告"));
  await waitFor(() => expect(mocks.fetchUserReportDetail).toHaveBeenCalledTimes(1));

  await user.click(screen.getByText("第二份报告"));
  expect(await screen.findByText("第二份报告的正文")).toBeInTheDocument();

  stale.resolve(reportDetail({ content_md: "迟到的报告正文" }));

  await waitFor(() => {
    expect(screen.getByText("第二份报告的正文")).toBeInTheDocument();
  });
  expect(screen.queryByText("迟到的报告正文")).not.toBeInTheDocument();
});


// selectItem 传回的是 items 数组里同一个对象引用:重新点同一行时 React 按 Object.is
// 拦掉这次 setSelected,详情 effect 不会因为 `selected` 变化而重跑——但错误文案写的
// 正是「请重试」,不能点了没反应。
test("报告详情加载失败后，再次点击同一行会重新发起请求", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce(reportDetail({ content_md: "重试后的报告正文" }));
  view();

  const row = await screen.findByText("这次报告问了什么");
  await user.click(row);
  expect(await screen.findByText("报告详情加载失败，请重试")).toBeInTheDocument();

  await user.click(row);

  expect(await screen.findByText("重试后的报告正文")).toBeInTheDocument();
  expect(mocks.fetchUserReportDetail).toHaveBeenCalledTimes(2);
});


// 同一条规则也适用于提问详情(与上面报告详情共用 detailAttempt 计数器)。
test("提问详情加载失败后，再次点击同一行会重新发起请求", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([ask("a1", "这次问了什么")]));
  mocks.fetchUserAskDetail
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce({
      job_id: "a1",
      notebook_id: "nb-1",
      conversation_id: "conv-a1",
      question: "这次问了什么",
      mode: "reasoning",
      status: "done",
      asked_at: "2026-08-04T10:29:00",
      answered_at: "2026-08-04T10:31:00",
      error: "",
      trace: [],
      answer: null,
    });
  view();

  const row = await screen.findByText("这次问了什么");
  await user.click(row);
  expect(await screen.findByText("问答详情加载失败，请重试")).toBeInTheDocument();

  await user.click(row);

  await waitFor(() => {
    expect(mocks.fetchUserAskDetail).toHaveBeenCalledTimes(2);
  });
  expect(screen.queryByText("问答详情加载失败，请重试")).not.toBeInTheDocument();
});


// 换用户与「点某一行」是两个独立的状态更新源:同一次 commit 里可能出现 userId 已经
// 是新用户、但 `selected` 仍是旧用户那一项的窗口(reload() 里 setSelected(null) 要
// 等下一轮渲染才生效)。这里必须靠 selectedOwnerId 挡住,不能把旧项的 id 发给新
// 用户的详情端点——报告端点在 SQLite 上还会白占一次写锁。
test("换用户后不会用旧选中项的 id 请求新用户的报告详情", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([report("r1", "这次报告问了什么")]));
  mocks.fetchUserReportDetail.mockResolvedValue(reportDetail({ content_md: "user-1 的报告正文" }));
  const { rerender } = render(
    <ActivityView now={NOW} scopeKey='["activity","user-1",""]' userId="user-1" />,
  );

  await user.click(await screen.findByText("这次报告问了什么"));
  await waitFor(() => {
    expect(mocks.fetchUserReportDetail).toHaveBeenCalledWith("user-1", "r1");
  });
  mocks.fetchUserReportDetail.mockClear();

  rerender(
    <ActivityView now={NOW} scopeKey='["activity","user-2",""]' userId="user-2" />,
  );

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
      "user-2", expect.objectContaining({ notebookId: undefined }),
    );
  });
  // "r1" 是 user-1 选中的报告,绝不能拿它的 id 去请求 user-2 的报告详情端点。
  expect(mocks.fetchUserReportDetail).not.toHaveBeenCalled();
  expect(screen.queryByText("user-1 的报告正文")).not.toBeInTheDocument();
});


// ⚠ 右栏刻意**不**渲染后端的 `error_message` 原文（它可能带服务端绝对路径，而
// 管理员看的是别人的活动流）。契约给的是 `parse_failed` 布尔，界面只给固定文案。
test("选中一条来源时右栏按 parse_failed 显示固定文案与异常小字", async () => {
  const user = userEvent.setup();
  const failed: ActivitySource = {
    type: "source",
    id: "src-9",
    notebook_id: "nb-1",
    created_at: "2026-08-04T09:00:00",
    display_title: "坏掉的文档",
    file_name: "broken.pdf",
    source_type: "pdf",
    parse_status: "failed",
    status: "failed",
    parse_failed: true,
    extraction_warning: "",
    parse_quality_warning: false,
    paper_meta_status: "",
  };
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([failed]));
  const { container } = view();

  await user.click(await screen.findByText("坏掉的文档"));

  expect(screen.getByText("原始文件")).toBeInTheDocument();
  expect(container.querySelector(".detail-error")?.textContent)
    .toBe("解析没有成功完成，这个来源没有可检索的内容。");
  expect(container.querySelector(".activity-anomalies.block .anomaly-badge")).not.toBeNull();
});


// F5:右栏来源详情有**两个**入口。中栏走 created_at(浏览器本地时区),左栏曾经把
// 服务端按**服务端**日历日算好的 created_label 当兜底传进右栏——服务端 UTC+8、
// 浏览器 UTC 时,同一份来源左栏点是「8月5日」、中栏点是「8月4日 17:00」。验收判据
// 因此是「两个入口逐字相同」,而不是「左栏能显示出点什么」。
test("同一份来源从左栏点开与从中栏点开，右栏时间逐字相同", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([streamSource()]));
  mocks.fetchUserNotebookSources.mockResolvedValue(sourcePage([failedSource()], 1));
  const { container } = view();

  const detailTime = () => container
    .querySelector(".activity-detail .activity-detail-time")
    ?.textContent ?? "";

  // ① 中栏:此时左栏还没展开,"季度报告"只有活动流那一处。
  await user.click(await screen.findByText("季度报告"));
  const fromStream = detailTime();
  // 正向对照:两边都空同样能让下面那条相等断言通过。09:00 = created_at 按浏览器本地
  // 时区渲染(NOW 是同一天),既不是服务端的 2026-08-01,也不是空。
  expect(fromStream).toBe("09:00");

  // ② 左栏:同一份来源。
  await user.click(screen.getByRole("button", { name: "展开《笔记本一》的来源" }));
  await waitFor(() => {
    expect(container.querySelector(".activity-src-row")).not.toBeNull();
  });
  await user.click(container.querySelector(".activity-src-row") as HTMLElement);

  expect(detailTime()).toBe(fromStream);
  expect(screen.queryByText("2026-08-01")).not.toBeInTheDocument();
});


test("尚未确定用户时不发任何请求", () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  render(<ActivityView now={NOW} scopeKey='["activity","",""]' userId="" />);

  expect(mocks.fetchUserActivity).not.toHaveBeenCalled();
  expect(mocks.fetchUserNotebooks).not.toHaveBeenCalled();
});


// F5:「身份还没确定」与「确定了、就是空」是两回事。混为一谈会让页面在挂载瞬间
// 先说一句「这个范围里没有活动记录」,一个 RTT 之后才改口说「加载中」。
test("用户身份未就绪时渲染加载态，而不是「没有活动记录」", () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  render(<ActivityView now={NOW} scopeKey='["activity","",""]' userId="" userPending />);

  expect(screen.queryByText("这个范围里没有活动记录")).not.toBeInTheDocument();
  expect(screen.queryByText("这位用户还没有建过笔记本")).not.toBeInTheDocument();
  expect(screen.getAllByText("加载中…").length).toBeGreaterThan(0);
});


// F5 的另一半:fetchMe 失败必须可见。此前它被 `.catch(() => undefined)` 整个吞掉,
// 活动视图会**永远**声称这位用户没有任何活动,界面上不给任何提示。
//
// 「身份出错」是第三态,不是「确定了、就是空」:错误横幅之外绝不能再同屏出现
// 「这位用户还没有建过笔记本」「这个范围里没有活动记录」——那会把「未知」说成
// 「确定为空」(P2,codex 评审第 1 轮)。userId="" 时这两个组件各自的取数都提前
// 返回,不加 identityErrored 就会落进它们各自的空结果分支。
test("取不到当前用户时把错误上屏，不假装这位用户没有活动/没有笔记本", () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  render(
    <ActivityView
      now={NOW}
      scopeKey='["activity","",""]'
      userError="当前用户信息加载失败，请刷新页面重试"
      userId=""
    />,
  );

  expect(screen.getByText("当前用户信息加载失败，请刷新页面重试")).toBeInTheDocument();
  expect(screen.queryByText("这位用户还没有建过笔记本")).not.toBeInTheDocument();
  expect(screen.queryByText("这个范围里没有活动记录")).not.toBeInTheDocument();
});


// F6: 500 时顶部说「加载失败」、左栏同时说「还没有建过笔记本」是互相矛盾的两句话,
// 而后者是一句会被当真的假陈述。失败态必须是左栏自己的一档,并且给得起重试。
test("笔记本清单加载失败时左栏落失败态，不说「还没有建过笔记本」", async () => {
  const user = userEvent.setup();
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebooks
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce(NOTEBOOKS);
  view();

  expect(await screen.findByText("笔记本清单加载失败，请重试")).toBeInTheDocument();
  expect(screen.queryByText("这位用户还没有建过笔记本")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByRole("button", { name: /^笔记本一/ })).toBeInTheDocument();
  expect(screen.queryByText("笔记本清单加载失败，请重试")).not.toBeInTheDocument();
});


// F6 的第二半:笔记本清单的失败与活动流的错误不共用一个 slot。共用时随手改个日期
// 触发一次 reload 就会把它清掉,只剩左栏那句假陈述。
test("活动流重新取数不会抹掉笔记本清单的失败态", async () => {
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebooks.mockRejectedValue(new Error("boom"));
  const { rerender } = render(
    <ActivityView now={NOW} scopeKey='["activity","user-1",""]' userId="user-1" />,
  );
  expect(await screen.findByText("笔记本清单加载失败，请重试")).toBeInTheDocument();

  rerender(
    <ActivityView
      now={NOW}
      scopeKey='["activity","user-1","2026-08-04"]'
      since="2026-08-04T00:00:00"
      until="2026-08-05T00:00:00"
      userId="user-1"
    />,
  );

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(2);
  });
  expect(screen.getByText("笔记本清单加载失败，请重试")).toBeInTheDocument();
});


// F7: FORBIDDEN_SENTINEL 不带文案,toUserMessage 认不出就会兜成「…请重试」——
// 而 403 重试一万次也不会成功。普通用户手工把别人的 id 拼进 ?owner= 就会走到这里。
test("403 落到固定的无权限态，不给重试暗示", async () => {
  mocks.fetchUserActivity.mockRejectedValue(new Error("forbidden"));
  mocks.fetchUserNotebooks.mockRejectedValue(new Error("forbidden"));
  view();

  expect(await screen.findByText("没有权限查看这位用户的活动记录。")).toBeInTheDocument();
  expect(screen.queryByText(/请重试/)).not.toBeInTheDocument();
});


// F8: 失败态对象同样 truthy,按「这个键有没有值」判会让收起再展开永远不再发请求,
// 而那条文案写的正是「请重试」。
test("来源清单加载失败后，收起再展开会重新取数", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSources
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce(sourcePage([failedSource()], 1));
  view();

  const toggle = await screen.findByRole("button", { name: "展开《笔记本一》的来源" });
  await user.click(toggle);
  expect(await screen.findByText("来源清单加载失败，请重试")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "收起《笔记本一》的来源" }));
  await user.click(screen.getByRole("button", { name: "展开《笔记本一》的来源" }));

  expect(await screen.findByText("季度报告")).toBeInTheDocument();
  expect(mocks.fetchUserNotebookSources).toHaveBeenCalledTimes(2);
});


// 右栏的职责就是渲染**任意历史** payload,asAnswer/asTrace 只是 `as` 断言。一条早年
// 写坏的答案不该把整页炸成白屏——而白屏正好发生在管理员最需要看那条坏记录的时候。
test("一条坏掉的历史答案不会白屏，其余两栏照常可用", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([ask("a1", "这次问了什么")]));
  mocks.fetchUserAskDetail.mockResolvedValue({
    job_id: "a1",
    notebook_id: "nb-1",
    conversation_id: "conv-a1",
    question: "这次问了什么",
    mode: "reasoning",
    status: "done",
    asked_at: "2026-08-04T10:29:00",
    answered_at: "2026-08-04T10:31:00",
    error: "",
    trace: [],
    // 缺 coverage 的表格结果:渲染时会在 `coverage.returned_rows` 上抛 TypeError。
    answer: {
      answer_id: "ans-1",
      answer: "坏掉的结果。",
      anchors: [],
      citations: [],
      result_sets: [{ kind: "knowhow", table_id: "t", title: "T", columns: [], rows: [] }],
    },
  });
  const { container } = view();

  await user.click(await screen.findByText("这次问了什么"));

  expect(await screen.findByText(/这条记录的内容没能显示出来/)).toBeInTheDocument();
  // 左栏与中栏照常在（白屏的反面）。
  expect(container.querySelector(".activity-scope")).not.toBeNull();
  expect(container.querySelector(".activity-stream")).not.toBeNull();
});


// 反向对照:成功结果仍然复用,收起再展开不重发。
test("来源清单已成功取回后，收起再展开不重复取数", async () => {
  const user = userEvent.setup();
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  mocks.fetchUserNotebookSources.mockResolvedValue(sourcePage([failedSource()], 1));
  view();

  await user.click(await screen.findByRole("button", { name: "展开《笔记本一》的来源" }));
  expect(await screen.findByText("季度报告")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "收起《笔记本一》的来源" }));
  await user.click(screen.getByRole("button", { name: "展开《笔记本一》的来源" }));

  expect(await screen.findByText("季度报告")).toBeInTheDocument();
  expect(mocks.fetchUserNotebookSources).toHaveBeenCalledTimes(1);
});
