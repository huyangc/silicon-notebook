// 问答欢迎页「怎么开始」那一句（welcome-guidance.tsx）：说到的动作就是可点的字。
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { WelcomeGuidance } from "../../app/welcome-guidance";

afterEach(cleanup);


function renderGuidance(props: Partial<Parameters<typeof WelcomeGuidance>[0]> = {}) {
  const onAddSources = vi.fn();
  const onMountBases = vi.fn();
  const view = render(
    <WelcomeGuidance
      blocked
      canAddSources
      canMountBases
      onAddSources={onAddSources}
      onMountBases={onMountBases}
      {...props}
    />,
  );
  return { ...view, onAddSources, onMountBases };
}


test("没有可检索内容：「上传来源」与「挂载一个参考库」就在句子里，按下即做", async () => {
  const user = userEvent.setup();
  const { container, onAddSources, onMountBases } = renderGuidance();
  expect(container).toHaveTextContent("上传来源（PDF、Markdown、DOCX 或 PPTX），或挂载一个参考库，就能基于内容对话了。");
  await user.click(screen.getByRole("button", { name: "上传来源" }));
  await user.click(screen.getByRole("button", { name: "挂载一个参考库" }));
  expect(onAddSources).toHaveBeenCalledOnce();
  expect(onMountBases).toHaveBeenCalledOnce();
});

test("不能挂载参考库（非库主）时只给「上传来源」", () => {
  const { container } = renderGuidance({ canMountBases: false });
  expect(container).toHaveTextContent("上传来源（PDF、Markdown、DOCX 或 PPTX），就能基于内容对话了。");
  expect(screen.queryByRole("button", { name: /参考库/ })).toBeNull();
});

test("只读成员两件都做不了：不给死按钮，如实说内容由谁添加", () => {
  const { container } = renderGuidance({ canAddSources: false, canMountBases: false });
  expect(container).toHaveTextContent("这本笔记本还没有可检索的内容，由库主或可编辑的成员添加之后就能在这里提问。");
  expect(screen.queryByRole("button")).toBeNull();
});

test("有参考库可问、本库还没来源：「上传来源」可点", async () => {
  const user = userEvent.setup();
  const { container, onAddSources } = renderGuidance({ blocked: false });
  expect(container).toHaveTextContent("上传来源（PDF、Markdown、DOCX 或 PPTX）后，这里会根据来源内容生成可追溯的问题建议。");
  await user.click(screen.getByRole("button", { name: "上传来源" }));
  expect(onAddSources).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: /参考库/ })).toBeNull();
});

test("镜像库的内容随源环境同步：不说「由库主添加」", () => {
  const { container } = renderGuidance({ canAddSources: false, canMountBases: false, mirrored: true });
  expect(container).toHaveTextContent("这本笔记本还没有可检索的内容，内容随源环境同步过来之后就能在这里提问。");
  expect(screen.queryByRole("button")).toBeNull();
});
