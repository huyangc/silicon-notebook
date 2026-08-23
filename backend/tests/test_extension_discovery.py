"""Deployment plugin discovery: config slot, loading, settings, capabilities.

Every fake plugin here is written to a real ``.py`` file and imported through
``importlib`` on a real ``sys.path`` entry.  Monkeypatching the import would
skip exactly the machinery discovery exists to contain — a plugin module that
raises on import, exposes the wrong object, or ships a mismatched manifest.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import textwrap

import pytest

from app.core.config import Settings
from app.extension_sdk import Availability, AvailabilityStatus
from app.extensions.discovery import (
    DiscoveredExtension,
    ExtensionDiscoveryError,
    capability_decisions_from_bundles,
    discover_deployment_extensions,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact shipped topology.  Hard-coded rather than derived: the point of
# this list is to fail loudly when an unset EXTENSIONS_CONFIG stops producing
# byte-identical builtin composition, and a derived expectation cannot do that.
_BUILTIN_PLUGIN_IDS = (
    "builtin.ask_agent_profile",
    "builtin.ask_retrieval_experience",
    "builtin.ask_search_profile",
    "builtin.report_agent_profile",
    "builtin.report_markdown_exporter",
    "parser.builtin",
    "builtin.generated_question",
    "parser.mineru_cloud",
    "builtin.selected_source_graph",
    "parser.mineru_self_hosted",
)
_BUILTIN_POINTS = {
    "ask.completed_observer": [
        "builtin.ask_agent_profile",
        "builtin.ask_retrieval_experience",
        "builtin.ask_search_profile",
    ],
    "report.completed_observer": ["builtin.report_agent_profile"],
    "report.exporter": ["builtin.report_markdown"],
    "retrieval.contributor": [
        "builtin.generated_question",
        "builtin.selected_source_graph",
    ],
    "source.parser_chain": [
        "parser.mineru_self_hosted",
        "parser.mineru_cloud",
        "parser.builtin",
    ],
}


# --------------------------------------------------------------------------
# Shared fixtures (§3.1)
# --------------------------------------------------------------------------


@pytest.fixture
def frozen_runtime_reset():
    """Touching EXTENSIONS_CONFIG invalidates three process-wide lru_caches.

    ``get_settings`` holds the path, ``default_extension_runtime`` holds the
    frozen topology built from it, and ``deps.repository`` holds a facade wired
    to that runtime.  Clear all three on the way in *and* on the way out: on the
    way in because an earlier test may have warmed them, on the way out because
    the next test must not inherit a topology built from this test's config.

    There is a fourth holder these ``cache_clear()`` calls cannot reach:
    ``app.main`` builds ``app`` at module scope, and that application's
    ``app.state.extension_ui_projection`` closes over whichever runtime existed
    at first import.  It is harmless today only because nothing in this module
    reuses that module-level app — every test that needs an application calls
    ``create_app()`` itself after clearing.  A future test that imports
    ``app.main.app`` and expects it to reflect a config set here would be
    silently wrong.
    """

    def _clear() -> None:
        from app.api import deps
        from app.core.config import get_settings
        from app.extensions.bootstrap import default_extension_runtime

        get_settings.cache_clear()
        default_extension_runtime.cache_clear()
        deps.repository.cache_clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _plugin_import_isolation():
    """Undo every trace of the real imports the helpers below perform."""

    path_before = list(sys.path)
    modules_before = set(sys.modules)
    yield
    sys.path[:] = path_before
    for name in sorted(set(sys.modules) - modules_before):
        if name.startswith(_MODULE_PREFIX):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


_DEFAULT_BODY = """
from dataclasses import dataclass

from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest

CALLS: list[str] = []


@dataclass
class Bundle:
    manifest: ExtensionManifest

    def register(self, registrar) -> None:
        CALLS.append("register")


