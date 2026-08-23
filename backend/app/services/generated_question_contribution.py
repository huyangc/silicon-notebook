"""Core-private bridge for the generated-question retrieval contributor."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
import hashlib
from typing import Any

from app.domain.extensions import (
    GENERATED_QUESTION_ACCESS_CAPABILITY,
    RetrievalContributionCallContext,
    RetrievalEvidenceProposal,
    lane_is_dormant,
)
from app.services.cancellation import AskCancelled
from app.services.retrieval import RetrievedChunk, est_tokens
from app.services.retrieval_run import current_retrieval_run
from app.services.source_scope import current_source_scope


class _CancellationToken:
    def __init__(self, event: Any) -> None:
        self._event = event
        self._observed_cancelled = False

    def is_set(self) -> bool:
        if self._observed_cancelled:
            return True
        if self._event is None:
            return False
        cancelled = self._event.is_set()
        if type(cancelled) is not bool:
            raise TypeError("malformed cancellation state")
        if cancelled:
            # Core cancellation is monotonic. Cache the first authoritative
            # True so the host's is_set()+raise_if_cancelled() sequence cannot
            # be turned into a hostile second-read type change.
            self._observed_cancelled = True
        return cancelled

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            raise AskCancelled()


class GeneratedQuestionContributionCall:
    """Run the legacy optional lane behind a narrow request-local capability.

    The plugin never receives the query, index rows, baseline, settings, model
    client, or repository collaborators.  A defensive baseline copy isolates
    collision-support overlays until the host has accepted the whole lane.
    """

    def __init__(
        self,
        notebook_id: str,
        baseline: tuple[Sequence[RetrievedChunk], Any, Any],
        *,
        mode: str,
        evaluate: Callable[[tuple[list[RetrievedChunk], Any, Any]], Any],
        matrix_hydrate: Callable[[Sequence[str]], tuple[Any, Any, Any]],
        failure_event: Callable[[], None],
        matrix_failure_event: Callable[[], None] = lambda: None,
        cancel_event: Any = None,
    ) -> None:
        self._notebook_id = notebook_id
        self._baseline = baseline
        self._mode = mode
        self._evaluate = evaluate
        self._matrix_hydrate = matrix_hydrate
        self._failure_event = failure_event
        self._matrix_failure_event = matrix_failure_event
        self._cancellation = _CancellationToken(cancel_event)
        self._attempted = False
        self._failure_emitted = False
        self._evaluated: tuple[Any, Any, Any] | None = None
        self._changed = False
        self._proposals: tuple[RetrievalEvidenceProposal, ...] = ()
        self._by_id: dict[str, RetrievalEvidenceProposal] = {}

    def propose(self) -> tuple[RetrievalEvidenceProposal, ...]:
        if self._attempted:
            return self._proposals
        self._attempted = True
        scored, ids, matrix = self._baseline
        isolated = deepcopy(list(scored))
        try:
            evaluated = self._evaluate((isolated, ids, matrix))
            if not self._valid_evaluation(evaluated, isolated):
                raise TypeError("invalid generated-question result")
        except AskCancelled:
            raise
        except Exception:
            self._emit_failure()
            return ()
        self._evaluated = evaluated
        self._changed = bool(
            len(evaluated[0]) > len(isolated)
            or any(
                candidate.retrieval_supports != original.retrieval_supports
                for candidate, original in zip(evaluated[0], isolated)
            )
        )
        if self._mode != "on":
            return ()
        tail = evaluated[0][len(isolated):]
        self._proposals = tuple(
            RetrievalEvidenceProposal(
                identity=chunk.chunk_id,
                notebook_id=self._notebook_id,
                source_id=chunk.source_id,
                provenance_kind="chunk",
                provenance_reference=chunk.chunk_id,
                value=chunk,
                # The legacy lane is bounded by recall/scan settings.  The
                # generic host must not impose a second text-length budget.
                token_cost=0,
            )
            for chunk in tail
        )
        self._by_id = {proposal.identity: proposal for proposal in self._proposals}
        return self._proposals

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]:
        return tuple(
            proposal
            for identity in identities
            if (proposal := self._by_id.get(identity)) is not None
        )

    def visible_result(self, host_chunks: Sequence[RetrievedChunk]):
        """Commit overlays only after the host accepted the complete tail."""
        evaluated = self._evaluated
        original, _ids, _matrix = self._baseline
        host_tail = tuple(host_chunks[len(original):])
        if evaluated is None:
            evaluated_chunks, evaluated_ids, evaluated_matrix = (
                original, _ids, _matrix
            )
        else:
            evaluated_chunks, evaluated_ids, evaluated_matrix = evaluated
        expected_tail = tuple(
            proposal.identity for proposal in self._proposals
        ) if self._mode == "on" and self._changed else ()
        expected_by_id = {
            proposal.identity: proposal.value for proposal in self._proposals
        }
        accepted_generated = tuple(
            chunk.chunk_id for chunk in host_tail
            if expected_by_id.get(chunk.chunk_id) is chunk
        )
        independent_tail = tuple(
            chunk for chunk in host_tail
            if expected_by_id.get(chunk.chunk_id) is not chunk
        )
        generated_accepted = (
            self._mode == "on"
            and self._changed
            and accepted_generated == expected_tail
        )
        if not generated_accepted:
            if expected_tail:
                self._emit_failure()
        visible_tail = (
            host_tail if generated_accepted else independent_tail
        )
        if independent_tail:
            try:
                self._cancellation.raise_if_cancelled()
            except AskCancelled:
                raise
            except Exception:
                return self._baseline
            try:
                _rows, final_ids, final_matrix = self._matrix_hydrate(
                    [chunk.chunk_id for chunk in (*original, *visible_tail)]
                )
            except AskCancelled:
                raise
            except Exception:
                # Authority admission has already succeeded for the independent
                # tail.  A failed optional diversity-matrix rebuild must not
                # erase that evidence; downstream MMR treats ids absent from
                # the original matrix as zero pair-similarity.
                final_ids, final_matrix = _ids, _matrix
                try:
                    self._matrix_failure_event()
                except Exception:
                    pass
            try:
                # The bounded matrix read is allowed to finish safely, but a
                # cancellation raised while it was in flight must take effect
                # before any newly admitted evidence becomes visible.
                self._cancellation.raise_if_cancelled()
            except AskCancelled:
                raise
            except Exception:
                return self._baseline
        else:
            final_ids, final_matrix = evaluated_ids, evaluated_matrix
        if generated_accepted:
            for target, candidate in zip(
                original, evaluated_chunks[:len(original)]
            ):
                target.retrieval_supports = candidate.retrieval_supports
        if not visible_tail and not generated_accepted:
            return self._baseline
        return ([*original, *visible_tail], final_ids, final_matrix)

    def fail_open_result(self):
        self._emit_failure()
        return self._baseline

    def _valid_evaluation(
        self, result: object, isolated: Sequence[RetrievedChunk]
    ) -> bool:
        try:
            if type(result) is not tuple or len(result) != 3:
                return False
            chunks = result[0]
            if type(chunks) not in (list, tuple) or len(chunks) < len(isolated):
                return False
            prefix = chunks[:len(isolated)]
            if any(
                not self._baseline_chunk_is_monotonic(candidate, original)
                for candidate, original in zip(prefix, isolated)
            ):
                return False
            baseline_ids = {chunk.chunk_id for chunk in isolated}
            tail_ids: set[str] = set()
            for chunk in chunks[len(isolated):]:
                if (
                    type(chunk) is not RetrievedChunk
                    or type(chunk.chunk_id) is not str
                    or not chunk.chunk_id
                    or chunk.chunk_id in baseline_ids
                    or chunk.chunk_id in tail_ids
                    or type(chunk.source_id) is not str
                    or not chunk.source_id
                    or not any(
                        support.origin == "generated_question"
                        for support in chunk.retrieval_supports
                    )
                ):
                    return False
                tail_ids.add(chunk.chunk_id)
            return True
        except Exception:
            return False

    @classmethod
    def _baseline_chunk_is_monotonic(
        cls, candidate: RetrievedChunk, original: RetrievedChunk
    ) -> bool:
        if (
            type(candidate) is not RetrievedChunk
            or cls._chunk_signature(candidate) != cls._chunk_signature(original)
            or not all(
                support in candidate.retrieval_supports
                for support in original.retrieval_supports
            )
        ):
            return False
        original_supports = set(original.retrieval_supports)
        return all(
            support in original_supports or support.origin == "generated_question"
            for support in candidate.retrieval_supports
        )

    @staticmethod
    def _chunk_signature(chunk: RetrievedChunk) -> tuple[object, ...]:
        return (
            chunk.chunk_id,
            chunk.source_id,
            chunk.source_title,
            chunk.section_path,
            chunk.text,
            tuple(chunk.element_ids),
            chunk.score,
            chunk.relevance,
            chunk.notebook_id,
        )

    def _emit_failure(self) -> None:
        if self._failure_emitted:
            return
        self._failure_emitted = True
        try:
            self._failure_event()
        except Exception:
            return


class _AdmissionSource:
    """Generated request memory plus one batch authority for future peers."""

    def __init__(
        self,
        call: GeneratedQuestionContributionCall,
        notebook_id: str,
        actor_id: str,
        fallback: Callable[
            [str, str, Sequence[str]], Sequence[RetrievedChunk]
        ],
    ) -> None:
        self._call = call
        self._notebook_id = notebook_id
        self._actor_id = actor_id
        self._fallback = fallback

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]:
        primary = self._call.read(identities)
        by_id = {proposal.identity: proposal for proposal in primary}
        missing = tuple(identity for identity in identities if identity not in by_id)
        if missing:
            try:
                hydrated = self._fallback(
                    self._notebook_id, self._actor_id, missing
                )
            except Exception:
                return ()
            if type(hydrated) not in (list, tuple):
                return ()
            missing_set = set(missing)
            for chunk in hydrated:
                if (
                    type(chunk) is not RetrievedChunk
                    or chunk.chunk_id not in missing_set
                    or chunk.chunk_id in by_id
                    or not chunk.source_id
                    or chunk.notebook_id != self._notebook_id
                ):
                    return ()
                by_id[chunk.chunk_id] = RetrievalEvidenceProposal(
                    identity=chunk.chunk_id,
                    notebook_id=self._notebook_id,
                    source_id=chunk.source_id,
                    provenance_kind="chunk",
                    provenance_reference=chunk.chunk_id,
                    value=chunk,
                    token_cost=est_tokens(chunk.text),
                )
        return tuple(by_id[identity] for identity in identities if identity in by_id)


def generated_question_lane_is_dormant(host: Any) -> bool:
    """True when disabling the generated-question capability leaves
    ``chunk_candidates`` with nothing.

    Delegates to ``app.domain.extensions.lane_is_dormant`` for the generic
    probe-safety argument (defensive read, fail into the host) shared with
    ``source_graph_activation.selected_evidence_lane_is_dormant`` (codex
    #565). What is lane-specific here: the caller only asks this from the
    branch that is about to disable the capability anyway (``not
    generated_enabled`` -- deployment mode is ``off``, no actor is bound to
    this run, or this request's baseline already cleared the trigger
    threshold): the same
    ``disabled_capabilities={GENERATED_QUESTION_ACCESS_CAPABILITY}`` set that
    branch would otherwise only hand to ``host.run`` after unconditionally
    building a call/context envelope. When dormant, the caller returns the
    untouched baseline before building that envelope at all -- byte-identical
    to what ``host.run`` would have handed back after filtering out the only
    registration, since a fully-filtered ``_run`` returns the exact baseline
    object with zero contribution attempts and therefore zero events.
    """
    return lane_is_dormant(
        host,
        "chunk_candidates",
        frozenset({GENERATED_QUESTION_ACCESS_CAPABILITY}),
    )


def generated_question_call_context(
    call: GeneratedQuestionContributionCall,
    *,
    actor_id: str,
    connection_probe: Any,
    admission_hydrate: Callable[
        [str, str, Sequence[str]], Sequence[RetrievedChunk]
    ],
    max_items: int,
    max_tokens: int,
    generated_question_enabled: bool = True,
) -> RetrievalContributionCallContext:
    """Build the candidate-stage invocation envelope without performing I/O."""
    scope = current_source_scope()
    run = current_retrieval_run()
    run_id = str(getattr(run, "run_id", "") or "chunk-candidates")
    scope_id = hashlib.sha256(
        f"{run_id}:{id(call)}".encode("ascii")
    ).hexdigest()
    execution_limit = max(1, int(max_items))
    token_limit = max(1, int(max_tokens))
    return RetrievalContributionCallContext(
        actor_id=actor_id,
        notebook_id=call._notebook_id,
        scope_id=scope_id,
        scope_narrowed=bool(getattr(scope, "restricted", False)),
        run_id=run_id,
        run_kind=str(getattr(run, "run_kind", "") or "ask_chunk"),
        cancellation=call._cancellation,
        max_items=execution_limit,
        max_tokens=token_limit,
        max_proposals=execution_limit,
        admission_source=_AdmissionSource(
            call, call._notebook_id, actor_id, admission_hydrate
        ),
        selected_source_graph_source=None,
        connection_probe=connection_probe,
        generated_question_source=(call if generated_question_enabled else None),
    )
