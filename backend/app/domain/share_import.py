"""Portable, untrusted citation manifest carried by a saved Markdown source."""
from __future__ import annotations

import base64
import json
import re
from typing import Any

MANIFEST_VERSION = 1
_MANIFEST = re.compile(r"\A<!-- silicon-notebook-share:([A-Za-z0-9+/=]+) -->\n")
_KEY = re.compile(r"t\d+-(?:k)?\d+\Z")
_TEXT_FIELDS = ("key", "original_key", "title", "file_name", "location", "snippet")


def _reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("key"), str) or not _KEY.fullmatch(value["key"]):
        return None
    ref = {key: value[key] for key in _TEXT_FIELDS if isinstance(value.get(key), str)}
    verification = value.get("verification")
    if isinstance(verification, str) and verification in ("changed", "source_gone", "unverifiable"):
        ref["verification"] = verification
    for key in ("title_truncated", "file_name_truncated", "snippet_truncated", "is_memory"):
        if type(value.get(key)) is bool:
            ref[key] = value[key]
    evidence = value.get("evidence")
    if not ref.get("verification") and isinstance(evidence, dict) and evidence.get("kind") in ("original", "knowledge", "memory"):
        ref["evidence"] = {key: evidence[key] for key in ("kind", "text", "quote", "location") if isinstance(evidence.get(key), str)}
        ref["evidence"]["truncated"] = evidence.get("truncated") is True
    return ref


def encode_manifest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "<!-- silicon-notebook-share:" + base64.b64encode(payload.encode()).decode() + " -->\n"


def split_manifest(text: str) -> tuple[dict[str, Any] | None, str]:
    """Only our versioned envelope is special; other Markdown stays untouched."""
    match = _MANIFEST.match(text)
    if not match:
        return None, text
    try:
        value = json.loads(base64.b64decode(match[1], validate=True))
    except (ValueError, UnicodeError, RecursionError):
        return None, text
    if not isinstance(value, dict) or value.get("version") != MANIFEST_VERSION:
        return None, text
    turns = value.get("turns")
    if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
        return None, text
    normalized = {"version": MANIFEST_VERSION, "kind": value.get("kind") if value.get("kind") in ("reports", "conversations") else "conversations",
                  "title": value.get("title") if isinstance(value.get("title"), str) else "", "turns": []}
    for turn in turns:
        refs = turn.get("references")
        if not isinstance(refs, list):
            return None, text
        normalized["turns"].append({
            **{key: turn[key] for key in ("question", "asked_at", "answered_at") if isinstance(turn.get(key), str)},
            **{flag: True for flag in ("question_truncated", "truncated_references") if turn.get(flag) is True},
            "references": [clean for item in refs if (clean := _reference(item)) is not None],
        })
    # Imported metadata is a citation record, never proof of authenticity.
    return normalized, text[match.end():]


def references_for_text(manifest: dict[str, Any], text: str) -> list[dict[str, Any]]:
    keys = set(re.findall(r"\[(t\d+-[A-Za-z0-9]+)\]", text))
    return [ref for turn in manifest["turns"] for ref in turn.get("references", [])
            if isinstance(ref, dict) and ref.get("key") in keys]
