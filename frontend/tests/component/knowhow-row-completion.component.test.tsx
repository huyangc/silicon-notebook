import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-model.ts")>();
  return {
    ...actual,
    fetchKnowhowTables: vi.fn(),
    fetchKnowhowTable: vi.fn(),
    fetchKnowhowRowCodeByColumn: vi.fn(),
    completeKnowhowRow: vi.fn(),
    optimizeKnowhowCell: vi.fn(),
    patchKnowhowCell: vi.fn(),
    batchPatchKnowhowCells: vi.fn(),
  };
});

import {
  completeKnowhowRow,
  optimizeKnowhowCell,
  batchPatchKnowhowCells,
  fetchKnowhowRowCodeByColumn,
  fetchKnowhowTable,
  fetchKnowhowTables,
  patchKnowhowCell,
  type KnowhowRowCompletion,
  type KnowhowTableDetail,
} from "../../app/knowhow-model.ts";
import { modeLabel } from "../../app/ask-modes.ts";
import { KnowhowPanel } from "../../app/knowhow-panel.tsx";

function makeDetail(): KnowhowTableDetail {
  return {
    id: "t1",
    title: "排障表",
    description: "",
    anchorColumnId: null,
    columns: [
      { id: "c-problem", name: "问题", role: "attribute", position: 0 },
      { id: "c-symptom", name: "现象", role: "attribute", position: 1 },
      { id: "c-fix", name: "处理方法", role: "procedure", position: 2 },
      { id: "c-tool", name: "工具", role: "entity", position: 3 },
    ],
    rows: [
      {
        id: "r1", position: 0, projectionStatus: "synced",
        cells: { "c-problem": "供电异常", "c-symptom": "无法启动", "c-fix": "", "c-tool": "" },
      },
      {
        id: "internal-r2", position: 1, projectionStatus: "synced",
        cells: { "c-problem": "电压跌落", "c-symptom": "反复重启", "c-fix": "检查电源", "c-tool": "万用表" },
      },
    ],
  };
}

function completionEnvelope(
  suggestions: KnowhowRowCompletion["suggestions"],
  overrides: Partial<Omit<KnowhowRowCompletion, "suggestions">> = {},
): KnowhowRowCompletion {
  return {
    retrievalMode: "reasoning",
    retrievalScope: "active_and_mounted",
    retrievalStatus: "succeeded",
    reasoningTrace: [],
    evidence: [],
    ...overrides,
    suggestions,
  };
}

