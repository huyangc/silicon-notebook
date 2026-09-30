// 报告「分享」前的个人记忆披露确认(产品裁决 M4)。
//
// 端到端接的是**真的** `useReportWorkspace` + `ReportsPanel` + `report-api`,只把 `fetch`
// 换成按路径路由的假服务端——这样 409 正文的解析、403 的人话层、请求序列与请求体
// (`acknowledged_memory_count`)都是真代码跑出来的,而不是对着 mock 的 mock 断言。
//
// 覆盖:0 条不加步骤(请求与今天逐字节相同)、>0 就地展开确认条并把数字交回服务端、取消
// 不发请求、409 就地换成确数后再确认发新数字、403 就地显示那句原因且按钮回到待命、取数失败
// 走无确认值的 POST、在飞时的禁用态、成功文案落在按钮上并到点还原。
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef, useState } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { COPY_RESULT_HOLD_MS } from "../../app/copy-result";
import type { ReportDetailT } from "../../app/report-model";
import { ReportShareConfirm } from "../../app/report-share-confirm";
import { ReportsPanel } from "../../app/report-view";
import { useReportWorkspace } from "../../app/use-report-workspace";

const NB = "nb-1";
const NB2 = "nb-2";
const RID = "rep-1";
const RID2 = "rep-2";
const BASE = `/notebooks/${NB}/reports`;

const REPORT: ReportDetailT = {
  id: RID,
  question: "比较两类封装工艺",
  status: "done",
  progress: "",
  section_count: 1,
  created_at: "2026-08-01T00:00:00Z",
  created_by: "user-a",
  outline: [],
  sections: [],
  section_status: [],
  gaps: [],
  content_md: "# 报告正文",
  references: [],
  error: "",
  understanding: {},
};

type Handler = (init: RequestInit) => Response | Promise<Response>;
type Call = { method: string; path: string; body: string | null };

let calls: Call[] = [];
let handlers: { disclosure: Handler; share: Handler; shareRead: Handler };
const announceShareLink = vi.fn<(token: string) => Promise<boolean>>();
const notify = vi.fn();

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

function fakeFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const url = new URL(String(input), "http://localhost");
  const path = url.pathname.replace(/^\/api(?=\/)/, "");
  const method = (init.method || "GET").toUpperCase();
  calls.push({ method, path, body: typeof init.body === "string" ? init.body : null });
  if (method === "GET" && path === BASE) return Promise.resolve(json(200, []));
  if (method === "GET" && path === `/notebooks/${NB2}/reports`) return Promise.resolve(json(200, []));
  if (method === "GET" && path === `${BASE}/${RID}`) return Promise.resolve(json(200, REPORT));
  if (method === "GET" && path === `${BASE}/${RID2}`) {
    return Promise.resolve(json(200, { ...REPORT, id: RID2, question: "另一份报告" }));
  }
  if (method === "GET" && path === `${BASE}/${RID}/share`) {
    return Promise.resolve(handlers.shareRead(init));
  }
  if (method === "GET" && path === `${BASE}/${RID}/share/disclosure`) {
    return Promise.resolve(handlers.disclosure(init));
  }
  if (method === "POST" && path === `${BASE}/${RID}/share`) {
    return Promise.resolve(handlers.share(init));
  }
  if (method === "DELETE" && path === `${BASE}/${RID}/share`) {
    return Promise.resolve(new Response(null, { status: 204 }));
  }
  return Promise.resolve(json(404, { detail: "not found" }));
}

const shareCalls = () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/share"));
const disclosureCalls = () => calls.filter((c) => c.path.endsWith("/share/disclosure"));

