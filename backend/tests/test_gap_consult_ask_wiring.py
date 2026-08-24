"""X9 PR-A T2: ``ask.gap_consult`` wired into reasoning Ask.

T1 proved the host in isolation.  What this file pins is everything between
the host and a user: that the seat actually reaches ``AskService`` through the
production injection chain, that the run decides *when* to consult from its own
output rather than on every request, that what leaves the deployment is a
bounded question plus at most two direction labels and nothing else, and that a
suggestion is attached to the answer without ever becoming part of it.

Two deliberate testing choices, both about what a case can actually prove:

* **Trigger boundaries are exercised against the real limits tables, not
  against a seeded corpus.**  Whether a notebook happens to yield 7 or 9
  evidence items is a property of FakeEmbedder and the seed rows, so an
  end-to-end case straddling ``ranked_final_floor`` would pin the fixture, not
  the rule.  The boundary cases therefore call ``_consult_gap_sources`` with
  real ``ask_retrieval_limits(...)`` objects and a known item count — which is
  what makes "the floor is read per tier" a checkable claim — while separate
  end-to-end cases prove the same method is genuinely reached by a real Ask.

* **The shared-constant case runs the real retriever.**  ``run()`` writes the
  terminal disclosure reason as a literal (its body is pinned at a zero-margin
  line ceiling), so the only honest reconciliation is to make a real run emit
  that step and compare it against the constant the consumer reads.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.domain.gap_consult import (
    GAP_CONSULT_MAX_GAP_PHRASES,
    GAP_CONSULT_MAX_SUGGESTIONS,
    GAP_CONSULT_PHRASE_MAX_CHARS,
    GAP_CONSULT_QUESTION_MAX_CHARS,
    GapSuggestion,
)
from app.extension_sdk import (
    ASK_GAP_CONSULT_POINT,
    EXTENSION_API_VERSION,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
)
from app.extensions.bootstrap import build_extension_runtime
from app.models.ask import (
    AskIntentConfirmation,
    AskRequest,
    QueryIntentContract,
    QueryIntentTopic,
)
from app.models.schemas import NotebookCreate
from app.services.cancellation import AskCancelled
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import (
    RecordingModelProvider,
    bind_all_embedding_clients,
    bind_chat_client,
)


SOURCE_TITLE = "版图设计内部手册ZZZZ"
QUESTION = "RTL 到 GDSII 的完整实现流程有哪些阶段？"


# --------------------------------------------------------------------------
# plugin scaffolding (mirrors test_gap_consult_host.py's ``_Bundle``)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.add_contributor(self.contribution)


def _bundle(implementation: object, contribution_id: str = "corp.gap") -> _Bundle:
    declaration = ContributionDeclaration(
        contribution_id, ASK_GAP_CONSULT_POINT, ContributionKind.CONTRIBUTOR
    )
    return _Bundle(
        ExtensionManifest(
            id=contribution_id,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=contribution_id,
            trust="deployment",
            contributions=(declaration,),
        ),
        ExtensionContribution(declaration, implementation, None),
    )


class _Recorder:
    """Records every context it is handed and answers a canned result."""

    def __init__(self, *items: GapSuggestion) -> None:
        self.items = items
        self.contexts: list[object] = []

    def consult(self, context):
        self.contexts.append(context)
        return ContributorResult(self.items, ExtensionResultStatus.AVAILABLE)

    @property
    def queries(self) -> list[object]:
        return [context.query for context in self.contexts]


SUGGESTION = GapSuggestion(
    "Physical Design Flow Survey",
    "https://example.org/pd-flow.pdf",
    "A survey of RTL-to-GDSII stages.",
    "arXiv",
)


class _SeqLLM:
    """plan -> immediate answer, i.e. the shortest real reasoning run."""

    configured = True

    def __init__(self, answer: str = "流程见 [k1]。") -> None:
        self._answer = answer
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.prompts.append(messages[-1]["content"])
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "RTL 到 GDSII"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": self._answer, "grounded": True})


@pytest.fixture
def make_repo(tmp_path, monkeypatch):
    """Build a real SQLiteRepository whose gap-consult seat we control.

    The host travels the SAME kwarg chain production uses
    (``SQLiteRepository`` -> ``RepositoryFacade`` -> ``RepositoryRuntime`` ->
    ``ask_service()``), so a break anywhere along it shows up here as a plugin
    that is simply never called.
    """
    made: list[SQLiteRepository] = []

    def _make(*bundles, host="build", llm=None, **env):
        index = len(made)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f't{index}.db'}")
        monkeypatch.setenv(
            "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / f"s{index}")
        )
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        monkeypatch.setenv("EMBED_DIM", "16")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        seat = (
            build_extension_runtime(bundles).gap_consult
            if host == "build"
            else host
        )
        repo = SQLiteRepository(
            Settings(_env_file=None),
            model_provider=RecordingModelProvider(),
            gap_consult_host=seat,
        )
        bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
        repo.settings.graph_ppr_enabled = False
        for workload_id in ("reasoning_agent", "evidence_refine", "ask_answer",
                            "query_rewrite", "knowhow_complete"):
            bind_chat_client(repo, workload_id, llm or _SeqLLM())
        made.append(repo)
        return repo

    return _make


def _seed(repo):
    notebook = repo.create_notebook(NotebookCreate(name="设计库"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("src-a", notebook.id, SOURCE_TITLE, "md", "ready", now, now),
        )
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"},
         "evidence": []},
    ], [])
    return notebook


def _ask(repo, notebook, **kwargs):
    return repo.ask(
        notebook.id,
        AskRequest(question=QUESTION, mode="reasoning", **kwargs),
    )


def _many_topics_intent(count: int) -> AskIntentConfirmation:
    """A confirmed intent with more directions than an ``overview`` run can
    execute, which is what makes the terminal disclosure step appear."""
    contract = QueryIntentContract(
        objective=QUESTION,
        resolved_question=QUESTION,
        mandatory_topics=[
            QueryIntentTopic(
                id=f"t{index}",
                title=f"主题{index}",
                question=f"阶段{index}的关键步骤是什么",
            )
            for index in range(count)
        ],
        needs_clarification=False,
        confirmed=True,
    )
    return AskIntentConfirmation(
        contract=contract, resolved_question=QUESTION, answers=[]
    )


# Two runs of the "no plugin" baseline necessarily use two databases and two
# wall clocks, so generated handles, completion instants and per-step durations
# differ by construction.  Blanking exactly those three — and nothing else —
# keeps the rest of the comparison a byte comparison.
_VOLATILE = re.compile(
    r"[a-z]{2,10}-[0-9a-f]{12,}"
    r"|\d{4}-\d\d-\d\dT[\d:.+\-]+"
    r"|(?<=\"duration_ms\": )\d+"
)


def _stable(payload: dict) -> str:
    return _VOLATILE.sub(
        "<volatile>", json.dumps(payload, sort_keys=True, ensure_ascii=False)
    )


def _gap_step(response):
    steps = [
        step for step in (response.reasoning_trace or [])
        if step.step_type == "gap_consult"
    ]
    return steps[0] if len(steps) == 1 else (None if not steps else steps)


# --------------------------------------------------------------------------
# the off / dormant baseline
# --------------------------------------------------------------------------
def test_zero_plugin_answer_is_byte_identical(make_repo):
    """Two ways of having no gap-consult plugin must both be invisible.

    ``None`` is an unwired composition root (a direct-constructor test, a CLI);
    a frozen host with zero contributions is the *shipped* deployment, since
    this point has no built-in contribution at all.  Neither may add a field,
    a trace step, or a byte to the serialized answer.
    """
    baselines = []
    for host in (None, "build"):
        repo = make_repo(host=host)
        notebook = _seed(repo)
        response = _ask(repo, notebook)
        assert response.gap_suggestions == []
        assert _gap_step(response) is None
        payload = json.loads(
            response.model_dump_json(exclude={"answer_id", "conversation_id"})
        )
        assert "gap_suggestions" not in payload
        baselines.append(payload)

    assert sorted(baselines[0]) == sorted(baselines[1])
    assert _stable(baselines[0]) == _stable(baselines[1])


# --------------------------------------------------------------------------
# triggers
# --------------------------------------------------------------------------
def test_trigger_on_uncovered_directions(make_repo):
    """A confirmed direction the run never executed is a gap worth asking about.

    The phrases handed outward are exactly the terminal disclosure step's own
    labels, bounded to the egress rail — never the ``uncovered_intent_queries``
    elements themselves, which carry the whole confirmed intent contract.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)

    response = _ask(
        repo, notebook,
        retrieval_effort="overview", intent=_many_topics_intent(6),
    )

    skips = [
        step for step in response.reasoning_trace or []
        if step.step_type == "skip"
        and (step.detail or {}).get("reason") == "intent_coverage_incomplete"
    ]
    assert len(skips) == 1, "这一轮必须真的留下未执行方向"
    expected = skips[0].detail["directions"][:GAP_CONSULT_MAX_GAP_PHRASES]
    assert expected, "披露步必须逐条列出方向简称"

    (query,) = plugin.queries
    assert list(query.gaps) == expected
    assert len(query.gaps) <= GAP_CONSULT_MAX_GAP_PHRASES
    assert response.gap_suggestions[0].url == SUGGESTION.url


