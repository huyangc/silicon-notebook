import type { ReasoningTraceStep } from "./ask-stream";
import type { GrantedGroupRef } from "./group-api";
import type { NotebookRef } from "./notebook-bases";

/**
 * 新建 notebook 的默认名 —— **持久化契约，不是界面文案，不受「界面词汇表」管辖。**
 *
 * 这个字符串会随 `POST /notebooks` 落库成 `notebooks.name` 的真实值，并被以下位置
 * 逐字钉死：`docs/product-and-api*.md`、`architecture.md`、
 * `fangan_done.md`，后端侧还有 `app/models/schemas.py` 的字段默认值、
 * `notebook_store.py` 的空名兜底、`sqlite_repository.py` 的
 * `_DEFAULT_NOTEBOOK_NAMES`（决定「自动改名只覆盖占位名、不覆盖用户起的名字」）。
 *
 * 曾经被「散文去黑话」顺手改成中文——那不是改文案，是改写入库里的数据，会让后端
 * 的占位名识别与文档契约同时失配。要改必须连同上述所有位置一起改，并处理存量数据。
 * `notebook-default-name.test.mjs` 守着这条线。
 */
export const DEFAULT_NOTEBOOK_NAME = "Untitled notebook";

export type KgBuildJobStatus = {
  job_id: string;
  mode: "incremental" | "rebuild";
  status: "running" | "succeeded" | "failed";
  stage: "probing" | "extracting" | "stopping" | "finished";
  total_sources: number;
  completed_sources: number;
  failed_sources: number;
  error_code: string;
  user_message: string;
  updated_at: string;
};

/** API/view models shared by the workspace orchestrator and extracted panels. */
export type NotebookSummary = {
  id: string;
  name: string;
  purpose: string;
  primary_domain: string;
  status: string;
  counts: Record<string, number>;
  created_label: string;
  target_users?: string;
  expected_questions?: string[];
  source_types?: string[];
  taxonomy?: string[];
  access_scope?: string;
  tier?: string;
  kg_ready?: boolean;
  kg_building?: boolean;
  base_kg_available?: boolean;
  /**
   * `base_kg_available` 的**分解**:上面那批挂载的参考库里,**哪几个**已建知识图谱。
   *
   * 参考库现在可以按库取消勾选,而聚合布尔分不出「这次勾选的库里有没有带图的」——
   * 本库无图、又恰好取消勾选了唯一带图的那个库时,读聚合布尔会放行一个这轮根本取不到
   * 图谱的模式,还会点名一个本轮不参与的库。读法见 ask-availability 的
   * `kgAvailableForScope` / `borrowedKgBaseNames`,一律不要在别处重算。
   */
  base_kg_notebook_ids?: string[];
  base_notebooks?: NotebookRef[];
  // 该 notebook 能否在任一模式下产出有据回答(后端权威计算:可见来源/任意 chunk[含
  // knowhow 格子]/已建 KG/参考库有 KG/confirmed memory 任一即真)。仅单库 get() 精确
  // 回填,列表恒 True。前端据此禁用空库对话框(见 ask-availability.isAskBlocked)。
  ask_available?: boolean;
  /**
   * 该 notebook **自己**(不含挂载参考库)有没有可检索证据 —— `ask_available` 那批
   * 判据里的本地那三条(Knowhow 格子 / 该用户已确认的 Memory / 本地图谱)。参考库
   * 现在可按库取消勾选,「取消之后还剩不剩东西」只能问它,不能拿可见来源数代替
   * (Knowhow-only 的库来源恒为 0 却照常可搜)。读法见 ask-availability.hasLocalEvidence。
   */
  local_evidence_available?: boolean;
  kg_pending_sources?: number;
  kg_build?: KgBuildJobStatus | null;
  access?: "owner" | "reader";
  shared_from?: string;
  is_shared?: boolean;
  /**
   * 这本笔记本是**经哪几个群组**共享给我的（群组知识共享 P1）。
   *
   * 非空即「群组」分区的判据。`access` 刻意没有新增枚举值（已定裁决 7）——群组
   * 共享与只读共享同为 `reader`，区别只由本字段表达。两者在界面上必须分开的地方
   * 见 `group-api.ts::partitionByGrant`：只读共享有「退出共享」这个用户自己能按的
   * 出口，群组共享没有（那个按钮打的是成员表，对授权边一点作用都没有）。
   */
  granted_via?: GrantedGroupRef[];
  /**
   * 本用户对这本库是否持有**内容管理权**（群组知识共享 P2，裁决 P2-3）。
   *
   * = owner ∨ 一条 `role='admin'` 的有效授权边。组管理员打开被共享进本组的库时，
   * `access` 仍是 `"reader"`（那是权限档，没有新增枚举值），但来源添加/删除/重解析、
   * 图谱与索引构建、knowhow 写、知识治理写、分享管理这些入口必须画出来。
   *
   * 缺失/false = 只读，与本字段出现之前逐字一致（旧后端不发它）。读法只有一处：
   * `workspaceCapabilities`——别在组件里直接判它，否则「哪些入口算内容管理」会散成
   * 二十几份。
   */
  can_manage_content?: boolean;
  indexing_pipeline_id?: string | null;
  indexing_pipeline_version?: string | null;
  indexing_pipeline_available?: boolean;
  indexing_pipeline_missing?: boolean;
  indexing_pipeline_pending?: boolean;
  indexing_pipeline_stale?: boolean;
  paper_meta_backfilling?: boolean;
  /**
   * 是否存在缺论文元数据的合规候选源(后端按「补全论文信息」真正会排队的同一
   * 口径计算)。仅单库 get() 精确回填;列表投影与旧后端为 null/缺失 = 未计算,
   * 前端按旧行为继续显示按钮。读法见 showPaperMetaBackfill。
   */
  paper_meta_missing?: boolean | null;
  /**
   * owner 的有效文档数量上限。**只在 GET /notebooks/{id} 详情里是真值**——列表
   * 投影(listNotebooks)里是 0 哨兵,别拿来门控。0/缺失一律当「未知」处理。
   */
  document_limit?: number;
};

