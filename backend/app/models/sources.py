import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


PDF_PYTHON_FALLBACK_WARNING_PREFIX = "[pdf-python-fallback]"

# 所选索引管线对该来源的分块提案畸形/失败、已按内建策略回退时,写进
# sources.error_message 的稳定前缀(同 PDF_PYTHON_FALLBACK_WARNING_PREFIX 的
# 机制:原始 warning code 只留在这条诊断里,对外只投影布尔)。写者是
# SourceChunkingService.build_chunks_for_source;为不覆盖 MinerU 降级诊断,
# 只在 error_message 为空或已是本前缀时写入。
INDEXING_CHUNK_FALLBACK_WARNING_PREFIX = "[indexing-chunk-fallback]"


def has_pdf_python_fallback_warning(error_message: object) -> bool:
    """Return only the safe public fact, never the stored MinerU diagnostic."""
    return str(error_message or "").startswith(PDF_PYTHON_FALLBACK_WARNING_PREFIX)


def has_indexing_chunk_fallback_warning(error_message: object) -> bool:
    """Return only the safe public fact, never the stored warning code."""
    return str(error_message or "").startswith(INDEXING_CHUNK_FALLBACK_WARNING_PREFIX)


_WINDOWS_FAILED = re.compile(r"windows_failed=(\d+)/(\d+)")

# 一次**跑完了、且合法地产出零个知识对象**的 KG 抽取,在 extraction_runs 里留下的
# 正向标记。写者是 source_ingestion.run_extraction 成功路径那条 f-string,它以
# ``kg objects={n}`` 起头,故 n=0 时整条消息以本前缀开头。
#
# 为什么需要一个**正向**标记,而不是「completed 且没有失败标记」:``no-llm``(抽取
# 模型未配置)写的也是一条 completed 且不带任何失败标记的记录,但那种源确实还没被
# 分析过——模型配好之后「分析新增」必须重新捡起它。只有本前缀能把「分析过、这篇
# 里没有可整理的知识」与「压根没分析」分开。
#
# ⚠ 取值与名字都不可改:四处存储层(双后端 × 计数/目标选择/来源投影)按它做前缀
# 匹配,已入库的历史行也带着这个前缀。改措辞会让存量行整体失配、把一批已分析的
# 来源打回「待分析」并重新付一遍模型钱。
KG_RUN_MESSAGE_OBJECTS_PREFIX = "kg objects="
KG_EMPTY_RUN_MESSAGE_PREFIX = f"{KG_RUN_MESSAGE_OBJECTS_PREFIX}0 "


def kg_analyzed_without_objects(status: object, error_message: object) -> bool:
    """最近一次 KG 抽取**已完成、且如实产出零个知识对象**吗?

    与 ``extraction_warning_text`` 同住一个模块、同一个理由:它是喂给「已分析 /
    待分析」这组判定的派生谓词,而四处存储层各按自己的方言把同一条判据写进 SQL。
    Python 侧留这一份是**判据的权威表述**——SQL 片段(两个后端各一份
    ``kg_run_sql.py``)必须与它逐条对应,守卫见
    ``tests/test_kg_empty_extraction_marker.py``。

    三条缺一不可:
    - ``status == 'completed'``:running/failed 的 run 什么都证明不了;
    - 消息以 ``KG_EMPTY_RUN_MESSAGE_PREFIX`` 开头:抽取真的跑到了成功路径且产出为零
      (排除 ``no-llm``、排除中止,见该常量的说明);
    - 没有失败窗口、也不是 partial 重试的半成品:``windows_failed=N/T``(N≥1)说明这
      一轮有窗口没跑成,零产出可能只是因为那些窗口丢了,不能据此判「这篇没有知识」。
    """
    if str(status or "") != "completed":
        return False
    message = str(error_message or "")
    if not message.startswith(KG_EMPTY_RUN_MESSAGE_PREFIX):
        return False
    if "retry_incomplete=1" in message:
        return False
    match = _WINDOWS_FAILED.search(message)
    return not (match and int(match.group(1)) > 0)


