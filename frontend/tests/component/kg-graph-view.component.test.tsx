// 「知识图谱」全屏视图从 page.tsx 抽出来之后的组件覆盖（PR-5 分片 3）。page.tsx 整体
// 不可直接渲染，画布四态 / 待确认合并 / 概念详情这些呈现判据以前只能靠源码守卫近似
// 钉住；现在可以真渲染了。
//
// react-force-graph-2d 依赖 canvas + window，jsdom 里跑不起来，所以在 next/dynamic
// 这一层桩掉：既拿到了 ForceGraph2D 真正收到的 props，也顺带把 `{ ssr: false }`
// 这条（服务端渲染会直接炸）钉在测试里。
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import type { KgOccurrence, PendingMerge } from "../../app/workspace-model";

const dynamicOptions: unknown[] = [];
const forceGraphProps: Array<Record<string, unknown>> = [];

vi.mock("next/dynamic", () => ({
  default: (_loader: unknown, options: unknown) => {
    dynamicOptions.push(options);
    return function ForceGraph2DStub(props: Record<string, unknown>) {
      forceGraphProps.push(props);
      return <div data-testid="force-graph" />;
    };
  },
}));

const { KgGraphView } = await import("../../app/kg-graph-view");

type Props = Parameters<typeof KgGraphView>[0];

function graphView(overrides: Partial<Props["kgGraph"]> = {}): Props["kgGraph"] {
  const base = {
    buildingKg: false,
    conceptDetail: null,
    conceptDetailGeneration: 0,
    conceptMembersLoadError: false,
    conceptMembersLoadingMore: false,
    decidingMerge: null,
    deleteResult: null,
    deleting: false,
    graph: null,
    merged: null,
    nodeContext: null,
    pendingMerges: [],
    rangeBusy: false,
    rangeLimit: 0,
    rebuilding: false,
    relinking: false,
    reviewAllJob: null,
    reviewAllRunning: false,
    reviewAllStarting: false,
    reviewBusy: false,
    search: "",
    searchBusy: false,
    selectedNodeId: null,
    selectedTypes: [],
    status: null,
  } satisfies Props["kgGraph"];
  return { ...base, ...overrides };
}

const noop = () => undefined;

function renderView(overrides: Partial<Props> = {}) {
  const props: Props = {
    kgGraph: graphView(),
    fgData: { nodes: [], links: [], searchHitCount: 0 },
    kgCanvas: "graph",
    kgSearching: false,
    kgDenseView: false,
    kgSize: { width: 720, height: 560 },
    kgTypeCounts: [],
    kgNodeGroups: [],
    selectedKgNode: null,
    selectedKgEdges: [],
    relatedNodeGroups: [],
    kgCanvasRef: { current: null },
    kgGraphRef: { current: null },
    kgDetailRef: { current: null },
    readOnlyWorkspace: false,
    currentNotebookId: "nb-1",
    kgReady: true,
    baseKgAvailable: false,
    scaleIndexStatus: null,
    openKgAnalysis: noop,
    openKgSchemas: noop,
    closeKgView: noop,
    relinkFromKgView: noop,
    confirmRefreshUnifiedKg: noop,
    startKgRebuild: noop,
    confirmDeleteKg: noop,
    handleKgSearchChange: noop,
    changeKgRange: noop,
    toggleKgType: noop,
    reviewPendingMerges: noop,
    reviewAllMerges: noop,
    decideMerge: noop,
    fitKgGraphView: noop,
    runScaleIndexOp: noop,
    onClearTypes: noop,
    onLoadMoreConceptMembers: noop,
    onSelectCanvasNode: noop,
    onSelectOverviewNode: noop,
    ...overrides,
  };
  return { ...render(<KgGraphView {...props} />), props };
}

beforeEach(() => {
  forceGraphProps.length = 0;
});


