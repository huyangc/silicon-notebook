// 来源栏「本库来源」那一段从 page.tsx 抽出来之后的组件覆盖（PR-5 分片 2）。page.tsx
// 整体不可直接渲染，这些呈现判据以前只能靠源码守卫近似钉住；现在可以真渲染了。
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { SourceListPanel } from "../../app/source-list-panel";
import { defaultSourceScopeSelection } from "../../app/source-scope";
import type { SourceSummary } from "../../app/workspace-model";

function source(id: string, overrides: Partial<SourceSummary> = {}): SourceSummary {
  // 用类型标注而不是 `as SourceSummary`：后者是断言，SourceSummary 新增必填字段时
  // 这个字面量仍会被强行放行，测试无声地拿着不完整夹具跑。标注是赋值检查，漏字段
  // 会在 tsc 就地报错。
  const base: SourceSummary = {
    id,
    notebook_id: "nb-1",
    title: id,
    display_title: id,
    type: "file",
    status: "extracted",
    parse_status: "extracted",
    summary: "",
    element_count: 0,
    file_name: `${id}.pdf`,
    file_size: 1,
    created_at: "2026-09-01T00:00:00Z",
    created_label: "9月1日",
  };
  return { ...base, ...overrides };
}

const noop = () => undefined;

function renderPanel(overrides: Partial<Parameters<typeof SourceListPanel>[0]> = {}) {
  const props = {
    sources: [] as SourceSummary[],
    uiMode: "advanced" as const,
    notebookId: "nb-1",
    sourceQuery: "",
    sourcesPage: 0,
    sourcesTotal: 0,
    sourcesPageLoading: false,
    sourceScopeSelection: defaultSourceScopeSelection(),
    deletingSourceIds: new Set<string>(),
    askInFlight: false,
    readOnlyWorkspace: false,
    kgReady: false,
    onQueryChange: noop,
    onSubmitSearch: noop,
    onToggleSource: noop,
    onOpenSource: noop,
    onDeleteSource: noop,
    onPage: noop,
    ...overrides,
  };
  return { props, ...render(<SourceListPanel {...props} />) };
}

test("空库给出引导文案而不是一片空白", () => {
  renderPanel();

  expect(screen.getByText("已保存的来源将显示在此处")).toBeInTheDocument();
  // 单页时 Pagination 自己返回 null，不该出现翻页控件。
  expect(screen.queryByRole("button", { name: "下一页" })).not.toBeInTheDocument();
});

test("来源逐行渲染：标题去扩展名、勾选框可切换、打开与删除把 source 交回调用方", async () => {
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  const onDeleteSource = vi.fn();
  const onToggleSource = vi.fn();
  const rows = [source("alpha"), source("beta")];
  renderPanel({ sources: rows, onOpenSource, onDeleteSource, onToggleSource });

  // compactSourceTitle 去掉 .pdf 后缀。
  expect(screen.getByText("alpha")).toBeInTheDocument();
  expect(screen.getByText("beta")).toBeInTheDocument();

  await user.click(screen.getByRole("checkbox", { name: "检索来源：alpha" }));
  expect(onToggleSource).toHaveBeenCalledWith("alpha");

  await user.click(screen.getByText("alpha"));
  expect(onOpenSource).toHaveBeenCalledWith(rows[0]);

  await user.click(screen.getByRole("button", { name: "删除来源：beta" }));
  expect(onDeleteSource).toHaveBeenCalledWith(rows[1]);
});

test("提交搜索调注入命令；在途期间按钮变「搜索中…」并禁用", async () => {
  const user = userEvent.setup();
  const onSubmitSearch = vi.fn();
  const { unmount } = renderPanel({ sourceQuery: "梯度", onSubmitSearch });

  await user.click(screen.getByRole("button", { name: "搜索" }));
  expect(onSubmitSearch).toHaveBeenCalledTimes(1);
  unmount();

  renderPanel({ sourceQuery: "梯度", sourcesPageLoading: true, onSubmitSearch });
  const busyButton = screen.getByRole("button", { name: "搜索中…" });
  expect(busyButton).toBeDisabled();
});

test("在途期间分页控件整体禁用", () => {
  const { unmount } = renderPanel({ sources: [source("alpha")], sourcesTotal: 120, sourcesPage: 1 });
  expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
  unmount();

  renderPanel({
    sources: [source("alpha")],
    sourcesTotal: 120,
    sourcesPage: 1,
    sourcesPageLoading: true,
  });
  expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("KG 徽章只在图谱就绪时出现，解析状态与 Agent 来源徽章同行呈现", () => {
  const analyzed = source("alpha", { kg_extracted: true, agent_created: true, parse_status: "failed" });
  const { unmount, container } = renderPanel({ sources: [analyzed], kgReady: false });
  expect(screen.queryByText("已分析")).not.toBeInTheDocument();
  // 解析状态点跟着 parse_status 走，与 KG 徽章无关。
  expect(container.querySelector(".source-status-dot.status-failed")).not.toBeNull();
  unmount();

  renderPanel({ sources: [analyzed], kgReady: true });
  const row = screen.getByTitle("alpha");
  expect(within(row).getByText("已分析")).toBeInTheDocument();
  expect(within(row).getByText("Agent 添加")).toBeInTheDocument();
});

test("只读工作区不给删除入口；自动模式不给检索勾选框", () => {
  const { unmount } = renderPanel({ sources: [source("alpha")], readOnlyWorkspace: true });
  expect(screen.queryByRole("button", { name: "删除来源：alpha" })).not.toBeInTheDocument();
  unmount();

  renderPanel({ sources: [source("alpha")], uiMode: "auto" });
  expect(screen.queryByRole("checkbox", { name: "检索来源：alpha" })).not.toBeInTheDocument();
});

test("正在删除的行标 aria-busy、禁用删除按钮、外链变不可达", () => {
  renderPanel({
    sources: [source("alpha", { source_url: "https://example.com/alpha" })],
    deletingSourceIds: new Set(["alpha"]),
  });

  const row = screen.getByTitle("alpha");
  expect(row).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("button", { name: "正在删除来源：alpha" })).toBeDisabled();

  const link = within(row).getByRole("link", { name: "打开原始链接" });
  expect(link).toHaveAttribute("tabIndex", "-1");
  expect(link).toHaveAttribute("aria-disabled", "true");
});

test("问答在途时检索勾选框整体禁用", () => {
  renderPanel({ sources: [source("alpha")], askInFlight: true });
  expect(screen.getByRole("checkbox", { name: "检索来源：alpha" })).toBeDisabled();
});