function Harness() {
  const [notebookId, setNotebookId] = useState(NB);
  const workspace = useReportWorkspace({
    actorId: "user-a",
    notebookId,
    active: true,
    policy: {
      advanced: true,
      canManageReports: true,
      creationDisabled: false,
      sourceScope: { mode: "include", source_ids: [] },
      baseScope: { mode: "include", notebook_ids: [] },
    },
    effects: {
      notify,
      downloadMarkdown: vi.fn(),
      downloadArchive: vi.fn(),
      announceShareLink,
    },
  });
  const { focusReport } = workspace;
  useEffect(() => { focusReport(RID); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <button type="button" onClick={() => focusReport(RID2)}>打开另一份报告</button>
      <button type="button" onClick={() => focusReport(RID)}>打开原报告</button>
      <button type="button" onClick={() => setNotebookId((id) => (id === NB ? NB2 : NB))}>换笔记本</button>
      <ReportsPanel notebookId={notebookId} workspace={workspace} setToast={vi.fn()} />
    </>
  );
}

async function openReport() {
  render(<Harness />);
  // 详情读回来之后,「分享」才会出现。
  return screen.findByRole("button", { name: "分享" });
}

const disclosureSentence = (n: number) => `公开页可能包含来自 ${n} 条个人记忆的内容。`;
const changedSentence = (n: number, added: number) =>
  added > 0
    ? `条数有变化（新增 ${added} 条）：${disclosureSentence(n)}`
    : `条数有变化：${disclosureSentence(n)}`;
const strip = () => screen.queryByRole("group", { name: "公开前确认" });

beforeEach(() => {
  calls = [];
  announceShareLink.mockReset();
  announceShareLink.mockResolvedValue(true);
  notify.mockReset();
  handlers = {
    disclosure: () => json(200, { memory_count: 0 }),
    share: () => json(200, { share_token: "tok-1" }),
    shareRead: () => json(404, { detail: "not shared" }),
  };
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "debug").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("0 条:不出确认条,先取披露再发与今天逐字节相同的 POST(无请求体)", async () => {
  const user = userEvent.setup();
  const button = await openReport();

  await user.click(button);

  await waitFor(() => expect(announceShareLink).toHaveBeenCalledWith("tok-1"));
  expect(strip()).toBeNull();
  const relevant = calls.filter((c) => c.path.includes("/share"));
  expect(relevant).toEqual([
    { method: "GET", path: `${BASE}/${RID}/share/disclosure`, body: null },
    { method: "POST", path: `${BASE}/${RID}/share`, body: null },
  ]);
});

test("成功:结果落在按钮自己身上(已公开，链接已复制),停留一阵后回到「取消分享」", async () => {
  const user = userEvent.setup();
  let finishCopy!: (copied: boolean) => void;
  announceShareLink.mockImplementation(() => new Promise<boolean>((resolve) => { finishCopy = resolve; }));
  const button = await openReport();

  await user.click(button);
  await waitFor(() => expect(announceShareLink).toHaveBeenCalledTimes(1));
  // 复制还没完成:请求已成功,按钮是「取消分享」,且还在忙。
  vi.useFakeTimers();
  await act(async () => { finishCopy(true); });

  const landed = screen.getByRole("button", { name: "已公开，链接已复制" });
  expect(landed).toHaveClass("copy-result-copied");
  await act(async () => { vi.advanceTimersByTime(COPY_RESULT_HOLD_MS - 1); });
  expect(screen.getByRole("button", { name: "已公开，链接已复制" })).toBeInTheDocument();
  await act(async () => { vi.advanceTimersByTime(2); });
  const idle = screen.getByRole("button", { name: "取消分享" });
  expect(idle).not.toHaveClass("copy-result-copied");
  expect(screen.queryByRole("button", { name: "已公开，链接已复制" })).toBeNull();
});

test("公开成功后在停留期内取消分享:旧结果立刻清掉,按钮回到「分享」,不带到下一条链接上", async () => {
  const user = userEvent.setup();
  await user.click(await openReport());
  await user.click(await screen.findByRole("button", { name: "已公开，链接已复制" }));

  const back = await screen.findByRole("button", { name: "分享" });
  expect(back).not.toHaveClass("copy-result-copied");
  expect(screen.queryByRole("button", { name: "已公开，链接已复制" })).toBeNull();
  expect(calls.some((c) => c.method === "DELETE" && c.path === `${BASE}/${RID}/share`)).toBe(true);
});

test("成功但没进剪贴板:按钮上画失败态,链接仍由「复制链接」取回", async () => {
  const user = userEvent.setup();
  announceShareLink.mockResolvedValue(false);
  await user.click(await openReport());

  const failed = await screen.findByRole("button", { name: "已公开，复制失败" });
  expect(failed).toHaveClass("copy-result-failed");
  expect(screen.getByRole("button", { name: "复制链接" })).toBeInTheDocument();
});

test("N>0:确认条就地展开,显示数字与两个按钮;此时还没有 POST", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 3 });
  await user.click(await openReport());

  const group = await screen.findByRole("group", { name: "公开前确认" });
  expect(within(group).getByText(disclosureSentence(3))).toBeInTheDocument();
  expect(within(group).getByRole("button", { name: "确认公开" })).toBeEnabled();
  expect(within(group).getByRole("button", { name: "取消" })).toBeEnabled();
  // 「分享」按钮回到待命,确认条不是页顶横幅——它和按钮在同一个报告卡里。
  expect(screen.getByRole("button", { name: "分享" })).toBeEnabled();
  expect(shareCalls()).toHaveLength(0);
  expect(announceShareLink).not.toHaveBeenCalled();
});

