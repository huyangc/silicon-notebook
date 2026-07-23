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
 * 系统模型服务状态的失败类别（`ModelServiceStatusItem.code`）。
 *
 * 状态接口只传稳定 code；上游错误详情留在后端日志，不能拼进 UI 文案。
 */
export const MODEL_SERVICE_STATUS_ERROR: Record<string, string> = {
  upstream_error: "连接未通过",
  missing_config: "系统未配置",
  model_queue_full: "等待队列已满",
  model_queue_timeout: "排队等待超时",
  model_service_unavailable: "服务暂不可用",
  provider_auth: "服务鉴权失败",
  provider_rate_limited: "上游服务限流",
  provider_unavailable: "上游服务不可用",
  provider_error: "上游调用失败",
  malformed_response: "返回格式异常",
  model_not_configured: "系统未配置",
};

// 流水线体检(P2)的内部代号 → 界面词。/checkup 响应体是内部契约(code=H2..H8、
// fix=reparse 等枚举),面向用户的标签只在这里映射,绝不能含黑话(见 AGENTS.md
// 「界面词汇表」+ scripts/check_ui_vocabulary.py)。H4/H5 同为「检索向量缺失」是有意的
// (对用户是同一件事、同一个修复动作),渲染时按 label 合并成一行。
export const CHECKUP_ISSUE: Record<string, string> = {
  H2: "未解析出内容",
  H3: "检索片段缺失",
  H4: "检索向量缺失",
  H5: "检索向量缺失",
  H6: "待分析来源",
  H7: "检索索引过期",
  H8: "检索索引损坏",
};

// 体检修复动作枚举(fix)→ 修复按钮文案。extract_kg 复用既有「分析新增」,
// fold_index/rebuild_index 复用既有检索索引的更新/全量重建入口。
export const CHECKUP_FIX: Record<string, string> = {
  reparse: "重新解析",
  backfill_vectors: "补齐向量",
  extract_kg: "分析新增",
  fold_index: "更新索引",
  rebuild_index: "重建索引",
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