bundle = Bundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="Sample deployment plugin",
    trust="deployment",
    contributions=(),
))
"""


_MODULE_PREFIX = "sn_test_plugin_"


def _module_name(plugin_id: str) -> str:
    return _MODULE_PREFIX + re.sub(r"[^0-9a-zA-Z]", "_", plugin_id)


def _write_plugin_package(
    tmp_path: Path,
    *,
    plugin_id: str = "corp.sample",
    body: str = _DEFAULT_BODY,
) -> Path:
    """Write an importable plugin module and put its directory on ``sys.path``.

    ``PLUGIN_ID`` is injected as a module-level name so bodies can reference it
    without brace-escaping their own dict literals.  Teardown is the autouse
    ``_plugin_import_isolation`` fixture.
    """

    root = tmp_path / "plugin_root"
    root.mkdir(exist_ok=True)
    module = root / f"{_module_name(plugin_id)}.py"
    module.write_text(
        f"PLUGIN_ID = {plugin_id!r}\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    return module


def _write_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "extensions.toml"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return str(path)


def _entry(
    module: Path,
    *,
    plugin_id: str = "corp.sample",
    attribute: str = "bundle",
    extra: str = "",
) -> str:
    return (
        f'[extensions."{plugin_id}"]\n'
        f'bundle = "{module.stem}:{attribute}"\n'
        f"{extra}"
    )


def _runtime(monkeypatch, config_path: str):
    """Rebuild the process-wide frozen topology from ``config_path``."""

    from app.core.config import get_settings
    from app.extensions.bootstrap import default_extension_runtime

    monkeypatch.setenv("EXTENSIONS_CONFIG", config_path)
    get_settings.cache_clear()
    default_extension_runtime.cache_clear()
    return default_extension_runtime()


# --------------------------------------------------------------------------
# T1 — configuration slot and verification isolation
# --------------------------------------------------------------------------


def test_extensions_config_defaults_to_empty_and_anchors_relative_paths(
    monkeypatch, tmp_path
):
    import app.core.config as config_module

    # conftest hard-assigns EXTENSIONS_CONFIG="" for the whole session, and
    # pydantic-settings reads the environment: without dropping it here the
    # "defaults to empty" assertion would hold for any Field default at all.
    monkeypatch.delenv("EXTENSIONS_CONFIG", raising=False)
    assert Settings(_env_file=None).extensions_config == ""

    monkeypatch.setattr(config_module, "_ROOT_DIR", tmp_path)
    relative = Settings(_env_file=None, extensions_config="extensions.toml")
    assert relative.extensions_config == str(tmp_path / "extensions.toml")
    assert Path(relative.extensions_config).is_absolute()

    absolute_path = str(tmp_path / "abs" / "extensions.toml")
    absolute = Settings(_env_file=None, extensions_config=absolute_path)
    assert absolute.extensions_config == absolute_path


def test_verification_entrypoints_clear_extensions_config(frozen_runtime_reset):
    check_sh = (_REPO_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    assert 'export EXTENSIONS_CONFIG=""' in check_sh

    conftest = (_REPO_ROOT / "backend" / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ["EXTENSIONS_CONFIG"] = ""' in conftest

    # Source text alone proves nothing: the conftest line could be inside a
    # branch that never runs, and `check.sh` could export it after the point
    # where it matters.  Assert the effect that both lines exist to produce —
    # this process, right now, resolves no deployment plugin config at all.
    from app.core.config import get_settings

    assert os.environ.get("EXTENSIONS_CONFIG") == ""
    assert get_settings().extensions_config == ""


# --------------------------------------------------------------------------
# Discovery: the named list, and only the named list
# --------------------------------------------------------------------------


def test_unset_config_yields_the_exact_builtin_topology(
    monkeypatch, frozen_runtime_reset
):
    runtime = _runtime(monkeypatch, "")

    registry = runtime.registry
    assert [manifest.id for manifest in registry.manifests()] == list(
        _BUILTIN_PLUGIN_IDS
    )
    for point, contribution_ids in _BUILTIN_POINTS.items():
        assert [
            registered.contribution.declaration.id
            for registered in registry.contributions(point)
        ] == contribution_ids, point
    assert dict(runtime.plugin_settings) == {}
    assert discover_deployment_extensions("") == ()
    assert discover_deployment_extensions("   ") == ()


def test_named_and_enabled_plugin_is_discovered_and_frozen(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    module = _write_plugin_package(tmp_path)
    config = _write_config(tmp_path, _entry(module))

    discovered = discover_deployment_extensions(config)
    assert [item.plugin_id for item in discovered] == ["corp.sample"]
    assert isinstance(discovered[0], DiscoveredExtension)
    assert discovered[0].settings is None

    runtime = _runtime(monkeypatch, config)
    assert [manifest.id for manifest in runtime.registry.manifests()] == [
        *_BUILTIN_PLUGIN_IDS,
        "corp.sample",
    ]
    assert runtime.registry.frozen is True
    assert sys.modules[module.stem].CALLS == ["register"]


def test_disabled_plugin_is_not_loaded_and_not_imported(tmp_path):
    module = _write_plugin_package(tmp_path)
    config = _write_config(
        tmp_path, _entry(module, extra="enabled = false\n")
    )

    assert discover_deployment_extensions(config) == ()
    # Not merely dropped afterwards: the module was never imported at all.
    assert module.stem not in sys.modules


def test_discovered_plugins_are_appended_after_builtins_and_sorted_by_id(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    first = _write_plugin_package(tmp_path, plugin_id="corp.aaa")
    second = _write_plugin_package(tmp_path, plugin_id="corp.zzz")
    # Declared in reverse order on purpose: ordering must come from the id,
    # not from the operator's file layout.
    config = _write_config(
        tmp_path,
        _entry(second, plugin_id="corp.zzz") + _entry(first, plugin_id="corp.aaa"),
    )

    assert [item.plugin_id for item in discover_deployment_extensions(config)] == [
        "corp.aaa",
        "corp.zzz",
    ]
    runtime = _runtime(monkeypatch, config)
    assert [manifest.id for manifest in runtime.registry.manifests()] == [
        *_BUILTIN_PLUGIN_IDS,
        "corp.aaa",
        "corp.zzz",
    ]


# --------------------------------------------------------------------------
# Discovery: rejection ordering and stable reason codes
# --------------------------------------------------------------------------


def test_missing_config_file_fails_startup(tmp_path):
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(str(tmp_path / "absent.toml"))
    assert excinfo.value.reason == "config_unreadable"
    assert excinfo.value.plugin_id == ""
    assert excinfo.value.exception_type == "FileNotFoundError"
    # The path itself may be a deployment secret; never echo it.
    assert "absent.toml" not in str(excinfo.value)


def test_invalid_toml_fails_startup(tmp_path):
    config = _write_config(tmp_path, "this is not = = toml\n")
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "config_invalid_toml"
    assert excinfo.value.__cause__ is None


def test_unknown_top_level_table_fails_startup(tmp_path):
    config = _write_config(tmp_path, "[plugins]\nfoo = 1\n")
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "config_unknown_top_level_key"
    assert excinfo.value.names == ("plugins",)


def test_bundle_attribute_missing_fails_startup(tmp_path):
    module = _write_plugin_package(tmp_path)
    config = _write_config(tmp_path, _entry(module, attribute="not_here"))
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_attribute_missing"
    assert excinfo.value.plugin_id == "corp.sample"


def test_object_that_is_not_a_bundle_fails_startup(tmp_path):
    module = _write_plugin_package(
        tmp_path, body=_DEFAULT_BODY + "\nnot_a_bundle = object()\n"
    )
    config = _write_config(tmp_path, _entry(module, attribute="not_a_bundle"))
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_not_a_bundle"


def test_manifest_id_must_equal_the_config_key(tmp_path):
    module = _write_plugin_package(tmp_path, plugin_id="corp.sample")
    config = _write_config(
        tmp_path,
        f'[extensions."corp.other"]\nbundle = "{module.stem}:bundle"\n',
    )
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_id_mismatch"
    assert excinfo.value.plugin_id == "corp.other"


def test_trust_must_be_deployment(tmp_path):
    module = _write_plugin_package(
        tmp_path, body=_DEFAULT_BODY.replace('"deployment"', '"builtin"')
    )
    config = _write_config(tmp_path, _entry(module))
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_trust_not_deployment"


def test_api_version_mismatch_fails_startup(tmp_path):
    module = _write_plugin_package(
        tmp_path,
        body=_DEFAULT_BODY.replace(
            "api_version=EXTENSION_API_VERSION,", 'api_version="99",'
        ),
    )
    config = _write_config(tmp_path, _entry(module))
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_api_version_unsupported"


@pytest.mark.parametrize(
    "config_text, reason",
    [
        ('[extensions]\n"Corp Sample" = { bundle = "m:b" }\n', "plugin_id_invalid"),
        ('[extensions]\n"corp.sample" = 7\n', "plugin_entry_not_a_table"),
        (
            '[extensions."corp.sample"]\nbundle = "m:b"\nnickname = "x"\n',
            "plugin_unknown_key",
        ),
        (
            '[extensions."corp.sample"]\nbundle = "m:b"\nenabled = "yes"\n',
            "plugin_enabled_not_bool",
        ),
        ('[extensions."corp.sample"]\nenabled = true\n', "plugin_bundle_missing"),
        (
            '[extensions."corp.sample"]\nbundle = "no_colon_here"\n',
            "plugin_bundle_spec_invalid",
        ),
        (
            '[extensions."corp.sample"]\nbundle = "mod:not an identifier"\n',
            "plugin_bundle_spec_invalid",
        ),
        (
            '[extensions."corp.sample"]\nbundle = "sn_test_absent_module:bundle"\n',
            "plugin_module_import_failed",
        ),
        ("extensions = 5\n", "config_extensions_not_a_table"),
    ],
)
def test_malformed_config_entries_fail_with_stable_reason_codes(
    tmp_path, config_text, reason
):
    config = _write_config(tmp_path, config_text)
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == reason
    assert excinfo.value.__cause__ is None


_IMPORT_TIME_FAILURE_BODY = """
raise {exception}("TOPSECRET")
"""


def test_plugin_import_failure_is_sanitized_to_a_class_name(tmp_path, caplog):
    module = _write_plugin_package(
        tmp_path,
        body=_IMPORT_TIME_FAILURE_BODY.format(exception="RuntimeError"),
    )
    config = _write_config(tmp_path, _entry(module))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ExtensionDiscoveryError) as excinfo:
            discover_deployment_extensions(config)

    error = excinfo.value
    assert error.reason == "plugin_module_import_failed"
    assert error.exception_type == "RuntimeError"
    assert error.plugin_id == "corp.sample"
    assert "TOPSECRET" not in str(error) + repr(error) + caplog.text
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_a_custom_base_exception_on_import_is_still_converted(tmp_path):
    """Only KeyboardInterrupt and SystemExit get out; nothing else does.

    ``except Exception`` would let any other ``BaseException`` subclass — whose
    message the plugin wrote — propagate raw.
    """

    module = _write_plugin_package(
        tmp_path,
        body=(
            "class CorpAbort(BaseException):\n    pass\n\n\n"
            'raise CorpAbort("TOPSECRET")\n'
        ),
    )
    config = _write_config(tmp_path, _entry(module))

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_module_import_failed"
    assert excinfo.value.exception_type == "CorpAbort"
    assert "TOPSECRET" not in str(excinfo.value)


def test_system_exit_during_plugin_import_propagates_but_is_recorded(
    tmp_path, caplog
):
    module = _write_plugin_package(
        tmp_path, body='import sys\n\nsys.exit("TOPSECRET")\n'
    )
    config = _write_config(tmp_path, _entry(module))

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.extensions"):
        # Untouched: an operator's interrupt, or a plugin calling sys.exit
        # during import, must still stop the process.
        with pytest.raises(SystemExit):
            discover_deployment_extensions(config)

    message = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "silicon_notebook.extensions"
    )
    # ...but the one fact that would otherwise be lost — which plugin's import
    # the interpreter was inside — is recorded, with no exception text.
    assert "plugin=corp.sample" in message
    assert "reason=plugin_module_import_interrupted" in message
    assert "TOPSECRET" not in caplog.text
    assert module.stem not in caplog.text


# --------------------------------------------------------------------------
# Registration: the plugin's own register() may raise anything
# --------------------------------------------------------------------------


_REGISTER_RAISES_BODY = """
from dataclasses import dataclass