test("N>0 确认:POST 带 acknowledged_memory_count,成功后确认条收起、结果落在按钮上", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 3 });
  await user.click(await openReport());

  await user.click(await screen.findByRole("button", { name: "确认公开" }));

  await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(strip()).toBeNull();
  expect(shareCalls()).toEqual([
    { method: "POST", path: `${BASE}/${RID}/share`, body: JSON.stringify({ acknowledged_memory_count: 3 }) },
  ]);
  expect(announceShareLink).toHaveBeenCalledWith("tok-1");
});

test("取消:确认条收起,什么请求都不再发,「分享」仍可再点", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 2 });
  await user.click(await openReport());
  await screen.findByRole("group", { name: "公开前确认" });

  await user.click(screen.getByRole("button", { name: "取消" }));

  await waitFor(() => expect(strip()).toBeNull());
  expect(shareCalls()).toHaveLength(0);
  expect(screen.getByRole("button", { name: "分享" })).toBeEnabled();

  await user.click(screen.getByRole("button", { name: "分享" }));
  await screen.findByRole("group", { name: "公开前确认" });
  expect(disclosureCalls()).toHaveLength(2);
  expect(shareCalls()).toHaveLength(0);
});

test("409:确认条就地换成服务端确数,再确认发的是新数字", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 3 });
  let attempt = 0;
  handlers.share = () => {
    attempt += 1;
    return attempt === 1
      ? json(409, { detail: { code: "share_disclosure_required", memory_count: 5, new_memory_count: 2 } })
      : json(200, { share_token: "tok-2" });
  };
  await user.click(await openReport());

  await user.click(await screen.findByRole("button", { name: "确认公开" }));

  // 条还在、数字变了且说明「条数有变化」、没有横幅式的新元素;按钮回到可点。
  expect(await screen.findByText(changedSentence(5, 2))).toBeInTheDocument();
  expect(screen.queryByText(disclosureSentence(3))).toBeNull();
  expect(strip()).not.toBeNull();
  expect(announceShareLink).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "确认公开" })).toBeEnabled();

  await user.click(screen.getByRole("button", { name: "确认公开" }));
  await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(shareCalls().map((c) => c.body)).toEqual([
    JSON.stringify({ acknowledged_memory_count: 3 }),
    JSON.stringify({ acknowledged_memory_count: 5 }),
  ]);
  expect(announceShareLink).toHaveBeenCalledWith("tok-2");
});

