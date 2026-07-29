// PR-2 T6: AskResponse.result_sets 泛化成 kind 判别 union 之后, answer-panel.tsx
// 的结果卡渲染必须按 kind 分派——旧实现把整个数组无条件喂进 KnowhowResultSetCard
// (它假定 `.rows`/`.columns` 存在), 一份 kind="collection" 的行在
// `resultSet.rows.slice(...)` 上会抛 `TypeError: Cannot read properties of
// undefined (reading 'slice')`, 崩穿整个答案面板(且答案持久化后, 历史重开会
// 继续崩)。本文件覆盖:
//   1. kind="collection" 行不再崩, 渲染出清单卡;
//   2. 未知 kind 被跳过(返回 null), 不崩且不渲染空 Knowhow 卡;
//   3. kind="knowhow" 行与既有行为逐字不变(混排场景下的回归);
//   4. coverage 四种硬性渲染规则: 分母未知(null, 禁止 /0)、
//      concurrent_change(终态, 不与普通"部分结果"合并)、
//      "枚举完整、分析部分"复合披露、单源限定;
//   5. image 条目走 AuthedImage(经 fetchInternalAssetBlob 取图), formula 条目走
//      KaTeX 容器(镜像 math-rendering.component.test.tsx 的断言方式)。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, expect, test, vi } from "vitest";

vi.mock("./source-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./source-api")>();
  return {
    ...actual,
    fetchInternalAssetBlob: vi.fn(async () => new Blob(["fake-image-bytes"], { type: "image/png" })),
  };
});

import { AnswerView } from "./answer-panel";
import { fetchInternalAssetBlob } from "./source-api";
import type { AskResponse, TypedCollectionItem, TypedCollectionResult } from "./workspace-model";

beforeAll(() => {
  // jsdom 不实现 blob object URL;AuthedImage 依赖它们创建/回收 <img src>,
  // 测试只关心"渲染到了 img 节点"而不关心真实 URL 的可解析性。
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = vi.fn();
  }
});


function baseAnswer(): AskResponse {
  return {
    answer_id: "answer-collection",
    conversation_id: "conversation-1",
    conclusion: "已列出清单。",
    answer: "已列出清单。",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "reasoning",
  };
}


function elementItem(overrides: Partial<TypedCollectionItem> = {}): TypedCollectionItem {
  return {
    item_id: "el-1",
    source_id: "src-1",
    source_title: "工艺手册",
    element_type: "formula",
    location_label: "第 2 页",
    text: "E=mc^2",
    asset_id: "",
    notebook_id: "nb-1",
    tier: "personal",
    ...overrides,
  };
}


function collectionResult(overrides: Partial<TypedCollectionResult> = {}): TypedCollectionResult {
  return {
    kind: "collection",
    collection: "elements",
    element_kind: "formula",
    object_type: "",
    source_id: "",
    items: [elementItem()],
    coverage: {
      returned_total: 1,
      total: 1,
      complete: true,
      truncated_reason: "",
      overflow_semantics: "",
    },
    synthesis_rows: 0,
    synthesis_complete: null,
    ...overrides,
  };
}


function renderAnswer(answer: AskResponse, extraProps: Record<string, unknown> = {}) {
  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
      {...extraProps}
    />,
  );
}


test("kind=collection 行不再崩溃,渲染出清单卡(此前会在 .rows.slice 上抛 TypeError)", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult()];
  expect(() => renderAnswer(answer)).not.toThrow();
  expect(screen.getByText("已全部列出 1 条")).toBeInTheDocument();
  expect(screen.getByText("公式清单")).toBeInTheDocument();
});


test("未知 kind 被跳过(不崩溃、不渲染空 Knowhow 卡)", () => {
  const answer = baseAnswer();
  // 故意构造一个后端未来可能引入、前端尚不认识的 kind——按 T6 合同必须原样跳过。
  answer.result_sets = [
    { kind: "future_kind" } as unknown as NonNullable<AskResponse["result_sets"]>[number],
  ];
  expect(() => renderAnswer(answer)).not.toThrow();
  expect(screen.queryByText("表格结果")).not.toBeInTheDocument();
  expect(document.querySelector(".answer-knowhow-result")).toBeNull();
  expect(document.querySelector(".answer-collection-result")).toBeNull();
});


