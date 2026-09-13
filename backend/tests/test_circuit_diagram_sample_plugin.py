"""Unit tests for the circuit-diagram sample deployment plugin (T4).

The sample plugin is not part of the backend package: it lives at
``examples/extensions/circuit-diagram`` and is meant to be installed into a
deployment's interpreter.  Its ``src`` directory therefore goes on
``sys.path`` here, the same shape ``test_arxiv_sample_plugin.py`` uses, so
these tests exercise the module a deployment would import rather than a copy
maintained beside them.

Why these tests live under the backend test root at all, when the SOP tells an
out-of-tree plugin to keep its tests in its own repository: the backend
verification lane only collects ``backend/tests``.  A sample that shipped its
tests in its own tree would ship them unrun.  The plugin README records the
difference so nobody copies this arrangement into a real out-of-tree plugin.

This file covers what the plugin *decides*, against hand-built seams: the
settings model, the bundle's topology and probe, the transport's request and
its parsing of three shapes of model answer, and the enricher's filtering,
budget and failure behaviour.  Real discovery, a real ``create_app()`` and a
real parse are deliberately left to ``…_e2e.py``.

**No test here sleeps.**  The budget behaviour is driven by a fake monotonic
clock installed on the enricher module, because the real one advances by
microseconds inside a test and would make "not enough deadline left" either
untestable or a race.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.extension_sdk import (
    AvailabilityStatus,
    ElementEnrichmentAvailabilityContext,
    ElementEnrichmentBudget,
    ElementEnrichmentContext,
    ElementRef,
    ElementView,
    ExtensionFailureKind,
    ExtensionResultStatus,
    SOURCE_ELEMENT_ENRICHER_POINT,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "examples" / "extensions" / "circuit-diagram"
_PLUGIN_SRC = _PLUGIN_ROOT / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

import silicon_notebook_circuit_diagram as circuit_package  # noqa: E402
from silicon_notebook_circuit_diagram import bundle as circuit_bundle  # noqa: E402
from silicon_notebook_circuit_diagram import client as circuit_client  # noqa: E402
from silicon_notebook_circuit_diagram import enricher as circuit_enricher  # noqa: E402
from silicon_notebook_circuit_diagram.bundle import (  # noqa: E402
    BUNDLE,
    PLUGIN_ID,
    CircuitDiagramBundle,
)
from silicon_notebook_circuit_diagram.settings import (  # noqa: E402
    CircuitDiagramSettings,
    chat_completions_url,
    classify_kwargs,
)

_CONTRIBUTION_ID = f"{PLUGIN_ID}.enricher"
_VERSION = BUNDLE.manifest.version
_KEY_ENV = "DEEPSEEK_API_KEY"
# 1x1 transparent PNG; the same bytes core's markdown data-URI path accepts.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png(serial: int) -> bytes:
    """Distinct image bytes.

    The enricher classifies one *digest* once per call, so a test that needs
    N outbound requests needs N distinct payloads — reusing ``_PNG`` would be
    testing the cache rather than the thing under test.  The transport never
    decodes the image, so a trailing byte is enough to make them different.
    """

    return _PNG + bytes([serial])


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


class _PostSpy:
    """Stand in for ``client._post`` and record every call it receives."""

    def __init__(self, *answers: object) -> None:
        # Each answer is either bytes to return or an exception to raise.
        self._answers = list(answers) or [_answer_bytes(True)]
        self.calls: list[tuple[str, bytes, dict, float]] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        self.calls.append((url, body, dict(headers), timeout))
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def body(self, index: int = 0) -> dict:
        return json.loads(self.calls[index][1].decode("utf-8"))


def _answer_text(text: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": text}}]}
    ).encode("utf-8")


def _answer_bytes(
    is_circuit: bool, *, netlist: str = "R1 in out 10k", function: str = "分压"
) -> bytes:
    return _answer_text(
        json.dumps(
            {"is_circuit": is_circuit, "netlist": netlist, "function": function},
            ensure_ascii=False,
        )
    )


class _Clock:
    """A monotonic clock that answers a scripted sequence, then repeats."""

    def __init__(self, *values: float) -> None:
        self.values = list(values) or [0.0]
        self.reads = 0

    def monotonic(self) -> float:
        value = self.values[min(self.reads, len(self.values) - 1)]
        self.reads += 1
        return value


class _FakeTimeModule:
    def __init__(self, clock: _Clock) -> None:
        self.monotonic = clock.monotonic


class _Cancellation:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.checks = 0

    def is_set(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.cancelled:
            raise RuntimeError("cancelled")


class _Assets:
    """Core's call-scoped reader, backed by a ref → bytes mapping."""

    def __init__(self, payloads: dict[int, bytes | None]) -> None:
        self._payloads = payloads
        self.reads: list[int] = []

    def read(self, ref: object) -> bytes | None:
        self.reads.append(id(ref))
        return self._payloads.get(id(ref))


