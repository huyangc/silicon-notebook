import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import { ReportMarkdown } from "../../app/report-view";


test("报告引用在论文标题之外显示原始上传文件名", async () => {
  const user = userEvent.setup();
  render(
    <ReportMarkdown
      markdown="结论 [k1]。"
      references={[{
        key: "k1",
        label: "Reliable Analog Design Methods",
        source_title: "Reliable Analog Design Methods",
        source_file_name: "user-uploaded-paper.pdf",
        location_label: "p. 2",
        snippet: "grounded excerpt",
      }]}
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("原始文件：user-uploaded-paper.pdf")).toBeInTheDocument();
  expect(screen.queryByText(/mineru.*\.md/i)).not.toBeInTheDocument();
});