test("kind=knowhow 行与既有行为逐字不变(混排场景回归)", () => {
  const answer = baseAnswer();
  answer.result_sets = [
    {
      kind: "knowhow",
      table_id: "table-1",
      title: "方法清单",
      columns: [{ id: "method", name: "方法" }],
      rows: [{ row_id: "row-0", position: 0, cells: { method: "方法 1" } }],
      coverage: {
        total_rows: 1, scanned_rows: 1, returned_rows: 1, complete: true,
        truncated_reason: "", overflow_semantics: "",
      },
    },
    collectionResult(),
  ];
  renderAnswer(answer);
  expect(screen.getByText("完整 1/1")).toBeInTheDocument();
  expect(screen.getByText("方法 1")).toBeInTheDocument();
  // 两种卡片同时出现,互不干扰。
  expect(screen.getByText("公式清单")).toBeInTheDocument();
});


test("coverage: total=null 渲染「总数未知」,绝不写成 /0", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    coverage: {
      returned_total: 30, total: null, complete: false,
      truncated_reason: "budget", overflow_semantics: "explicit_partial",
    },
  })];
  renderAnswer(answer);
  const text = screen.getByText("已列 30 条（总数未知）");
  expect(text).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/30\s*\/\s*0/);
});


test("coverage: concurrent_change 是终态,单独一句话,不与普通「部分结果」合并", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    coverage: {
      returned_total: 12, total: 40, complete: false,
      truncated_reason: "concurrent_change", overflow_semantics: "explicit_partial",
    },
  })];
  renderAnswer(answer);
  expect(screen.getByText("已列 12 条，但资料在枚举期间有变动，无法确认完整")).toBeInTheDocument();
  // 不得额外拼出「已明确标注为部分结果(concurrent_change)」——那是别的 truncated_reason
  // 才用的通用措辞,concurrent_change 是自成一句的终态。
  expect(screen.queryByText(/已明确标注为部分结果/)).not.toBeInTheDocument();
});


test("coverage: 枚举完整、分析部分——两轨分开披露", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    coverage: {
      returned_total: 20, total: 20, complete: true,
      truncated_reason: "", overflow_semantics: "",
    },
    synthesis_rows: 10,
    synthesis_complete: false,
  })];
  renderAnswer(answer);
  expect(screen.getByText("已全部列出 20 条")).toBeInTheDocument();
  expect(screen.getByText("本轮分析基于前 10 条预览")).toBeInTheDocument();
});


test("coverage: synthesis_complete=null 不渲染分析预览行(不是「分析不完整」)", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    synthesis_rows: 0,
    synthesis_complete: null,
  })];
  renderAnswer(answer);
  expect(screen.queryByText(/本轮分析基于前/)).not.toBeInTheDocument();
});


test("source_id 非空时标注「仅限来源」,取首个条目的 source_title", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    source_id: "src-1",
    items: [elementItem({ source_id: "src-1", source_title: "某工艺手册" })],
  })];
  renderAnswer(answer);
  expect(screen.getByText("仅限来源：某工艺手册")).toBeInTheDocument();
});


test("formula 元素条目走 KaTeX 容器渲染", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    items: [elementItem({ element_type: "formula", text: "E=mc^2" })],
  })];
  const { container } = render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );
  expect(container.querySelector(".element-formula .katex")).not.toBeNull();
});


test("image 元素条目走 AuthedImage(经鉴权 blob fetch 取图)", async () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "image",
    items: [elementItem({
      element_type: "image", text: "", asset_id: "asset-1", notebook_id: "nb-1",
    })],
  })];
  renderAnswer(answer);
  const img = await screen.findByRole("img");
  expect(img).toBeInTheDocument();
});


