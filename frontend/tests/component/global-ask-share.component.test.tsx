import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

// 全局问答的会话分享（任务 3：前端弹窗与接点）。
//
// 钉的是**接线**，不是弹窗正文：弹窗、五句范围文案、两条披露与全部按钮反馈都由
// `conversation-share-modal.component.test.tsx` 钉住，这里只证明全局这一侧把它接对了——
// 端点是全局那一组、边界是**作业**（job_id）、轮次经 `globalShareTurns` 适配（只取
// done）、错误语义与单库一致、切会话不串味、Esc 只收弹窗这一层。
//
// 网络闸放在 `api-client` 的 `requestJson`/`requestVoid` 上，而**不是**替掉
// `globalConversationShareApi` 本身：后者正是本任务要验的那段（路径怎么拼、body 里
// 送的是不是 expected_through_id），mock 掉它就只剩一个壳子。

import GlobalAskPage from "../../app/ask/page.tsx";
import { GlobalAskLauncher } from "../../app/ask/global-ask-launcher.tsx";
import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator.ts";
import { humanizedError } from "../../app/errors.ts";
import type { GlobalConversation, GlobalConversationDetail, GlobalJob } from "../../app/global-ask-api.ts";
import type { AskResponse, ConversationShareResponse, NotebookSummary } from "../../app/workspace-model.ts";

const net = vi.hoisted(() => ({
  requestJson: vi.fn(),
  requestVoid: vi.fn(),
  me: vi.fn(),
  notebooks: vi.fn(),
}));

vi.mock("../../app/api-client.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/api-client.ts")>(),
  requestJson: net.requestJson,
  requestVoid: net.requestVoid,
}));
vi.mock("../../app/auth.ts", () => ({ fetchMe: net.me }));
vi.mock("../../app/notebook-api.ts", () => ({ listNotebooks: net.notebooks }));

const notebooks: NotebookSummary[] = [{
  id: "nb-0", name: "材料研究", purpose: "电池资料", primary_domain: "",
  status: "ready", counts: { sources: 2 }, created_label: "今天",
}] as NotebookSummary[];

const conversation = (id = "conv-a"): GlobalConversation => ({
  id, title: `对话 ${id}`, created_at: "2026-09-19T01:00:00Z", updated_at: "2026-09-19T01:00:00Z",
  notebook_scope: { mode: "all" }, submitted_via: "web",
});

const answer = (answerId: string, memoryId = ""): AskResponse => ({
  answer_id: answerId, conversation_id: "conv-a", conclusion: "跨库结论。",
  answer: `跨库结论 ${answerId}。`, grounded: true, anchors: [], related_knowledge: [],
  citations: memoryId
    ? [{ label: "1", source_id: "s1", element_id: "e1", location_label: "第 1 页", quoted_span: "原文片段", memory_id: memoryId }]
    : [],
  llm_mode: "chunk", completeness_notice: "回答仅使用本次命中的有限原文。",
});

const doneJob = (jobId: string, createdAt: string, memoryId = ""): GlobalJob => ({
  job_id: jobId, conversation_id: "conv-a", status: "done", question: `问题 ${jobId}`,
  created_at: createdAt, notebook_scope: { mode: "all" }, resolved_notebook_ids: ["nb-0"],
  searched_notebook_ids: ["nb-0"], cited_notebook_ids: ["nb-0"], skipped_notebooks: [],
  error: null, mode: "chunk", response: null, answer: answer(`answer-${jobId}`, memoryId),
});

// 两条 done + 一条**未完成**。未完成那条没有可公开的回答，后端快照也不会包含它——
// `globalShareTurns` 必须把它滤掉，否则披露会凭空多报一轮。
const JOB_1 = doneJob("job-1", "2026-09-19T01:00:00Z");
const JOB_2 = doneJob("job-2", "2026-09-19T01:00:01Z", "m1");
const UNFINISHED: GlobalJob = { ...doneJob("job-3", "2026-09-19T01:00:02Z"), status: "failed", answer: null };

const shareApi = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), del: vi.fn() }));
/** 每次命中分享端点时记下的请求路径 —— 端点走的是全局那一组，不是 /notebooks/… */
let sharePaths: string[] = [];
let turnsByConversation: Record<string, GlobalJob[]> = {};

const SHARED = (throughId: string, at: string): ConversationShareResponse => ({
  share_token: "gshr-token", shared_through_at: at, shared_through_id: throughId,
});
const NOT_SHARED = () => humanizedError("not shared", 404);

const detailFor = (id: string): GlobalConversationDetail => ({
  ...conversation(id), turns: turnsByConversation[id] ?? [], has_more: false, next_offset: null,
});

