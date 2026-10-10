"""System-update notice: read the build's release manifest and decide which
notes a user still has to be shown.

The manifest (``release-manifest.json`` at the package/repo root, written by
``scripts/build_release_manifest.py`` at pack time) carries the build's mainline
ordinal and every hand-written release note with the ordinal of the mainline
commit that introduced it. A user's baseline is one integer
(``users.seen_release_ordinal``); pending notes are the ones with
``seen < note.ordinal <= build.ordinal`` that the user may see (``audience``).
Each note carries a ``level``: only ``HEADLINE_LEVELS`` notes are shown in the
dialog (at most ``HEADLINE_LIMIT``); the rest are only counted or live in the
history list.

Invalid manifests are "unavailable", never an error to the caller: the reason
*code* is logged (once per file version), never the note bodies.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.models.system import (
    ReleaseNoteItem,
    ReleaseNotesBuild,
    ReleaseNotesHistoryResponse,
    ReleaseNotesResponse,
)
from app.repositories.ports import IdentityRepository

logger = logging.getLogger("silicon_notebook.release_notes")

MANIFEST_FILENAME = "release-manifest.json"
MANIFEST_SCHEMA = 2

LEVELS = ("feature", "change", "fix", "internal")
AUDIENCES = ("all", "admin")
# Levels shown as dialog headlines (a set, not a priority: headlines sort newest first).
HEADLINE_LEVELS = ("change", "feature")
HEADLINE_LIMIT = 5

# services/ -> app/ -> backend/ -> package root: the same root
# ``app.core.config`` resolves ``.env`` and relative storage paths against.
_ROOT_DIR = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ReleaseNote:
    id: str
    ordinal: int
    level: str
    audience: str
    title: str
    body: str


@dataclass(frozen=True)
class ReleaseBuild:
    version: str
    ordinal: int


@dataclass(frozen=True)
class ReleaseManifest:
    build: ReleaseBuild
    notes: tuple[ReleaseNote, ...]


class ManifestInvalid(ValueError):
    """The manifest is unusable; ``code`` is a stable, body-free reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def manifest_path() -> Path:
    return _ROOT_DIR / MANIFEST_FILENAME


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_release_manifest(raw: object) -> ReleaseManifest:
    """Validate a decoded manifest; raise ``ManifestInvalid`` with a code."""
    if not isinstance(raw, dict):
        raise ManifestInvalid("not_an_object")
    if not _is_int(raw.get("schema")) or raw["schema"] != MANIFEST_SCHEMA:
        raise ManifestInvalid("unsupported_schema")
    build = raw.get("build")
    if not isinstance(build, dict):
        raise ManifestInvalid("bad_build")
    version, ordinal = build.get("version"), build.get("ordinal")
    if not isinstance(version, str) or not version:
        raise ManifestInvalid("bad_build")
    if not _is_int(ordinal) or ordinal < 0:
        raise ManifestInvalid("bad_build")
    raw_notes = raw.get("notes")
    if not isinstance(raw_notes, list):
        raise ManifestInvalid("bad_notes")
    notes: list[ReleaseNote] = []
    seen_ids: set[str] = set()
    for entry in raw_notes:
        if not isinstance(entry, dict):
            raise ManifestInvalid("bad_note")
        note_id, note_ordinal, body = entry.get("id"), entry.get("ordinal"), entry.get("body")
        level, audience, title = entry.get("level"), entry.get("audience"), entry.get("title")
        if not isinstance(note_id, str) or not note_id:
            raise ManifestInvalid("bad_note")
        if not _is_int(note_ordinal) or note_ordinal < 0:
            raise ManifestInvalid("bad_note")
        if level not in LEVELS or audience not in AUDIENCES:
            raise ManifestInvalid("bad_note")
        if not isinstance(title, str) or not title.strip():
            raise ManifestInvalid("bad_note")
        if not isinstance(body, str):  # an empty body is fine
            raise ManifestInvalid("bad_note")
        if note_id in seen_ids:
            raise ManifestInvalid("duplicate_note")
        seen_ids.add(note_id)
        notes.append(ReleaseNote(note_id, note_ordinal, level, audience, title, body))
    return ReleaseManifest(ReleaseBuild(version, ordinal), tuple(notes))


# path -> ((inode, mtime_ns, size), manifest-or-None). The None result is cached too
# so a broken file logs once per file version instead of once per request.
_cache: dict[Path, tuple[tuple[int, int, int], ReleaseManifest | None]] = {}