def test_trigger_on_thin_evidence_below_the_tier_floor(make_repo):
    """The thin-evidence trigger reads THIS effort tier's own floor.

    Ten evidence items are above ``overview``'s floor and below ``standard``'s,
    so a hardcoded number — whichever one — flips exactly one of these two.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    service = repo._runtime.ask_service()
    prepared = _PreparedStub()

    overview = ask_retrieval_limits("overview")
    standard = ask_retrieval_limits("standard")
    assert overview.ranked_final_floor <= 10 < standard.ranked_final_floor

    covered = [object()] * 10
    assert service._consult_gap_sources(
        prepared, overview, [], top_hits=covered, chunks=[], elements=[],
        cancellation=None, on_step=None,
    ) == ()
    assert plugin.contexts == []

    admitted = service._consult_gap_sources(
        prepared, standard, [], top_hits=covered, chunks=[], elements=[],
        cancellation=None, on_step=None,
    )
    assert [item.url for item in admitted] == [SUGGESTION.url]

    # The count is the SUM of the three evidence planes, not just top_hits.
    plugin.contexts.clear()
    assert service._consult_gap_sources(
        prepared, overview, [],
        top_hits=covered[:4], chunks=covered[:4], elements=covered[:4],
        cancellation=None, on_step=None,
    ) == ()
    assert plugin.contexts == []


def test_no_trigger_when_covered_and_above_floor(make_repo):
    """Neither condition holding means no consultation and no trace step.

    This is the common case for a healthy notebook, and it has to cost nothing:
    no plugin call, no event, no step for the reader to wonder about.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    service = repo._runtime.ask_service()
    trace: list = []

    assert service._consult_gap_sources(
        _PreparedStub(), ask_retrieval_limits("overview"), trace,
        top_hits=[object()] * 30, chunks=[], elements=[],
        cancellation=None, on_step=None,
    ) == ()
    assert plugin.contexts == []
    assert trace == []