from pydantic import BaseModel

from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest


class SampleSettings(BaseModel):
    token: str = ""


@dataclass
class Bundle:
    manifest: ExtensionManifest
    settings_model: object = SampleSettings
    settings: object = None

    def configure(self, settings) -> None:
        self.settings = settings

    def register(self, registrar) -> None:
        raise ValueError(f"corp backend rejected {self.settings.token}")


bundle = Bundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="Sample deployment plugin",
    trust="deployment",
    contributions=(),
))
"""


def test_deployment_register_failure_is_sanitized_and_logged(
    monkeypatch, tmp_path, caplog, frozen_runtime_reset
):
    """``register()`` runs plugin code holding plugin settings — the last hole.

    Settings binding is sanitized, capability merge is sanitized, but the
    registration call happens during composition; without conversion a plugin's
    own exception message (here, its API token) reaches the traceback and the
    startup log verbatim.
    """

    module = _write_plugin_package(tmp_path, body=_REGISTER_RAISES_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\ntoken = "TOPSECRET"\n',
        ),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ExtensionDiscoveryError) as excinfo:
            _runtime(monkeypatch, config)

    error = excinfo.value
    assert error.reason == "plugin_registration_failed"
    assert error.exception_type == "ValueError"
    assert error.plugin_id == "corp.sample"
    assert "TOPSECRET" not in str(error) + repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    # The composition root logs it, so the rejection is not silent — and the
    # log line carries the same three sanitized fields, nothing else.
    assert "TOPSECRET" not in caplog.text
    assert "reason=plugin_registration_failed" in caplog.text
    assert "exc=ValueError" in caplog.text


def test_builtin_register_failure_keeps_its_verbatim_registry_error():
    """Core's own diagnostics are not plugin-controlled and stay readable."""

    from dataclasses import dataclass

    from app.extension_sdk import (
        EXTENSION_API_VERSION,
        ContributionDeclaration,
        ContributionKind,
        ExtensionManifest,
    )
    from app.extensions.registry import ExtensionRegistryError, frozen_registry

    @dataclass
    class Bundle:
        manifest: ExtensionManifest

        def register(self, registrar) -> None:
            raise ExtensionRegistryError("core diagnostic text")

    manifest = ExtensionManifest(
        id="builtin.sample",
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Built-in sample",
        trust="builtin",
        contributions=(
            ContributionDeclaration(
                id="builtin.sample.one",
                point="ask.completed_observer",
                kind=ContributionKind.OBSERVER,
            ),
        ),
    )

    with pytest.raises(ExtensionRegistryError) as excinfo:
        frozen_registry([Bundle(manifest)])
    assert "core diagnostic text" in str(excinfo.value)


