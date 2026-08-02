"""命令目录抽取(方案 C·C1b)的 API 传输模型。

服务层(`app/services/catalog_job.py`)产出 dict / dataclass;这里是它的 pydantic
传输层,也是前端(C1c)唯一该读的合同。

读这份契约前必须先知道的三件事:

1. **`diagnostic` 不在任何响应里,这是刻意的。** 任务行有两列文案:
   `failure_reason` 是按 `user_error()` 口径写的中文用户文案,可以原样上屏;
   `diagnostic` 是内部诊断(拦截样本、异常码)。「能不能给用户看」按**出处**判定,
   所以内部那一列根本不进传输层,而不是靠前端自觉不渲染。

2. **`preview` 的数字是下界,不是普查。** 它只读来源的一段有界前缀
   (`element_limit` 条元素,每条再截断),`sampled=true` 就表示到顶了、真实成本更高。
   一个会把自己要估算的那次扫描先做一遍的成本预告没有意义。

3. **候选是「未确认」的,`apply` 才写库。** `state` 有四档:`candidate`(待审阅)、
   `rejected`(接地校验整条拦下,连同 `rejections` 一起入表——一次零产出的抽取,
   用户唯一能自己判断「是模型错了还是这份文档不是手册」的依据就是它)、
   `applied`(已确认并落入命令目录表)、`dismissed`(未落库,原因经 `dismiss_reason`
   带一个稳定原因码——「已跳过」页签靠它显示为什么这条候选没有落库,不是普通的
   拒绝/接地失败)。`dismissed` 有两个写者、两个原因码:`apply` 冲突时自动写
   `conflict_existing_row`(目标表已有同名行);`.../dismiss` 端点(R7)由审阅者
   显式写 `user_dismissed`——R5/R6 加的「有待审候选拦重跑」守卫需要一条真正的
   放弃路径,否则一条审阅者刻意不要的候选会把整个来源永久锁在重新识别之外。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class CommandCatalogSignal(BaseModel):
    """形状检测的计数证据 + 一个从不单独成立的阈值判断。

    `is_manual` 只是让调用方有个默认值;计数才是重点——「找到约 N 个命令段落」交给
    人来判断,是对一个启发式唯一诚实的用法。
    """

    total_sections: int = 0
    identifier_headings: int = 0
    syntax_sections: int = 0
    flag_sections: int = 0
    command_shaped_sections: int = 0
    command_ratio: float = 0.0
    flag_ratio: float = 0.0
    is_manual: bool = False
    reason: str = "no_sections"


class CommandCatalogPreview(BaseModel):
    source_id: str
    source_title: str = ""
    signal: CommandCatalogSignal = CommandCatalogSignal()
    estimated_sections: int = 0
    estimated_calls: int = 0
    sampled: bool = False
    element_limit: int = 0


class CommandCatalogProgress(BaseModel):
    sections_total: int = 0
    sections_done: int = 0
    entries: int = 0
    rejected: int = 0
    uncovered: int = 0
    truncated_sections: int = 0
    # How many of this job's candidates are still `state='candidate'`
    # (unreviewed). NOT derivable from the columns above — `entries` is a
    # write-once extraction-time tally that never moves once `apply`/`cancel`
    # changes a row's state. Callers that can answer this cheaply (a job that
    # is not brand-new) fill it in; a freshly created job has no candidates
    # yet, so the default of 0 is already correct there without a query.
    # R5 P2: this is what lets the frontend block "重新识别" while the
    # previous run's candidates are still unreviewed — `.../job` only ever
    # returns the latest job, so starting a new one would orphan them.
    pending_candidates: int = 0


class CommandCatalogJob(BaseModel):
    id: str
    notebook_id: str
    source_id: str
    status: str
    progress: CommandCatalogProgress = CommandCatalogProgress()
    failure_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    finished_at: str = ""

    @classmethod
    def of(cls, row: dict, *, pending_candidates: int = 0) -> "CommandCatalogJob":
        return cls(
            id=row["id"],
            notebook_id=row["notebook_id"],
            source_id=row["source_id"],
            status=row["status"],
            progress=CommandCatalogProgress(
                sections_total=int(row.get("sections_total") or 0),
                sections_done=int(row.get("sections_done") or 0),
                entries=int(row.get("entries") or 0),
                rejected=int(row.get("rejected") or 0),
                uncovered=int(row.get("uncovered") or 0),
                truncated_sections=int(row.get("truncated_sections") or 0),
                pending_candidates=max(0, int(pending_candidates)),
            ),
            failure_reason=str(row.get("failure_reason") or ""),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            finished_at=str(row.get("finished_at") or ""),
        )


class CommandCatalogJobResponse(BaseModel):
    job: Optional[CommandCatalogJob] = None


class CommandCatalogStartResponse(BaseModel):
    status: str
    job: CommandCatalogJob


class CommandCatalogCancelResponse(BaseModel):
    status: str
    job: Optional[CommandCatalogJob] = None


class CommandCatalogArg(BaseModel):
    name: str = ""
    required: bool = False
    desc: str = ""
    default: str = ""


class CommandCatalogRejection(BaseModel):
    """一处被接地校验丢掉的值:哪个字段、什么值、为什么、以及在原文哪一段找的。

    `window` 是**有界**的原文片段(C1a 的 `REJECT_WINDOW_CHARS`),存在的理由是
    「模型少写了一个短横」这类问题只有把原文摆出来才看得懂。
    """

    field: str = ""
    value: str = ""
    reason: str = ""
    window: str = ""


class CommandCatalogCandidate(BaseModel):
    id: str
    position: int = 0
    section_path: str = ""
    command_name: str = ""
    state: str = "candidate"
    syntax: str = ""
    description: str = ""
    args: List[CommandCatalogArg] = []
    examples: List[str] = []
    anchors: List[str] = []
    excerpt: str = ""
    suspect_related: bool = False
    rejections: List[CommandCatalogRejection] = []
    # `dismissed` 候选的跳过原因码(如 `conflict_existing_row`),来自
    # `catalog_job.py` `_apply_locked` 写的 `reject_info={"reason": ...}` ——
    # 与 `rejections`(来自 `reject_info["fields"]`,接地校验逐字段拦截)是两套
    # 互不相关的载荷,一条候选只会落进其中一种。界面词由前端
    # `command-catalog-model.ts` 的 `dismissReasonText()` 负责,这里只传原始码。
    dismiss_reason: str = ""
    # `reject_info["overflow"]`/`["desc_overflow"]`(`catalog_job.py` 的
    # `_reject_info()`)此前落了库却在这里被丢弃——审阅面板因此没有任何办法
    # 知道「拦截记录被截了」这件事本身。两个都是有界记账,不是装饰:前者数
    # 超过 `MAX_SECTION_REJECTIONS` 未能进 `rejections` 的记录,后者数因
    # `MODEL_ARG_DESC_TOTAL_CHARS` 聚合预算被截短的参数说明段数,两者互不相关
    # (同一处 docstring 的理由),所以是两个字段而不是合并成一个。
    rejections_overflow: int = 0
    desc_overflow: int = 0

    @classmethod
    def of(cls, row: dict) -> "CommandCatalogCandidate":
        payload = row.get("payload") or {}
        reject_info = row.get("reject_info") or {}
        return cls(
            id=row["id"],
            position=int(row.get("position") or 0),
            section_path=str(row.get("section_path") or ""),
            command_name=str(row.get("command_name") or ""),
            state=str(row.get("state") or "candidate"),
            syntax=str(payload.get("syntax") or ""),
            description=str(payload.get("description") or ""),
            args=[
                CommandCatalogArg(
                    name=str(arg.get("name") or ""),
                    required=bool(arg.get("required")),
                    desc=str(arg.get("desc") or ""),
                    default=str(arg.get("default") or ""),
                )
                for arg in (payload.get("args") or [])
                if isinstance(arg, dict)
            ],
            examples=[str(item) for item in (payload.get("examples") or [])],
            anchors=[str(item) for item in (payload.get("anchors") or [])],
            excerpt=str(payload.get("excerpt") or ""),
            suspect_related=bool(payload.get("suspect_related")),
            rejections=[
                CommandCatalogRejection(
                    field=str(item.get("field") or ""),
                    value=str(item.get("value") or ""),
                    reason=str(item.get("reason") or ""),
                    window=str(item.get("window") or ""),
                )
                for item in (reject_info.get("fields") or [])
                if isinstance(item, dict)
            ],
            dismiss_reason=str(reject_info.get("reason") or ""),
            rejections_overflow=int(reject_info.get("overflow") or 0),
            desc_overflow=int(reject_info.get("desc_overflow") or 0),
        )


class CommandCatalogCandidatePage(BaseModel):
    """一页候选 + 这个 job 各档的总数。

    `next_cursor` 是 keyset 游标(上一页最后一条的 `position`),不是 offset:
    确认候选会改 `state`,offset 分页会在筛选后的集合上漏行/重行。
    """

    items: List[CommandCatalogCandidate] = []
    next_cursor: int = 0
    has_more: bool = False
    counts: Dict[str, int] = {}


class CommandCatalogApplyRequest(BaseModel):
    """确认请求。`candidate_ids` 与 `all_pending` 二选一,不是各自独立的开关:
    两者同时非空/为真会被路由层拒成 422(R13,codex PR #412 评审第 13 轮),不再
    像早前那样静默偏向 `all_pending` 而悄悄吞掉调用方明确写出的 `candidate_ids`。
    """

    candidate_ids: List[str] = []
    all_pending: bool = False


class CommandCatalogConflict(BaseModel):
    candidate_id: str
    command_name: str


class CommandCatalogApplyResult(BaseModel):
    """确认落库的结果。

    `conflicts` 是**没有写入**的那些:目标表里已经有同名命令的行。v1 刻意保守——
    绝不覆盖用户手工编辑过的内容,完整 diff/merge 属后续任务。

    `pending_remaining` 是这次确认之后**仍待审阅**的候选数。一次 `all_pending`
    最多确认一页,所以它不是可选的装饰:少了它,300 条候选的库点一次「确认全部」
    看到 `rows_added: 100` 会以为做完了。前端必须据它继续提示。

    `table_title` 是这次 apply 实际解析/创建的目标表标题(`CATALOG_TABLE_TITLE_
    PREFIX` 拼当前来源的**规范**标题——论文来源优先用接地论文标题,而不是上传
    文件名)。R15(codex PR #412 评审第 15 轮,P2)之前前端只能自己用
    `sourceDetail.title`(原始上传名)预测这个标题,论文来源两者一旦不一致,
    「已写入《命令目录：<预测名>》」这句话就在撒谎——用户点开的其实是
    《命令目录：<论文标题>》。这个字段让前端直接读后端权威值,不再预测。
    """

    table_id: str
    table_title: str = ""
    created: bool = False
    applied: List[str] = []
    rows_added: int = 0
    conflicts: List[CommandCatalogConflict] = []
    pending_remaining: int = 0


class CommandCatalogDismissRequest(BaseModel):
    """跳过请求。形状与 `CommandCatalogApplyRequest` 逐字相同(`candidate_ids`
    二选一 `all_pending`,同时非空/为真同样 422,见该请求模型的注释),但刻意
    分开建模:这是各端点自己的传输合同,不是同一个请求体在两个 URL 下复用——
    将来任一个的字段独立演化都不会牵动另一个。
    """

    candidate_ids: List[str] = []
    all_pending: bool = False


class CommandCatalogDismissResult(BaseModel):
    """跳过的结果。不写 knowhow 表,所以没有 `apply` 结果里的
    `table_id`/`created`/`conflicts` 三个字段——那三个字段都是「写了哪张表」
    的追问,跳过从不触碰任何表,复用 `CommandCatalogApplyResult` 会强迫这里
    永远填一堆空/假值,读着像是「这次没写」而不是「这个动作压根不写」。

    `dismissed` 是这次真正被标记为 `dismissed` 的候选 id(已经不是
    `candidate` 状态的 id 静默跳过,不重复报告,与 `apply` 的 `selected` 过滤
    同一口径)。`pending_remaining` 与 `apply` 同名字段同一语义:这次动作之后
    仍待审阅的候选数,写在这里是因为它是「重新识别」拦截守卫读的同一个数字,
    前端跳过后要能立即知道守卫是否已经解除。
    """

    dismissed: List[str] = []
    pending_remaining: int = 0


__all__ = [
    "CommandCatalogApplyRequest",
    "CommandCatalogApplyResult",
    "CommandCatalogArg",
    "CommandCatalogCancelResponse",
    "CommandCatalogCandidate",
    "CommandCatalogCandidatePage",
    "CommandCatalogConflict",
    "CommandCatalogDismissRequest",
    "CommandCatalogDismissResult",
    "CommandCatalogJob",
    "CommandCatalogJobResponse",
    "CommandCatalogPreview",
    "CommandCatalogProgress",
    "CommandCatalogRejection",
    "CommandCatalogSignal",
    "CommandCatalogStartResponse",
]
