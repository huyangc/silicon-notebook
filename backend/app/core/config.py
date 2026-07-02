from functools import lru_cache
from typing import Annotated, List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = Field("development", validation_alias="SILICON_NOTEBOOK_ENV")
    single_user_email: str = Field(
        "local-user@silicon-notebook.dev",
        validation_alias="SILICON_NOTEBOOK_SINGLE_USER_EMAIL",
    )
    single_user_name: str = Field(
        "Local Curator",
        validation_alias="SILICON_NOTEBOOK_SINGLE_USER_NAME",
    )
    # 用户系统：admin 初始密码（每次启动据此重置 admin 密码；改密=改此变量后重启）。
    admin_password: str = Field("admin", validation_alias="SILICON_NOTEBOOK_ADMIN_PASSWORD")
    # True 时无 token 的请求回退为 seeded admin（仅本地/测试用）；生产保持 False=强制登录。
    auth_optional: bool = Field(False, validation_alias="SILICON_NOTEBOOK_AUTH_OPTIONAL")
    # 每用户模型配置策略。"fallback"(第一阶段)=用户没配则回退系统 env 默认；
    # "required"(第二阶段)=用户没配则该服务不可用(解析为 none，经 model_error 通道提示)。
    user_model_config_policy: str = Field("fallback", validation_alias="USER_MODEL_CONFIG_POLICY")

    openai_compat_base_url: str = Field("", env="OPENAI_COMPAT_BASE_URL")
    openai_compat_api_key: str = Field("", env="OPENAI_COMPAT_API_KEY")
    openai_compat_model: str = Field("", env="OPENAI_COMPAT_MODEL")
    openai_compat_timeout_seconds: int = Field(
        60,
        env="OPENAI_COMPAT_TIMEOUT_SECONDS",
    )
    # Extra attempts on transient connection/timeout errors (total = 1 + this).
    # We drive retries ourselves with the SDK's max_retries pinned to 0.
    openai_compat_max_retries: int = Field(
        2,
        env="OPENAI_COMPAT_MAX_RETRIES",
    )
    # 单次输出 token 上限(max_tokens)。全局默认应用到所有 LLM 生成调用(改写/扩展、
    # 冲突/合并预审、graph-reason、元信息等短输出);0=不传、由服务端默认。答案综合与
    # KG 抽取输出更长,各自走下方更高的专用上限(answer_max_tokens / kg_extract_max_tokens)。
    openai_compat_max_tokens: int = Field(
        8192,
        validation_alias="OPENAI_COMPAT_MAX_TOKENS",
    )
    # 答案综合(chunk/mix/reasoning/graph 最终答案)单次输出上限:长对比表 + 推理散文可能
    # 很长,给足 headroom 避免截断。注意与大输入共享模型总窗口,不宜盲目再调高。
    answer_max_tokens: int = Field(
        16384,
        validation_alias="ANSWER_MAX_TOKENS",
    )

    # 推理搜索 (mode=reasoning) 专用 LLM 端点（可选）。三项全部非空时推理路径改用此
    # 模型，与全局 OPENAI_COMPAT_* 解耦；任一为空 → 整体回退全局。超时/重试沿用
    # reasoning_timeout_seconds / reasoning_max_retries，此处不另设。
    reasoning_llm_base_url: str = Field("", env="REASONING_LLM_BASE_URL")
    reasoning_llm_api_key: str = Field("", env="REASONING_LLM_API_KEY")
    reasoning_llm_model: str = Field("", env="REASONING_LLM_MODEL")
    # 查询改写/扩展专用快模型(如 DeepSeek v4-fast)。只填 MODEL 即启用,base_url/api_key
    # 缺省则复用主 OPENAI_COMPAT_* 端点;未填则改写/扩展回退到主模型。
    rewrite_llm_base_url: str = Field("", env="REWRITE_LLM_BASE_URL")
    rewrite_llm_api_key: str = Field("", env="REWRITE_LLM_API_KEY")
    rewrite_llm_model: str = Field("", env="REWRITE_LLM_MODEL")
    # KG 构建/融合专用 LLM 端点（可选，批量离线任务）。三项全部非空时 KG 路径改用此
    # 模型（重抽取/融合/冲突消解/概念描述）；任一为空 → 整体回退全局主模型。
    kg_llm_base_url: str = Field("", env="KG_LLM_BASE_URL")
    kg_llm_api_key: str = Field("", env="KG_LLM_API_KEY")
    kg_llm_model: str = Field("", env="KG_LLM_MODEL")

    embed_provider: str = Field("", env="EMBED_PROVIDER")          # ""(off) | dashscope
    embed_model: str = Field("", env="EMBED_MODEL")
    embed_base_url: str = Field("", env="EMBED_BASE_URL")
    embed_api_key: str = Field("", env="EMBED_API_KEY")
    embed_dim: int = Field(1024, env="EMBED_DIM")
    llm_cache_enabled: bool = Field(False, env="LLM_CACHE_ENABLED")
    llm_cache_path: str = Field(".local/llm_cache.db", env="LLM_CACHE_PATH")

    # --- 大文档摄取/检索旋钮（2026-06-04 大文档加固）---
    # KG 窗口化：相邻 prose 贪心打包到 target 字符、相邻窗口 overlap。
    # 抽取窗口：0=按文档大小+并发自适应（见 plan_window_size）；>0=固定字符数（覆盖/调试）。
    kg_window_target_chars: int = Field(0, env="KG_WINDOW_TARGET_CHARS")
    # 自适应窗口的下/上限：level = clamp(内容字符/并发, min, max)。
    kg_window_min_chars: int = Field(4000, env="KG_WINDOW_MIN_CHARS")
    kg_window_max_chars: int = Field(8000, env="KG_WINDOW_MAX_CHARS")
    kg_window_overlap_chars: int = Field(450, env="KG_WINDOW_OVERLAP_CHARS")
    # KG 抽取并发线程数。
    kg_extract_workers: int = Field(16, env="KG_EXTRACT_WORKERS")
    # KG 抽取单次输出 token 上限:一个窗口可能抽出很多节点/关系(JSON 较大),给足避免
    # 截断(截断→JSON 坏→静默空抽取)。也是原「输出太大超时」处的上限兜底而非目标。
    kg_extract_max_tokens: int = Field(51200, validation_alias="KG_EXTRACT_MAX_TOKENS")
    # 抽取自校验: 默认开启(2026-06-18,攻 KG 内容质量瓶颈); 每窗口抽取完再做一次 LLM refine pass 剔幻觉。仅建图时生效。
    kg_refine_enabled: bool = Field(True, env="KG_REFINE_ENABLED")
    # gleaning 补抽: 默认开启(2026-06-18); 每窗口首抽完再多轮让 LLM 补"遗漏的节点"(提 recall)。仅建图时生效。
    kg_gleaning_enabled: bool = Field(True, env="KG_GLEANING_ENABLED")
    kg_gleaning_rounds: int = Field(1, env="KG_GLEANING_ROUNDS")
    # 概念簇描述融合: 默认开启(2026-06-18); 对 ≥2 成员的概念簇用 LLM 融合证据成一段描述。仅 rebuild_unified 时生效。
    kg_concept_desc_enabled: bool = Field(True, env="KG_CONCEPT_DESC_ENABLED")
    # 社区摘要: 默认关闭; 开启后对每个社区用 LLM 生成 title/summary/findings 报告。
    kg_community_summary_enabled: bool = Field(False, env="KG_COMMUNITY_SUMMARY_ENABLED")
    # 增量融合: 默认开启; 每次文档抽取后立即将新子图与全局 KG 增量融合（无需手动 rebuild）。
    kg_incremental_fusion_enabled: bool = Field(True, env="KG_INCREMENTAL_FUSION_ENABLED")
    # Tier2 桥接检测成本护栏:已有 concept 数超此值则跳过 Tier2(Tier1 名种子 append 照跑)。
    kg_incremental_tier2_max_entities: int = Field(50000, env="KG_INCREMENTAL_TIER2_MAX_ENTITIES")
    # unified 聚类 rep-ANN 上限:唯一 name-seed 超此值则分片建 ANN + WARNING(绝不静默截断)。
    kg_cluster_rep_ann_max: int = Field(2_000_000, validation_alias="KG_CLUSTER_REP_ANN_MAX")
    # 同时抽取的文档数上限（作业池容量）。窗口级并发仍由 KG_EXTRACT_WORKERS 全局封顶。
    kg_job_concurrency: int = Field(8, env="KG_JOB_CONCURRENCY")
    # LLM 连接池为交互式 ask 预留的连接数（连接池容量 = KG_EXTRACT_WORKERS + 此值）。
    kg_ask_reserve: int = Field(64, env="KG_ASK_RESERVE")
    # 单文档窗口数超过此值 → 记 WARN（不截断、不丢弃，仍全量抽取）。
    kg_window_warn_threshold: int = Field(1200, env="KG_WINDOW_WARN_THRESHOLD")
    # 孤立节点补连: 默认开启。抽取后(inline)及 build_notebook_kg 末尾(backfill)用确定性
    # 信号(共享证据元素 + 概念名命中文本)在**同源内**为 degree-0 节点补边,治 ~22%
    # gleaning/首遍无边的孤立节点。注意 pydantic-settings v2 用 validation_alias 映射环境变量。
    kg_relink_enabled: bool = Field(True, validation_alias="KG_RELINK_ENABLED")
    # embedding：每条截断长度、每条 API 批大小、落库分块大小。
    embed_truncate_chars: int = Field(2000, env="EMBED_TRUNCATE_CHARS")
    embed_batch_size: int = Field(10, env="EMBED_BATCH_SIZE")
    embed_persist_chunk: int = Field(200, env="EMBED_PERSIST_CHUNK")
    # 元素向量化并发度（并行发出的 batch 请求数；dashscope 单请求 batch≤10）。
    # 注意：批量并发上传多文档时，每文档各自以此并发嵌入，峰值会叠加，过高会打爆
    # embedding 服务 QPS（429 limit_requests）导致向量缺失；配合下面的 429 退避重试，
    # 默认取一个温和值。账户 QPS 高时可上调。
    embed_concurrency: int = Field(8, env="EMBED_CONCURRENCY")
    # embedding 限流（429）退避重试：批量摄取易瞬时超 QPS，退避到窗口恢复而非丢批。
    embed_rate_limit_retries: int = Field(5, env="EMBED_RATE_LIMIT_RETRIES")
    embed_rate_limit_base_delay: float = Field(2.0, env="EMBED_RATE_LIMIT_BASE_DELAY")
    # SQLite 忙等待超时（毫秒），配合 WAL 支持后台向量化与抽取并发写。
    db_busy_timeout_ms: int = Field(30000, env="DB_BUSY_TIMEOUT_MS")
    # 检索：top-N 知识对象。
    retrieval_top_n: int = Field(12, env="RETRIEVAL_TOP_N")
    # 检索排序: 默认用关键词+语义加权融合; 开启后改用 BM25 与语义的 RRF 融合排序。
    retrieval_rrf_enabled: bool = Field(False, env="RETRIEVAL_RRF_ENABLED")
    retrieval_rrf_k: int = Field(60, env="RETRIEVAL_RRF_K")
    chunk_ann_enabled: bool = Field(True, env="CHUNK_ANN_ENABLED")  # 有索引的库 chunk 检索走 ANN 核⊕delta(默认开:大库全量暴力不可扩展;小库无 scale 索引→_scale_index 返 None→自然回退暴力,零影响)。ANN 是语义候选,纯关键词命中缺口待 chunk 侧 FTS(词法∪语义)补;可用 CHUNK_ANN_ENABLED=false 关
    index_suggest_chunk_threshold: int = Field(2000, env="INDEX_SUGGEST_CHUNK_THRESHOLD")  # 未索引库总 chunk 超此 → 建议建索引
    index_stale_delta_threshold: int = Field(500, env="INDEX_STALE_DELTA_THRESHOLD")        # 已索引库 delta chunk 超此 → 建议重建
    scale_index_offpeak_start_hour: int = Field(2, env="SCALE_INDEX_OFFPEAK_START_HOUR")    # 低峰窗口起(含)
    scale_index_offpeak_end_hour: int = Field(6, env="SCALE_INDEX_OFFPEAK_END_HOUR")        # 低峰窗口止(不含);start>end 视为跨零点
    scale_index_scheduler_poll_seconds: int = Field(300, env="SCALE_INDEX_SCHEDULER_POLL_SECONDS")  # 调度器轮询间隔
    # 已索引大库检索策略:默认只搜已索引部分(ANN 核 ∪ FTS 词法);delta(水位后新增 source)
    # 的 chunk 不做暴力语义补召回。True 时对 delta 额外暴力(强一致,但大库慢),供 opt-in。
    # 配合 scale_auto_fold_on_add:新增内容排增量 fold,使 delta 尽快进索引取代暴力。
    # pydantic-settings v2 下 Field(env=...) 对新字段静默失效,必须用 validation_alias。
    scale_search_include_delta: bool = Field(False, validation_alias="SCALE_SEARCH_INCLUDE_DELTA")
    scale_auto_fold_on_add: bool = Field(True, validation_alias="SCALE_AUTO_FOLD_ON_ADD")            # 已索引库新增内容后自动排增量 fold(idle,合并多次新增),使 delta 尽快进索引
    # 大库自动建/重建检索索引(复用分享/拷贝的「大」定义 notebook_copy_stats().copyable==False):
    # 默认开,写路径(抽取完成/rebuild_unified_kg)与检索回退路径均可触发,入队走既有
    # trigger_scale_index_rebuild 去重/状态机,零前端改动。pydantic-settings v2 下
    # Field(env=...) 对新字段静默失效,必须用 validation_alias。
    scale_index_auto_enabled: bool = Field(True, validation_alias="SCALE_INDEX_AUTO_ENABLED")
    # "idle"=默认低峰窗口重建(避免高峰抢核);"now"=立即后台重建。Literal 约束:
    # 非法取值(如拼错的 env)在 Settings() 构造期就 ValidationError 快速失败,不静默
    # 落入 trigger_scale_index_rebuild 的 "now" 分支(见 fix review #4 finding 1)。
    scale_index_auto_when: Literal["idle", "now"] = Field("idle", validation_alias="SCALE_INDEX_AUTO_WHEN")
    # KG 视图 viz 索引:notebook 有效对象数(status!='deprecated')≤ 此阈值时,首次打开 KG 视图
    # 仍同步懒建(现有行为,小库瞬时);> 阈值则后台构建 + GET 立即返回 viz_building 占位,避免
    # 分钟级全图折叠拖垮请求线程(真机 49 万对象库卡死的根因)。pydantic-settings v2 下
    # Field(env=...) 对新字段静默失效,必须用 validation_alias。
    viz_sync_build_max_objects: int = Field(20000, validation_alias="VIZ_SYNC_BUILD_MAX_OBJECTS")
    # 大库(> viz_sync_build_max_objects)unified_graph 请求缺省 limit 时的服务端兜底上限。
    # 折叠视图本就是 object 级有界核心图,防止「不传 limit」的旧前端/裸调用绕过大库守卫
    # 落到 _unified_graph_full(全量拉取+多 GB 缓存,49 万对象库会打满 64GB 内存)。
    viz_default_limit: int = Field(300, validation_alias="VIZ_DEFAULT_LIMIT")
    # qwen3-rerank (DashScope text-rerank) 配置
    rerank_model: str = Field("", env="RERANK_MODEL")
    rerank_base_url: str = Field("https://dashscope.aliyuncs.com/api/v1", env="RERANK_BASE_URL")
    rerank_api_key: str = Field("", env="RERANK_API_KEY")
    rerank_max_docs: int = Field(500, env="RERANK_MAX_DOCS")
    # rerank 接口风格:dashscope=DashScope 原生 text-rerank(嵌套 input/output);
    # openai=OpenAI 兼容(vLLM/Cohere 等)的 {base}/rerank(扁平 body、顶层 results)。
    rerank_api_style: str = Field("dashscope", env="RERANK_API_STYLE")
    relation_retrieval_enabled: bool = Field(False, env="RELATION_RETRIEVAL_ENABLED")
    relation_seed_top_n: int = Field(8, env="RELATION_SEED_TOP_N")
    # P0-1/2: 关系候选池上限(有关系向量时,先按向量 sim 取 top-N 候选再 hydrate 文本
    # 做关键词+语义融合打分;镜像 chunk_recall 的"猛召回池"语义,非最终截断
    # 的 relation_seed_top_n/_MIX_REL_SEEDS——池要显著大于两者才不伤召回)。
    relation_recall: int = Field(200, validation_alias="RELATION_RECALL")
    # HippoRAG 式 PPR 跨文档检索(graph 模式;默认开)
    graph_ppr_enabled: bool = Field(True, env="GRAPH_PPR_ENABLED")
    ppr_damping: float = Field(0.5, env="PPR_DAMPING")               # rx.pagerank alpha
    ppr_passage_node_weight: float = Field(0.05, env="PPR_PASSAGE_NODE_WEIGHT")
    ppr_top_chunks: int = Field(20, env="PPR_TOP_CHUNKS")            # 最终喂答案的 chunk 数
    ppr_kg_seed_top_n: int = Field(20, env="PPR_KG_SEED_TOP_N")      # reset 向量里的 KG 种子数
    ppr_chunk_seed_top_n: int = Field(30, env="PPR_CHUNK_SEED_TOP_N")  # reset 向量里的 chunk 种子数
    ppr_fact_rerank_enabled: bool = Field(False, env="PPR_FACT_RERANK_ENABLED")  # LLM 过滤候选种子(每查一次 LLM)
    ppr_variant_edge_weight: float = Field(0.5, env="PPR_VARIANT_EDGE_WEIGHT")
    ppr_emb_synonym_enabled: bool = Field(True, env="PPR_EMB_SYNONYM_ENABLED")
    ppr_emb_synonym_threshold: float = Field(0.83, env="PPR_EMB_SYNONYM_THRESHOLD")
    ppr_emb_synonym_topk: int = Field(20, env="PPR_EMB_SYNONYM_TOPK")
    ppr_emb_synonym_max_entities: int = Field(50000, env="PPR_EMB_SYNONYM_MAX_ENTITIES")  # cost guard
    ppr_community_context_top_n: int = Field(3, env="PPR_COMMUNITY_CONTEXT_TOP_N")
    kg_canonical_fold_enabled: bool = Field(False, env="KG_CANONICAL_FOLD_ENABLED")
    kg_about_downweight_enabled: bool = Field(False, env="KG_ABOUT_DOWNWEIGHT_ENABLED")
    # 孤立(度为0)KG 节点的检索排序降权乘子。仅压 score 不动 relevance([0,1]/tau 守恒)。
    # 1.0=不降权(no-op); 0.5=孤立节点 score 减半;与 _EDGE_TYPE_RANK_WEIGHT 同模式。
    kg_isolated_rank_penalty: float = Field(0.5, validation_alias="KG_ISOLATED_RANK_PENALTY")
    # hnsw 建索引质量参数(recall/build-time 折衷)。三处共用同一值:build_scale_index
    # 里唯一的一次 KG-embedding hnsw 构建(emb_synonym KNN 复用点 + 持久化 ann.bin)、
    # chunk ANN(save_scale_index 里的 chunk_ann.bin)、emb_synonym_edges 未传
    # prebuilt_index 时的内部自建 fallback(_rx_graph 联邦路径等)。真机 49 万对象库
    # hnsw 构建是流水线里最贵的计算,把该参数暴露出来便于按部署规模调优,不改默认值。
    hnsw_ef_construction: int = Field(200, validation_alias="HNSW_EF_CONSTRUCTION")
    # 跨文档概念合并预审(review_pending_merges)的非对称自动落地阈值。单一 0.95 时
    # 绝大多数 LLM 判定落入 unsure → 队列永不清空。改为非对称:auto-merge 需更高置信
    # (误并污染图、不可逆),auto-keep-separate 可低些(误判仅多留一对待审、无害)。
    kg_merge_confirm_threshold: float = Field(0.90, validation_alias="KG_MERGE_CONFIRM_THRESHOLD")
    kg_merge_separate_threshold: float = Field(0.80, validation_alias="KG_MERGE_SEPARATE_THRESHOLD")
    # 合并复核(review_merge_candidates)每次送 LLM 的候选批大小。分批=每条回复更短,
    # 规避大候选集时单条回复超输出上限被截断→JSON 解析炸→拖垮 rebuild_unified_kg。
    kg_merge_review_batch_size: int = Field(30, validation_alias="KG_MERGE_REVIEW_BATCH_SIZE")
    # 进程级 VectorCache（embedding 矩阵/分词语料/rustworkx 图等 GB 级条目）的
    # LRU 容量上限，防止 fed_rxgraph 等按用户/notebook 线性增长的条目无界占用内存。
    vector_cache_max_entries: int = Field(32, validation_alias="VECTOR_CACHE_MAX_ENTRIES")
    answer_context_budget_chars: int = Field(6000, env="ANSWER_CONTEXT_BUDGET_CHARS")
    answer_context_min_items: int = Field(3, env="ANSWER_CONTEXT_MIN_ITEMS")
    # grounded 三档阈值（作用于融合相关度 .relevance ∈[0,1]）。
    # 注意：现有 grounded 测试要求 tau_high ≤ 0.4（纯关键词命中融合分=0.4）。
    evidence_tau_low: float = Field(0.18, env="EVIDENCE_TAU_LOW")
    evidence_tau_high: float = Field(0.35, env="EVIDENCE_TAU_HIGH")
    # 流程类问题 top-N 至少保底召回的 procedure 条数。
    proc_min: int = Field(2, env="PROC_MIN")
    # 推理模式(mode=reasoning)护栏: Reflect 循环总步数 circuit breaker。
    reasoning_max_steps: int = Field(50, env="REASONING_MAX_STEPS")
    # 推理模式 Plan 输出子查询数上限。
    reasoning_max_subqueries: int = Field(5, env="REASONING_MAX_SUBQUERIES")
    # 复合问题最终排序: 开启后按子查询配额 round-robin 选 top-N(避免整串全局排序让
    # 信息量大的一方通吃); 关闭则回退全局重排。单子查询时自动等价全局。
    reasoning_quota_enabled: bool = Field(True, env="REASONING_QUOTA_ENABLED")
    # 退化循环熔断: 连续 N 轮无有效进展(含反复请求已访问节点)即强制收尾作答,
    # 不空转到 reasoning_max_steps; search_elements 累计次数上限(防"每次有新增但永不满足")。
    reasoning_stale_limit: int = Field(3, env="REASONING_STALE_LIMIT")
    reasoning_max_element_searches: int = Field(5, env="REASONING_MAX_ELEMENT_SEARCHES")
    # 推理模式(交互式,用户在线等)专用的 per-call LLM 超时/重试,与批量抽取
    # 的全局 openai_compat_* 解耦：单步更短超时 + 更少重试，避免卡死时久等。
    reasoning_timeout_seconds: int = Field(90, env="REASONING_TIMEOUT_SECONDS")
    reasoning_max_retries: int = Field(1, env="REASONING_MAX_RETRIES")
    # Global 问答:map-reduce 时纳入的社区报告上限(按 size 取前 N)。
    global_max_communities: int = Field(20, env="GLOBAL_MAX_COMMUNITIES")
    # 问题感知证据精炼: 默认开启(隔离 eval: 正确性 1.57→1.73 且伪引用全层→0%;
    # 代价每 ask 多 1 次 LLM)。答题前对已装配证据按问题抽"相关要点"前置,聚焦答题。设 false 关。
    kg_query_refine_enabled: bool = Field(True, env="KG_QUERY_REFINE_ENABLED")
    query_refine_max_chars: int = Field(4000, env="QUERY_REFINE_MAX_CHARS")
    # chunk-native 检索分块: chunk 目标字数 / 相邻重叠(P1 overlap 默认 0)。
    chunk_target_chars: int = Field(600, env="CHUNK_TARGET_CHARS")
    chunk_overlap_chars: int = Field(0, env="CHUNK_OVERLAP_CHARS")
    # chunk-native 检索: 大召回候选数 / MMR 精选数 / MMR λ / 答案上下文预算(长上下文综合)。
    chunk_recall: int = Field(200, env="CHUNK_RECALL")   # mix 候选池/MMR 候选;LightRAG 风格猛召回
    chunk_mmr_k: int = Field(16, env="CHUNK_MMR_K")
    chunk_mmr_lambda: float = Field(0.5, env="CHUNK_MMR_LAMBDA")
    chunk_answer_budget_chars: int = Field(30000, env="CHUNK_ANSWER_BUDGET_CHARS")
    # P3 查询改写/扩展: 多子查询上限(0=禁用多子查询); 开关(false→单查询 MMR 原路径)。
    chunk_max_subqueries: int = Field(4, env="CHUNK_MAX_SUBQUERIES")
    query_rewrite_enabled: bool = Field(True, env="QUERY_REWRITE_ENABLED")
    # P4: 摄取默认不抽 KG(只建 chunk);要严格推理时经"建图"端点/离线 CLI 按需构建。
    # True 恢复旧"每次摄取整库自动抽"行为(迁移/测试逃生口)。base 库复用 tier='base'。
    kg_auto_extract: bool = Field(False, env="KG_AUTO_EXTRACT")
    # 冲突消解(conflict resolution): 默认关闭(opt-in)。开启后建图末尾跑一遍 KG
    # 矛盾三元组检测+LLM裁决+写回(keep/discard/modify)。设计见
    # docs/superpowers/specs/2026-06-17-kg-conflict-resolution-design.md。
    kg_conflict_resolution_enabled: bool = Field(False, env="KG_CONFLICT_RESOLUTION_ENABLED")
    # 自动应用阈值: 裁决 confidence ≥ 此值才自动写回; 低于则入评审队列等人工确认。
    # 设 1.0 = 纯评审模式(只检测不自动改图)。
    kg_conflict_auto_apply_threshold: float = Field(0.95, env="KG_CONFLICT_AUTO_APPLY_THRESHOLD")
    # 语义候选阈值: 端点对象 embedding 余弦 ≥ 此值才作为 semantic 冲突候选(高=更稀疏)。
    kg_conflict_sim_threshold: float = Field(0.8, env="KG_CONFLICT_SIM_THRESHOLD")
    # chunk×graph mix: 叠加 KG 子图 block 和源 chunk 进候选池(默认开)。关闭后退化为纯 chunk 检索。
    chunk_kg_overlay_enabled: bool = Field(True, env="CHUNK_KG_OVERLAY_ENABLED")
    # chunk×graph mix token 预算(照 LightRAG 6000/8000/30000)。
    max_entity_tokens: int = Field(6000, env="MAX_ENTITY_TOKENS")
    max_relation_tokens: int = Field(8000, env="MAX_RELATION_TOKENS")
    max_total_tokens: int = Field(30000, env="MAX_TOTAL_TOKENS")

    # LLM interaction logging. Records every chat/embedding call (request,
    # response, latency, token usage, errors) to a JSONL file plus a brief
    # console line. Defaults on; no-op when the LLM is not configured.
    llm_log_enabled: bool = Field(True, env="LLM_LOG_ENABLED")
    llm_log_path: str = Field(".local/logs/llm.jsonl", env="LLM_LOG_PATH")
    llm_log_max_chars: int = Field(4000, env="LLM_LOG_MAX_CHARS")

    # General event logging: HTTP requests, async pipeline stages, status
    # transitions. Written to <event_log_dir>/<channel>.jsonl plus the console.
    event_log_enabled: bool = Field(True, env="EVENT_LOG_ENABLED")
    event_log_dir: str = Field(".local/logs", env="EVENT_LOG_DIR")
    # Requests slower than this (ms) are flagged SLOW so "stuck" calls stand out.
    slow_request_ms: int = Field(3000, env="SLOW_REQUEST_MS")

    # Read-only debug log viewer endpoints (/api/debug/logs/...). Local dev tool;
    # opt in with DEBUG_LOGS_ENABLED=true because full records may contain
    # prompt/response text from private source material.
    debug_logs_enabled: bool = Field(False, env="DEBUG_LOGS_ENABLED")

    # PDF parsing via MinerU (decoupled from GPU). Modes:
    #   "off"  -> use the built-in pypdf text fallback (default; no GPU, offline)
    #   "http" -> call a remote MinerU service (mineru-api) at mineru_api_url
    #   "cli"  -> run MinerU's Python API in an isolated subprocess
    mineru_mode: str = Field("off", env="MINERU_MODE")
    mineru_api_url: str = Field("", env="MINERU_API_URL")
    mineru_backend: str = Field("pipeline", env="MINERU_BACKEND")
    # Only used by the VLM *client* backends (vlm-http-client / vlm-sglang-client):
    # the URL of a standalone VLM inference server (vllm/sglang serving MinerU's VLM).
    mineru_vlm_server_url: str = Field("", env="MINERU_VLM_SERVER_URL")
    mineru_parse_method: str = Field("auto", env="MINERU_PARSE_METHOD")
    mineru_lang: str = Field("", env="MINERU_LANG")
    mineru_model_source: str = Field("huggingface", env="MINERU_MODEL_SOURCE")
    mineru_timeout_seconds: int = Field(600, env="MINERU_TIMEOUT_SECONDS")
    mineru_formula_enable: bool = Field(True, env="MINERU_FORMULA_ENABLE")
    mineru_table_enable: bool = Field(True, env="MINERU_TABLE_ENABLE")

    # MinerU.net cloud (v4) — parse a public PDF URL via the hosted service.
    # 独立于上面的 MINERU_MODE(off/http/cli)；仅用于 URL 来源的 PDF。
    mineru_api_token: str = Field("", env="MINERU_API_TOKEN")
    mineru_api_base: str = Field("https://mineru.net", env="MINERU_API_BASE")
    mineru_cloud_model_version: str = Field("vlm", env="MINERU_CLOUD_MODEL_VERSION")
    mineru_cloud_language: str = Field("ch", env="MINERU_CLOUD_LANGUAGE")
    mineru_cloud_formula_enable: bool = Field(True, env="MINERU_CLOUD_FORMULA_ENABLE")
    mineru_cloud_table_enable: bool = Field(True, env="MINERU_CLOUD_TABLE_ENABLE")
    mineru_cloud_timeout_seconds: int = Field(600, env="MINERU_CLOUD_TIMEOUT_SECONDS")
    mineru_cloud_poll_interval_seconds: int = Field(5, env="MINERU_CLOUD_POLL_INTERVAL_SECONDS")

    database_url: str = Field(
        "sqlite:///.local/silicon_notebook.db",
        env="DATABASE_URL",
    )
    storage_dir: str = Field(".local/storage", validation_alias="SILICON_NOTEBOOK_STORAGE_DIR")
    # Notebook 分享拷贝阈值:超过任一阈值的库仅可只读共享(Phase 2),不可深拷贝。
    notebook_copy_max_bytes: int = Field(50 * 1024 * 1024, validation_alias="NOTEBOOK_COPY_MAX_BYTES")
    notebook_copy_max_rows: int = Field(5000, validation_alias="NOTEBOOK_COPY_MAX_ROWS")
    # NoDecode 让 pydantic-settings 不把环境变量当 JSON 解析（否则逗号串会崩），
    # 改由下面的 split_cors_origins(mode="before") 处理逗号分隔；validation_alias
    # 才能真正读到 SILICON_NOTEBOOK_CORS_ORIGINS（v2 下 Field(env=...) 会被忽略）。
    cors_origins: Annotated[List[str], NoDecode] = Field(
        [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3001",
        ],
        validation_alias="SILICON_NOTEBOOK_CORS_ORIGINS",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.openai_compat_base_url
            and self.openai_compat_api_key
            and self.openai_compat_model
        )

    @property
    def reasoning_llm_configured(self) -> bool:
        return bool(
            self.reasoning_llm_base_url
            and self.reasoning_llm_api_key
            and self.reasoning_llm_model
        )

    @property
    def reasoning_llm_partially_configured(self) -> bool:
        """有些 REASONING_LLM_* 填了但非全填（疑似配漏，将整体回退全局）。"""
        vals = [self.reasoning_llm_base_url, self.reasoning_llm_api_key, self.reasoning_llm_model]
        return any(vals) and not all(vals)

    @property
    def rewrite_llm_configured(self) -> bool:
        """设了 REWRITE_LLM_MODEL 即启用专用快改写模型(base_url/api_key 缺省复用主端点)。"""
        return bool(self.rewrite_llm_model)

    @property
    def kg_llm_configured(self) -> bool:
        return bool(self.kg_llm_base_url and self.kg_llm_api_key and self.kg_llm_model)

    @property
    def embedder_configured(self) -> bool:
        return bool(
            (self.embed_provider or "").strip().lower() == "dashscope"  # 大小写不敏感:DashScope 也算配置
            and (self.embed_base_url or "").strip()
            and (self.embed_api_key or "").strip()
            and (self.embed_model or "").strip()
        )

    @property
    def mineru_enabled(self) -> bool:
        mode = (self.mineru_mode or "off").lower()
        if mode == "http":
            return bool(self.mineru_api_url)
        if mode == "cli":
            return True
        return False

    @property
    def mineru_cloud_enabled(self) -> bool:
        return bool(self.mineru_api_token)

    @property
    def sqlite_path(self) -> str:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return self.database_url[len(prefix) :]
        return ".local/silicon_notebook.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
