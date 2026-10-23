// 深度报告输入框因检索范围为空而锁住时，输入框旁那句：出路就是可点的字（「全选」/「添加
// 来源」/「挂载参考库」），当前用户做不了的那一半不说；「全选」让这句消失后焦点交给输入框。
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../../app/waiting-wish-carousel", () => ({
  WaitingWishCarousel: () => <div aria-label="测试许愿轮播" />,
}));

import { ReportsPanel } from "../../app/report-view";
import { ReportScopeHint, type ReportScopeHintProps } from "../../app/report-scope-hint";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(cleanup);

function hintProps(overrides: Partial<ReportScopeHintProps> = {}): ReportScopeHintProps {
  return {
    advanced: true,
    canSelectAll: true,
    canAddSources: true,
    canMountBases: true,
    onSelectAll: vi.fn(),
    onAddSources: vi.fn(),
    onMountBases: vi.fn(),
    ...overrides,
  };
}

test("高级模式范围被清空：句子里的「全选」就是来源栏那个全选", async () => {
  const props = hintProps();
  const { container } = render(<p><ReportScopeHint {...props} /></p>);
  expect(container).toHaveTextContent("当前检索范围为空，全选来源与参考库，或在来源栏勾选要用的来源，再开始生成报告");
  const action = screen.getByRole("button", { name: "全选" });
  expect(action).toHaveClass("link-button", "inline-action");
  await userEvent.setup().click(action);
  expect(props.onSelectAll).toHaveBeenCalledOnce();
});

test("没有可检索的内容：「添加来源」与「挂载参考库」各是入口", async () => {
  const user = userEvent.setup();
  const props = hintProps({ canSelectAll: false });
  const { container } = render(<p><ReportScopeHint {...props} /></p>);
  expect(container).toHaveTextContent("请先添加来源或挂载参考库，再开始生成报告");
  expect(screen.queryByRole("button", { name: "全选" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "添加来源" }));
  await user.click(screen.getByRole("button", { name: "挂载参考库" }));
  expect(props.onAddSources).toHaveBeenCalledOnce();
  expect(props.onMountBases).toHaveBeenCalledOnce();
});

test("自动模式不说全选（范围固定全选，锁住只能是没有内容）", () => {
  const { container } = render(<p><ReportScopeHint {...hintProps({ advanced: false })} /></p>);
  expect(screen.queryByRole("button", { name: "全选" })).toBeNull();
  expect(container).toHaveTextContent("请先添加来源或挂载参考库，再开始生成报告");
});

test("做不了的那一半不说", () => {
  const { container, unmount } = render(
    <p><ReportScopeHint {...hintProps({ canSelectAll: false, canMountBases: false })} /></p>,
  );
  expect(container).toHaveTextContent("请先添加来源，再开始生成报告");
  expect(screen.queryByRole("button", { name: "挂载参考库" })).toBeNull();
  unmount();

  const readOnly = render(
    <p><ReportScopeHint {...hintProps({ canSelectAll: false, canAddSources: false, canMountBases: false })} /></p>,
  );
  expect(readOnly.container).toHaveTextContent("由库主或可编辑的成员添加之后就能生成报告");
  expect(screen.queryByRole("button")).toBeNull();
  readOnly.unmount();

  const mirror = render(
    <p>
      <ReportScopeHint
        {...hintProps({ canSelectAll: false, canAddSources: false, canMountBases: false, mirrored: true })}
      />
    </p>,
  );
  expect(mirror.container).toHaveTextContent("内容随源环境同步过来之后就能生成报告");
});

test("ReportsPanel：锁住时出路在输入框旁，「全选」解锁后焦点交给输入框", async () => {
  const onSelectAll = vi.fn();
  const workspace = reportWorkspaceFixture();
  const panel = (creationDisabled: boolean) => (
    <ReportsPanel
      notebookId="nb-1"
      workspace={workspace}
      setToast={vi.fn()}
      creationDisabled={creationDisabled}
      creationDisabledReason="当前检索范围为空，请至少选择一个来源或参考库"
      creationBlockedHint={hintProps({ onSelectAll })}
    />
  );
  const { rerender } = render(panel(true));
  const input = screen.getByRole("textbox");
  expect(input).toBeDisabled();
  expect(input).toHaveAttribute("placeholder", "当前检索范围为空，请至少选择一个来源或参考库");

  // 这句带着出路，换行显示全句，不被省略号截掉。
  expect(screen.getByRole("button", { name: "全选" }).closest(".report-compose-hint")).toHaveClass("scope-blocked");
  await userEvent.setup().click(screen.getByRole("button", { name: "全选" }));
  expect(onSelectAll).toHaveBeenCalledOnce();

  rerender(panel(false));
  expect(screen.queryByRole("button", { name: "全选" })).toBeNull();
  expect(screen.getByRole("textbox")).toBeEnabled();
  expect(screen.getByRole("textbox")).toHaveFocus();
});

test("ReportsPanel：范围本来就在、或宿主没给出路时，那一格照旧是平常的说明", () => {
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture()}
      setToast={vi.fn()}
      creationBlockedHint={hintProps()}
    />,
  );
  expect(screen.queryByRole("button", { name: "全选" })).toBeNull();
  expect(screen.getByText("后台多轮检索并逐节撰写，约 5–15 分钟，期间可离开此页")).toBeInTheDocument();
  expect(screen.getByRole("textbox")).not.toHaveFocus();
});
