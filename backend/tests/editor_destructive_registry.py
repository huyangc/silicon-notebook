"""Which edit-tier write routes narrow a named editor, and why the rest do not
(share-edit P1-T4, decision D14).

Every unsafe route whose ``ROUTE_LEVELS`` entry belongs to one of the seven
edit-tier capabilities (``Capability`` or ``ReaderBody``) has exactly one entry
here; ``test_every_edit_capability_unsafe_route_is_classified`` asserts the key
sets are equal, so a new content-write route has to say how it treats an
editor. Three kinds:

* ``OwnUploadOnly``: an editor may only act on what they uploaded themselves;
* ``AdminRequired``: an editor is refused (404), the owner and the notebook's
  admins are not -- the irreversible actions (a hard delete with its history,
  a history prune, moving a whole table out of the notebook);
* ``NotNarrowed(reason)``: an editor acts like a group admin, with the reason
  that is safe (recorded in history and revertible, rebuildable from the
  sources, only cancels a job, ...).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class OwnUploadOnly:
    pass


@dataclass(frozen=True)
class AdminRequired:
    pass


@dataclass(frozen=True)
class NotNarrowed:
    reason: str


EditorTreatment = Union[OwnUploadOnly, AdminRequired, NotNarrowed]

_NB = "/api/notebooks/{notebook_id}"
_KH = _NB + "/knowhow/{table_id}"
_CELL = _KH + "/rows/{row_id}/cells/{column_id}"
_SRC = _NB + "/sources/{source_id}/command-catalog"
_CODE = "/api/agent/knowhow/rows/{row_id}/cells/{column_id}/code"

_NEW = NotNarrowed("新增/更新")
_HISTORY = NotNarrowed("记入变更历史,可回退")
_REBUILDABLE = NotNarrowed("可从来源重建;D14 选 (b)")
_REVIEW = NotNarrowed("协作编辑本身的决定")
_CANCEL = NotNarrowed("只取消作业")
_KNOWLEDGE = NotNarrowed("知识治理改动可再改;读者分支只动本人对象")

REGISTRY: dict[tuple[str, str], EditorTreatment] = {
    # ---------------------------------------------------------------- sources
    ("DELETE", "/api/sources/{source_id}"): OwnUploadOnly(),
    ("POST", "/api/sources/{source_id}/parse"): NotNarrowed("重解析不删内容,可再解析"),
    ("POST", _NB + "/sources"): _NEW,
    ("POST", _NB + "/sources/import"): _NEW,
    ("POST", _NB + "/sources/url"): _NEW,
    ("POST", _NB + "/sources/reparse"): NotNarrowed("重解析不删内容,可再解析"),
    ("POST", _NB + "/assets"): _NEW,
    ("POST", _NB + "/backfill-vectors"): _REBUILDABLE,
    ("POST", _NB + "/paper-meta/backfill"): _REBUILDABLE,
    ("POST", _SRC): _NEW,
    ("POST", _SRC + "/apply"): _NEW,
    ("POST", _SRC + "/cancel"): _CANCEL,
    ("POST", _SRC + "/dismiss"): NotNarrowed("只丢弃一份待应用的提案,可再生成"),
    # --------------------------------------------------------------------- kg
    ("PATCH", _NB + "/indexing-pipeline"): NotNarrowed("配置,可重设"),
    ("POST", _NB + "/kg/build"): _REBUILDABLE,
    ("POST", _NB + "/kg/rebuild"): _REBUILDABLE,
    ("POST", _NB + "/kg/relink"): _REBUILDABLE,
    ("POST", _NB + "/kg/delete"): _REBUILDABLE,
    ("POST", _NB + "/unified-kg/rebuild"): _REBUILDABLE,
    ("POST", _NB + "/kg/conflicts/resolve"): _REBUILDABLE,
    ("POST", _NB + "/kg/conflicts/{candidate_id}/confirm"): _REVIEW,
    ("POST", _NB + "/kg/conflicts/{candidate_id}/reject"): _REVIEW,
    ("POST", _NB + "/unified-kg/merges/review"): _REVIEW,
    ("POST", _NB + "/unified-kg/merges/review-all"): _REVIEW,
    ("POST", _NB + "/unified-kg/merges/{candidate_id}/confirm"): _REVIEW,
    ("POST", _NB + "/unified-kg/merges/{candidate_id}/reject"): _REVIEW,
    ("PUT", _NB + "/kg-analysis/findings/{rule_id}/{subject_key:path}/dismissal"): (
        NotNarrowed("忽略一条分析发现,可撤销")
    ),
    ("DELETE", _NB + "/kg-analysis/findings/{rule_id}/{subject_key:path}/dismissal"): (
        NotNarrowed("撤销忽略")
    ),
    ("POST", _NB + "/scale-index/rebuild"): _REBUILDABLE,
    ("POST", _NB + "/scale-index/cancel"): _CANCEL,
    # -------------------------------------------------------------- knowledge
    ("POST", _NB + "/object-schemas"): _NEW,
    ("PATCH", _NB + "/object-schemas/{object_type}"): _NEW,
    ("DELETE", _NB + "/object-schemas/{object_type}"): NotNarrowed("配置,可重设"),
    ("POST", _NB + "/schema-proposals"): _NEW,
    ("POST", _NB + "/relations/{rel_id}/review"): _REVIEW,
    ("POST", _NB + "/knowledge/{knowledge_id}/promote"): NotNarrowed(
        "只提交收录申请,由系统管理员审批"
    ),
    ("PATCH", _NB + "/knowledge/{knowledge_id}"): _KNOWLEDGE,
    ("POST", _NB + "/knowledge/{knowledge_id}/merge"): _KNOWLEDGE,
    # ---------------------------------------------------------- understanding
    ("PUT", _NB + "/understanding/{label}"): _NEW,
    ("DELETE", _NB + "/understanding/{label}"): NotNarrowed("可重建"),
    ("POST", _NB + "/understanding/rebuild"): NotNarrowed("可重建"),
    ("POST", _NB + "/understanding/experiences/distill"): _REBUILDABLE,
    ("DELETE", _NB + "/understanding/experiences"): NotNarrowed("可再蒸馏"),
    # ---------------------------------------------------------------- knowhow
    ("POST", _NB + "/knowhow"): _NEW,
    ("POST", _NB + "/knowhow/import"): _NEW,
    ("POST", _NB + "/knowhow/import/preview"): NotNarrowed("只预览,不写"),
    ("DELETE", _KH): AdminRequired(),
    ("POST", _KH + "/history/prune"): AdminRequired(),
    ("POST", _KH + "/transfer"): AdminRequired(),  # the move half; copy is not narrowed
    ("PATCH", _KH): _HISTORY,
    ("POST", _KH + "/append"): _HISTORY,
    ("PATCH", _KH + "/cells"): _HISTORY,
    ("POST", _KH + "/columns"): _HISTORY,
    ("PATCH", _KH + "/columns/{column_id}"): _HISTORY,
    ("DELETE", _KH + "/columns/{column_id}"): _HISTORY,
    ("POST", _KH + "/rows"): _HISTORY,
    ("DELETE", _KH + "/rows/{row_id}"): _HISTORY,
    ("POST", _KH + "/revert"): NotNarrowed("回退也记历史"),
    ("POST", _KH + "/milestones"): _NEW,
    ("DELETE", _KH + "/milestones/{milestone_id}"): NotNarrowed("命名标记,流水仍在"),
    ("POST", _KH + "/reproject"): _REBUILDABLE,
    ("POST", _KH + "/rows/{row_id}/complete"): _HISTORY,
    ("POST", _KH + "/rows/{row_id}/complete/stream"): _HISTORY,
    ("PATCH", _CELL): _HISTORY,
    ("POST", _CELL + "/optimize"): _HISTORY,
    ("POST", _CELL + "/optimize/stream"): _HISTORY,
    ("POST", _CELL + "/reformat"): _HISTORY,
    ("POST", _CELL + "/reformat/stream"): _HISTORY,
    ("PUT", _CODE): _HISTORY,
    ("DELETE", _CODE): _HISTORY,
}


#: ``AdminRequired`` routes that refuse an editor in the body rather than
#: through the ``_refuse_named_editor`` dependency: the transfer, whose ``mode``
#: decides (only ``move`` is narrowed, ``copy`` is not). Every other
#: ``AdminRequired`` route must carry the dependency
#: (``test_every_admin_required_route_refuses_a_named_editor``).
ADMIN_REQUIRED_IN_BODY: frozenset[tuple[str, str]] = frozenset({
    ("POST", _KH + "/transfer"),
})