def _view(
    ref: ElementRef,
    *,
    element_type: str = "image",
    asset_id: str = "a1",
    asset_mime: str = "image/png",
) -> ElementView:
    return ElementView(
        ref,
        element_type,
        "p1",
        "",
        "",
        "",
        asset_id,
        asset_mime,
    )


def _context(
    views: list[ElementView],
    payloads: dict[int, bytes | None],
    *,
    deadline: float | None = None,
    max_proposals: int = 64,
    max_metadata_bytes: int = 1_048_576,
    max_description_chars: int = 8192,
    max_asset_bytes: int = 10_000_000,
    cancellation: _Cancellation | None = None,
) -> tuple[ElementEnrichmentContext, _Assets, _Cancellation]:
    assets = _Assets(payloads)
    token = cancellation or _Cancellation()
    # Relative to the REAL clock unless a test pins an absolute one: a literal
    # deadline would sit in the past on any machine whose monotonic clock is
    # its uptime, and every budget check would then refuse.
    if deadline is None:
        deadline = time.monotonic() + 600.0
    return (
        ElementEnrichmentContext(
            tuple(views),
            assets,
            token,
            ElementEnrichmentBudget(
                max_proposals,
                max_metadata_bytes,
                max_description_chars,
                max_asset_bytes,
                deadline,
            ),
        ),
        assets,
        token,
    )


def _enricher(monkeypatch, settings: CircuitDiagramSettings | None = None):
    monkeypatch.setenv(_KEY_ENV, "test-key")
    resolved = settings if settings is not None else CircuitDiagramSettings()
    return _bare_enricher(lambda: resolved)


def _bare_enricher(settings_source):
    """The enricher with the provenance the bundle gives it.

    Not optional: the byte budget is sized with core's own function over the
    envelope core writes, so an enricher built without provenance would size
    a different envelope than the one being budgeted.
    """

    return circuit_enricher.CircuitDiagramEnricher(
        settings_source,
        plugin_id=PLUGIN_ID,
        plugin_version=_VERSION,
        contribution_id=_CONTRIBUTION_ID,
    )


def _persisted_size(candidate) -> int:
    """What core will charge this candidate — core's own function, not a copy."""

    from app.domain.element_enrichment import persisted_element_enrichment_size

    return persisted_element_enrichment_size(
        plugin_id=PLUGIN_ID,
        plugin_version=_VERSION,
        contribution_id=_CONTRIBUTION_ID,
        metadata=dict(candidate.metadata),
        description=candidate.description,
    )


def _one_image(monkeypatch, spy, **context_kwargs):
    """The common arrangement: one PNG element, one scripted answer."""

    monkeypatch.setattr(circuit_client, "_post", spy)
    ref = ElementRef(object())
    context, assets, token = _context(
        [_view(ref)], {id(ref): _PNG}, **context_kwargs
    )
    return ref, context, assets, token


# --------------------------------------------------------------------------
# Bundle topology
# --------------------------------------------------------------------------


def test_registered_contributions_match_the_manifest():
    """Core stops the process on any difference between these two sets."""

    registered: list = []

    class _Registrar:
        def add_contributor(self, contribution) -> None:
            registered.append(contribution)

        def __getattr__(self, name):  # pragma: no cover - defensive
            raise AssertionError(f"unexpected registrar call: {name}")

    CircuitDiagramBundle(BUNDLE.manifest).register(_Registrar())

    declared = {declaration.id for declaration in BUNDLE.manifest.contributions}
    assert {c.declaration.id for c in registered} == declared == {_CONTRIBUTION_ID}
    assert registered[0].declaration.point == SOURCE_ELEMENT_ENRICHER_POINT
    assert registered[0].availability is not None


def test_manifest_declares_a_deployment_plugin_with_no_capability_surface():
    """Empty on purpose, all three of them — see the bundle module docstring."""

    manifest = BUNDLE.manifest
    assert manifest.id == PLUGIN_ID == "examples.circuit_diagram"
    assert manifest.trust == "deployment"
    assert manifest.requires == ()
    assert manifest.provides == ()
    assert manifest.ui_contributions == ()
    assert manifest.version == circuit_package.__version__


def test_the_package_init_does_not_re_export_the_bundle():
    """``EXTENSIONS_CONFIG`` names ``…bundle:BUNDLE`` because of this.

    Re-exporting would make the plain ``import silicon_notebook_circuit_diagram``
    — what a packaging check or a version probe does — pull the extension SDK
    in behind it.
    """

    assert not hasattr(circuit_package, "BUNDLE")
    assert circuit_package.__all__ == ["__version__"]