beforeEach(() => {
  vi.mocked(fetchKnowhowTables).mockResolvedValue([]);
  vi.mocked(fetchKnowhowTable).mockResolvedValue(makeDetail());
  vi.mocked(fetchKnowhowRowCodeByColumn).mockResolvedValue({});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("可编辑成员逐项审阅补全：展示置信度/可读参考行，接受携带空基线和来源；放弃项不可接受", async () => {
  const user = userEvent.setup();
  let resolveSave!: (value: {
    rowId: string; columnId: string; contentMd: string; projectionStatus: "pending";
  }) => void;
  vi.mocked(completeKnowhowRow).mockResolvedValue(completionEnvelope(
    [
      {
        columnId: "c-fix", suggestionMd: "检查输入电压并重新上电", confidence: "high",
        basedOnRowIds: ["internal-r2"], evidenceKeys: ["k-base"], basis: "现象与已完成记录相近", abstainReason: "",
      },
      {
        columnId: "c-tool", suggestionMd: null, confidence: "low",
        basedOnRowIds: [], evidenceKeys: [], basis: "", abstainReason: "现有记录不足以确定工具",
      },
    ],
    {
      reasoningTrace: [{ step_type: "retrieve", summary: "检索知识库", detail: { hits: 1 }, duration_ms: 8 }],
      evidence: [{
        key: "k-base", kind: "knowledge", objectType: "Claim", label: "供电检查规则",
        excerptMd: "输入电压异常时，先检查供电稳定性。[外链](https://example.test) ![示意图](asset://foreign/image.png)", sourceTitle: "设计规范", locationLabel: "第 4 节", tier: "base",
      }],
    },
  ));
  vi.mocked(patchKnowhowCell).mockReturnValue(new Promise((resolve) => { resolveSave = resolve; }));

  render(<KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t1" initialRowId="r1" />);

  const trigger = await screen.findByRole("button", { name: "智能补全空列" });
  await user.click(trigger);
  await waitFor(() => expect(completeKnowhowRow).toHaveBeenCalledWith(
    "nb-1", "t1", "r1", ["c-fix", "c-tool"], expect.any(AbortSignal),
  ));
  const completionDialog = await screen.findByRole("dialog", { name: /智能补全空列/ });
  const closeButtons = within(completionDialog).getAllByRole("button", { name: "关闭" });
  expect(closeButtons[0]).toHaveFocus();
  await user.tab({ shift: true });
  expect(closeButtons.at(-1)).toHaveFocus();
  await user.tab();
  expect(closeButtons[0]).toHaveFocus();
  expect(within(completionDialog).getByText(`检索模式：${modeLabel("reasoning")}`)).toBeInTheDocument();
  expect(within(completionDialog).getByText("检索完成")).toBeInTheDocument();
  expect(within(completionDialog).getByText("高置信度")).toBeInTheDocument();
  expect(within(within(completionDialog).getByLabelText("处理方法 的表内参考")).getByText(/电压跌落/)).toBeInTheDocument();
  expect(within(completionDialog).queryByText("internal-r2")).not.toBeInTheDocument();
  const evidenceRegion = within(completionDialog).getByLabelText("处理方法 的知识库证据");
  expect(within(evidenceRegion).getByText("供电检查规则")).toBeInTheDocument();
  expect(within(evidenceRegion).getByText("公共知识库")).toBeInTheDocument();
  expect(within(evidenceRegion).getByText("设计规范")).toBeInTheDocument();
  expect(within(evidenceRegion).getByText("第 4 节")).toBeInTheDocument();
  expect(within(evidenceRegion).getByText(/先检查供电稳定性/)).toBeInTheDocument();
  expect(within(evidenceRegion).queryByRole("link")).not.toBeInTheDocument();
  expect(within(evidenceRegion).queryByRole("img")).not.toBeInTheDocument();
  expect(within(evidenceRegion).getByText("外链")).toBeInTheDocument();
  expect(within(evidenceRegion).getByText("[图片：示意图]")).toBeInTheDocument();
  expect(within(completionDialog).getByRole("button", { name: /检索知识库/ })).toHaveAttribute("aria-expanded", "false");
  expect(within(completionDialog).getByText(/暂不建议填写：现有记录不足以确定工具/)).toBeInTheDocument();
  expect(within(completionDialog).getByLabelText("工具 的表内参考")).toHaveTextContent("未引用表内参考记录");
  expect(within(completionDialog).getByLabelText("工具 的知识库证据")).toHaveTextContent("此项未引用知识库证据");
  expect(within(completionDialog).getByRole("button", { name: "接受 工具 建议" })).toBeDisabled();

  await user.click(within(completionDialog).getByRole("button", { name: "接受 处理方法 建议" }));
  await waitFor(() => expect(patchKnowhowCell).toHaveBeenCalledWith(
    "nb-1", "t1", "r1", "c-fix", "检查输入电压并重新上电", "", "llm_complete",
  ));
  expect(within(completionDialog).getByRole("status")).toHaveTextContent("保存中");
  resolveSave({
    rowId: "r1", columnId: "c-fix", contentMd: "检查输入电压并重新上电", projectionStatus: "pending",
  });
  expect(await screen.findByText("已填入")).toBeInTheDocument();
  await user.click(closeButtons.at(-1)!);
  await waitFor(() => expect(trigger).toHaveFocus());
});

test("anchor 概念矩阵每个可补分支有入口；共享空格冻结完整组守卫，非共享列只写当前行", async () => {
  const user = userEvent.setup();
  const anchorDetail: KnowhowTableDetail = {
    id: "t-anchor", title: "概念表", description: "", anchorColumnId: "c-anchor",
    columns: [
      { id: "c-anchor", name: "概念", role: "anchor", position: 0 },
      { id: "c-shared", name: "共同方法", role: "procedure", position: 1 },
      { id: "c-own", name: "分支备注", role: "attribute", position: 2 },
    ],
    rows: [
      { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-shared": "", "c-own": "" } },
      { id: "r2", position: 1, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-shared": "", "c-own": "已有备注" } },
    ],
  };
  vi.mocked(fetchKnowhowTable).mockResolvedValue(anchorDetail);
  vi.mocked(completeKnowhowRow).mockResolvedValue(completionEnvelope([
    { columnId: "c-shared", suggestionMd: "统一检查时钟源", confidence: "high", basedOnRowIds: ["r2"], evidenceKeys: [], basis: "同概念分支", abstainReason: "" },
    { columnId: "c-own", suggestionMd: "记录当前分支", confidence: "medium", basedOnRowIds: [], evidenceKeys: [], basis: "当前行信息", abstainReason: "" },
    // 非请求列即使后端异常返回也必须被前端过滤。
    { columnId: "c-anchor", suggestionMd: "不应展示", confidence: "low", basedOnRowIds: [], evidenceKeys: [], basis: "", abstainReason: "" },
  ], { retrievalStatus: "no_evidence" }));
  vi.mocked(batchPatchKnowhowCells).mockResolvedValue([
    { rowId: "r1", columnId: "c-shared", contentMd: "统一检查时钟源", projectionStatus: "pending" },
    { rowId: "r2", columnId: "c-shared", contentMd: "统一检查时钟源", projectionStatus: "pending" },
  ]);
  vi.mocked(patchKnowhowCell).mockResolvedValue({
    rowId: "r1", columnId: "c-own", contentMd: "记录当前分支", projectionStatus: "pending",
  });

  render(<KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t-anchor" initialRowId="r1" />);
  const branchTrigger = await screen.findByRole("button", { name: "智能补全分支 1 的空列" });
  await user.click(branchTrigger);
  await waitFor(() => expect(completeKnowhowRow).toHaveBeenCalledWith(
    "nb-1", "t-anchor", "r1", ["c-shared", "c-own"], expect.any(AbortSignal),
  ));
  const dialog = await screen.findByRole("dialog", { name: /智能补全空列/ });
  expect(within(dialog).queryByText("不应展示")).not.toBeInTheDocument();
  expect(within(dialog).getByText("全库检索未找到可用证据")).toBeInTheDocument();
  expect(within(dialog).queryByText("等待后端事件…")).not.toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /0 步/ })).not.toBeInTheDocument();
  expect(within(dialog).getAllByText("此项未引用知识库证据。")).toHaveLength(2);

  await user.click(within(dialog).getByRole("button", { name: "接受 共同方法 建议" }));
  await waitFor(() => expect(batchPatchKnowhowCells).toHaveBeenCalledTimes(1));
  expect(vi.mocked(batchPatchKnowhowCells).mock.calls[0][2]).toEqual({
    columnId: "c-shared",
    rowIds: ["r1", "r2"],
    contentMd: "统一检查时钟源",
    expectedBefore: ["", ""],
    anchorColumnId: "c-anchor",
    expectedAnchor: ["时钟", "时钟"],
    origin: "llm_complete",
  });

  await user.click(within(dialog).getByRole("button", { name: "接受 分支备注 建议" }));
  await waitFor(() => expect(patchKnowhowCell).toHaveBeenCalledWith(
    "nb-1", "t-anchor", "r1", "c-own", "记录当前分支", "", "llm_complete",
  ));
});

test("anchor 概念矩阵对只读成员隐藏分支补全入口", async () => {
  const anchorDetail = makeDetail();
  anchorDetail.anchorColumnId = "c-problem";
  anchorDetail.columns[0] = { ...anchorDetail.columns[0], role: "anchor" };
  vi.mocked(fetchKnowhowTable).mockResolvedValue(anchorDetail);
  render(<KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit={false} onClose={() => undefined} initialTableId="t1" initialRowId="r1" />);
  await screen.findByRole("dialog", { name: /概念 供电异常/ });
  expect(screen.queryByRole("button", { name: /智能补全分支/ })).not.toBeInTheDocument();
  expect(completeKnowhowRow).not.toHaveBeenCalled();
});

test("只读成员没有智能补全入口，也不会触发生成请求", async () => {
  render(<KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit={false} onClose={() => undefined} initialTableId="t1" initialRowId="r1" />);
  await screen.findByRole("dialog", { name: /供电异常/ });
  expect(screen.queryByRole("button", { name: "智能补全空列" })).not.toBeInTheDocument();
  expect(completeKnowhowRow).not.toHaveBeenCalled();
});

test("接受唯一空列后入口卸载，关闭审阅弹窗把焦点恢复到底层行详情", async () => {
  const user = userEvent.setup();
  const detail = makeDetail();
  detail.rows[0].cells["c-tool"] = "万用表";
  vi.mocked(fetchKnowhowTable).mockResolvedValue(detail);
  vi.mocked(completeKnowhowRow).mockResolvedValue(completionEnvelope([{
    columnId: "c-fix", suggestionMd: "检查输入电压", confidence: "high",
    basedOnRowIds: [], evidenceKeys: [], basis: "当前现象", abstainReason: "",
  }]));
  vi.mocked(patchKnowhowCell).mockResolvedValue({
    rowId: "r1", columnId: "c-fix", contentMd: "检查输入电压", projectionStatus: "pending",
  });

  render(<KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t1" initialRowId="r1" />);
  const rowDrawer = await screen.findByRole("dialog", { name: /供电异常/ });
  const drawerClose = within(rowDrawer).getByTitle("关闭");
  const trigger = within(rowDrawer).getByRole("button", { name: "智能补全空列" });
  await user.click(trigger);
  const completionDialog = await screen.findByRole("dialog", { name: /智能补全空列/ });
  await user.click(within(completionDialog).getByRole("button", { name: "接受 处理方法 建议" }));
  await within(completionDialog).findByText("已填入");
  expect(trigger).not.toBeInTheDocument();

  await user.click(within(completionDialog).getAllByRole("button", { name: "关闭" }).at(-1)!);
  await waitFor(() => expect(drawerClose).toHaveFocus());
});

test("生成请求晚到时不污染已关闭弹窗；技术错误只展示安全中文兜底", async () => {
  const user = userEvent.setup();
  let resolveRequest!: (value: KnowhowRowCompletion) => void;
  vi.mocked(completeKnowhowRow).mockReturnValue(new Promise((resolve) => { resolveRequest = resolve; }));
  render(<KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t1" initialRowId="r1" />);

  await user.click(await screen.findByRole("button", { name: "智能补全空列" }));
  const completionDialog = await screen.findByRole("dialog", { name: /智能补全空列/ });
  expect(within(completionDialog).getByRole("status")).toHaveTextContent("表内参考 + 全库推理检索");
  const requestSignal = vi.mocked(completeKnowhowRow).mock.calls[0][4];
  expect(requestSignal?.aborted).toBe(false);
  await user.click(completionDialog.querySelector<HTMLButtonElement>('button[title="关闭"]')!);
  expect(requestSignal?.aborted).toBe(true);
  resolveRequest(completionEnvelope([]));
  await Promise.resolve();
  expect(screen.queryByRole("dialog", { name: /智能补全空列/ })).not.toBeInTheDocument();

  vi.spyOn(console, "error").mockImplementation(() => undefined);
  vi.mocked(completeKnowhowRow).mockRejectedValue(new Error("upstream secret timeout"));
  await user.click(screen.getByRole("button", { name: "智能补全空列" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("生成建议失败，请稍后重试");
  expect(screen.queryByText(/upstream secret timeout/)).not.toBeInTheDocument();
});

test("StrictMode 探测不重复补全或整行优化的物理模型请求", async () => {
  const user = userEvent.setup();
  vi.mocked(completeKnowhowRow).mockReturnValue(new Promise(() => undefined));
  vi.mocked(optimizeKnowhowCell).mockReturnValue(new Promise(() => undefined));

  render(
    <StrictMode>
      <KnowhowPanel
        notebookId="nb-1"
        apiBase="http://api.test"
        canEdit
        onClose={() => undefined}
        initialTableId="t1"
        initialRowId="r1"
      />
    </StrictMode>,
  );
  const rowDrawer = await screen.findByRole("dialog", { name: /供电异常/ });

  await user.click(within(rowDrawer).getByRole("button", { name: "智能补全空列" }));
  await waitFor(() => expect(completeKnowhowRow).toHaveBeenCalledTimes(1));
  const completionDialog = await screen.findByRole("dialog", { name: /智能补全空列/ });
  await user.click(completionDialog.querySelector<HTMLButtonElement>('button[title="关闭"]')!);

  await user.click(within(rowDrawer).getByRole("button", { name: "优化整行" }));
  await waitFor(() => expect(optimizeKnowhowCell).toHaveBeenCalledTimes(1));
});