# --------------------------------------------------------------------------
# what leaves the deployment
# --------------------------------------------------------------------------
class _PreparedStub:
    """The two fields ``_egress_question`` reads, plus the one it must not.

    ``research_question`` is present precisely because it is NOT a candidate:
    it is the intent contract's composite (objective + topics + constraints +
    assumptions), so a case that never carries it could not tell the difference
    between "skipped" and "absent".
    """

    def __init__(
        self,
        question: str = QUESTION,
        resolved: str = "",
        research: str = "",
    ) -> None:
        self.question = question
        self.research_question = research
        self.intent_projection = type(
            "_Projection", (), {"resolved_question": resolved}
        )()


def test_egress_payload_carries_nothing_else(make_repo):
    """One bounded question, at most two short labels — audit is one object.

    The negative half names things that DO exist in this run and are exactly
    what a leak would look like: the notebook id, the asking user's id, and a
    source title only this deployment has.
    """
    plugin = _Recorder()
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)
    user_id = repo.current_user().id

    _ask(repo, notebook, retrieval_effort="overview",
         intent=_many_topics_intent(6))

    (query,) = plugin.queries
    assert set(type(query).__dataclass_fields__) == {
        "question", "gaps", "max_suggestions",
    }
    assert query.max_suggestions == GAP_CONSULT_MAX_SUGGESTIONS
    rendered = repr(query)
    for secret in (notebook.id, user_id, SOURCE_TITLE, "RTL到GDSII流程概述"):
        assert secret not in rendered, secret
    assert QUESTION[:10] in query.question


