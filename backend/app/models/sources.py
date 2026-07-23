from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PaperAuthor(BaseModel):
    name: str
    affiliation: str = ""  # 多机构以 "; " 连接;接地校验不过则为空


class PaperMeta(BaseModel):
    """论文元数据(接地校验后)。非论文源/未抽取时整个对象缺省。"""
    is_paper: bool = False
    title: Optional[str] = None
    venue: Optional[str] = None
    year: Optional[int] = None
    doi: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    authors: List[PaperAuthor] = Field(default_factory=list)


class SourceElement(BaseModel):
    id: str
    source_id: str
    element_type: str
    location_label: str
    text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SourceSummary(BaseModel):
    id: str
    notebook_id: str
    title: str
    type: str
    status: str
    summary: str
    element_count: int
    file_name: str = ""
    file_size: int = 0
    file_hash: str = ""
    source_url: str = ""  # 非空表示这是「在线 URL」来源，由 mineru.net 云端解析
    parse_status: str = ""
    created_label: str = ""
    doc_type: str = ""  # "" = auto-detect; else an extraction profile id
    # Non-empty when the latest KG extraction had network-failed windows that
    # silently contributed zero nodes (degraded run, not a clean "completed").
    extraction_warning: Optional[str] = None
    # 该 source 是否已抽取 KG / 已入图
    kg_extracted: bool = False
    # 论文元数据投影:作者姓名按署名序;非论文/未抽取为空(paper-metadata)。
    authors: List[str] = Field(default_factory=list)
    pub_year: Optional[int] = None
    venue: Optional[str] = None
    # 派生态(零新查询,见 SourceStore._paper_meta_status_for):
    # "has_meta"=已判定是论文且有元数据 | "not_paper"=已判定非论文(标记行)
    # | "missing"=合规候选但尚未跑过论文元数据抽取 | None=不适用(memory/knowhow/
    # 非论文 doc_type/未解析完成)。
    paper_meta_status: Optional[str] = None


class PaginatedSources(BaseModel):
    items: List[SourceSummary]
    total_count: int
    offset: int
    limit: int


class SourceImportFile(BaseModel):
    file_name: str
    file_size: int = 0
    mime_type: str = ""
    doc_type: str = ""


class SourceImportRequest(BaseModel):
    files: List[SourceImportFile]


class AddUrlSourcesRequest(BaseModel):
    urls: List[str]


class RejectedUrl(BaseModel):
    url: str
    reason: str


class AddUrlSourcesResult(BaseModel):
    created: List[SourceSummary]
    rejected: List[RejectedUrl]


class ReparseSourcesRequest(BaseModel):
    """体检修复(H2/H3):批量重新解析指定的源。source_ids 由前端从体检结果的 sample
    带来;后端按 notebook 作用域过滤后逐个后台重跑 process_source。"""
    source_ids: List[str]


class RepairScheduledResult(BaseModel):
    """一个后台修复动作的受理回执。``scheduled`` 是实际排入后台的 source_id(reparse 经
    notebook 作用域过滤后的);notebook 级动作(补向量)用布尔 ``accepted``。"""
    scheduled: List[str] = []
    accepted: bool = False


class SourceDetail(SourceSummary):
    file_path: str = ""
    error_message: str = ""
    paper_meta: Optional[PaperMeta] = None


class UploadedSourceSummary(SourceSummary):
    """One row of an upload response: a plain ``SourceSummary`` plus whether it
    is a brand-new source or an existing same-content one that was reused.

    Purely additive — every field the pre-dedup upload contract returned is
    still here, so any client that ignores ``reused`` behaves exactly as
    before. Clients that count "how many sources did this upload add" MUST
    read it: same-notebook content dedup means an upload can return sources it
    did not create, and counting those inflates the notebook's source total
    until the page is reloaded."""

    reused: bool = False


class DetectDocTypeItem(BaseModel):
    """One file's name + leading text sample for upload-time type detection."""
    name: str
    sample: str = ""


class DetectDocTypesRequest(BaseModel):
    """Payload for POST /detect-doc-types (batched, one request for many files)."""
    items: List[DetectDocTypeItem] = Field(default_factory=list)


class DetectedDocType(BaseModel):
    """Detection result per file; doc_type_id '' means undetected (-> auto)."""
    name: str
    doc_type_id: str