# --------------------------------------------------------------------------
# Availability probe
# --------------------------------------------------------------------------


def test_probe_is_disabled_until_configure_has_run(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "test-key")
    probe = _probe(CircuitDiagramBundle(BUNDLE.manifest))

    availability = probe(_availability_context())

    assert availability.status is AvailabilityStatus.DISABLED
    assert availability.reason_code == "not_configured"


def test_probe_is_disabled_while_the_key_variable_is_blank(monkeypatch):
    bundle = CircuitDiagramBundle(BUNDLE.manifest)
    bundle.configure(CircuitDiagramSettings())
    probe = _probe(bundle)

    monkeypatch.delenv(_KEY_ENV, raising=False)
    assert probe(_availability_context()).reason_code == "api_key_missing"
    # Present but blank is the same answer: an operator who exported an empty
    # variable has not supplied a credential.
    monkeypatch.setenv(_KEY_ENV, "   ")
    assert probe(_availability_context()).reason_code == "api_key_missing"


def test_probe_is_available_once_configured_and_credentialled(monkeypatch):
    bundle = CircuitDiagramBundle(BUNDLE.manifest)
    bundle.configure(CircuitDiagramSettings())
    monkeypatch.setenv(_KEY_ENV, "test-key")

    assert _probe(bundle)(_availability_context()).status is (
        AvailabilityStatus.AVAILABLE
    )


def test_probe_reads_the_configured_variable_name_not_a_hard_coded_one(monkeypatch):
    bundle = CircuitDiagramBundle(BUNDLE.manifest)
    bundle.configure(CircuitDiagramSettings(api_key_env="CORP_VISION_KEY"))
    probe = _probe(bundle)

    monkeypatch.setenv(_KEY_ENV, "test-key")
    monkeypatch.delenv("CORP_VISION_KEY", raising=False)
    assert probe(_availability_context()).reason_code == "api_key_missing"

    monkeypatch.setenv("CORP_VISION_KEY", "k")
    assert probe(_availability_context()).status is AvailabilityStatus.AVAILABLE


def _probe(bundle: CircuitDiagramBundle):
    """The probe core will actually call — taken off the registration itself.

    Reaching for the bundle's own method instead would test a function that
    might not be the one wired to the contribution.
    """

    collected: list = []

    class _Registrar:
        def add_contributor(self, contribution) -> None:
            collected.append(contribution)

    bundle.register(_Registrar())
    [contribution] = collected
    assert contribution.declaration.id == _CONTRIBUTION_ID
    return contribution.availability


def _availability_context() -> ElementEnrichmentAvailabilityContext:
    return ElementEnrichmentAvailabilityContext(
        PLUGIN_ID, _CONTRIBUTION_ID, 3, 1, time.monotonic() + 600.0
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "api.deepseek.com",
        "https://",
        "https://api.deepseek.com/v1?key=x",
        "https://api.deepseek.com/v1#frag",
    ],
)
def test_base_url_rejects_anything_that_is_not_a_plain_http_url(value):
    with pytest.raises(ValidationError):
        CircuitDiagramSettings(base_url=value)


def test_base_url_trailing_slash_is_stripped_once_so_callers_can_join():
    settings = CircuitDiagramSettings(base_url="https://gw.example/v1/")
    assert settings.base_url == "https://gw.example/v1"
    assert chat_completions_url(settings) == "https://gw.example/v1/chat/completions"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 120.5},
        {"max_images_per_source": 0},
        {"max_images_per_source": 65},
        {"max_image_bytes": 1023},
        {"max_image_bytes": 33_554_433},
        {"prompt_language": "fr"},
        {"api_key_env": "9KEY"},
        {"api_key_env": "sk-not-a-variable-name"},
        {"model": "  "},
        {"model": "deep\nseek"},
        {"unknown_key": 1},
    ],
)
def test_settings_reject_out_of_range_and_unknown_keys(kwargs):
    with pytest.raises(ValidationError):
        CircuitDiagramSettings(**kwargs)


def test_classify_kwargs_is_the_single_mapping_onto_the_transport():
    settings = CircuitDiagramSettings(
        base_url="https://gw.example", model="m", prompt_language="en"
    )

    assert classify_kwargs(settings, api_key="k") == {
        "url": "https://gw.example/chat/completions",
        "model": "m",
        "api_key": "k",
        "timeout_seconds": 30.0,
        "language": "en",
    }


# --------------------------------------------------------------------------
# Transport and parsing
# --------------------------------------------------------------------------