export type MemoryOrigin = "ask_answer" | "external_agent";
export type MemoryStatus = "candidate" | "confirmed" | "rejected" | "deprecated";
export type MemoryPromotionState = "none" | "proposed" | "approved" | "rejected";

export type MemoryRecord = {
  id: string;
  notebook_id: string;
  created_by: string;
  agent_profile_id?: string | null;
  source_answer_id?: string | null;
  origin: MemoryOrigin;
  status: MemoryStatus;
  promotion_state: MemoryPromotionState;
  title: string;
  content_md: string;
  tags: string[];
  confirmed_by?: string | null;
  confirmed_at?: string | null;
  embedding_status: string;
  embedding_error: string;
  created_at: string;
  updated_at: string;
  provenance: Record<string, unknown>;
};

export type MemoryOverviewItem = {
  id: string;
  title: string;
  status: "candidate" | "confirmed";
  updated_at: string;
};

export type KnowhowOverviewTable = {
  id: string;
  title: string;
  row_count: number;
  last_activity_at: string;
};

export type NotebookContentOverview = {
  memory: {
    total: number;
    confirmed: number;
    candidate: number;
    recent: MemoryOverviewItem[];
  };
  knowhow: {
    table_count: number;
    row_count: number;
    projection_pending: number;
    projection_failed: number;
    stale_code_count: number;
    recent_tables: KnowhowOverviewTable[];
  };
};

export type PaginatedMemories = {
  items: MemoryRecord[];
  total_count: number;
  offset: number;
  limit: number;
  owner_total_count: number;
  owner_pending_count: number;
  notebook_options: Array<{
    notebook_id: string;
    name: string;
    memory_count: number;
    pending_count: number;
  }>;
  /** True only for a notebook-scoped list whose KG can absorb Memory; null/undefined for the global list. */
  kg_extract_eligible?: boolean;
};

export type MemoryPreview = {
  title: string;
  content_md: string;
  tags: string[];
  provenance_summary: Record<string, unknown>;
  kg_extract_eligible?: boolean;
};