test("409 的确数也认根层同形状(不依赖 detail 包裹)", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 1 });
  handlers.share = () => json(409, { code: "share_disclosure_required", memory_count: 4, new_memory_count: 0 });
  await user.click(await openReport());

  await user.click(await screen.findByRole("button", { name: "确认公开" }));

  expect(await screen.findByText(changedSentence(4, 0))).toBeInTheDocument();
});

test("别的 409(不是披露要求)走通用错误:toast 报错,确认条不被改写", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 3 });
  handlers.share = () => json(409, { detail: "conflict" });
  await user.click(await openReport());

  await user.click(await screen.findByRole("button", { name: "确认公开" }));

  await waitFor(() => expect(notify).toHaveBeenCalled());
  expect(screen.getByText(disclosureSentence(3))).toBeInTheDocument();
});

test("403:确认条就地显示服务端那句原因,不再给「确认公开」,「分享」回到待命", async () => {
  const user = userEvent.setup();
  const sentence = "报告引用了作者本人的个人记忆，只有作者可以公开分享";
  handlers.disclosure = () => json(200, { memory_count: 2 });
  handlers.share = () => json(403, { detail: sentence }, { "X-User-Message": "1" });
  await user.click(await openReport());

  await user.click(await screen.findByRole("button", { name: "确认公开" }));

  const group = await screen.findByRole("group", { name: "公开前确认" });
  await waitFor(() => expect(within(group).getByText(sentence)).toBeInTheDocument());
  expect(within(group).queryByText(disclosureSentence(2))).toBeNull();
  expect(within(group).queryByRole("button", { name: "确认公开" })).toBeNull();
  expect(within(group).getByRole("button", { name: "取消" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "分享" })).toBeEnabled();
  expect(announceShareLink).not.toHaveBeenCalled();

  await user.click(within(group).getByRole("button", { name: "取消" }));
  await waitFor(() => expect(strip()).toBeNull());
});

test("取披露失败:POST 不带确认值;0 条的报告照常公开", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(500, { detail: "boom" });
  await user.click(await openReport());

  await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(strip()).toBeNull();
  expect(shareCalls()).toEqual([{ method: "POST", path: `${BASE}/${RID}/share`, body: null }]);
});

test("取披露失败 + 服务端 409:确认条用确数就地出现,确认后发这个数", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(500, { detail: "boom" });
  let attempt = 0;
  handlers.share = () => {
    attempt += 1;
    return attempt === 1
      ? json(409, { detail: { code: "share_disclosure_required", memory_count: 6, new_memory_count: 6 } })
      : json(200, { share_token: "tok-3" });
  };
  await user.click(await openReport());

  expect(await screen.findByText(disclosureSentence(6))).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "确认公开" }));

  await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(shareCalls().map((c) => c.body)).toEqual([
    null,
    JSON.stringify({ acknowledged_memory_count: 6 }),
  ]);
});

test("取披露失败 + 403:确认条只显示那句原因(没有可确认的数字)", async () => {
  const user = userEvent.setup();
  const sentence = "报告引用了作者本人的个人记忆，只有作者可以公开分享";
  handlers.disclosure = () => json(500, { detail: "boom" });
  handlers.share = () => json(403, { detail: sentence }, { "X-User-Message": "1" });
  await user.click(await openReport());

  expect(await screen.findByText(sentence)).toBeInTheDocument();
  const group = screen.getByRole("group", { name: "公开前确认" });
  expect(within(group).queryByRole("button", { name: "确认公开" })).toBeNull();
  expect(screen.getByRole("button", { name: "分享" })).toBeEnabled();
});

test("引用了其他成员的个人记忆:取披露就知道,不发 POST,确认条就地说明不能公开、只留「取消」", async () => {
  const user = userEvent.setup();
  const sentence = "报告引用了其他成员的个人记忆，不能公开";
  handlers.disclosure = () => json(200, { memory_count: 2, foreign_memory_count: 1 });
  await user.click(await openReport());

  const group = await screen.findByRole("group", { name: "公开前确认" });
  expect(await within(group).findByText(sentence)).toBeInTheDocument();
  expect(within(group).queryByText(disclosureSentence(2))).toBeNull();
  expect(within(group).queryByRole("button", { name: "确认公开" })).toBeNull();
  expect(shareCalls()).toHaveLength(0);
  expect(screen.getByRole("button", { name: "分享" })).toBeEnabled();
  expect(within(group).getByRole("button", { name: "取消" })).toHaveFocus();
});