test("画布四态各渲染自己的曲面，只有 graph 态才挂 ForceGraph2D", () => {
  const { unmount } = renderView({ kgCanvas: "loading" });
  expect(screen.getByText("加载中…")).toBeTruthy();
  expect(screen.queryByTestId("force-graph")).toBeNull();
  unmount();

  const building = renderView({ kgCanvas: "building" });
  expect(screen.getByText("图谱索引构建中，首次构建大库可能需要几分钟…")).toBeTruthy();
  expect(screen.queryByTestId("force-graph")).toBeNull();
  building.unmount();

  // W4 T3b 的第四态：大库这一次打开**不会**在后台生成预览，文案必须说明这一点，
  // 而不是让用户以为再等一会儿就有（那是上面的 building 态）。
  const unavailable = renderView({ kgCanvas: "unavailable" });
  expect(screen.getByText("库规模较大，图谱预览将在下一次索引构建后可用")).toBeTruthy();
  expect(screen.getByText("这一次打开不会在后台生成预览；其余功能不受影响")).toBeTruthy();
  expect(screen.queryByTestId("force-graph")).toBeNull();
  unavailable.unmount();

  // empty 态按成因各说一句，且**先看图本身是不是空的**：删除知识图谱或从没整理过的库
  // 既没搜索也没过滤；即便还有一个搜索词挂着，「清空搜索」也兑现不了。
  const empty = renderView({ kgCanvas: "empty" });
  expect(screen.getByText("还没有知识图谱内容。整理来源后会显示在这里。")).toBeTruthy();
  expect(screen.queryByText("没有匹配的节点。清空搜索后可查看完整图谱。")).toBeNull();
  expect(screen.queryByTestId("force-graph")).toBeNull();
  empty.unmount();

  const emptyGraphWhileSearching = renderView({
    kgCanvas: "empty",
    kgSearching: true,
    kgGraph: graphView({ graph: { nodes: [], edges: [] }, merged: { nodes: [], edges: [] }, selectedTypes: ["claim"] }),
  });
  expect(screen.getByText("还没有知识图谱内容。整理来源后会显示在这里。")).toBeTruthy();
  expect(screen.queryByText("没有匹配的节点。清空搜索后可查看完整图谱。")).toBeNull();
  emptyGraphWhileSearching.unmount();

  // 构建轮询 20 分钟封顶后 vizBuilding 已归位、响应里的 viz_building 仍为 true：画布与
  // 封顶那句 toast 说同一句话，而不是说「还没有内容」。
  const cappedBuild = renderView({
    kgCanvas: "empty",
    kgGraph: graphView({
      graph: { nodes: [], edges: [], viz_building: true },
      merged: { nodes: [], edges: [], viz_building: true },
    }),
  });
  expect(screen.getByText("图谱索引仍在后台构建，请稍后重新打开查看")).toBeTruthy();
  cappedBuild.unmount();

  const nonEmptyGraph = {
    nodes: [{ id: "K-1", object_type: "concept", payload: { name: "阈值" } }],
    edges: [],
  };
  const emptySearch = renderView({
    kgCanvas: "empty",
    kgSearching: true,
    kgGraph: graphView({ graph: nonEmptyGraph, merged: nonEmptyGraph }),
  });
  expect(screen.getByText("没有匹配的节点。清空搜索后可查看完整图谱。")).toBeTruthy();
  emptySearch.unmount();

  const emptyFiltered = renderView({
    kgCanvas: "empty",
    kgGraph: graphView({ graph: nonEmptyGraph, merged: nonEmptyGraph, selectedTypes: ["claim"] }),
  });
  expect(screen.getByText("当前类型过滤下没有节点。清除过滤后可查看完整图谱。")).toBeTruthy();
  emptyFiltered.unmount();

  renderView({ kgCanvas: "graph" });
  expect(screen.getByTestId("force-graph")).toBeTruthy();
});


test("graph 态把注入的图数据与画布尺寸原样交给 ForceGraph2D，且它是 ssr:false 动态加载的", () => {
  const fgData = {
    nodes: [{ id: "K-1", name: "阈值", type: "concept", val: 6, degree: 1 }],
    links: [],
    searchHitCount: 0,
  };
  renderView({ kgCanvas: "graph", fgData, kgSize: { width: 640, height: 480 } });

  const last = forceGraphProps.at(-1)!;
  expect(last.graphData).toBe(fgData);
  expect(last.width).toBe(640);
  expect(last.height).toBe(480);
  // 画布 label 直接画在图上（不是 DOM），只能从传下去的回调上验：类型也要进 label。
  expect((last.nodeLabel as (n: unknown) => string)(fgData.nodes[0])).toBe("阈值 (concept)");
  expect(dynamicOptions.some((options) => (options as { ssr?: boolean })?.ssr === false)).toBe(true);
});


