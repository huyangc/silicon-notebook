"""Persistence contract for independent global question-answer sessions."""
from typing import Protocol, Sequence

from app.models.global_ask import GlobalAskJob, GlobalConversationSummary


class GlobalAskAuthorityStorePort(Protocol):
    """Batch read authority used only by global orchestration."""
    def readable_notebook_names(self, user_id: str) -> dict[str, str]: ...
    def readable_notebook_ids(self, notebook_ids: Sequence[str], user_id: str) -> set[str]: ...


class GlobalAskSourceStorePort(Protocol):
    """Narrow source snapshots for global execution and evidence validation.

    ``passage_evidence_snapshot`` and ``evidence_fingerprints`` are not two
    spellings of one read. The first answers "what did the passage this run
    RETRIEVED consist of", and it has to answer both halves -- the passage's
    own text and its elements' fingerprints -- out of ONE database snapshot,
    because element ids are reused deterministically across a re-ingest
    (``el-<source>-<index>``): read the two separately and a source reparsed in
    between hands back new text under an old id, which would be filed as the
    old passage's evidence and then compared against itself at re-check time.
    The second is the terminal "what is it NOW" read, taken once the answer
    exists and deliberately by element id, because by then the question is
    about the cited elements rather than about any passage.

    ``passage_evidence_snapshot`` returns, per chunk id that still exists,
    ``{"text_sha": <sha256 of chunks.text>, "elements": {element_id:
    (source_id, sha256 of the element text)}}``. A chunk the library no longer
    holds is ABSENT from the mapping rather than present and empty, which is
    how the caller tells "this passage is gone" from "this passage declares no
    elements". Only hashes cross the boundary; element bodies never do.
    """
    def visible_source_ids_by_notebook(self, notebook_ids: Sequence[str]) -> dict[str, list[str]]: ...
    def evidence_fingerprints(self, element_ids: Sequence[str]) -> dict[str, tuple[str, str]]: ...
    def passage_evidence_snapshot(self, chunk_ids: Sequence[str]) -> dict[str, dict]: ...


class GlobalAskStorePort(Protocol):
    def conversation(self, conversation_id: str, user_id: str) -> GlobalConversationSummary | None: ...
    def list_conversations(self, user_id: str, limit: int, offset: int) -> list[GlobalConversationSummary]: ...
    def job(self, job_id: str, user_id: str) -> GlobalAskJob | None: ...
    def request_job(self, user_id: str, request_id: str | None) -> tuple[GlobalAskJob, str] | None: ...
    def create(self, job: GlobalAskJob, user_id: str, request_id: str | None, request_json: str, submitted_via: str, *, new_conversation: bool) -> None: ...
    def save(self, job: GlobalAskJob, user_id: str) -> bool: ...
    def save_progress(self, job: GlobalAskJob, user_id: str) -> bool: ...
    def set_feedback(self, job_id: str, user_id: str, rating: str) -> GlobalAskJob | None: ...
    def jobs(self, conversation_id: str, user_id: str, limit: int, offset: int) -> list[GlobalAskJob]: ...
    def completed_history(self, conversation_id: str, user_id: str, limit: int) -> list[dict]: ...
    def running_job_ids(self, conversation_id: str, user_id: str) -> list[str]: ...
    def rename(self, conversation_id: str, user_id: str, title: str) -> bool: ...
    def delete(self, conversation_id: str, user_id: str) -> bool: ...
    def recover(self) -> None: ...

    # Public sharing. ``share_conversation`` and ``conversation_share_state``
    # are owner-scoped and raise ``KeyError`` when the conversation is missing
    # or belongs to someone else -- a non-owner must be indistinguishable from
    # a missing row. ``unshare_conversation`` does NOT raise: it is one
    # idempotent owner-scoped UPDATE that silently matches nothing for a
    # missing or foreign conversation (same as the notebook twin), so a caller
    # that owes the client a 404 must establish ownership itself first.
    # ``share_conversation`` additionally raises ``ConversationShareWatermarkStale``
    # (``expected_through_id`` -- a JOB id -- no longer resolves to a done job
    # of this conversation, or the boundary would regress an already-published
    # watermark) and ``ConversationHasNoShareableAnswer`` (no completed job to
    # bound the snapshot; refused atomically, so no token is minted). It returns
    # ``{"share_token", "shared_through_at", "shared_through_id"}``, the same
    # shape ``conversation_share_state`` reads back.
    # ``public_conversation_by_token`` is the ONLY session-free read: it takes
    # nothing but the token, returns ``None`` for unknown/revoked, and hands
    # back the sharer's ``user_id`` for the caller's live authorization
    # re-check plus the watermark-bounded done jobs with their payloads
    # untouched -- the public whitelist projection is the service layer's job.
    def share_conversation(self, conversation_id: str, user_id: str, *, expected_through_id: str | None = None) -> dict: ...
    def conversation_share_state(self, conversation_id: str, user_id: str) -> dict: ...
    def unshare_conversation(self, conversation_id: str, user_id: str) -> None: ...
    def public_conversation_by_token(self, token: str) -> dict | None: ...