test("服务端 403(引用了其他成员的个人记忆)时那句原因同样就地显示,焦点落到「取消」", async () => {
  const user = userEvent.setup();
  const sentence = "报告引用了其他成员的个人记忆，不能公开";
  handlers.disclosure = () => json(200, { memory_count: 1 });
  handlers.share = () => json(403, { detail: sentence }, { "X-User-Message": "1" });
  await user.click(await openReport());

  await user.click(await screen.findByRole("button", { name: "确认公开" }));

  const group = screen.getByRole("group", { name: "公开前确认" });
  await waitFor(() => expect(within(group).getByText(sentence)).toBeInTheDocument());
  await waitFor(() => expect(within(group).getByRole("button", { name: "取消" })).toHaveFocus());
});

test("键盘:确认条出现时焦点进入条里(落在「取消」),Esc 收起且焦点回到「分享」,不发请求", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 2 });
  const button = await openReport();
  await user.click(button);

  const group = await screen.findByRole("group", { name: "公开前确认" });
  await waitFor(() => expect(within(group).getByRole("button", { name: "取消" })).toHaveFocus());

  await user.keyboard("{Escape}");

  await waitFor(() => expect(strip()).toBeNull());
  expect(screen.getByRole("button", { name: "分享" })).toHaveFocus();
  expect(shareCalls()).toHaveLength(0);
});

test("键盘:点「取消」收起后焦点回到「分享」;公开成功后焦点也回到带结果的「分享」按钮上", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 1 });
  await user.click(await openReport());
  await user.click(await screen.findByRole("button", { name: "取消" }));
  await waitFor(() => expect(strip()).toBeNull());
  expect(screen.getByRole("button", { name: "分享" })).toHaveFocus();

  await user.click(screen.getByRole("button", { name: "分享" }));
  await user.click(await screen.findByRole("button", { name: "确认公开" }));
  const landed = await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(strip()).toBeNull();
  expect(landed).toHaveFocus();
});

test("在飞时 Esc 不收起确认条(结果马上落地)", async () => {
  const user = userEvent.setup();
  let releaseShare!: () => void;
  const shareGate = new Promise<void>((resolve) => { releaseShare = resolve; });
  const routed = vi.fn(fakeFetch);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method || "GET").toUpperCase() === "POST" && String(input).endsWith("/share")) {
      await shareGate;
    }
    return routed(input, init);
  }));
  handlers.disclosure = () => json(200, { memory_count: 2 });
  await user.click(await openReport());
  const group = await screen.findByRole("group", { name: "公开前确认" });
  await user.click(within(group).getByRole("button", { name: "确认公开" }));
  await screen.findByRole("button", { name: "生成链接中…" });

  // 焦点在条里的禁用按钮上时按 Esc。
  within(group).getByRole("button", { name: "取消" }).focus();
  await user.keyboard("{Escape}");
  expect(strip()).not.toBeNull();

  await act(async () => { releaseShare(); });
  await screen.findByRole("button", { name: "已公开，链接已复制" });
});

