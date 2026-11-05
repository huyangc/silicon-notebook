"""Frozen, token-scoped citation context. Anonymous reads never hydrate sources.

Turn identities stay internal; public projection receives only display content.
An absent row is a legacy link, never a request to fetch more private content.
"""
from __future__ import annotations

from typing import Any

from app.services.conversation_public_view import _turn_body_and_references, public_window
from app.services.report_public_view import MAX_REFERENCES
from app.services.share_disclosure import author_memory_sources


def _store(repo: Any) -> Any:
    return repo._runtime.answer_links


def _turn_key(turn: dict) -> str:
    return str(turn.get("answer_id") or turn.get("job_id") or "")


def _bounded(text: str, quote: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    at = text.find(quote) if quote else -1
    start = max(0, at - max(0, limit - len(quote)) // 2) if at >= 0 else 0
    return text[start:start + limit], True


def _capture(repo: Any, owner: str, selected: list[tuple[str, dict]]) -> dict:
    source_ids = list(dict.fromkeys(str(ref.get("source_id") or "") for _, ref in selected))
    memory_sources = author_memory_sources(repo._runtime.memory_store, source_ids, owner)
    # Explicit actor is essential: this helper must never inherit anonymous
    # request context's seeded-administrator fallback.
    readable = {sid: notebook for sid in source_ids if sid
                and (notebook := repo.source_notebook_id(sid, viewer_id=owner))}
    element_ids = [str(ref.get("element_id")) for _, ref in selected
                   if ref.get("element_id") and ref.get("source_id") in readable
                   and ref.get("source_id") not in memory_sources]
    elements = repo.evidence_elements(element_ids) if element_ids else {}
    chunk_ids = list(dict.fromkeys(str(ref.get("object_id") or ref.get("chunk_id") or "")
        for _, ref in selected if ref.get("object_type") == "chunk" or ref.get("chunk_id")))
    chunks = {}
    # Flat Citation has no chunk_id: recover candidate chunks through the
    # indexed first-element reverse map and verify BOTH source and quote.
    candidates: dict[str, list[str]] = {}
    if chunk_ids or element_ids:
        store = repo._runtime.chunk_store
        with store.database.connect() as db:
            for notebook in set(readable.values()):
                ids = [str(ref.get("element_id")) for _, ref in selected
                       if ref.get("element_id") and readable.get(str(ref.get("source_id") or "")) == notebook
                       and ref.get("source_id") not in memory_sources]
                if ids:
                    for row in store.chunks_for_element_ids(db, notebook, ids):
                        candidates.setdefault(row["element_id"], []).append(row["chunk_id"])
                        chunk_ids.append(row["chunk_id"])
            chunk_ids = list(dict.fromkeys(chunk_ids))
            chunks = {row["id"]: dict(row) for row in store.rows_by_ids(db, chunk_ids)}
    out = {}
    for key, ref in selected:
        if ref.get("verification"):
            continue
        quote = str(ref.get("snippet") or ref.get("quoted_span") or "")
        source_id = str(ref.get("source_id") or "")
        is_memory = bool(ref.get("memory_id") or ref.get("object_type") == "memory"
                         or source_id in memory_sources)
        kind = "memory" if is_memory else "original"
        text = quote if is_memory else ""
        location = str(ref.get("location_label") or "")
        if not is_memory:
            element = elements.get(str(ref.get("element_id") or ""))
            chunk = chunks.get(str(ref.get("object_id") or ref.get("chunk_id") or ""))
            if source_id in readable and chunk and chunk.get("source_id") == source_id:
                original = str(chunk.get("text") or "")
                if quote and quote in original:
                    text = original
            if not text and source_id in readable:
                for candidate in candidates.get(str(ref.get("element_id") or ""), []):
                    found = chunks.get(candidate, {})
                    original = str(found.get("text") or "")
                    if found.get("source_id") == source_id and quote and quote in original:
                        text = original
                        break
            if element and element.get("source_id") == source_id:
                original = str(element.get("text") or "")
                # A chunk may name its first element while quoting a later
                # element: never present that unrelated first paragraph.
                if quote and quote in original:
                    text = original
                    location = str(element.get("location_label") or location)
            if not text and ref.get("definition"):
                kind, text = "knowledge", str(ref["definition"])
        if not text:
            continue
        text, truncated = _bounded(text, quote, repo.settings.public_share_evidence_max_chars)
        out[str(key)] = {"kind": kind, "text": text, "quote": quote if quote in text else "",
                         "location": location, "truncated": truncated}
    return out


def prepare_share_evidence(repo: Any, row: dict, *, token: str = "") -> dict:
    owner = str(row.get("created_by") or "")
    if not owner:
        return {}
    existing = _store(repo).share_evidence(token) if token else {}
    payload = {}
    for turn in public_window(row.get("turns") or []):
        key = _turn_key(turn)
        if key and key not in existing:
            _, selected, _ = _turn_body_and_references(turn.get("payload"))
            # Empty captures are frozen too: later source recovery must not
            # silently add content to an already published turn.
            payload[key] = {"__captured__": {}, **_capture(repo, owner, selected)}
    return payload


def capture_share_evidence(repo: Any, token: str, row: dict) -> None:
    _store(repo).put_share_evidence(token, prepare_share_evidence(repo, row, token=token))


def prepare_report_evidence(repo: Any, row: dict, *, token: str = "") -> dict:
    if token and "report" in _store(repo).share_evidence(token):
        return {}
    selected = [(str(ref.get("key") or ""), ref)
                for ref in (row.get("references") or [])[:MAX_REFERENCES] if isinstance(ref, dict) and ref.get("key")]
    evidence = _capture(repo, str(row.get("created_by") or ""), selected)
    return {"report": {"__captured__": {}, **evidence}}


def capture_report_evidence(repo: Any, token: str, row: dict) -> None:
    _store(repo).put_share_evidence(token, prepare_report_evidence(repo, row, token=token))


def overlay_share_evidence(repo: Any, token: str, row: dict, payload: dict) -> dict:
    frozen = _store(repo).share_evidence(token)
    for turn, public in zip(public_window(row.get("turns") or []), payload.get("turns") or []):
        _overlay(public, frozen.get(_turn_key(turn), {}))
    return payload


def _overlay(payload: dict, evidence: dict) -> None:
    for ref in payload.get("references") or []:
        if ref.get("key") in evidence and evidence[ref["key"]].get("kind") and not ref.get("verification"):
            ref["evidence"] = evidence[ref["key"]]


def overlay_report_evidence(repo: Any, token: str, payload: dict) -> dict:
    _overlay(payload, _store(repo).share_evidence(token).get("report", {}))
    return payload