export type AgentProfile = {
  id: string;
  owner_id: string;
  name: string;
  description: string;
  status: "active" | "revoked";
  created_at: string;
  updated_at: string;
};

export type AgentTokenSummary = {
  id: string;
  agent_profile_id: string;
  profile_name: string;
  scopes: string[];
  default_notebook_id: string;
  notebook_ids: string[];
  expires_at?: string | null;
  revoked_at?: string | null;
  last_used_at?: string | null;
  created_at: string;
};

export type AgentTokenIssued = Omit<AgentTokenSummary, "profile_name" | "revoked_at" | "last_used_at"> & {
  token: string;
};

export type PaperAuthor = {
  name: string;
  affiliation: string;
};

export type PaperMeta = {
  is_paper: boolean;
  title?: string | null;
  venue?: string | null;
  year?: number | null;
  doi?: string | null;
  keywords: string[];
  authors: PaperAuthor[];
};

export type SourceSummary = {
  id: string;
  notebook_id: string;
  /** 原始 `sources.title` 列。**不要拿它给用户命名来源**——那份来源被论文元数据
   *  接地后，`title` 仍是上传时的文件名。为用户命名一律用 `display_title`（后端
   *  由 `app/services/source_display.py::source_display_title` 合成，与引用卡、
   *  检索证据卡、清单卡共用同一份实现）。 */
  title: string;
  /** 面向用户的显示名（论文标题优先）。`docs/product-and-api.md` 契约：所有为用户命名来源的路径
   *  共用这一个真源，否则同一篇论文会在同一屏里有两个名字。 */
  display_title: string;
  type: string;
  status: string;
  parse_status: string;
  summary: string;
  element_count: number;
  file_name: string;
  file_size: number;
  source_url?: string;
  /** 抽取用的文档类型（'' = 自动检测，否则是一个 extraction profile id）。上传
   *  时同内容判重会沿用既有来源，但用户新选的类型仍会写进那条来源——所以这个字段
   *  是判断「这次上传到底改没改东西」的依据。 */
  doc_type?: string;
  /** 原始 ISO 时间戳，**按浏览器本地时区渲染的权威时间来源**（同一份来源在任何入口
   *  都必须显示同一个时刻）。`created_label` 是服务端按**服务端**日历日格式化好的旧
   *  字段（既有来源页签仍在用），新路径一律不要用它——服务端 UTC+8、浏览器 UTC 时，
   *  同一份来源会一边显示「8月5日」一边显示「8月4日 17:00」。 */
  created_at: string;
  created_label: string;
  error_message?: string;
  /** 参与集内代理读取（`/notebooks/{active}/sources/{id}`）的响应用它代替
   *  `error_message`：那是后端的原始异常串（可能带服务端绝对路径），跨库读取不该
   *  拿到；前端本来也只用它的真假。老的 `/sources/{id}` 仍返回 `error_message`。 */
  parse_failed?: boolean;
  extraction_warning?: string | null;
  /** MinerU 重试耗尽后由该格式对应的本地解析器完成；原始上游错误不对外暴露。 */
  parse_quality_warning?: boolean;
  /** 所选索引管线对这份来源的提案畸形/失败,内容已按内建策略回退整理。 */
  indexing_chunk_fallback?: boolean;
  kg_extracted?: boolean;
  // 分析跑完了、这份文档里确实没有可整理成知识图谱的内容。与 kg_extracted
  // 并列的互斥态,合起来给来源徽标三态(见 source-kg-badge.ts)。旧后端不发
  // 这个字段 → undefined → 徽标逐字回到原来的两态。
  kg_analyzed_empty?: boolean;
  /** 该来源是否由 Agent 经 MCP 接入通道添加。可选：旧后端缺字段按 false 处理。 */
  agent_created?: boolean;
  authors?: string[];
  pub_year?: number | null;
  venue?: string | null;
  paper_meta?: PaperMeta | null;
  paper_meta_status?: "has_meta" | "not_paper" | "missing" | null;
};

