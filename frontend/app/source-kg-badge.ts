// 来源列表里那枚「已分析 / 待分析」徽标的三态派生。
//
// 为什么是三态而不是布尔：后端的 `kg_extracted` 只回答「这份来源有没有知识对象」。
// 一份正文极少、或几乎全是没有图注的图片的文档，分析跑完了也确实一个知识对象都没有
// ——旧的二态把它显示成「待分析」，于是看板计数永远降不下来、「继续分析」按钮永远
// 亮着，而每次点它都会把这份文档重新分析一遍、再得到零。用户读到的就是「一直分析
// 不成功」。
//
// 所以后端新增了并列的 `kg_analyzed_empty`（判据见 backend/app/models/sources.py 的
// `kg_analyzed_without_objects`），这里只做展示映射。两个字段互斥：`kg_extracted`
// 为真时 `kg_analyzed_empty` 必为假。旧后端不发新字段 → 缺省 false → 逐字回到二态。

export type SourceKgBadgeState = "analyzed" | "analyzed_empty" | "pending";

export type SourceKgBadge = {
  state: SourceKgBadgeState;
  label: string;
  title: string;
  /** 完整 class 串。放在这里而不是让 JSX 拼三元：三态的样式差异是这个视图模型的一
   *  部分（尤其「已分析·无知识」必须拿到自己那条 pointer-events，否则它的解释性
   *  title 根本弹不出来），拼在 JSX 里就没法单测。 */
  className: string;
};

const BADGES: Record<SourceKgBadgeState, Omit<SourceKgBadge, "state">> = {
  analyzed: {
    label: "已分析",
    title: "已分析：该来源已完成知识图谱分析",
    className: "source-kg-badge source-kg-badge--in",
  },
  analyzed_empty: {
    label: "已分析·无知识",
    title:
      "已完成分析：这份文档里没有可整理成知识图谱的内容（正文很少，或图片没有图注）。"
      + "再分析一次结果相同；若是扫描件，可开启 OCR 后重新解析。",
    // 刻意不带 --in：那是绿色的「已入库」实心态，而这一态没有任何知识对象进图，
    // 用同一个绿色就是在说一件没发生的事。
    className: "source-kg-badge source-kg-badge--empty",
  },
  pending: {
    label: "待分析",
    title: "待分析：该来源尚未加入知识图谱",
    className: "source-kg-badge",
  },
};

export function sourceKgBadge(source: {
  kg_extracted?: boolean;
  kg_analyzed_empty?: boolean;
}): SourceKgBadge {
  // 顺序即优先级：真有知识对象就是「已分析」，不看第二个字段——两者本该互斥，
  // 万一后端给出矛盾组合（旧行 + 新字段回填），显示更强的那个事实，不显示更弱的。
  const state: SourceKgBadgeState = source.kg_extracted
    ? "analyzed"
    : source.kg_analyzed_empty
      ? "analyzed_empty"
      : "pending";
  return { state, ...BADGES[state] };
}