beforeEach(() => {
  sharePaths = [];
  turnsByConversation = { "conv-a": [JOB_1, JOB_2, UNFINISHED] };
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  net.me.mockResolvedValue({ id: "user-1", ui_mode: "advanced" });
  net.notebooks.mockResolvedValue(notebooks);
  shareApi.get.mockRejectedValue(NOT_SHARED());
  shareApi.post.mockResolvedValue(SHARED("job-2", "2026-09-19T01:00:01Z"));
  shareApi.del.mockResolvedValue(undefined);

  net.requestJson.mockImplementation(async (path: string, options: { method?: string; body?: string } = { }) => {
    const method = (options.method || "GET").toUpperCase();
    if (path.endsWith("/share")) {
      sharePaths.push(path);
      if (method === "POST") {
        const body = JSON.parse(String(options.body || "{}"));
        return shareApi.post(body.expected_through_id);
      }
      return shareApi.get();
    }
    if (path.startsWith("/global-ask/conversations?")) return [conversation()];
    const detail = /^\/global-ask\/conversations\/([^/?]+)\?/.exec(path);
    if (detail) return detailFor(decodeURIComponent(detail[1]));
    throw new Error(`unexpected request: ${method} ${path}`);
  });
  net.requestVoid.mockImplementation(async (path: string, options: { method?: string } = {}) => {
    if (path.endsWith("/share") && (options.method || "").toUpperCase() === "DELETE") {
      sharePaths.push(path);
      return shareApi.del();
    }
    throw new Error(`unexpected request: ${options.method} ${path}`);
  });
});

function installDialogMethods() {
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  for (const method of ["show", "showModal"] as const) {
    Object.defineProperty(HTMLDialogElement.prototype, method, { configurable: true, value() { this.setAttribute("open", ""); } });
  }
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value() { this.removeAttribute("open"); } });
}

/** 打开 conv-a，点第 `index` 条回答（0 基）下面的「分享到这条回答」。 */
async function openShareOn(index: number) {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  const view = render(<GlobalAskPage />);
  const buttons = await screen.findAllByRole("button", { name: "分享到这条回答" });
  fireEvent.click(buttons[index]);
  await screen.findByText("分享会话");
  return view;
}


test("每条完成的回答下面都有分享入口，运行中的那条没有（它还没有可公开的回答）", async () => {
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  render(<GlobalAskPage />);
  await waitFor(() => expect(screen.getAllByRole("button", { name: "分享到这条回答" })).toHaveLength(2));
});


test("边界钉在这条**作业**上：POST 走全局端点，expected_through_id 是 job_id", async () => {
  await openShareOn(1);

  // 抬头按适配后的轮次说事：运行中那条不算轮次，所以是「第 2 轮 / 共 2 轮」。
  expect(screen.getByText(/分享至第 2 轮回答（本会话共 2 轮）/)).toBeInTheDocument();
  // 披露按截断批次算得出——第二轮引用的那条个人记忆被数出来（适配器确实把引用带过来了）。
  expect(screen.getByText(/公开页会包含 1 条你引用到的个人记忆摘录/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /分享到这一条/ }));
  await waitFor(() => expect(shareApi.post).toHaveBeenCalledWith("job-2"));
  // 端点是全局那一组，不是 /notebooks/{id}/conversations/{id}/share。
  expect(new Set(sharePaths)).toEqual(new Set(["/global-ask/conversations/conv-a/share"]));
  expect(await screen.findByText("已生成分享链接（到这一条为止）")).toBeInTheDocument();
  // 公开链接仍然是同一个 /c/{token} 页面。
  expect((screen.getByLabelText("分享链接") as HTMLInputElement).value).toContain("/c/gshr-token");
});


test("五态之 unshared：还没分享时，说的是「按下去会发布什么」", async () => {
  await openShareOn(0);
  expect(screen.getByText(/只包含这条回答以及它之前的问答/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /分享到这一条/ })).toBeInTheDocument();
});


test("五态之 at：链接正好停在这条作业上，如实说「就到这条回答为止」", async () => {
  shareApi.get.mockResolvedValue(SHARED("job-2", "2026-09-19T01:00:01Z"));
  await openShareOn(1);

  await screen.findByLabelText("分享链接");
  expect(screen.getByText(/链接的内容就到这条回答为止/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /更新到这一条/ })).toBeNull();
});


test("五态之 behind：水位停在更早那条作业，说清这条回答**还没**进链接", async () => {
  shareApi.get.mockResolvedValue(SHARED("job-1", "2026-09-19T01:00:00Z"));
  await openShareOn(1);

  await screen.findByLabelText("分享链接");
  // 复制按钮就在这句话下面：照着「只包含这条回答以及它之前的问答」复制，发出去的快照缺了它。
  expect(screen.queryByText(/只包含这条回答以及它之前的问答/)).toBeNull();
  expect(screen.getByText(/还不包含这条回答/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /更新到这一条/ }));
  await waitFor(() => expect(shareApi.post).toHaveBeenCalledWith("job-2"));
  expect(await screen.findByText("已更新到这一条")).toBeInTheDocument();
});


test("撤销分享：DELETE 走同一条全局端点，界面回到未分享", async () => {
  shareApi.get.mockResolvedValue(SHARED("job-2", "2026-09-19T01:00:01Z"));
  await openShareOn(1);

  fireEvent.click(await screen.findByRole("button", { name: /撤销分享/ }));
  await waitFor(() => expect(shareApi.del).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("已取消分享，原链接立即失效")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: /分享到这一条/ })).toBeInTheDocument();
  expect(new Set(sharePaths)).toEqual(new Set(["/global-ask/conversations/conv-a/share"]));
});