test("搜索框把输入原样交给注入命令；节点/边计数按当前视图与全图两段显示", async () => {
  const handleKgSearchChange = vi.fn();
  renderView({
    handleKgSearchChange,
    fgData: {
      nodes: [{ id: "K-1", name: "阈值", type: "concept", val: 6, degree: 1 }],
      links: [],
      searchHitCount: 0,
    },
    kgGraph: graphView({
      merged: {
        nodes: [
          { id: "K-1", object_type: "concept", payload: { name: "阈值" } },
          { id: "K-2", object_type: "claim", payload: { name: "论断" } },
        ],
        edges: [],
      },
    }),
  });

  await userEvent.type(screen.getByPlaceholderText("搜索节点名称或类型…"), "阈");
  expect(handleKgSearchChange).toHaveBeenCalledWith("阈");
  expect(screen.getByText("节点 1 / 2")).toBeTruthy();
  expect(screen.getByText("边 0 / 0")).toBeTruthy();
});


test("待确认合并把候选与 confirm 位交回注入命令，且在飞/重建时两颗都禁用", async () => {
  const decideMerge = vi.fn();
  const candidate: PendingMerge = { id: "m-1", canonical_a: "K-A", canonical_b: "K-B", score: 0.91, status: "pending" };
  const { unmount } = renderView({
    decideMerge,
    kgGraph: graphView({ pendingMerges: [candidate] }),
  });

  expect(screen.getByText("待确认合并 (1)")).toBeTruthy();
  const row = screen.getByText(/A ↔ B/).closest(".kg-merge-row") as HTMLElement;
  await userEvent.click(within(row).getByRole("button", { name: "合并" }));
  await userEvent.click(within(row).getByRole("button", { name: "拒绝" }));
  expect(decideMerge.mock.calls).toEqual([[candidate, true], [candidate, false]]);
  unmount();

  // 确认分支连带跑一次全量重建：重建期间新决定会与正在发布的旧候选代次竞态，
  // 所以整列锁住——两颗按钮都要禁用，不只是被点的那颗。
  renderView({
    kgGraph: graphView({ pendingMerges: [candidate], rebuilding: true }),
  });
  const busyRow = screen.getByText(/A ↔ B/).closest(".kg-merge-row") as HTMLElement;
  expect(within(busyRow).getByRole("button", { name: "合并" }).hasAttribute("disabled")).toBe(true);
  expect(within(busyRow).getByRole("button", { name: "拒绝" }).hasAttribute("disabled")).toBe(true);
});


test("只读工作区看不到图谱处理、自动判重与逐行合并决定", () => {
  const candidate: PendingMerge = { id: "m-1", canonical_a: "K-A", canonical_b: "K-B", score: 0.91, status: "pending" };
  renderView({
    readOnlyWorkspace: true,
    kgGraph: graphView({ pendingMerges: [candidate] }),
  });

  expect(screen.queryByText("图谱处理")).toBeNull();
  expect(screen.queryByRole("button", { name: "补上关联" })).toBeNull();
  expect(screen.queryByRole("button", { name: "删除知识图谱" })).toBeNull();
  expect(screen.queryByRole("button", { name: "自动判重" })).toBeNull();
  expect(screen.queryByRole("button", { name: "全部自动判重" })).toBeNull();
  // 候选本身仍然看得见（只读成员可以知道有待确认项），只是没有决定入口。
  expect(screen.getByText(/A ↔ B/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "合并" })).toBeNull();
});


test("「删除知识图谱」排在「全部重新分析」之后，空闲时可点并交回注入的确认命令", async () => {
  const confirmDeleteKg = vi.fn();
  renderView({ confirmDeleteKg });

  const actions = screen.getByText("图谱处理").parentElement!;
  const labels = within(actions).getAllByRole("button").map((button) => button.textContent);
  expect(labels).toEqual(["补上关联", "重新合并", "全部重新分析", "删除知识图谱"]);

  const button = screen.getByRole("button", { name: "删除知识图谱" });
  expect(button).toBeEnabled();
  expect(button.className).toContain("kg-action-danger");
  expect(button.getAttribute("title")).toBe("删除从来源分析出的知识图谱（来源保留，之后可重新整理；会先确认）");
  await userEvent.click(button);
  expect(confirmDeleteKg).toHaveBeenCalledTimes(1);
  // 空闲、没有结果时按钮旁不留一行空的状态。
  expect(screen.queryByRole("status")).toBeNull();
});


