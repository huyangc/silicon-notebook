import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import { ReportMarkdown } from "../../app/report-view";


test.each(["[k1]", "(k1)", "（k1）"])("报告引用 %s 在论文标题之外显示原始上传文件名", async (marker) => {
  const user = userEvent.setup();
  render(
    <ReportMarkdown
      markdown={`结论 ${marker}。`}
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
