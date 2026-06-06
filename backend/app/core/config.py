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

    embed_provider: str = Field("", env="EMBED_PROVIDER")          # ""(off) | dashscope
    embed_model: str = Field("", env="EMBED_MODEL")
    embed_base_url: str = Field("", env="EMBED_BASE_URL")
    embed_api_key: str = Field("", env="EMBED_API_KEY")
    embed_dim: int = Field(1024, env="EMBED_DIM")

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
    embed_concurrency: int = Field(50, env="EMBED_CONCURRENCY")
    # SQLite 忙等待超时（毫秒），配合 WAL 支持后台向量化与抽取并发写。
    db_busy_timeout_ms: int = Field(30000, env="DB_BUSY_TIMEOUT_MS")
    # 检索：top-N 知识对象。
    retrieval_top_n: int = Field(12, env="RETRIEVAL_TOP_N")
    # 追问改写：问题长度 ≤ 此值（或含指代标记）才触发轻量 LLM 改写。
    followup_max_len: int = Field(12, env="FOLLOWUP_MAX_LEN")
    # grounded 三档阈值（作用于融合相关度 .relevance ∈[0,1]）。
    # 注意：现有 grounded 测试要求 tau_high ≤ 0.4（纯关键词命中融合分=0.4）。
    evidence_tau_low: float = Field(0.18, env="EVIDENCE_TAU_LOW")
    evidence_tau_high: float = Field(0.35, env="EVIDENCE_TAU_HIGH")
    # 流程类问题 top-N 至少保底召回的 procedure 条数。
    proc_min: int = Field(2, env="PROC_MIN")

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
    def sqlite_path(self) -> str:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return self.database_url[len(prefix) :]
        return ".local/silicon_notebook.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
