import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, test } from "vitest";

import { Pagination } from "../../app/Pagination";

function Harness({ compact = false }: { compact?: boolean }) {
  const [page, setPage] = useState(0);
  return (
    <>
      <Pagination page={page} pageSize={10} total={60} onPage={setPage} label="测试分页" compact={compact} />
      <output aria-label="当前页">{page}</output>
    </>
  );
}

test("跳页框回车提交，页码按 1 起算并夹紧到最后一页", async () => {
  const user = userEvent.setup();
  render(<Harness />);

  await user.type(screen.getByRole("spinbutton", { name: "跳到第几页" }), "4{Enter}");
  expect(screen.getByLabelText("当前页")).toHaveTextContent("3");

  await user.type(screen.getByRole("spinbutton", { name: "跳到第几页" }), "99{Enter}");
  expect(screen.getByLabelText("当前页")).toHaveTextContent("5");
});

test("框里敲了页码没回车就点下一页：只翻一页，不先按框里的数跳一次", async () => {
  const user = userEvent.setup();
  render(<Harness />);

  await user.type(screen.getByRole("spinbutton", { name: "跳到第几页" }), "4");
  await user.click(screen.getByRole("button", { name: "下一页" }));

  expect(screen.getByLabelText("当前页")).toHaveTextContent("1");
  expect(screen.getByRole("spinbutton", { name: "跳到第几页" })).toHaveValue(null);
});

test("紧凑样式的箭头按钮保留「上一页/下一页」的可访问名称", async () => {
  const user = userEvent.setup();
  render(<Harness compact />);

  expect(screen.getByRole("navigation", { name: "测试分页" })).toHaveClass("compact");
  expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "下一页" }));
  expect(screen.getByLabelText("当前页")).toHaveTextContent("1");
  expect(screen.getByText("2 / 6")).toBeInTheDocument();
});