/** 上传接口返回的一条来源：比 SourceSummary 多一个 reused。
 *  reused=true 表示这条并不是本次新建的，而是本笔记本里内容完全相同的既有来源被
 *  沿用了（同一个文件传第二遍不会再建一条）。计数与提示文案必须区分二者，否则
 *  「N 个来源」会一路虚高到重新打开笔记本为止。字段可选：老后端不返回时按 false。 */
export type UploadedSource = SourceSummary & { reused?: boolean };

export type PaginatedSources = {
  items: SourceSummary[];
  total_count: number;
  offset: number;
  limit: number;
};

export const SOURCES_PAGE_SIZE = 50;

/**
 * 「补全论文信息」按钮的显示门:只在确有活可干(或无法证明没有)时显示。
 *  - backfilling:补抽任务运行期间保持可见,承载「补全中…」文案与轮询收尾。
 *  - notebook.paper_meta_missing === true:后端按补抽排队同口径判定存在缺元数据源。
 *  - null/缺失(列表投影兜底、旧后端):无法证明「已补全」,按旧行为显示——隐藏
 *    只能由显式的 false 触发。
 *  - 可见页任一来源 paper_meta_status === "missing":单库快照与来源页刷新节奏不同
 *    (解析完成先刷新来源页),取并集消掉两者之间的陈旧窗口;方向刻意宁多显示。
 */
export function showPaperMetaBackfill(
  notebook: Pick<NotebookSummary, "paper_meta_missing"> | null,
  sources: Pick<SourceSummary, "paper_meta_status">[],
  backfilling: boolean,
): boolean {
  if (backfilling) return true;
  const missing = notebook?.paper_meta_missing;
  if (missing !== false) return true;
  return sources.some((source) => source.paper_meta_status === "missing");
}

// 流水线体检(P2)。/checkup 响应体是内部契约:code(H2..H8)与 fix(修复动作枚举)
// 都是内部代号,面向用户的界面词由 vocabulary.ts 的 CHECKUP_ISSUE / CHECKUP_FIX 映射。
// sample 只给 source_id(前端自取标题);H4–H8 是计数/布尔型,sample 为空。
export type CheckupItem = {
  code: string;
  count: number;
  sample: string[];
  fix: string;
};

export type CheckupResponse = {
  notebook_id: string;
  checked_at: string;
  healthy: boolean;
  checks: CheckupItem[];
};

// 一个后台修复动作的受理回执:reparse 回 scheduled(实际排入的 source_id),
// notebook 级动作(补齐向量)回 accepted。
export type RepairScheduledResult = {
  scheduled: string[];
  accepted: boolean;
};

export type PaginatedKnowledge = {
  items: KnowledgeRecord[];
  total_count: number;
  offset: number;
  limit: number;
};

export type SourceElement = {
  id: string;
  source_id: string;
  element_type: string;
  location_label: string;
  text: string;
  metadata: Record<string, unknown>;
};

export type PaginatedSourceElements = {
  items: SourceElement[];
  total_count: number;
  offset: number;
  limit: number;
};

export type SearchHit = {
  scope: string;
  notebook_id: string;
  label: string;
  text: string;
  source_id: string;
  element_id: string;
};

export type Health = { status: string; llm_configured: boolean };

export type Evidence = {
  source_id: string;
  source_title: string;
  location_label: string;
  quoted_span: string;
  element_id: string;
};

/**
 * 一张「本段附图」：绑定证据里带图注（或带 markdown `> **图片描述**` 引用块）的
 * 图片元素——两者之一命中检索才带得出。模型不看图——这是响应装配层的展示增强，
 * 不是模型引用过的证据，前端必须与引证内容视觉区分。`caption` 在没有图注时装的是
 * 那段图片描述；为空串代表两样都没有，由前端只渲染图不渲染说明。
 * 真源：`backend/app/models/ask.py CitationImage`。
 */
export type CitationImage = {
  element_id: string;
  asset_id: string;
  caption: string;
};

export type AnswerAnchor = {
  key: string;
  object_id: string;
  object_type: string;
  label: string;
  name: string;
  definition?: string | null;
  snippet?: string | null;
  source_title: string;
  source_file_name?: string;
  location_label: string;
  source_id?: string;
  element_id?: string;
  tier?: string;
  notebook_id?: string;
  /** 空数组同 exclude_if 惯例整体缺席；旧持久化答案缺这个键时按「无附图」回退。 */
  images?: CitationImage[];
};

