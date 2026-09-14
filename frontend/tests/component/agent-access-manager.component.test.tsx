import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestJson: vi.fn() }));

vi.mock("../../app/api-client.ts", () => ({ requestJson: mocks.requestJson }));

import { AgentAccessManager } from "../../app/agent-access-manager";
import { localDateTimeToUtcIso } from "../../app/agent-token-model";
import { humanizedError } from "../../app/errors";

const notebooks = [
  { id: "nb-1", name: "工艺笔记本", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "" },
  { id: "nb-2", name: "器件笔记本", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "" },
];

const activeToken = {
  id: "token-1",
  agent_profile_id: "agent-1",
  profile_name: "Claude Code",
  scopes: ["knowledge:read"],
  default_notebook_id: "nb-1",
  notebook_ids: ["nb-1"],
  expires_at: "2030-01-01T00:00:00Z",
  revoked_at: null,
  last_used_at: null,
  created_at: "2026-09-01T00:00:00Z",
};

const revokedToken = { ...activeToken, id: "token-2", profile_name: "Codex", revoked_at: "2026-09-02T00:00:00Z" };

type Route = (options: RequestInit & { signal?: AbortSignal }) => unknown;

function routeRequests(routes: Record<string, Route>) {
  mocks.requestJson.mockImplementation(async (path: string, options: RequestInit = {}) => {
    const method = options.method ?? "GET";
    const key = `${method} ${path.split("?")[0]}`;
    const route = routes[key];
    if (!route) throw new Error(`unexpected request ${key}`);
    return route(options);
  });
}

function baseRoutes(extra: Record<string, Route> = {}): Record<string, Route> {
  return {
    "GET /agent-profiles": () => [],
    "GET /agent-tokens": () => [activeToken, revokedToken],
    "GET /notebooks": () => notebooks,
    ...extra,
  };
}

function tokenRow(name: string): HTMLElement {
  const row = screen.getByText(name).closest("article");
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

beforeEach(() => {
  mocks.requestJson.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

test("已签发 token 可以回来修改权限,保存后行内确认并按整体替换提交", async () => {
  let resolveSave!: (value: unknown) => void;
  const saveBodies: unknown[] = [];
  routeRequests(baseRoutes({
    "PUT /agent-tokens/token-1/access": (options) => {
      saveBodies.push(JSON.parse(String(options.body)));
      return new Promise((resolve) => { resolveSave = resolve; });
    },
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  // 已撤销的 token 不给修改入口。
  expect(within(tokenRow("Codex")).queryByRole("button", { name: /修改权限/ })).not.toBeInTheDocument();

  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  const save = within(editor).getByRole("button", { name: "保存" });
  expect(within(editor).getByRole("checkbox", { name: "读取知识库" })).toBeChecked();
  // 没改任何东西时不能保存。
  expect(save).toBeDisabled();

  await user.click(within(editor).getByRole("checkbox", { name: "读取已确认记忆" }));
  await user.click(within(editor).getByRole("checkbox", { name: "器件笔记本" }));
  expect(save).toBeEnabled();
  await user.click(save);

  // 在途:按钮换文案并禁用,取消也点不动。
  expect(within(editor).getByRole("button", { name: "保存中…" })).toBeDisabled();
  expect(within(editor).getByRole("button", { name: "取消" })).toBeDisabled();
  expect(saveBodies).toHaveLength(1);
  const [body] = saveBodies as Array<Record<string, unknown>>;
  expect(body).toEqual({
    scopes: ["knowledge:read", "memory:read"],
    default_notebook_id: "nb-1",
    notebook_ids: ["nb-1", "nb-2"],
    expires_at: localDateTimeToUtcIso((within(editor).getByLabelText("过期时间") as HTMLInputElement).value),
  });
  expect(body.expires_at).toBe("2030-01-01T00:00:00.000Z");

  vi.useFakeTimers();
  await act(async () => {
    resolveSave({ ...activeToken, scopes: ["knowledge:read", "memory:read"], notebook_ids: ["nb-1", "nb-2"] });
  });

  const row = tokenRow("Claude Code");
  expect(screen.queryByRole("form", { name: "修改 Claude Code 的 token 权限" })).not.toBeInTheDocument();
  expect(within(row).getByRole("status")).toHaveTextContent("已保存，立即生效");
  expect(row).toHaveTextContent("读取知识库 · 读取已确认记忆");
  expect(row).toHaveTextContent("允许 2 个笔记本");

  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(within(row).queryByRole("status")).not.toBeInTheDocument();
});

test("保存失败时错误落在编辑行内,编辑内容保留以便重试", async () => {
  routeRequests(baseRoutes({
    "PUT /agent-tokens/token-1/access": () => {
      throw humanizedError("这个 Token 已撤销，不能再修改权限");
    },
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "执行笔记本问答" }));
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(editor).findByRole("alert")).toHaveTextContent("这个 Token 已撤销，不能再修改权限");
  expect(within(editor).getByRole("checkbox", { name: "执行笔记本问答" })).toBeChecked();
  expect(within(editor).getByRole("button", { name: "保存" })).toBeEnabled();
});

test("白名单里已无法访问的笔记本照样列出,可以取消勾选", async () => {
  routeRequests(baseRoutes({
    "GET /agent-tokens": () => [{ ...activeToken, notebook_ids: ["nb-1", "nb-gone"] }],
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  const gone = within(editor).getByRole("checkbox", { name: "无法访问的笔记本" });
  expect(gone).toBeChecked();

  await user.click(gone);
  await waitFor(() => expect(within(editor).getByRole("button", { name: "保存" })).toBeEnabled());
});
