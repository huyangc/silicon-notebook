import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 仓库根锚点：backend/app/core/config.py -> parents[3] = 仓库根。与
# event_logging.py 的 _ROOT_DIR 同口径。相对路径类的设置项（storage_dir、
# database_url 的 sqlite 路径部分）统一在下面的 model_validator 里锚定到此处，
# 使其与进程启动时的 CWD 无关（根治 `npm run dev`（cd backend/ 再起 uvicorn）
# 与离线 CLI（仓库根跑）解析到两个不同 .local 目录的「双 .local」问题）。
_ROOT_DIR = Path(__file__).resolve().parents[3]
_ENV_FILE_OVERRIDE = os.environ.get("SILICON_NOTEBOOK_ENV_FILE")
_ENV_FILE = (
    str(_ROOT_DIR / ".env")
    if _ENV_FILE_OVERRIDE is None
    else (_ENV_FILE_OVERRIDE.strip() or None)
)


def env_file_diagnosis(root: "Path | None" = None) -> "tuple[Path, bool, list[str]]":
    """启动预检用:(期望的 .env 路径, 是否存在, 疑似改名残骸文件名列表)。

    pydantic-settings 对缺失的 env_file 静默跳过——曾把「.env 被改名成
    .env.local」演成整站默认空配置、embed 未配置、chunk 检索静默落进全库
    暴力路径的半小时假死。lookalike 只在 .env 缺失时收集(在位时留备份是
    合法习惯);.env.example 是提交进仓库的模板,永远排除。"""
    root = root or _ROOT_DIR
    env_path = root / ".env"
    if env_path.is_file():
        return env_path, True, []
    lookalikes = sorted(
        p.name for p in root.glob(".env.*")
        if p.is_file() and p.name != ".env.example"
    )
    return env_path, False, lookalikes


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
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
    auth_session_touch_interval_seconds: int = Field(
        300, validation_alias="AUTH_SESSION_TOUCH_INTERVAL_SECONDS"
    )
    # 每用户模型配置策略。"fallback"(第一阶段)=用户没配则回退系统 env 默认；
    # "required"(第二阶段)=用户没配则该服务不可用(解析为 none，经 model_error 通道提示)。
    user_model_config_policy: str = Field("fallback", validation_alias="USER_MODEL_CONFIG_POLICY")
    # Deployment-owned system model-service registry. An empty path explicitly
    # selects deterministic/offline model behavior; relative paths are anchored
    # to the repository root below so startup CWD cannot change the deployment.
    model_services_config: str = Field("", validation_alias="MODEL_SERVICES_CONFIG")

    openai_compat_timeout_seconds: int = Field(
        60,
        validation_alias="OPENAI_COMPAT_TIMEOUT_SECONDS",
    )
    # Extra attempts on transient connection/timeout errors (total = 1 + this).
    # We drive retries ourselves with the SDK's max_retries pinned to 0.
    openai_compat_max_retries: int = Field(
        2,
        validation_alias="OPENAI_COMPAT_MAX_RETRIES",
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

    # --- 深度报告(report_engine) ---
    report_max_sections: int = Field(6, validation_alias="REPORT_MAX_SECTIONS")
    # (report_section_top_n 已移除:逐节深挖与 ask 统一走 effective_top_n 自适应预算,
    #  由 retrieval_top_n / REASONING_TOP_N_* 统一治理;不再有报告专属的 KG 预算旋钮。)
    report_section_chunk_budget: int = Field(
        20000, validation_alias="REPORT_SECTION_CHUNK_BUDGET")
    report_section_max_tokens: int = Field(
        8192, validation_alias="REPORT_SECTION_MAX_TOKENS")
    # 【通识】层:允许报告引入库外参数知识(行内标注,仅报告管线读取)。
    report_allow_parametric: bool = Field(
        True, validation_alias="REPORT_ALLOW_PARAMETRIC")
    # 节间并行度复用 kg_job_concurrency(节深挖无耦合;尊重全局限流退避)。

    # --- 跨文档社区层(communities;GraphRAG 式社区检测 + 兄弟扩展) ---
    # 纯图/无 LLM 的离线资产;community_layer_enabled 关闭时既不建也不用。
    community_layer_enabled: bool = Field(True, validation_alias="COMMUNITY_LAYER_ENABLED")
    community_min_size: int = Field(3, validation_alias="COMMUNITY_MIN_SIZE")
    community_peers_topk: int = Field(8, validation_alias="COMMUNITY_PEERS_TOPK")
    community_rerank_candidates: int = Field(200, validation_alias="COMMUNITY_RERANK_CANDIDATES")

    # 共提桥接层(P2):claim 文本确定性提取 mention 边/共提对,治对比题坍缩。默认开(零 LLM,有界)。
    mention_bridge_enabled: bool = Field(True, validation_alias="MENTION_BRIDGE_ENABLED")
    # 别名 DF 双门:命中 claim 数 > max(floor, cap×总claims) 判泛词整体丢弃(如 "model"),事件计数。
    # 比例门管大库("model" 8% vs GQA 0.3%);绝对下限 floor 防小库误杀真实高频概念
    # (200 claims 的库里 GQA 命中 10 条是正常信号,2%×200=4 会误杀)。
    mention_alias_df_cap: float = Field(0.02, validation_alias="MENTION_ALIAS_DF_CAP")
    mention_alias_df_floor: int = Field(20, validation_alias="MENTION_ALIAS_DF_FLOOR")
    # mention 边在 PPR 图中的权重(与 variant 边同量级)。
    mention_edge_weight: float = Field(0.5, validation_alias="MENTION_EDGE_WEIGHT")
    # sibling_peers 的最小桥 claim 数(低于此不算同类伙伴)。
    sibling_min_bridge: int = Field(2, validation_alias="SIBLING_MIN_BRIDGE")

    kg_llm_timeout_seconds: int = Field(
        60,
        gt=0,
        validation_alias="KG_LLM_TIMEOUT_SECONDS",
    )
    kg_llm_max_retries: int = Field(
        2,
        ge=0,
        le=3,
        validation_alias="KG_LLM_MAX_RETRIES",
    )

    # --- 论文元数据抽取(paper-metadata):对 academic_paper 源的文档头部做一次小
    # LLM 调用,接地校验后入库(source_paper_meta/source_authors)。
    paper_meta_enabled: bool = Field(True, validation_alias="PAPER_META_ENABLED")
    paper_meta_head_chars: int = Field(4000, validation_alias="PAPER_META_HEAD_CHARS")

    embed_dim: int = Field(1024, validation_alias="EMBED_DIM")
    # 运行时向量维度(MRL 前缀截断 + re-normalize):所有相似度/矩阵/ANN 在截断后
    # 空间进行;0 = 关闭(不截断,行为零变化)。EMBED_DIM 语义固定为「存储/模型原生
    # 维」——**绝对禁止用改 EMBED_DIM 实现降维**:存量向量按 EMBED_DIM 过滤,改小
    # 会把全部原生维向量当异维残留丢光(全库静默失忆)。真相源永远按原生维落库,
    # 截断只在读取/构建/查询侧(见 docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md)。
    embed_runtime_dim: int = Field(0, validation_alias="EMBED_RUNTIME_DIM")
    llm_cache_enabled: bool = Field(False, validation_alias="LLM_CACHE_ENABLED")
    llm_cache_path: str = Field(".local/llm_cache.db", validation_alias="LLM_CACHE_PATH")

    # --- 大文档摄取/检索旋钮（2026-06-04 大文档加固）---
    # KG 窗口化：相邻 prose 贪心打包到 target 字符、相邻窗口 overlap。
    # 抽取窗口：0=按文档大小+并发自适应（见 plan_window_size）；>0=固定字符数（覆盖/调试）。
    kg_window_target_chars: int = Field(0, validation_alias="KG_WINDOW_TARGET_CHARS")
    # 自适应窗口的下/上限：level = clamp(内容字符/并发, min, max)。
    kg_window_min_chars: int = Field(4000, validation_alias="KG_WINDOW_MIN_CHARS")
    kg_window_max_chars: int = Field(8000, validation_alias="KG_WINDOW_MAX_CHARS")
    kg_window_overlap_chars: int = Field(450, validation_alias="KG_WINDOW_OVERLAP_CHARS")
    # KG 抽取单次输出 token 上限:一个窗口可能抽出很多节点/关系(JSON 较大),给足避免
    # 截断(截断→JSON 坏→静默空抽取)。也是原「输出太大超时」处的上限兜底而非目标。
    kg_extract_max_tokens: int = Field(51200, validation_alias="KG_EXTRACT_MAX_TOKENS")
    # 抽取自校验: 默认开启(2026-06-18,攻 KG 内容质量瓶颈); 每窗口抽取完再做一次 LLM refine pass 剔幻觉。仅建图时生效。
    kg_refine_enabled: bool = Field(True, validation_alias="KG_REFINE_ENABLED")
    # gleaning 补抽: 默认开启(2026-06-18); 每窗口首抽完再多轮让 LLM 补"遗漏的节点"(提 recall)。仅建图时生效。
    kg_gleaning_enabled: bool = Field(True, validation_alias="KG_GLEANING_ENABLED")
    kg_gleaning_rounds: int = Field(1, validation_alias="KG_GLEANING_ROUNDS")
    # 概念簇描述融合: 默认开启(2026-06-18); 对 ≥2 成员的概念簇用 LLM 融合证据成一段描述。仅 rebuild_unified 时生效。
    kg_concept_desc_enabled: bool = Field(True, validation_alias="KG_CONCEPT_DESC_ENABLED")
    # 社区摘要: 默认关闭; 开启后对每个社区用 LLM 生成 title/summary/findings 报告。
    kg_community_summary_enabled: bool = Field(False, validation_alias="KG_COMMUNITY_SUMMARY_ENABLED")
    # 增量融合: 默认开启; 每次文档抽取后立即将新子图与全局 KG 增量融合（无需手动 rebuild）。
    kg_incremental_fusion_enabled: bool = Field(True, validation_alias="KG_INCREMENTAL_FUSION_ENABLED")
    # Tier2 桥接检测成本护栏:已有 concept 数超此值则跳过 Tier2(Tier1 名种子 append 照跑)。
    kg_incremental_tier2_max_entities: int = Field(50000, validation_alias="KG_INCREMENTAL_TIER2_MAX_ENTITIES")
    # Tier2 ANN 过取系数:初始近邻数 k = top_k * 此值;kg ANN 是全类型索引(claim 密集库里
    # concept 可能被挤出近邻窗),类型过滤后 concept 命中不足 top_k 时 k 倍增重查(见
    # _tier2_bridge_candidates_ann 的迭代过取)。
    kg_tier2_ann_pad_factor: int = Field(4, validation_alias="KG_TIER2_ANN_PAD_FACTOR")
    # unified 聚类 rep-ANN 上限:唯一 name-seed 超此值则分片建 ANN + WARNING(绝不静默截断)。
    kg_cluster_rep_ann_max: int = Field(2_000_000, validation_alias="KG_CLUSTER_REP_ANN_MAX")
    # 同时抽取的文档数上限（业务作业池容量）。模型调用由服务 scheduler 全局封顶。
    kg_job_concurrency: int = Field(8, validation_alias="KG_JOB_CONCURRENCY")
    # 0 = auto:未显式设置时按核数解析为 min(cpu_count, 32)(见 _resolve_core_bound_defaults)。
    # 这是唯一按本机核数缩放的 KG 旋钮——服务端绑定旋钮(EXTRACT/JOB/EMBED)刻意不跟核数走。
    kg_cluster_ann_threads: int = Field(0, validation_alias="KG_CLUSTER_ANN_THREADS")
    # 单文档窗口数超过此值 → 记 WARN（不截断、不丢弃，仍全量抽取）。
    kg_window_warn_threshold: int = Field(1200, validation_alias="KG_WINDOW_WARN_THRESHOLD")
    # 孤立节点补连: 默认开启。抽取后(inline)及 build_notebook_kg 末尾(backfill)用确定性
    # 信号(共享证据元素 + 概念名命中文本)在**同源内**为 degree-0 节点补边,治 ~22%
    # gleaning/首遍无边的孤立节点。注意 pydantic-settings v2 用 validation_alias 映射环境变量。
    kg_relink_enabled: bool = Field(True, validation_alias="KG_RELINK_ENABLED")
    # embedding：每条截断长度、每条 API 批大小、落库分块大小。
    embed_truncate_chars: int = Field(2000, validation_alias="EMBED_TRUNCATE_CHARS")
    embed_batch_size: int = Field(10, validation_alias="EMBED_BATCH_SIZE")
    embed_commit_batches: int = Field(50, validation_alias="EMBED_COMMIT_BATCHES")
    embed_persist_chunk: int = Field(200, validation_alias="EMBED_PERSIST_CHUNK")
    # embedding 限流（429）退避重试：批量摄取易瞬时超 QPS，退避到窗口恢复而非丢批。
    embed_rate_limit_retries: int = Field(5, validation_alias="EMBED_RATE_LIMIT_RETRIES")
    embed_rate_limit_base_delay: float = Field(2.0, validation_alias="EMBED_RATE_LIMIT_BASE_DELAY")
    # SQLite 忙等待超时（毫秒），配合 WAL 支持后台向量化与抽取并发写。
    db_busy_timeout_ms: int = Field(30000, validation_alias="DB_BUSY_TIMEOUT_MS")
    # 复用连接下每条连接长期持有 page cache;总内存 = 连接数 × |cache_size|。
    # 负值=KB。默认 16MB(低于旧 64MB)以在 O(线程数) 条连接下控总内存;可按部署上调。
    sqlite_cache_size_kb: int = Field(-16384, validation_alias="SQLITE_CACHE_SIZE_KB")
    # 检索：top-N 知识对象。旧默认 12 是 6 月从 scored[:12] 魔数原样抬入、从未校准;
    # 提到 20 给简单/单方面题更多深度余量(对比题在此 floor 之上再按方面数自适应扩容)。
    retrieval_top_n: int = Field(20, validation_alias="RETRIEVAL_TOP_N")
    # 检索排序: 默认用关键词+语义加权融合; 开启后改用 BM25 与语义的 RRF 融合排序。
    retrieval_rrf_enabled: bool = Field(False, validation_alias="RETRIEVAL_RRF_ENABLED")
    retrieval_rrf_k: int = Field(60, validation_alias="RETRIEVAL_RRF_K")
    chunk_ann_enabled: bool = Field(True, validation_alias="CHUNK_ANN_ENABLED")  # 有索引的库 chunk 检索走 ANN 核⊕delta(默认开:大库全量暴力不可扩展;小库无 scale 索引→_scale_index 返 None→自然回退暴力,零影响)。ANN 是语义候选,纯关键词命中缺口待 chunk 侧 FTS(词法∪语义)补;可用 CHUNK_ANN_ENABLED=false 关
    index_suggest_chunk_threshold: int = Field(2000, validation_alias="INDEX_SUGGEST_CHUNK_THRESHOLD")  # 未索引库总 chunk 超此 → 建议建索引
    index_stale_delta_threshold: int = Field(500, validation_alias="INDEX_STALE_DELTA_THRESHOLD")        # 已索引库 delta chunk 超此 → 建议重建
    scale_index_offpeak_start_hour: int = Field(2, validation_alias="SCALE_INDEX_OFFPEAK_START_HOUR")    # 低峰窗口起(含)
    scale_index_offpeak_end_hour: int = Field(6, validation_alias="SCALE_INDEX_OFFPEAK_END_HOUR")        # 低峰窗口止(不含);start>end 视为跨零点
    scale_index_scheduler_poll_seconds: int = Field(300, validation_alias="SCALE_INDEX_SCHEDULER_POLL_SECONDS")  # 调度器轮询间隔
    # 已索引大库检索策略:默认只搜已索引部分(ANN 核 ∪ FTS 词法);delta(水位后新增 source)
    # 的 chunk 不做暴力语义补召回。True 时对 delta 额外暴力(强一致,但大库慢),供 opt-in。
    # 配合 scale_auto_fold_on_add:新增内容排增量 fold,使 delta 尽快进索引取代暴力。
    # pydantic-settings v2 下 Field(validation_alias=...) 对新字段静默失效,必须用 validation_alias。
    scale_search_include_delta: bool = Field(False, validation_alias="SCALE_SEARCH_INCLUDE_DELTA")  # 已索引库检索是否含水位后 delta:统一门控 chunk(_retrieve_chunks_ann)/KG对象(_kg_object_candidates)/关系(_relation_ann_candidates)/PPR图基底(_active_kg_delta) 四处;默认关=只检索已索引部分(delta 靠 auto-fold 收进,最终一致);开=强一致 delta 暴力(大 delta 不可扩展)
    scale_auto_fold_on_add: bool = Field(True, validation_alias="SCALE_AUTO_FOLD_ON_ADD")            # 已索引库新增内容后自动排增量 fold(idle,合并多次新增),使 delta 尽快进索引
    scale_fold_max_delta_sources: int = Field(500, validation_alias="SCALE_FOLD_MAX_DELTA_SOURCES")  # fold(含 auto 解析出的)在 delta 源数超此值时升级 full 全量重建:fold 逐源 incremental_fuse 是 O(delta) 但常数大(生产 48k 源 delta 实测不可行),大 delta 全量重建反而有界
    # 大库自动建/重建检索索引(复用分享/拷贝的「大」定义 notebook_copy_stats().copyable==False):
    # 默认开,写路径(抽取完成/rebuild_unified_kg)与检索回退路径均可触发,入队走既有
    # trigger_scale_index_rebuild 去重/状态机,零前端改动。pydantic-settings v2 下
    # Field(validation_alias=...) 对新字段静默失效,必须用 validation_alias。
    scale_index_auto_enabled: bool = Field(True, validation_alias="SCALE_INDEX_AUTO_ENABLED")
    # "idle"=默认低峰窗口重建(避免高峰抢核);"now"=立即后台重建。Literal 约束:
    # 非法取值(如拼错的 env)在 Settings() 构造期就 ValidationError 快速失败,不静默
    # 落入 trigger_scale_index_rebuild 的 "now" 分支(见 fix review #4 finding 1)。
    scale_index_auto_when: Literal["idle", "now"] = Field("idle", validation_alias="SCALE_INDEX_AUTO_WHEN")
    # KG 视图 viz 索引:notebook 有效对象数(status!='deprecated')≤ 此阈值时,首次打开 KG 视图
    # 仍同步懒建(现有行为,小库瞬时);> 阈值则后台构建 + GET 立即返回 viz_building 占位,避免
    # 分钟级全图折叠拖垮请求线程(真机 49 万对象库卡死的根因)。pydantic-settings v2 下
    # Field(validation_alias=...) 对新字段静默失效,必须用 validation_alias。
    viz_sync_build_max_objects: int = Field(20000, validation_alias="VIZ_SYNC_BUILD_MAX_OBJECTS")
    # 大库(> viz_sync_build_max_objects)unified_graph 请求缺省 limit 时的服务端兜底上限。
    # 折叠视图本就是 object 级有界核心图,防止「不传 limit」的旧前端/裸调用绕过大库守卫
    # 落到 _unified_graph_full(全量拉取+多 GB 缓存,49 万对象库会打满 64GB 内存)。
    viz_default_limit: int = Field(300, validation_alias="VIZ_DEFAULT_LIMIT")
    # qwen3-rerank (DashScope text-rerank) 配置
    rerank_max_docs: int = Field(500, validation_alias="RERANK_MAX_DOCS")
    # rerank 接口风格:dashscope=DashScope 原生 text-rerank(嵌套 input/output);
    # openai=OpenAI 兼容(vLLM/Cohere 等)的 {base}/rerank(扁平 body、顶层 results)。
    relation_retrieval_enabled: bool = Field(False, validation_alias="RELATION_RETRIEVAL_ENABLED")
    relation_seed_top_n: int = Field(8, validation_alias="RELATION_SEED_TOP_N")
    # P0-1/2: 关系候选池上限(有关系向量时,先按向量 sim 取 top-N 候选再 hydrate 文本
    # 做关键词+语义融合打分;镜像 chunk_recall 的"猛召回池"语义,非最终截断
    # 的 relation_seed_top_n/_MIX_REL_SEEDS——池要显著大于两者才不伤召回)。
    relation_recall: int = Field(200, validation_alias="RELATION_RECALL")
    # HippoRAG 式 PPR 跨文档检索(graph 模式;默认开)
    graph_ppr_enabled: bool = Field(True, validation_alias="GRAPH_PPR_ENABLED")
    # P0-C:seed pass PPR 与 plan LLM 并行(纯调度,结果逐位等价);False=原串行
    reasoning_ppr_prefetch: bool = Field(True, validation_alias="REASONING_PPR_PREFETCH")
    ppr_damping: float = Field(0.5, validation_alias="PPR_DAMPING")               # rx.pagerank alpha
    ppr_tol: float = Field(1e-6, validation_alias="PPR_TOL")          # PPR 幂迭代收敛阈(L1);float32 下 <1e-6 会低于噪声地板被 clamp
    ppr_float32: bool = Field(True, validation_alias="PPR_FLOAT32")   # scale PPR 的 combined 转移阵/迭代用 float32(带宽减半≈2x;top-k 实践一致,分数长尾有 1e-6 级波动)
    ppr_passage_node_weight: float = Field(0.05, validation_alias="PPR_PASSAGE_NODE_WEIGHT")
    ppr_top_chunks: int = Field(20, validation_alias="PPR_TOP_CHUNKS")            # 最终喂答案的 chunk 数
    ppr_kg_seed_top_n: int = Field(20, validation_alias="PPR_KG_SEED_TOP_N")      # reset 向量里的 KG 种子数
    ppr_chunk_seed_top_n: int = Field(30, validation_alias="PPR_CHUNK_SEED_TOP_N")  # reset 向量里的 chunk 种子数
    ppr_fact_rerank_enabled: bool = Field(False, validation_alias="PPR_FACT_RERANK_ENABLED")  # LLM 过滤候选种子(每查一次 LLM)
    ppr_variant_edge_weight: float = Field(0.5, validation_alias="PPR_VARIANT_EDGE_WEIGHT")
    ppr_emb_synonym_enabled: bool = Field(True, validation_alias="PPR_EMB_SYNONYM_ENABLED")
    ppr_emb_synonym_threshold: float = Field(0.83, validation_alias="PPR_EMB_SYNONYM_THRESHOLD")
    ppr_emb_synonym_topk: int = Field(20, validation_alias="PPR_EMB_SYNONYM_TOPK")
    ppr_emb_synonym_max_entities: int = Field(50000, validation_alias="PPR_EMB_SYNONYM_MAX_ENTITIES")  # cost guard
    ppr_community_context_top_n: int = Field(3, validation_alias="PPR_COMMUNITY_CONTEXT_TOP_N")
    kg_canonical_fold_enabled: bool = Field(False, validation_alias="KG_CANONICAL_FOLD_ENABLED")
    kg_about_downweight_enabled: bool = Field(False, validation_alias="KG_ABOUT_DOWNWEIGHT_ENABLED")
    # knowhow 格子级 KO 进入 reasoning/graph 的 KG-node 检索路径(object_type=列名)。
    # 默认关闭:开启 / 最终调参须等真机 recall+latency 评测(效率优先约束)。关闭时
    # _retrieve_scored 字节不变(不加类型、不加查询、不注入);开启后仅当 notebook 真有
    # knowhow 列名类型时才放开类型 + 跑一次针对隐藏 knowhow 源的小规模 chunk 向量旁挂。
    knowhow_kg_node_retrieval_enabled: bool = Field(
        False, validation_alias="KNOWHOW_KG_NODE_RETRIEVAL_ENABLED")
    # 孤立(度为0)KG 节点的检索排序降权乘子。仅压 score 不动 relevance([0,1]/tau 守恒)。
    # 1.0=不降权(no-op); 0.5=孤立节点 score 减半;与 _EDGE_TYPE_RANK_WEIGHT 同模式。
    kg_isolated_rank_penalty: float = Field(0.5, validation_alias="KG_ISOLATED_RANK_PENALTY")
    # hnsw 建索引质量参数(recall/build-time 折衷)。三处共用同一值:build_scale_index
    # 里唯一的一次 KG-embedding hnsw 构建(emb_synonym KNN 复用点 + 持久化 ann.bin)、
    # chunk ANN(save_scale_index 里的 chunk_ann.bin)、emb_synonym_edges 未传
    # prebuilt_index 时的内部自建 fallback(_federated_rx_graph 联邦路径等)。真机 49 万对象库
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
    # 进程级 scale/viz 索引缓存(_scale_idx_cache/_viz_idx_cache;每条目 = numpy
    # 数组 + memoized hnsw handle,单条目可达数十 MB~GB)的 LRU 容量上限——此前是
    # 无界 plain dict,每个访问过的 notebook 常驻一条目直到进程重启。8 = 典型部署
    # 同时活跃的库数量级(远小于 VectorCache 的 32,因单条目体量大得多)。
    scale_idx_cache_max: int = Field(8, validation_alias="SCALE_IDX_CACHE_MAX")
    # review_queue 的边介数中心性(rustworkx Brandes O(V·E))超此节点数时,不再对
    # 全图跑,改对「度数 top-K 诱导子图」算(见 sqlite_repository._edge_centrality_map)。
    edge_centrality_max_nodes: int = Field(20000, validation_alias="EDGE_CENTRALITY_MAX_NODES")
    answer_context_budget_chars: int = Field(6000, validation_alias="ANSWER_CONTEXT_BUDGET_CHARS")
    answer_context_min_items: int = Field(3, validation_alias="ANSWER_CONTEXT_MIN_ITEMS")
    # grounded 三档阈值（作用于融合相关度 .relevance ∈[0,1]）。
    # 注意：现有 grounded 测试要求 tau_high ≤ 0.4（纯关键词命中融合分=0.4）。
    evidence_tau_low: float = Field(0.18, validation_alias="EVIDENCE_TAU_LOW")
    evidence_tau_high: float = Field(0.35, validation_alias="EVIDENCE_TAU_HIGH")
    # 流程类问题 top-N 至少保底召回的 procedure 条数。
    proc_min: int = Field(2, validation_alias="PROC_MIN")
    # 推理模式(mode=reasoning)护栏: Reflect 循环总步数 circuit breaker。
    reasoning_max_steps: int = Field(50, validation_alias="REASONING_MAX_STEPS")
    # 推理模式 Plan 输出子查询数上限。
    reasoning_max_subqueries: int = Field(5, validation_alias="REASONING_MAX_SUBQUERIES")
    # 复合问题最终排序: 开启后按子查询配额 round-robin 选 top-N(避免整串全局排序让
    # 信息量大的一方通吃); 关闭则回退全局重排。单子查询时自动等价全局。
    reasoning_quota_enabled: bool = Field(True, validation_alias="REASONING_QUOTA_ENABLED")
    # 自适应证据预算(top_n=None 时):最终采用的 KG 候选数 = 每方面(子查询,含
    # expand_community 兄弟)× per_query 席位,floor=retrieval_top_n(简单题与旧 12
    # 逐字一致),cap 封顶(对比题 3+8 兄弟=11 方面 → 33,不再被总数 12 摊薄)。
    reasoning_top_n_per_query: int = Field(3, validation_alias="REASONING_TOP_N_PER_QUERY")
    reasoning_top_n_cap: int = Field(36, validation_alias="REASONING_TOP_N_CAP")
    reasoning_quota_reuse_enabled: bool = Field(True, validation_alias="REASONING_QUOTA_REUSE")  # P1-B:quota 收尾复用初检索留存的全量打分(一次 run 内图只读⇒与重跑逐位等价);False=原收尾重跑
    # 退化循环熔断: 连续 N 轮无有效进展(含反复请求已访问节点)即强制收尾作答,
    # 不空转到 reasoning_max_steps; search_elements 累计次数上限(防"每次有新增但永不满足")。
    reasoning_stale_limit: int = Field(3, validation_alias="REASONING_STALE_LIMIT")
    reasoning_max_element_searches: int = Field(5, validation_alias="REASONING_MAX_ELEMENT_SEARCHES")
    # 推理模式(交互式,用户在线等)专用的 per-call LLM 超时/重试,与批量抽取
    # 的全局 openai_compat_* 解耦：单步更短超时 + 更少重试，避免卡死时久等。
    reasoning_timeout_seconds: int = Field(90, validation_alias="REASONING_TIMEOUT_SECONDS")
    reasoning_max_retries: int = Field(1, validation_alias="REASONING_MAX_RETRIES")
    # Global 问答:map-reduce 时纳入的社区报告上限(按 size 取前 N)。
    global_max_communities: int = Field(20, validation_alias="GLOBAL_MAX_COMMUNITIES")
    # 问题感知证据精炼: 默认开启(隔离 eval: 正确性 1.57→1.73 且伪引用全层→0%;
    # 代价每 ask 多 1 次 LLM)。答题前对已装配证据按问题抽"相关要点"前置,聚焦答题。设 false 关。
    kg_query_refine_enabled: bool = Field(True, validation_alias="KG_QUERY_REFINE_ENABLED")
    query_refine_max_chars: int = Field(4000, validation_alias="QUERY_REFINE_MAX_CHARS")
    # chunk-native 检索分块: chunk 目标字数 / 相邻重叠(P1 overlap 默认 0)。
    chunk_target_chars: int = Field(600, validation_alias="CHUNK_TARGET_CHARS")
    chunk_overlap_chars: int = Field(0, validation_alias="CHUNK_OVERLAP_CHARS")
    # chunk-native 检索: 大召回候选数 / MMR 精选数 / MMR λ / 答案上下文预算(长上下文综合)。
    chunk_recall: int = Field(200, validation_alias="CHUNK_RECALL")   # mix 候选池/MMR 候选;LightRAG 风格猛召回
    chunk_mmr_k: int = Field(16, validation_alias="CHUNK_MMR_K")
    chunk_mmr_lambda: float = Field(0.5, validation_alias="CHUNK_MMR_LAMBDA")
    chunk_answer_budget_chars: int = Field(30000, validation_alias="CHUNK_ANSWER_BUDGET_CHARS")
    # 大库 chunk 暴力回退守卫:ANN 不可用(未建 scale 索引/embed 失败/ANN fail-open)
    # 时,chunk 数超过该阈值的库不再「全表拉文本+全量纯 Python 分词」(生产 55 万 KG
    # 级库曾因 .env 丢失走到这里,单问磨半小时),改走 FTS 词法候选+有界打分并发
    # chunk_bruteforce_skipped 事件;真解仍是建 scale 索引(chunk ANN)。
    # 0 = 关守卫(任何规模都保留全量暴力)。
    chunk_bruteforce_max_chunks: int = Field(
        20000, validation_alias="CHUNK_BRUTEFORCE_MAX_CHUNKS")
    # P3 查询改写/扩展: 多子查询上限(0=禁用多子查询); 开关(false→单查询 MMR 原路径)。
    chunk_max_subqueries: int = Field(4, validation_alias="CHUNK_MAX_SUBQUERIES")
    query_rewrite_enabled: bool = Field(True, validation_alias="QUERY_REWRITE_ENABLED")
    # P4: 摄取默认不抽 KG(只建 chunk);要严格推理时经"建图"端点/离线 CLI 按需构建。
    # True 恢复旧"每次摄取整库自动抽"行为(迁移/测试逃生口)。base 库复用 tier='base'。
    kg_auto_extract: bool = Field(False, validation_alias="KG_AUTO_EXTRACT")
    # 冲突消解(conflict resolution): 默认关闭(opt-in)。开启后建图末尾跑一遍 KG
    # 矛盾三元组检测+LLM裁决+写回(keep/discard/modify)。设计见
    # docs/superpowers/specs/2026-06-17-kg-conflict-resolution-design.md。
    kg_conflict_resolution_enabled: bool = Field(False, validation_alias="KG_CONFLICT_RESOLUTION_ENABLED")
    # 自动应用阈值: 裁决 confidence ≥ 此值才自动写回; 低于则入评审队列等人工确认。
    # 设 1.0 = 纯评审模式(只检测不自动改图)。
    kg_conflict_auto_apply_threshold: float = Field(0.95, validation_alias="KG_CONFLICT_AUTO_APPLY_THRESHOLD")
    # 语义候选阈值: 端点对象 embedding 余弦 ≥ 此值才作为 semantic 冲突候选(高=更稀疏)。
    kg_conflict_sim_threshold: float = Field(0.8, validation_alias="KG_CONFLICT_SIM_THRESHOLD")
    # chunk×graph mix: 叠加 KG 子图 block 和源 chunk 进候选池(默认开)。关闭后退化为纯 chunk 检索。
    chunk_kg_overlay_enabled: bool = Field(True, validation_alias="CHUNK_KG_OVERLAY_ENABLED")
    # chunk×graph mix token 预算(照 LightRAG 6000/8000/30000)。
    max_entity_tokens: int = Field(6000, validation_alias="MAX_ENTITY_TOKENS")
    max_relation_tokens: int = Field(8000, validation_alias="MAX_RELATION_TOKENS")
    max_total_tokens: int = Field(30000, validation_alias="MAX_TOTAL_TOKENS")

    # LLM interaction logging. Records every chat/embedding call (request,
    # response, latency, token usage, errors) to a JSONL file plus a brief
    # console line. Defaults on; no-op when the LLM is not configured.
    llm_log_enabled: bool = Field(True, validation_alias="LLM_LOG_ENABLED")
    llm_log_path: str = Field(".local/logs/llm.jsonl", validation_alias="LLM_LOG_PATH")
    llm_log_max_chars: int = Field(4000, validation_alias="LLM_LOG_MAX_CHARS")

    # General event logging: HTTP requests, async pipeline stages, status
    # transitions. Written to <event_log_dir>/<channel>.jsonl plus the console.
    event_log_enabled: bool = Field(True, validation_alias="EVENT_LOG_ENABLED")
    event_log_dir: str = Field(".local/logs", validation_alias="EVENT_LOG_DIR")
    # Requests slower than this (ms) are flagged SLOW so "stuck" calls stand out.
    slow_request_ms: int = Field(3000, validation_alias="SLOW_REQUEST_MS")

    # Read-only debug log viewer endpoints (/api/debug/logs/...). Local dev tool;
    # opt in with DEBUG_LOGS_ENABLED=true because full records may contain
    # prompt/response text from private source material.
    debug_logs_enabled: bool = Field(False, validation_alias="DEBUG_LOGS_ENABLED")

    # PDF parsing via MinerU (decoupled from GPU). Modes:
    #   "off"  -> use the built-in pypdf text fallback (default; no GPU, offline)
    #   "http" -> call a remote MinerU service (mineru-api) at mineru_api_url
    #   "cli"  -> run MinerU's Python API in an isolated subprocess
    mineru_mode: str = Field("off", validation_alias="MINERU_MODE")
    mineru_api_url: str = Field("", validation_alias="MINERU_API_URL")
    mineru_backend: str = Field("pipeline", validation_alias="MINERU_BACKEND")
    # Only used by the VLM *client* backends (vlm-http-client / vlm-sglang-client):
    # the URL of a standalone VLM inference server (vllm/sglang serving MinerU's VLM).
    mineru_vlm_server_url: str = Field("", validation_alias="MINERU_VLM_SERVER_URL")
    mineru_parse_method: str = Field("auto", validation_alias="MINERU_PARSE_METHOD")
    mineru_lang: str = Field("", validation_alias="MINERU_LANG")
    mineru_model_source: str = Field("huggingface", validation_alias="MINERU_MODEL_SOURCE")
    mineru_timeout_seconds: int = Field(600, validation_alias="MINERU_TIMEOUT_SECONDS")
    mineru_formula_enable: bool = Field(True, validation_alias="MINERU_FORMULA_ENABLE")
    mineru_table_enable: bool = Field(True, validation_alias="MINERU_TABLE_ENABLE")

    # MinerU 内嵌图片保留（pdf/docx/pptx 抽图落盘为可见资产）。默认开；护栏
    # 让内网 mineru-api 回传载荷与磁盘占用有界，可整体关停回到「只文字」。
    mineru_return_images: bool = Field(True, validation_alias="MINERU_RETURN_IMAGES")
    mineru_max_image_bytes: int = Field(5 * 1024 * 1024, validation_alias="MINERU_MAX_IMAGE_BYTES")
    mineru_max_images_per_source: int = Field(200, validation_alias="MINERU_MAX_IMAGES_PER_SOURCE")

    # MinerU.net cloud (v4) — parse a public PDF URL or an uploaded file via the hosted service.
    # 独立于上面的 MINERU_MODE(off/http/cli)；URL 来源与上传文件在本地 MinerU 未配置时都用它作云端兜底。
    mineru_api_token: str = Field("", validation_alias="MINERU_API_TOKEN")
    mineru_api_base: str = Field("https://mineru.net", validation_alias="MINERU_API_BASE")
    mineru_cloud_model_version: str = Field("vlm", validation_alias="MINERU_CLOUD_MODEL_VERSION")
    mineru_cloud_language: str = Field("ch", validation_alias="MINERU_CLOUD_LANGUAGE")
    mineru_cloud_formula_enable: bool = Field(True, validation_alias="MINERU_CLOUD_FORMULA_ENABLE")
    mineru_cloud_table_enable: bool = Field(True, validation_alias="MINERU_CLOUD_TABLE_ENABLE")
    mineru_cloud_timeout_seconds: int = Field(600, validation_alias="MINERU_CLOUD_TIMEOUT_SECONDS")
    mineru_cloud_poll_interval_seconds: int = Field(5, validation_alias="MINERU_CLOUD_POLL_INTERVAL_SECONDS")

    database_url: str = Field(
        "sqlite:///.local/silicon_notebook.db",
        validation_alias="DATABASE_URL",
    )
    storage_dir: str = Field(".local/storage", validation_alias="SILICON_NOTEBOOK_STORAGE_DIR")
    # Notebook 分享拷贝阈值:超过任一阈值的库仅可只读共享(Phase 2),不可深拷贝。
    notebook_copy_max_bytes: int = Field(50 * 1024 * 1024, validation_alias="NOTEBOOK_COPY_MAX_BYTES")
    notebook_copy_max_rows: int = Field(5000, validation_alias="NOTEBOOK_COPY_MAX_ROWS")
    notebook_copy_stale_seconds: int = Field(3600, validation_alias="NOTEBOOK_COPY_STALE_SECONDS")
    # NoDecode 让 pydantic-settings 不把环境变量当 JSON 解析（否则逗号串会崩），
    # 改由下面的 split_cors_origins(mode="before") 处理逗号分隔；validation_alias
    # 才能真正读到 SILICON_NOTEBOOK_CORS_ORIGINS（v2 下 Field(validation_alias=...) 会被忽略）。
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

    @model_validator(mode="after")
    def _validate_runtime_dim(self) -> "Settings":
        """EMBED_RUNTIME_DIM 越界即硬错(呼应 PR#175 预检哲学:配置错误出声,
        不静默降级)。收口在 Settings 构造处 → 后端 create_app、离线 CLI
        (batch_ingest)、eval 三类入口一处全覆盖。"""
        if self.embed_runtime_dim < 0:
            raise ValueError(
                f"EMBED_RUNTIME_DIM={self.embed_runtime_dim} 非法(须 ≥0;0=关闭截断)")
        if self.embed_runtime_dim > 0 and self.embed_runtime_dim > self.embed_dim:
            raise ValueError(
                f"EMBED_RUNTIME_DIM={self.embed_runtime_dim} > EMBED_DIM={self.embed_dim} —— "
                f"运行时维不能超过存储/原生维。若想提高运行时维,先确认存量向量原生维;"
                f"绝对不要反过来改小 EMBED_DIM(存量按 EMBED_DIM 过滤,改小=全库向量被当异维残留丢光)")
        return self

    @model_validator(mode="after")
    def _validate_production_credentials(self) -> "Settings":
        if self.environment.strip().lower() in {"prod", "production"}:
            if not self.admin_password.strip() or self.admin_password == "admin":
                raise ValueError(
                    "production requires a non-default SILICON_NOTEBOOK_ADMIN_PASSWORD"
                )
        return self

    @model_validator(mode="after")
    def _resolve_core_bound_defaults(self) -> "Settings":
        """核绑定旋钮的按核数默认。仅在未显式设置(<=0 哨兵)时生效;显式值原样保留。
        封顶 32:HNSW 图构建争用 + rep 矩阵内存带宽,超 ~32 线程收益递减。"""
        if self.kg_cluster_ann_threads <= 0:
            self.kg_cluster_ann_threads = min(os.cpu_count() or 1, 32)
        return self

    @model_validator(mode="after")
    def _anchor_relative_paths_to_repo_root(self) -> "Settings":
        """storage_dir 与 database_url（仅 sqlite:/// 且路径部分相对时）统一
        锚定到仓库根，与默认值/env 覆盖是否相对无关，与进程 CWD 无关。

        - 绝对路径（默认或 env 覆盖）原样保留，不重锚。
        - 当前 repository 只实现 SQLite；其它 URL 直接报错，避免部署时
          看似连上 PostgreSQL、实际静默写进本地 SQLite 的数据分叉。
        - sqlite:/// 的拼写规则：`sqlite:///relative/path`（三斜杠+相对路径）
          与 `sqlite:////abs/path`（三斜杠分隔符 + 绝对路径本身以 / 开头，
          视觉上四个斜杠）；判断标准是把 `sqlite:///` 前缀去掉后剩余部分是否
          是绝对路径，而非数斜杠。
        """
        if self.storage_dir and not Path(self.storage_dir).is_absolute():
            self.storage_dir = str(_ROOT_DIR / self.storage_dir)

        if self.model_services_config and not Path(self.model_services_config).is_absolute():
            self.model_services_config = str(_ROOT_DIR / self.model_services_config)

        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError(
                "DATABASE_URL currently supports only sqlite:/// URLs; "
                "configure SQLite until a PostgreSQL repository is implemented"
            )
        raw_path = self.database_url[len(prefix):]
        if raw_path and not Path(raw_path).is_absolute():
            self.database_url = f"{prefix}{_ROOT_DIR / raw_path}"
        return self

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
        raise ValueError("DATABASE_URL is not a supported sqlite:/// URL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
