// GapSuggestionsPanel（ask.gap_consult，X9 PR-A T3）—— 站外来源建议披露区块。
//
// 覆盖：
//   ① 空建议不渲染区块；
//   ② 默认折叠，展开后能看到免责句；
//   ③ 不占用引用编号、不进来源分布计数（通过完整 AnswerView 集成断言）；
//   ④ 点击导入立即禁用并换文案（在 onImport resolve 之前断言）；
//   ⑤ 成功固化「已导入」，不可再点；
//   ⑥ 失败文案持久显示，不像即逝 toast 那样自动消失；
//   ⑦ 没有 onImport 时一颗导入按钮都不出，但披露本身仍在；
//   ⑧ 持久化（JSON 往返）的历史回答重新打开时同样渲染这份披露。
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import { GapSuggestionsPanel } from "../../app/answer-gap-suggestions";
import type { AskResponse, GapSuggestion } from "../../app/workspace-model";

function suggestion(overrides: Partial<GapSuggestion> = {}): GapSuggestion {
  return {
    title: "站外相关文档",
    url: "https://example.com/doc.pdf",
    summary: "一句摘要",
    source_label: "示例来源",
    ...overrides,
  };
}

// AnswerView 的必填 prop 集合，照 answer-retrieval-scope.component.test.tsx 的
// view() helper 同款搭法：只填渲染所必需的，可选交互回调按各条用例需要单独传。
function baseAnswer(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    answer_id: "ans-1",
    conversation_id: "conv-1",
    conclusion: "结论见正文 [k1]。",
    answer: "结论见正文 [k1]。",
    grounded: true,
    anchors: [{
      key: "k1",
      object_id: "obj-1",
      object_type: "claim",
      label: "结论",
      name: "结论",
      source_title: "笔记本内文档",
      location_label: "第 1 段",
      source_id: "src-1",
      element_id: "el-1",
      tier: "personal",
    }] as unknown as AskResponse["anchors"],
    related_knowledge: [],
    citations: [],
    llm_mode: "reasoning",
    ...overrides,
  };
}

function renderAnswerView(answer: AskResponse, onImportGapSuggestion?: () => Promise<{ ok: boolean; message?: string }>) {
  return render(
    <AnswerView
      answer={answer}
      buildingScaleIndex={false}
      feedbackSent=""
      memorySaved={false}
      notebookId="nb-1"
      notebookNames={{}}
      onImportGapSuggestion={onImportGapSuggestion}
      scaleIndexStatus={null}
    />,
  );
}


test("没有建议时区块不渲染", () => {
  const { container } = render(
    <GapSuggestionsPanel suggestions={[]} onImport={vi.fn()} />,
  );
  expect(container.querySelector(".answer-gap-consult")).toBeNull();
});