// 两种 409 的中文提示与单库**逐字一致**：全局侧复用的就是后端同一套 user_error 文案，
// 前端这一侧一个字都不该自己编。
for (const [label, message] of [
  ["零可分享回答", "这条会话还没有已完成的回答，暂时无法分享。"],
  ["水位过期", "这条会话已有变化，请刷新后重新分享。"],
] as const) {
  test(`分享失败 409（${label}）原样上屏`, async () => {
    shareApi.post.mockRejectedValue(humanizedError(message, 409));
    await openShareOn(1);

    fireEvent.click(screen.getByRole("button", { name: /分享到这一条/ }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    // 失败不留半个已分享态：链接框不出现，CTA 恢复可点。
    expect(screen.queryByLabelText("分享链接")).toBeNull();
    expect(screen.getByRole("button", { name: /分享到这一条/ })).not.toBeDisabled();
  });
}


test("切到别的会话后，上一条会话迟到的分享回执不写进新会话", async () => {
  let settle: (value: ConversationShareResponse) => void = () => {};
  shareApi.post.mockReturnValue(new Promise<ConversationShareResponse>((resolve) => { settle = resolve; }));
  turnsByConversation = { ...turnsByConversation, "conv-b": [] };
  await openShareOn(1);

  fireEvent.click(screen.getByRole("button", { name: /分享到这一条/ }));
  await waitFor(() => expect(shareApi.post).toHaveBeenCalledWith("job-2"));

  // 在途时切会话：弹窗按 conversationId 重挂（这里直接被收掉），aliveRef 因此为假。
  fireEvent.click((await screen.findAllByRole("button", { name: "新建对话" }))[0]);
  await waitFor(() => expect(screen.queryByText("分享会话")).toBeNull());

  settle(SHARED("job-2", "2026-09-19T01:00:01Z"));
  await Promise.resolve();
  // 上一条会话的分享态绝不该在新会话里冒出来。
  expect(screen.queryByText("已生成分享链接（到这一条为止）")).toBeNull();
  expect(screen.queryByLabelText("分享链接")).toBeNull();
});


function Launcher() {
  const presentation = useRootModalCoordinator({ actorId: "user-1", sourceId: null, onClosed() {} });
  return <GlobalAskLauncher presentation={presentation} />;
}

test("弹窗打开时按 Esc 只关弹窗，不关全局问答浮窗", async () => {
  installDialogMethods();
  window.history.replaceState(null, "", "/?conversation_id=host-state");
  render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "对话 conv-a" }));

  const shareButtons = await screen.findAllByRole("button", { name: "分享到这条回答" });
  fireEvent.click(shareButtons[1]);
  const heading = await screen.findByText("分享会话");

  // 弹窗必须渲染在 <dialog> 子树内：全屏形态 showModal() 把 dialog 提到 top layer，
  // 渲染在它之外的 fixed 元素会被整个盖住且不可交互。
  const windowDialog = screen.getByRole("dialog", { name: "全局问答" });
  expect(windowDialog.contains(heading)).toBe(true);

  // ⚠ 从 **dialog 子树内**的元素派发（真实按键时焦点就在弹窗里）。从 document.body
  // 派发的事件压根不经过 dialog，把 stopPropagation 删掉也不会红。
  fireEvent.keyDown(heading, { key: "Escape" });
  await waitFor(() => expect(screen.queryByText("分享会话")).toBeNull());
  // 一次 Esc 只收一层：浮窗还在，草稿框仍可用。
  expect(screen.getByRole("dialog", { name: "全局问答" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "输入问题" })).toBeTruthy();

  // 弹窗关掉之后，同一个 Esc 才轮到浮窗自己。
  fireEvent.keyDown(screen.getByRole("dialog", { name: "全局问答" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "全局问答" })).toBeNull());
});


test("收起浮窗会一并收掉分享弹窗，并把宿主页面的 Esc 还回去", async () => {
  installDialogMethods();
  window.history.replaceState(null, "", "/?conversation_id=host-state");
  render(<Launcher />);
  fireEvent.click(screen.getByRole("button", { name: "打开全局问答" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "对话 conv-a" }));
  fireEvent.click((await screen.findAllByRole("button", { name: "分享到这条回答" }))[1]);
  await screen.findByText("分享会话");

  // 浮窗收起后工作区仍然挂载：弹窗与它那个捕获期监听不跟着收，就会把宿主页面的
  // 下一次 Esc 整个吞掉。
  fireEvent.click(screen.getByRole("button", { name: "收起全局问答" }));
  await waitFor(() => expect(screen.queryByText("分享会话")).toBeNull());

  const pageEscape = vi.fn();
  window.addEventListener("keydown", pageEscape);
  try {
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(pageEscape).toHaveBeenCalledTimes(1);
  } finally {
    window.removeEventListener("keydown", pageEscape);
  }
});