def test_the_request_carries_a_data_uri_a_bearer_token_and_the_model(monkeypatch):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)

    circuit_client.classify(
        _PNG,
        "image/png",
        url="https://gw.example/chat/completions",
        model="deepseek-flash",
        api_key="secret-key",
        timeout_seconds=7.5,
    )

    url, _body, headers, timeout = spy.calls[0]
    assert url == "https://gw.example/chat/completions"
    assert headers["Authorization"] == "Bearer secret-key"
    assert headers["Content-Type"] == "application/json"
    assert timeout == 7.5

    body = spy.body()
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "JSON" in parts[0]["text"]
    assert parts[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    )


def test_an_unsupported_media_type_never_reaches_the_wire(monkeypatch):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)

    with pytest.raises(circuit_client.CircuitDiagramError):
        circuit_client.classify(
            b"<svg/>",
            "image/svg+xml",
            url="https://gw.example/chat/completions",
            model="m",
            api_key="k",
            timeout_seconds=1.0,
        )
    assert spy.calls == []


def test_a_bare_json_answer_is_parsed():
    parsed = circuit_client.parse_classification(
        _answer_bytes(True, netlist="R1 1 0 1k", function="分压")
    )

    assert parsed == circuit_client.CircuitClassification(True, "R1 1 0 1k", "分压")


def test_a_fenced_json_answer_is_unwrapped():
    """Models fence their JSON often enough that this is the normal path."""

    fenced = '```json\n{"is_circuit": true, "netlist": "R1 1 0 1k"}\n```'
    parsed = circuit_client.parse_classification(_answer_text(fenced))

    assert parsed.is_circuit is True
    assert parsed.netlist == "R1 1 0 1k"
    # Absent keys fall back rather than raising: a model that answered the
    # question and stopped has answered the question.
    assert parsed.function == ""


def test_an_answer_that_is_not_json_at_all_raises():
    with pytest.raises(circuit_client.CircuitDiagramError):
        circuit_client.parse_classification(_answer_text("I cannot tell."))


def test_a_malformed_envelope_raises_rather_than_answering_false():
    with pytest.raises(circuit_client.CircuitDiagramError):
        circuit_client.parse_classification(b'{"choices": []}')


def test_json_embedded_in_prose_is_recovered_by_the_brace_scan():
    """The model that answered "Sure! {...} Hope that helps." without a fence."""

    prose = 'Sure! {"is_circuit": true, "netlist": "R1 1 0 1k"} Hope that helps.'
    parsed = circuit_client.parse_classification(_answer_text(prose))

    assert parsed.is_circuit is True
    assert parsed.netlist == "R1 1 0 1k"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ('"TRUE"', True),
        ('"true"', True),
        ("1", True),
        ('"1"', True),
        ("false", False),
        ('"False"', False),
        ("0", False),
        ("null", False),
        ("2", False),
    ],
)
def test_the_three_spellings_of_a_boolean_a_model_actually_emits(value, expected):
    parsed = circuit_client.parse_classification(
        _answer_text('{"is_circuit": %s}' % value)
    )

    assert parsed.is_circuit is expected


def test_a_non_string_netlist_falls_back_rather_than_raising():
    parsed = circuit_client.parse_classification(
        _answer_text('{"is_circuit": "TRUE", "netlist": 17, "function": null}')
    )

    assert parsed == circuit_client.CircuitClassification(True, "", "")


def test_no_setting_value_reaches_the_exception_message(monkeypatch):
    """SOP §3.6: settings never enter a log line, an exception or an event."""

    def _boom(url, body, headers, timeout):
        raise OSError("connection refused to https://gw.example")

    monkeypatch.setattr(circuit_client, "_post", _boom)

    with pytest.raises(circuit_client.CircuitDiagramError) as caught:
        circuit_client.classify(
            _PNG,
            "image/png",
            url="https://gw.example/chat/completions",
            model="corp-internal-model",
            api_key="secret-key",
            timeout_seconds=1.0,
        )
    message = str(caught.value)
    assert "gw.example" not in message
    assert "secret-key" not in message
    assert "corp-internal-model" not in message
    # And nothing is chained: a traceback renders ``__cause__``'s message too,
    # so ``raise … from exc`` would print the endpoint the caller never asked
    # to have logged.
    assert caught.value.__cause__ is None


def test_the_transport_refuses_to_follow_a_redirect():
    """The credential rides in a header; ``urllib`` would replay it verbatim.

    Exercised against the handler itself rather than a live 3xx: what makes a
    redirect an error is ``redirect_request`` answering ``None``, and that is
    the one line a future edit could drop.
    """

    handler = circuit_client._RefuseRedirects()

    assert (
        handler.redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.example/"
        )
        is None
    )
    # And the opener the transport actually uses is built with it.
    assert any(
        isinstance(handler, circuit_client._RefuseRedirects)
        for handler in circuit_client._OPENER.handlers
    )


