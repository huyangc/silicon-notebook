"""Differential and fallback boundaries for scalar Settings classification."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated

import pydantic
import pydantic_settings
import pytest
from annotated_types import Ge
from pydantic import BaseModel, Json, RootModel, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, ForceDecode, NoDecode
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
from pydantic_settings.sources.providers.env import EnvSettingsSource

from app.core.config import Settings
from tests.speedups import settings_sources as shim


@pytest.fixture
def installed_shim():
    was_installed = shim.installed()
    assert shim.install(force=True)
    try:
        yield
    finally:
        shim.uninstall()
        if was_installed:
            assert shim.install(force=True)


def _source(source_class=EnvSettingsSource, settings_class=Settings):
    kwargs = {"env_file": None} if source_class is DotEnvSettingsSource else {}
    return source_class(settings_class, **kwargs)


def test_verified_versions_match_the_runtime_lock(installed_shim):
    lock = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert f"pydantic=={shim.VERIFIED_PYDANTIC_VERSION} " in lock
    assert f"pydantic-settings=={shim.VERIFIED_SETTINGS_VERSION} " in lock
    assert pydantic.__version__ == shim.VERIFIED_PYDANTIC_VERSION
    assert pydantic_settings.__version__ == shim.VERIFIED_SETTINGS_VERSION


def test_every_current_field_matches_real_classification_in_both_sources(installed_shim):
    for source_class in (EnvSettingsSource, DotEnvSettingsSource):
        source = _source(source_class)
        for name, field in Settings.model_fields.items():
            with shim.disabled():
                expected = source._field_is_complex(field)
            assert source._field_is_complex(field) == expected, (source_class, name)


def test_current_scalar_annotation_and_metadata_are_observed(installed_shim):
    source = _source()
    field = copy.copy(Settings.model_fields["notebook_metadata_batch_chars"])
    for annotation in (str, int, float, bool):
        field.annotation = annotation
        for metadata in ([], [Json()], [NoDecode], [ForceDecode], [Ge(1)]):
            field.metadata = metadata
            with shim.disabled():
                expected = source._field_is_complex(field)
            assert source._field_is_complex(field) == expected == (False, False)
    # The same FieldInfo subsequently becoming complex must stop qualifying.
    field.annotation = list[str]
    field.metadata = []
    assert source._field_is_complex(field) == (True, False)
    field.metadata.append(Json())
    assert source._field_is_complex(field) == (False, False)


def test_non_scalar_annotations_keep_real_semantics(installed_shim):
    class Nested(BaseModel):
        value: int

    source = _source()
    for annotation in (
        list[str], dict[str, int], str | None, Annotated[str, Json],
        Nested, RootModel[list[str]],
    ):
        field = FieldInfo.from_annotation(annotation)
        with shim.disabled():
            expected = source._field_is_complex(field)
        assert source._field_is_complex(field) == expected, annotation


def test_foreign_models_subclasses_and_custom_sources_fall_through(installed_shim, monkeypatch):
    class Other(BaseSettings):
        value: int = 1

    class Child(Settings):
        pass

    class CustomSource(EnvSettingsSource):
        pass

    calls = []
    monkeypatch.setattr(shim, "_real_field_is_complex", lambda source, field: calls.append(source) or (True, True))
    field = FieldInfo.from_annotation(int)
    for source in (_source(settings_class=Other), _source(settings_class=Child), _source(CustomSource)):
        assert source._field_is_complex(field) == (True, True)
    assert len(calls) == 3


def test_a_changed_public_classifier_is_not_bypassed(installed_shim, monkeypatch):
    source = _source()
    calls = []
    monkeypatch.setattr(source, "field_is_complex", lambda field: calls.append(field) or True)
    field = FieldInfo.from_annotation(int)
    assert source._field_is_complex(field) == (True, False)
    assert calls == [field]


def test_unknown_or_custom_metadata_is_read_by_the_real_classifier(installed_shim, monkeypatch):
    field = FieldInfo.from_annotation(int)
    calls = []
    monkeypatch.setattr(shim, "_real_field_is_complex", lambda source, field: calls.append(field) or (True, True))
    source = _source()
    field.metadata.append(object())
    assert source._field_is_complex(field) == (True, True)
    field.metadata.clear()
    assert source._field_is_complex(field) == (False, False)

    class CustomList(list):
        pass

    field.metadata = CustomList()
    assert source._field_is_complex(field) == (True, True)
    assert len(calls) == 2


def test_custom_metadata_errors_are_preserved(installed_shim):
    class HostileMetadata:
        @property
        def __class__(self):
            raise RuntimeError("metadata probe")

    field = FieldInfo.from_annotation(int)
    field.metadata.append(HostileMetadata())
    source = _source()
    for context in (shim.disabled, nullcontext):
        with context(), pytest.raises(RuntimeError, match="metadata probe"):
            source._field_is_complex(field)


def test_annotation_eligibility_uses_identity_without_hash_or_equality(installed_shim, monkeypatch):
    class Hostile:
        def __eq__(self, other):
            raise AssertionError("annotation equality was evaluated")

        def __hash__(self):
            raise AssertionError("annotation hash was evaluated")

    field = FieldInfo.from_annotation(int)
    field.annotation = Hostile()
    calls = []
    monkeypatch.setattr(shim, "_real_field_is_complex", lambda source, field: calls.append(field) or (True, True))
    assert _source()._field_is_complex(field) == (True, True)
    assert calls == [field]


def test_environment_aliases_validation_and_mutable_outputs_stay_fresh(installed_shim, monkeypatch):
    observations = []
    for context in (shim.disabled, nullcontext):
        with context():
            monkeypatch.setenv("NOTEBOOK_METADATA_BATCH_CHARS", "16000")
            monkeypatch.setenv("CORS_ORIGINS", "https://one.example,https://two.example")
            first = Settings(_env_file=None)
            monkeypatch.setenv("NOTEBOOK_METADATA_BATCH_CHARS", "20000")
            second = Settings(_env_file=None)
            first.cors_origins.append("https://private.example")
            assert "https://private.example" not in second.cors_origins
            assert first.cors_origins is not second.cors_origins
            explicit = Settings(_env_file=None, NOTEBOOK_METADATA_BATCH_CHARS=24000)
            monkeypatch.setenv("NOTEBOOK_METADATA_BATCH_CHARS", "1")
            with pytest.raises(ValidationError) as invalid:
                Settings(_env_file=None)
            observations.append((
                first.notebook_metadata_batch_chars, second.notebook_metadata_batch_chars,
                explicit.notebook_metadata_batch_chars, second.cors_origins,
                [(error["loc"], error["type"]) for error in invalid.value.errors()],
            ))
    assert observations[0] == observations[1]
    assert observations[0][:3] == (16000, 20000, 24000)


def test_dotenv_is_reread_and_named_inputs_still_validate(installed_shim, tmp_path, monkeypatch):
    monkeypatch.delenv("NOTEBOOK_METADATA_BATCH_CHARS", raising=False)
    path = tmp_path / "settings.env"
    observations = []
    for context in (shim.disabled, nullcontext):
        with context():
            path.write_text("NOTEBOOK_METADATA_BATCH_CHARS=16000\n")
            first = Settings(_env_file=path)
            path.write_text("NOTEBOOK_METADATA_BATCH_CHARS=20000\n")
            second = Settings(_env_file=path)
            named = Settings(_env_file=None, notebook_metadata_batch_chars=24000)
            with pytest.raises(ValidationError):
                Settings(_env_file=None, notebook_metadata_batch_chars="invalid")
            observations.append((first.notebook_metadata_batch_chars, second.notebook_metadata_batch_chars, named.notebook_metadata_batch_chars))
    assert observations[0] == observations[1] == (16000, 20000, 24000)


def test_disabled_contexts_nest_and_restore_after_an_error(installed_shim, monkeypatch):
    calls = []
    monkeypatch.setattr(shim, "_real_field_is_complex", lambda source, field: calls.append(field) or (True, True))
    source = _source()
    field = FieldInfo.from_annotation(int)
    with pytest.raises(RuntimeError), shim.disabled():
        with shim.disabled():
            assert source._field_is_complex(field) == (True, True)
        assert source._field_is_complex(field) == (True, True)
        raise RuntimeError("probe")
    assert source._field_is_complex(field) == (False, False)
    assert len(calls) == 2


def test_off_switch_and_unsupported_versions_leave_real_implementation(installed_shim, monkeypatch):
    shim.uninstall()
    with monkeypatch.context() as patch:
        patch.setenv(shim.ENV_SWITCH, "0")
        assert not shim.install()
        assert not shim.installed()
        assert shim.install(force=True)
    shim.uninstall()
    for module in (pydantic, pydantic_settings):
        with monkeypatch.context() as patch:
            patch.setattr(module, "__version__", "unverified")
            assert not shim.install(force=True)
            assert not shim.installed()
    assert shim.install(force=True)


def test_install_is_idempotent_and_preserves_a_replacement_method(installed_shim, monkeypatch):
    assert shim.install(force=True)
    shim.uninstall()
    with monkeypatch.context() as patch:
        replacement = lambda source, field: (True, True)
        patch.setattr(EnvSettingsSource, "_field_is_complex", replacement)
        assert not shim.install(force=True)
        shim.uninstall()
        assert EnvSettingsSource._field_is_complex is replacement
    assert shim.install(force=True)
