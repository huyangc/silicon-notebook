import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchUserActivity: vi.fn(),
  fetchUserAskDetail: vi.fn(),
  fetchUserNotebookSources: vi.fn(),
  fetchUserNotebooks: vi.fn(),
}));

vi.mock("../../app/dev/logs/activity/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchUserActivity: mocks.fetchUserActivity,
  fetchUserAskDetail: mocks.fetchUserAskDetail,
  fetchUserNotebookSources: mocks.fetchUserNotebookSources,
}));
vi.mock("../../app/admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));

import { ActivityView } from "../../app/dev/logs/activity/ActivityView";
import type { ActivityAsk, ActivityItem, ActivitySource } from "../../app/dev/logs/activity/types";

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


test("左栏列出该用户的笔记本与界面词计数（提问用 questions，不是会话容器数）", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([]));
  view();

  const row = await screen.findByRole("button", { name: /^笔记本一/ });
  expect(row.textContent).toContain("来源 2");
  expect(row.textContent).toContain("提问 3"); // questions=3，不是 conversations=1
  expect(row.textContent).toContain("报告 0");
});

test("嵌入提问分析页时固定为提问且隐藏活动类型切换", async () => {
  mocks.fetchUserNotebooks.mockResolvedValue(NOTEBOOKS);
  mocks.fetchUserActivity.mockResolvedValue(page([ask("ask-1", "分析问题")]))
  render(
    <ActivityView
      fixedActivityType="ask"
      now={NOW}
      scopeKey='["admin-usage-questions","user-1"]'
      userId="user-1"
    />,
  );

  expect(await screen.findByText("分析问题")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenCalledWith(
    "user-1", expect.objectContaining({ activityType: "ask" }),
  );
  expect(screen.queryByRole("group", { name: "按活动类型筛选" })).not.toBeInTheDocument();
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