test("没有知识图谱时无可删除；任一维护/整理任务在跑时删除按钮禁用", () => {
  const { unmount } = renderView({ kgReady: false });
  expect(screen.getByRole("button", { name: "删除知识图谱" })).toBeDisabled();
  unmount();

  for (const busy of ["relinking", "rebuilding", "buildingKg", "reviewBusy", "reviewAllStarting", "reviewAllRunning"] as const) {
    const view = renderView({ kgGraph: graphView({ [busy]: true }) });
    expect(screen.getByRole("button", { name: "删除知识图谱" }), busy).toBeDisabled();
    view.unmount();
  }
  // 合并决定在飞：删除会把正在决定的那个候选一并清掉。
  const deciding = renderView({ kgGraph: graphView({ decidingMerge: { id: "m-1", confirm: false } }) });
  expect(screen.getByRole("button", { name: "删除知识图谱" })).toBeDisabled();
  deciding.unmount();
});


test("删除在飞：自动判重与全部自动判重同样禁用（删除会清掉待确认合并）", () => {
  const candidate: PendingMerge = { id: "m-1", canonical_a: "K-A", canonical_b: "K-B", score: 0.91, status: "pending" };
  const idle = renderView({ kgGraph: graphView({ pendingMerges: [candidate] }) });
  expect(screen.getByRole("button", { name: "自动判重" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "全部自动判重" })).toBeEnabled();
  idle.unmount();

  renderView({ kgGraph: graphView({ pendingMerges: [candidate], deleting: true }) });
  expect(screen.getByRole("button", { name: "自动判重" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "全部自动判重" })).toBeDisabled();
});