test("code_block 元素条目渲染为 <pre><code>,并可经「查看来源」按钮跳转", async () => {
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "code_block",
    items: [elementItem({
      element_type: "code_block", text: "print('hi')", source_id: "src-9", source_title: "脚本",
    })],
  })];
  renderAnswer(answer, { onOpenSource });
  expect(document.querySelector("pre code")?.textContent).toBe("print('hi')");
  const button = screen.getByRole("button", { name: "查看来源" });
  await user.click(button);
  expect(onOpenSource).toHaveBeenCalledWith("src-9", "el-1");
});


test("table 元素条目的跳转按钮文案是「在来源详情查看完整表格」,不把 text 当 HTML 渲染", () => {
  const onOpenSource = vi.fn();
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "table",
    items: [elementItem({
      element_type: "table", text: "表格摘录: A列 B列", source_id: "src-9", source_title: "报表",
    })],
  })];
  renderAnswer(answer, { onOpenSource });
  expect(screen.getByText("表格摘录: A列 B列")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "在来源详情查看完整表格" })).toBeInTheDocument();
});


test("KG 对象条目使用 result 级 object_type(条目本身没有 per-item object_type)", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    collection: "kg_objects",
    element_kind: "",
    object_type: "concept",
    items: [{
      item_id: "ko-1", name: "锁相环", section_path: "3.2", notebook_id: "nb-1", tier: "personal",
    }],
  })];
  renderAnswer(answer);
  expect(screen.getByText("概念知识对象清单")).toBeInTheDocument();
  expect(screen.getByText("锁相环")).toBeInTheDocument();
  expect(screen.getByText("3.2")).toBeInTheDocument();
});


test("元素条目按来源分组显示(source_title 作为分组小标题)", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    coverage: { returned_total: 2, total: 2, complete: true, truncated_reason: "", overflow_semantics: "" },
    items: [
      elementItem({ item_id: "el-a", source_id: "src-a", source_title: "来源甲", text: "a" }),
      elementItem({ item_id: "el-b", source_id: "src-b", source_title: "来源乙", text: "b" }),
    ],
  })];
  renderAnswer(answer);
  expect(screen.getByText("来源甲")).toBeInTheDocument();
  expect(screen.getByText("来源乙")).toBeInTheDocument();
});


test("初始显示 20 行 + 客户端展开全部(对齐既有结果卡口径)", async () => {
  const user = userEvent.setup();
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "table",
    coverage: { returned_total: 25, total: 25, complete: true, truncated_reason: "", overflow_semantics: "" },
    items: Array.from({ length: 25 }, (_, index) => elementItem({
      item_id: `el-${index}`, element_type: "table", text: `条目 ${index + 1}`,
    })),
  })];
  renderAnswer(answer);
  expect(screen.getByText("条目 20")).toBeInTheDocument();
  expect(screen.queryByText("条目 21")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "展开全部已加载的 25 条" }));
  expect(screen.getByText("条目 25")).toBeInTheDocument();
});


// ---------------------------------------------------------------------------
// 双评审 P1-1:跨库条目收口。挂载公共参考库不等于获得该库的直接成员权限——
// `GET /sources/{id}` 与资产端点都是 owner∪member 口径,前端不能替用户猜权限去
// 直连另一个库的资源。跨库条目(item.notebook_id 非空且 ≠ 当前 notebookId)必须
// 收敛成"只读展示":不发图片鉴权请求、不提供「查看来源」跳转,只标注所属参考库。
// ---------------------------------------------------------------------------

test("跨库图片条目:不发鉴权请求,渲染降级占位而不是 AuthedImage", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "image",
    items: [elementItem({
      element_type: "image", asset_id: "asset-1", notebook_id: "base-1", text: "",
    })],
  })];
  renderAnswer(answer);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.getByText("图片来自参考库，暂不支持预览")).toBeInTheDocument();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
});

