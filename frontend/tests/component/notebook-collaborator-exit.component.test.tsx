import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { NotebookExitPanel } from "../../app/notebook-exit-panel.tsx";
import { NotebookMenuActions, ReaderNotebookBadge } from "../../app/notebook-reader-actions.tsx";
import { STILL_EDITOR_NOTE } from "../../app/notebook-exit-entries.ts";
import { anchorAt, useNotebookExit } from "../../app/use-notebook-exit.ts";
import type { NotebookSummary } from "../../app/workspace-model.ts";

// 分享编辑与协作 P1-T12(规格 §4.4「交叉态两个退出」、§9 徽章与 D17)。真实的组件 + 真实的
// 退出流程 hook + 同一个确认面板,只替换网络:每颗退出按钮打的是哪条出口、两步计划的顺序、
// 成功提示说了什么,都从真实请求上断言。

const ME = "u-me";

const base = {
  id: "nb1",
  name: "封装工艺库",
  purpose: "",
  primary_domain: "",
  status: "ready",
  counts: {},
  created_label: "2026年10月28日",
  access: "reader",
  shared_from: "carol",
} as NotebookSummary;

const editorOnly: NotebookSummary = {
  ...base, can_manage_content: true, can_manage_notebook: false, collaborator_role: "editor", collaborator_is_member: false,
};
const editorAndMember: NotebookSummary = { ...editorOnly, collaborator_is_member: true };
const namedViewer: NotebookSummary = { ...base, collaborator_role: "viewer", collaborator_is_member: false };
const namedViewerAndMember: NotebookSummary = { ...namedViewer, collaborator_is_member: true };

type Call = { method: string; path: string; search: string };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function installServer(counts: { membership?: number; collaborator?: number; collaboratorSuggestions?: number }, editors = [{ username: "lisi", display_name: "李四" }]) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const call = { method: (init?.method ?? "GET").toUpperCase(), path: url.pathname, search: url.search };
    calls.push(call);
    if (call.method === "GET" && call.path.endsWith("/membership/exit-disclosure")) {
      return json({ memory_count: counts.membership ?? 0 });
    }
    if (call.method === "GET" && call.path.endsWith(`/collaborators/${ME}/exit-disclosure`)) {
      return json({ memory_count: counts.collaborator ?? 0, suggestion_count: counts.collaboratorSuggestions ?? 0 });
    }
    if (call.method === "GET" && call.path.endsWith("/collaborators")) {
      return json({ view: "editor", items: [], editors });
    }
    if (call.method === "DELETE" && (call.path.endsWith("/membership") || call.path.endsWith(`/collaborators/${ME}`))) {
      const acknowledged = Number(new URLSearchParams(call.search).get("acknowledged_memory_count") ?? 0);
      if (call.path.endsWith(`/collaborators/${ME}`) && counts.collaboratorSuggestions) return json({ deleted_memory_count: acknowledged, deleted_suggestion_count: counts.collaboratorSuggestions });
      return acknowledged > 0 ? json({ deleted_memory_count: acknowledged }) : new Response(null, { status: 204 });
    }
    throw new Error(`unexpected request ${call.method} ${call.path}${call.search}`);
  }));
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function BarHost({ notebook, onToast }: { notebook: NotebookSummary; onToast: (text: string) => void }) {
  const exit = useNotebookExit({ onToast, onError: () => undefined });
  return (
    <>
      <ReaderNotebookBadge
        notebook={notebook}
        selfUserId={ME}
        leaveBusy={exit.isBusy(notebook.id)}
        leaveBusyLabel={exit.flow?.label ?? ""}
        onLeave={(anchor, plan) => exit.start(notebook.id, anchor, async () => undefined, plan)}
      />
      <NotebookExitPanel exit={exit} />
    </>
  );
}