# --------------------------------------------------------------------------
# Settings binding and secret hygiene
# --------------------------------------------------------------------------


_SETTINGS_BODY = """
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest

CALLS: list[str] = []
BOUND: list[object] = []


class SampleSettings(BaseModel):
    endpoint: str = ""
    token: str = ""
    retries: int = 0


@dataclass
class Bundle:
    manifest: ExtensionManifest
    settings_model: object = SampleSettings

    def configure(self, settings) -> None:
        CALLS.append("configure")
        BOUND.append(settings)

    def register(self, registrar) -> None:
        CALLS.append("register")


bundle = Bundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="Sample deployment plugin",
    trust="deployment",
    contributions=(),
))
"""


def test_settings_unknown_key_fails_and_error_never_contains_the_value(tmp_path):
    module = _write_plugin_package(tmp_path, body=_SETTINGS_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\napi_key = "TOPSECRET"\n',
        ),
    )
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)

    error = excinfo.value
    assert error.reason == "plugin_settings_unknown_key"
    # The offending key name is what an operator needs; the value never is.
    assert error.names == ("api_key",)
    assert "api_key" in str(error)
    assert "TOPSECRET" not in str(error)
    assert "TOPSECRET" not in repr(error)
    assert error.__cause__ is None


def test_settings_type_error_fails_and_error_never_contains_the_value(tmp_path):
    module = _write_plugin_package(tmp_path, body=_SETTINGS_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\nretries = "TOPSECRET"\n',
        ),
    )
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)

    error = excinfo.value
    assert error.reason == "plugin_settings_invalid"
    assert error.exception_type == "ValidationError"
    # pydantic's ValidationError repeats the rejected input; stringifying it
    # here would print the secret into the log and the traceback.
    assert "TOPSECRET" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


_SECRET_UI_BODY = """
from dataclasses import dataclass

from pydantic import BaseModel

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    Availability,
    ExtensionManifest,
    UiContributionDeclaration,
)

BOUND: list[object] = []


class SampleSettings(BaseModel):
    # Deliberately a plain `str`, not `SecretStr`: SecretStr would redact the
    # value in every repr all by itself, so the assertions below would pass no
    # matter what core does with the mapping.  Core must hold the line for a
    # plugin that did not opt into redaction.
    token: str


@dataclass
class Bundle:
    manifest: ExtensionManifest
    settings_model: object = SampleSettings
    capability_decisions: object = None

    def configure(self, settings) -> None:
        BOUND.append(settings)

    def register(self, registrar) -> None:
        return None


bundle = Bundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Sample deployment plugin",
        trust="deployment",
        contributions=(),
        provides=("corp.sample.available",),
        ui_contributions=(UiContributionDeclaration(
            id="corp.sample.panel",
            slot="workspace.side_panel",
            capability="corp.sample.available",
        ),),
    ),
    capability_decisions={
        "corp.sample.available": lambda _context: Availability.available(),
    },
)
"""


def test_secret_settings_never_reach_logs_or_projections(
    monkeypatch, tmp_path, caplog, frozen_runtime_reset
):
    module = _write_plugin_package(tmp_path, body=_SECRET_UI_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\ntoken = "TOPSECRET"\n',
        ),
    )

    with caplog.at_level(logging.DEBUG):
        runtime = _runtime(monkeypatch, config)

    from app.extensions.ui_projection import ui_contribution_contract

    contract = json.dumps(ui_contribution_contract(runtime.registry))
    assert "corp.sample.panel" in contract
    assert "TOPSECRET" not in contract
    assert "TOPSECRET" not in caplog.text
    # The plugin really did receive it — the value exists, it just never leaves.
    assert sys.modules[module.stem].BOUND[0].token == "TOPSECRET"
    # `repr(runtime)` is what an unhandled exception's frame locals, a debugger,
    # and most log formatters print.  The settings mapping must not be in it.
    assert "TOPSECRET" not in repr(runtime)
    assert "plugin_settings" not in repr(runtime)
    # Still reachable on purpose — this is a redaction of the default rendering,
    # not of the value; T5's route host reads it deliberately.
    assert runtime.plugin_settings["corp.sample"].token == "TOPSECRET"
    # TODO(T4): also assert the /admin/extensions projection
    # (app.extensions.admin_projection.project_loaded_extensions) carries no
    # settings value once that surface lands.


