import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../../app/knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-model.ts")>();
  return { ...actual, putCellCode: vi.fn(), deleteCellCode: vi.fn() };
});

import { KnowhowCodeModal } from "../../app/knowhow-code.tsx";
import { putCellCode, type KnowhowCellCode } from "../../app/knowhow-model.ts";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("session 用户保存代码后 UI 立即展示响应中的 username 溯源", async () => {
  const user = userEvent.setup();
  const saved: KnowhowCellCode = {
    codeText: "print(1)",
    language: "python",
    status: "implemented",
    updatedAt: "2026-07-25T10:00:00Z",
    updatedBy: "a00123456",
  };
  vi.mocked(putCellCode).mockResolvedValue(saved);

  function Harness() {
    const [code, setCode] = useState<KnowhowCellCode>({
      codeText: "",
      language: "",
      status: "none",
      updatedAt: null,
      updatedBy: null,
    });
    return (
      <KnowhowCodeModal
        rowId="r1"
        columnId="c1"
        rowTitle="示波器"
        columnName="实现"
        code={code}
        canEdit
        onSaved={(_columnId, entry) => setCode(entry)}
        onDeleted={() => undefined}
        onClose={() => undefined}
      />
    );
  }

  render(<Harness />);
  await user.type(screen.getByPlaceholderText("粘贴或编写代码…"), "print(1)");
  await user.type(screen.getByPlaceholderText("语言标记（可选，如 python / tcl）"), "python");
  await user.click(screen.getByRole("button", { name: "保存" }));

  await waitFor(() => expect(putCellCode).toHaveBeenCalledWith("r1", "c1", "print(1)", "python"));
  expect(await screen.findByText(/最近更新：.*来自 a00123456/)).toBeInTheDocument();
  expect(screen.getByText("print(1)")).toBeInTheDocument();
});