export type Citation = {
  label: string;
  source_id: string;
  element_id: string;
  location_label: string;
  quoted_span: string;
  source_file_name?: string;
  tier?: string;
  notebook_id?: string;
  /** 引用到的个人记忆(Memory)id；非空即代表这条引用来自私有记忆。后端同 exclude_if
   *  惯例只在非空时下发。会话分享弹窗据它统计"包含 K 条个人记忆摘录"的披露。 */
  memory_id?: string;
  /** 空数组同 exclude_if 惯例整体缺席；旧持久化答案缺这个键时按「无附图」回退。 */
  images?: CitationImage[];
};

/** 可验证的 Knowhow 行枚举。`cells` 的键是 column id；点击行可打开权威完整单元格。 */
export type KnowhowResultRow = {
  row_id: string;
  position: number;
  cells: Record<string, string>;
};

export type KnowhowResultSet = {
  kind: "knowhow";
  table_id: string;
  title: string;
  columns: { id: string; name: string; role?: string }[];
  rows: KnowhowResultRow[];
  coverage: {
    total_rows: number;
    scanned_rows: number;
    returned_rows: number;
    complete: boolean;
    truncated_reason?: string | null;
    overflow_semantics?: "explicit_partial" | "";
  };
};

export type KnowhowBatchCoverage = {
  known_tables: number;
  selected_tables: number;
  known_total_rows: number;
  scanned_rows: number;
  returned_rows: number;
  complete: boolean;
  truncated_reason?: string | null;
  overflow_semantics?: "explicit_partial" | "";
  synthesis_rows: number;
  synthesis_complete?: boolean | null;
};

/**
 * 枚举工具(PR-2)的类型化元素/知识对象清单覆盖率。**不是** `KnowhowResultSet["coverage"]`
 * 的复用——那一个的 `total_rows` 缺省是 0，表达不了"分母未知"（大库地图算不出总数时，
 * 后端把 `total` 置 `None`）。渲染时 `total === null` 必须显示"总数未知"，绝不能落成
 * `N/0`。真源：`backend/app/models/ask.py TypedCollectionCoverage`。
 */
export type TypedCollectionCoverage = {
  returned_total: number;
  total: number | null;
  complete: boolean;
  truncated_reason?: string | null;
  overflow_semantics?: "explicit_partial" | "";
};

/**
 * 一条枚举出的来源元素或知识对象。两种用途共享一个线上形状（真源见
 * `backend/app/models/ask.py TypedCollectionItem`）：元素行填
 * `source_id`/`source_title`/`element_type`/`location_label`/`text`/`asset_id`；
 * 知识对象行填 `name`/`section_path`/`evidence_element_ids`。`notebook_id` 两种都填
 * （多领域基准库场景下是证据的真实来源笔记本，不是"只在跨库命中才非空"的
 * Citation.notebook_id 惯例）。
 */
export type TypedCollectionItem = {
  item_id: string;
  // 仅 collection === "elements" 时有效
  // ——collection === "sources" 复用其中三个:`source_id`/`source_title` 同义
  // （被列出的那份文档本身）、`location_label` 装文档类型的界面词（如「学术论文」，
  // 后端 PROFILES 是唯一真源，前端不再翻一遍）、`text` 装已存摘要的摘录；
  // `element_type` 保持空串（文档不是元素，分派看 `collection`）。
  source_id?: string;
  source_title?: string;
  element_type?: string;
  location_label?: string;
  text?: string;
  /** 仅 image 元素非空；空串代表"非图片/无图片资产"，不是 null。 */
  asset_id?: string;
  // 仅 collection === "kg_objects" 时有效
  name?: string;
  section_path?: string;
  evidence_element_ids?: string[];
  /** 首个仍存活的权威原文出处；跨库时由后端在 participant 范围内代理解析。 */
  citation?: Citation;
  // 两种都填
  notebook_id?: string;
  tier?: string;
};

