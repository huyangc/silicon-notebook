import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { PromotionCandidateActions } from "../../app/promotion-candidate-actions";

test("有目标库：批准/拒绝都可点，没有存量候选提示", async () => {
  const user = userEvent.setup();
  const onApprove = vi.fn();
  const onReject = vi.fn();
  render(
    <PromotionCandidateActions
      hasTargetBase
      busy={false}
      onApprove={onApprove}
      onReject={onReject}
    />,
  );

  expect(screen.queryByText(/未指定目标公共知识库/)).toBeNull();
  const approveButton = screen.getByRole("button", { name: "批准收录" });
  expect(approveButton).toBeEnabled();
  await user.click(approveButton);
  expect(onApprove).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "拒绝" }));
  expect(onReject).toHaveBeenCalledTimes(1);
});

test("A3：没有目标库时批准置灰并原地指路补救脚本，拒绝仍可点", async () => {
  const user = userEvent.setup();
  const onApprove = vi.fn();
  const onReject = vi.fn();
  render(
    <PromotionCandidateActions
      hasTargetBase={false}
      busy={false}
      onApprove={onApprove}
      onReject={onReject}
    />,
  );

  expect(
    screen.getByText("未指定目标公共知识库，需先运行 scripts/backfill_promotion_targets.py 指定目标库"),
  ).toBeInTheDocument();

  const approveButton = screen.getByRole("button", { name: "批准收录" });
  expect(approveButton).toBeDisabled();
  expect(approveButton).toHaveAttribute("title", "未指定目标公共知识库，暂不能批准");

  const rejectButton = screen.getByRole("button", { name: "拒绝" });
  expect(rejectButton).toBeEnabled();
  await user.click(rejectButton);
  expect(onReject).toHaveBeenCalledTimes(1);
  expect(onApprove).not.toHaveBeenCalled();
});

test("busy 时批准与拒绝都禁用", () => {
  render(
    <PromotionCandidateActions
      hasTargetBase
      busy
      onApprove={() => undefined}
      onReject={() => undefined}
    />,
  );

  expect(screen.getByRole("button", { name: "批准收录" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "拒绝" })).toBeDisabled();
});
