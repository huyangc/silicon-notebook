"""Hand-transcribed copy of ``provider_messages`` as it read at commit
``a61e81bb5`` (``backend/app/core/llm.py``) — the commit immediately before
the E1 marker seam (T-EX2) was added, i.e. before ``provider_messages`` grew
its ``markers`` parameter.

This file exists so ``test_provider_messages_matches_the_pre_seam_fixture`` in
``backend/tests/test_llm_client.py`` can compare the current, seam-bearing
``provider_messages`` against an INDEPENDENT copy of the pre-seam expression,
without depending on ``a61e81bb5`` staying reachable in git history. That
commit lives on a feature branch; once it merges (a squash merge in
particular) a shallow OR full clone can lose it, which previously turned that
guard into a permanent ``pytest.skip``. A file checked into the repo cannot go
unreachable the way a commit can.

Do NOT "fix" this module to track a later ``provider_messages`` — that would
defeat the guard it backs, which exists to catch exactly that kind of silent
drift between the closed-seam behaviour and what the function used to do.
"""

#: Byte-identical to ``_PROVIDER_WRAPPER_PREFIX`` / ``_PRE_SEAM_WRAPPER`` in
#: ``app/core/llm.py`` / ``backend/tests/test_llm_client.py`` as of the
#: pre-seam commit. Spelled out again here, independently, for the same
#: reason those two do: a reworded wrapper must fail this comparison too,
#: rather than all copies drifting together.
_PRE_SEAM_WRAPPER = (
    "You are the extraction and reasoning engine for "
    "silicon-notebook. Return valid JSON only, no markdown fences. "
    "Schema hint: "
)


def provider_messages(messages, response_schema_hint):
    """``provider_messages(messages, response_schema_hint)`` before the E1
    marker seam existed — no ``markers`` parameter, no marker branch."""
    return [
        {
            "role": "system",
            "content": f"{_PRE_SEAM_WRAPPER}{response_schema_hint}",
        },
        *messages,
    ]
