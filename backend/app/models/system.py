from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ParserEngineCapability(BaseModel):
    """Sanitized automatic parser capability exposed to signed-in clients."""

    model_config = ConfigDict(extra="forbid")

    id: Literal["mineru_self_hosted", "mineru_cloud", "builtin"]
    priority: int = Field(gt=0)
    execution: Literal["local", "private_service", "public_cloud"]
    file_extensions: list[str]
    capabilities: list[
        Literal[
            "structured_text",
            "headings",
            "layout",
            "tables",
            "formulas",
            "images",
            "ocr",
        ]
    ]
    supports_url: bool
    fallback: bool
    available: bool
    unavailable_reason: Literal[
        "disabled", "missing_endpoint", "missing_credentials"
    ] | None = None


class SystemConfiguration(BaseModel):
    """Non-sensitive, authenticated configuration required by the web client."""

    model_config = ConfigDict(extra="forbid")

    source_upload_max_bytes: int = Field(gt=0)
    source_upload_max_files_per_batch: int = Field(gt=0)
    supported_source_extensions: list[str]
    parser_engines: list[ParserEngineCapability]
    # Browser outline editor mirrors the server validation rail so a deployment
    # override never turns into a surprising 422 after the user finishes editing.
    report_max_sections: int = Field(gt=0)
    report_max_subqueries_per_section: int = Field(gt=0)
    # /dev/logs「活动」tab 的能力位，直接反映 Settings.user_activity_view_enabled——
    # 关闭时前端不应默认进一个会全部 404 的视图（见 dev/logs/page.tsx 与 system-api.ts
    # 的消费逻辑）。
    user_activity_view_enabled: bool
