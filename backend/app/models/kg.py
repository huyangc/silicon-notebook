from typing import Literal

from pydantic import BaseModel


class KgBuildJobStatus(BaseModel):
    job_id: str
    mode: Literal["incremental", "rebuild"]
    status: Literal["running", "succeeded", "failed"]
    stage: Literal["probing", "extracting", "stopping", "finished"]
    total_sources: int = 0
    completed_sources: int = 0
    failed_sources: int = 0
    error_code: str = ""
    user_message: str = ""
    updated_at: str = ""
