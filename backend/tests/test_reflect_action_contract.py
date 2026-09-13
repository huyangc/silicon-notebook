"""Registration contract for the ``ask.reflect_action`` extension point (T1).

Three groups of guard live here:

1. A well-formed descriptor registers, and ``reflect_action_specs()`` reports
   it in the registry's own frozen order with its plugin id attached.
2. Every rejection rule gets its own case, and each one is a *startup* failure
   (``ExtensionRegistryError``) rather than a clamp or a runtime skip -- the
   over-length and control-character cases are the ones that make that
   distinction observable.
3. A REFLECTIVE guard on ``RESERVED_REFLECT_KEYS``: the reserved set is
   re-derived from ``prompts.reflect_schema_hint`` with every gate open and
   compared for equality, so the set cannot silently fall behind a new core
   action or a new schema field.  ``reflect_schema_hint``'s own signature is
   reflected rather than transcribed -- see ``_all_gates_open_kwargs``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
import json

import pytest

from app.domain.reflect_action import (
    EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS,
    REFLECT_ACTION_DESCRIPTION_MAX_CHARS,
    REFLECT_ACTION_ENUM_VALUES_MAX,
    REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS,
    REFLECT_ACTION_PARAMETERS_MAX,
    RESERVED_REFLECT_KEYS,
    ReflectActionSpec,
)
from app.extension_sdk import (
    ASK_REFLECT_ACTION_POINT,
    EXTENSION_API_VERSION,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
    ReflectActionDescriptor,
    ReflectActionParameter,
)
from app.extensions import build_extension_registry
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError
from app.services.collection_catalog import (
    ENUMERABLE_ELEMENT_KINDS,
    ENUMERABLE_KG_OBJECT_TYPES,
)
from app.services.prompts import reflect_schema_hint
from app.services.reasoning_retrieval import ReasoningResult


@dataclass
class _Bundle:
    manifest: ExtensionManifest
    implementations: tuple[object, ...] = ()

    def register(self, registrar) -> None:
        for declaration, implementation in zip(
            self.manifest.contributions, self.implementations
        ):
            registrar.add(ExtensionContribution(declaration, implementation))


class _Action:
    def __init__(self, descriptor: object) -> None:
        self.descriptor = descriptor

    def invoke(self, context):  # pragma: no cover - never called at freeze
        raise AssertionError("registration must not invoke the contributor")


_DESCRIPTOR = ReflectActionDescriptor(
    name="search_ieee",
    description="Search IEEE Xplore for peer-reviewed papers and return excerpts.",
    source_label="IEEE Xplore",
    parameters=(
        ReflectActionParameter(
            name="query",
            description="The search phrase, in English.",
            kind="text",
            required=True,
        ),
        ReflectActionParameter(
            name="venue",
            description="Restrict to one venue type.",
            kind="enum",
            values=("journal", "conference"),
        ),
    ),
    max_calls_per_run=2,
)


def _bundle(
    plugin_id: str,
    descriptor: object,
    *,
    contribution_id: str = "",
    kind: ContributionKind = ContributionKind.CONTRIBUTOR,
) -> _Bundle:
    declaration = ContributionDeclaration(
        contribution_id or f"{plugin_id}-action",
        ASK_REFLECT_ACTION_POINT,
        kind,
    )
    return _Bundle(
        ExtensionManifest(
            id=plugin_id,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=plugin_id,
            trust="builtin",
            contributions=(declaration,),
        ),
        (_Action(descriptor),),
    )


def _freeze(*bundles: _Bundle):
    return build_extension_registry(bundles)


def _rejects(*bundles: _Bundle) -> str:
    with pytest.raises(ExtensionRegistryError) as excinfo:
        _freeze(*bundles)
    return str(excinfo.value)


# --- Group 1: the happy path ------------------------------------------------


def test_a_valid_descriptor_registers_and_is_projected_into_a_spec():
    registry = _freeze(_bundle("ieee", _DESCRIPTOR))

    specs = registry.reflect_action_specs()

    assert specs == (
        ReflectActionSpec("ieee-action", "ieee", _DESCRIPTOR),
    )
    assert specs[0].descriptor is _DESCRIPTOR


def test_specs_follow_the_registry_s_own_frozen_contribution_order():
    """The spec tuple is the same order ``contributions()`` reports.

    Asserted as an identity between the two rather than against a hand-written
    list, so it stays true if the registry ever changes how it orders a point.
    """

    second = replace(_DESCRIPTOR, name="search_arxiv")
    registry = _freeze(
        _bundle("zeta", second, contribution_id="zeta-action"),
        _bundle("alpha", _DESCRIPTOR, contribution_id="alpha-action"),
    )

    specs = registry.reflect_action_specs()

    assert [spec.contribution_id for spec in specs] == [
        registered.contribution.declaration.id
        for registered in registry.contributions(ASK_REFLECT_ACTION_POINT)
    ]
    assert [spec.plugin_id for spec in specs] == ["alpha", "zeta"]


def test_a_deployment_without_reflect_actions_reports_an_empty_topology():
    assert build_extension_registry().reflect_action_specs() == ()


def test_specs_are_unreachable_before_freeze():
    with pytest.raises(ExtensionRegistryError, match="not frozen"):
        ExtensionRegistry().reflect_action_specs()


# --- Group 2: one case per rejection rule -----------------------------------


@pytest.mark.parametrize(
    "name",
    ["Search", "se", "search-ieee", "9search", "search ieee", "search_" + "x" * 32],
)
def test_an_action_name_off_the_pattern_is_refused(name):
    assert "name" in _rejects(_bundle("p", replace(_DESCRIPTOR, name=name)))


def test_an_action_named_after_a_core_action_is_refused():
    message = _rejects(_bundle("p", replace(_DESCRIPTOR, name="search_chunks")))

    assert "reserved reflect key" in message


def test_an_action_named_after_a_schema_field_is_refused():
    """``expand`` is not an action id -- it is the graph traversal's PARAMETER
    object. An action of that name would make the model write its arguments
    into a core slot, which is why the reserved set covers both halves."""

    message = _rejects(_bundle("p", replace(_DESCRIPTOR, name="expand")))

    assert "reserved reflect key" in message


def test_two_plugins_cannot_claim_the_same_action_name():
    message = _rejects(
        _bundle("ieee", _DESCRIPTOR, contribution_id="ieee-action"),
        _bundle("acm", _DESCRIPTOR, contribution_id="acm-action"),
    )

    assert "claimed by both" in message


def test_one_plugin_cannot_register_the_same_action_name_twice():
    declarations = (
        ContributionDeclaration("first", ASK_REFLECT_ACTION_POINT, ContributionKind.CONTRIBUTOR),
        ContributionDeclaration("second", ASK_REFLECT_ACTION_POINT, ContributionKind.CONTRIBUTOR),
    )
    bundle = _Bundle(
        ExtensionManifest(
            id="ieee",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="ieee",
            trust="builtin",
            contributions=declarations,
        ),
        (_Action(_DESCRIPTOR), _Action(replace(_DESCRIPTOR, description="other"))),
    )

    assert "claimed by both" in _rejects(bundle)


def test_a_non_contributor_kind_is_refused():
    message = _rejects(
        _bundle("p", _DESCRIPTOR, kind=ContributionKind.OBSERVER)
    )

    assert "must be registered as contributor" in message


def test_an_implementation_without_a_descriptor_is_refused():
    assert "does not expose a ReflectActionDescriptor" in _rejects(
        _bundle("p", None)
    )


def test_a_look_alike_descriptor_type_is_refused():
    """Exact type, not duck typing: a structurally similar object from another
    module would carry fields this validation has never seen."""

    @dataclass(frozen=True)
    class _LookAlike:
        name: str = "search_ieee"
        description: str = "x"
        source_label: str = "y"
        parameters: tuple = ()
        max_calls_per_run: int = 1

    assert "does not expose a ReflectActionDescriptor" in _rejects(
        _bundle("p", _LookAlike())
    )


def test_more_parameters_than_the_rail_allows_is_refused():
    parameters = tuple(
        ReflectActionParameter(f"p{index}", "d", "text")
        for index in range(REFLECT_ACTION_PARAMETERS_MAX + 1)
    )

    assert "more than" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


@pytest.mark.parametrize("name", ["Query", "query-term", "1query", "q" * 25, ""])
def test_a_parameter_name_off_the_pattern_is_refused(name):
    parameters = (ReflectActionParameter(name, "d", "text"),)

    assert "stable lowercase identifier" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


@pytest.mark.parametrize("name", ["name", "reason"])
def test_a_parameter_shadowing_an_outer_reflect_field_is_refused(name):
    parameters = (ReflectActionParameter(name, "d", "text"),)

    assert "reserved parameter name" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


def test_a_repeated_parameter_name_is_refused():
    parameters = (
        ReflectActionParameter("query", "d", "text"),
        ReflectActionParameter("query", "e", "text"),
    )

    assert "twice" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


def test_an_enum_parameter_without_values_is_refused():
    parameters = (ReflectActionParameter("venue", "d", "enum"),)

    assert "enum with no values" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


def test_more_enum_values_than_the_rail_allows_is_refused():
    values = tuple(f"v{index}" for index in range(REFLECT_ACTION_ENUM_VALUES_MAX + 1))
    parameters = (ReflectActionParameter("venue", "d", "enum", values),)

    assert "enum values" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


def test_an_enum_value_off_the_pattern_is_refused():
    parameters = (ReflectActionParameter("venue", "d", "enum", ("Journal",)),)

    assert "stable lowercase identifier" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


def test_a_text_parameter_carrying_enum_values_is_refused():
    parameters = (ReflectActionParameter("query", "d", "text", ("a", "b")),)

    assert "text but declares enum values" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


def test_a_max_calls_per_run_below_one_is_refused():
    assert "at least one call per run" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, max_calls_per_run=0))
    )


@pytest.mark.parametrize("required", ["false", "", 1, None])
def test_a_non_boolean_required_flag_is_refused(required):
    """Exact ``bool``. ``required="false"`` is truthy, so the run would fail
    closed on an argument the plugin author believed was optional."""

    parameters = (ReflectActionParameter("query", "d", "text", (), required),)

    assert "non-boolean required flag" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("field", ["description", "source_label"])
def test_an_empty_descriptor_string_is_refused(field, blank):
    """A blank ``description`` would put a bare action name in front of the
    model with nothing saying what it does; a blank ``source_label`` would
    leave the citation badge with no origin word. Whitespace counts as empty --
    it renders identically."""

    message = _rejects(_bundle("p", replace(_DESCRIPTOR, **{field: blank})))

    assert f"empty {field}" in message


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_parameter_description_is_refused(blank):
    parameters = (ReflectActionParameter("query", blank, "text"),)

    assert "empty parameter 'query' description" in _rejects(
        _bundle("p", replace(_DESCRIPTOR, parameters=parameters))
    )


# A recognisable stand-in for the kind of thing a deployment plugin's own
# descriptor string can hold: an API key, an internal hostname. It must never
# reach the operator log, so every over-limit/control-character case below
# asserts on its ABSENCE from the message as well as on the rejection.
_SENTINEL = "SENTINEL_SECRET_do_not_log"


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("description", REFLECT_ACTION_DESCRIPTION_MAX_CHARS),
        ("source_label", EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS),
    ],
)
def test_an_over_length_descriptor_string_fails_startup_without_echoing_it(
    field, limit
):
    value = _SENTINEL + "x" * (limit + 1)

    message = _rejects(_bundle("p", replace(_DESCRIPTOR, **{field: value})))

    assert "longer than" in message and field in message
    assert _SENTINEL not in message


def test_an_over_length_parameter_description_fails_startup_without_echoing_it():
    value = _SENTINEL + "x" * (REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS + 1)
    parameters = (ReflectActionParameter("query", value, "text"),)

    message = _rejects(_bundle("p", replace(_DESCRIPTOR, parameters=parameters)))

    assert "longer than" in message
    assert _SENTINEL not in message


@pytest.mark.parametrize(
    "control",
    [
        "\n",        # C0: the direct attack -- a second line in the prompt
        "\r",
        "\x00",
        "\x1b",
        "\x7f",      # DEL
        "\x85",      # C1 NEL: a line break to many tokenizers
        " ",    # LINE SEPARATOR
        " ",    # PARAGRAPH SEPARATOR
        "‮",    # bidi override: reverses the rendered line
        "⁦",    # bidi isolate
    ],
)
def test_a_control_character_in_a_descriptor_string_fails_startup(control):
    """Not stripped: a surviving line break would let plugin text pose as
    another line of core's own prompt template, and a bidi override would make
    the reviewed line and the rendered line say different things."""

    value = f"{_SENTINEL}{control}things"

    message = _rejects(_bundle("p", replace(_DESCRIPTOR, description=value)))

    assert "control characters" in message
    assert _SENTINEL not in message and control not in message


def test_a_control_character_in_a_parameter_description_fails_startup():
    parameters = (ReflectActionParameter("query", f"{_SENTINEL}\nb", "text"),)

    message = _rejects(_bundle("p", replace(_DESCRIPTOR, parameters=parameters)))

    assert "control characters" in message
    assert _SENTINEL not in message


def test_a_rejected_descriptor_leaves_the_registry_unfrozen_and_unusable():
    """A bad descriptor must not yield a half-usable registry.

    Registration is per bundle and succeeds; the reflect rules run at FREEZE,
    over the whole topology at once. So this exercises one registry instance
    holding a good bundle and a bad one: freeze raises, the registry stays
    unfrozen, and every frozen-only accessor -- including the spec list the bad
    bundle would otherwise have polluted -- keeps refusing.
    """

    registry = ExtensionRegistry()
    registry.register(_bundle("good", _DESCRIPTOR, contribution_id="good-action"))
    registry.register(
        _bundle(
            "bad",
            replace(_DESCRIPTOR, name="search_arxiv", source_label=""),
            contribution_id="bad-action",
        )
    )

    with pytest.raises(ExtensionRegistryError, match="empty source_label"):
        registry.freeze()

    assert registry.frozen is False
    with pytest.raises(ExtensionRegistryError, match="not frozen"):
        registry.reflect_action_specs()


# --- Group 3: the reflective reserved-key guard -----------------------------

# The gates ``reflect_schema_hint`` is known to have. Pinned so that ANY new
# parameter -- of any type, including one this file could not guess how to open
# -- goes red at ``test_the_reflect_schema_gate_set_is_pinned`` before it can
# quietly widen the model's action space behind the reserved set.
_KNOWN_REFLECT_SCHEMA_GATES = frozenset({
    "element_kinds",
    "object_types",
    "outline",
    "consult_memory",
    "search_chunks",
    "kg_actions",
})
# The full whitelist each sequence-shaped gate is opened with.
_REFLECT_SCHEMA_SEQUENCE_GATES = {
    "element_kinds": ENUMERABLE_ELEMENT_KINDS,
    "object_types": ENUMERABLE_KG_OBJECT_TYPES,
}


def _all_gates_open_kwargs() -> dict[str, object]:
    """Every gate of ``reflect_schema_hint``, opened, derived from its SIGNATURE.

    The second of the two layers guarding the reserved set, and the one that
    covers the case the pinned name set does not: someone adds
    ``search_web: bool = False``, updates ``_KNOWN_REFLECT_SCHEMA_GATES``
    because that assertion told them to, and stops there. Because the kwargs
    are reflected rather than transcribed, the new gate is opened anyway, its
    action words enter ``next_action``, and the equality below fails until
    ``RESERVED_REFLECT_KEYS`` learns them too.

    A DEFAULT-OFF gate is exactly the dangerous shape here: transcribing six
    keyword arguments by hand leaves such a gate closed forever, and the guard
    stays green over a schema it is no longer reading.
    """

    kwargs: dict[str, object] = {}
    for name, parameter in inspect.signature(reflect_schema_hint).parameters.items():
        if name in _REFLECT_SCHEMA_SEQUENCE_GATES:
            kwargs[name] = _REFLECT_SCHEMA_SEQUENCE_GATES[name]
        elif type(parameter.default) is bool:
            kwargs[name] = True
        else:  # pragma: no cover - the pinned-name test fails first
            raise AssertionError(
                f"reflect_schema_hint gate {name!r} has no known way to be "
                "opened; teach this file how before adding it"
            )
    return kwargs


def test_the_reflect_schema_gate_set_is_pinned():
    """First layer: the gate NAMES are frozen.

    A new parameter of any shape -- an int budget, an enum, a sequence this
    file has no whitelist for -- lands here rather than slipping past a
    hand-written call that simply never mentions it.
    """

    gates = set(inspect.signature(reflect_schema_hint).parameters)

    assert gates == set(_KNOWN_REFLECT_SCHEMA_GATES)


def test_reserved_keys_are_exactly_what_an_all_gates_open_reflect_schema_offers():
    """Second layer: re-derive the reserved set from the schema hint itself.

    Every gate is opened, so the schema carries every core action id and every
    top-level field the model can ever be shown. If a future change adds an
    action or a field and forgets ``RESERVED_REFLECT_KEYS``, a plugin could
    claim that exact word and shadow it -- this equality is what makes that a
    red test instead of a production surprise.
    """

    schema = json.loads(reflect_schema_hint(**_all_gates_open_kwargs()))

    offered = set(schema) | set(schema["next_action"].split("|"))

    assert offered == set(RESERVED_REFLECT_KEYS)


# --- The retrieval result carrier ------------------------------------------


def test_a_fresh_reasoning_result_carries_no_external_evidence():
    assert ReasoningResult().external_evidence == []