test("默认折叠，展开后能看到免责句", async () => {
  const user = userEvent.setup();
  const { container } = render(<GapSuggestionsPanel suggestions={[suggestion()]} onImport={vi.fn()} />);

  const summary = screen.getByText("站外来源建议 · 1 条");
  expect(summary.closest("details")).not.toHaveAttribute("open");

  await user.click(summary);
  const disclaimer = screen.getByText(
    "以下结果来自笔记本之外，没有参与本次回答，也不会被引用。导入后才会进入这个笔记本。",
  );
  expect(disclaimer).toBeVisible();

  // 免责句必须排在建议清单之前：这句话的存在意义是"点任何链接之前先看到它"，
  // 排在清单下面就等于用户已经先扫过标题/摘要/导入按钮才读到这句免责声明。
  const list = container.querySelector(".answer-gap-consult-list");
  expect(list).not.toBeNull();
  expect(
    disclaimer.compareDocumentPosition(list!) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});


// 真机事故的同款教训（见 answer-retrieval-scope 的先例）：非证据区块必须结构性地
// 够不着引用编号与来源分布统计，不能靠约定。
test("不占用引用编号，也不计入来源分布统计", async () => {
  const user = userEvent.setup();
  const answer = baseAnswer({
    gap_suggestions: [suggestion()],
  });
  renderAnswerView(answer, vi.fn());

  // 只有一条来源被真正引用（个人知识库 1 条），站外建议不应该把这个数顶高，
  // 也不该在「公共」那一格里冒出来。
  expect(screen.getByText(/来源 · 个人 1/)).toBeInTheDocument();
  expect(screen.queryByText(/公共/)).not.toBeInTheDocument();

  // 站外建议从不进 anchors/citations，答案正文里也就绝不会有第二个引用编号。
  expect(screen.queryByRole("button", { name: "[2]" })).not.toBeInTheDocument();

  // 打开披露区块本身：标题文字只出现在这个折叠块里，不出现在正文/引用浮层里。
  await user.click(screen.getByText("站外来源建议 · 1 条"));
  expect(screen.getByText("站外相关文档")).toBeVisible();
});


test("点击导入立即禁用并换文案，在 onImport resolve 之前", async () => {
  const user = userEvent.setup();
  let resolveImport: (outcome: { ok: boolean }) => void = () => undefined;
  const onImport = vi.fn(
    () => new Promise<{ ok: boolean }>((resolve) => { resolveImport = resolve; }),
  );
  render(<GapSuggestionsPanel suggestions={[suggestion()]} onImport={onImport} />);

  await user.click(screen.getByText("站外来源建议 · 1 条"));
  await user.click(screen.getByRole("button", { name: "导入" }));

  const busy = screen.getByRole("button", { name: "导入中…" });
  expect(busy).toBeDisabled();
  expect(onImport).toHaveBeenCalledWith("https://example.com/doc.pdf");

  // 收尾：把挂起的 promise 结清，避免测试留下未处理的 rejection/悬挂 act 警告。
  resolveImport({ ok: true });
  await screen.findByRole("button", { name: "已导入" });
});


test("导入成功后固化为已导入，不可再点", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: true });
  render(<GapSuggestionsPanel suggestions={[suggestion()]} onImport={onImport} />);

  await user.click(screen.getByText("站外来源建议 · 1 条"));
  await user.click(screen.getByRole("button", { name: "导入" }));

  const done = await screen.findByRole("button", { name: "已导入" });
  expect(done).toBeDisabled();
  expect(onImport).toHaveBeenCalledTimes(1);
});


test("导入失败的提示持久显示，不会像即逝 toast 那样自动消失", async () => {
  vi.useFakeTimers();
  try {
    const onImport = vi.fn().mockResolvedValue({ ok: false, message: "这不是一个可解析的直链" });
    render(<GapSuggestionsPanel suggestions={[suggestion()]} onImport={onImport} />);

    fireEvent.click(screen.getByText("站外来源建议 · 1 条"));
    fireEvent.click(screen.getByRole("button", { name: "导入" }));

    await vi.waitFor(() => expect(screen.getByText("这不是一个可解析的直链")).toBeInTheDocument());
    // 按钮回到可点（失败不像 busy/done 那样锁死，允许用户重试）。
    expect(screen.getByRole("button", { name: "导入" })).toBeEnabled();

    // act 包裹是承重的:若组件哪天悄悄加一段定时自动清除,setTimeout 回调里的
    // setState 需要被 flush 掉这次断言才靠得住——不包 act 时 React 的调度可能
    // 落在这次断言之后才提交,DOM 还是旧的,让一次真实回归读成误通过。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(screen.getByText("这不是一个可解析的直链")).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});


test("没有 onImport 时不出导入按钮，但披露本身仍在", async () => {
  const user = userEvent.setup();
  render(<GapSuggestionsPanel suggestions={[suggestion()]} />);

  await user.click(screen.getByText("站外来源建议 · 1 条"));
  expect(screen.getByText("站外相关文档")).toBeVisible();
  expect(screen.queryByRole("button", { name: "导入" })).not.toBeInTheDocument();
});


test("持久化(JSON 往返)的历史回答重新打开时同样渲染这份披露", async () => {
  const user = userEvent.setup();
  // 模拟从存储里重新读回的历史 payload —— 没有任何前端在写入时加工过的字段，
  // 逐字是后端 wire 形状(gap_suggestions 数组,4 个字段)的一次 JSON 往返。
  const persisted = JSON.parse(JSON.stringify(
    baseAnswer({ gap_suggestions: [suggestion({ summary: "", source_label: "" })] }),
  )) as AskResponse;

  renderAnswerView(persisted, vi.fn());

  const summary = screen.getByText("站外来源建议 · 1 条");
  await user.click(summary);
  expect(screen.getByText("站外相关文档")).toBeVisible();
});