/**
 * 一份类型化的元素/知识对象/文档清单结果（PR-2 枚举工具，PR-2.5 加入文档清单）。
 * 与 `KnowhowResultSet` 并列进 `AskResponse.result_sets`，按 `kind` 判别；
 * `coverage`（而非它在相关度排序里的位置）才是完整/部分的权威依据。
 * `collection === "sources"`（库里的文档清单）没有子类型，`element_kind` 与
 * `object_type` 两个字段对它都为空。真源：
 * `backend/app/models/ask.py TypedCollectionResult`。
 */
export type TypedCollectionResult = {
  kind: "collection";
  collection: "elements" | "kg_objects" | "sources";
  /** collection === "elements" 时非空："formula" | "table" | "image" | "code_block"。 */
  element_kind?: string;
  /** collection === "kg_objects" 时非空："concept" | "claim" | "formula" | "procedure"。 */
  object_type?: string;
  /** 非空时表示这份清单被限定在单个来源范围内。 */
  source_id?: string;
  items: TypedCollectionItem[];
  coverage: TypedCollectionCoverage;
  /** 实际进入本轮答案合成预览的条目数；与 coverage.returned_total（枚举出的总条目数）分开披露。 */
  synthesis_rows: number;
  /** null = 本轮没有尝试过合成预览（模型未配置，或从未触发）；不要渲染成"分析不完整"。 */
  synthesis_complete?: boolean | null;
};

/**
 * 一轮回答**实际获准搜索的范围**的只读回执。真源：
 * `backend/app/models/source_scope.py RetrievalScopeReceipt`。
 *
 * ⚠ 只在**确有收窄**时才由后端产生（浏览器每次都会提交显式范围，全选也不例外，
 * 所以「回执在场」是信号、「范围对象在场」不是）。缺席 = 这一轮两维都是全量，
 * 或那条回答早于本特性；两种情况都不该渲染。
 *
 * ⚠ `bases[].name` 是**授权时刻的持久化快照**，不是当前挂载表的查询键：一条回答
 * 活得比挂载边久，重开历史会话时那个库可能已经被卸载、易主或降级，拿当前
 * `base_notebooks` 重新映射恰好会丢掉最该解释这条回答的那一行。
 */
export type RetrievalScopeReceipt = {
  local: { selected: number; total: number };
  bases: { notebook_id: string; name: string; included: boolean }[];
};

/**
 * A pointer to material OUTSIDE this notebook (``ask.gap_consult``, X9 PR-A).
 * Deliberately just these four fields — no source_id/element_id/relevance/
 * citation key, because nothing here was retrieved, cited, anchored, or
 * shown to the answering model. Real source:
 * `backend/app/models/ask.py AskGapSuggestion`.
 */
export type GapSuggestion = {
  title: string;
  url: string;
  summary?: string;
  source_label?: string;
};

export type AskResponse = {
  answer_id: string;
  asked_at?: string;
  answered_at?: string;
  conversation_id: string;
  conclusion: string;
  answer: string;
  grounded: boolean;
  anchors: AnswerAnchor[];
  related_knowledge: KnowledgeRecord[];
  citations: Citation[];
  llm_mode: string;
  evidence_level?: "grounded" | "overview" | "inferred";
  retrieval_query?: string;
  top_relevance?: number;
  reasoning_trace?: ReasoningTraceStep[];
  intent?: import("./ask-intent-model").QueryIntentContract;
  mode?: string;
  /** 提交本轮时选的 reasoning 检索档位；历史打开时据此恢复。 */
  retrieval_effort?: import("./ask-retrieval-effort").AskRetrievalEffortId;
  /** 本轮实际获准的检索范围；后端只在确有收窄时下发，缺席即不渲染。 */
  retrieval_scope?: RetrievalScopeReceipt | null;
  /**
   * complete / aggregate / hybrid 查询的可验证行集，独立于 Markdown 摘要。
   * kind 判别的 union（PR-2 T5/T6）：Knowhow 的整表批次（kind="knowhow"）与
   * 枚举工具产出的类型化元素/知识对象清单（kind="collection"）共用这个数组，
   * 渲染必须按 kind 分派，不能按下标猜测；未知 kind 一律跳过，不得当作 knowhow
   * 卡片渲染（会在 `.rows` 上炸出 TypeError）。
   */
  result_sets?: (KnowhowResultSet | TypedCollectionResult)[];
  /** 整个请求范围的覆盖率；与每张已选表自身的 coverage 分开。 */
  result_coverage?: KnowhowBatchCoverage;
  /** 站外来源建议（``ask.gap_consult``）：不是证据，缺席时后端按 `exclude_if`
   *  惯例整键缺席（零插件部署与历史回答因此逐字节相同）。 */
  gap_suggestions?: GapSuggestion[];
  model_errors?: {
    service_id: string;
    service_name: string;
    workload_id: string;
    workload_label: string;
    stage: string;
    model: string;
    message: string;
    support_id: string;
  }[];
  index_required?: boolean;
};

