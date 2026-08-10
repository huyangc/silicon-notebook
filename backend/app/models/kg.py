from typing import Literal, Optional

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


class UnifiedKgStatus(BaseModel):
    dirty: bool
    last_rebuild_at: str = ""
    objects: int = 0
    relations: int = 0
    clusters: int = 0
    viz_indexed: bool = False
    viz_nodes: int = 0
    viz_edges: int = 0
    viz_stale: bool = False
    viz_building: bool = False


class MergeReviewJob(BaseModel):
    status: str
    total: int = 0
    done: int = 0
    error: str = ""


class ScaleIndexStatus(BaseModel):
    exists: bool
    stale: bool
    building: bool
    eligible: bool
    n_nodes: int = 0
    n_chunks: int = 0
    n_ann: int = 0
    n_chunk_ann: int = 0
    has_chunk_ann: bool = False
    state: str = "unindexed"
    delta_chunks: int = 0
    total_chunks: int = 0
    unindexed_sources: int = 0
    has_unindexed_content: bool = False
    delta_searchable: bool = False
    last_built_at: str = ""
    queue_position: int = 0
    queue_length: int = 0
    queued_at: str = ""
    offpeak_in_window: bool = False
    offpeak_next_start_at: str = ""
    last_build_ms: int = 0


class RebuildScaleIndexRequest(BaseModel):
    when: str = "now"    # now(立即后台) | idle(低峰调度)
    mode: str = "auto"   # auto(有索引→fold 否则→full) | fold | full


class MergeReviewRequest(BaseModel):
    limit: int = 50
    # 非对称自动落地阈值;省略时由后端 settings 决定(KG_MERGE_CONFIRM/SEPARATE_THRESHOLD)。
    confirm_threshold: Optional[float] = None    # auto-merge 最低置信
    separate_threshold: Optional[float] = None   # auto keep-separate(reject)最低置信


class MergeReviewSummary(BaseModel):
    reviewed: int = 0
    confirmed: int = 0
    rejected: int = 0
    unsure: int = 0


class ConceptWhitelistEntry(BaseModel):
    term: str
    note: str = ""
    created_at: str = ""


class ConceptWhitelistAdd(BaseModel):
    term: str
    note: str = ""