# --------------------------------------------------------------------------
# The enricher
# --------------------------------------------------------------------------


def test_a_schematic_becomes_one_candidate_with_metadata_and_a_fenced_description(
    monkeypatch,
):
    spy = _PostSpy(_answer_bytes(True, netlist="R1 in out 10k", function="分压网络"))
    ref, context, assets, _token = _one_image(monkeypatch, spy)
    enricher = _enricher(monkeypatch)

    result = enricher.enrich(context)

    assert result.status is ExtensionResultStatus.AVAILABLE
    [candidate] = result.items
    # Identity, not equality: core matches the ref it minted with ``is``.
    assert candidate.element is ref
    assert candidate.metadata == {
        "is_circuit": True,
        "netlist": "R1 in out 10k",
        "function": "分压网络",
        "model": "deepseek-flash",
    }
    assert candidate.description == (
        "电路功能：分压网络\n\n```spice\nR1 in out 10k\n```"
    )
    assert assets.reads == [id(ref)]


def test_english_prompt_language_changes_the_heading(monkeypatch):
    spy = _PostSpy(_answer_bytes(True, netlist="R1 1 0 1k", function="A divider"))
    _ref, context, _assets, _token = _one_image(monkeypatch, spy)
    enricher = _enricher(
        monkeypatch, CircuitDiagramSettings(prompt_language="en")
    )

    [candidate] = enricher.enrich(context).items

    assert candidate.description.startswith("Circuit function: A divider")
    assert "```spice" in candidate.description


def test_a_non_schematic_produces_nothing_at_all(monkeypatch):
    spy = _PostSpy(_answer_bytes(False, netlist="", function=""))
    _ref, context, _assets, _token = _one_image(monkeypatch, spy)
    enricher = _enricher(monkeypatch)

    result = enricher.enrich(context)

    assert result.items == ()
    assert result.status is ExtensionResultStatus.AVAILABLE
    assert len(spy.calls) == 1


def test_non_images_missing_assets_and_unsupported_mimes_are_never_sent(monkeypatch):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    refs = [ElementRef(object()) for _ in range(4)]
    views = [
        _view(refs[0], element_type="paragraph", asset_id="", asset_mime=""),
        _view(refs[1], asset_id=""),
        _view(refs[2], asset_mime="image/svg+xml"),
        _view(refs[3], element_type="table", asset_id="a4"),
    ]
    context, assets, _token = _context(views, {id(ref): _PNG for ref in refs})
    enricher = _enricher(monkeypatch)

    result = enricher.enrich(context)

    assert result.items == ()
    assert result.status is ExtensionResultStatus.AVAILABLE
    assert spy.calls == []
    # Not even the bytes were read: filtering happens before the reader.
    assert assets.reads == []


