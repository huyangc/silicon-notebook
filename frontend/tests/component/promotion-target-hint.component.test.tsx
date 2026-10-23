// 「贡献到公共知识库」因没挂公共知识库而禁用时，紧挨按钮的那半句：能挂的人看到可点的
// 「挂载公共知识库」；其他成员只被告知要库主先挂；镜像谁都挂不了，就不说。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { PromotionTargetHint } from "../../app/promotion-target-hint";

test("库主看到可点的「挂载公共知识库」", async () => {
  const onMountBases = vi.fn();
  const { container } = render(<PromotionTargetHint access="self" onMountBases={onMountBases} />);
  expect(container).toHaveTextContent("需先挂载公共知识库");
  const action = screen.getByRole("button", { name: "挂载公共知识库" });
  expect(action).toHaveClass("link-button", "inline-action");
  await userEvent.setup().click(action);
  expect(onMountBases).toHaveBeenCalledOnce();
});

test("非库主只被告知要库主先挂，没有按钮", () => {
  const { container } = render(<PromotionTargetHint access="owner" onMountBases={vi.fn()} />);
  expect(container).toHaveTextContent("需库主先挂载一个公共知识库");
  expect(screen.queryByRole("button")).toBeNull();
});

test("镜像库谁都挂不了：什么都不说", () => {
  const { container } = render(<PromotionTargetHint access="none" onMountBases={vi.fn()} />);
  expect(container).toBeEmptyDOMElement();
});