test("删除在飞：按钮换成「删除中…」并禁用，同一维护槽的其余三颗也一并禁用", () => {
  renderView({ kgGraph: graphView({ deleting: true }) });

  expect(screen.queryByRole("button", { name: "删除知识图谱" })).toBeNull();
  expect(screen.getByRole("button", { name: "删除中…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "补上关联" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "重新合并" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "全部重新分析" })).toBeDisabled();
});


test("删除期间待确认合并的逐行决定也禁用（候选正被一并清掉）", () => {
  const candidate: PendingMerge = { id: "m-1", canonical_a: "K-A", canonical_b: "K-B", score: 0.91, status: "pending" };
  renderView({ kgGraph: graphView({ pendingMerges: [candidate], deleting: true }) });
  const row = screen.getByText(/A ↔ B/).closest(".kg-merge-row") as HTMLElement;
  expect(within(row).getByRole("button", { name: "合并" })).toBeDisabled();
  expect(within(row).getByRole("button", { name: "拒绝" })).toBeDisabled();
});


test("删除结果画在按钮紧挨着的下一行（role=status），成功/失败/中性各自原样显示", () => {
  const cases = [
    { tone: "success", text: "已删除 12 个知识对象" },
    { tone: "failed", text: "删除没有完成，请重试" },
    { tone: "neutral", text: "删除状态未知，请刷新后查看" },
  ] as const;
  for (const result of cases) {
    const view = renderView({ kgGraph: graphView({ deleteResult: result }) });
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(result.text);
    // 结果紧邻被按下的那颗按钮，而不是落在页面别处的横幅里。
    expect(status.previousElementSibling).toBe(screen.getByRole("button", { name: "删除知识图谱" }));
    view.unmount();
  }
});


test("概念详情经既有 KgEvidenceList 渲染出处，并按 next_cursor 给出加载更多成员", async () => {
  const onLoadMoreConceptMembers = vi.fn();
  renderView({
    onLoadMoreConceptMembers,
    selectedKgNode: { id: "K-1", object_type: "concept", payload: { name: "阈值" } },
    kgGraph: graphView({
      selectedNodeId: "K-1",
      merged: {
        nodes: [{ id: "K-1", object_type: "concept", payload: { name: "阈值" } }],
        edges: [],
      },
      conceptDetail: {
        canonical_id: "K-1",
        canonical_name: "阈值",
        members: [{ id: "K-1", object_type: "concept", payload: { name: "阈值" }, evidence: [] }],
        member_total: 4,
        attached: [],
        evidence: [
          {
            source_id: "s-1",
            source_title: "手册",
            location_label: "第 3 页",
            quoted_span: "阈值定义如下",
            element_id: "e-1",
            confidence: 0.9,
            // 补上 element_type：缺它时 vocabulary 的 label() 会在开发期打印
            // 「未映射的枚举值」，那是夹具噪音，不是被测行为。
            element_type: "paragraph",
          },
        ],
        next_cursor: "cur-2",
      },
    }),
  });

  expect(screen.getByText("出处")).toBeTruthy();
  expect(screen.getByText("手册")).toBeTruthy();
  expect(screen.getByText("阈值定义如下")).toBeTruthy();

  await userEvent.click(screen.getByRole("button", { name: /加载更多成员（已加载 1\/4）/ }));
  expect(onLoadMoreConceptMembers).toHaveBeenCalledTimes(1);
});


test("画布上点节点走 onSelectCanvasNode，不是总览那条", () => {
  const onSelectOverviewNode = vi.fn();
  const onSelectCanvasNode = vi.fn();
  renderView({
    onSelectOverviewNode,
    onSelectCanvasNode,
    kgCanvas: "graph",
    fgData: {
      nodes: [{ id: "K-1", name: "阈值", type: "concept", val: 6, degree: 1 }],
      links: [],
      searchHitCount: 0,
    },
  });

  const last = forceGraphProps.at(-1)!;
  (last.onNodeClick as (n: unknown) => void)({ id: "K-1" });
  expect(onSelectCanvasNode).toHaveBeenCalledWith("K-1");
  expect(onSelectOverviewNode).not.toHaveBeenCalled();
});


test("节点总览按类型分组，点条目走带 catch 的那条选点回调（不是画布那条）", async () => {
  const onSelectOverviewNode = vi.fn();
  const onSelectCanvasNode = vi.fn();
  renderView({
    onSelectOverviewNode,
    onSelectCanvasNode,
    kgNodeGroups: [
      {
        type: "concept",
        label: "概念 Concept",
        nodes: [{ id: "K-1", name: "阈值", type: "concept", val: 6, degree: 2 }],
      },
    ],
  });

  // 总览计数是各类型分组节点数之和，不是分组数。
  expect(screen.getByText("1 个")).toBeTruthy();
  // 「概念 Concept」在图例与分组标题各出现一次，这里只确认分组标题在场。
  expect(screen.getAllByText("概念 Concept").length).toBeGreaterThanOrEqual(2);
  await userEvent.click(screen.getByRole("button", { name: /阈值/ }));
  expect(onSelectOverviewNode).toHaveBeenCalledWith("K-1");
  expect(onSelectCanvasNode).not.toHaveBeenCalled();
});


test("头部两个只读诊断入口与关闭按钮各自交回注入命令", async () => {
  const openKgAnalysis = vi.fn();
  const openKgSchemas = vi.fn();
  const closeKgView = vi.fn();
  renderView({ openKgAnalysis, openKgSchemas, closeKgView });

  await userEvent.click(screen.getByRole("button", { name: /图谱分析/ }));
  await userEvent.click(screen.getByRole("button", { name: /图谱 Schema/ }));
  await userEvent.click(screen.getByRole("button", { name: "×" }));
  expect(openKgAnalysis).toHaveBeenCalledTimes(1);
  expect(openKgSchemas).toHaveBeenCalledTimes(1);
  expect(closeKgView).toHaveBeenCalledTimes(1);
});


// 待确认合并 / 相邻关系 / 出处三份清单接口一次性整份返回（无 LIMIT），此前各自被
// 硬 slice 到 24/10 条（相邻关系）或压根没有上限却随全量渲染，界面改为分页展示。
function manyPendingMerges(count: number): PendingMerge[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `m-${index + 1}`,
    canonical_a: `A${String(index + 1).padStart(2, "0")}`,
    canonical_b: `B${String(index + 1).padStart(2, "0")}`,
    score: 0.5,
    status: "pending",
  }));
}

