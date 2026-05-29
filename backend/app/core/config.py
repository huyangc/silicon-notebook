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
    openai_compat_embedding_model: str = Field(
        "",
        env="OPENAI_COMPAT_EMBEDDING_MODEL",
    )
    openai_compat_timeout_seconds: int = Field(
        60,
        env="OPENAI_COMPAT_TIMEOUT_SECONDS",
    )

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

    # PDF parsing via MinerU (decoupled from GPU). Modes:
    #   "off"  -> use the built-in pypdf text fallback (default; no GPU, offline)
    #   "http" -> call a remote MinerU service (mineru-api) at mineru_api_url
    #   "cli"  -> run the local `mineru` CLI as a subprocess (MinerU installed on host)
    mineru_mode: str = Field("off", env="MINERU_MODE")
    mineru_api_url: str = Field("", env="MINERU_API_URL")
    mineru_backend: str = Field("pipeline", env="MINERU_BACKEND")
    mineru_lang: str = Field("", env="MINERU_LANG")
    mineru_cli_bin: str = Field("mineru", env="MINERU_CLI_BIN")
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
    def embedding_configured(self) -> bool:
        return bool(
            self.openai_compat_base_url
            and self.openai_compat_api_key
            and self.openai_compat_embedding_model
        )

    @property
    def mineru_enabled(self) -> bool:
        mode = (self.mineru_mode or "off").lower()
        if mode == "http":
            return bool(self.mineru_api_url)
        if mode == "cli":
            return bool(self.mineru_cli_bin)
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
