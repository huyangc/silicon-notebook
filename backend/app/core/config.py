from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = Field("development", env="SILICON_NOTEBOOK_ENV")
    single_user_email: str = Field(
        "local-user@silicon-notebook.dev",
        env="SILICON_NOTEBOOK_SINGLE_USER_EMAIL",
    )
    single_user_name: str = Field(
        "Local Curator",
        env="SILICON_NOTEBOOK_SINGLE_USER_NAME",
    )

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
    # 抽取自校验: 默认开启(2026-06-18,攻 KG 内容质量瓶颈); 每窗口抽取完再做一次 LLM refine pass 剔幻觉。仅建图时生效。
    kg_refine_enabled: bool = Field(True, env="KG_REFINE_ENABLED")
    # gleaning 补抽: 默认开启(2026-06-18); 每窗口首抽完再多轮让 LLM 补"遗漏的节点"(提 recall)。仅建图时生效。
    kg_gleaning_enabled: bool = Field(True, env="KG_GLEANING_ENABLED")
    kg_gleaning_rounds: int = Field(1, env="KG_GLEANING_ROUNDS")
    # 概念簇描述融合: 默认开启(2026-06-18); 对 ≥2 成员的概念簇用 LLM 融合证据成一段描述。仅 rebuild_unified 时生效。
    kg_concept_desc_enabled: bool = Field(True, env="KG_CONCEPT_DESC_ENABLED")
    # 社区摘要: 默认关闭; 开启后对每个社区用 LLM 生成 title/summary/findings 报告。
    kg_community_summary_enabled: bool = Field(False, env="KG_COMMUNITY_SUMMARY_ENABLED")
    # 同时抽取的文档数上限（作业池容量）。窗口级并发仍由 KG_EXTRACT_WORKERS 全局封顶。
    kg_job_concurrency: int = Field(8, env="KG_JOB_CONCURRENCY")
    # LLM 连接池为交互式 ask 预留的连接数（连接池容量 = KG_EXTRACT_WORKERS + 此值）。
    kg_ask_reserve: int = Field(64, env="KG_ASK_RESERVE")
    # 单文档窗口数超过此值 → 记 WARN（不截断、不丢弃，仍全量抽取）。
    kg_window_warn_threshold: int = Field(1200, env="KG_WINDOW_WARN_THRESHOLD")
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
    # qwen3-rerank (DashScope text-rerank) 配置
    rerank_model: str = Field("", env="RERANK_MODEL")
    rerank_base_url: str = Field("https://dashscope.aliyuncs.com/api/v1", env="RERANK_BASE_URL")
    rerank_api_key: str = Field("", env="RERANK_API_KEY")
    rerank_max_docs: int = Field(500, env="RERANK_MAX_DOCS")
    relation_retrieval_enabled: bool = Field(False, env="RELATION_RETRIEVAL_ENABLED")
    relation_seed_top_n: int = Field(8, env="RELATION_SEED_TOP_N")
    kg_canonical_fold_enabled: bool = Field(False, env="KG_CANONICAL_FOLD_ENABLED")
    kg_about_downweight_enabled: bool = Field(False, env="KG_ABOUT_DOWNWEIGHT_ENABLED")
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
    storage_dir: str = Field(".local/storage", env="SILICON_NOTEBOOK_STORAGE_DIR")
    cors_origins: List[str] = Field(
        [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3001",
        ],
        env="SILICON_NOTEBOOK_CORS_ORIGINS",
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
    def embedder_configured(self) -> bool:
        return bool(
            (self.embed_provider or "").strip() == "dashscope"
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
