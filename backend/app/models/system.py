from pydantic import BaseModel, ConfigDict, Field


class SystemConfiguration(BaseModel):
    """Non-sensitive, authenticated configuration required by the web client."""

    model_config = ConfigDict(extra="forbid")

    source_upload_max_bytes: int = Field(gt=0)
    source_upload_max_files_per_batch: int = Field(gt=0)
