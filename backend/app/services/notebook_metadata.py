"""Whole-corpus metadata synthesis and in-process refresh coalescing.

Only source metadata enters this workflow, never document bodies or KG objects.
Every source participates; large inputs are summarized in bounded batches.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from contextvars import copy_context
from dataclasses import dataclass, field

from app.services.prompts import NOTEBOOK_META_SCHEMA_HINT, notebook_meta_prompt


NOTEBOOK_AUTO_NAME_MAX_CHARS = 120
NOTEBOOK_AUTO_DESCRIPTION_MAX_CHARS = 1000


@dataclass
class _Refresh:
    generation: int
    pending: Callable[[], None] | None
    settled: threading.Event = field(default_factory=threading.Event)


class MetadataRefreshCoordinator:
    """One worker per notebook; requests in flight collapse to the latest one.

    The caller reserves its database generation BEFORE enqueueing, so an older
    model response cannot publish while a newer refresh is pending. No lock or
    database connection is held across the callback.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, _Refresh] = {}

    def run(self, notebook_id: str, generation: int, refresh: Callable[[], None]) -> None:
        context = copy_context()
        callback_in_context = lambda: context.run(refresh)
        with self._lock:
            active = self._active.get(notebook_id)
            wait_for = active
            if active is not None:
                if generation > active.generation:
                    active.generation = generation
                    active.pending = callback_in_context
            else:
                active = _Refresh(generation, callback_in_context)
                self._active[notebook_id] = active
        if wait_for is not None:
            # Source terminal events and delete responses must not precede the
            # coalesced write: the UI refetches metadata at those boundaries.
            wait_for.settled.wait()
            return
        try:
            while True:
                with self._lock:
                    callback, active.pending = active.pending, None
                if callback is not None:
                    callback()
                with self._lock:
                    if active.pending is None:
                        del self._active[notebook_id]
                        return
        finally:
            with self._lock:
                if self._active.get(notebook_id) is active:
                    del self._active[notebook_id]
                active.settled.set()


def _batches(records: Iterable[str], max_chars: int) -> Iterable[str]:
    """Pack all text, splitting oversized records without dropping their tail."""
    current = ""
    for record in records:
        if current and len(current) + len(record) + 1 > max_chars:
            yield current
            current = ""
        while len(record) > max_chars:
            yield record[:max_chars]
            record = record[max_chars:]
        if record:
            current = f"{current}\n{record}" if current else record
    if current:
        yield current


def synthesize_metadata(client, records: list[str], *, batch_chars: int) -> tuple[str, str]:
    """Hierarchically combine every record; invalid output fails the whole pass.

    The validated input budget always fits at least two maximum-sized output
    records, so every reduction round makes progress without a depth cutoff.
    """
    while True:
        outputs = []
        for block in _batches(records, batch_chars):
            raw = client.chat_json(
                [{"role": "user", "content": notebook_meta_prompt(block)}],
                NOTEBOOK_META_SCHEMA_HINT,
            )
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("invalid notebook metadata")
            name, description = parsed.get("name"), parsed.get("description")
            if not isinstance(name, str) or not isinstance(description, str):
                raise ValueError("invalid notebook metadata fields")
            name, description = name.strip(), description.strip()
            if not (name and description):
                raise ValueError("empty notebook metadata")
            if (len(name) > NOTEBOOK_AUTO_NAME_MAX_CHARS
                    or len(description) > NOTEBOOK_AUTO_DESCRIPTION_MAX_CHARS):
                raise ValueError("oversized notebook metadata")
            outputs.append((name, description))
        if len(outputs) == 1:
            return outputs[0]
        if not outputs:
            raise ValueError("empty notebook metadata input")
        records = [f"- {name}: {description}" for name, description in outputs]


def fallback_metadata(titles: list[str], labels: list[str]) -> tuple[str, str]:
    if not titles:
        return "未命名笔记本", "尚未添加来源。"
    name = f"资料集（{len(titles)} 个来源）"
    if len(titles) == 1 and titles[0].strip():
        title = titles[0].strip()
        if len(title) <= NOTEBOOK_AUTO_NAME_MAX_CHARS:
            name = title
    description = f"本笔记本收录了 {len(titles)} 个来源。"
    if labels:
        description += f"文档类型涵盖 {'、'.join(labels)}。"
    return name, description