def test_scalar_settings_value_is_rejected_before_it_is_read_as_a_key_set(
    tmp_path,
):
    """``settings = "hunter2XYZ"`` must not be spelled out one letter at a time.

    Without the ``isinstance(table, dict)`` guard the very next statement is
    ``set(table) - accepted``, and ``set()`` over a string yields its
    *characters* — which are then reported to the operator, and written to the
    log, as the offending "key" names.
    """

    secret = "hunter2XYZ"
    # A plugin that *does* accept settings: with a plugin that accepts none, the
    # "no settings_model" branch rejects the entry first and this never reaches
    # the key-set arithmetic it is here to pin.
    module = _write_plugin_package(tmp_path, body=_SETTINGS_BODY)
    config = _write_config(
        tmp_path, _entry(module, extra=f'settings = "{secret}"\n')
    )

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)

    error = excinfo.value
    assert error.reason == "plugin_settings_not_a_table"
    assert error.plugin_id == "corp.sample"
    assert error.names == ()
    assert error.__cause__ is None
    rendered = str(error) + repr(error)
    assert secret not in rendered
    # The character multiset is the leak this guard exists to prevent: with a
    # 10-character secret, ten single-quoted single characters is the secret.
    for character in set(secret):
        assert f"'{character}'" not in rendered, character


_HALF_BOUND_BODY = """
from dataclasses import dataclass

from pydantic import BaseModel

from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest


class SampleSettings(BaseModel):
    endpoint: str = ""


@dataclass
class Bundle:
    manifest: ExtensionManifest
    settings_model: object = SampleSettings

    def register(self, registrar) -> None:
        return None


bundle = Bundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="Sample deployment plugin",
    trust="deployment",
    contributions=(),
))
"""


@pytest.mark.parametrize(
    "body, reason",
    [
        # settings_model without configure: core validated a table it then has
        # nowhere to deliver, so the plugin would run unconfigured.
        (_HALF_BOUND_BODY, "plugin_settings_binding_missing"),
        # configure without settings_model: nothing defines the accepted key
        # set, so the fail-closed unknown-key check could not run.
        (
            _HALF_BOUND_BODY.replace(
                "    settings_model: object = SampleSettings",
                "    def configure(self, settings) -> None:\n"
                "        return None",
            ),
            "plugin_settings_binding_missing",
        ),
        # configure present but not callable.
        (
            _HALF_BOUND_BODY.replace(
                "    def register(self, registrar) -> None:",
                "    configure: object = 7\n\n"
                "    def register(self, registrar) -> None:",
            ),
            "plugin_settings_binding_missing",
        ),
        # settings_model is not a pydantic model, so model_fields/model_validate
        # would be whatever the plugin happens to define.
        (
            _HALF_BOUND_BODY.replace(
                "    settings_model: object = SampleSettings",
                "    settings_model: object = dict\n\n"
                "    def configure(self, settings) -> None:\n"
                "        return None",
            ),
            "plugin_settings_model_invalid",
        ),
        (
            _HALF_BOUND_BODY.replace(
                "    settings_model: object = SampleSettings",
                '    settings_model: object = "SampleSettings"\n\n'
                "    def configure(self, settings) -> None:\n"
                "        return None",
            ),
            "plugin_settings_model_invalid",
        ),
    ],
)
def test_malformed_settings_binding_fails_with_stable_reason_codes(
    tmp_path, body, reason
):
    module = _write_plugin_package(tmp_path, body=body)
    config = _write_config(tmp_path, _entry(module))

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == reason
    assert excinfo.value.plugin_id == "corp.sample"
    assert excinfo.value.__cause__ is None


def test_configure_failure_is_sanitized_and_never_echoes_the_value(tmp_path):
    """``configure`` sees the real settings; its exception must not carry them."""

    module = _write_plugin_package(
        tmp_path,
        body=_SETTINGS_BODY.replace(
            '        CALLS.append("configure")',
            "        raise RuntimeError("
            '            f"cannot reach {settings.endpoint} with {settings.token}"\n'
            "        )",
        ),
    )
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra=(
                '[extensions."corp.sample".settings]\n'
                'endpoint = "http://10.0.0.7"\ntoken = "TOPSECRET"\n'
            ),
        ),
    )

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)

    error = excinfo.value
    assert error.reason == "plugin_settings_binding_failed"
    assert error.exception_type == "RuntimeError"
    rendered = str(error) + repr(error)
    assert "TOPSECRET" not in rendered
    assert "10.0.0.7" not in rendered
    # `from None`: without it the plugin's message rides along in the traceback
    # of every log that renders this exception.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_settings_table_without_a_settings_model_fails_closed(tmp_path):
    module = _write_plugin_package(tmp_path)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\nendpoint = "http://x"\n',
        ),
    )
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        discover_deployment_extensions(config)
    assert excinfo.value.reason == "plugin_settings_not_accepted"


