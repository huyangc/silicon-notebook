import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { PromotionTargetModal } from "../../app/promotion-target-modal";

const base = (id: string, name: string) => ({
  id,
  name,
  tier: "base",
  active: true,
  inactive_reason: "",
});

test("0 个候选：原地显示中性提示，且从不回调 onPick", () => {
  const onPick = vi.fn();
  render(
    <PromotionTargetModal
      storageKey="test.promotion.window"
      title="选择贡献目标"
      description="说明文字"
      bases={[]}
      onPick={onPick}
      onClose={() => undefined}
    />,
  );

  expect(screen.getByText("可选的公共知识库已变化，请关闭后重新提交")).toBeInTheDocument();
  expect(screen.queryAllByRole("button", { name: "物理知识库" })).toHaveLength(0);
  expect(onPick).not.toHaveBeenCalled();
});

test("1 个候选：仍渲染成一个可点击项，点击前不回调 onPick", async () => {
  const user = userEvent.setup();
  const onPick = vi.fn();
  render(
    <PromotionTargetModal
      storageKey="test.promotion.window"
      title="选择贡献目标"
      description="说明文字"
      bases={[base("b1", "物理知识库")]}
      onPick={onPick}
      onClose={() => undefined}
    />,
  );

  expect(screen.getByText("选择贡献目标")).toBeInTheDocument();
  const option = screen.getByRole("button", { name: "物理知识库" });
  expect(onPick).not.toHaveBeenCalled();

  await user.click(option);
  expect(onPick).toHaveBeenCalledTimes(1);
  expect(onPick).toHaveBeenCalledWith("b1");
});

test("私有/共享库与失效的公共库不是合格目标：不渲染、也不算进 0 态判定", () => {
  const onPick = vi.fn();
  render(
    <PromotionTargetModal
      storageKey="test.promotion.window"
      title="选择贡献目标"
      description="说明文字"
      bases={[
        { ...base("p1", "我的私有库"), tier: "personal" },
        { ...base("b0", "已下线的公共库"), active: false, inactive_reason: "demoted" },
        base("b1", "物理知识库"),
      ]}
      onPick={onPick}
      onClose={() => undefined}
    />,
  );

  expect(screen.getByRole("button", { name: "物理知识库" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "我的私有库" })).toBeNull();
  expect(screen.queryByRole("button", { name: "已下线的公共库" })).toBeNull();
  expect(screen.queryByText("可选的公共知识库已变化，请关闭后重新提交")).toBeNull();
});

test("只剩私有库时按 0 态处理（不列出后端必拒的目标）", () => {
  render(
    <PromotionTargetModal
      storageKey="test.promotion.window"
      title="选择贡献目标"
      description="说明文字"
      bases={[{ ...base("p1", "我的私有库"), tier: "personal" }]}
      onPick={vi.fn()}
      onClose={() => undefined}
    />,
  );
  expect(screen.getByText("可选的公共知识库已变化，请关闭后重新提交")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "我的私有库" })).toBeNull();
});

test(">1 个候选：渲染完整列表(每个候选各一个按钮)，点击后回调对应的目标库 id", async () => {
  const user = userEvent.setup();
  const onPick = vi.fn();
  const onClose = vi.fn();
  render(
    <PromotionTargetModal
      storageKey="test.promotion.window"
      title="选择贡献目标"
      description="本笔记本挂载了多个公共知识库，请选择这条知识要进入哪一个。"
      bases={[base("b1", "物理知识库"), base("b2", "化学知识库")]}
      onPick={onPick}
      onClose={onClose}
    />,
  );

  expect(screen.getByText("选择贡献目标")).toBeInTheDocument();
  expect(screen.getByText("本笔记本挂载了多个公共知识库，请选择这条知识要进入哪一个。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "物理知识库" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "化学知识库" })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "化学知识库" }));
  expect(onPick).toHaveBeenCalledWith("b2");
  expect(onPick).not.toHaveBeenCalledWith("b1");

  await user.click(screen.getByTitle("Close"));
  expect(onClose).toHaveBeenCalledTimes(1);
});
