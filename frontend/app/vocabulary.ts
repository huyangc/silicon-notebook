// 跨模块枚举 → 用户可见中文的单一真源。
// 只装「跨模块」的枚举；功能自己的枚举留在各自模块里，但同样必须走 label()。
// 散文词不进这里（抽成常量只会让代码更难读）——由 docs/ui-vocabulary.md +
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
  parsing: "解析中", // source_ingestion.py:929 真实会写入(置于 try 内首行)
  parsed: "已解析",
  extracting: "分析中",
  extracted: "已就绪",
  failed: "解析失败",
  "metadata-only": "仅元数据", // source_ingestion.py:274 真实会写入
};

// source_elements.element_type 的取值真源有三类产出方,合起来才是全集:
//   backend/app/services/parsers.py —— 各格式解析器(table_row / page_text /
//     slide_text / speaker_notes / heading / paragraph)与 MinerU 映射
//     (heading / paragraph / table / formula / image);
//   backend/app/services/structural_markdown.py —— Markdown 块类型
//     (heading / paragraph / table / code_block / list_item / image),经 parsers.py
//     的 parse_markdown_text 原样写入;
//   backend/app/services/knowhow/projection.py —— Knowhow 投影固定写 knowhow_cell。
// 刻意不含 text:它从来没有产出方。parsers.py:32 传的 "text" 是 parser_name(进
// metadata),structural_markdown.py 里的 "text" 是 markdown-it 的行内 token 类型,
// 两者都不是 element_type。留着只会让人误以为后端会写这个值。
export const ELEMENT_TYPE: Record<string, string> = {
  heading: "标题",
  paragraph: "正文",
  // pypdf 兜底(无 MinerU)时的 PDF 段落。与 paragraph 同标签是有意的——对用户是
  // 同一件东西,差别只在哪个解析器产出(先例见下方 CHECKUP_ISSUE 的 H4/H5)。
  page_text: "正文",
  table: "表格",
  table_row: "表格行", // CSV 行、XLSX 行、DOCX 表格行
  formula: "公式",
  code_block: "代码",
  list_item: "列表项",
  image: "图片",
  slide_text: "幻灯片文本", // PPTX 无 MinerU 时的兜底
  speaker_notes: "演讲者备注",
  // 历史遗留:MinerU 映射的初版产出过这个值,PR#280 起改产 image。已部署库里未重新
  // 解析过的旧来源仍可能是它,所以保留翻译。
  image_caption: "图片说明",
  // 非文件解析器产出:Knowhow 投影(backend/app/services/knowhow/projection.py)
  // 写入隐藏来源 source_elements 的行。
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

// reports.status 的取值集。真源=report_engine.py/report_execution.py 的
// update_report(status=…) 调用点(done/failed/cancelled)+ report-view.tsx 的
// 状态机注释(pending/planning/intent_ready/outline_ready/running/generating
// 是各阶段的非终态)。report-view.tsx 与 dev/logs/activity/format.ts 共用这张表
// ——前者本就 import 本模块的 label(),后者是新增消费方,不该各自维护一份、
// 让「同一个 status 值在两处显示不同中文」这类漂移悄悄发生。
export const REPORT_STATUS: Record<string, string> = {
  pending: "排队中",
  planning: "规划中",
  intent_ready: "待确认问题",
  outline_ready: "待确认",
  running: "生成中",
  generating: "生成中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};

// reports.depth 的取值 → 「研究深度」档名。五档名与 EffortPicker 的档位表同名
// (概览/标准/深入/详尽/穷尽),取值集的真源是 report-model.ts 的 REPORT_DEPTHS。
//
// 为什么放在这里而不是留在 report-view.tsx:展示层的 DEPTH_LABELS 是**按下标**的
// 有序档位表(喂给滑块控件),而只拿到 reports.depth 数值的消费方(dev/logs 活动流)
// 需要的是**按取值**反查。两种形状各写一份就会漂移,所以档名收敛到这张表,
// report-view.tsx 的有序展示表由它派生。
export const REPORT_DEPTH: Record<string, string> = {
  "1": "概览",
  "2": "标准",
  "4": "深入",
  "8": "详尽",
  "16": "穷尽",
};

// ask_jobs.status 的取值集 {running, done, cancelled, failed}——真源见
// backend/app/services/ask_execution.py 的 `_finish()` 落终态三选一,以及
// backend/app/repositories/sqlite/ask_state_store.py 的初始态 'running'。
// 措辞与上面 REPORT_STATUS 的同名状态对齐(完成/失败/已取消/生成中)。
export const ASK_STATUS: Record<string, string> = {
  running: "生成中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
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
// fix=reparse 等枚举),面向用户的标签只在这里映射,绝不能含黑话(见 docs/ui-vocabulary.md
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

// 同一批修复动作**已触发、后台执行中**时的按钮文案。修复都是后台 job(点完到
// count 下降之间有一段真空期),按钮必须在这段时间里禁用并改文案,否则用户会反复
// 点、每点一次就再排一份同样的活。文案按各自动作的语义写,不是笼统的「处理中」
// ——「补齐向量」变「补齐中…」、「重新解析」变「解析中…」,让用户知道在等什么。
export const CHECKUP_FIX_BUSY: Record<string, string> = {
  reparse: "解析中…",
  backfill_vectors: "补齐中…",
  extract_kg: "分析中…",
  fold_index: "更新中…",
  rebuild_index: "重建中…",
};

// 群组分类（groups.kind）。只是分类标签，不影响任何权限；影响的是谁能建组与这里
// 的界面文案（后端 app/models/groups.py 的 GROUP_KINDS 是取值真源）。
export const GROUP_KIND: Record<string, string> = {
  project: "项目",
  department: "部门",
  domain: "领域",
};

// 组内角色（group_members.role）。两级，不引入第三级。
export const GROUP_ROLE: Record<string, string> = {
  admin: "组管理员",
  member: "成员",
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
