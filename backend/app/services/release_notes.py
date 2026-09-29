"""System-update notice: read the build's release manifest and decide which
notes a user still has to be shown.

The manifest (``release-manifest.json`` at the package/repo root, written by
``scripts/build_release_manifest.py`` at pack time) carries the build's mainline
ordinal and every hand-written release note with the ordinal of the mainline
commit that introduced it. A user's baseline is one integer
(``users.seen_release_ordinal``); pending notes are the ones with
``seen < note.ordinal <= build.ordinal``.

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
    ReleaseNotesResponse,
)
from app.repositories.ports import IdentityRepository

logger = logging.getLogger("silicon_notebook.release_notes")

MANIFEST_FILENAME = "release-manifest.json"
MANIFEST_SCHEMA = 1

# services/ -> app/ -> backend/ -> package root: the same root
# ``app.core.config`` resolves ``.env`` and relative storage paths against.
_ROOT_DIR = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ReleaseNote:
    id: str
    ordinal: int
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
        if not isinstance(note_id, str) or not note_id:
            raise ManifestInvalid("bad_note")
        if not _is_int(note_ordinal) or note_ordinal < 0:
            raise ManifestInvalid("bad_note")
        if not isinstance(body, str) or not body.strip():
            raise ManifestInvalid("bad_note")
        if note_id in seen_ids:
            raise ManifestInvalid("duplicate_note")
        seen_ids.add(note_id)
        notes.append(ReleaseNote(note_id, note_ordinal, body))
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


def pending_release_notes(
    seen: int, build_ordinal: int, notes: Sequence[ReleaseNote]
) -> list[ReleaseNote]:
    """Notes with ``seen < ordinal <= build_ordinal``, newest first.

    Ties on ordinal keep a stable order by id ascending. A rollback
    (``build_ordinal < seen``) yields nothing by construction.
    """
    pending = [n for n in notes if seen < n.ordinal <= build_ordinal]
    pending.sort(key=lambda n: (-n.ordinal, n.id))
    return pending


def release_notes_for_user(
    identity: IdentityRepository,
    user_id: str,
    manifest: ReleaseManifest | None,
) -> ReleaseNotesResponse:
    """GET semantics: unavailable -> no write; NULL baseline -> initialize it
    silently and show nothing; otherwise the pending notes."""
    if manifest is None:
        return ReleaseNotesResponse(available=False, build=None, notes=[])
    build = ReleaseNotesBuild(
        version=manifest.build.version, ordinal=manifest.build.ordinal
    )
    seen = identity.get_seen_release_ordinal(user_id)
    if seen is None:
        identity.initialize_seen_release_ordinal(user_id, manifest.build.ordinal)
        return ReleaseNotesResponse(available=True, build=build, notes=[])
    pending = pending_release_notes(seen, manifest.build.ordinal, manifest.notes)
    return ReleaseNotesResponse(
        available=True,
        build=build,
        notes=[
            ReleaseNoteItem(id=n.id, ordinal=n.ordinal, body=n.body)
            for n in pending
        ],
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
