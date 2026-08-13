import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { SchemaManager } from "../../app/schema-manager.tsx";
import type { ObjectSchema } from "../../app/workspace-model.ts";

function item(overrides: Partial<ObjectSchema> = {}): ObjectSchema {
  return {
    object_type: "claim",
    plural: "claims",
    fields: ["statement", "condition"],
    primary: "statement",
    description: "可验证结论",
    label: "结论",
    list_fields: [],
    source: "global",
    status: "active",
    rationale: "",
    notebook_id: "nb-1",
    scope: "global",
    inherited: true,
    overrides_global: false,
    can_edit: true,
    ...overrides,
  };
}

function mount(overrides: Partial<Parameters<typeof SchemaManager>[0]> = {}) {
  const callbacks = {
    onView: vi.fn(),
    onPatch: vi.fn(),
    onCreate: vi.fn(),
    onDelete: vi.fn(),
    onInduce: vi.fn(),
  };
  render(
    <SchemaManager
      schemas={[item()]}
      busy={false}
      view="notebook"
      canEdit
      canManageGlobal={false}
      {...callbacks}
      {...overrides}
    />,
  );
  return callbacks;
}

test("当前笔记本把继承类型标为全局继承，保存走当前笔记本的写回调", () => {
  const callbacks = mount();
  expect(screen.getByText("全局继承")).toBeInTheDocument();
  fireEvent.change(screen.getAllByLabelText("显示名")[0], { target: { value: "项目结论" } });
  expect(screen.getByRole("button", { name: "保存并建立覆盖" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停用并建立覆盖" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  expect(callbacks.onPatch).toHaveBeenCalledWith("claim", expect.objectContaining({ label: "项目结论" }));
});

test("字段替换可以同步修改主字段、列表字段和复数名称", async () => {
  const callbacks = mount();
  fireEvent.change(screen.getAllByLabelText("字段（逗号分隔，按顺序）")[0], { target: { value: "title, evidence" } });
  fireEvent.change(screen.getAllByLabelText("主字段")[0], { target: { value: "title" } });
  fireEvent.change(screen.getAllByLabelText("列表字段（逗号分隔，可留空）")[0], { target: { value: "evidence" } });
  fireEvent.change(screen.getAllByLabelText("复数名称")[0], { target: { value: "findings" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  await waitFor(() => expect(callbacks.onPatch).toHaveBeenCalledWith("claim", expect.objectContaining({
    fields: ["title", "evidence"],
    primary: "title",
    list_fields: ["evidence"],
    plural: "findings",
  })));
});

test("新增校验失败或请求失败时显示原因并保留输入", async () => {
  const onCreate = vi.fn().mockRejectedValue(new Error("conflict"));
  mount({ schemas: [], onCreate });
  const typeInput = screen.getByLabelText("类型标识（snake_case）");
  const fieldsInput = screen.getByLabelText("字段（逗号分隔）");
  fireEvent.change(typeInput, { target: { value: "Bad-Type" } });
  fireEvent.change(fieldsInput, { target: { value: "title" } });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  expect(screen.getByRole("alert")).toHaveTextContent("类型标识须以小写字母开头");
  expect(onCreate).not.toHaveBeenCalled();

  fireEvent.change(typeInput, { target: { value: "lab_recipe" } });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("当前输入已保留"));
  expect(typeInput).toHaveValue("lab_recipe");
  expect(fieldsInput).toHaveValue("title");
  expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
    object_type: "lab_recipe",
    plural: "lab_recipes",
    primary: "title",
    list_fields: [],
  }));
});

test("新增表单在请求前执行名称与文本长度护栏", () => {
  const onCreate = vi.fn();
  mount({ schemas: [], onCreate });
  fireEvent.change(screen.getByLabelText("类型标识（snake_case）"), { target: { value: `a${"b".repeat(80)}` } });
  fireEvent.change(screen.getByLabelText("字段（逗号分隔）"), { target: { value: "title" } });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  expect(screen.getByRole("alert")).toHaveTextContent("类型标识不能超过 80 个字符");
  expect(onCreate).not.toHaveBeenCalled();
});

test("当前笔记本覆盖提供恢复全局，而本库自建条目提供删除", () => {
  const callbacks = mount({ schemas: [item({ inherited: false, overrides_global: true, scope: "notebook" })] });
  expect(screen.getByText("当前笔记本覆盖")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "恢复全局" }));
  expect(callbacks.onDelete).toHaveBeenCalledWith("claim");

  const selfOwned = mount({ schemas: [item({ object_type: "project_note", inherited: false, overrides_global: false, scope: "notebook" })] });
  expect(screen.getByText("当前笔记本自建")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(selfOwned.onDelete).toHaveBeenCalledWith("project_note");
});

test("只读成员可以查看，但不会渲染任何写入控件", () => {
  mount({ canEdit: false, schemas: [item({ can_edit: false })] });
  expect(screen.getByText("你拥有只读权限，可以查看当前笔记本实际采用的类型。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "从当前笔记本归纳候选类型" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "新增类型" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "保存并建立覆盖" })).not.toBeInTheDocument();
  expect(screen.queryByText("新增当前笔记本类型")).not.toBeInTheDocument();
});

test("管理员可切换全局基线，并看到它影响未覆盖笔记本的说明", () => {
  const callbacks = mount({ canManageGlobal: true, view: "global", canEdit: true, schemas: [item({ scope: "global", inherited: false, source: "custom" })] });
  expect(screen.getByText("全局基线会影响所有尚未建立当前笔记本覆盖的笔记本。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "全局基线" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "当前笔记本" }));
  expect(callbacks.onView).toHaveBeenCalledWith("notebook");
});
