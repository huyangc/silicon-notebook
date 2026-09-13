"""Service-level composition for ``source.element_enricher`` (T2).

The host decides what a contribution may PROPOSE; this module decides what is
WRITTEN.  Everything here drives ``enrich_source_elements`` with a stand-in
host, so the rules under test are exactly the synthesis rules from the design
spec §4 — where the ``extensions`` subtree lands, how a description reaches
both ``metadata["description"]`` and the element's retrievable ``text``, and
which violations discard the whole batch and under which reason code.
"""
from __future__ import annotations

import pytest

from app.domain.cancellation import CoreCancellation
from app.domain.extensions import (
    ElementAssetLocation,
    ElementEnrichmentPatch,
)
from app.models.sources import SourceElement
from app.services.source_element_enrichment import (
    REJECT_BUDGET_EXCEEDED,
    REJECT_DUPLICATE_CONTRIBUTION,
    REJECT_HOST_FAILED,
    REJECT_INVALID_BUDGET,
    REJECT_INVALID_DESCRIPTION,
    REJECT_INVALID_OWNER,
    REJECT_INVALID_PATCH,
    REJECT_ORDINAL_OUT_OF_RANGE,
    enrich_source_elements,
)


PLUGIN = "examples.circuit_diagram"
VERSION = "0.1.0"
CONTRIBUTION = "examples.circuit_diagram.enricher"
OTHER_PLUGIN = "examples.other"
OTHER_CONTRIBUTION = "examples.other.enricher"


class _Host:
    """A stand-in element-enricher host: records its call, returns patches."""

    def __init__(self, patches=(), *, contributions: object = True, raises=None):
        self._patches = patches
        self._contributions = contributions
        self._raises = raises
        self.calls = []

    def has_contributions(self) -> bool:
        return self._contributions

    def enrich_application(self, call_context, *, event_sink=None):
        self.calls.append(call_context)
        if self._raises is not None:
            raise self._raises
        return tuple(self._patches)


def _element(
    ordinal: int,
    *,
    element_type: str = "paragraph",
    text: str = "body text",
    metadata: dict | None = None,
) -> SourceElement:
    return SourceElement(
        id=f"el-{ordinal:04d}",
        source_id="src-1",
        element_type=element_type,
        location_label=f"Markdown {element_type} {ordinal}",
        text=text,
        metadata={} if metadata is None else metadata,
    )


def _image(ordinal: int = 1, **metadata) -> SourceElement:
    return _element(
        ordinal, element_type="image", text="a wiring diagram", metadata=metadata
    )


def _patch(
    ordinal: int = 1,
    *,
    metadata: dict | None = None,
    description: str = "",
    contribution_id: str = CONTRIBUTION,
    plugin_id: str = PLUGIN,
    plugin_version: str = VERSION,
) -> ElementEnrichmentPatch:
    return ElementEnrichmentPatch(
        ordinal,
        plugin_id,
        plugin_version,
        contribution_id,
        {"is_circuit": True} if metadata is None else metadata,
        description,
    )


def _run(elements, host, *, asset_locations=None, **overrides):
    """The call under test.  Returns ``(elements, reason_code)``."""
    budgets = {
        "max_proposals": 16,
        "max_metadata_bytes": 65536,
        "max_description_chars": 4096,
        "max_asset_bytes": 1024,
        "timeout_seconds": 30.0,
    }
    budgets.update(overrides)
    return enrich_source_elements(
        elements,
        host=host,
        asset_locations={} if asset_locations is None else asset_locations,
        connection_probe=object(),
        event_sink=None,
        **budgets,
    )


# --- synthesis rules ------------------------------------------------------


def test_extensions_subtree_lands_under_the_contribution_id():
    elements = [_image(1, asset_id="asset-1")]
    host = _Host([_patch(metadata={"netlist": "R1 1 0 1k", "is_circuit": True})])

    result, rejected = _run(elements, host)

    assert (result is not elements, rejected) == (True, "")
    assert result[0].metadata["extensions"] == {
        CONTRIBUTION: {
            "plugin_id": PLUGIN,
            "plugin_version": VERSION,
            "metadata": {"netlist": "R1 1 0 1k", "is_circuit": True},
        }
    }
    # The parser's own metadata survives beside it, and the input element is
    # never mutated in place.
    assert result[0].metadata["asset_id"] == "asset-1"
    assert "extensions" not in elements[0].metadata


