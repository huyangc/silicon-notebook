// 「关系审核队列」弹窗从 page.tsx 抽出来之后的组件覆盖（PR-5 分片 1）。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { EdgeReviewModal } from "../../app/edge-review-modal";
import type { EdgeReviewItem } from "../../app/edge-review-queue";

function edge(overrides: Partial<EdgeReviewItem> = {}): EdgeReviewItem {
  return {
    rel_id: "rel-1",
    edge_type: "supports",
    review_status: "pending",
    trust_score: 0.42,
    review_priority: 0.91,
    source_name: "梯度下降",
    source_object_id: "obj-a",
    target_name: "学习率",
    target_object_id: "obj-b",
    ...overrides,
  } as unknown as EdgeReviewItem;
}

const noop = () => undefined;

test("空队列时给出「暂无待审关系」", () => {
  render(<EdgeReviewModal edges={[]} total={0} busy={false} onRequestClose={noop} onDecide={noop} />);

  expect(screen.getByText("暂无待审关系。")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "关系审核队列（共 0 条）" })).toBeInTheDocument();
});

// R3 T-A3：抬头的 total 是端点报的真实队列长度，与被 limit 截断的当前页是两个数。
test("抬头按 total 与本页条数分别呈现完整与截断两种情形", () => {
  const { rerender } = render(
    <EdgeReviewModal edges={[edge()]} total={1} busy={false} onRequestClose={noop} onDecide={noop} />,
  );
  expect(screen.getByRole("heading", { name: "关系审核队列（共 1 条）" })).toBeInTheDocument();

  rerender(
    <EdgeReviewModal edges={[edge()]} total={120} busy={false} onRequestClose={noop} onDecide={noop} />,
  );
  expect(screen.getByRole("heading", { level: 2 }).textContent).toContain("120");

  rerender(
    <EdgeReviewModal edges={[edge()]} total={null} busy={false} onRequestClose={noop} onDecide={noop} />,
  );
  expect(screen.getByRole("heading", { name: "关系审核队列" })).toBeInTheDocument();
});

test("逐条渲染并把 relId + 判定交回调用方；已拒绝的关系不再给按钮", async () => {
  const user = userEvent.setup();
  const onDecide = vi.fn();
  render(
    <EdgeReviewModal
      edges={[
        edge(),
        edge({ rel_id: "rel-2", review_status: "rejected", source_name: "动量", target_name: "收敛" }),
      ]}
      total={2}
      busy={false}
      onRequestClose={noop}
      onDecide={onDecide}
    />,
  );

  expect(screen.getByRole("heading", { name: "梯度下降 → 学习率" })).toBeInTheDocument();
  expect(screen.getAllByText("可信 0.42")).toHaveLength(2);
  expect(screen.getAllByText("优先级 0.91")).toHaveLength(2);

  // 两条候选里只有未拒绝的那条带按钮。
  expect(screen.getAllByRole("button", { name: "确认可信" })).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "确认可信" }));
  expect(onDecide).toHaveBeenCalledWith("rel-1", "verified");

  await user.click(screen.getByRole("button", { name: "拒绝" }));
  expect(onDecide).toHaveBeenLastCalledWith("rel-1", "rejected");
});

test("busy 时确认与拒绝都禁用（防重复提交）", () => {
  render(
    <EdgeReviewModal edges={[edge()]} total={1} busy onRequestClose={noop} onDecide={noop} />,
  );

  expect(screen.getByRole("button", { name: "确认可信" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "拒绝" })).toBeDisabled();
});

test("「×」报 button、点背景报 backdrop；被盖住时整体退出交互树", async () => {
  const user = userEvent.setup();
  const onRequestClose = vi.fn();
  const { container, rerender } = render(
    <EdgeReviewModal
      edges={[edge()]}
      total={1}
      busy={false}
      interactive
      zIndex={62}
      onRequestClose={onRequestClose}
      onDecide={noop}
    />,
  );

  await user.click(screen.getByTitle("Close"));
  expect(onRequestClose).toHaveBeenLastCalledWith("button");

  const dialog = container.querySelector("section.utility-modal")!;
  await user.click(dialog);
  expect(onRequestClose).toHaveBeenLastCalledWith("backdrop");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect((dialog as HTMLElement).style.zIndex).toBe("62");

  rerender(
    <EdgeReviewModal
      edges={[edge()]}
      total={1}
      busy={false}
      interactive={false}
      zIndex={62}
      onRequestClose={onRequestClose}
      onDecide={noop}
    />,
  );

  expect(dialog.getAttribute("aria-hidden")).toBe("true");
  expect(dialog.hasAttribute("inert")).toBe(true);
});
