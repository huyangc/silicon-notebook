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

from app.domain.auth_utils import ascii_lower


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
    # ``(source username, target username)`` for every entry of ``matched``
    # that only matched once ASCII letter case was ignored, sorted. Reported,
    # because an operator should be able to see which accounts were joined
    # on a spelling that was not identical.
    case_folded: tuple[tuple[str, str], ...] = ()


def build_user_mapping(
    source_users: Iterable[UserProjection],
    target_users: Iterable[UserProjection],
) -> UserMapping:
    """Match source users to target users by ``username``, no whitespace
    stripping, in two passes (user ruling 2026-10-19, design doc §11):

    1. **Exact.** A duplicate username on either side is ambiguous and raises
       ``ValueError`` naming it -- silently picking one match would hide a
       data problem the caller must fix upstream.
    2. **ASCII case-insensitive, for what pass 1 left unmatched.** The two
       auth services may spell one person's username with different letter
       case. Folding is ``ascii_lower`` -- the same fold as the
       ``lower(username)`` unique index both backends keep -- so ``Ä`` and
       ``ä`` stay different, exactly as the database sees them. A fold that
       names more than one target user, or a target user another source user
       already resolved to, is ambiguous: the whole call raises
       ``ValueError`` listing every such pair, because joining two people
       onto one account would hand one person's content to the other.

    A source user with an empty username goes straight to ``unmatched``."""
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
    target_by_folded: dict[str, list[UserProjection]] = {}
    for user in target_list:
        if not user.username:
            continue
        if user.username in target_by_username:
            raise ValueError(
                f"duplicate target username: {user.username!r}"
            )
        target_by_username[user.username] = user.id
        target_by_folded.setdefault(ascii_lower(user.username), []).append(user)

    matched: dict[str, str] = {}
    leftover: list[UserProjection] = []
    unmatched: list[UserProjection] = []
    for user in source_list:
        if not user.username:
            unmatched.append(user)
            continue
        target_id = target_by_username.get(user.username)
        if target_id is None:
            leftover.append(user)
        else:
            matched[user.id] = target_id

    claimed = {target_id: source_id for source_id, target_id in matched.items()}
    usernames = {user.id: user.username for user in source_list}
    case_folded: list[tuple[str, str]] = []
    problems: list[str] = []
    for user in leftover:
        candidates = target_by_folded.get(ascii_lower(user.username), [])
        if not candidates:
            unmatched.append(user)
            continue
        if len(candidates) > 1:
            names = "、".join(sorted(candidate.username for candidate in candidates))
            problems.append(
                f"源 {user.username} 忽略大小写对应多个目标用户 {names}"
            )
            continue
        candidate = candidates[0]
        other = claimed.get(candidate.id)
        if other is not None:
            problems.append(
                f"源 {user.username} 与源 {usernames[other]} 忽略大小写会落到"
                f"同一个目标用户 {candidate.username}"
            )
            continue
        claimed[candidate.id] = user.id
        matched[user.id] = candidate.id
        case_folded.append((user.username, candidate.username))
    if problems:
        raise ValueError(
            "usernames are ambiguous once ASCII letter case is ignored, rename "
            "one side: " + "；".join(sorted(problems))
        )

    unmatched.sort(key=lambda user: user.username)
    return UserMapping(
        matched=MappingProxyType(matched),
        unmatched=tuple(unmatched),
        case_folded=tuple(sorted(case_folded)),
    )