function manyKgEdges(count: number, prefix = "边"): Props["selectedKgEdges"] {
  return Array.from({ length: count }, (_, index) => ({
    source_object_id: `s-${prefix}-${index}`,
    target_object_id: `t-${prefix}-${index}`,
    edge_type: "about",
    sourceName: `${prefix}源${String(index + 1).padStart(2, "0")}`,
    sourceType: "concept",
    targetName: `${prefix}目标${String(index + 1).padStart(2, "0")}`,
    targetType: "concept",
  }));
}

function manyKgOccurrences(count: number): KgOccurrence[] {
  return Array.from({ length: count }, (_, index) => ({
    source_title: `来源${String(index + 1).padStart(2, "0")}`,
    element_text: `内容${index + 1}`,
    // 补上 element_type：缺它时 vocabulary 的 label() 会在开发期打印
    // 「未映射的枚举值」，那是夹具噪音，不是被测行为（同上方既有用例的注释）。
    element_type: "paragraph",
  }));
}

test("待确认合并超过一页时分页展示，翻页后显示下一批候选", async () => {
  const user = userEvent.setup();
  renderView({ kgGraph: graphView({ pendingMerges: manyPendingMerges(25) }) });

  expect(document.querySelectorAll(".kg-merge-row")).toHaveLength(20);
  const pager = screen.getByRole("navigation", { name: "待确认合并分页" });
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(document.querySelectorAll(".kg-merge-row")).toHaveLength(5);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("相邻关系超过一页时分页展示，切换选中节点时回到第一页", async () => {
  const user = userEvent.setup();
  const { rerender, props } = renderView({
    selectedKgNode: { id: "K-1", object_type: "concept", payload: { name: "阈值" } },
    selectedKgEdges: manyKgEdges(25, "一"),
  });

  expect(document.querySelectorAll(".kg-relation-row")).toHaveLength(20);
  const pager = screen.getByRole("navigation", { name: "相邻关系分页" });
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(document.querySelectorAll(".kg-relation-row")).toHaveLength(5);

  // 切到另一个节点：即便新节点的相邻关系同样超过一页，也必须从第一页开始看，
  // 不能停在上一个节点翻到的那一页——那会让用户第一眼看到的是错位的关系。
  rerender(
    <KgGraphView
      {...props}
      selectedKgNode={{ id: "K-2", object_type: "concept", payload: { name: "另一个" } }}
      selectedKgEdges={manyKgEdges(25, "二")}
    />,
  );
  expect(document.querySelectorAll(".kg-relation-row")).toHaveLength(20);
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();
});

test("出处超过一页时分页展示，翻页后显示下一批", async () => {
  const user = userEvent.setup();
  renderView({
    selectedKgNode: { id: "K-1", object_type: "concept", payload: { name: "阈值" } },
    kgGraph: graphView({
      selectedNodeId: "K-1",
      nodeContext: {
        id: "K-1",
        object_type: "concept",
        name: "阈值",
        section_path: "",
        occurrences: manyKgOccurrences(15),
        definition: null,
        steps: null,
      },
    }),
  });

  expect(document.querySelectorAll(".kg-evidence-card")).toHaveLength(10);
  const pager = screen.getByRole("navigation", { name: "出处分页" });
  expect(within(pager).getByText("1–10 / 15")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(document.querySelectorAll(".kg-evidence-card")).toHaveLength(5);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
  // 序号跨页连续:第 2 页从 11 数到 15。
  expect(Array.from(document.querySelectorAll(".kg-evidence-index")).map((node) => node.textContent))
    .toEqual(["11", "12", "13", "14", "15"]);
});


test("children 原位渲染在 .kg-view section 内（图谱分析弹窗的层叠上下文归属）", () => {
  const { container } = renderView({
    children: <div data-testid="analysis-slot" />,
  });
  const section = container.querySelector("section.kg-view")!;
  const slot = section.querySelector('[data-testid="analysis-slot"]')!;
  expect(slot).toBeTruthy();
  // 不只是「在 section 内某处」：children 必须是 section 自己的直接子节点
  // （不是塞进 .kg-view-body 里），且排在 .kg-view-body 之后、是 section 的最后一个子元素——
  // 这决定了弹窗的层叠上下文归属。
  expect(slot.parentElement).toBe(section);
  expect(section.lastElementChild).toBe(slot);
  expect(slot.previousElementSibling).toBe(section.querySelector(".kg-view-body"));
});
