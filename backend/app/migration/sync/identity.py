"""Pure username-based identity mapping for cross-environment sync
(docs/incremental-sync-design.md §4).

Both environments run separate auth services, so a user's id and email differ
between them; ``username`` is the only reliable join key. This module has no
I/O and does not import any repository -- callers gather the two user
projections (source export, target live query) and pass them in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class UserProjection:
    id: str
    username: str
    display_name: str
    role: str


@dataclass(frozen=True)
class UserMapping:
    # source user id -> target user id. A real (read-only) Mapping, not a
    # plain dict -- frozen=True only blocks reassigning the attribute, it
    # does not stop in-place mutation of a mutable dict stored in it.
    # build_user_mapping wraps the dict it builds in MappingProxyType so the
    # frozen dataclass's immutability is not just skin deep.
    matched: Mapping[str, str]
    # source users with no target match, sorted by username
    unmatched: tuple[UserProjection, ...]


def build_user_mapping(
    source_users: Iterable[UserProjection],
    target_users: Iterable[UserProjection],
) -> UserMapping:
    """Match source users to target users by exact ``username`` (case
    sensitive, no whitespace stripping). A source user with an empty
    username goes straight to ``unmatched``. A duplicate username on either
    side is ambiguous and raises ``ValueError`` naming the username --
    silently picking one match would hide a data problem the caller must fix
    upstream."""
    source_list = list(source_users)
    target_list = list(target_users)

    source_seen: dict[str, str] = {}
    for user in source_list:
        if not user.username:
            continue
        if user.username in source_seen:
            raise ValueError(
                f"duplicate source username: {user.username!r}"
            )
        source_seen[user.username] = user.id

    target_by_username: dict[str, str] = {}
    for user in target_list:
        if not user.username:
            continue
        if user.username in target_by_username:
            raise ValueError(
                f"duplicate target username: {user.username!r}"
            )
        target_by_username[user.username] = user.id

    matched: dict[str, str] = {}
    unmatched: list[UserProjection] = []
    for user in source_list:
        if not user.username:
            unmatched.append(user)
            continue
        target_id = target_by_username.get(user.username)
        if target_id is None:
            unmatched.append(user)
        else:
            matched[user.id] = target_id

    unmatched.sort(key=lambda user: user.username)
    return UserMapping(matched=MappingProxyType(matched), unmatched=tuple(unmatched))
