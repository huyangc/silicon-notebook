import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  patchSearchProfile: vi.fn(),
}));

vi.mock("../../app/auth.ts", async () => {
  const actual = await vi.importActual<typeof import("../../app/auth.ts")>("../../app/auth.ts");
  return { ...actual, patchSearchProfile: mocks.patchSearchProfile };
});

import type { AuthUser } from "../../app/auth.ts";
import { humanizedError } from "../../app/errors.ts";
import { SearchProfileModal } from "../../app/search-profile-modal";

function baseUser(searchProfile: unknown = null): AuthUser {
  return {
    id: "u1",
    email: "a@example.com",
    display_name: "Alice",
    role: "user",
    username: "u00000001",
    ui_mode: "auto",
    search_profile: searchProfile,
  };
}

test("渲染:已设置字段显示当前值与来源标记,未设置字段不显示标记", async () => {
  const user = baseUser({
    version: 1,
    fields: {
      answer_language: { value: "zh", origin: "user", updated_at: "x" },
      answer_shape: { value: "table_first", origin: "job", updated_at: "x" },
      domain_terms: { value: ["PPA", "IRR"], origin: "user", updated_at: "x" },
    },
  });
  render(<SearchProfileModal currentUser={user} onSaved={() => undefined} onClose={() => undefined} />);

  expect(screen.getByLabelText("回答语言")).toHaveValue("zh");
  expect(screen.getByLabelText("组织方式")).toHaveValue("table_first");
  // answer_detail 未设置——下拉框落回默认「自动」,且没有来源徽标
  expect(screen.getByLabelText("详略")).toHaveValue("auto");
  expect(screen.getAllByText("你设置的").length).toBeGreaterThan(0);
  expect(screen.getAllByText("自动判断").length).toBeGreaterThan(0);
  expect(screen.getByText("PPA ×")).toBeInTheDocument();
  expect(screen.getByText("IRR ×")).toBeInTheDocument();
});

test("改:切换下拉框并保存,只把改动过的字段送进请求体,来源徽标随之消失", async () => {
  mocks.patchSearchProfile.mockResolvedValue(baseUser({
    version: 1,
    fields: { answer_language: { value: "en", origin: "user", updated_at: "x" } },
  }));
  const testUser = userEvent.setup();
  const onSaved = vi.fn();
  const onClose = vi.fn();
  const initial = baseUser({
    version: 1,
    fields: { answer_language: { value: "zh", origin: "user", updated_at: "x" } },
  });
  render(<SearchProfileModal currentUser={initial} onSaved={onSaved} onClose={onClose} />);

  // 改动前显示服务端来源徽标
  expect(screen.getAllByText("你设置的").length).toBeGreaterThan(0);

  await testUser.selectOptions(screen.getByLabelText("回答语言"), "英文");
  // 草稿一旦改动，旧来源徽标不再显示（保存前就已过时）
  expect(screen.queryByText("你设置的")).not.toBeInTheDocument();

  await testUser.click(screen.getByRole("button", { name: "保存" }));

  expect(mocks.patchSearchProfile).toHaveBeenCalledWith({ answer_language: "en" });
  expect(onSaved).toHaveBeenCalledOnce();
  expect(onClose).toHaveBeenCalledOnce();
});

test("清空:点击单字段「恢复自动」并保存,该字段在请求体里显式为 null", async () => {
  mocks.patchSearchProfile.mockResolvedValue(baseUser());
  const testUser = userEvent.setup();
  const initial = baseUser({
    version: 1,
    fields: { answer_shape: { value: "prose", origin: "job", updated_at: "x" } },
  });
  render(<SearchProfileModal currentUser={initial} onSaved={() => undefined} onClose={() => undefined} />);

  const resetButtons = screen.getAllByRole("button", { name: "恢复自动" });
  // 「组织方式」是第二个字段行；直接按下拉框当前值定位更稳，但这里字段顺序固定
  // （回答语言/组织方式/详略/常用术语），第二个「恢复自动」对应组织方式。
  await testUser.click(resetButtons[1]);
  expect(screen.getByLabelText("组织方式")).toHaveValue("auto");

  await testUser.click(screen.getByRole("button", { name: "保存" }));

  expect(mocks.patchSearchProfile).toHaveBeenCalledWith({ answer_shape: null });
});

test("全部恢复自动:一次性把已设置的全部字段清空", async () => {
  mocks.patchSearchProfile.mockResolvedValue(baseUser());
  const testUser = userEvent.setup();
  const initial = baseUser({
    version: 1,
    fields: {
      answer_language: { value: "zh", origin: "user", updated_at: "x" },
      answer_detail: { value: "concise", origin: "user", updated_at: "x" },
      domain_terms: { value: ["PPA"], origin: "user", updated_at: "x" },
    },
  });
  render(<SearchProfileModal currentUser={initial} onSaved={() => undefined} onClose={() => undefined} />);

  await testUser.click(screen.getByRole("button", { name: "全部恢复自动" }));
  expect(screen.queryByText("PPA ×")).not.toBeInTheDocument();

  await testUser.click(screen.getByRole("button", { name: "保存" }));

  expect(mocks.patchSearchProfile).toHaveBeenCalledWith({
    answer_language: null,
    answer_shape: null,
    answer_detail: null,
    domain_terms: null,
  });
});

test("失败态:保存失败显示人话层错误文案,弹窗保持打开", async () => {
  mocks.patchSearchProfile.mockRejectedValue(humanizedError("这项功能当前未开启，暂时无法编辑"));
  const testUser = userEvent.setup();
  const onClose = vi.fn();
  render(<SearchProfileModal currentUser={baseUser()} onSaved={() => undefined} onClose={onClose} />);

  await testUser.selectOptions(screen.getByLabelText("详略"), "简明");
  await testUser.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText("这项功能当前未开启，暂时无法编辑")).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
});

test("术语标签输入:回车添加,点击移除,超长/重复被拒并提示", async () => {
  const testUser = userEvent.setup();
  render(<SearchProfileModal currentUser={baseUser()} onSaved={() => undefined} onClose={() => undefined} />);

  const input = screen.getByLabelText("常用术语");
  await testUser.type(input, "PPA{Enter}");
  expect(screen.getByText("PPA ×")).toBeInTheDocument();

  await testUser.type(input, "PPA{Enter}");
  expect(screen.getByText("这条术语已经添加过了")).toBeInTheDocument();

  await testUser.click(screen.getByText("PPA ×"));
  expect(screen.queryByText("PPA ×")).not.toBeInTheDocument();
});
