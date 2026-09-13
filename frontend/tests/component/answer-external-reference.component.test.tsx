// 外部证据引用卡（`ask.reflect_action`，设计文档 §6.3/§七/§九 不变量 3 与 8）。
//
// 库外材料以 `object_type === "external"` / `tier === "external"` / 非空 `url` /
// 空 `source_id`/`element_id` 的锚点下发，进答案、可被 `[k]` 引用。引用卡因此长得
// 和库内证据不一样，这份用例逐条钉住那些不一样的地方：
//   ① 卡头有「外部」标记，且**不出**「知识图谱」与「查看原文」——前者绑的
//      `object_id` 是核心铸的 `ext:…` 键，在图谱里根本不存在（不显式排除的话按钮
//      会渲染成可点、点了定位到空）；后者的 source_id 恒为空。
//   ② 「打开链接」只对 http/https 渲染（§九 不变量 8：前端是同一把闸，不假设后端
//      净化过），且必须带 target=_blank + rel=noopener noreferrer。
//   ③ 「导入为来源」复用站外来源建议同一条通道与同一份逐行状态机（长任务按钮红线：
//      按下即禁用、进行中文案、成功冻结、失败原地持久显示，不发 toast）。
//   ④ 「已导入」跨浮层开合存活——引用卡是会被反复关掉重开的浮层，状态若住在卡片
//      内部，用户重新点开就会导入第二次。
//   ⑤ 来源分布徽章的第三格「外部 K」只在真有库外引用时出现；没有时那句话逐字不变。
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";

const EXTERNAL_URL = "https://example.org/paper?id=7";

function externalAnchor(overrides: Record<string, unknown> = {}) {
  return {
    key: "k2",
    // 核心按 run 内序号铸的键（设计文档 §6.1）。它是一个合法非空的 object_id,
    // 所以「知识图谱」按钮的 canLocateInGraph 判据**不会**自动禁用它——必须靠
    // isExternalReference 显式排除,这正是本文件第一条用例的承重点。
    object_id: "ext:acme.ieee:1",
    object_type: "external",
    label: "IEEE Xplore · 某篇论文",
    name: "某篇论文",
    snippet: "库外摘录一段。",
    source_title: "某篇论文",
    location_label: "§3.2",
    source_id: "",
    element_id: "",
    tier: "external",
    url: EXTERNAL_URL,
    ...overrides,
  };
}

function answerFixture(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    answer_id: "ans-1",
    conversation_id: "conv-1",
    conclusion: "库内依据 [k1]，库外材料 [k2]。",
    answer: "库内依据 [k1]，库外材料 [k2]。",
    grounded: true,
    anchors: [
      {
        key: "k1",
        object_id: "obj-1",
        object_type: "claim",
        label: "库内结论",
        name: "库内结论",
        source_title: "笔记本内文档",
        location_label: "第 1 段",
        source_id: "src-1",
        element_id: "el-1",
        tier: "personal",
      },
      externalAnchor(),
    ],
    related_knowledge: [],
    citations: [],
    llm_mode: "reasoning",
    ...overrides,
  } as unknown as AskResponse;
}

function renderAnswer(
  answer: AskResponse,
  props: Partial<React.ComponentProps<typeof AnswerView>> = {},
) {
  return render(
    <AnswerView
      answer={answer}
      buildingScaleIndex={false}
      feedbackSent=""
      memorySaved={false}
      notebookId="nb-1"
      notebookNames={{}}
      onOpenKnowledgeGraph={vi.fn()}
      onOpenSource={vi.fn()}
      scaleIndexStatus={null}
      {...props}
    />,
  );
}