test("跨库元素条目:不渲染「查看来源」按钮", () => {
  const onOpenSource = vi.fn();
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "code_block",
    items: [elementItem({
      element_type: "code_block", text: "print(1)", notebook_id: "base-1",
      source_id: "src-9", source_title: "别的库的脚本",
    })],
  })];
  renderAnswer(answer, { onOpenSource });
  expect(screen.queryByRole("button", { name: "查看来源" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "在来源详情查看完整表格" })).not.toBeInTheDocument();
});

test("跨库条目:库名可解析时标注「来自参考库《名》」", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    items: [elementItem({ notebook_id: "base-1" })],
  })];
  renderAnswer(answer, { notebookNames: { "base-1": "工艺基础库" } });
  expect(screen.getByText("来自参考库《工艺基础库》")).toBeInTheDocument();
});

test("跨库条目:库名解析不到时退回 tier 徽章文案,绝不吐裸 notebook id", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    items: [elementItem({ notebook_id: "base-unresolvable-id", tier: "base" })],
  })];
  renderAnswer(answer, { notebookNames: {} });
  expect(screen.getByText("公共知识库")).toBeInTheDocument();
  expect(screen.queryByText(/base-unresolvable-id/)).not.toBeInTheDocument();
});

test("跨库 KG 对象条目:同样标注所属参考库", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    collection: "kg_objects",
    element_kind: "",
    object_type: "concept",
    items: [{
      item_id: "ko-1", name: "锁相环", section_path: "3.2",
      notebook_id: "base-1", tier: "base",
    }],
  })];
  renderAnswer(answer, { notebookNames: { "base-1": "工艺基础库" } });
  expect(screen.getByText("来自参考库《工艺基础库》")).toBeInTheDocument();
});

test("本库条目(notebook_id 等于当前 notebookId)回归:按钮/图片/标注都不受影响", async () => {
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "code_block",
    items: [elementItem({
      element_type: "code_block", text: "print(1)", notebook_id: "nb-1",
      source_id: "src-9", source_title: "本库脚本",
    })],
  })];
  renderAnswer(answer, { onOpenSource });
  expect(screen.queryByText(/来自参考库/)).not.toBeInTheDocument();
  const button = screen.getByRole("button", { name: "查看来源" });
  await user.click(button);
  expect(onOpenSource).toHaveBeenCalledWith("src-9", "el-1");
});

test("本库图片条目回归:notebook_id 为空(旧数据/兜底)时仍按当前笔记本取图,不是跨库", async () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    element_kind: "image",
    items: [elementItem({
      element_type: "image", asset_id: "asset-1", notebook_id: "", text: "",
    })],
  })];
  renderAnswer(answer);
  await screen.findByRole("img");
  expect(fetchInternalAssetBlob).toHaveBeenCalled();
});


// ---------------------------------------------------------------------------
// 双评审 P2-4/P2-5:coverage 文案与分支顺序的补充用例
// ---------------------------------------------------------------------------

test("synthesis_rows===0 且 synthesis_complete===false:措辞是「本轮分析未包含该清单」,不是「基于前 0 条预览」", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    coverage: { returned_total: 20, total: 20, complete: true, truncated_reason: "", overflow_semantics: "" },
    synthesis_rows: 0,
    synthesis_complete: false,
  })];
  renderAnswer(answer);
  expect(screen.getByText("本轮分析未包含该清单")).toBeInTheDocument();
  expect(screen.queryByText(/基于前 0 条预览/)).not.toBeInTheDocument();
});

test("coverage 分支顺序:concurrent_change + total=null 组合仍优先显示终态整句", () => {
  const answer = baseAnswer();
  answer.result_sets = [collectionResult({
    coverage: {
      returned_total: 15, total: null, complete: false,
      truncated_reason: "concurrent_change", overflow_semantics: "explicit_partial",
    },
  })];
  renderAnswer(answer);
  expect(screen.getByText("已列 15 条，但资料在枚举期间有变动，无法确认完整")).toBeInTheDocument();
  expect(screen.queryByText("已列 15 条（总数未知）")).not.toBeInTheDocument();
});
