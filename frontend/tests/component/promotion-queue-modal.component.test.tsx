// 「内容审核」弹窗从 page.tsx 抽出来之后的组件覆盖（PR-5 分片 1）。page.tsx 整体不可
// 直接渲染，这些呈现判据以前只能靠源码守卫近似钉住；现在可以真渲染了。
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { PromotionQueueModal } from "../../app/promotion-queue-modal";
import type { PromotionCandidate } from "../../app/promotion-queue";

function candidate(overrides: Partial<PromotionCandidate> = {}): PromotionCandidate {
  return {
    id: "cand-1",
    notebook_id: "nb-abcdefghij",
    object_id: "obj-1",
    object_type: "concept",
    status: "proposed",
    payload: { name: "梯度下降" },
    base_match_id: null,
    target_base_id: "base-1",
    target_base_name: "",
    source_kind: "knowledge",
    ...overrides,
  } as unknown as PromotionCandidate;
}

const noop = () => undefined;
const noName = () => undefined;

test("空队列时给出「暂无待审核」而不是一片空白", () => {
  render(
    <PromotionQueueModal
      candidates={[]}
      busy={false}
      lookupNotebookName={noName}
      onRequestClose={noop}
      onApprove={noop}
      onReject={noop}
    />,
  );

  expect(screen.getByText("暂无待审核的收录申请。")).toBeInTheDocument();
});

test("有候选时逐条渲染，批准/拒绝把候选 id 交回调用方", async () => {
  const user = userEvent.setup();
  const onApprove = vi.fn();
  const onReject = vi.fn();
  render(
    <PromotionQueueModal
      candidates={[candidate(), candidate({ id: "cand-2", payload: { title: "反向传播" } })]}
      busy={false}
      lookupNotebookName={noName}
      onRequestClose={noop}
      onApprove={onApprove}
      onReject={onReject}
    />,
  );

  expect(screen.getByRole("heading", { name: "梯度下降" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "反向传播" })).toBeInTheDocument();

  const [firstApprove] = screen.getAllByRole("button", { name: "批准收录" });
  await user.click(firstApprove);
  expect(onApprove).toHaveBeenCalledWith("cand-1");

  const [, secondReject] = screen.getAllByRole("button", { name: "拒绝" });
  await user.click(secondReject);
  expect(onReject).toHaveBeenCalledWith("cand-2");
});

test("A3：没有目标库的存量候选批准置灰并原地指路，拒绝仍可点", async () => {
  const user = userEvent.setup();
  const onApprove = vi.fn();
  const onReject = vi.fn();
  render(
    <PromotionQueueModal
      candidates={[candidate({ target_base_id: "" })]}
      busy={false}
      lookupNotebookName={noName}
      onRequestClose={noop}
      onApprove={onApprove}
      onReject={onReject}
    />,
  );

  expect(
    screen.getByText("未指定目标公共知识库，需先运行 scripts/backfill_promotion_targets.py 指定目标库"),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "批准收录" })).toBeDisabled();

  await user.click(screen.getByRole("button", { name: "拒绝" }));
  expect(onReject).toHaveBeenCalledWith("cand-1");
  expect(onApprove).not.toHaveBeenCalled();
});

test("目标库名三级回退：后端 join 名 → 调用方本地列表 → 截断 id", () => {
  const lookup = (id: string) => (id === "base-local" ? "本地知道的库" : undefined);
  render(
    <PromotionQueueModal
      candidates={[
        candidate({ id: "c-joined", target_base_id: "base-x", target_base_name: "后端给的库名" }),
        candidate({ id: "c-local", target_base_id: "base-local" }),
        candidate({ id: "c-unknown", target_base_id: "base-unknown-0123456789" }),
      ]}
      busy={false}
      lookupNotebookName={lookup}
      onRequestClose={noop}
      onApprove={noop}
      onReject={noop}
    />,
  );

  expect(screen.getByText("目标公共知识库: 后端给的库名")).toBeInTheDocument();
  expect(screen.getByText("目标公共知识库: 本地知道的库")).toBeInTheDocument();
  expect(screen.getByText("目标公共知识库: base-unkno")).toBeInTheDocument();
});