/** 打开某条引用的浮层（点正文里的编号按钮）。 */
async function openReference(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getByRole("button", { name: label }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
}


test("外部引用卡：出「外部」标记与「打开链接」，不出「知识图谱」与「查看原文」", async () => {
  const user = userEvent.setup();
  renderAnswer(answerFixture(), { onImportGapSuggestion: vi.fn() });

  // 正向对照：同一份答案里的**库内**引用照常有那两颗按钮。没有这条，把它们整体
  // 删掉也能让下面的 queryBy 通过。
  await openReference(user, "[1]");
  expect(screen.getByRole("button", { name: "知识图谱" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看原文" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /打开链接/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "导入为来源" })).not.toBeInTheDocument();

  await user.keyboard("{Escape}");
  await openReference(user, "[2]");
  const card = screen.getByRole("dialog");
  expect(card).toHaveTextContent("外部");
  expect(screen.queryByRole("button", { name: "知识图谱" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看原文" })).not.toBeInTheDocument();

  const link = screen.getByRole("link", { name: /打开链接/ });
  expect(link).toHaveAttribute("href", EXTERNAL_URL);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(screen.getByRole("button", { name: "导入为来源" })).toBeInTheDocument();
});


// §九 不变量 8 的前端一侧。宿主净化期本应丢掉整条,但前端不假设它跑过——这里是
// 同一把闸。不合格就整个不渲染链接(而不是渲染一个点不动的按钮),引用卡其余内容
// 照常可读。
test("非 http(s) 的 url 一律不渲染成链接，也不给导入入口", async () => {
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
    "file:///etc/passwd",
    " https://example.org/leading-space",
    "//example.org/protocol-relative",
    "",
  ]) {
    const user = userEvent.setup();
    const view = renderAnswer(
      answerFixture({ anchors: [externalAnchor({ url })] as AskResponse["anchors"] }),
      { onImportGapSuggestion: vi.fn() },
    );
    await user.click(screen.getByRole("button", { name: "[1]" }));
    // 卡片本身照常在，只是没有出口。
    expect(screen.getByRole("dialog")).toHaveTextContent("外部");
    expect(screen.queryByRole("link", { name: /打开链接/ }), url).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "导入为来源" }), url).not.toBeInTheDocument();
    view.unmount();
  }
});


test("http:// 与大小写变体照常渲染（只挡非 http(s) scheme，不误杀合法链接）", async () => {
  for (const url of ["http://example.org/a", "HTTPS://example.org/b"]) {
    const user = userEvent.setup();
    const view = renderAnswer(
      answerFixture({ anchors: [externalAnchor({ url })] as AskResponse["anchors"] }),
    );
    await user.click(screen.getByRole("button", { name: "[1]" }));
    expect(screen.getByRole("link", { name: /打开链接/ })).toHaveAttribute("href", url);
    view.unmount();
  }
});


test("导入按钮：按下即禁用并换文案，在 onImport resolve 之前", async () => {
  const user = userEvent.setup();
  let resolveImport: (outcome: { ok: boolean }) => void = () => undefined;
  const onImport = vi.fn(
    () => new Promise<{ ok: boolean }>((resolve) => { resolveImport = resolve; }),
  );
  renderAnswer(answerFixture(), { onImportGapSuggestion: onImport });

  await openReference(user, "[2]");
  await user.click(screen.getByRole("button", { name: "导入为来源" }));

  expect(screen.getByRole("button", { name: "导入中…" })).toBeDisabled();
  expect(onImport).toHaveBeenCalledWith(EXTERNAL_URL);

  resolveImport({ ok: true });
  const done = await screen.findByRole("button", { name: "已导入" });
  expect(done).toBeDisabled();
  expect(onImport).toHaveBeenCalledTimes(1);
});


test("导入失败的提示原地持久显示，按钮回到可点以便重试（绝不发 toast）", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: false, message: "这不是一个可解析的直链" });
  renderAnswer(answerFixture(), { onImportGapSuggestion: onImport });

  await openReference(user, "[2]");
  await user.click(screen.getByRole("button", { name: "导入为来源" }));

  const message = await screen.findByText("这不是一个可解析的直链");
  // 「结果落在按钮自身或紧邻处」——失败说明必须与那颗按钮同处一张卡片，
  // 不是页面顶部横幅（AGENTS.md「Interactive feedback」）。
  expect(screen.getByRole("dialog")).toContainElement(message);
  expect(screen.getByRole("button", { name: "导入为来源" })).toBeEnabled();
});


// 引用卡是随点外部/滚动/Esc 卸载的浮层。状态若住在卡片内部，「已导入」会在关掉浮层
// 的一瞬间蒸发，用户重新点开同一条引用看到的又是可点的「导入为来源」——于是导入第
// 二次（后端 POST /sources/url 没有单飞守卫）。
test("「已导入」跨浮层关闭重开仍然冻结，不会退回可点状态", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: true });
  renderAnswer(answerFixture(), { onImportGapSuggestion: onImport });

  await openReference(user, "[2]");
  await user.click(screen.getByRole("button", { name: "导入为来源" }));
  await screen.findByRole("button", { name: "已导入" });

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  await openReference(user, "[2]");
  expect(screen.getByRole("button", { name: "已导入" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "导入为来源" })).not.toBeInTheDocument();
  expect(onImport).toHaveBeenCalledTimes(1);
});


