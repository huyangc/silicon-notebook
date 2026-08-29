"""Byte-equivalence and registry-invariant tests for the L0/L1/L2 prompt
layering refactor (see ``app.services.prompt_layers``).

Section 1 re-renders the exact same calls the frozen snapshots under
``backend/tests/fixtures/prompt_layer_snapshots/`` were generated from (by
``scripts/generate_prompt_layer_snapshots.py``, run against pre-refactor
``prompts.py``) and asserts byte-for-byte equality — this is the "zero
behavior change" acceptance criterion for the whole refactor. Never
regenerate those fixtures from the refactored code: that would make this
test compare the refactor's output against itself. The case dict itself is
loaded straight from the generator script (see ``_load_snapshot_cases``)
rather than re-declared here, so there is exactly one place that knows what
each snapshot case renders.
"""
import ast
import importlib.util
import inspect
import re
from pathlib import Path

import pytest

from app.services import prompts
from app.services.prompt_layers import L1_FRAGMENTS, L2_BLOCKS, fragment_text

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
FIXTURES_DIR = TESTS_DIR / "fixtures" / "prompt_layer_snapshots"
GENERATOR_SCRIPT = REPO_ROOT / "scripts" / "generate_prompt_layer_snapshots.py"


def _load_snapshot_cases():
    spec = importlib.util.spec_from_file_location(
        "generate_prompt_layer_snapshots", GENERATOR_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._cases()


CASES = _load_snapshot_cases()


def _read_fixture(case: str) -> str:
    return (FIXTURES_DIR / f"{case}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Snapshot byte-equivalence: every case the fixtures were generated from.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("case", sorted(CASES))
def test_prompt_rendering_matches_frozen_snapshot(case):
    rendered = CASES[case]()
    expected = _read_fixture(case)
    assert rendered == expected, (
        f"{case!r} rendering diverged from the frozen pre-refactor snapshot "
        "— the L0/L1/L2 layering refactor must be zero-behavior-change"
    )


def test_all_fixture_files_are_covered_by_a_case():
    fixture_names = {p.stem for p in FIXTURES_DIR.glob("*.txt")}
    assert fixture_names == set(CASES)


# --------------------------------------------------------------------------- #
# 2. L1_FRAGMENTS naming invariants.
# --------------------------------------------------------------------------- #

# A fragment_id's prefix does not always spell the full function name
# (``intent`` for ``query_intent_prompt``; ``report`` shared by two
# ``report_storm_outline_prompt`` fragments) — this table is the single
# explicit place that says which prompt_id each currently-used prefix means,
# so the naming test below is checking a real mapping, not just "has a dot".
_PREFIX_TO_PROMPT_ID = {
    "answer": "answer_prompt",
    "expand_query": "expand_query_prompt",
    "intent": "query_intent_prompt",
    "report": "report_storm_outline_prompt",
    "report_section": "report_section_prompt",
}

# The exact, closed set of fragment ids this refactor introduced. A new
# fragment (or a rename) must update this set deliberately, in the same
# diff — it is not something a future PR should be able to drift past.
_EXPECTED_FRAGMENT_IDS = {
    "answer.style_language",
    "answer.mechanism_organization",
    "answer.domain_conventions",
    "answer.numeric_attribution",
    "expand_query.decomposition_guidance",
    "intent.cross_tool_mapping",
    "report.storm_lenses",
    "report.frame_example",
    "report_section.domain_conventions",
}


def test_fragment_ids_are_exactly_the_nine_expected_ids():
    assert set(L1_FRAGMENTS) == _EXPECTED_FRAGMENT_IDS


def test_fragment_ids_follow_prompt_dot_slug_naming_with_known_prefix():
    for fragment_id, fragment in L1_FRAGMENTS.items():
        assert "." in fragment_id, f"{fragment_id!r} is not <prompt>.<slug>"
        prefix, slug = fragment_id.split(".", 1)
        assert prefix and slug, fragment_id
        assert prefix in _PREFIX_TO_PROMPT_ID, (
            f"{fragment_id!r}'s prefix {prefix!r} is not registered in "
            "_PREFIX_TO_PROMPT_ID — add it there rather than leaving the "
            "prefix unverified"
        )
        assert _PREFIX_TO_PROMPT_ID[prefix] == fragment.prompt_id, (
            f"{fragment_id!r}'s prefix {prefix!r} is registered as meaning "
            f"{_PREFIX_TO_PROMPT_ID[prefix]!r}, but the fragment's own "
            f"prompt_id is {fragment.prompt_id!r}"
        )


def test_fragment_text_and_boundary_are_nonempty():
    for fragment_id, fragment in L1_FRAGMENTS.items():
        assert fragment.text, f"{fragment_id!r} has empty text"
        assert fragment.boundary, f"{fragment_id!r} has empty boundary"
        assert fragment.layer == "L1"
        assert fragment.fragment_id == fragment_id


def test_l1_fragment_count_is_nine():
    assert len(L1_FRAGMENTS) == 9


# --------------------------------------------------------------------------- #
# 2b. L1_FRAGMENTS shape contract: the sequence-number prefix and the
# terminating newline/trailing-space suffix are part of what any future
# override must preserve (see each fragment's ``boundary``), not incidental
# formatting — this table makes that machine-checked.
# --------------------------------------------------------------------------- #

# (fragment_id, required_prefix_or_None, required_suffix_or_None)
_FRAGMENT_SHAPE_CONTRACT = (
    ("answer.style_language", "4. ", "\n"),
    ("answer.mechanism_organization", "8. ", "\n"),
    ("answer.domain_conventions", "9. ", "\n"),
    ("answer.numeric_attribution", "10. ", "\n"),
    ("expand_query.decomposition_guidance", "For a COMPARISON", "\n"),
    ("intent.cross_tool_mapping", None, "\n"),
    ("report.storm_lenses", "1. ", "\n"),
    ("report.frame_example", None, "peers. "),
    ("report_section.domain_conventions", "3. ", "\n"),
)


def test_fragment_shape_contract_covers_every_fragment():
    assert {row[0] for row in _FRAGMENT_SHAPE_CONTRACT} == set(L1_FRAGMENTS)


@pytest.mark.parametrize(
    "fragment_id,required_prefix,required_suffix", _FRAGMENT_SHAPE_CONTRACT
)
def test_fragment_text_shape_contract(fragment_id, required_prefix, required_suffix):
    text = fragment_text(fragment_id)
    if required_prefix is not None:
        assert text.startswith(required_prefix), (
            f"{fragment_id!r} must start with {required_prefix!r}, got "
            f"{text[:len(required_prefix) + 10]!r}"
        )
    if required_suffix is not None:
        assert text.endswith(required_suffix), (
            f"{fragment_id!r} must end with {required_suffix!r}, got "
            f"{text[-(len(required_suffix) + 10):]!r}"
        )


# Which rendered snapshot case(s) exercise each fragment's owning prompt_id,
# used below to prove each fragment's text actually appears verbatim in at
# least one rendering of its own prompt.
_CASES_BY_PROMPT_ID = {
    "answer_prompt": ["answer_default", "answer_history", "answer_sectioned_style"],
    "expand_query_prompt": ["expand_basic", "expand_full"],
    "query_intent_prompt": ["intent_default", "intent_confirm"],
    "report_storm_outline_prompt": ["outline_storm"],
    "report_section_prompt": ["section_parametric", "section_strict"],
}


def test_every_fragment_text_appears_verbatim_in_a_rendering_of_its_prompt():
    for fragment_id, fragment in L1_FRAGMENTS.items():
        case_names = _CASES_BY_PROMPT_ID.get(fragment.prompt_id)
        assert case_names, (
            f"no known snapshot case for prompt_id {fragment.prompt_id!r} "
            f"(fragment {fragment_id!r})"
        )
        renderings = [CASES[name]() for name in case_names]
        assert any(fragment.text in rendering for rendering in renderings), (
            f"{fragment_id!r}'s text does not appear verbatim in any "
            f"rendering of {fragment.prompt_id!r}"
        )


def test_fragment_text_matches_source_of_truth():
    """fragment_text() returns exactly the fragment registered under that id."""
    for fragment_id, fragment in L1_FRAGMENTS.items():
        assert fragment_text(fragment_id) == fragment.text


def test_fragment_text_raises_key_error_for_unknown_id():
    with pytest.raises(KeyError):
        fragment_text("nope")


# --------------------------------------------------------------------------- #
# 3. L2_BLOCKS registry invariants — including reconciliation against real
# ``inspect.signature`` parameters, so this ~100 lines of metadata is a
# checked contract rather than prose that can silently drift from the code.
# --------------------------------------------------------------------------- #

def test_l2_block_ids_are_unique():
    ids = [block.block_id for block in L2_BLOCKS]
    assert len(ids) == len(set(ids)), f"duplicate L2 block ids: {ids!r}"


def test_l2_blocks_cover_the_required_minimum_set():
    required = {
        "history_block", "profile_block", "experience_block", "style_block",
        "collection_map", "corpus_langs", "discovered_structure",
        "assumptions", "report_frame", "synthesis_commitment",
        "intent_block", "coverage_block", "corpus_map",
    }
    ids = {block.block_id for block in L2_BLOCKS}
    missing = required - ids
    assert not missing, f"L2_BLOCKS missing required entries: {missing!r}"


def test_l2_blocks_have_nonempty_metadata():
    for block in L2_BLOCKS:
        assert block.block_id
        assert block.prompts
        assert block.description
        assert block.source


_PROMPT_FUNCTION_NAME = re.compile(r"^[a-z][a-z0-9_]*_prompt$")


def _public_prompt_functions() -> dict:
    """Every public ``*_prompt`` function actually DEFINED in prompts.py.

    Filters to ``func.__module__ == prompts.__name__`` so an unrelated
    imported name (there is none matching this suffix today, but the guard
    costs nothing) can never be mistaken for one of prompts.py's own prompt
    builders.
    """
    return {
        name: func
        for name, func in inspect.getmembers(prompts, inspect.isfunction)
        if _PROMPT_FUNCTION_NAME.match(name) and func.__module__ == prompts.__name__
    }


def test_l2_blocks_forward_every_listed_prompt_really_has_the_parameter():
    """Forward direction: every (block, prompt) pair L2_BLOCKS claims must be
    real — the named function exists in prompts.py and its signature
    actually declares a parameter named after the block_id."""
    for block in L2_BLOCKS:
        for prompt_name in block.prompts:
            func = getattr(prompts, prompt_name, None)
            assert func is not None and inspect.isfunction(func), (
                f"L2Block {block.block_id!r} lists {prompt_name!r}, which is "
                "not a real function in app.services.prompts"
            )
            params = inspect.signature(func).parameters
            assert block.block_id in params, (
                f"L2Block {block.block_id!r} lists {prompt_name!r}, but "
                f"{prompt_name!r}'s signature has no {block.block_id!r} "
                f"parameter (actual params: {sorted(params)!r})"
            )


def test_l2_blocks_backward_every_matching_parameter_is_declared():
    """Backward direction: any public ``*_prompt`` function that has a
    parameter named after a REGISTERED block_id must be listed in that
    block's ``prompts`` tuple — a function cannot silently gain (or keep) a
    data-injection parameter that L2_BLOCKS does not know about."""
    registered_blocks = {block.block_id: block for block in L2_BLOCKS}
    for name, func in _public_prompt_functions().items():
        params = inspect.signature(func).parameters
        for block_id, block in registered_blocks.items():
            if block_id in params:
                assert name in block.prompts, (
                    f"{name!r} has a {block_id!r} parameter but is not "
                    f"listed in L2_BLOCKS[{block_id!r}].prompts "
                    f"(currently {block.prompts!r})"
                )


# --------------------------------------------------------------------------- #
# 4. Single-read-path guard: prompts.py may only ever import fragment_text
# from prompt_layers — never L1_FRAGMENTS itself (that would open a second,
# unmanaged read path around the one sanctioned override seam).
# --------------------------------------------------------------------------- #

def test_prompts_py_only_imports_fragment_text_from_prompt_layers():
    source = Path(prompts.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = set()
    found_import = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "app.services.prompt_layers"
        ):
            found_import = True
            imported_names.update(alias.name for alias in node.names)
    assert found_import, (
        "expected prompts.py to import from app.services.prompt_layers"
    )
    assert imported_names == {"fragment_text"}, (
        f"prompts.py imports {sorted(imported_names)!r} from prompt_layers "
        "— only fragment_text is allowed; importing L1_FRAGMENTS (or "
        "anything else) directly would bypass the single override seam"
    )