def test_validated_settings_reach_the_bundle_before_register(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    module = _write_plugin_package(tmp_path, body=_SETTINGS_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra=(
                '[extensions."corp.sample".settings]\n'
                'endpoint = "http://corp"\nretries = 3\n'
            ),
        ),
    )

    runtime = _runtime(monkeypatch, config)
    plugin = sys.modules[module.stem]
    # A configuration error must always surface before a topology error.
    assert plugin.CALLS == ["configure", "register"]
    assert plugin.BOUND[0].endpoint == "http://corp"
    assert plugin.BOUND[0].retries == 3
    assert runtime.plugin_settings["corp.sample"] is plugin.BOUND[0]
    with pytest.raises(TypeError):
        runtime.plugin_settings["corp.sample"] = None


# --------------------------------------------------------------------------
# Capability merge
# --------------------------------------------------------------------------


_CAPABILITY_BODY = """
from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ExtensionManifest,
    UiContributionDeclaration,
)

STATE = {"available": True}


def probe(_context):
    if STATE["available"]:
        return Availability.available()
    return Availability(
        AvailabilityStatus.UNAVAILABLE, "corp_backend_at_10_0_0_7_is_down"
    )


@dataclass
class Bundle:
    manifest: ExtensionManifest
    capability_decisions: object

    def register(self, registrar) -> None:
        return None


bundle = Bundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Corp capability plugin",
        trust="deployment",
        contributions=(),
        requires=(CAPABILITY,),
        provides=(CAPABILITY,),
        ui_contributions=(UiContributionDeclaration(
            id="corp.sample.panel",
            slot="workspace.side_panel",
            capability=CAPABILITY,
        ),),
    ),
    {CAPABILITY: probe},
)
"""


def _capability_body(capability: str = "corp.sample.available", **replace) -> str:
    body = f"CAPABILITY = {capability!r}\n" + _CAPABILITY_BODY
    for old, new in replace.items():
        body = body.replace(old, new)
    return body