export type ChatTurn = { question: string; response: AskResponse; askedAt?: string };
export type ConversationSummary = {
  id: string;
  title: string;
  updated_at: string;
  turn_count: number;
  used_reasoning?: boolean;
};
export type ConversationDetail = {
  id: string;
  notebook_id: string;
  title: string;
  updated_at: string;
  turn_count: number;
  used_reasoning?: boolean;
  turns: {
    answer_id: string;
    question: string;
    response: AskResponse;
    asked_at: string;
    created_at: string;
  }[];
  active_job?: {
    job_id: string;
    question: string;
    asked_at: string;
    mode: string;
    trace: ReasoningTraceStep[];
  };
};

/** 一条会话的公开链接 + 读取水位（T2）。真源：`backend/app/models/ask.py
 *  ConversationShareResponse`。「分享」与「更新到最新」是同一个调用，所以每次回执
 *  都带当前水位，前端据此显示快照截止到哪一轮。 */
export type ConversationShareResponse = {
  share_token: string;
  /** 水位时刻——快照包含到"内容截至何时"的最后一条答案。 */
  shared_through_at: string;
  shared_through_id: string;
};

export type ChatMode = "ask" | "rules" | "reports" | "memory";
export const CHAT_MODES: Array<[ChatMode, string]> = [
  ["ask", "问答"],
  ["rules", "知识库"],
  ["memory", "记忆"],
  ["reports", "深度报告"],
];

export type KnowledgeKind = string;
export type KnowledgeFieldValue = { key: string; value: string };
export type KnowledgeTypeCount = { object_type: string; label: string; count: number };

export type ObjectSchema = {
  object_type: string;
  plural: string;
  fields: string[];
  primary: string;
  description: string;
  label: string;
  list_fields: string[];
  source: string;
  status: string;
  rationale: string;
  notebook_id: string;
  /** 当前响应里的生效范围：全局基线或当前笔记本条目。 */
  scope?: "global" | "notebook";
  /** 当前笔记本仍直接采用全局基线。 */
  inherited?: boolean;
  /** 当前笔记本有同名、覆盖全局基线的版本。 */
  overrides_global?: boolean;
  /** 后端按当前用户和视图计算出的写权限。 */
  can_edit?: boolean;
};

export type KnowledgeRecord = {
  id: string;
  object_type: string;
  headline?: string;
  fields: KnowledgeFieldValue[];
  status: string;
  owner?: string;
  last_reviewed?: string;
  evidence: Evidence[];
};

export const KNOWLEDGE_STATUS_OPTIONS = [
  "reviewed",
  "approved",
  "deprecated",
  "conflict",
  "project_specific",
];

export type KnowledgeItem = {
  id: string;
  status: string;
  owner?: string;
  last_reviewed?: string;
  evidence: Evidence[];
  title?: string;
  statement?: string;
  applies_to?: string[];
  recommendation?: string;
  risk_if_ignored?: string;
  severity?: string;
  name?: string;
  use_when?: string;
  benefit?: string;
  limitation?: string;
  description?: string;
  term?: string;
  definition?: string;
  headline?: string;
  object_type?: string;
  fields?: KnowledgeFieldValue[];
};

export const EMPTY_KNOWLEDGE: Record<string, KnowledgeItem[] | null> = {};