function MenuHost({ notebook, onToast }: { notebook: NotebookSummary; onToast: (text: string) => void }) {
  const exit = useNotebookExit({ onToast, onError: () => undefined });
  return (
    <>
      <NotebookMenuActions
        notebook={notebook}
        selfUserId={ME}
        canManageNotebook={false}
        canDeleteNotebook={false}
        onLeave={(plan) => exit.start(notebook.id, anchorAt(40, 60), async () => undefined, plan)}
        onEdit={() => undefined}
        onDelete={() => undefined}
      />
      <NotebookExitPanel exit={exit} />
    </>
  );
}

const writes = (calls: Call[]) => calls.filter((call) => call.method === "DELETE").map((call) => `${call.path}${call.search}`);

test("退出点名编辑权也披露草稿建议，只有建议时仍经确认", async () => {
  const onToast = vi.fn();
  const calls = installServer({ collaborator: 0, collaboratorSuggestions: 3 });
  render(<BarHost notebook={editorOnly} onToast={onToast} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "退出协作" }));
  const dialog = await screen.findByRole("dialog", { name: "退出协作" });
  expect(dialog).toHaveTextContent("3 条未采纳的系统建议会被永久删除");
  expect(writes(calls)).toEqual([]);
  await user.click(within(dialog).getByRole("button", { name: "确认退出并删除" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出协作，已删除 3 条未采纳的系统建议"));
  expect(writes(calls)).toEqual([`/api/notebooks/nb1/collaborators/${ME}`]);
});

test("编辑者徽章写「可编辑 · 来自 库主」,点开列出其他编辑者(D17)", async () => {
  const user = userEvent.setup();
  installServer({}, [{ username: "lisi", display_name: "李四" }, { username: "wangwu", display_name: "" }]);
  render(<BarHost notebook={editorOnly} onToast={() => undefined} />);

  const chip = screen.getByRole("button", { name: "可编辑 · 来自 carol" });
  expect(chip).toHaveClass("reader-badge-chip");
  await user.click(chip);
  const roster = await screen.findByRole("dialog", { name: "这本笔记本的编辑者" });
  expect(await within(roster).findByText("李四（lisi）")).toBeInTheDocument();
  expect(within(roster).getByText("wangwu")).toBeInTheDocument();
});

test("编辑者没有管理权:传了改名承接方也只显示标题,不给输入框", () => {
  installServer({});
  render(
    <ReaderNotebookBadge
      notebook={editorOnly}
      selfUserId={ME}
      leaveBusy={false}
      onLeave={() => undefined}
      rename={{ value: "封装工艺库", saving: false, onChange: () => undefined, onCommit: () => undefined, onReset: () => undefined }}
    />,
  );
  expect(screen.queryByRole("textbox", { name: "笔记本名称" })).toBeNull();
  expect(screen.getByRole("heading", { name: "封装工艺库" })).toBeInTheDocument();
});

test("组管理员(管理权来自 admin 授权)徽章仍是「可管理」,改名输入框照给", () => {
  installServer({});
  render(
    <ReaderNotebookBadge
      notebook={{ ...base, can_manage_content: true, can_manage_notebook: true, granted_via: [{ group_id: "g1", group_name: "封装项目", kind: "project" }] }}
      leaveBusy={false}
      onLeave={() => undefined}
      rename={{ value: "封装工艺库", saving: false, onChange: () => undefined, onCommit: () => undefined, onReset: () => undefined }}
    />,
  );
  expect(screen.getByText(/^可管理 · /)).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "笔记本名称" })).toBeInTheDocument();
});

