import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.database_url import (
    database_identity,
    normalize_database_url,
    redact_database_url,
    sanitize_database_url_for_error,
)

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

# Multipart resource guard, intentionally fixed rather than a second deployment
# knob: one request may carry at most this many source files. The browser reads
# the value from /api/system/config so its staging behavior stays in lockstep.
SOURCE_UPLOAD_MAX_FILES_PER_BATCH = 20

# Canonical defaults also used by narrow compatibility adapters that are
# intentionally duck-typed in offline tools. Normal application composition
# always supplies ``Settings``; keeping these names here prevents those
# adapters from reintroducing anonymous result-changing literals.
DEFAULT_REASONING_PER_QUERY_LIMIT = 8
DEFAULT_CHUNK_KG_NODE_SEED_TOP_N = 20
DEFAULT_CHUNK_KG_RELATION_SEED_TOP_N = 10
DEFAULT_CHUNK_KG_MAX_DEPTH = 1
DEFAULT_CHUNK_KG_FAN_OUT = 8
DEFAULT_REPORT_RETRIEVAL_FANOUT = 8
DEFAULT_REPORT_PROBE_CHANNEL_CONCURRENCY = 2


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


class _InvalidDatabaseUrl(str):
    """Safe replacement input that preserves a URL-validation reason."""

    def __new__(cls, sanitized: str, reason: str):
        value = super().__new__(cls, sanitized)
        value.reason = reason
        return value


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
    # 每个笔记本「用户可见文档」数量的全局默认上限。管理员可在 app_settings 里覆盖
    # 全局默认、或给某用户单独设覆盖值(user_profiles.upload_document_limit);两者都
    # 缺省时回退到此值。pydantic-settings v2 下 Field(env=) 失效,必须用 validation_alias。
    user_upload_document_limit: int = Field(
        20, validation_alias="USER_UPLOAD_DOCUMENT_LIMIT"
    )
    # 单个用户上传来源文件的 MB 上限。这个 Settings 值是唯一真源：上传路由用
    # source_upload_max_bytes 作权威 413 校验，而已认证的浏览器取得同一解析后的
    # 字节数，在选择文件时提前拒绝。部署变量用 MB，避免要求运维手算字节。
    source_upload_max_mb: int = Field(
        50,
        gt=0,
        le=1024,
        validation_alias="SOURCE_UPLOAD_MAX_MB",
    )
    # 每用户模型配置策略。"fallback"(第一阶段)=用户没配则回退系统 env 默认；
    # "required"(第二阶段)=用户没配则该服务不可用(解析为 none，经 model_error 通道提示)。
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
    # Post-terminal extension work is cooperative and synchronous: a callback
    # already inside core I/O may finish, but the host starts no later
    # contribution after this point-owned wall-clock budget is exhausted.
    ask_post_completion_extension_timeout_seconds: float = Field(
        30.0,
        gt=0,
        le=300,
        validation_alias="ASK_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS",
    )
    answer_audit_max_findings: int = Field(
        32,
        ge=1,
        le=256,
        validation_alias="ANSWER_AUDIT_MAX_FINDINGS",
    )
    report_post_completion_extension_timeout_seconds: float = Field(
        30.0,
        gt=0,
        le=300,
        validation_alias="REPORT_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS",
    )
    report_audit_max_findings: int = Field(
        32,
        ge=1,
        le=256,
        validation_alias="REPORT_AUDIT_MAX_FINDINGS",
    )
    # Parsed-element enrichers run once per contributor and source between
    # parser materialization and the core-owned element transaction. The
    # point-wide cooperative budget starts no later contributor after expiry.
    source_element_enricher_timeout_seconds: float = Field(
        10.0,
        gt=0,
        le=300,
        validation_alias="SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS",
    )
    source_element_enricher_max_proposals: int = Field(
        2048,
        ge=1,
        le=25000,
        validation_alias="SOURCE_ELEMENT_ENRICHER_MAX_PROPOSALS",
    )
    source_element_enricher_max_metadata_bytes: int = Field(
        1048576,
        ge=1024,
        le=16777216,
        validation_alias="SOURCE_ELEMENT_ENRICHER_MAX_METADATA_BYTES",
    )
    source_element_enricher_max_caption_chars: int = Field(
        4096,
        ge=1,
        le=32768,
        validation_alias="SOURCE_ELEMENT_ENRICHER_MAX_CAPTION_CHARS",
    )
    # Candidate projectors run after the legacy KG candidate/relink/partial
    # gates and before the one core-owned source-generation transaction.
    knowledge_candidate_projector_timeout_seconds: float = Field(
        10.0,
        gt=0,
        le=300,
        validation_alias="KNOWLEDGE_CANDIDATE_PROJECTOR_TIMEOUT_SECONDS",
    )
    knowledge_candidate_projector_max_objects: int = Field(
        512,
        ge=1,
        le=25000,
        validation_alias="KNOWLEDGE_CANDIDATE_PROJECTOR_MAX_OBJECTS",
    )
    knowledge_candidate_projector_max_relations: int = Field(
        1024,
        ge=1,
        le=50000,
        validation_alias="KNOWLEDGE_CANDIDATE_PROJECTOR_MAX_RELATIONS",
    )
    knowledge_candidate_projector_max_candidate_bytes: int = Field(
        1048576,
        ge=1024,
        le=16777216,
        validation_alias="KNOWLEDGE_CANDIDATE_PROJECTOR_MAX_CANDIDATE_BYTES",
    )

    # --- 深度报告(report_engine) ---
    report_max_sections: int = Field(
        6, ge=1, validation_alias="REPORT_MAX_SECTIONS"
    )
    # One section may execute several independently approved retrieval
    # directions.  This is both a prompt contract and an API/UI validation
    # rail; user-authored directions over the cap are rejected rather than
    # silently discarded by a literal ``[:4]``.
    report_max_subqueries_per_section: int = Field(
        4, ge=2, validation_alias="REPORT_MAX_SUBQUERIES_PER_SECTION"
    )
    # Corpus-blind planning probes and the small corpus-map scout are bounded
    # separately from final drafting evidence.  They affect planning quality
    # and database work, so keep them deployment-owned instead of burying
    # result-changing slices in report_engine.py.
    report_probe_element_limit: int = Field(
        8, ge=1, validation_alias="REPORT_PROBE_ELEMENT_LIMIT"
    )
    report_scout_kg_limit: int = Field(
        12, ge=1, validation_alias="REPORT_SCOUT_KG_LIMIT"
    )
    report_scout_chunk_limit: int = Field(
        8, ge=1, validation_alias="REPORT_SCOUT_CHUNK_LIMIT"
    )
    report_scout_memory_limit: int = Field(
        8, ge=1, validation_alias="REPORT_SCOUT_MEMORY_LIMIT"
    )
    # (report_section_top_n 已移除:逐节深挖与 ask 统一走 effective_top_n 自适应预算,
    #  由 retrieval_top_n / REASONING_TOP_N_* 统一治理;不再有报告专属的 KG 预算旋钮。)
    report_section_chunk_budget: int = Field(
        20000, validation_alias="REPORT_SECTION_CHUNK_BUDGET")
    # Business-level retrieval/drafting fan-out for one report.  Model calls
    # remain globally governed by the bound service scheduler; this lower gate
    # protects the database pool from many reports multiplying that capacity.
    report_section_concurrency: int = Field(
        5, ge=1, validation_alias="REPORT_SECTION_CONCURRENCY")
    # One report-wide leaf-I/O budget shared by every section worker, its
    # initial subqueries and PPR prefetch.  Orchestrators never hold this slot.
    report_retrieval_fanout: int = Field(
        DEFAULT_REPORT_RETRIEVAL_FANOUT,
        ge=1,
        validation_alias="REPORT_RETRIEVAL_FANOUT",
    )
    # A probe query has exactly two independent retrieval channels (KG and raw
    # elements).  Deployments may serialize them, but cannot create more work
    # than those two leaves.
    report_probe_channel_concurrency: int = Field(
        DEFAULT_REPORT_PROBE_CHANNEL_CONCURRENCY,
        ge=1,
        le=2,
        validation_alias="REPORT_PROBE_CHANNEL_CONCURRENCY",
    )
    # Whole-report jobs admitted per backend process.  Keep this separate from
    # model-service concurrency because each section performs database work
    # before and between model calls.
    report_generation_concurrency: int = Field(
        1, ge=1, validation_alias="REPORT_GENERATION_CONCURRENCY")
    # 后台**重活** job(KG 分析/重建、补上关联、统一图重建、冲突检测、合并预审)
    # 在一个后端进程内的并发上限。每一类各自已有单飞或去重闸,但它们**互相之间**
    # 此前没有闸:同一时刻多个笔记本各排一个重活,数据库与模型扇出会成倍叠加。闸在
    # worker 线程内获取,submit 仍立即返回,请求线程不等待。用户交互路径(ask-*)与
    # 已有整篇准入闸的 report-* 不进此闸。
    background_maintenance_concurrency: int = Field(
        4, ge=1, validation_alias="BACKGROUND_MAINTENANCE_CONCURRENCY")
    # 后台**轻活** job(论文元数据补抽、命令目录识别、knowhow 投影与孤儿资产清扫)
    # 的独立并发上限。刻意与重活分池:轻活是秒级的单表/单来源工作,合用一个池时几个
    # 小时级重建就能把「用户点一下就该出结果」的格子投影饿死到几十分钟后。
    background_light_job_concurrency: int = Field(
        4, ge=1, validation_alias="BACKGROUND_LIGHT_JOB_CONCURRENCY")
    report_section_max_tokens: int = Field(
        65536, ge=1, validation_alias="REPORT_SECTION_MAX_TOKENS")
    # Detailed tiers emit one report-wide, evidence-keyed JSON blueprint before
    # section drafting.  Both this stage and the final editor need independent
    # high ceilings and must not silently inherit OPENAI_COMPAT_MAX_TOKENS.
    report_synthesis_max_tokens: int = Field(
        102400, ge=1, validation_alias="REPORT_SYNTHESIS_MAX_TOKENS")
    report_summary_max_tokens: int = Field(
        102400, ge=1, validation_alias="REPORT_SUMMARY_MAX_TOKENS")
    # 【通识】层:允许报告引入库外参数知识(行内标注,仅报告管线读取)。
    report_allow_parametric: bool = Field(
        True, validation_alias="REPORT_ALLOW_PARAMETRIC")
    # Deterministic citation audit for numeric/complexity/absolute/ranking
    # assertions.  The audit is always disclosed; automatic evidence-level
    # downgrade stays opt-in until production distributions are calibrated.
    report_high_risk_unsupported_ratio: float = Field(
        0.25,
        ge=0.0,
        le=1.0,
        validation_alias="REPORT_HIGH_RISK_UNSUPPORTED_RATIO",
    )
    report_high_risk_downgrade_enabled: bool = Field(
        False,
        validation_alias="REPORT_HIGH_RISK_DOWNGRADE_ENABLED",
    )
    # Report sufficiency heuristics.  These defaults are the historical inline
    # policy; exposing them here centralizes later eval-driven calibration.
    report_sufficiency_min_relevant_items: int = Field(
        3, ge=1, validation_alias="REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS"
    )
    report_sufficiency_min_families: int = Field(
        2, ge=1, validation_alias="REPORT_SUFFICIENCY_MIN_FAMILIES"
    )
    report_sufficiency_complete_min_families: int = Field(
        3, ge=1, validation_alias="REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES"
    )
    report_sufficiency_max_top_family_share: float = Field(
        0.8,
        ge=0.0,
        le=1.0,
        validation_alias="REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE",
    )
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
    llm_cache_enabled: bool = Field(True, validation_alias="LLM_CACHE_ENABLED")
    llm_cache_path: str = Field(".local/llm_cache_v2.db", validation_alias="LLM_CACHE_PATH")
    # 容量上限(字节)与条目寿命(天)。TTL 必须长——reparse/reextract 那种几万源重跑
    # 可能几个月后才发生，届时命中率接近 100%，短 TTL 会毁掉最大的收益场景。
    llm_cache_size_limit: int = Field(2 * 2**30, validation_alias="LLM_CACHE_SIZE_LIMIT")
    llm_cache_ttl_days: int = Field(90, validation_alias="LLM_CACHE_TTL_DAYS")

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
    # Bounded post-extraction relation completion.  Default is a strict no-op;
    # shadow/write additionally require notebook allowlist/hash rollout gating.
    kg_relation_completion_mode: str = Field(
        "off", validation_alias="KG_RELATION_COMPLETION_MODE"
    )
    kg_relation_completion_notebook_allowlist: str = Field(
        "", validation_alias="KG_RELATION_COMPLETION_NOTEBOOK_ALLOWLIST"
    )
    kg_relation_completion_rollout_percent: int = Field(
        0, ge=0, le=100, validation_alias="KG_RELATION_COMPLETION_ROLLOUT_PERCENT"
    )
    kg_relation_completion_max_objects: int = Field(
        160, gt=0, validation_alias="KG_RELATION_COMPLETION_MAX_OBJECTS"
    )
    kg_relation_completion_max_pairs: int = Field(
        120, gt=0, validation_alias="KG_RELATION_COMPLETION_MAX_PAIRS"
    )
    kg_relation_completion_section_quota: int = Field(
        24, gt=0, validation_alias="KG_RELATION_COMPLETION_SECTION_QUOTA"
    )
    kg_relation_completion_batch_pairs: int = Field(
        24, gt=0, validation_alias="KG_RELATION_COMPLETION_BATCH_PAIRS"
    )
    kg_relation_completion_max_batches: int = Field(
        4, gt=0, validation_alias="KG_RELATION_COMPLETION_MAX_BATCHES"
    )
    kg_relation_completion_excerpt_chars: int = Field(
        800, gt=0, validation_alias="KG_RELATION_COMPLETION_EXCERPT_CHARS"
    )
    kg_relation_completion_max_pages_per_run: int = Field(
        4, gt=0, validation_alias="KG_RELATION_COMPLETION_MAX_PAGES_PER_RUN"
    )
    kg_relation_completion_neighbor_top_k: int = Field(
        8, gt=0, validation_alias="KG_RELATION_COMPLETION_NEIGHBOR_TOP_K"
    )
    kg_relation_completion_candidate_overfetch: int = Field(
        64, gt=0, validation_alias="KG_RELATION_COMPLETION_CANDIDATE_OVERFETCH"
    )
    kg_relation_completion_batch_chars: int = Field(
        48_000, ge=512, validation_alias="KG_RELATION_COMPLETION_BATCH_CHARS"
    )
    # embedding：每条截断长度、每条 API 批大小、落库分块大小。
    embed_truncate_chars: int = Field(2000, validation_alias="EMBED_TRUNCATE_CHARS")
    embed_batch_size: int = Field(10, validation_alias="EMBED_BATCH_SIZE")
    embed_commit_batches: int = Field(50, validation_alias="EMBED_COMMIT_BATCHES")
    embed_persist_chunk: int = Field(200, validation_alias="EMBED_PERSIST_CHUNK")
    # embedding 限流（429）退避重试：批量摄取易瞬时超 QPS，退避到窗口恢复而非丢批。
    embed_rate_limit_retries: int = Field(5, validation_alias="EMBED_RATE_LIMIT_RETRIES")
    embed_rate_limit_base_delay: float = Field(2.0, validation_alias="EMBED_RATE_LIMIT_BASE_DELAY")
    # SQLite 忙等待超时（毫秒），配合 WAL 支持后台向量化与抽取并发写。
    db_busy_timeout_ms: int = Field(
        30000, ge=0, validation_alias="DB_BUSY_TIMEOUT_MS"
    )
    # 写锁观测(wait/hold per 调用点):详见 write_lock_stats.py。默认开,警戒线 200ms,
    # 每 site 每刷新窗口最多报一条违规(flush_interval_s)。
    db_write_lock_stats_enabled: bool = Field(
        True, validation_alias="DB_WRITE_LOCK_STATS")
    db_write_lock_warn_ms: int = Field(
        200, validation_alias="DB_WRITE_LOCK_WARN_MS")
    db_write_lock_flush_seconds: int = Field(
        60, validation_alias="DB_WRITE_LOCK_FLUSH_SECONDS")
    # 复用连接下每条连接长期持有 page cache;总内存 = 连接数 × |cache_size|。
    # 负值=KB。默认 16MB(低于旧 64MB)以在 O(线程数) 条连接下控总内存;可按部署上调。
    sqlite_cache_size_kb: int = Field(-16384, validation_alias="SQLITE_CACHE_SIZE_KB")
    # 检索：top-N 知识对象。旧默认 12 是 6 月从 scored[:12] 魔数原样抬入、从未校准;
    # 提到 20 给简单/单方面题更多深度余量(对比题在此 floor 之上再按方面数自适应扩容)。
    retrieval_top_n: int = Field(20, validation_alias="RETRIEVAL_TOP_N")
    # Legacy/no-effort callers still need an explicit per-query take. Normal
    # browser Ask/report requests use the user-selected effort table instead.
    reasoning_per_query_limit: int = Field(
        DEFAULT_REASONING_PER_QUERY_LIMIT,
        ge=1,
        validation_alias="REASONING_PER_QUERY_LIMIT",
    )
    # Response projection and graph-walk budgets. These used to be unrelated
    # literal slices/defaults in ask_service.py, which made retrieval tuning
    # incomplete and hid the effective graph ceiling from deployments.
    ask_related_knowledge_limit: int = Field(
        12, ge=1, validation_alias="ASK_RELATED_KNOWLEDGE_LIMIT"
    )
    graph_seed_top_n: int = Field(
        5, ge=1, validation_alias="GRAPH_SEED_TOP_N"
    )
    graph_max_depth: int = Field(
        3, ge=1, validation_alias="GRAPH_MAX_DEPTH"
    )
    graph_max_fan_out: int = Field(
        8, ge=1, validation_alias="GRAPH_MAX_FAN_OUT"
    )
    # 检索排序: 默认用关键词+语义加权融合; 开启后改用 BM25 与语义的 RRF 融合排序。
    retrieval_rrf_enabled: bool = Field(False, validation_alias="RETRIEVAL_RRF_ENABLED")
    retrieval_rrf_k: int = Field(60, validation_alias="RETRIEVAL_RRF_K")
    chunk_ann_enabled: bool = Field(True, validation_alias="CHUNK_ANN_ENABLED")  # 有索引的库 chunk 检索走 ANN 核⊕delta(默认开:大库全量暴力不可扩展;小库无 scale 索引→_scale_index 返 None→自然回退暴力,零影响)。ANN 是语义候选,纯关键词命中缺口待 chunk 侧 FTS(词法∪语义)补;可用 CHUNK_ANN_ENABLED=false 关
    # Optional LLM-generated question index. ``shadow`` computes bounded
    # low-recall candidates and records counts but cannot change results;
    # only explicit ``on`` merges them. Default off keeps model cost opt-in.
    generated_question_index_mode: Literal["off", "shadow", "on"] = Field(
        "off", validation_alias="GENERATED_QUESTION_INDEX_MODE"
    )
    generated_question_questions_per_chunk: int = Field(
        3, ge=1, le=8, validation_alias="GENERATED_QUESTION_QUESTIONS_PER_CHUNK"
    )
    generated_question_trigger_hits: int = Field(
        5, ge=1, validation_alias="GENERATED_QUESTION_TRIGGER_HITS"
    )
    generated_question_recall: int = Field(
        40, ge=1, validation_alias="GENERATED_QUESTION_RECALL"
    )
    generated_question_max_scan_rows: int = Field(
        10_000, ge=1, validation_alias="GENERATED_QUESTION_MAX_SCAN_ROWS"
    )
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
    # 的 relation_seed_top_n/chunk_kg_relation_seed_top_n——池要显著大于
    # 两者才不伤召回)。
    relation_recall: int = Field(200, validation_alias="RELATION_RECALL")
    # HippoRAG 式 PPR 跨文档检索(graph 模式;默认开)
    graph_ppr_enabled: bool = Field(True, validation_alias="GRAPH_PPR_ENABLED")
    # 来源子图内的独立稀疏 PPR；Shadow 默认运行但绝不改变用户结果。
    source_subgraph_ppr_enabled: bool = Field(
        True, validation_alias="SOURCE_SUBGRAPH_PPR_ENABLED"
    )
    # 超大单来源的 source-addressable scale companion 与其 invisible shadow
    # consumer 分开回滚；默认构建与读取，但只服务内部激活层。
    source_partitioned_graph_artifacts_enabled: bool = Field(
        True, validation_alias="SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED"
    )
    source_partitioned_ppr_enabled: bool = Field(
        True, validation_alias="SOURCE_PARTITIONED_PPR_ENABLED"
    )
    # Selected-source graph enrichment is independently quality gated. Shadow
    # is the invisible safe default because it cannot alter returned evidence.
    # Active modes require a content-free attestation plus exact corpus/model
    # pins; rollout state never belongs in a public response.
    selected_source_graph_rollout_mode: Literal[
        "off", "shadow", "allowlist", "rollout", "on"
    ] = Field("shadow", validation_alias="SELECTED_SOURCE_GRAPH_ROLLOUT_MODE")
    selected_source_graph_attestation_path: str = Field(
        "", validation_alias="SELECTED_SOURCE_GRAPH_ATTESTATION_PATH"
    )
    selected_source_graph_expected_corpus_signature: str = Field(
        "", validation_alias="SELECTED_SOURCE_GRAPH_EXPECTED_CORPUS_SIGNATURE"
    )
    selected_source_graph_expected_model_json: str = Field(
        "", validation_alias="SELECTED_SOURCE_GRAPH_EXPECTED_MODEL_JSON"
    )
    selected_source_graph_notebook_allowlist: str = Field(
        "", validation_alias="SELECTED_SOURCE_GRAPH_NOTEBOOK_ALLOWLIST"
    )
    selected_source_graph_rollout_percent: float = Field(
        0.0, ge=0.0, le=100.0,
        validation_alias="SELECTED_SOURCE_GRAPH_ROLLOUT_PERCENT",
    )
    selected_source_graph_enrichment_tokens: int = Field(
        4000, gt=0, validation_alias="SELECTED_SOURCE_GRAPH_ENRICHMENT_TOKENS"
    )
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
    # 默认开启，让结构化格子既参与 chunk 召回，也可作为 KG 节点参与图检索；仍保留
    # env 回滚开关。仅当 notebook 真有 knowhow 投影类型时才放开类型并查询隐藏
    # knowhow 源的缓存向量旁挂，普通 notebook 不增加检索工作。
    knowhow_kg_node_retrieval_enabled: bool = Field(
        True, validation_alias="KNOWHOW_KG_NODE_RETRIEVAL_ENABLED")
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
    # 启动就绪前把所有已发布 scale 索引的 CSR/节点映射、在线会用到的 ANN handle
    # 与可安全复用的单索引 PPR core 全部载入进程缓存。跨 notebook combined graph
    # 不在这里全量物化：每种挂载组合复制千万节点图会把 readiness 变成 OOM 风险。
    # 默认开：大库把冷加载成本前置到 readiness gate，首位用户不再承担。若索引数
    # 超过 SCALE_IDX_CACHE_MAX 或任一所需工件无法加载，启动 fail-closed；紧急进入
    # UI 修复旧/坏索引时可临时设 false，正常部署应保持 true 并配足 RAM/cache cap。
    startup_preload_scale_indexes: bool = Field(
        True, validation_alias="STARTUP_PRELOAD_SCALE_INDEXES"
    )
    # 大库 ScaleIndex 常驻上限(内存整改 PR-4):8 条上限对小/中库合适,但一个千万级
    # 对象(或源密集、chunk/relation ANN 巨大)的基准库 ScaleIndex 的 ANN 矩阵可达数十
    # GB。多领域 base 并发 warm 时,8 个这种大库 = 数百 GB → OOM。故对「大库」另设一个
    # 更小的常驻上限 scale_idx_cache_max_large:大库条目只算进这个小上限,小库仍用
    # scale_idx_cache_max;超限按 LRU 淘汰最久未用的大库(只丢引用,GC 收 numpy/hnsw,下次
    # 访问冷加载=冷启动)。「大库」= 三类 ANN(kg n_ann + n_chunk_ann + n_relation_ann)
    # 估算常驻字节(rows×runtime_dim×4,ANN 矩阵是常驻内存大头)> scale_idx_large_bytes。
    # 只按 n_nodes 会漏掉 KG 节点少但 chunk/relation ANN 巨大的源密集库(codex PR#359 r1 P1)。
    scale_idx_cache_max_large: int = Field(2, validation_alias="SCALE_IDX_CACHE_MAX_LARGE")
    scale_idx_large_bytes: int = Field(8 * 1024 ** 3, validation_alias="SCALE_IDX_LARGE_BYTES")
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
    # Agent action/cross-library expansion rails.  Defaults preserve the
    # historical module constants exactly; the centralized policy reads these
    # once per run so a deployment cannot drift between branches.
    reasoning_max_ppr_retrieves: int = Field(
        3, ge=0, validation_alias="REASONING_MAX_PPR_RETRIEVES"
    )
    reasoning_max_exact_lookups: int = Field(
        3, ge=0, validation_alias="REASONING_MAX_EXACT_LOOKUPS"
    )
    reasoning_max_follow_chain_actions: int = Field(
        3, ge=0, validation_alias="REASONING_MAX_FOLLOW_CHAIN_ACTIONS"
    )
    # Agentic Memory P4 (T5): reflect 动作 consult_memory 的总开关与 per-run 次数
    # 上限。仅在 deep 及以上档且 ``RETRIEVAL_EXPERIENCE_INJECT_ENABLED`` 也开着时
    # 才提供这个动作(见 ``reasoning_retrieval.consult_memory_active`` 的单点判定
    # ——总闸并入注入闸,不是独立策略位:注入闸关着时经验库对全部署都读不到,
    # 只按这里的 kill switch 放行会让模型看到一个能调但永远拉不到经验的动作)。
    reasoning_consult_memory_enabled: bool = Field(
        True, validation_alias="REASONING_CONSULT_MEMORY_ENABLED"
    )
    reasoning_max_consult_memory: int = Field(
        2, ge=0, validation_alias="REASONING_MAX_CONSULT_MEMORY"
    )
    reasoning_community_peers_cap_factor: int = Field(
        2, ge=1, validation_alias="REASONING_COMMUNITY_PEERS_CAP_FACTOR"
    )
    reasoning_max_outline_updates: int = Field(
        6, ge=1, validation_alias="REASONING_MAX_OUTLINE_UPDATES"
    )
    # 单次 expand_graph 每个方向(出边/入边)展开的 1-hop 邻居上限。没有它,一个
    # 病态枢纽节点(百万边)会让 `neighbor_ids` 无 LIMIT 全取、再整批 hydrate
    # 其 knowledge_objects 行——请求内存与耗时随该节点度数线性膨胀。默认取在
    # 只有病态枢纽才会触发的量级;触发时不静默,轨迹与 reflect 回喂都要披露。
    #
    # 已登记不修的残余:`edge_type` 与 `review_status` 都**不在**排序索引
    # `(notebook_id, source/target_object_id, id)` 里,是 LIMIT 之前的残余过滤。
    # 因此指定了一个**稀有** edge_type 时,数据库仍会沿该节点的邻接扫到底才凑不满
    # 这一页——返回行数照样有界(内存/hydrate 不受影响),但扫描量退回该节点的度数。
    # 修它要给每个 edge_type 建索引,收益只在「枢纽 + 稀有边类型」这一个交集上。
    reasoning_neighbor_expand_limit: int = Field(
        1000, gt=0, validation_alias="REASONING_NEIGHBOR_EXPAND_LIMIT"
    )
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
    # 逐步推理的类型化集合枚举工具(enumerate_elements / enumerate_kg_objects)总开关。
    # 关掉即完全回到接入前:reflect 既不提供这两个动作、也不注入集合地图,零额外查询。
    # 每次调用的成本由档位表的 enum_* 预算(每 run 行数/额外翻页)封死,不是这里。
    reasoning_enum_tools_enabled: bool = Field(
        True, validation_alias="REASONING_ENUM_TOOLS_ENABLED")
    # 逐步推理的大纲便签(reflect 动作 update_outline)总开关。它**另外**受档位
    # 约束:只有用户把检索档位选到「穷尽」时才提供(设计文档 §3.1,用户拍板),
    # 因为按节合成的成本要由用户显式选择来承担。关掉即两处都回到接入前:prompt
    # 不写这个动作、schema 没有 outline 分支、allowed_actions 里也没有它。
    reasoning_outline_enabled: bool = Field(
        True, validation_alias="REASONING_OUTLINE_ENABLED")
    # 大纲便签的 KG 弱支撑边回喂(设计文档 §3.3)。它**叠在**上面那把闸之上:
    # 大纲总闸或档位闸关着时它自然也不生效。单独留一把,是为了在不关掉整个大纲
    # 特性的前提下回退这条回喂。关闭态零查询、prompt 逐字回到接入前(沿用
    # 「flag 关 = 执行处 skip、不改动作面」的惯例——这条回喂本来也不新增动作)。
    reasoning_outline_kg_gap_enabled: bool = Field(
        True, validation_alias="REASONING_OUTLINE_KG_GAP_ENABLED")
    # Agentic Memory P1:Agent 对每个笔记本的「已有理解」(共享底座 + 个人覆盖层)
    # 总开关。判据只有一处 —— ``reasoning_retrieval.profile_wiring_active``,注入、
    # 巡固触发、API 可见性与前端显隐四处必须共用它(镜像上面那把枚举闸的教训:
    # 各写一份的话,关掉之后总会剩下一处还在跑)。关掉即完全回到接入前:plan/
    # reflect 不注入理解块、不记 ``profile`` 轨迹步、不排巡固任务、API 报
    # ``enabled=false``、前端不显示入口,零额外查询。
    agent_profile_enabled: bool = Field(
        True, validation_alias="AGENT_PROFILE_ENABLED")
    # 两条巡固链路各自的触发阈值(确定性闸,零模型调用):底座按「来源增删/重解析」
    # 累计次数,覆盖层按该成员「已完成提问」次数(深度报告完成直接达阈)。``ge=1``
    # 而不是允许 0:0 会让每一次来源变更/每一次提问都排一次有界 LLM 调用,那不是
    # 「更灵敏」,是把一个后台整理变成随每次写入触发的同步成本。
    agent_profile_base_trigger: int = Field(
        5, ge=1, validation_alias="AGENT_PROFILE_BASE_TRIGGER")
    agent_profile_overlay_trigger: int = Field(
        10, ge=1, validation_alias="AGENT_PROFILE_OVERLAY_TRIGGER")
    # Agentic Memory P3(B-Profile,T6):每用户「检索/回答风格偏好」文档
    # (``user_profiles.search_profile_json``)总开关。判据只有一处——
    # ``reasoning_retrieval.search_profile_wiring_active``,T7 归纳 job 的
    # 触发闸、T8 的 Ask plan/answer 注入闸与本端点的可写性/可见性判据三处必须
    # 共用它(镜像上面 agent_profile_enabled 的教训)。关掉即完全回到接入前:
    # 不归纳、不注入、``PATCH /me/search-profile`` 一律 409——但读路径不看这个
    # 开关,``GET /me`` 仍照常返回该用户上次归纳/编辑留下的既存值(列本身没有
    # 被清空),不是伪造成 ``search_profile: null``。
    user_search_profile_enabled: bool = Field(
        True, validation_alias="USER_SEARCH_PROFILE_ENABLED")
    # T7 归纳 job 的触发阈值(确定性闸,零模型调用——v1 归纳规则本身就是零 LLM
    # 的多数语言统计):该用户累计完成的 Ask 数。``ge=1`` 而非允许 0,理由同
    # 两条巡固链路的触发阈值——0 会让每一次提问都排一次归纳,那不是「更灵敏」,
    # 是把一次后台整理变成随每次提问触发的同步成本。
    user_search_profile_trigger: int = Field(
        20, ge=1, validation_alias="USER_SEARCH_PROFILE_TRIGGER")
    # Agentic Memory P2:部署级**全局**的检索策略经验库。两把闸刻意**分开**,
    # 因为蒸馏与注入是两个独立的决定,而且它们的风险完全不同:蒸馏只是往一张
    # 没有任何用户可见面的表里攒行(默认 **开**,先把数据攒起来),注入才会改变
    # 每一次真实提问的规划提示(默认 **关**,等观测)。设计文档的风险条目明说
    # 「效果不好可以只留蒸馏观测、不注入」——那正是这两个默认值。
    # 关掉 ``RETRIEVAL_EXPERIENCE_ENABLED`` 即完全回到接入前:不读 ask 轨迹、
    # 不排蒸馏任务、不发模型调用、零额外查询。
    retrieval_experience_enabled: bool = Field(
        True, validation_alias="RETRIEVAL_EXPERIENCE_ENABLED")
    retrieval_experience_inject_enabled: bool = Field(
        False, validation_alias="RETRIEVAL_EXPERIENCE_INJECT_ENABLED")
    # 蒸馏的触发阈值(确定性闸,零模型调用):**全局**累计完成提问数。与两条巡固
    # 链路的阈值同款 ``ge=1``——0 会让每一次提问都排一次有界 LLM 调用,那不是
    # 「学得更快」,是把一个后台整理变成随每次问答触发的成本。
    # ⚠ 计数是**进程内**的,重启归零。这是刻意接受的:经验库是纯增量优化,不是
    # 正确性要求,重启后少蒸馏一轮的代价是零;而把它持久化就要么新开一张表,
    # 要么把一个全局计数器塞进一张按 notebook 分区的表里。
    # ⚠ 默认值与 ``RETRIEVAL_EXPERIENCE_BATCH_RUNS``(每批读取的 run 数上限,
    # 40)对齐,而不是任取一个大于它的数:两者相等让相邻两个批次天然趋于「刚好
    # 衔接、不重叠」,读全局最近 40 条完成提问的批次读到的正是上一次触发之后
    # 新增的那些——真正的重叠仍会发生(触发计数到达阈值与实际读取之间有间隙),
    # 但把默认值对齐到批次大小是把重叠窗口压到最小,而不是放大它。
    retrieval_experience_trigger: int = Field(
        40, ge=1, validation_alias="RETRIEVAL_EXPERIENCE_TRIGGER")
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
    query_refine_max_items: int = Field(
        12, ge=1, validation_alias="QUERY_REFINE_MAX_ITEMS"
    )
    # Relationship lines appended to the citable evidence context are ranked
    # by support. Their result-changing take belongs beside the other Ask
    # retrieval budgets rather than in evidence_context.py as ``[:30]``.
    ask_context_relation_limit: int = Field(
        30, ge=1, validation_alias="ASK_CONTEXT_RELATION_LIMIT"
    )
    # chunk-native 检索分块: chunk 目标字数 / 相邻重叠(P1 overlap 默认 0)。
    chunk_target_chars: int = Field(600, validation_alias="CHUNK_TARGET_CHARS")
    chunk_overlap_chars: int = Field(0, validation_alias="CHUNK_OVERLAP_CHARS")
    # chunk-native 检索: 大召回候选数 / MMR 精选数 / MMR λ / 答案上下文预算(长上下文综合)。
    chunk_recall: int = Field(200, validation_alias="CHUNK_RECALL")   # mix 候选池/MMR 候选;LightRAG 风格猛召回
    # 词法候选的语料语言闸:库内没有任何 CJK 字符时,丢掉纯 CJK 的三字片段词项
    # ——它们对该库保证零命中,却在 PostgreSQL 上各买一次真实探针(实测 7,026 块
    # 的英文库:64 词项 29.7s/26 行,其中 26 行全部来自 2 个拉丁词项;报告 4 节
    # 并发即撞 POSTGRES_STATEMENT_TIMEOUT_SECONDS,整条词法臂 fail-open 阵亡)。
    # 只做 CJK 方向,只过滤优先头之后的词项;判据是既有采样探针 `_notebook_langs`。
    # 设 false 完全回到接入前(不过滤、零行为差异),用于探针误判某库时的临时恢复。
    lexical_language_gate_enabled: bool = Field(
        True, validation_alias="LEXICAL_LANGUAGE_GATE_ENABLED")
    # PostgreSQL 大库 KG 名词法探针的 GiST KNN 早停(默认开;设 false 回滚)。
    # 现状 ORDER BY similarity 是形态 B:LIMIT 无法提前终止,常见短词要对全部
    # trgm 候选回表、算相似度、排序(9.1M 行 base 实测 'DAC' 单词项 7.4s,多词项
    # 直接超时,整条词法臂 fail-open 阵亡)。KNN(`<->` 距离序)按相似度序增量
    # 吐行、到 LIMIT 即停(同库实测 123ms,60×)。只改访问路径,分数仍是
    # similarity();等相似度并列类内的成员选择可能与旧路径不同、且 KNN 自身在
    # 并列类内不是 run-to-run 稳定的(实测 'DAC' 有 285 行 sim=1.0 同名对象)
    # ——登记接受的取舍(2026-08-06 拍板:60× 收益优先;需要位稳定候选集的部署
    # 用本开关回滚)。只在 PostgreSQL 且存在精确匹配形状的 GiST 索引时生效
    # (运行时探测,缺索引静默走旧路径;PG 侧每次未收窄探针为规模判定付一条
    # 已索引的单行版本查询——亚毫秒,登记接受,见 _lexical_knn_allowed);
    # SQLite 适配器声明不具备该能力(lexical_knn_capable=False),判定在读取任何
    # 规模统计之前零成本短路——发行默认后端不为 PG 专属特性付哪怕一条查询。
    postgres_lexical_knn_enabled: bool = Field(
        True, validation_alias="POSTGRES_LEXICAL_KNN_ENABLED")
    # KNN 路由的规模下限(nodes+chunks)。GiST 索引没有 notebook 键,KNN 走的是
    # **全库**距离序、逐行按 notebook 过滤——只有目标库在整张表里占主导份额时才
    # 划算;小份额库要在别人的行里翻找自己的 67 条,反而丢掉它刚拿到的复合 GIN
    # 快路径。份额没有零查询的算法,用远高于 copyable 闸(5000)的绝对下限近似:
    # 请把它设在「除主导库外最大的那个库」之上(实测部署:主导库 1.1e7,次大 2.7e4,
    # 默认 5e5 干净分割)。低于下限 → legacy,SQL 逐字不变。
    postgres_lexical_knn_min_rows: int = Field(
        500_000, validation_alias="POSTGRES_LEXICAL_KNN_MIN_ROWS")
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
    # 语义策略的规模分派阈值: 同类型组内节点数 ≤ 此值走原逐对暴力余弦(逐位保持既有
    # 行为); 超过则改 hnswlib cosine ANN 近邻(近似召回,登记接受的行为差异——阈值以下
    # 一字不差,阈值以上的库在改造前根本跑不完 O(N²))。
    # 512 不是 2000: C(512,2)≈13 万次 1024 维点积尚在秒级,而 C(2000,2)≈200 万次
    # 是分钟级的纯 Python 二次方窗口——数百节点的典型库仍整段走精确分支。
    kg_conflict_semantic_bruteforce_max: int = Field(
        512, gt=0, validation_alias="KG_CONFLICT_SEMANTIC_BRUTEFORCE_MAX"
    )
    # ANN 分支每个节点取的近邻数(不含自身)。
    kg_conflict_semantic_ann_k: int = Field(
        10, gt=0, validation_alias="KG_CONFLICT_SEMANTIC_ANN_K"
    )
    # 进入 LLM 裁决的候选总上限;按信号类(边策略 / 节点策略)各分一半配额,某类
    # 用不满的额度让给另一类,类内保留检测器产出序。纯前缀截断会让一个 hub 产生的
    # shared_head 边候选吃光预算、判别类候选整类消失。分类截断数进返回值与事件日志。
    kg_conflict_max_candidates: int = Field(
        800, gt=0, validation_alias="KG_CONFLICT_MAX_CANDIDATES"
    )
    # 规模准入: 活跃知识对象数超过此值时拒绝触发冲突检测(409/建图末尾跳过),不把
    # 超大库放进注定跑不完的后台任务。内存依据: 向量以一整块 (N, dim) float32 装载,
    # 20 万 × 1024 维 ≈ 0.8 GB;判别索引每个对象只留 (组号, 余量哈希) 整数对。
    kg_conflict_max_objects: int = Field(
        200000, gt=0, validation_alias="KG_CONFLICT_MAX_OBJECTS"
    )
    # Relation-side admission is independent of the object ceiling: a sparse
    # object set may still carry a very dense relation graph.  The resolver
    # refuses the whole pass (never a partial candidate set) above this rail.
    kg_conflict_max_relations: int = Field(
        1000000, gt=0, validation_alias="KG_CONFLICT_MAX_RELATIONS"
    )
    # chunk×graph mix: 叠加 KG 子图 block 和源 chunk 进候选池(默认开)。关闭后退化为纯 chunk 检索。
    chunk_kg_overlay_enabled: bool = Field(True, validation_alias="CHUNK_KG_OVERLAY_ENABLED")
    # Candidate widths for the optional chunk×KG overlay. They are retrieval
    # budgets (not protocol constants), and therefore must be named/tunable.
    chunk_kg_node_seed_top_n: int = Field(
        DEFAULT_CHUNK_KG_NODE_SEED_TOP_N,
        ge=1,
        validation_alias="CHUNK_KG_NODE_SEED_TOP_N",
    )
    chunk_kg_relation_seed_top_n: int = Field(
        DEFAULT_CHUNK_KG_RELATION_SEED_TOP_N,
        ge=1,
        validation_alias="CHUNK_KG_RELATION_SEED_TOP_N",
    )
    chunk_kg_max_depth: int = Field(
        DEFAULT_CHUNK_KG_MAX_DEPTH,
        ge=1,
        validation_alias="CHUNK_KG_MAX_DEPTH",
    )
    chunk_kg_fan_out: int = Field(
        DEFAULT_CHUNK_KG_FAN_OUT,
        ge=1,
        validation_alias="CHUNK_KG_FAN_OUT",
    )
    # Final mix selection reserves this many slots for graph-only chunks when
    # they clear the existing relevance floor.  It never enlarges the token
    # budget or changes the historical oversized-first-chunk exception.
    chunk_graph_reserve: int = Field(0, validation_alias="CHUNK_GRAPH_RESERVE")
    # Selected-source graph snapshots are an internal, read-only preparation
    # surface until a later rollout PR wires them into Ask/Report. Every
    # projection query probes LIMIT+1 and disables only the affected channel
    # when a rail is crossed; none may fall back to a whole-notebook scan.
    source_subgraph_max_sources: int = Field(
        32, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_SOURCES"
    )
    source_subgraph_max_objects: int = Field(
        20_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_OBJECTS"
    )
    source_subgraph_max_relations: int = Field(
        40_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_RELATIONS"
    )
    source_subgraph_max_chunks: int = Field(
        20_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_CHUNKS"
    )
    source_subgraph_max_facts: int = Field(
        20_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_FACTS"
    )
    source_subgraph_max_fact_elements: int = Field(
        60_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_FACT_ELEMENTS"
    )
    source_subgraph_max_memberships: int = Field(
        60_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_MEMBERSHIPS"
    )
    source_subgraph_max_cluster_memberships: int = Field(
        20_000, gt=0, validation_alias="SOURCE_SUBGRAPH_MAX_CLUSTER_MEMBERSHIPS"
    )
    source_subgraph_cache_max_entries: int = Field(
        64, gt=0, validation_alias="SOURCE_SUBGRAPH_CACHE_MAX_ENTRIES"
    )
    # The artifact PPR lane shares the selected-source graph row rails above.
    # Its separate iteration ceiling bounds request-time full-CSR passes.
    source_partitioned_ppr_max_iterations: int = Field(
        30, gt=0, le=100, validation_alias="SOURCE_PARTITIONED_PPR_MAX_ITERATIONS"
    )
    # 精确标识符 fast path(exact_lookup):问题里出现 `_`/`-`/`.` 连接的完整命令名
    # (set_db、place_opt_design)时,先精确定位它所在的小节,再把整节的 chunk 一次
    # 取齐并入候选——手册小节被 600 字分块切成主描述/参数表/示例,普通检索只召回
    # 其中最相关的一块,答案就缺参数细节。零模型调用、零 embedding;查询里没有标识符
    # 时整条通道零 IO(总闸在 identifier_terms)。
    exact_lookup_enabled: bool = Field(True, validation_alias="EXACT_LOOKUP_ENABLED")
    # 一次问答最多用前 N 个标识符探测(粘贴整页命令表时的上界)。
    exact_lookup_max_identifiers: int = Field(
        3, validation_alias="EXACT_LOOKUP_MAX_IDENTIFIERS")
    # 每个标识符的精确命中窗口(用于统计哪个小节命中最多,不是最终结果数)。
    exact_lookup_fts_k: int = Field(50, validation_alias="EXACT_LOOKUP_FTS_K")
    # 最多取齐几个小节 / 每个小节最多取几块 —— 通道的结构性硬界。
    exact_lookup_max_sections: int = Field(
        3, validation_alias="EXACT_LOOKUP_MAX_SECTIONS")
    exact_lookup_max_chunks_per_section: int = Field(
        12, validation_alias="EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION")
    # mix 最终选择为精确小节 chunk 预留的席位数。与 chunk_graph_reserve 同构:
    # 只在既有 token 预算内预留,绝不扩预算,也不制造第二个 oversize 例外。
    exact_section_reserve: int = Field(4, validation_alias="EXACT_SECTION_RESERVE")
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
    # Strict JSON remains the fast path. Approved interactive workloads may
    # conservatively recover complete object-shaped responses with quote/comma
    # defects; shadow measures repairability without changing acceptance.
    model_json_repair_mode: Literal["off", "shadow", "on"] = Field(
        "on", validation_alias="MODEL_JSON_REPAIR_MODE"
    )

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

    # /dev/logs "Activity" view (default tab). Distinct from debug_logs_enabled:
    # this gates the three admin activity endpoints (own/other users' asks,
    # source lists, ask detail incl. full question/answer text), not JSONL log
    # file reads. Defaults on because Activity is the default tab; deployments
    # that care about cross-user question/answer visibility can opt out.
    user_activity_view_enabled: bool = Field(
        True, validation_alias="USER_ACTIVITY_VIEW_ENABLED"
    )

    # PDF parsing via MinerU (decoupled from GPU). Modes:
    #   "off"  -> use local PyMuPDF4LLM (pypdf last resort; no GPU, offline)
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
    # Extra attempts after a transient remote HTTP failure. Shared by the
    # self-hosted /file_parse adapter and mineru.net cloud requests.
    mineru_max_retries: int = Field(
        2,
        ge=0,
        le=5,
        validation_alias="MINERU_MAX_RETRIES",
    )
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
    # Reserved for the later shadow-sync phase. It never changes the active backend.
    shadow_database_url: str | None = Field(None, validation_alias="SHADOW_DATABASE_URL")
    postgres_pool_min_size: int = Field(1, validation_alias="POSTGRES_POOL_MIN_SIZE")
    postgres_pool_max_size: int = Field(10, validation_alias="POSTGRES_POOL_MAX_SIZE")
    postgres_pool_acquire_timeout_seconds: int = Field(
        10,
        validation_alias="POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS",
    )
    postgres_statement_timeout_seconds: int = Field(
        30,
        validation_alias="POSTGRES_STATEMENT_TIMEOUT_SECONDS",
    )
    postgres_lock_timeout_seconds: int = Field(
        5,
        validation_alias="POSTGRES_LOCK_TIMEOUT_SECONDS",
    )
    storage_dir: str = Field(".local/storage", validation_alias="SILICON_NOTEBOOK_STORAGE_DIR")
    # Notebook 分享拷贝阈值:超过任一阈值的库仅可只读共享(Phase 2),不可深拷贝。
    notebook_copy_max_bytes: int = Field(50 * 1024 * 1024, validation_alias="NOTEBOOK_COPY_MAX_BYTES")
    notebook_copy_max_rows: int = Field(5000, validation_alias="NOTEBOOK_COPY_MAX_ROWS")
    # 深拷贝纵深护栏:整本拷贝会把「所有」参与表(relations/embeddings/elements/knowhow…)
    # 的行一并读进内存做 id 重映射,而上面的 copyable 闸只界定 chunks+nodes——一个
    # 图/向量扇出远超其 chunk+node 数的源(极少节点、百万关系)会通过 copyable 闸却在
    # 此物化出数 GB。本上限对「快照全部表的总行数」封顶,超了在任何 fetchall 之前拒绝
    # (改只读共享),使深拷贝的峰值内存与病态扇出解耦(codex PR#353 r5 P2)。
    notebook_copy_max_snapshot_rows: int = Field(200_000, validation_alias="NOTEBOOK_COPY_MAX_SNAPSHOT_ROWS")
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

    @field_validator("database_url", mode="before")
    @classmethod
    def validate_database_url(cls, value):
        if isinstance(value, _InvalidDatabaseUrl):
            raise ValueError(value.reason)
        return normalize_database_url(value)

    @field_validator("shadow_database_url", mode="before")
    @classmethod
    def validate_shadow_database_url(cls, value):
        if isinstance(value, _InvalidDatabaseUrl):
            raise ValueError(value.reason)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_database_url(value)

    @model_validator(mode="before")
    @classmethod
    def sanitize_invalid_database_url_inputs(cls, values):
        if not isinstance(values, dict):
            return values
        prepared = dict(values)
        for key in (
            "DATABASE_URL",
            "database_url",
            "SHADOW_DATABASE_URL",
            "shadow_database_url",
        ):
            raw = prepared.get(key)
            if raw is None or ("SHADOW" in key.upper() and isinstance(raw, str) and not raw.strip()):
                continue
            try:
                prepared[key] = normalize_database_url(raw)
            except ValueError as exc:
                prepared[key] = _InvalidDatabaseUrl(
                    sanitize_database_url_for_error(raw),
                    str(exc),
                )
        return prepared

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
        """storage_dir 与 SQLite database_url 的相对路径统一锚定到仓库根。

        - 绝对路径（默认或 env 覆盖）原样保留，不重锚。
        - PostgreSQL URL 在这里仅接受、规范化和保留；正式 backend 仍由后续
          repository factory 选择，不能静默回落到 SQLite。
        - sqlite:/// 的拼写规则：`sqlite:///relative/path`（三斜杠+相对路径）
          与 `sqlite:////abs/path`（三斜杠分隔符 + 绝对路径本身以 / 开头，
          视觉上四个斜杠）；判断标准是把 `sqlite:///` 前缀去掉后剩余部分是否
          是绝对路径，而非数斜杠。
        """
        if self.storage_dir and not Path(self.storage_dir).is_absolute():
            self.storage_dir = str(_ROOT_DIR / self.storage_dir)

        if self.model_services_config and not Path(self.model_services_config).is_absolute():
            self.model_services_config = str(_ROOT_DIR / self.model_services_config)

        if database_identity(self.database_url).scheme != "sqlite":
            return self

        prefix = "sqlite:///"
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
    def source_upload_max_bytes(self) -> int:
        """Configured per-source upload cap in bytes (1 MB = 1024 × 1024 bytes)."""
        return self.source_upload_max_mb * 1024 * 1024

    @property
    def mineru_cloud_enabled(self) -> bool:
        return bool(self.mineru_api_token)

    @property
    def sqlite_path(self) -> str:
        if database_identity(self.database_url).scheme == "sqlite":
            return database_identity(self.database_url).database
        raise ValueError("DATABASE_URL is not a SQLite URL")

    def __repr_args__(self):
        for name, value in super().__repr_args__():
            if name in {"database_url", "shadow_database_url"} and value:
                value = redact_database_url(value)
            yield name, value


@lru_cache
def get_settings() -> Settings:
    return Settings()