def extraction_warning_text(error_message: object) -> Optional[str]:
    """最近一次 KG 抽取留下网络失败窗口时的**用户可见**告警,从抽取记录的
    ``windows_failed=N/T`` 标记派生(这个事实不存在 sources 行上)。

    与 ``has_pdf_python_fallback_warning`` 同住一个模块、同一个理由:它是喂给前端
    异常徽标(``anomaly-severity.ts`` 的 ``sourceAnomalies()``)的派生谓词,来源页签
    和用户活动流命名的是同一批来源。四处存储层各抄一份的话,任何一处漂移都会让同一
    份来源在两个界面显示不同的异常态——与 ``source_display_title`` 是同一类问题,
    所以同样只留一份实现(守卫见 ``tests/test_source_derivations_single_source.py``)。
    """
    match = _WINDOWS_FAILED.search(str(error_message or ""))
    if not match:
        return None
    failed_windows = int(match.group(1))
    if failed_windows <= 0:
        return None
    total_windows = int(match.group(2))
    return (
        f"部分内容因网络问题未完成分析（{failed_windows}/{total_windows} 段失败），"
        "建议重新上传或重试。"
    )


# 论文元数据抽取的候选口径(与 SourceStore.sources_missing_paper_meta 的 WHERE 同源)。
HIDDEN_SYNTHETIC_SOURCE_TYPES = ("memory", "knowhow")
PAPER_META_ELIGIBLE_DOC_TYPES = ("", "academic_paper")
PAPER_META_ELIGIBLE_PARSE_STATUSES = ("parsed", "extracting", "extracted")


def paper_meta_status(
    is_paper: Any,
    source_type: object,
    doc_type: object,
    parse_status: object,
) -> Optional[str]:
    """``SourceSummary.paper_meta_status`` 的四态派生。纯函数、零 DB 访问。

    ``is_paper`` 直接收 SQL 侧的原值(SQLite 是 0/1,PostgreSQL 是 bool),按真值判定;
    为 None 表示**没有 source_paper_meta 行**(不是「有行但字段为空」);
    有行时按 is_paper 分流 has_meta / not_paper(标记行)。没有行时再区分「合规候选但
    尚未抽取」(missing)与「不适用」(None) —— 后者涵盖隐藏合成源(memory/knowhow)、
    非论文 doc_type、以及尚未解析完成的源。

    单一实现的理由同 ``extraction_warning_text``。
    """
    if is_paper is not None:
        return "has_meta" if is_paper else "not_paper"
    if source_type in HIDDEN_SYNTHETIC_SOURCE_TYPES:
        return None
    if doc_type not in PAPER_META_ELIGIBLE_DOC_TYPES:
        return None
    if parse_status not in PAPER_META_ELIGIBLE_PARSE_STATUSES:
        return None
    return "missing"


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
    # 面向用户的显示名(论文标题优先),由 source_display.source_display_title
    # 合成——那是这条规则的唯一定义点。刻意与 ``title`` 并存而不是改写它:
    # ``title`` 是 sources 表的原始列,既有的来源页签按它渲染,改写会是一次
    # 计划外的用户可见变更。新的展示路径一律读这个字段。空串表示该来源无名,
    # 由调用方决定占位文案(该函数刻意不造占位符)。
    display_title: str = ""
    type: str
    status: str
    summary: str
    element_count: int
    file_name: str = ""
    file_size: int = 0
    file_hash: str = ""
    source_url: str = ""  # 非空表示这是「在线 URL」来源，由 mineru.net 云端解析
    parse_status: str = ""
    # 原始 ISO 时间戳(SQLite 是文本列原样、PostgreSQL 经 iso_timestamp)。
    # ⚠ 为用户显示时间一律用这个字段:界面上的一切时间都按**浏览器本地时区**渲染
    # (设计稿 §3.1 的红线),而 created_label 是**服务端**按自己的日历日预先格式化好的
    # 「2026年8月4日」。同一份来源在活动视图左栏(来源清单)与中栏(活动流)各有一个
    # 入口,左栏吃 created_label、中栏吃时间戳时,服务端 UTC+8、浏览器 UTC 就会让同一
    # 份来源在两处显示成两个日期。created_label 保留只为不动既有来源页签的渲染。
    created_at: str = ""
    created_label: str = ""
    doc_type: str = ""  # "" = auto-detect; else an extraction profile id
    # Non-empty when the latest KG extraction had network-failed windows that
    # silently contributed zero nodes (degraded run, not a clean "completed").
    extraction_warning: Optional[str] = None
    # MinerU failed after its retry budget and this source completed through a
    # LOSSY local library parser for its format (PyMuPDF4LLM/pypdf, python-docx,
    # slide XML — see parsers.MINERU_FALLBACK_WARNING_SUFFIXES). Workbooks are
    # deliberately excluded: the openpyxl fallback is cell-value faithful, so a
    # warning there is noise the user could never clear. The raw upstream error
    # remains private in SourceDetail.error_message; clients receive only this
    # stable safe fact.
    parse_quality_warning: bool = False
    # 所选索引管线对这份来源的提案畸形/失败、内容已按内建策略回退整理(派生自
    # error_message 的 INDEXING_CHUNK_FALLBACK_WARNING_PREFIX,同上一条的机制)。
    indexing_chunk_fallback: bool = False
    # 该 source 是否已抽取 KG / 已入图
    kg_extracted: bool = False
    # 最近一次分析**跑完了、而这篇文档里确实没有可整理成知识图谱的内容**(正文极少、
    # 或几乎全是没有图注的图片)。与 kg_extracted 是互斥的两态,合起来给界面三态:
    # 已分析(kg_extracted) / 已分析·无知识(本字段) / 待分析(两者皆假)。
    #
    # 为什么必须新增一个字段而不是把 kg_extracted 置真:这批来源在检索里确实拿不出
    # 知识对象,谎称「已分析」会让用户以为图谱里有它。而沿用旧的二态则相反——它会
    # 永远显示「待分析」,让计数降不下来、每次「分析新增」都重跑一遍,那正是本字段
    # 要修的现场。派生规则见 kg_analyzed_without_objects。
    kg_analyzed_empty: bool = False
    # 论文元数据投影:作者姓名按署名序;非论文/未抽取为空(paper-metadata)。
    authors: List[str] = Field(default_factory=list)
    pub_year: Optional[int] = None
    venue: Optional[str] = None
    # 派生态(零新查询,见 SourceStore._paper_meta_status_for):
    # "has_meta"=已判定是论文且有元数据 | "not_paper"=已判定非论文(标记行)
    # | "missing"=合规候选但尚未跑过论文元数据抽取 | None=不适用(memory/knowhow/
    # 非论文 doc_type/未解析完成)。
    paper_meta_status: Optional[str] = None
    # 这份来源是不是 Agent 添加的(v48 `sources.agent_profile_id IS NOT NULL`)。
    # 刻意只上 wire 一个布尔:原始 agent_profile_id 是删除权限的判据,由服务层直接
    # 读行内列,没有任何理由把某个 Agent 的身份 id 发给浏览器。新增字段而不改写
    # 既有字段(同 display_title 先例),默认 False = 人添加的——那也正是全部存量行
    # 的取值。派生是零成本的:`SELECT *` 本来就把这一列取回来了,不加 join、不加查询。
    agent_created: bool = False