test("只读工作区（不传导入回调）不出导入按钮，但「打开链接」仍在", async () => {
  const user = userEvent.setup();
  renderAnswer(answerFixture());

  await openReference(user, "[2]");
  expect(screen.queryByRole("button", { name: "导入为来源" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: /打开链接/ })).toBeInTheDocument();
});


test("文档数量满额时导入按钮置灰并写明原因，点击零调用", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: true });
  renderAnswer(answerFixture(), {
    onImportGapSuggestion: onImport,
    importGapSuggestionDisabledReason: "已达该笔记本的文档数量上限，无法继续添加文档。",
  });

  await openReference(user, "[2]");
  const button = screen.getByRole("button", { name: "导入为来源" });
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute("title", "已达该笔记本的文档数量上限，无法继续添加文档。");

  fireEvent.click(button);
  expect(onImport).not.toHaveBeenCalled();
});


test("来源分布徽章：有库外引用时追加「外部 K」，个人格不把它算进去", () => {
  renderAnswer(answerFixture());
  const badge = screen.getByTitle(/本次引用的来源分布/);
  expect(badge).toHaveTextContent("来源 · 个人 1 · 外部 1");
});


// 关闭态零差异（§九 不变量 6 的界面侧同款纪律）：没有库外引用的回答，这枚徽章
// 逐字等于接入前——文案与 title 都是。
test("没有库外引用时徽章文案与 title 逐字不变", () => {
  renderAnswer(answerFixture({
    conclusion: "库内依据 [k1]。",
    answer: "库内依据 [k1]。",
    anchors: [{
      key: "k1",
      object_id: "obj-1",
      object_type: "claim",
      label: "库内结论",
      name: "库内结论",
      source_title: "笔记本内文档",
      location_label: "第 1 段",
      source_id: "src-1",
      element_id: "el-1",
      tier: "personal",
    }] as unknown as AskResponse["anchors"],
  }));
  const badge = screen.getByTitle("本次引用的来源分布（个人知识库 / 公共知识库）");
  expect(badge).toHaveTextContent("来源 · 个人 1");
  expect(badge).not.toHaveTextContent("外部");
});


// citation 回退列表（答案里一个 `[kN]` 标记都没有，模型写的是裸编号 `[1]`）：这条
// 路上没有 anchor，也就没有 object_type——引用卡靠 tier 把「外部」补齐，否则库外
// 条目会以一张与库内一模一样的卡片示人。
test("citation 回退路径上的外部引用同样出「外部」标记与两个出口", async () => {
  const user = userEvent.setup();
  renderAnswer(
    answerFixture({
      conclusion: "库外材料 [1]。",
      answer: "库外材料 [1]。",
      anchors: [],
      citations: [{
        label: "IEEE Xplore · 某篇论文",
        source_id: "",
        element_id: "",
        location_label: "§3.2",
        quoted_span: "库外摘录一段。",
        tier: "external",
        url: EXTERNAL_URL,
      }] as unknown as AskResponse["citations"],
    }),
    { onImportGapSuggestion: vi.fn() },
  );

  await openReference(user, "[1]");
  expect(screen.getByRole("dialog")).toHaveTextContent("外部");
  expect(screen.queryByRole("button", { name: "查看原文" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: /打开链接/ })).toHaveAttribute("href", EXTERNAL_URL);
  expect(screen.getByRole("button", { name: "导入为来源" })).toBeInTheDocument();
});


// tier 徽章的 title 从**真实 DOM** 上读，不在测试里重写一份产品表达式：那种写法把
// 产品代码退回 `tier === "base" ? "公共知识库" : "个人知识库"` 的三元式也照样绿。
// 库内两个 tier 的文案必须逐字不变，而外部证据根本不该出现这枚徽章（头上那枚类型
// 标记已经写着「外部」）。
test("库内引用的 tier 徽章 title 逐字不变（真实 DOM）", async () => {
  const user = userEvent.setup();
  const view = renderAnswer(answerFixture());
  await openReference(user, "[1]");
  expect(screen.getByTitle("来自个人知识库")).toHaveTextContent("个人知识库");
  view.unmount();

  const user2 = userEvent.setup();
  renderAnswer(answerFixture({
    anchors: [
      { ...answerFixture().anchors![0], tier: "base" },
      externalAnchor(),
    ] as unknown as AskResponse["anchors"],
  }));
  await openReference(user2, "[1]");
  expect(screen.getByTitle("来自公共知识库")).toHaveTextContent("公共知识库");
});

