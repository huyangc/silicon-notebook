"""The expected access level of every notebook write route (share-edit P1-T3).

``ROUTE_LEVELS`` maps every ``(method, path)`` of
``notebook_route_inventory.unsafe_notebook_routes`` to what it requires, and
``test_route_levels_cover_inventory_exactly`` asserts the key set *equals* the
census: a new route without an entry fails, and so does a stale entry.

Three kinds, written out explicitly (the capability name, not just a tier: a
capability whose mirror fence is False gets the shared bare tier guard from the
factory, so a route's dependency reveals only the tier, never the name --
``deps._CAPABILITY_LEVELS``'s identity note):

* ``Capability(name)``: the route is guarded by that capability, by a
  dependency or (the body-lookup routes) by the same table in its body;
* ``ReaderBody(capability, reader_branch)``: only the read guard hangs on the
  route; the body judges ``capability`` for its write branch, and a reader
  without it reaches at most ``reader_branch`` (empty: no reader branch at all)
  -- every other request answers the same 404 as a missing notebook;
* ``ReaderOnly(reason)``: any reader may write their own things here (asking,
  their own reports / conversations / Memory); the read guard hangs on the
  route and a stranger is refused with 404;
* ``SelfAuthorized(axis)``: no notebook guard at all -- the route authorizes on
  another axis (the caller's own membership row, her own pending share
  request, her group-admin role). A stranger is refused with 404 unless the
  entry says ``stranger_refused=False`` (an idempotent self-scoped no-op).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Capability:
    name: str


@dataclass(frozen=True)
class ReaderBody:
    capability: str
    reader_branch: str
    #: True only when even the read half is judged in the body: the Knowhow
    #: transfer (``mode`` picks read or knowhow:write for the source, so no
    #: static guard fits) and the named-share DELETE (removing yourself is the
    #: idempotent exit, answered without any notebook guard like the membership
    #: exit). Every other ReaderBody route carries the read guard.
    read_in_body: bool = False


@dataclass(frozen=True)
class ReaderOnly:
    reason: str


@dataclass(frozen=True)
class SelfAuthorized:
    axis: str
    #: False only for a self-scoped idempotent route whose documented answer to
    #: a caller with nothing there is a no-op success (leaving a share one is
    #: not in): it touches nothing but the caller's own rows, so there is no
    #: existence to hide.
    stranger_refused: bool = True


RouteLevel = Union[Capability, ReaderBody, ReaderOnly, SelfAuthorized]

_NB = "/api/notebooks/{notebook_id}"
_KH = _NB + "/knowhow/{table_id}"
_CELL = _KH + "/rows/{row_id}/cells/{column_id}"
_SRC = _NB + "/sources/{source_id}/command-catalog"

_ASK = ReaderOnly("提问:任何读者都可以问")
_OWN_REPORT = ReaderOnly("深度报告按创建者隔离(行级 created_by)")
_OWN_CONVERSATION = ReaderOnly("本人的对话与对话分享")
_OWN_MEMORY = ReaderOnly("本人的 Memory")
_UNDERSTANDING = ReaderBody("agent_profile:write", "scope=mine:本人的理解覆盖层")
_EXPERIENCES = ReaderBody("agent_profile:write", "")

ROUTE_LEVELS: dict[tuple[str, str], RouteLevel] = {
    # ---------------------------------------------------------------- notebook
    ("DELETE", _NB): Capability("notebook:delete"),
    ("POST", _NB + "/cancel-delete"): Capability("notebook:delete"),
    ("PATCH", _NB): Capability("notebook:manage"),
    ("POST", _NB + "/tier"): Capability("notebook:manage"),
    ("PUT", _NB + "/bases"): Capability("notebook:mount"),
    ("POST", _NB + "/share"): Capability("notebook:configure"),
    ("DELETE", _NB + "/share"): Capability("notebook:configure"),
    ("POST", _NB + "/export"): Capability("notebook:export"),
    ("POST", _NB + "/grants"): Capability("notebook:grant"),
    ("DELETE", _NB + "/grants/{grant_id}"): Capability("notebook:grant"),
    ("POST", _NB + "/share-requests"): Capability("notebook:grant"),
    # 点名共享(分享编辑 P1-T6):加编辑者恒 owner;DELETE 不挂路由守卫、体内按「删谁」
    # 分流——删别人要 notebook:collaborate,删自己(user_id 是调用者)是本人退出协作,
    # 与 DELETE .../membership 同一个退出合同(幂等:没有点名边也是 204)。
    ("PUT", _NB + "/collaborators/{user_id}"): Capability("notebook:collaborate"),
    ("DELETE", _NB + "/collaborators/{user_id}"): ReaderBody(
        "notebook:collaborate",
        "user_id 是调用者自己:本人退出协作(幂等,没有点名边时 204)",
        read_in_body=True,
    ),
    ("DELETE", _NB + "/share-requests/{request_id}"): SelfAuthorized(
        "申请人撤回本人的待审批申请:授权轴是申请归属(store 按 requested_by 兜底)"
    ),
    ("DELETE", "/api/groups/{group_id}/shared-notebooks/{notebook_id}"): SelfAuthorized(
        "组管理员撤回指向本组的边:授权轴是组管理员身份,不是这本库的能力"
    ),
    ("DELETE", _NB + "/membership"): SelfAuthorized(
        "本人退出只读共享:幂等,本来就不在也是 204(notebook_routes 的退出合同)",
        stranger_refused=False,
    ),
    ("POST", _NB + "/question-suggestions"): _ASK,
    ("POST", _NB + "/pulse"): ReaderOnly("协作脉搏:任意读者"),
    # ------------------------------------------------------------------- ask
    ("POST", _NB + "/ask"): _ASK,
    ("POST", _NB + "/ask/stream"): _ASK,
    ("POST", _NB + "/ask/intent"): _ASK,
    ("POST", _NB + "/ask/intent/stream"): _ASK,
    ("POST", _NB + "/ask/report-check"): _ASK,
    ("POST", _NB + "/ask/jobs/{job_id}/cancel"): _ASK,
    ("DELETE", _NB + "/conversations"): _OWN_CONVERSATION,
    ("POST", _NB + "/conversations/{conversation_id}/share"): _OWN_CONVERSATION,
    ("DELETE", _NB + "/conversations/{conversation_id}/share"): _OWN_CONVERSATION,
    # ---------------------------------------------------------------- reports
    ("POST", _NB + "/reports"): _OWN_REPORT,
    ("POST", _NB + "/reports/export"): _OWN_REPORT,
    ("DELETE", _NB + "/reports/{report_id}"): _OWN_REPORT,
    ("POST", _NB + "/reports/{report_id}/cancel"): _OWN_REPORT,
    ("POST", _NB + "/reports/{report_id}/generate"): _OWN_REPORT,
    ("POST", _NB + "/reports/{report_id}/intent"): _OWN_REPORT,
    ("PATCH", _NB + "/reports/{report_id}/outline"): _OWN_REPORT,
    ("POST", _NB + "/reports/{report_id}/share"): _OWN_REPORT,
    ("DELETE", _NB + "/reports/{report_id}/share"): _OWN_REPORT,
    # ----------------------------------------------------------------- memory
    ("POST", _NB + "/answer-memory-links"): _OWN_MEMORY,
    ("POST", _NB + "/memories/from-answer"): _OWN_MEMORY,
    ("POST", _NB + "/memories/transfer"): _OWN_MEMORY,
    ("DELETE", _NB + "/agent-observations"): ReaderOnly("本人的 Agent 记录"),
    # ---------------------------------------------------------- understanding
    ("PUT", _NB + "/understanding/{label}"): _UNDERSTANDING,
    ("DELETE", _NB + "/understanding/{label}"): _UNDERSTANDING,
    ("POST", _NB + "/understanding/rebuild"): _UNDERSTANDING,
    ("POST", _NB + "/understanding/experiences/distill"): _EXPERIENCES,
    ("DELETE", _NB + "/understanding/experiences"): _EXPERIENCES,
    # ---------------------------------------------------------------- sources
    ("POST", _NB + "/sources"): Capability("sources:write"),
    ("POST", _NB + "/sources/import"): Capability("sources:write"),
    ("POST", _NB + "/sources/url"): Capability("sources:write"),
    ("POST", _NB + "/sources/reparse"): Capability("sources:write"),
    ("POST", _NB + "/assets"): Capability("sources:write"),
    ("POST", _NB + "/backfill-vectors"): Capability("sources:write"),
    ("POST", _NB + "/paper-meta/backfill"): Capability("sources:write"),
    ("DELETE", "/api/sources/{source_id}"): Capability("sources:write"),
    ("POST", "/api/sources/{source_id}/parse"): Capability("sources:write"),
    # ---------------------------------------------------------------- catalog
    ("POST", _SRC): Capability("catalog:write"),
    ("POST", _SRC + "/apply"): Capability("catalog:write"),
    ("POST", _SRC + "/cancel"): Capability("catalog:write"),
    ("POST", _SRC + "/dismiss"): Capability("catalog:write"),
    # --------------------------------------------------------------------- kg
    ("PATCH", _NB + "/indexing-pipeline"): Capability("kg:write"),
    ("POST", _NB + "/kg/build"): Capability("kg:write"),
    ("POST", _NB + "/kg/rebuild"): Capability("kg:write"),
    ("POST", _NB + "/kg/relink"): Capability("kg:write"),
    ("POST", _NB + "/kg/delete"): Capability("kg:write"),
    ("POST", _NB + "/kg/conflicts/resolve"): Capability("kg:write"),
    ("POST", _NB + "/kg/conflicts/{candidate_id}/confirm"): Capability("kg:write"),
    ("POST", _NB + "/kg/conflicts/{candidate_id}/reject"): Capability("kg:write"),
    ("POST", _NB + "/unified-kg/rebuild"): Capability("kg:write"),
    ("POST", _NB + "/unified-kg/merges/review"): Capability("kg:write"),
    ("POST", _NB + "/unified-kg/merges/review-all"): Capability("kg:write"),
    ("POST", _NB + "/unified-kg/merges/{candidate_id}/confirm"): Capability("kg:write"),
    ("POST", _NB + "/unified-kg/merges/{candidate_id}/reject"): Capability("kg:write"),
    (
        "PUT", _NB + "/kg-analysis/findings/{rule_id}/{subject_key:path}/dismissal",
    ): Capability("kg:write"),
    (
        "DELETE", _NB + "/kg-analysis/findings/{rule_id}/{subject_key:path}/dismissal",
    ): Capability("kg:write"),
    ("POST", _NB + "/scale-index/rebuild"): Capability("scale_index:write"),
    ("POST", _NB + "/scale-index/cancel"): Capability("scale_index:write"),
    # -------------------------------------------------------------- knowledge
    ("POST", _NB + "/object-schemas"): Capability("knowledge:write"),
    ("PATCH", _NB + "/object-schemas/{object_type}"): Capability("knowledge:write"),
    ("DELETE", _NB + "/object-schemas/{object_type}"): Capability("knowledge:write"),
    ("POST", _NB + "/schema-proposals"): Capability("knowledge:write"),
    ("POST", _NB + "/relations/{rel_id}/review"): Capability("knowledge:write"),
    ("POST", _NB + "/knowledge/{knowledge_id}/promote"): Capability("knowledge:write"),
    ("PATCH", _NB + "/knowledge/{knowledge_id}"): ReaderBody(
        "knowledge:write", "own_status:本人 Memory 对象在 approved/deprecated 间切换"
    ),
    ("POST", _NB + "/knowledge/{knowledge_id}/merge"): ReaderBody(
        "knowledge:write", "own_pair:合并本人 Memory 支撑的两个 approved 对象"
    ),
    # ---------------------------------------------------------------- knowhow
    ("POST", _NB + "/knowhow"): Capability("knowhow:write"),
    ("POST", _NB + "/knowhow/import"): Capability("knowhow:write"),
    ("POST", _NB + "/knowhow/import/preview"): Capability("knowhow:write"),
    ("PATCH", _KH): Capability("knowhow:write"),
    ("DELETE", _KH): Capability("knowhow:write"),
    ("POST", _KH + "/append"): Capability("knowhow:write"),
    ("PATCH", _KH + "/cells"): Capability("knowhow:write"),
    ("POST", _KH + "/columns"): Capability("knowhow:write"),
    ("PATCH", _KH + "/columns/{column_id}"): Capability("knowhow:write"),
    ("DELETE", _KH + "/columns/{column_id}"): Capability("knowhow:write"),
    ("POST", _KH + "/history/prune"): Capability("knowhow:write"),
    ("POST", _KH + "/milestones"): Capability("knowhow:write"),
    ("DELETE", _KH + "/milestones/{milestone_id}"): Capability("knowhow:write"),
    ("POST", _KH + "/reproject"): Capability("knowhow:write"),
    ("POST", _KH + "/revert"): Capability("knowhow:write"),
    ("POST", _KH + "/rows"): Capability("knowhow:write"),
    ("DELETE", _KH + "/rows/{row_id}"): Capability("knowhow:write"),
    ("POST", _KH + "/rows/{row_id}/complete"): Capability("knowhow:write"),
    ("POST", _KH + "/rows/{row_id}/complete/stream"): Capability("knowhow:write"),
    ("PATCH", _CELL): Capability("knowhow:write"),
    ("POST", _CELL + "/optimize"): Capability("knowhow:write"),
    ("POST", _CELL + "/optimize/stream"): Capability("knowhow:write"),
    ("POST", _CELL + "/reformat"): Capability("knowhow:write"),
    ("POST", _CELL + "/reformat/stream"): Capability("knowhow:write"),
    ("POST", _KH + "/transfer"): ReaderBody(
        "knowhow:write", "copy:源库读权 + 目标库 knowhow:write;move 要源库 knowhow:write",
        read_in_body=True,
    ),
    ("PUT", "/api/agent/knowhow/rows/{row_id}/cells/{column_id}/code"): Capability(
        "knowhow:write"
    ),
    ("DELETE", "/api/agent/knowhow/rows/{row_id}/cells/{column_id}/code"): Capability(
        "knowhow:write"
    ),
}