def test_persisted_metadata_is_a_copy_the_host_cannot_reach_afterwards():
    proposed = {"netlist": "R1 1 0 1k"}
    elements = [_image(1, asset_id="asset-1")]

    result, _ = _run(elements, _Host([_patch(metadata=proposed)]))
    proposed["netlist"] = "MUTATED"

    persisted = result[0].metadata["extensions"][CONTRIBUTION]["metadata"]
    assert persisted == {"netlist": "R1 1 0 1k"}


def test_description_is_written_and_flattened_into_an_image_elements_text():
    elements = [_image(1, asset_id="asset-1")]
    description = "电路功能：分压\n\n```spice\nR1 1 0 1k\n```"

    result, rejected = _run(elements, _Host([_patch(description=description)]))

    # The detail view keeps the fenced block verbatim...
    assert result[0].metadata["description"] == description
    # ...while the retrieval text stays the single flattened line an image
    # element's text always is (``parsers._element``).
    assert result[0].text == (
        "a wiring diagram 电路功能：分压 ```spice R1 1 0 1k ```"
    )
    assert "\n" not in result[0].text
    assert rejected == ""


@pytest.mark.parametrize("element_type", ["code_block", "table"])
def test_a_structured_elements_text_keeps_the_descriptions_own_line_breaks(
    element_type,
):
    """``parsers._element`` stores these two types with their structure
    intact, so appending to one must not flatten what it appends."""
    elements = [
        _element(1, element_type=element_type, text="line one\nline two")
    ]
    description = "电路功能：分压\n\n```spice\nR1 1 0 1k\n```"

    result, rejected = _run(elements, _Host([_patch(description=description)]))

    assert result[0].text == f"line one\nline two\n{description}"
    assert result[0].metadata["description"] == description
    assert rejected == ""


def test_description_appends_to_an_existing_parser_description():
    elements = [_image(1, asset_id="asset-1", description="作者给的说明")]

    result, _ = _run(elements, _Host([_patch(description="插件读出的说明")]))

    assert result[0].metadata["description"] == "作者给的说明\n\n插件读出的说明"
    assert result[0].text == "a wiring diagram 插件读出的说明"


def test_an_empty_description_leaves_text_and_description_untouched():
    elements = [_image(1, asset_id="asset-1", description="作者给的说明")]

    result, _ = _run(elements, _Host([_patch(description="")]))

    assert result[0].metadata["description"] == "作者给的说明"
    assert result[0].text == "a wiring diagram"
    assert CONTRIBUTION in result[0].metadata["extensions"]


def test_patches_apply_by_ordinal_and_leave_every_other_element_alone():
    elements = [_element(1), _image(2, asset_id="asset-2"), _element(3)]

    result, _ = _run(elements, _Host([_patch(ordinal=2, description="说明")]))

    assert result[0] is elements[0]
    assert result[2] is elements[2]
    assert result[1].metadata["description"] == "说明"


def test_two_contributions_may_enrich_the_same_element_and_both_are_kept():
    """Distinct contributions are not a duplicate: each owns its own key, and
    their descriptions accumulate in patch order the same way a plugin's
    description accumulates onto the parser's own."""
    elements = [_image(1, asset_id="asset-1")]
    host = _Host(
        [
            _patch(ordinal=1, metadata={"netlist": "R1 1 0 1k"}, description="第一段"),
            _patch(
                ordinal=1,
                metadata={"score": 3},
                description="第二段",
                contribution_id=OTHER_CONTRIBUTION,
                plugin_id=OTHER_PLUGIN,
            ),
        ]
    )

    result, rejected = _run(elements, host)

    assert rejected == ""
    extensions = result[0].metadata["extensions"]
    assert set(extensions) == {CONTRIBUTION, OTHER_CONTRIBUTION}
    assert extensions[CONTRIBUTION]["metadata"] == {"netlist": "R1 1 0 1k"}
    assert extensions[OTHER_CONTRIBUTION]["plugin_id"] == OTHER_PLUGIN
    assert extensions[OTHER_CONTRIBUTION]["metadata"] == {"score": 3}
    assert result[0].metadata["description"] == "第一段\n\n第二段"
    assert result[0].text == "a wiring diagram 第一段 第二段"


