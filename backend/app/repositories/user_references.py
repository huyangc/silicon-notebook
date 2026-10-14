"""Every column that stores a ``users.id``, and what deleting that user means for it.

The administrator account merge (``AuthStore.merge_sso_account``) physically
deletes an account, and is allowed only while nothing but that account's own
scaffolding refers to it. This registry is the one list it reads. A guard test
enumerates the migrated schema on both backends (every foreign key to
``users`` plus every column whose name looks like a user reference) and fails
on any column missing from both ``USER_REFERENCE_COLUMNS`` and
``NOT_USER_REFERENCES``: a new column must be classified here when it is added.

Kinds:

- ``scaffolding``: belongs to the account itself and is deleted with it;
- ``audit_kept``: identity audit, kept with a dangling id (no foreign key);
- ``blocking``: anything else; one row refuses the merge.
"""
from __future__ import annotations

from typing import Literal, NamedTuple

ReferenceKind = Literal["scaffolding", "audit_kept", "blocking"]


class UserReference(NamedTuple):
    table: str
    column: str
    kind: ReferenceKind
    # Administrator-facing category; blocking rows are summed per label.
    label: str = ""
    # Extra SQL predicate (a module constant, never user input), e.g. limiting
    # a polymorphic principal column to its user principals.
    extra_where: str = ""


# Display order and counting unit of each blocking category.
REFERENCE_LABEL_UNITS: dict[str, str] = {
    "笔记本": "个",
    "来源": "份",
    "提问记录": "条",
    "全局问答": "条",
    "报告": "份",
    "知识图谱": "条",
    "Knowhow 表": "条",
    "记忆": "条",
    "笔记本共享与成员": "条",
    "群组": "条",
    "Agent 接入": "条",
    "许愿墙": "条",
    "插件设置": "条",
    "历史活动记录": "条",
}


def _blocking(table: str, column: str, label: str, extra_where: str = "") -> UserReference:
    return UserReference(table, column, "blocking", label, extra_where)


USER_REFERENCE_COLUMNS: tuple[UserReference, ...] = (
    UserReference("auth_sessions", "user_id", "scaffolding"),
    UserReference("user_profiles", "user_id", "scaffolding"),
    UserReference("model_service_status", "user_id", "scaffolding"),
    UserReference("ask_intent_handles", "owner_id", "scaffolding"),
    UserReference("auth_identity_audit", "actor_id", "audit_kept"),
    UserReference("auth_identity_audit", "target_user_id", "audit_kept"),
    _blocking("notebooks", "created_by", "笔记本"),
    _blocking("notebook_bases", "created_by", "笔记本"),
    _blocking("notebook_assets", "created_by", "笔记本"),
    _blocking("notebook_object_schemas", "created_by", "知识图谱"),
    _blocking("sources", "uploaded_by", "来源"),
    _blocking("catalog_jobs", "created_by", "来源"),
    _blocking("ask_jobs", "created_by", "提问记录"),
    _blocking("conversations", "created_by", "提问记录"),
    _blocking("global_ask_conversations", "user_id", "全局问答"),
    _blocking("global_ask_jobs", "user_id", "全局问答"),
    _blocking("reports", "created_by", "报告"),
    _blocking("kg_build_jobs", "created_by", "知识图谱"),
    _blocking("concept_merge_candidates", "reviewed_by", "知识图谱"),
    _blocking("knowhow_tables", "created_by", "Knowhow 表"),
    _blocking("knowhow_changes", "actor", "Knowhow 表"),
    _blocking("knowhow_cell_code", "updated_by", "Knowhow 表"),
    _blocking("knowhow_milestones", "created_by", "Knowhow 表"),
    # memory_items.created_by cascades on delete: checked here, never cascaded.
    _blocking("memory_items", "created_by", "记忆"),
    _blocking("memory_items", "confirmed_by", "记忆"),
    _blocking("memory_revisions", "changed_by", "记忆"),
    _blocking("promotion_candidates", "reviewed_by", "记忆"),
    _blocking("notebook_members", "user_id", "笔记本共享与成员"),
    # principal_id names a user or a group depending on principal_type.
    _blocking("notebook_grants", "principal_id", "笔记本共享与成员", "principal_type='user'"),
    _blocking("notebook_grants", "created_by", "笔记本共享与成员"),
    _blocking("notebook_share_requests", "requested_by", "笔记本共享与成员"),
    _blocking("notebook_share_requests", "decided_by", "笔记本共享与成员"),
    _blocking("groups", "created_by", "群组"),
    _blocking("groups", "owner_id", "群组"),
    _blocking("groups", "invite_created_by", "群组"),
    _blocking("group_members", "user_id", "群组"),
    _blocking("group_members", "added_by", "群组"),
    # agent_profiles.owner_id cascades on delete: checked here, never cascaded.
    _blocking("agent_profiles", "owner_id", "Agent 接入"),
    _blocking("agent_profile_jobs", "owner_id", "Agent 接入"),
    _blocking("agent_observations", "owner_id", "Agent 接入"),
    _blocking("agent_notebook_profile", "owner_id", "Agent 接入"),
    _blocking("agent_notebook_profile", "updated_by", "Agent 接入"),
    _blocking("wishes", "author_id", "许愿墙"),
    _blocking("wish_votes", "user_id", "许愿墙"),
    _blocking("extension_runtime_toggles", "updated_by", "插件设置"),
    _blocking("retained_user_activity", "actor_id", "历史活动记录"),
    _blocking("retained_user_activity", "notebook_owner_id", "历史活动记录"),
)


# Columns whose names look like user references but do not hold a users.id.
NOT_USER_REFERENCES: dict[tuple[str, str], str] = {
    ("knowledge_objects", "owner"): "free-text governance owner taken from extracted content",
    ("notebooks", "target_users"): "free-text description of the intended audience",
}
