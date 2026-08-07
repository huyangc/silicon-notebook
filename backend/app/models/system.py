from pydantic import BaseModel, ConfigDict, Field


class SystemConfiguration(BaseModel):
    """Non-sensitive, authenticated configuration required by the web client."""

    model_config = ConfigDict(extra="forbid")

    source_upload_max_bytes: int = Field(gt=0)
    source_upload_max_files_per_batch: int = Field(gt=0)
    # Browser outline editor mirrors the server validation rail so a deployment
    # override never turns into a surprising 422 after the user finishes editing.
    report_max_sections: int = Field(gt=0)
    report_max_subqueries_per_section: int = Field(gt=0)
    # /dev/logs「活动」tab 的能力位，直接反映 Settings.user_activity_view_enabled——
    # 关闭时前端不应默认进一个会全部 404 的视图（见 dev/logs/page.tsx 与 system-api.ts
    # 的消费逻辑）。
    user_activity_view_enabled: bool