test("记忆候选带出「记忆提取候选」标签、固定修订与服务端证据", () => {
  render(
    <PromotionQueueModal
      candidates={[candidate({
        source_kind: "memory",
        source_revision: 3,
        payload: {
          name: "记忆候选",
          candidates: [{ object_type: "concept", payload: { name: "候选概念", definition: "候选定义" } }],
        },
        evidence: [{ source_title: "论文 A", location_label: "第 2 页", quoted_span: "原文片段" }],
      })]}
      busy={false}
      lookupNotebookName={noName}
      onRequestClose={noop}
      onApprove={noop}
      onReject={noop}
    />,
  );

  expect(screen.getByText("记忆提取候选")).toBeInTheDocument();
  expect(screen.getByText("固定修订 #3")).toBeInTheDocument();
  expect(within(screen.getByLabelText("记忆待审知识对象")).getByText("候选定义")).toBeInTheDocument();
  const evidence = screen.getByLabelText("服务端校验证据");
  expect(within(evidence).getByText("原文片段")).toBeInTheDocument();
  expect(within(evidence).getByText("论文 A · 第 2 页")).toBeInTheDocument();
});

test("busy 时批准与拒绝都禁用（防重复提交）", () => {
  render(
    <PromotionQueueModal
      candidates={[candidate()]}
      busy
      lookupNotebookName={noName}
      onRequestClose={noop}
      onApprove={noop}
      onReject={noop}
    />,
  );

  expect(screen.getByRole("button", { name: "批准收录" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "拒绝" })).toBeDisabled();
});

// 协调器契约：背景点击与「×」是**两种** close reason（ROOT_MODAL_POLICIES 按 reason 判
// 这个 slot 允不允许这样关），不能合并成一个无参 onClose。
test("「×」报 button、点背景报 backdrop、点卡片内部不关", async () => {
  const user = userEvent.setup();
  const onRequestClose = vi.fn();
  const { container } = render(
    <PromotionQueueModal
      candidates={[candidate()]}
      busy={false}
      lookupNotebookName={noName}
      onRequestClose={onRequestClose}
      onApprove={noop}
      onReject={noop}
    />,
  );

  await user.click(screen.getByTitle("Close"));
  expect(onRequestClose).toHaveBeenLastCalledWith("button");

  await user.click(screen.getByRole("heading", { name: "梯度下降" }));
  expect(onRequestClose).toHaveBeenCalledTimes(1);

  await user.click(container.querySelector("section.utility-modal")!);
  expect(onRequestClose).toHaveBeenLastCalledWith("backdrop");
});

test("被别的弹窗盖住时整体退出交互树（aria-hidden + inert）", () => {
  const { container, rerender } = render(
    <PromotionQueueModal
      candidates={[candidate()]}
      busy={false}
      lookupNotebookName={noName}
      interactive
      zIndex={61}
      onRequestClose={noop}
      onApprove={noop}
      onReject={noop}
    />,
  );

  const dialog = container.querySelector("section.utility-modal")!;
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(dialog.getAttribute("aria-hidden")).toBe("false");
  expect(dialog.hasAttribute("inert")).toBe(false);
  expect((dialog as HTMLElement).style.zIndex).toBe("61");

  rerender(
    <PromotionQueueModal
      candidates={[candidate()]}
      busy={false}
      lookupNotebookName={noName}
      interactive={false}
      zIndex={61}
      onRequestClose={noop}
      onApprove={noop}
      onReject={noop}
    />,
  );

  expect(dialog.getAttribute("aria-modal")).toBe("false");
  expect(dialog.getAttribute("aria-hidden")).toBe("true");
  expect(dialog.hasAttribute("inert")).toBe(true);
});