test("只有编辑者授权:一颗「退出协作」,打点名授权那条出口,不碰成员关系", async () => {
  const user = userEvent.setup();
  const calls = installServer({ collaborator: 0 });
  const onToast = vi.fn();
  render(<BarHost notebook={editorOnly} onToast={onToast} />);

  expect(screen.queryByRole("button", { name: "退出共享" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "退出协作" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出协作"));
  expect(writes(calls)).toEqual([`/api/notebooks/nb1/collaborators/${ME}`]);
  expect(calls.some((call) => call.path.includes("/membership"))).toBe(false);
});

test("编辑者兼成员:两颗按钮各删各的;「退出共享」只删成员关系并说明仍是编辑者", async () => {
  const user = userEvent.setup();
  const calls = installServer({ membership: 0, collaborator: 0 });
  const onToast = vi.fn();
  render(<BarHost notebook={editorAndMember} onToast={onToast} />);

  const keep = screen.getByRole("button", { name: "退出协作（保留只读）" });
  const share = screen.getByRole("button", { name: "退出共享" });
  expect(share).toHaveAttribute("title", STILL_EDITOR_NOTE);
  void keep;

  await user.click(share);
  await waitFor(() => expect(onToast).toHaveBeenCalledWith(`已退出共享；${STILL_EDITOR_NOTE}`));
  expect(writes(calls)).toEqual(["/api/notebooks/nb1/membership"]);

  await user.click(screen.getByRole("button", { name: "退出协作（保留只读）" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出协作；你仍可以阅读这本笔记本"));
  expect(writes(calls)).toEqual(["/api/notebooks/nb1/membership", `/api/notebooks/nb1/collaborators/${ME}`]);
});

test("卡片菜单里同一句「你仍是这本笔记本的编辑者」写成正文", () => {
  installServer({});
  render(<MenuHost notebook={editorAndMember} onToast={() => undefined} />);
  expect(screen.getByRole("button", { name: "退出协作（保留只读）" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "退出共享" })).toBeInTheDocument();
  expect(screen.getByText(STILL_EDITOR_NOTE)).toBeInTheDocument();
});

test("只持只读点名授权(没有成员行):「退出共享」指向点名授权,不打成员关系(否则是空操作假成功)", async () => {
  const user = userEvent.setup();
  const calls = installServer({ collaborator: 0 });
  const onToast = vi.fn();
  render(<MenuHost notebook={namedViewer} onToast={onToast} />);

  await user.click(screen.getByRole("button", { name: "退出共享" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(writes(calls)).toEqual([`/api/notebooks/nb1/collaborators/${ME}`]);
  expect(calls.some((call) => call.path.includes("/membership"))).toBe(false);
});

test("只读点名授权兼成员:「退出共享」先退成员关系、再退点名授权,最后一步照常告知与确认", async () => {
  const user = userEvent.setup();
  const calls = installServer({ membership: 0, collaborator: 2 });
  const onToast = vi.fn();
  render(<BarHost notebook={namedViewerAndMember} onToast={onToast} />);

  await user.click(screen.getByRole("button", { name: "退出共享" }));
  // 第一步(成员关系,告知 0)直接退;第二步(点名授权,告知 2)停在确认面板上。
  const confirm = await screen.findByRole("button", { name: "确认退出并删除" });
  expect(writes(calls)).toEqual(["/api/notebooks/nb1/membership"]);
  expect(onToast).not.toHaveBeenCalled();
  await user.click(confirm);
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享，已删除 2 条记忆"));
  expect(writes(calls)).toEqual([
    "/api/notebooks/nb1/membership",
    `/api/notebooks/nb1/collaborators/${ME}?acknowledged_memory_count=2`,
  ]);
});

test("只读成员(没有点名授权)与改动前逐字相同:一颗「退出共享」打成员关系", async () => {
  const user = userEvent.setup();
  const calls = installServer({ membership: 0 });
  const onToast = vi.fn();
  render(<BarHost notebook={base} onToast={onToast} />);
  await user.click(screen.getByRole("button", { name: "退出共享" }));
  await waitFor(() => expect(onToast).toHaveBeenCalledWith("已退出共享"));
  expect(writes(calls)).toEqual(["/api/notebooks/nb1/membership"]);
});

test("拿不到当前用户 id 时不给点名授权那几颗入口(不发一条打不中的 DELETE)", () => {
  installServer({});
  render(
    <ReaderNotebookBadge notebook={editorOnly} leaveBusy={false} onLeave={() => undefined} />,
  );
  expect(screen.queryByRole("button", { name: /退出/ })).toBeNull();
});
