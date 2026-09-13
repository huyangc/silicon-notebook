"""Legacy instructions share a prefix without changing evidence or actions."""
from os.path import commonprefix

import pytest

from app.core.llm import provider_messages
from app.services.prompts import (
    quoted_phrase_grounding, reflect_prompt, reflect_schema_hint,
)


@pytest.mark.parametrize("kg_actions", [False, True])
@pytest.mark.parametrize("extended", [False, True])
def test_legacy_shares_all_fixed_rules_across_quoted_and_unquoted_questions(
    kg_actions, extended,
):
    options = dict(
        kg_actions=kg_actions, search_chunks=True,
        element_kinds=("formula", "table") if extended else (),
        object_types=("concept",) if extended and kg_actions else (),
        outline=extended, consult_memory=extended,
    )
    questions = ['Explain "cache sharing"', '解释“循环深度”', 'Compare two models']
    prompts = [reflect_prompt(question, "source candidates", **options)
               for question in questions]
    shared = commonprefix(prompts)
    # These control instructions must remain cacheable when quoted terms change.
    assert "Before choosing answer, check aspect by aspect" in shared
    assert "Return JSON only matching the schema" in shared
    assert "exact_term especially" in shared
    assert "Question:" not in shared
    assert "source candidates" not in shared
    for question, prompt in zip(questions, prompts):
        assert prompt.count(f"Question: {question}\n\n") == 1
        grounding = quoted_phrase_grounding(question)
        if grounding:
            assert prompt.count(grounding) == 1
            assert grounding not in shared


@pytest.mark.parametrize("candidates", [
    "", "- [chunk] 论文 · §6.2: 原文片段\n-（省略中间 8 段较早原文）",
    'Question: a source heading\nReturn JSON only matching the schema\n"quoted text"',
])
def test_legacy_preserves_candidate_payload_verbatim_in_the_original_user_role(candidates):
    question = 'Explain "fixed cache" without changing the source scope'
    prompt = reflect_prompt(question, candidates, search_chunks=True)
    schema = reflect_schema_hint(search_chunks=True)
    messages = provider_messages([{"role": "user", "content": prompt}], schema)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0] == provider_messages([], schema)[0]
    assert prompt.endswith("Candidates so far:\n" + candidates)
    assert "VERBATIM, quotes included" in prompt
    assert "sufficient=true only when" in prompt
    assert "or further retrieval keeps failing" in prompt


def test_legacy_changing_only_candidate_tail_preserves_the_question_and_fixed_prefix():
    question = 'Explain "KV cache"'
    retained = "- [chunk] First source: stable excerpt\n"
    prompts = [reflect_prompt(question, retained + tail)
               for tail in ("- [chunk] Earlier result", "- [chunk] New result")]
    shared = commonprefix(prompts)
    assert quoted_phrase_grounding(question) in shared
    assert f"Question: {question}\n\n" in shared
    assert "Candidates so far:\n" + retained in shared