class PaginatedSources(BaseModel):
    items: List[SourceSummary]
    total_count: int
    offset: int
    limit: int


class PaginatedSourceElements(BaseModel):
    """A bounded window used by the source-detail reader.

    ``offset`` is the resolved window start.  When callers provide an anchor
    element the backend moves the window so that the anchor is included and
    reports that resolved offset back to the browser.
    """

    items: List[SourceElement]
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


class ScopedSourceDetail(SourceSummary):
    """参与集内**代理读取**的来源详情：`SourceDetail` 去掉两个后端内部字段。

    这个模型存在的唯一理由是收窄披露面。代理读取会把挂载参考库的来源交给一个对
    该库既非 owner 也非成员的用户，而 `SourceDetail` 带着两样只对库主有意义、却
    会泄露服务端内部形态的东西：

    - `file_path` 是后端存储的**本机绝对路径**，前端一处都没用过；
    - `error_message` 是 `str(exc)` 原样落库的异常串（`services/source_ingestion.py`），
      同样可能带上绝对路径（`FileNotFoundError: /…/storage/notebooks/…`）。前端从来
      不直出它，只按「有没有失败」二选一给固定文案——那点信息由 `parse_failed` 如实
      承载，原文没有任何理由跨出这个库。

    刻意做成**去掉字段**而不是运行时置空：schema 层的保证不会因为哪条分支忘了清而
    悄悄退回去。也刻意对同库/跨库**一视同仁**——同库来源另有 `/sources/{id}` 那条
    owner∪member 的完整详情路径，代理端点没必要按调用场景分叉出两种响应形状。
    """
    paper_meta: Optional[PaperMeta] = None
    parse_failed: bool = False

    @classmethod
    def of(cls, detail: SourceDetail) -> "ScopedSourceDetail":
        return cls(
            **detail.model_dump(exclude={"file_path", "error_message"}),
            parse_failed=detail.parse_status == "failed",
        )


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