def test_egress_strings_are_bounded_and_marker_free(make_repo):
    """Bounds and marker-stripping are the host's INPUT contract, so they are
    applied on the way out, not hoped for from the caller.

    ``[k3]`` names a server-owned evidence key; outside the deployment it is
    meaningless at best and a correlatable handle at worst.
    """
    from app.services.ask_service import (
        _egress_question,
        _uncovered_directions_from_trace,
    )
    from app.models.ask import TraceStep

    long_question = "阶[k1]段" * 400
    question = _egress_question(_PreparedStub(question=long_question))
    assert len(question) == GAP_CONSULT_QUESTION_MAX_CHARS
    assert "[k1]" not in question and "k1" not in question

    gaps = _uncovered_directions_from_trace([
        TraceStep(
            step_type="skip",
            summary="x",
            detail={
                "reason": "intent_coverage_incomplete",
                "directions": [
                    "方向一【k2，k3】收尾  \n 换行",
                    "方向一 收尾 换行",
                    "长" * 200,
                    "方向二",
                ],
            },
        ),
    ])
    # A marker becomes a SPACE, never nothing: gluing "方向一" onto "收尾"
    # would invent a token the user never wrote.  Whitespace then collapses,
    # which makes the second entry an exact duplicate and drops it; the third
    # is bounded per phrase; the fourth is past the count bound.
    assert gaps[0] == "方向一 收尾 换行"
    assert gaps[1] == "长" * GAP_CONSULT_PHRASE_MAX_CHARS
    assert len(gaps) == GAP_CONSULT_MAX_GAP_PHRASES
    assert all(len(phrase) <= GAP_CONSULT_PHRASE_MAX_CHARS for phrase in gaps)
    assert all("k2" not in phrase for phrase in gaps)