def test_a_media_type_with_parameters_is_still_recognised(monkeypatch):
    spy = _PostSpy()
    ref = ElementRef(object())
    monkeypatch.setattr(circuit_client, "_post", spy)
    context, _assets, _token = _context(
        [_view(ref, asset_mime="image/PNG; charset=binary")], {id(ref): _PNG}
    )

    assert _enricher(monkeypatch).enrich(context).items
    assert spy.body()["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_max_images_per_source_bounds_the_number_of_requests(monkeypatch):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    refs = [ElementRef(object()) for _ in range(5)]
    context, _assets, _token = _context(
        [_view(ref) for ref in refs],
        {id(ref): _png(index) for index, ref in enumerate(refs)},
    )
    enricher = _enricher(
        monkeypatch, CircuitDiagramSettings(max_images_per_source=2)
    )

    result = enricher.enrich(context)

    assert len(spy.calls) == 2
    assert len(result.items) == 2


def test_the_point_wide_proposal_budget_bounds_it_too(monkeypatch):
    """``max_proposals`` may already be smaller than the deployment maximum.

    Exceeding it makes core discard this contribution's whole batch, so the
    plugin clamps rather than proposing something it knows will be refused.
    """

    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    refs = [ElementRef(object()) for _ in range(4)]
    context, _assets, _token = _context(
        [_view(ref) for ref in refs],
        {id(ref): _png(index) for index, ref in enumerate(refs)},
        max_proposals=1,
    )

    assert len(_enricher(monkeypatch).enrich(context).items) == 1
    assert len(spy.calls) == 1


def test_an_unreadable_or_oversized_asset_is_skipped_and_makes_the_batch_partial(
    monkeypatch,
):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    missing, oversized, good = (ElementRef(object()) for _ in range(3))
    context, _assets, _token = _context(
        [_view(missing), _view(oversized), _view(good)],
        {id(missing): None, id(oversized): b"x" * 5000, id(good): _png(1)},
    )
    enricher = _enricher(
        monkeypatch, CircuitDiagramSettings(max_image_bytes=4096)
    )

    result = enricher.enrich(context)

    assert len(spy.calls) == 1
    assert [candidate.element for candidate in result.items] == [good]
    assert result.status is ExtensionResultStatus.PARTIAL


def test_a_failing_request_skips_one_image_and_keeps_going(monkeypatch):
    spy = _PostSpy(OSError("refused"), _answer_bytes(True))
    monkeypatch.setattr(circuit_client, "_post", spy)
    first, second = ElementRef(object()), ElementRef(object())
    context, _assets, _token = _context(
        [_view(first), _view(second)], {id(first): _png(1), id(second): _png(2)}
    )

    result = _enricher(monkeypatch).enrich(context)

    assert len(spy.calls) == 2
    assert [candidate.element for candidate in result.items] == [second]
    assert result.status is ExtensionResultStatus.PARTIAL


def test_an_unparseable_answer_skips_one_image_and_keeps_going(monkeypatch):
    spy = _PostSpy(_answer_text("no idea"), _answer_bytes(True))
    monkeypatch.setattr(circuit_client, "_post", spy)
    first, second = ElementRef(object()), ElementRef(object())
    context, _assets, _token = _context(
        [_view(first), _view(second)], {id(first): _png(1), id(second): _png(2)}
    )

    result = _enricher(monkeypatch).enrich(context)

    assert [candidate.element for candidate in result.items] == [second]
    assert result.status is ExtensionResultStatus.PARTIAL


# --------------------------------------------------------------------------
# Budget, cancellation and the two "cannot serve" states
# --------------------------------------------------------------------------


def test_a_spent_deadline_stops_before_the_next_image_and_reports_partial(
    monkeypatch,
):
    """One image classified, the second refused: ``PARTIAL``, not abandoned.

    The fake clock is what makes this deterministic: the real one advances by
    microseconds inside a test, so the second iteration would still fit.
    """

    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    monkeypatch.setattr(
        circuit_enricher, "time", _FakeTimeModule(_Clock(0.0, 95.0))
    )
    first, second = ElementRef(object()), ElementRef(object())
    context, _assets, _token = _context(
        [_view(first), _view(second)],
        {id(first): _png(1), id(second): _png(2)},
        deadline=100.0,
    )
    enricher = _enricher(monkeypatch, CircuitDiagramSettings(timeout_seconds=10.0))

    result = enricher.enrich(context)

    assert len(spy.calls) == 1
    assert [candidate.element for candidate in result.items] == [first]
    assert result.status is ExtensionResultStatus.PARTIAL


def test_a_deadline_too_short_for_even_one_image_is_unavailable(monkeypatch):
    """``UNAVAILABLE`` is discarded whole by core, which is what is wanted.

    Nothing was classified, so there is nothing to persist, and saying
    ``PARTIAL`` with no items would file this contributor as having served a
    call it never started.
    """

    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    monkeypatch.setattr(circuit_enricher, "time", _FakeTimeModule(_Clock(99.0)))
    ref = ElementRef(object())
    context, _assets, _token = _context(
        [_view(ref)], {id(ref): _PNG}, deadline=100.0
    )
    enricher = _enricher(monkeypatch, CircuitDiagramSettings(timeout_seconds=10.0))

    result = enricher.enrich(context)

    assert spy.calls == []
    assert result.items == ()
    assert result.status is ExtensionResultStatus.UNAVAILABLE
    assert result.failure is not None
    assert result.failure.code == "budget_exhausted"
    assert result.failure.kind is ExtensionFailureKind.UNAVAILABLE


def test_cancellation_is_checked_before_every_outbound_request(monkeypatch):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    refs = [ElementRef(object()) for _ in range(3)]
    token = _Cancellation()
    context, _assets, _token = _context(
        [_view(ref) for ref in refs],
        {id(ref): _png(index) for index, ref in enumerate(refs)},
        cancellation=token,
    )

    _enricher(monkeypatch).enrich(context)

    assert token.checks == 3


def test_a_cancelled_token_stops_the_run_immediately(monkeypatch):
    """Raising is core's own contract for this point, not a plugin fault.

    Core checks cancellation on every join slice and raises on the calling
    thread, so this raise never becomes the observable outcome — what it buys
    is that a cancelled parse stops paying for outbound requests at once.
    """

    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    ref = ElementRef(object())
    context, _assets, _token = _context(
        [_view(ref)], {id(ref): _PNG}, cancellation=_Cancellation(cancelled=True)
    )

    with pytest.raises(RuntimeError):
        _enricher(monkeypatch).enrich(context)
    assert spy.calls == []


def test_an_unconfigured_or_uncredentialled_enricher_never_dials(monkeypatch):
    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    ref = ElementRef(object())
    context, _assets, _token = _context([_view(ref)], {id(ref): _PNG})

    unconfigured = _bare_enricher(lambda: None)
    assert unconfigured.enrich(context).failure.code == "not_configured"

    monkeypatch.delenv(_KEY_ENV, raising=False)
    configured = _bare_enricher(lambda: CircuitDiagramSettings())
    assert configured.enrich(context).failure.code == "api_key_missing"
    assert spy.calls == []


# --------------------------------------------------------------------------
# Ceilings on what is persisted
# --------------------------------------------------------------------------


def test_a_huge_netlist_is_cut_to_the_plugins_own_ceiling(monkeypatch):
    spy = _PostSpy(_answer_bytes(True, netlist="R" * 9000, function="F" * 4000))
    _ref, context, _assets, _token = _one_image(monkeypatch, spy)

    [candidate] = _enricher(monkeypatch).enrich(context).items

    assert len(candidate.metadata["netlist"]) == circuit_enricher.NETLIST_MAX_CHARS
    assert len(candidate.metadata["function"]) == circuit_enricher.FUNCTION_MAX_CHARS
    # Metadata and description carry the same text, so a reader comparing the
    # two can never find a netlist in one that is absent from the other.
    assert candidate.metadata["netlist"] in candidate.description
    assert candidate.metadata["function"] in candidate.description


def test_the_description_is_cut_to_cores_own_budget_scaffolding_included(
    monkeypatch,
):
    """Core discards the whole batch for one over-long description.

    The ``function`` alone is longer than the limit here, so a cut that
    ignored the heading and the fence would overshoot by exactly that
    scaffolding — which is the bug this pins.
    """

    spy = _PostSpy(_answer_bytes(True, netlist="R" * 900, function="F" * 400))
    _ref, context, _assets, _token = _one_image(
        monkeypatch, spy, max_description_chars=200
    )

    [candidate] = _enricher(monkeypatch).enrich(context).items

    assert len(candidate.description) <= 200
    assert candidate.description.endswith("```")
    assert candidate.metadata["function"] in candidate.description
    assert candidate.metadata["netlist"] in candidate.description


def test_a_description_limit_below_the_scaffolding_yields_no_description(
    monkeypatch,
):
    """Nothing would fit, so nothing is proposed — the metadata still stands."""

    spy = _PostSpy(_answer_bytes(True, netlist="R1 1 0 1k", function="分压"))
    _ref, context, _assets, _token = _one_image(
        monkeypatch, spy, max_description_chars=5
    )

    [candidate] = _enricher(monkeypatch).enrich(context).items

    assert candidate.description == ""
    assert candidate.metadata["netlist"] == "R1 1 0 1k"


def test_the_byte_budget_is_core_s_own_accounting_not_an_estimate(monkeypatch):
    """Stop exactly where core would, computed with core's own function.

    Core charges a candidate for the whole persisted envelope — provenance,
    JSON quoting, and the description *twice* (once inside the subtree, once
    appended to the element's retrievable text).  An estimate that misses any
    of that under-counts, and under-counting means proposing a batch core
    refuses whole, losing the images that did fit.
    """

    spy = _PostSpy(
        _answer_bytes(True, netlist="R1 in out 10k\n" * 300, function="分压")
    )
    monkeypatch.setattr(circuit_client, "_post", spy)
    refs = [ElementRef(object()) for _ in range(6)]
    payloads = {id(ref): _png(index) for index, ref in enumerate(refs)}
    views = [_view(ref) for ref in refs]

    # What the whole batch costs, when nothing stands in its way.
    generous, _assets, _token = _context(views, payloads, max_metadata_bytes=10**7)
    full = _enricher(monkeypatch).enrich(generous).items
    assert len(full) == 6
    sizes = [_persisted_size(candidate) for candidate in full]
    assert min(sizes) > 4000, "the netlist ceiling should make these substantial"

    # A budget with room for exactly three of them, one byte short of a fourth.
    budget = sum(sizes[:3]) + sizes[3] - 1
    context, _assets, _token = _context(views, payloads, max_metadata_bytes=budget)

    result = _enricher(monkeypatch).enrich(context)

    assert len(result.items) == 3
    spent = sum(_persisted_size(candidate) for candidate in result.items)
    assert spent <= budget
    assert spent + sizes[3] > budget
    assert result.status is ExtensionResultStatus.PARTIAL


# --------------------------------------------------------------------------
# Shaping the model's own answer
# --------------------------------------------------------------------------


def test_unprintable_characters_are_replaced_so_core_admits_the_description(
    monkeypatch,
):
    """One image's formatting habit must not cost the whole batch.

    Core refuses a description carrying anything that is neither printable nor
    a newline/tab, and refusing it discards this contribution's entire batch.
    U+3000 and NBSP are what a model writing Chinese prose emits; U+200B and
    the BOM are what a pasted datasheet carries.
    """

    from app.extensions.element_enrichment import _printable_description

    spy = _PostSpy(
        _answer_bytes(
            True,
            netlist="R1​in﻿out 10k",
            function="分压　网络 说明",
        )
    )
    _ref, context, _assets, _token = _one_image(monkeypatch, spy)

    [candidate] = _enricher(monkeypatch).enrich(context).items

    assert _printable_description(candidate.description) is True
    assert _printable_description(candidate.metadata["netlist"]) is True
    assert candidate.metadata["function"] == "分压 网络 说明"
    assert candidate.metadata["netlist"] == "R1 in out 10k"


def test_a_netlist_the_model_already_fenced_is_unfenced_before_it_is_nested(
    monkeypatch,
):
    """A nested fence ends the front end's code block three lines early."""

    spy = _PostSpy(
        _answer_bytes(True, netlist="```spice\nR1 1 0 1k\nR2 1 0 2k\n```")
    )
    _ref, context, _assets, _token = _one_image(monkeypatch, spy)

    [candidate] = _enricher(monkeypatch).enrich(context).items

    assert candidate.metadata["netlist"] == "R1 1 0 1k\nR2 1 0 2k"
    assert candidate.description.count("```") == 2


def test_a_schematic_with_neither_netlist_nor_summary_produces_no_candidate(
    monkeypatch,
):
    """``is_circuit`` alone is provenance with no content behind it."""

    spy = _PostSpy(_answer_bytes(True, netlist="   ```   ", function=""))
    _ref, context, _assets, _token = _one_image(monkeypatch, spy)

    result = _enricher(monkeypatch).enrich(context)

    assert result.items == ()
    assert result.status is ExtensionResultStatus.PARTIAL


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


def test_identical_image_bytes_are_classified_once_and_proposed_twice(
    monkeypatch,
):
    """A header logo repeated on every page is one answer, not forty."""

    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    first, second, other = (ElementRef(object()) for _ in range(3))
    context, _assets, _token = _context(
        [_view(first), _view(second), _view(other)],
        {id(first): _png(1), id(second): _png(1), id(other): _png(2)},
    )

    result = _enricher(monkeypatch).enrich(context)

    # Two distinct digests, two requests — and three candidates.
    assert len(spy.calls) == 2
    assert [candidate.element for candidate in result.items] == [
        first,
        second,
        other,
    ]
    assert result.status is ExtensionResultStatus.AVAILABLE


def test_a_repeat_of_an_already_classified_image_survives_a_spent_deadline(
    monkeypatch,
):
    """A cache hit costs nothing, so the budget gate must not refuse it."""

    spy = _PostSpy()
    monkeypatch.setattr(circuit_client, "_post", spy)
    monkeypatch.setattr(
        circuit_enricher, "time", _FakeTimeModule(_Clock(0.0, 95.0))
    )
    first, repeat = ElementRef(object()), ElementRef(object())
    context, _assets, _token = _context(
        [_view(first), _view(repeat)],
        {id(first): _png(1), id(repeat): _png(1)},
        deadline=100.0,
    )
    enricher = _enricher(monkeypatch, CircuitDiagramSettings(timeout_seconds=10.0))

    result = enricher.enrich(context)

    assert len(spy.calls) == 1
    assert [candidate.element for candidate in result.items] == [first, repeat]
    assert result.status is ExtensionResultStatus.AVAILABLE


def test_a_failed_classification_is_not_remembered(monkeypatch):
    """A refused request says nothing about the image, so it is retried."""

    spy = _PostSpy(OSError("refused"), _answer_bytes(True))
    monkeypatch.setattr(circuit_client, "_post", spy)
    first, repeat = ElementRef(object()), ElementRef(object())
    context, _assets, _token = _context(
        [_view(first), _view(repeat)], {id(first): _png(1), id(repeat): _png(1)}
    )

    result = _enricher(monkeypatch).enrich(context)

    assert len(spy.calls) == 2
    assert [candidate.element for candidate in result.items] == [repeat]