test("查得到库名时 tier 徽章 title 带库名，文案逐字不变（真实 DOM）", async () => {
  const user = userEvent.setup();
  renderAnswer(
    answerFixture({
      anchors: [
        { ...answerFixture().anchors![0], notebook_id: "nb-base" },
        externalAnchor(),
      ] as unknown as AskResponse["anchors"],
    }),
    { notebookNames: { "nb-base": "模拟笔记" } },
  );
  await openReference(user, "[1]");
  expect(screen.getByTitle("来自「模拟笔记」（个人知识库）")).toBeInTheDocument();
});

test("外部引用卡不叠 tier 徽章，也不会拼出「来自个人知识库」", async () => {
  const user = userEvent.setup();
  renderAnswer(answerFixture());
  await openReference(user, "[2]");
  const card = screen.getByRole("dialog");
  expect(card.querySelector(".tier-badge")).toBeNull();
  expect(within(card).queryByTitle(/来自个人知识库|来自公共知识库/)).not.toBeInTheDocument();
  expect(card).toHaveTextContent("外部");
});


// 同一个库外链接既可能出现在站外来源建议清单里，又可能作为外部证据被引用。两处各
// 持一份状态就意味着同一个 URL 能被导入两次——后端 POST /sources/url 既没有单飞守卫、
// 也不按 URL 去重。AnswerView 因此只建**一个**按 URL 键控的 controller，两个面共用。
test("同一 URL 在 gap 面板与引用卡之间共享「已导入」终态，onImport 只调一次", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: true });
  renderAnswer(
    answerFixture({
      gap_suggestions: [{
        title: "同一篇论文",
        url: EXTERNAL_URL,
        summary: "一句摘要",
        source_label: "IEEE Xplore",
      }],
    } as Partial<AskResponse>),
    { onImportGapSuggestion: onImport },
  );

  // ① 在站外来源建议清单里导入。
  await user.click(screen.getByText("站外来源建议 · 1 条"));
  await user.click(screen.getByRole("button", { name: "导入" }));
  await screen.findByRole("button", { name: "已导入" });

  // ② 引用卡上同一个 URL 直接是终态，连按钮文案都已经是「已导入」。
  await openReference(user, "[2]");
  const card = screen.getByRole("dialog");
  expect(within(card).getByRole("button", { name: "已导入" })).toBeDisabled();
  expect(within(card).queryByRole("button", { name: "导入为来源" })).not.toBeInTheDocument();

  expect(onImport).toHaveBeenCalledTimes(1);
  expect(onImport).toHaveBeenCalledWith(EXTERNAL_URL);
});

test("反向也成立：先在引用卡导入，站外来源建议清单里同一条随之冻结", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: true });
  renderAnswer(
    answerFixture({
      gap_suggestions: [{
        title: "同一篇论文",
        url: EXTERNAL_URL,
        summary: "",
        source_label: "",
      }],
    } as Partial<AskResponse>),
    { onImportGapSuggestion: onImport },
  );

  await openReference(user, "[2]");
  await user.click(screen.getByRole("button", { name: "导入为来源" }));
  // 查询限定在浮层内：折叠着的建议清单那一行**也会**同时变成「已导入」（这正是本
  // 用例要证明的事），不限定范围就会撞上「找到多个同名按钮」。
  await within(screen.getByRole("dialog")).findByRole("button", { name: "已导入" });
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  await user.click(screen.getByText("站外来源建议 · 1 条"));
  expect(screen.getByRole("button", { name: "已导入" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "导入" })).not.toBeInTheDocument();
  expect(onImport).toHaveBeenCalledTimes(1);
});

// 空转保护：不同 URL 必须各算各的，否则上面两条会被一个「一处导入、全部冻结」的
// 错误实现骗过去。
test("不同 URL 各自独立：导入建议清单那条，引用卡的另一条仍可点", async () => {
  const user = userEvent.setup();
  const onImport = vi.fn().mockResolvedValue({ ok: true });
  renderAnswer(
    answerFixture({
      gap_suggestions: [{
        title: "另一篇",
        url: "https://example.org/other",
        summary: "",
        source_label: "",
      }],
    } as Partial<AskResponse>),
    { onImportGapSuggestion: onImport },
  );

  await user.click(screen.getByText("站外来源建议 · 1 条"));
  await user.click(screen.getByRole("button", { name: "导入" }));
  await screen.findByRole("button", { name: "已导入" });

  await openReference(user, "[2]");
  const card = screen.getByRole("dialog");
  expect(within(card).getByRole("button", { name: "导入为来源" })).toBeEnabled();
});