export type KnowledgeRef = { id: string; object_type: string; headline: string; status: string };
export type DuplicateGroup = { object_type: string; similarity: number; members: KnowledgeRef[] };
export type NotebookAnalytics = {
  answers_total: number;
  feedback_useful: number;
  feedback_not_useful: number;
  usefulness_rate: number;
  low_rated_questions: string[];
  candidate_counts: Record<string, number>;
  knowledge_counts: Record<string, number>;
  source_status_counts: Record<string, number>;
  paper_meta_counts?: { has_meta: number; marker: number; missing: number };
};

export type KnowledgeNode = { id: string; object_type: string; headline: string; status: string };
export type KnowledgeEdge = { from_id: string; to_id: string; relation: string; label: string };
export type KnowledgeGraph = { nodes: KnowledgeNode[]; edges: KnowledgeEdge[] };
export type UnifiedConceptNode = {
  id: string;
  object_type: string;
  payload: { name?: string; [key: string]: unknown };
};
export type UnifiedEdge = {
  source_object_id: string;
  target_object_id: string;
  edge_type: string;
  support_count?: number;
  source_count?: number;
};
export type UnifiedGraphResp = {
  nodes: UnifiedConceptNode[];
  edges: UnifiedEdge[];
  total_nodes?: number;
  total_edges?: number;
  truncated?: boolean;
  viz_building?: boolean;
};
export type EvidenceItem = {
  source_id: string;
  source_title: string;
  element_id: string;
  element_type: string;
  location_label: string;
  quoted_span: string;
  confidence: number;
  element_text?: string;
};
export type KgObject = {
  id: string;
  object_type: string;
  payload: { name?: string; section_path?: string; [key: string]: unknown };
  evidence: EvidenceItem[];
  edge_type?: string;
};
export type ConceptDetailResp = {
  canonical_id: string;
  canonical_name: string;
  members: KgObject[];
  attached: KgObject[];
  evidence: EvidenceItem[];
  // Hub-cluster member pagination (R3·T-B2): `attached`/`evidence` are scoped
  // to `members` on THIS page, not the whole cluster — paging through every
  // page still surfaces the complete set.
  member_total: number;
  next_cursor: string | null;
};
export type KgOccurrence = {
  quoted_span?: string;
  source_title?: string;
  source_id?: string;
  element_text?: string;
  location_label?: string;
  element_type?: string;
  confidence?: number;
};
export type KgProcedureStep = { name: string; element_text: string };
export type NodeContext = {
  id: string;
  object_type: string;
  name: string;
  section_path: string;
  occurrences: KgOccurrence[];
  definition: string | null;
  steps: KgProcedureStep[] | null;
};
export type PendingMerge = { id: string; canonical_a: string; canonical_b: string; score: number; status: string };
export type UnifiedKgStatus = {
  dirty: boolean;
  last_rebuild_at: string;
  objects: number;
  relations: number;
  clusters: number;
  viz_indexed: boolean;
  viz_nodes: number;
  viz_edges: number;
  viz_stale: boolean;
  viz_building?: boolean;
};
export type MergeReviewSummary = { reviewed: number; confirmed: number; rejected: number; unsure: number };
export type MergeReviewJob = { status: string; total: number; done: number; error: string };
export type FgNode = {
  id: string;
  name: string;
  type: string;
  val: number;
  degree: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
};
export type FgLink = { source: string | FgNode; target: string | FgNode; label: string; sourceCount?: number };
export type KgSearchHit = { object_id: string; name: string; object_type: string; score: number; match: string };
export type KgSearchResp = { query: string; hits: KgSearchHit[] };
export type KgNeighborsResp = {
  nodes: UnifiedConceptNode[];
  edges: UnifiedEdge[];
  /** Canonical graph id for a raw cited concept id. */
  focus_id?: string;
  /** Raw knowledge-object id used for context when focus_id is synthetic. */
  focus_object_id?: string;
  /** The large-notebook viz artifact is not ready, so bounded focus is unavailable. */
  locating_unavailable?: boolean;
  /** Participant that owns the resolved node; reads remain authorized by the active notebook. */
  source_notebook_id?: string;
};