test("在飞:取披露与公开期间「分享」读「生成链接中…」且禁用,确认条两个按钮同样禁用", async () => {
  const user = userEvent.setup();
  let releaseDisclosure!: () => void;
  const disclosureGate = new Promise<void>((resolve) => { releaseDisclosure = resolve; });
  let releaseShare!: () => void;
  const shareGate = new Promise<void>((resolve) => { releaseShare = resolve; });
  // fetch 层的延迟:让两个请求都停在半空。
  const routed = vi.fn(fakeFetch);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/share/disclosure")) await disclosureGate;
    if ((init?.method || "GET").toUpperCase() === "POST" && path.endsWith("/share")) await shareGate;
    return routed(input, init);
  }));
  handlers.disclosure = () => json(200, { memory_count: 2 });
  await user.click(await openReport());

  // 取披露在飞。
  const pending = await screen.findByRole("button", { name: "生成链接中…" });
  expect(pending).toBeDisabled();
  await act(async () => { releaseDisclosure(); });

  await user.click(await screen.findByRole("button", { name: "确认公开" }));
  // POST 在飞。
  const busyShare = await screen.findByRole("button", { name: "生成链接中…" });
  expect(busyShare).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认公开" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();

  await act(async () => { releaseShare(); });
  await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(strip()).toBeNull();
});

test("换一份报告:上一份的确认条收起,不挂到这一份上;回到原报告也不再出现", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 2 });
  await user.click(await openReport());
  await screen.findByRole("group", { name: "公开前确认" });

  await user.click(screen.getByRole("button", { name: "打开另一份报告" }));
  await screen.findByRole("heading", { name: "另一份报告" });
  expect(strip()).toBeNull();

  await user.click(screen.getByRole("button", { name: "打开原报告" }));
  await screen.findByRole("heading", { name: "比较两类封装工艺" });
  expect(strip()).toBeNull();
  expect(shareCalls()).toHaveLength(0);
});

test("换笔记本:确认条随报告一起收起,换回来重新打开也不再出现", async () => {
  const user = userEvent.setup();
  handlers.disclosure = () => json(200, { memory_count: 2 });
  await user.click(await openReport());
  await screen.findByRole("group", { name: "公开前确认" });

  await user.click(screen.getByRole("button", { name: "换笔记本" }));
  await waitFor(() => expect(strip()).toBeNull());
  await user.click(screen.getByRole("button", { name: "换笔记本" }));
  await user.click(screen.getByRole("button", { name: "打开原报告" }));
  await screen.findByRole("heading", { name: "比较两类封装工艺" });
  expect(strip()).toBeNull();
  expect(shareCalls()).toHaveLength(0);
});

test("POST 中途断网但服务端已公开:重读分享状态,按公开成功报告在按钮上", async () => {
  const user = userEvent.setup();
  handlers.share = () => { throw new TypeError("Failed to fetch"); };
  handlers.shareRead = () => json(200, { share_token: "tok-9" });
  await user.click(await openReport());

  await screen.findByRole("button", { name: "已公开，链接已复制" });
  expect(announceShareLink).toHaveBeenCalledWith("tok-9");
  expect(notify).not.toHaveBeenCalled();
});

test("POST 中途断网且没有公开:如实报失败,按钮回到「分享」", async () => {
  const user = userEvent.setup();
  handlers.share = () => { throw new TypeError("Failed to fetch"); };
  await user.click(await openReport());

  await waitFor(() => expect(notify).toHaveBeenCalled());
  expect(screen.getByRole("button", { name: "分享" })).toBeEnabled();
  expect(announceShareLink).not.toHaveBeenCalled();
  expect(calls.some((c) => c.method === "GET" && c.path === `${BASE}/${RID}/share`)).toBe(true);
});

test("状态区先空着挂上,句子在挂载之后才填入(读屏会播报)", () => {
  function Probe() {
    const ref = useRef<HTMLButtonElement | null>(null);
    return (
      <ReportShareConfirm
        count={3}
        added={null}
        refusal={null}
        busy={false}
        returnFocusRef={ref}
        onReturnFocus={() => {}}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    );
  }
  const firstPaint = renderToStaticMarkup(<Probe />);
  expect(firstPaint).toContain('role="status"></span>');
  expect(firstPaint).not.toContain(disclosureSentence(3));
  render(<Probe />);
  expect(screen.getByRole("status")).toHaveTextContent(disclosureSentence(3));
});