# --- whole-batch rejections, each with its own reason code -----------------


def test_a_contribution_already_present_rejects_the_whole_batch():
    elements = [
        _image(1, asset_id="asset-1", extensions={CONTRIBUTION: {"metadata": {}}}),
        _element(2),
    ]

    result, rejected = _run(
        elements, _Host([_patch(ordinal=2, description="说明"), _patch(ordinal=1)])
    )

    assert (result, rejected) == (elements, REJECT_DUPLICATE_CONTRIBUTION)
    assert "description" not in elements[1].metadata


def test_exceeding_the_metadata_byte_budget_rejects_the_whole_batch():
    elements = [_image(1, asset_id="asset-1"), _element(2)]
    payload = {"netlist": "R" * 400}
    host = _Host(
        [_patch(ordinal=1, metadata=payload), _patch(ordinal=2, metadata=payload)]
    )

    # One patch fits in 700 bytes; the two of them, charged to the same
    # contribution, do not.
    refused, rejected = _run(elements, host, max_metadata_bytes=700)
    assert (refused, rejected) == (elements, REJECT_BUDGET_EXCEEDED)
    applied, rejected = _run(elements, host, max_metadata_bytes=65536)
    assert (applied is not elements, rejected) == (True, "")


def test_the_byte_budget_is_charged_per_contribution_not_per_point():
    elements = [_image(1, asset_id="asset-1"), _element(2)]
    payload = {"netlist": "R" * 400}
    host = _Host(
        [
            _patch(ordinal=1, metadata=payload),
            _patch(
                ordinal=2,
                metadata=payload,
                contribution_id=OTHER_CONTRIBUTION,
                plugin_id=OTHER_PLUGIN,
            ),
        ]
    )

    result, rejected = _run(elements, host, max_metadata_bytes=700)

    assert (result is not elements, rejected) == (True, "")
    assert CONTRIBUTION in result[0].metadata["extensions"]
    assert OTHER_CONTRIBUTION in result[1].metadata["extensions"]


def test_an_out_of_range_ordinal_rejects_the_whole_batch():
    elements = [_image(1, asset_id="asset-1")]

    for ordinal in (2, 0, -1):
        assert _run(elements, _Host([_patch(ordinal=ordinal)])) == (
            elements,
            REJECT_ORDINAL_OUT_OF_RANGE,
        )


def test_an_unpersistable_owner_rejects_the_whole_batch():
    elements = [_image(1, asset_id="asset-1")]

    for patch in (
        _patch(plugin_id="Examples.Bad"),
        _patch(plugin_version=""),
        _patch(contribution_id="not a stable id"),
    ):
        assert _run(elements, _Host([patch])) == (elements, REJECT_INVALID_OWNER)


def test_an_over_long_description_rejects_the_whole_batch():
    elements = [_image(1, asset_id="asset-1")]

    result = _run(
        elements, _Host([_patch(description="x" * 40)]), max_description_chars=39
    )

    assert result == (elements, REJECT_INVALID_DESCRIPTION)


def test_more_patches_than_the_proposal_budget_reject_the_whole_batch():
    elements = [_element(1), _element(2), _element(3)]
    host = _Host([_patch(ordinal=1), _patch(ordinal=2), _patch(ordinal=3)])

    assert _run(elements, host, max_proposals=2) == (elements, REJECT_BUDGET_EXCEEDED)


def test_a_patch_that_is_not_a_patch_rejects_the_whole_batch():
    elements = [_image(1, asset_id="asset-1")]

    assert _run(elements, _Host([object()])) == (elements, REJECT_INVALID_PATCH)


def test_a_result_that_is_not_a_tuple_rejects_the_whole_batch():
    elements = [_image(1, asset_id="asset-1")]

    class _ListHost(_Host):
        def enrich_application(self, call_context, *, event_sink=None):
            return [_patch()]

    assert _run(elements, _ListHost()) == (elements, REJECT_INVALID_PATCH)


