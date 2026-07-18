// 跨模块枚举 → 用户可见中文的单一真源。
// 只装「跨模块」的枚举；功能自己的枚举留在各自模块里，但同样必须走 label()。
// 散文词不进这里（抽成常量只会让代码更难读）——由 AGENTS.md 词汇表 +
// scripts/check_ui_vocabulary.py 管。
//
// object_type 刻意不在此处：后端 extraction_profiles.OBJECT_TYPE_LABELS 才是它的
// 真源，且已通过 API 下发（KnowledgeTypeCount.label），自定义类型也走同一条路。
// 见 docs/superpowers/specs/2026-07-17-user-facing-vocabulary-design.md §2.1。

export const TIER: Record<string, string> = {
  base: "公共知识库",
  personal: "个人知识库",
};

export const PARSE_STATUS: Record<string, string> = {
  uploaded: "已上传",
  queued: "排队中",
  parsed: "已解析",
  extracting: "分析中",
  extracted: "已就绪",
  failed: "解析失败",
  "metadata-only": "仅元数据", // source_ingestion.py:274 真实会写入
};

// 取值真源:structural_markdown.py 写入 source_elements 的 element_type。
// heading/paragraph/table/code_block/text/list_item/image + parsers 侧的 formula。
export const ELEMENT_TYPE: Record<string, string> = {
  heading: "标题",
  paragraph: "正文",
  table: "表格",
  formula: "公式",
  code_block: "代码",
  text: "正文",
  list_item: "列表项",
  image: "图片",
  knowhow_cell: "经验表单元格",
};

export const KNOWLEDGE_STATUS: Record<string, string> = {
  reviewed: "已审阅",
  approved: "已批准",
  deprecated: "已弃用",
  conflict: "有冲突",
  project_specific: "项目专用",
};

export const EVIDENCE_LEVEL: Record<string, string> = {
  grounded: "有据",
  inferred: "推断",
  overview: "概述",
};

// 知识条目重要度。真源 extraction_profiles.py:28 `severity: "high|medium|low"`。
export const SEVERITY: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

// 措辞刻意保持与现状一字不差(answer-panel.tsx:354-358 原有的四个名字)。
// 本 PR 只修「兜底即原值」这个机制,不碰命名——模型角色命名与设置页对齐
// (报错说「向量模型」但设置页没这一项)属于 PR C 错误层的范围。这里改名会
// 给同一批东西发明第三套叫法,PR C 还得再改一遍。
export const MODEL_STAGE: Record<string, string> = {
  embed: "向量模型",
  rerank: "重排模型",
  answer: "答案模型",
  rewrite: "改写模型",
};

// 取值真源:migrations.py:413 的建表注释 `proposed | under_review | approved | rejected`,
// 且 page.tsx:5156 线上代码正按 proposed / under_review 分支。没有 "pending" 这个值。
export const PROMOTION_STATUS: Record<string, string> = {
  proposed: "待审核",
  under_review: "审核中",
  approved: "已收录",
  rejected: "未采纳",
};

/**
 * 模型「测试连接」的失败原因（`ModelTestResult.code`）。
 *
 * 后端只回 code，文案在这里——这样它才落在界面词汇守卫的作用域里。上一版把中文
 * 放后端，结果「缺少 base_url / model / api_key」直接把字段名甩给用户，而守卫只扫
 * `frontend/app`、看不见它。`upstream_error` 刻意不展开成异常原文：原文是诊断，
 * 走 logDiagnostic 进 console。
 */
export const MODEL_TEST_ERROR: Record<string, string> = {
  unknown_service: "不认识这个模型用途",
  missing_config: "还没填完，需要接口地址、模型名和密钥",
  upstream_error: "连不上这个模型服务",
};

/**
 * 严格查表：命中返回映射值，未命中返回 `fallback`——永远不会是 `value` 本身。
 *
 * 签名强制传 fallback，是为了让「兜底即原值」这个 bug 写不出来。后端每加一个
 * 枚举值，旧写法（`MAP[v] ?? v`）都会自动把英文 id 泄漏给用户；这里则会退到一个
 * 中性词，并在开发期把未映射的值喊出来。
 *
 * 命中判断必须用 `Object.hasOwn`，不能写成 `map[value]` + 真值判断：`map[value]`
 * 会走原型链，`value` 传入 "constructor"/"toString"/"__proto__"/"hasOwnProperty"/
 * "valueOf" 时会命中 `Object.prototype` 上的同名成员，返回一个函数/对象而非
 * `fallback`。TS 把 `Record<string, string>` 的索引签名推成 `string`，`tsc` 抓不
 * 到这个类型谎言；这个值一旦被渲染进 JSX，React 会抛 "Objects are not valid as a
 * React child" 白屏。`Object.hasOwn` 只认自身属性，天然免疫原型链，同时也顺带修
 * 掉了真值判断的另一个坑——把「合法翻译成空串」的 key 误判为未命中。
 */
export function label(map: Record<string, string>, value: string, fallback: string): string {
  if (Object.hasOwn(map, value)) return map[value];
  if (process.env.NODE_ENV !== "production") {
    console.error(`[vocabulary] 未映射的枚举值：${JSON.stringify(value)}`);
  }
  return fallback;
}
