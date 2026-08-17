import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { PendingBell, type PendingItem } from "../../app/pending-center.tsx";

function renderBell(items: PendingItem[]) {
  render(
    <PendingBell
      snapshot={{ count: items.length, items }}
      doneItems={[]}
      userId="u1"
      onOpenItem={vi.fn()}
      onOpenDone={vi.fn()}
      onDismissDone={vi.fn()}
    />,
  );
}

// 共享库里成员自建的待确认报告此前连库名都解析不出来(后端只按「我自有的库」映射),
// 铃铛里是一条没有出处的「深度报告《…》」——用户不知道该点进哪个库去确认。
// 后端随行带回库名之后,前端的判据也要从「不是报告」改成「有库名」。
test("待确认报告条目显示所在的知识库名", async () => {
  const user = userEvent.setup();
  renderBell([
    {
      type: "report_outline",
      notebook_id: "nb1",
      notebook_name: "封装工艺库",
      report_id: "r1",
      title: "封装翘曲的主要成因",
    },
  ]);

  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByText("封装工艺库")).toBeInTheDocument();
  expect(screen.getByText("深度报告《封装翘曲的主要成因》")).toBeInTheDocument();
});

test("库名为空(旧数据 / 库已删)时不渲染一个空格子", async () => {
  const user = userEvent.setup();
  renderBell([
    {
      type: "report_outline",
      notebook_id: "nb1",
      notebook_name: "",
      report_id: "r1",
      title: "封装翘曲的主要成因",
    },
  ]);

  await user.click(screen.getByLabelText("待确认中心"));
  const row = screen.getByText("深度报告《封装翘曲的主要成因》").closest(".pending-row-main");
  expect(row?.querySelector(".pending-row-nb")).toBeNull();
});
