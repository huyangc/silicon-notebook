// 来源栏的知识图谱就绪度那一句（kg-readiness-hint.tsx）：说明里提到的动作就是可点的字。
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import {
  KgReadinessHint,
  KgUnavailableHint,
  type KgReadinessHintProps,
  type KgUnavailableHintProps,
} from "../../app/kg-readiness-hint";

afterEach(cleanup);


function renderHint(props: Partial<KgReadinessHintProps> = {}) {
  const onBuild = vi.fn();
  const onMountBases = vi.fn();
  const view = render(
    <KgReadinessHint
      kgReady={false}
      pendingSources={0}
      baseKgAvailable={false}
      hasMountedBase={false}
      strictLabel="深入分析"
      busy={false}
      building={false}
      canMountBases
      onBuild={onBuild}
      onMountBases={onMountBases}
      {...props}
    />,
  );
  return { ...view, onBuild, onMountBases };
}


test("没图谱也没参考库：「整理知识图谱」与「挂载已整理的参考库」都是句子里的可点文字", async () => {
  const user = userEvent.setup();
  const { container, onBuild, onMountBases } = renderHint();
  const sentence = container.querySelector("p")!;
  expect(sentence).toHaveTextContent("本笔记本尚未整理知识图谱，也没挂参考库。整理知识图谱可增强深入分析效果，也可挂载已整理的参考库。");

  const build = screen.getByRole("button", { name: "整理知识图谱" });
  const mount = screen.getByRole("button", { name: "挂载已整理的参考库" });
  // 动作就在句子里，不是句子旁另起一行的按钮。
  expect(build.parentElement).toBe(sentence);
  expect(mount.parentElement).toBe(sentence);

  await user.click(build);
  expect(onBuild).toHaveBeenCalledOnce();
  await user.click(mount);
  expect(onMountBases).toHaveBeenCalledOnce();
});

test("没有挂载权限时不提挂载参考库——不说一个找不到入口的动作", () => {
  const { container } = renderHint({ canMountBases: false });
  expect(container.querySelector("p")).toHaveTextContent("本笔记本尚未整理知识图谱，也没挂参考库。整理知识图谱可增强深入分析效果。");
  expect(screen.queryByRole("button", { name: /参考库/ })).toBeNull();
  expect(screen.getByRole("button", { name: "整理知识图谱" })).toBeEnabled();
});

test("挂了参考库、只是都没整理图谱：不说「也没挂参考库」", () => {
  const { container } = renderHint({ hasMountedBase: true });
  expect(container.querySelector("p")).toHaveTextContent("本笔记本尚未整理知识图谱，挂载的参考库也都还没有整理。");
  expect(container.querySelector("p")).not.toHaveTextContent("也没挂参考库");
});

test("整理在途时动作字换成「整理中…」并禁用", () => {
  renderHint({ busy: true, building: true });
  expect(screen.getByRole("button", { name: "整理中…" })).toBeDisabled();
});

test("借用参考库图谱时，可点的是「为本笔记本单独整理」", async () => {
  const user = userEvent.setup();
  const { onBuild } = renderHint({ baseKgAvailable: true });
  expect(screen.getByText(/深入分析将借用已挂载的参考库/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "为本笔记本单独整理" }));
  expect(onBuild).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: /挂载/ })).toBeNull();
});

test("图谱已就绪但有新来源待分析：可点「分析并合并」", async () => {
  const user = userEvent.setup();
  const { onBuild } = renderHint({ kgReady: true, pendingSources: 3 });
  expect(screen.getByText(/知识图谱已就绪 · 3 篇新来源待分析/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "分析并合并" }));
  expect(onBuild).toHaveBeenCalledOnce();
});

test("图谱已就绪且没有待分析：只说现状，没有动作", () => {
  renderHint({ kgReady: true });
  expect(screen.getByText("✓ 知识图谱已就绪 · 「深入分析」将同时使用图谱")).toBeInTheDocument();
  expect(screen.queryByRole("button")).toBeNull();
});


// ---------------------------------------------------------------------------
// 问答输入框下排那句「这一问取不到知识图谱」。
// ---------------------------------------------------------------------------

function renderUnavailable(props: Partial<KgUnavailableHintProps> = {}) {
  const handlers = { onSelectKgBases: vi.fn(), onBuild: vi.fn(), onMountBases: vi.fn() };
  const view = render(
    <KgUnavailableHint
      variant="strict"
      blockedByScope={false}
      strictLabel="深入分析"
      canBuild
      canMountBases
      busy={false}
      building={false}
      {...handlers}
      {...props}
    />,
  );
  return { ...view, ...handlers };
}

test("带图谱的参考库没勾：只给「勾选带图谱的参考库」，不劝整理整库", async () => {
  const user = userEvent.setup();
  const { container, onSelectKgBases } = renderUnavailable({ blockedByScope: true });
  expect(container).toHaveTextContent("已整理知识图谱的参考库这次都没勾选，深入分析将只用原文检索；勾选带图谱的参考库可增强");
  expect(screen.queryByRole("button", { name: "整理知识图谱" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "勾选带图谱的参考库" }));
  expect(onSelectKgBases).toHaveBeenCalledOnce();
});

test("一个带图的都没有：「整理知识图谱」或「挂载已整理的参考库」都是可点的字", async () => {
  const user = userEvent.setup();
  const { container, onBuild, onMountBases } = renderUnavailable();
  expect(container).toHaveTextContent("该笔记本尚无知识图谱，深入分析将只用原文检索；整理知识图谱或挂载已整理的参考库可增强问答效果");
  await user.click(screen.getByRole("button", { name: "整理知识图谱" }));
  await user.click(screen.getByRole("button", { name: "挂载已整理的参考库" }));
  expect(onBuild).toHaveBeenCalledOnce();
  expect(onMountBases).toHaveBeenCalledOnce();
});

test("只读成员两件事都做不了：只说现状，不给死按钮", () => {
  const { container } = renderUnavailable({ canBuild: false, canMountBases: false });
  expect(container).toHaveTextContent("该笔记本尚无知识图谱，深入分析将只用原文检索");
  expect(screen.queryByRole("button")).toBeNull();
});

test("扩展引擎需要图谱：同样给出可点的出路", () => {
  const { container } = renderUnavailable({ variant: "extension", canMountBases: false });
  expect(container).toHaveTextContent("当前扩展引擎需要知识图谱；整理知识图谱后即可使用");
  expect(container.querySelector(".chat-hint")).not.toBeNull();
});
