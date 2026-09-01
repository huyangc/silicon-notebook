from __future__ import annotations

import contextvars
import hashlib
import itertools
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from app.core.config import Settings
from app.services.model_work import model_artifact_scope


_QUESTION_BUILD_PAGE_ROWS = 100
_QUESTION_SCHEMA_HINT = '{"questions":["question answerable from the passage"]}'


def question_generation_prompt(text: str, maximum: int) -> str:
    return (
        "Generate natural search questions that can be answered completely and "
        "directly from the passage below. Cover distinct user intents, preserve "
        "technical identifiers, do not add external facts, and return JSON only.\n"
        f"Return at most {maximum} items in the `questions` array.\n\n"
        f"PASSAGE:\n{text}"
    )


def validate_generated_questions(
    raw: str,
    *,
    maximum: int,
    max_chars: int,
) -> list[str]:
    _valid, questions = _parse_generated_questions(
        raw, maximum=maximum, max_chars=max_chars
    )
    return questions


def _parse_generated_questions(
    raw: str,
    *,
    maximum: int,
    max_chars: int,
) -> tuple[bool, list[str]]:
    """Return schema validity separately from a valid empty question list."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False, []
    values = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return False, []
    if any(not isinstance(value, str) for value in values):
        return False, []
    seen: set[str] = set()
    accepted: list[str] = []
    for value in values:
        question = " ".join(value.split()).strip()
        folded = question.casefold()
        if not question or len(question) > max_chars or folded in seen:
            continue
        seen.add(folded)
        accepted.append(question)
    return True, list(itertools.islice(accepted, maximum))


class ChunkQuestionIndexService:
    """Offline builder for the optional question-to-original-chunk index."""

    def __init__(
        self,
        *,
        settings: Settings,
        chunks,
        models,
        event_log,
        now: Callable[[], str],
    ) -> None:
        self.settings = settings
        self.chunks = chunks
        self.models = models
        self.event_log = event_log
        self.now = now

    @staticmethod
    def _question_id(chunk_id: str, question: str) -> str:
        digest = hashlib.sha256(
            f"{chunk_id}\0{question}".encode("utf-8")
        ).hexdigest()
        return f"cq-{digest}"

    def _build_one(self, row: dict, chat_client, embedder) -> tuple[str, int]:
        text = str(row.get("text") or "").strip()
        # Do not silently crop source-authored text for the generation model.
        # Pathological chunks remain ordinary searchable chunks and are simply
        # absent from this optional supplement.
        if not text or len(text) > self.settings.embed_truncate_chars:
            self.chunks.replace_chunk_questions(
                str(row["chunk_id"]),
                str(row.get("notebook_id") or ""),
                str(row["source_id"]),
                (),
                created_at=self.now(),
            )
            return "skipped", 0
        with model_artifact_scope(
            notebook_id=str(row.get("notebook_id") or ""),
            parent_id=str(row["source_id"]),
        ):
            raw = chat_client.chat_json(
                [{
                    "role": "user",
                    "content": question_generation_prompt(
                        text, self.settings.generated_question_questions_per_chunk
                    ),
                }],
                _QUESTION_SCHEMA_HINT,
            )
        valid, questions = _parse_generated_questions(
            raw,
            maximum=self.settings.generated_question_questions_per_chunk,
            max_chars=self.settings.embed_truncate_chars,
        )
        if not valid:
            raise ValueError("question generation returned an invalid schema")
        if not questions:
            self.chunks.replace_chunk_questions(
                str(row["chunk_id"]),
                str(row.get("notebook_id") or ""),
                str(row["source_id"]),
                (),
                created_at=self.now(),
            )
            return "empty", 0
        vectors = embedder.embed_texts(questions)
        if len(vectors) != len(questions):
            raise RuntimeError("question embedding result count mismatch")
        rows = [
            (self._question_id(str(row["chunk_id"]), question), question, vector)
            for question, vector in zip(questions, vectors)
        ]
        self.chunks.replace_chunk_questions(
            str(row["chunk_id"]),
            str(row.get("notebook_id") or ""),
            str(row["source_id"]),
            rows,
            created_at=self.now(),
        )
        return "stored", len(rows)

    def build_notebook(
        self,
        notebook_id: str,
        *,
        workers: int,
        force: bool = False,
        progress: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, int]:
        if self.settings.generated_question_index_mode == "off":
            raise RuntimeError(
                "GENERATED_QUESTION_INDEX_MODE=off; use shadow or on for explicit opt-in"
            )
        chat_client = self.models.chat("chunk_question_generation")
        embedder = self.models.embedding("chunk_embedding")
        if not getattr(chat_client, "configured", False):
            raise RuntimeError("chunk_question_generation workload is not configured")
        if not getattr(embedder, "configured", False):
            raise RuntimeError("chunk_embedding workload is not configured")
        worker_count = max(
            1,
            min(
                int(workers),
                int(self.models.parallelism("chunk_question_generation")),
                int(self.models.parallelism("chunk_embedding")),
            ),
        )
        counts = {
            "chunks_seen": 0,
            "chunks_stored": 0,
            "questions_stored": 0,
            "empty": 0,
            "skipped": 0,
            "failed": 0,
        }
        after_id = ""
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="chunk-question"
        ) as pool:
            while True:
                page = self.chunks.question_index_chunk_page(
                    notebook_id,
                    after_id=after_id,
                    limit=_QUESTION_BUILD_PAGE_ROWS,
                    include_existing=force,
                )
                if not page:
                    break
                futures = {}
                for source_row in page:
                    item = dict(source_row)
                    item["notebook_id"] = notebook_id
                    context = contextvars.copy_context()
                    future = pool.submit(
                        context.run, self._build_one, item, chat_client, embedder
                    )
                    futures[future] = str(item["chunk_id"])
                for future in as_completed(futures):
                    counts["chunks_seen"] += 1
                    try:
                        status, questions = future.result()
                    except Exception:  # per-chunk retry remains possible next run
                        counts["failed"] += 1
                    else:
                        if status == "stored":
                            counts["chunks_stored"] += 1
                            counts["questions_stored"] += questions
                        else:
                            counts[status] += 1
                    if progress is not None:
                        progress(dict(counts))
                after_id = str(page[-1]["chunk_id"])
                if len(page) < _QUESTION_BUILD_PAGE_ROWS:
                    break
        stats = self.chunks.question_index_stats(notebook_id)
        counts["indexed_chunks"] = stats["chunks"]
        counts["question_bearing_chunks"] = stats["question_chunks"]
        counts["indexed_questions"] = stats["questions"]
        self.event_log.emit({
            "kind": "chunk_question_index_build",
            **counts,
        })
        return counts