def test_provided_capability_enters_the_catalog_and_unblocks_requires_and_ui(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    module = _write_plugin_package(tmp_path, body=_capability_body())
    config = _write_config(tmp_path, _entry(module))

    # Without the merge the registry cannot freeze at all: `requires` and the
    # UI contribution both name a capability that has no decision entry.
    runtime = _runtime(monkeypatch, config)

    registry = runtime.registry
    assert registry.frozen is True
    assert [manifest.id for manifest in registry.manifests()][-1] == "corp.sample"
    assert (
        registry.capability_availability("corp.sample.available", None).status
        is AvailabilityStatus.AVAILABLE
    )
    from app.extensions.ui_projection import ui_contribution_contract

    assert [
        row["contribution_id"] for row in ui_contribution_contract(registry)
    ] == ["builtin.ask_agent_profile.workspace_panel", "corp.sample.panel"]


def test_provided_capability_is_live_on_system_extensions(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    from fastapi.testclient import TestClient

    module = _write_plugin_package(tmp_path, body=_capability_body())
    config = _write_config(tmp_path, _entry(module))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    _runtime(monkeypatch, config)

    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/auth/register", json={"username": "z00654321", "password": "pw"})
    login = client.post(
        "/api/auth/login", json={"username": "z00654321", "password": "pw"}
    )
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    def _row() -> dict:
        response = client.get("/api/system/extensions", headers=headers)
        assert response.status_code == 200
        assert "corp_backend" not in response.text
        rows = [
            row
            for row in response.json()["extensions"]
            if row["contribution_id"] == "corp.sample.panel"
        ]
        assert len(rows) == 1
        return rows[0]

    assert _row() == {
        "plugin_id": "corp.sample",
        "display_name": "Corp capability plugin",
        "version": "0.1.0",
        "contribution_id": "corp.sample.panel",
        "available": True,
        "unavailable_reason": None,
    }
    # The topology is frozen but each decision stays live.
    sys.modules[module.stem].STATE["available"] = False
    assert _row()["available"] is False
    assert _row()["unavailable_reason"] == "unavailable"


_UNDECLARED_PROBE_BODY = """
from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    Availability,
    ExtensionManifest,
)


@dataclass
class Bundle:
    manifest: ExtensionManifest
    capability_decisions: object

    def register(self, registrar) -> None:
        return None


bundle = Bundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Corp capability plugin",
        trust="deployment",
        contributions=(),
    ),
    {"corp.sample.available": lambda _context: Availability.available()},
)
"""


def test_probe_not_listed_in_provides_fails_startup(tmp_path):
    module = _write_plugin_package(tmp_path, body=_UNDECLARED_PROBE_BODY)
    config = _write_config(tmp_path, _entry(module))
    discovered = discover_deployment_extensions(config)

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        capability_decisions_from_bundles(
            [item.bundle for item in discovered], {}
        )
    assert excinfo.value.reason == "plugin_capability_not_declared"
    assert excinfo.value.names == ("corp.sample.available",)


def test_provides_without_a_probe_fails_startup(tmp_path):
    module = _write_plugin_package(
        tmp_path,
        body=_capability_body().replace("    {CAPABILITY: probe},", "    {},"),
    )
    config = _write_config(tmp_path, _entry(module))
    discovered = discover_deployment_extensions(config)

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        capability_decisions_from_bundles(
            [item.bundle for item in discovered], {}
        )
    assert excinfo.value.reason == "plugin_capability_missing_decision"
    assert excinfo.value.names == ("corp.sample.available",)


def test_capability_name_colliding_with_core_fails_startup(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    module = _write_plugin_package(
        tmp_path, body=_capability_body("ui.agent_profile.available")
    )
    config = _write_config(tmp_path, _entry(module))

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _runtime(monkeypatch, config)
    assert excinfo.value.reason == "plugin_capability_conflicts_core"
    assert excinfo.value.names == ("ui.agent_profile.available",)


def test_capability_name_colliding_with_another_plugin_fails_startup(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    first = _write_plugin_package(
        tmp_path, plugin_id="corp.aaa", body=_capability_body("corp.shared.available")
    )
    second = _write_plugin_package(
        tmp_path, plugin_id="corp.zzz", body=_capability_body("corp.shared.available")
    )
    config = _write_config(
        tmp_path,
        _entry(first, plugin_id="corp.aaa")
        + _entry(second, plugin_id="corp.zzz").replace(
            'id="corp.sample.panel"', 'id="corp.zzz.panel"'
        ),
    )

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _runtime(monkeypatch, config)
    assert excinfo.value.reason == "plugin_capability_conflicts_plugin"
    assert excinfo.value.plugin_id == "corp.zzz"


def test_probe_exception_is_sanitized_not_propagated(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    module = _write_plugin_package(
        tmp_path,
        body=_capability_body().replace(
            '    if STATE["available"]:',
            '    raise RuntimeError("corp_backend_at_10_0_0_7_is_down")\n'
            '    if STATE["available"]:',
        ),
    )
    config = _write_config(tmp_path, _entry(module))
    runtime = _runtime(monkeypatch, config)

    decision = runtime.registry.capability_availability(
        "corp.sample.available", None
    )
    assert decision.status is AvailabilityStatus.UNAVAILABLE
    assert decision.reason_code == "capability_decision_failed"
    assert "10_0_0_7" not in repr(decision)


_MALFORMED_DECISIONS_BODY = """
from collections.abc import Mapping

from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest

_ABSENT = object()


class HostileMapping(Mapping):
    \"\"\"Registered as a Mapping, unusable as one — every read raises.\"\"\"

    def __iter__(self):
        raise RuntimeError("corp_backend_at_10_0_0_7_is_down")

    def __getitem__(self, key):
        raise RuntimeError("corp_backend_at_10_0_0_7_is_down")

    def __len__(self) -> int:
        return 1


class Bundle:
    # A plain class, not a dataclass: several of the declarations below are list
    # or dict literals, which a dataclass rejects outright as mutable defaults —
    # and the point is to reach discovery carrying them, not to be stopped by
    # the test plugin's own class machinery.
    def __init__(self, manifest, capability_decisions):
        self.manifest = manifest
        if capability_decisions is not _ABSENT:
            self.capability_decisions = capability_decisions

    def register(self, registrar) -> None:
        return None


bundle = Bundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Corp capability plugin",
        trust="deployment",
        contributions=(),
        provides=PROVIDES_EXPR,
    ),
    DECISIONS_EXPR,
)
"""


def _decisions_body(*, decisions: str, provides: str) -> str:
    """``decisions="_ABSENT"`` omits the attribute from the bundle entirely."""

    body = _MALFORMED_DECISIONS_BODY.replace("DECISIONS_EXPR", decisions)
    body = body.replace("PROVIDES_EXPR", provides)
    assert "_EXPR" not in body
    return body


@pytest.mark.parametrize(
    "decisions, provides, reason",
    [
        # `provides` declared, `capability_decisions` attribute absent.
        (
            "_ABSENT",
            '("corp.sample.available",)',
            "plugin_capability_declaration_invalid",
        ),
        # The attribute is present but explicitly None, which is the same hole.
        (
            "None",
            '("corp.sample.available",)',
            "plugin_capability_declaration_invalid",
        ),
        # `capability_decisions` is not a Mapping at all.
        (
            '[("corp.sample.available", lambda _c: None)]',
            '("corp.sample.available",)',
            "plugin_capability_declaration_invalid",
        ),
        # A Mapping whose iteration raises: `isinstance(..., Mapping)` proves the
        # shape, not that reading it is safe, and the message is plugin-written.
        (
            "HostileMapping()",
            '("corp.sample.available",)',
            "plugin_capability_declaration_invalid",
        ),
        # Declared and supplied, but the "probe" is not callable.
        (
            '{"corp.sample.available": 7}',
            '("corp.sample.available",)',
            "plugin_capability_declaration_invalid",
        ),
        # ":" is reserved for core's own "point:name" capabilities.
        (
            '{"corp.sample:available": lambda _c: None}',
            '("corp.sample:available",)',
            "plugin_capability_name_invalid",
        ),
        (
            '{"Corp Sample": lambda _c: None}',
            '("Corp Sample",)',
            "plugin_capability_name_invalid",
        ),
        # Invalid name on the `provides` side alone is caught too — both sides
        # go through the same check before any set arithmetic.
        (
            '{"corp.sample.available": lambda _c: None}',
            '("corp.sample.available", "Corp Sample")',
            "plugin_capability_name_invalid",
        ),
    ],
)
def test_malformed_capability_declarations_fail_with_stable_reason_codes(
    tmp_path, decisions, provides, reason
):
    module = _write_plugin_package(
        tmp_path,
        body=_decisions_body(decisions=decisions, provides=provides),
    )
    config = _write_config(tmp_path, _entry(module))
    discovered = discover_deployment_extensions(config)

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        capability_decisions_from_bundles(
            [item.bundle for item in discovered], {}
        )

    error = excinfo.value
    assert error.reason == reason
    assert error.plugin_id == "corp.sample"
    assert error.__cause__ is None
    # A hostile Mapping's exception text is plugin-authored; only its class name
    # may cross the boundary.
    assert "10_0_0_7" not in str(error) + repr(error)


def _builtin_bundles() -> tuple[object, ...]:
    """The exact ten bundles the composition root ships, by their own names."""

    from app.extensions import builtin

    bundles = (
        builtin.ASK_AGENT_PROFILE_COMPLETED_BUNDLE,
        builtin.ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE,
        builtin.ASK_SEARCH_PROFILE_COMPLETED_BUNDLE,
        builtin.REPORT_AGENT_PROFILE_COMPLETED_BUNDLE,
        builtin.REPORT_MARKDOWN_EXPORTER_BUNDLE,
        builtin.PARSER_BUILTIN_BUNDLE,
        builtin.GENERATED_QUESTION_BUNDLE,
        builtin.PARSER_CLOUD_BUNDLE,
        builtin.SELECTED_SOURCE_GRAPH_BUNDLE,
        builtin.PARSER_SELF_HOSTED_BUNDLE,
    )
    assert {bundle.manifest.id for bundle in bundles} == set(_BUILTIN_PLUGIN_IDS)
    return bundles


def test_capability_merge_returns_core_entries_unchanged_for_builtin_bundles():
    def core_probe(_context):
        return Availability.available()

    core = {"core.one": core_probe}
    # The real shipped bundles, not an empty list: an empty list would pass even
    # if the merge mangled every bundle it was handed.
    merged = capability_decisions_from_bundles(_builtin_bundles(), core)
    assert merged == core
    assert merged is not core


def test_builtin_bundles_are_checked_by_the_capability_merge_too(
    monkeypatch, frozen_runtime_reset
):
    """The merge's invariants hold for the whole topology, not the plugin half.

    ``default_extension_runtime`` hands it built-in *and* discovered bundles;
    handing it only the discovered ones would be invisible today (no shipped
    bundle declares ``provides``) and would silently stop checking the built-in
    half the moment one does.  Substituting a malformed built-in bundle is what
    makes that difference observable.
    """

    from dataclasses import dataclass

    from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest

    @dataclass
    class Bundle:
        manifest: ExtensionManifest

        def register(self, registrar) -> None:
            return None

    broken = Bundle(
        ExtensionManifest(
            id="builtin.report_markdown_exporter",
            version="0.1.0",
            api_version=EXTENSION_API_VERSION,
            display_name="Broken built-in",
            trust="builtin",
            contributions=(),
            provides=("builtin.synthetic.available",),
        )
    )
    # An empty mapping rather than a missing attribute, so the rejection is the
    # specific one — a declared capability with no probe behind it.
    broken.capability_decisions = {}
    monkeypatch.setattr(
        "app.extensions.bootstrap.REPORT_MARKDOWN_EXPORTER_BUNDLE", broken
    )

    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _runtime(monkeypatch, "")
    assert excinfo.value.reason == "plugin_capability_missing_decision"
    assert excinfo.value.plugin_id == "builtin.report_markdown_exporter"
    assert excinfo.value.names == ("builtin.synthetic.available",)


# --------------------------------------------------------------------------
# Startup failure surface
# --------------------------------------------------------------------------


def test_discovery_failure_log_carries_only_id_reason_and_exception_type(
    monkeypatch, tmp_path, caplog, frozen_runtime_reset
):
    # A rejection that actually *has* an exception class: with the unknown-key
    # shape `exception_type` is "" and `assert "exc=" in message` would hold
    # against the bare format string, proving nothing.
    module = _write_plugin_package(tmp_path, body=_SETTINGS_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\nretries = "TOPSECRET"\n',
        ),
    )

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.extensions"):
        with pytest.raises(ExtensionDiscoveryError):
            _runtime(monkeypatch, config)

    records = [
        record
        for record in caplog.records
        if record.name == "silicon_notebook.extensions"
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "plugin=corp.sample" in message
    assert "reason=plugin_settings_invalid" in message
    assert "exc=ValidationError" in message
    # No field name, no rendered message, no module path, no file path, no value.
    for forbidden in (
        "retries",
        "TOPSECRET",
        module.stem,
        str(config),
        "keys=",
    ):
        assert forbidden not in caplog.text, forbidden


def test_discovery_failure_log_never_carries_offending_key_names(
    monkeypatch, tmp_path, caplog, frozen_runtime_reset
):
    """The exception carries key names for the operator; the log must not.

    ``plugin_settings_unknown_key`` is the one reason code whose ``names`` are
    populated, so it is the only shape that can regress this.
    """

    module = _write_plugin_package(tmp_path, body=_SETTINGS_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\napi_key = "TOPSECRET"\n',
        ),
    )

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.extensions"):
        with pytest.raises(ExtensionDiscoveryError) as excinfo:
            _runtime(monkeypatch, config)

    assert excinfo.value.names == ("api_key",)
    assert "reason=plugin_settings_unknown_key" in caplog.text
    for forbidden in ("api_key", "TOPSECRET", "keys="):
        assert forbidden not in caplog.text, forbidden


def test_create_app_refuses_to_start_on_discovery_failure(
    monkeypatch, tmp_path, frozen_runtime_reset
):
    from app.core import readiness

    config = _write_config(tmp_path, "[plugins]\nfoo = 1\n")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("EXTENSIONS_CONFIG", config)

    from app.core.config import get_settings
    from app.extensions.bootstrap import default_extension_runtime

    get_settings.cache_clear()
    default_extension_runtime.cache_clear()

    before = readiness.snapshot()
    readiness.reset()
    try:
        with pytest.raises(ExtensionDiscoveryError):
            # `app.main` builds the application at module scope, so on a cold
            # import the import statement is itself the startup attempt.
            from app.main import create_app

            create_app()
        # `readiness.is_ready()` was just reset to False, so asserting it here
        # would hold whatever happened.  What actually shows there is no way
        # around the failure is that the *other* composition entry point — the
        # one every request goes through — refuses identically: on a warm
        # process where `app.main` was already imported, this is the call that
        # stops the service, and it stops it on every attempt rather than
        # degrading to a plugin-less topology.
        from app.api import deps

        with pytest.raises(ExtensionDiscoveryError):
            deps.repository()
        with pytest.raises(ExtensionDiscoveryError):
            deps.repository()
    finally:
        readiness._state.update(before)