def test_egress_question_truncation_is_the_privacy_bound_not_data_loss(
    make_repo,
):
    """Both halves of the rejected review point (codex #584 R1 P2), pinned
    together in one run.

    ``GAP_CONSULT_QUESTION_MAX_CHARS`` cuts the *retrieval hint* handed to a
    third party — egress minimization.  The "user data must not be silently
    truncated" rail governs write and render paths, and this case is the
    standing proof that this function is on neither: the very run that sends a
    300-character prefix outward stores and replays the question in full.
    Whichever half regresses — an unbounded egress string, or a truncated
    question reaching storage — lands here.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)
    # No whitespace and no `[k]` markers, so the only transformation that could
    # shorten this is the egress rail itself.
    long_question = "为什么" + "甲" * 900

    response = repo.ask(
        notebook.id, AskRequest(question=long_question, mode="reasoning")
    )
    assert response.gap_suggestions, "这一轮必须真的发生了外扩询问"

    # Outward: exactly the prefix, and no more of the user's words than that.
    (query,) = plugin.queries
    assert query.question == long_question[:GAP_CONSULT_QUESTION_MAX_CHARS]
    assert len(query.question) == GAP_CONSULT_QUESTION_MAX_CHARS

    # Inward: untouched everywhere it is persisted or replayed to the reader.
    with repo._connect() as db:
        row = db.execute(
            "SELECT question FROM answers WHERE id=?", (response.answer_id,)
        ).fetchone()
    assert row["question"] == long_question
    turn = repo.get_conversation(response.conversation_id).turns[0]
    assert turn.question == long_question


def test_egress_question_is_two_steps_and_skips_the_composite():
    """Both candidates are the user's OWN words; the composite is not one.

    ``research_question`` is the intent contract concatenated into a single
    string — the same shape ``_uncovered_directions_from_trace`` refuses to
    send outward — so it must not be reachable from either step, including the
    step that runs when the reviewed wording is empty.
    """
    from app.services.ask_service import _egress_question

    composite = "目标：X 必答主题：A;B 约束：C 假设：D"
    assert _egress_question(_PreparedStub(
        question="原始问题", resolved="审阅后问题", research=composite,
    )) == "审阅后问题"

    # A reviewed wording that is nothing but markers strips to empty, and the
    # fallback is the RAW question — never the composite standing between them.
    fell_back = _egress_question(_PreparedStub(
        question="原始问题", resolved="[k1]【k2】", research=composite,
    ))
    assert fell_back == "原始问题"
    assert composite not in fell_back


def test_only_the_terminal_disclosure_step_decides_the_gaps():
    """A later disclosure supersedes an earlier one; it is not unioned in.

    ``run()`` recomputes what stayed uncovered at the END of its reflect loop.
    Reading the earliest matching step would hand a third party the directions
    the run went on to execute after that earlier account was written.
    """
    from app.models.ask import TraceStep
    from app.services.ask_service import _uncovered_directions_from_trace

    def _step(*directions: str) -> TraceStep:
        return TraceStep(
            step_type="skip",
            summary="x",
            detail={
                "reason": "intent_coverage_incomplete",
                "directions": list(directions),
            },
        )

    gaps = _uncovered_directions_from_trace([
        _step("早期方向一", "早期方向二"),
        TraceStep(step_type="retrieve", summary="y", detail={}),
        _step("终态方向"),
    ])
    assert gaps == ("终态方向",)


def test_terminal_disclosure_reason_is_the_shared_constant(make_repo):
    """``run()`` writes the reason as a literal; the consumer reads a constant.

    That duplication is deliberate (``run``'s body is pinned at a zero-margin
    line ceiling), so this is the reconciliation: a REAL run must emit exactly
    the value the trigger looks for.  Change either side alone and the gap
    trigger silently stops firing — no error, just a feature that quietly
    never happens again.
    """
    from app.services.reasoning_retrieval import (
        INTENT_COVERAGE_INCOMPLETE_REASON,
    )

    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)

    response = _ask(repo, notebook, retrieval_effort="overview",
                    intent=_many_topics_intent(6))

    reasons = {
        (step.detail or {}).get("reason")
        for step in response.reasoning_trace or []
        if step.step_type == "skip"
    }
    assert INTENT_COVERAGE_INCOMPLETE_REASON in reasons
    assert plugin.contexts, "对不上这个值,触发判据就永远为假"


# --------------------------------------------------------------------------
# fail-open
# --------------------------------------------------------------------------
def _answer_without_plugin(make_repo):
    repo = make_repo(host=None)
    notebook = _seed(repo)
    response = _ask(repo, notebook)
    return json.loads(
        response.model_dump_json(exclude={"answer_id", "conversation_id"})
    )


def _assert_answer_survived(response, baseline):
    payload = json.loads(
        response.model_dump_json(exclude={"answer_id", "conversation_id"})
    )
    assert response.gap_suggestions == []
    assert "gap_suggestions" not in payload
    assert payload["conclusion"] == baseline["conclusion"]
    assert payload["answer"] == baseline["answer"]
    assert payload["evidence_level"] == baseline["evidence_level"]
    assert len(payload["citations"]) == len(baseline["citations"])


def test_timeout_leaves_the_answer_verbatim(make_repo):
    """A hung plugin costs its own budget and nothing else.

    The wall-clock assertion is the point: without the host's sliced join this
    request would block for the plugin's full 30s and the reader would simply
    never get an answer.
    """
    baseline = _answer_without_plugin(make_repo)
    release = threading.Event()

    class Hung:
        def consult(self, _context):
            release.wait(30.0)
            return ContributorResult((SUGGESTION,), ExtensionResultStatus.AVAILABLE)

    repo = make_repo(_bundle(Hung()), ASK_GAP_CONSULT_TIMEOUT_SECONDS="0.2")
    notebook = _seed(repo)
    assert repo.settings.ask_gap_consult_timeout_seconds == 0.2
    started = time.monotonic()
    try:
        response = _ask(repo, notebook)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    _assert_answer_survived(response, baseline)
    # The plugin would block for 30s; the whole Ask has to come back in a
    # fraction of that, or the sliced join is not doing its job.
    assert elapsed < 5.0, elapsed


def test_plugin_raises_leaves_the_answer_verbatim(make_repo):
    baseline = _answer_without_plugin(make_repo)

    class Raising:
        def consult(self, _context):
            raise RuntimeError("upstream is down")

    repo = make_repo(_bundle(Raising()))
    _assert_answer_survived(_ask(repo, _seed(repo)), baseline)


def test_malformed_result_leaves_the_answer_verbatim(make_repo):
    baseline = _answer_without_plugin(make_repo)

    class Malformed:
        def consult(self, _context):
            return {"suggestions": ["https://example.org/nope"]}

    repo = make_repo(_bundle(Malformed()))
    _assert_answer_survived(_ask(repo, _seed(repo)), baseline)


def test_a_misbehaving_host_still_leaves_the_answer_verbatim(make_repo):
    """Defence in depth: the host itself fails open per contributor, so the
    service's own ``except Exception`` only fires if the host misbehaves.  A
    gap suggestion is worth strictly less than the answer it accompanies."""
    baseline = _answer_without_plugin(make_repo)

    class BrokenHost:
        def has_contributions(self):
            return True

        def consult(self, _call_context, **_kwargs):
            raise RuntimeError("host itself is broken")

    repo = make_repo(host=BrokenHost())
    _assert_answer_survived(_ask(repo, _seed(repo)), baseline)


@pytest.mark.parametrize(
    "answer, label",
    [
        ([{"url": "https://example.org/nope"}], "dict 列表"),
        (None, "None"),
        (
            (GapSuggestion("题" * 500, "https://example.org/long.pdf"),),
            "超长 title",
        ),
        (
            (SimpleNamespace(
                title="鸭子类型", url="https://example.org/duck.pdf",
                summary="", source_label="",
            ),),
            "鸭子类型条目",
        ),
        (
            tuple(
                GapSuggestion(f"paper {i}", f"https://example.org/{i}.pdf")
                for i in range(GAP_CONSULT_MAX_SUGGESTIONS + 1)
            ),
            "超额批",
        ),
    ],
)
def test_a_host_answering_the_wrong_shape_is_dropped_as_a_batch(
    make_repo, answer, label
):
    """The seat is public, so "the host is well behaved" is an assumption.

    ``gap_consult_host=`` is threaded through five files and accepts whatever
    it is given; the frozen host sanitizes its contributors, but nothing
    sanitizes the host.  All five shapes here are ones a plausible injected
    implementation produces, and they split across the halves of the rail:
    the list of dicts, the ``None`` and the well-typed item whose title is past
    the wire rail all raise (the last one inside pydantic during CONVERSION,
    not inside ``consult`` — which is why the guard has to span the conversion
    too), while the duck-typed item raises nothing at all and is refused only
    because whole-batch admission compares its TYPE, and the over-cap batch of
    individually valid items is refused by the O(1) length check that also
    bounds the per-item scan (codex #584 R2).  Each must cost the batch and
    nothing else: the answer stays verbatim and the step is still recorded,
    because the run really did consult and the reader is owed that fact.
    """
    baseline = _answer_without_plugin(make_repo)

    class _WrongShape:
        def has_contributions(self):
            return True

        def consult(self, _call_context, **_kwargs):
            return answer

    repo = make_repo(host=_WrongShape())
    response = _ask(repo, _seed(repo))

    _assert_answer_survived(response, baseline)
    step = _gap_step(response)
    assert step is not None, f"{label}: 外扩确实发生过,步不能消失"
    assert step.detail["count"] == 0


def test_a_degraded_retrieval_still_consults_and_keeps_its_answer(
    make_repo, monkeypatch
):
    """Retrieval failing open is a REGISTERED trigger path (plan risk R4).

    ``_run_reasoning_stage`` catches a blown-up retrieval stage and continues
    with empty evidence, which lands under every tier's floor — so the thin
    branch fires on a run whose notebook may be perfectly well stocked.  That
    is deliberate (the run genuinely has nothing to answer from), and it is
    why the step's wording must not blame the corpus.  Pin all three halves:
    the consultation happens exactly once, the degraded answer is untouched,
    and the step is there.
    """
    from app.application import ask_reasoning

    def _blow_up(*_args, **_kwargs):
        raise RuntimeError("retrieval is down")

    monkeypatch.setattr(
        ask_reasoning, "execute_reasoning_retrieval_stage", _blow_up
    )

    bare = make_repo(host=None)
    degraded = json.loads(
        _ask(bare, _seed(bare)).model_dump_json(
            exclude={"answer_id", "conversation_id"}
        )
    )

    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    response = _ask(repo, _seed(repo))

    assert len(plugin.contexts) == 1, "降级路径必须恰好外扩一次"
    payload = json.loads(
        response.model_dump_json(exclude={"answer_id", "conversation_id"})
    )
    assert payload["conclusion"] == degraded["conclusion"]
    assert payload["answer"] == degraded["answer"]
    assert payload["evidence_level"] == degraded["evidence_level"]
    assert [item.url for item in response.gap_suggestions] == [SUGGESTION.url]

    step = _gap_step(response)
    assert step is not None and step.detail["reason"] == "thin_evidence"
    # The corpus is not what failed here, so the reader must not be told it is.
    assert "库内证据偏少" not in step.summary


def test_cancellation_during_consult_propagates(make_repo):
    """Cancellation is the caller's own signal — the one thing NOT failed open.

    Swallowing it here would let a cancelled run keep drafting an answer past
    the point the user asked it to stop.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    service = repo._runtime.ask_service()

    class _Cancelled:
        def is_set(self):
            return True

    with pytest.raises(AskCancelled):
        service._consult_gap_sources(
            _PreparedStub(), ask_retrieval_limits("standard"), [],
            top_hits=[], chunks=[], elements=[],
            cancellation=_Cancelled(), on_step=None,
        )
    assert plugin.contexts == [], "取消必须先于任何外发"


# --------------------------------------------------------------------------
# scope: reasoning Ask only
# --------------------------------------------------------------------------
def test_report_and_knowhow_paths_never_consult(make_repo):
    """Both other ``ReasoningRetriever`` consumers are structurally excluded.

    Deep-report sections and Knowhow completion build a retriever directly and
    never enter ``_run_reasoning_stage``, which is where the consultation
    lives.  Running both for real is what turns that from an argument into a
    check: a future refactor that routed either through the Ask orchestrator
    would start sending their queries to a third party, and nothing else in the
    suite would notice.
    """
    from app.services.knowhow import api as knowhow_api
    from app.services.report_engine import ReportEngine

    plugin = _Recorder(SUGGESTION)
    llm = _SeqLLM()
    repo = make_repo(_bundle(plugin), llm=llm)
    notebook = _seed(repo)
    for workload_id in ("report_outline", "report_sufficiency", "report_section",
                        "report_summary"):
        bind_chat_client(repo, workload_id, llm)

    ReportEngine.from_repository(repo, repo.settings)._deep_dive(
        notebook.id,
        {"title": "A", "scope": "s", "sub_queries": ["RTL 到 GDSII"]},
        "报告问题", depth=2,
    )
    assert plugin.contexts == [], "报告逐节深挖不得触发外扩"

    class _CompletionLLM(_SeqLLM):
        def chat_json(self, messages, schema_hint, **kwargs):
            if "suggestions" in schema_hint:
                return json.dumps({"suggestions": [
                    {"column_id": "cause", "abstain": True, "reason": "证据不足"},
                ]})
            return super().chat_json(messages, schema_hint, **kwargs)

    bind_chat_client(repo, "knowhow_complete", _CompletionLLM())
    knowhow_api.complete_row(repo, notebook.id, {
        "id": "table",
        "anchor_column_id": "anchor",
        "columns": [
            {"id": "anchor", "name": "场景", "kind": "attribute", "position": 0},
            {"id": "symptom", "name": "现象", "kind": "attribute", "position": 1},
            {"id": "cause", "name": "根因", "kind": "attribute", "position": 2},
        ],
        "rows": [{"id": "current", "position": 0,
                  "cells": {"anchor": "时序", "symptom": "setup violation"}}],
    }, "current", ["cause"])
    assert plugin.contexts == [], "Knowhow 智能补全不得触发外扩"


# --------------------------------------------------------------------------
# it is not evidence, and it survives the round trip
# --------------------------------------------------------------------------
def test_suggestions_are_not_evidence(make_repo):
    """Attached to the answer, absent from everything that IS evidence.

    The synthesis prompt assertion is the load-bearing one: it proves the
    answering model never saw a suggestion, so no wording in ``answer`` can
    have come from one.
    """
    plugin = _Recorder(SUGGESTION)
    llm = _SeqLLM()
    repo = make_repo(_bundle(plugin), llm=llm)
    notebook = _seed(repo)

    response = _ask(repo, notebook)
    assert [item.url for item in response.gap_suggestions] == [SUGGESTION.url]

    for prompt in llm.prompts:
        assert SUGGESTION.url not in prompt
        assert SUGGESTION.title not in prompt
    assert all(SUGGESTION.url not in (a.snippet or "") for a in response.anchors)
    assert all(
        SUGGESTION.url not in (c.snippet or "") for c in response.citations
    )
    # No citation key was minted for it, so it cannot be referenced from prose.
    assert SUGGESTION.url not in response.answer
    assert SUGGESTION.url not in response.conclusion


def test_trace_step_detail_is_content_free(make_repo):
    """The step discloses that a consultation happened, never what was asked.

    ``reasoning_trace`` is persisted with the turn and rendered to the reader,
    so a gap phrase or a suggestion title in ``detail`` would outlive the run.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)

    response = _ask(repo, notebook, retrieval_effort="overview",
                    intent=_many_topics_intent(6))
    step = _gap_step(response)
    assert step is not None
    assert set(step.detail) == {"reason", "count", "gaps"}
    assert step.detail["reason"] in {"uncovered_directions", "thin_evidence"}
    assert step.detail["count"] == 1
    assert isinstance(step.detail["gaps"], int)
    assert step.duration_ms is not None and step.duration_ms >= 0

    rendered = json.dumps(
        {"summary": step.summary, "detail": step.detail}, ensure_ascii=False
    )
    for secret in (SUGGESTION.title, SUGGESTION.url, SUGGESTION.source_label,
                   "阶段0的关键步骤是什么", SOURCE_TITLE):
        assert secret not in rendered, secret


def test_suggestions_survive_persistence_and_reopen(make_repo):
    """Persistence is free (``save_answer`` dumps the whole model) — but only
    while the field stays on ``AskResponse``, so pin the round trip.  A legacy
    payload written before the field existed must reopen as an empty list."""
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)
    response = _ask(repo, notebook)
    assert response.gap_suggestions

    with repo._connect() as db:
        row = db.execute(
            "SELECT payload FROM answers WHERE id=?", (response.answer_id,)
        ).fetchone()
    stored = json.loads(row["payload"])
    assert stored["gap_suggestions"][0]["url"] == SUGGESTION.url

    turn = repo.get_conversation(response.conversation_id).turns[0]
    assert [item.url for item in turn.response.gap_suggestions] == [SUGGESTION.url]

    legacy = "conv-legacy-gap"
    when = "2026-07-29T09:05:00+08:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (legacy, notebook.id, "legacy", repo.current_user().id, when, when),
        )
        db.execute(
            "INSERT INTO answers "
            "(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            ("ans-legacy-gap", notebook.id, "q",
             json.dumps({"conclusion": "old answer"}), when, legacy),
        )
    assert repo.get_conversation(legacy).turns[0].response.gap_suggestions == []


def test_an_injected_draft_stage_cannot_drop_the_suggestions(make_repo):
    """Core fills the field AFTER the draft stage, mirroring ``model_errors``.

    A ``ResponseDraftStage`` is injectable, and one that builds its own bare
    ``AskResponse`` (a plausible custom implementation) must not be able to
    silently erase a disclosure the core decided to make.
    """
    from app.application.ask_reasoning import ReasoningResponseDraft
    from app.models.ask import AskResponse

    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))
    notebook = _seed(repo)
    service = repo._runtime.ask_service()

    class _BareStage:
        def draft_response(self, stage, _runtime):
            prepared = stage.prepared
            return ReasoningResponseDraft(
                notebook_id=prepared.notebook_id,
                question=prepared.question,
                response=AskResponse(
                    conclusion="注入实现自己拼的答案", answer="",
                    mode="reasoning",
                    conversation_id=prepared.conversation_id,
                ),
                conversation_id=prepared.conversation_id,
                user_id=prepared.user_id,
                job_id=prepared.job_id,
                asked_at=prepared.asked_at,
            )

    service.response_draft_stage = _BareStage()
    response = _ask(repo, notebook)

    assert response.conclusion == "注入实现自己拼的答案"
    assert [item.url for item in response.gap_suggestions] == [SUGGESTION.url]


def test_the_production_injection_chain_reaches_the_ask_service(make_repo):
    """The seat is threaded, not merely accepted.

    Five files pass this host along; each of them silently defaulting to
    ``None`` still constructs a perfectly working repository, so the only
    symptom of a dropped link is a feature that never runs.
    """
    plugin = _Recorder(SUGGESTION)
    repo = make_repo(_bundle(plugin))

    host = repo._runtime.gap_consult
    assert host is not None and host.has_contributions() is True
    assert repo._runtime.ask_service().gap_consult_host is host


def test_the_factory_hop_is_covered_too(tmp_path):
    """``create_repository`` is the hop the direct-constructor cases skip.

    Every other case in this file builds ``SQLiteRepository`` itself, so the
    factory's two forwarding lines are the one link in the chain nothing
    exercises — and deleting them leaves a repository that constructs fine and
    silently never consults.  ``app.bootstrap`` reaches the seat only through
    here, so this is the production path, not a variant of it.
    """
    from app.repositories.factory import create_repository

    host = build_extension_runtime((_bundle(_Recorder(SUGGESTION)),)).gap_consult
    repository = create_repository(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'factory-gap.db'}",
            storage_dir=str(tmp_path / "factory-storage"),
            event_log_enabled=False,
            llm_log_enabled=False,
        ),
        gap_consult_host=host,
    )
    try:
        assert repository._runtime.gap_consult is host
        assert repository._runtime.ask_service().gap_consult_host is host
    finally:
        repository.close()


def test_the_consultation_counts_post_activation_evidence(make_repo, monkeypatch):
    """The evidence the trigger weighs is what graph activation *produced*.

    Selected-source-graph activation appends approved G chunks after the frozen
    baseline, and those are real evidence: counting the pre-activation list
    would let a run that did find enough material still report itself thin and
    go ask a third party about it.

    Activation is inert in an unconfigured fixture (same list in, same list
    out), so identity alone proves nothing — hence the sentinel: the only way
    the consultation can be handed THIS object is by reading the variable
    activation rebound.
    """
    from app.services.ask_service import AskService

    sentinel: list = []
    monkeypatch.setattr(
        AskService,
        "_activate_selected_source_graph",
        lambda self, notebook_id, chunks, **kwargs: (sentinel, None),
    )
    seen: dict = {}
    original = AskService._consult_gap_sources

    def _spy(self, prepared, limits, trace, *, chunks, **kwargs):
        seen["chunks"] = chunks
        return original(self, prepared, limits, trace, chunks=chunks, **kwargs)

    monkeypatch.setattr(AskService, "_consult_gap_sources", _spy)

    repo = make_repo(_bundle(_Recorder(SUGGESTION)))
    _ask(repo, _seed(repo))

    assert seen.get("chunks") is sentinel