def load_release_manifest(path: Path | None = None) -> ReleaseManifest | None:
    """Return the parsed manifest, or None when absent/unreadable/invalid."""
    target = path if path is not None else manifest_path()
    try:
        stat = target.stat()
    except OSError:
        _cache.pop(target, None)
        return None
    stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(target)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    manifest: ReleaseManifest | None
    try:
        manifest = parse_release_manifest(
            json.loads(target.read_text(encoding="utf-8"))
        )
    except ManifestInvalid as exc:
        logger.warning("release manifest unavailable: reason=%s", exc.code)
        manifest = None
    except (OSError, ValueError) as exc:
        # Only the exception class: a decode error message can quote content.
        logger.warning(
            "release manifest unavailable: reason=unreadable error=%s",
            type(exc).__name__,
        )
        manifest = None
    _cache[target] = (stamp, manifest)
    return manifest


def _visible(note: ReleaseNote, is_admin: bool) -> bool:
    return note.audience == "all" or (note.audience == "admin" and is_admin)


def _item(note: ReleaseNote) -> ReleaseNoteItem:
    return ReleaseNoteItem(
        id=note.id, ordinal=note.ordinal, level=note.level,
        audience=note.audience, title=note.title, body=note.body,
    )


def pending_release_notes(
    seen: int, build_ordinal: int, notes: Sequence[ReleaseNote], *, is_admin: bool
) -> list[ReleaseNote]:
    """Visible notes with ``seen < ordinal <= build_ordinal``, newest first.

    Ties on ordinal keep a stable order by id ascending. A rollback
    (``build_ordinal < seen``) yields nothing by construction.
    """
    pending = [
        n for n in notes
        if seen < n.ordinal <= build_ordinal and _visible(n, is_admin)
    ]
    pending.sort(key=lambda n: (-n.ordinal, n.id))
    return pending


def release_notes_for_user(
    identity: IdentityRepository,
    user_id: str,
    manifest: ReleaseManifest | None,
    *,
    is_admin: bool,
) -> ReleaseNotesResponse:
    """GET semantics: unavailable -> no write; NULL baseline -> initialize it
    silently and show nothing; otherwise the headline notes (<= HEADLINE_LIMIT,
    ``change`` before ``feature``, newest first) plus ``more_count`` = headline
    overflow + pending ``fix`` notes. ``internal`` notes are never counted. With no
    headline note ``notes`` is empty, so the client neither pops up nor advances
    the baseline: fixes wait for the next headline release on purpose."""
    if manifest is None:
        return ReleaseNotesResponse(available=False, build=None, notes=[], more_count=0)
    build = ReleaseNotesBuild(
        version=manifest.build.version, ordinal=manifest.build.ordinal
    )
    seen = identity.get_seen_release_ordinal(user_id)
    if seen is None:
        identity.initialize_seen_release_ordinal(user_id, manifest.build.ordinal)
        return ReleaseNotesResponse(available=True, build=build, notes=[], more_count=0)
    pending = pending_release_notes(
        seen, manifest.build.ordinal, manifest.notes, is_admin=is_admin
    )
    headline = [n for n in pending if n.level in HEADLINE_LEVELS]
    headline.sort(key=lambda n: (-n.ordinal, n.id))
    fixes = sum(1 for n in pending if n.level == "fix")
    return ReleaseNotesResponse(
        available=True,
        build=build,
        notes=[_item(n) for n in headline[:HEADLINE_LIMIT]],
        more_count=max(0, len(headline) - HEADLINE_LIMIT) + fixes,
    )


def release_notes_history(
    manifest: ReleaseManifest | None, *, is_admin: bool
) -> ReleaseNotesHistoryResponse:
    """Every visible note up to the running build (internal included), newest
    first. Reads and writes no per-user state."""
    if manifest is None:
        return ReleaseNotesHistoryResponse(available=False, build=None, notes=[])
    visible = [
        n for n in manifest.notes
        if n.ordinal <= manifest.build.ordinal and _visible(n, is_admin)
    ]
    visible.sort(key=lambda n: (-n.ordinal, n.id))
    return ReleaseNotesHistoryResponse(
        available=True,
        build=ReleaseNotesBuild(
            version=manifest.build.version, ordinal=manifest.build.ordinal
        ),
        notes=[_item(n) for n in visible],
    )


def mark_release_notes_seen(
    identity: IdentityRepository,
    user_id: str,
    manifest: ReleaseManifest | None,
    through_ordinal: int,
) -> None:
    """POST semantics: never above the running build, never decreasing."""
    if manifest is None:
        return
    identity.advance_seen_release_ordinal(
        user_id, min(through_ordinal, manifest.build.ordinal)
    )