# --- short circuits and fail-open -----------------------------------------


def test_no_host_returns_the_same_list_object_and_no_reason():
    elements = [_image(1, asset_id="asset-1")]

    assert _run(elements, None) == (elements, "")


def test_no_elements_never_reaches_the_host():
    host = _Host([_patch()])

    assert _run([], host) == ([], "")
    assert host.calls == []


def test_a_host_without_contributions_is_never_called():
    elements = [_image(1, asset_id="asset-1")]
    host = _Host([_patch()], contributions=False)

    assert _run(elements, host) == (elements, "")
    assert host.calls == []


def test_a_truthy_non_true_contribution_answer_is_not_a_yes():
    """The probe is read strictly: only the literal ``True`` enters the host."""
    elements = [_image(1, asset_id="asset-1")]
    host = _Host([_patch()], contributions="yes")

    assert _run(elements, host) == (elements, "")
    assert host.calls == []


def test_a_contributing_host_that_proposes_nothing_is_not_a_rejection():
    elements = [_image(1, asset_id="asset-1")]
    host = _Host([])

    assert _run(elements, host) == (elements, "")
    assert len(host.calls) == 1


def test_a_raising_host_ingests_the_source_unchanged_under_a_stable_code():
    elements = [_image(1, asset_id="asset-1")]

    result = _run(elements, _Host(raises=RuntimeError("plugin exploded")))

    assert result == (elements, REJECT_HOST_FAILED)


def test_cancellation_propagates_instead_of_failing_open():
    elements = [_image(1, asset_id="asset-1")]

    with pytest.raises(CoreCancellation):
        _run(elements, _Host(raises=CoreCancellation()))


@pytest.mark.parametrize(
    "override",
    [
        {"max_proposals": 0},
        {"max_metadata_bytes": 0},
        {"max_description_chars": 0},
        {"max_asset_bytes": 0},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
    ],
)
def test_an_invalid_budget_short_circuits_before_the_host(override):
    elements = [_image(1, asset_id="asset-1")]
    host = _Host([_patch()])

    assert _run(elements, host, **override) == (elements, REJECT_INVALID_BUDGET)
    assert host.calls == []


# --- the envelope the host receives ---------------------------------------


def test_asset_mime_comes_from_the_resolved_location():
    elements = [_image(1, asset_id="asset-1", caption="图 1")]
    host = _Host()

    _run(
        elements,
        host,
        asset_locations={"asset-1": ElementAssetLocation("/tmp/a.png", "image/png")},
    )

    envelope = host.calls[0].elements[0]
    assert (envelope.ordinal, envelope.element_type) == (1, "image")
    assert (envelope.asset_id, envelope.asset_mime) == ("asset-1", "image/png")
    assert envelope.caption == "图 1"


def test_an_unresolved_asset_id_is_not_shown_to_the_host_at_all():
    """"Non-empty asset id" must mean "there ARE bytes to read"."""
    elements = [_image(1, asset_id="asset-gone")]
    host = _Host()

    _run(elements, host, asset_locations={})

    envelope = host.calls[0].elements[0]
    assert (envelope.asset_id, envelope.asset_mime) == ("", "")


def test_non_string_metadata_fields_reach_the_host_as_empty_strings():
    elements = [_image(1, asset_id=7, caption=None, description=["x"])]
    host = _Host()

    _run(elements, host)

    envelope = host.calls[0].elements[0]
    assert (envelope.asset_id, envelope.caption, envelope.description) == ("", "", "")


def test_the_call_context_carries_the_callers_budgets_and_a_live_deadline():
    elements = [_image(1, asset_id="asset-1")]
    host = _Host()

    _run(elements, host, timeout_seconds=45.0, max_proposals=7)

    call = host.calls[0]
    assert call.max_proposals == 7
    assert call.max_asset_bytes == 1024
    assert call.cancellation.is_set() is False
    assert call.cancellation.raise_if_cancelled() is None
    assert call.deadline_monotonic > 0
