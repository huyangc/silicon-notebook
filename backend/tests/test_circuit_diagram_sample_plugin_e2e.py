"""End-to-end: the circuit-diagram sample plugin as a deployment would load it.

Everything here goes through the real machinery — a real TOML file, real
discovery, a real ``create_app()``, a real ``TestClient``, a real upload and a
real parse — because that is the part the unit tests cannot cover.
``test_circuit_diagram_sample_plugin.py`` exercises what the plugin *decides*
against hand-built seams; this file exercises what core does with it once the
config file names it: the host runs it, the service applies its patches, and
the enrichment reaches both the element a reader opens and the chunk a
retrieval reads.

**Zero network, and that is asserted rather than assumed.**  The only seam
faked is ``client._post`` — the injectable transport the package ships for
exactly this purpose — and ``socket.getaddrinfo`` is made to raise for every
test in the file, so a future edit that reintroduced a real dial fails loudly
instead of quietly reaching an endpoint from the test suite.  ``base_url``
also points at a ``.example`` host, which is reserved and never resolves.

**The upload is caption-free on purpose.**  An image element with neither a
caption nor a description is excluded from chunking, so "this element has a
chunk afterwards" is evidence the enrichment actually reached the retrieval
corpus rather than merely being persisted beside it.
"""
from __future__ import annotations

import base64
import importlib
import json
import socket
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_extension_discovery import (
    _plugin_import_isolation,  # noqa: F401 -- autouse pytest fixture, resolved by name
    frozen_runtime_reset,  # noqa: F401 -- pytest fixture, resolved by name
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "examples" / "extensions" / "circuit-diagram"
_PLUGIN_SRC = _PLUGIN_ROOT / "src"

_PACKAGE = "silicon_notebook_circuit_diagram"
_PLUGIN_ID = "examples.circuit_diagram"
_CONTRIBUTION_ID = f"{_PLUGIN_ID}.enricher"
_KEY_ENV = "DEEPSEEK_API_KEY"

# `.example` is reserved by RFC 2606 and never resolves, so a request that
# escaped the seam fails rather than reaching a real endpoint.
_BASE_URL = "https://api.deepseek.example"

_NETLIST = "R1 in out 10k\nR2 out 0 10k"
_FUNCTION = "一个由 R1/R2 构成的分压网络"

# A real 1x1 PNG: the markdown data-URI path validates the bytes before it
# persists an asset row, so a placeholder would never reach the enricher.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_MARKDOWN = f"# Board\n\n![](data:image/png;base64,{_PNG_B64})\n"

if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sample_package_isolation():
    """Restore the sample package's modules exactly as they were found.

    ``_plugin_import_isolation`` (imported above, autouse) restores ``sys.path``
    and drops the discovery tests' throwaway modules — it knows nothing about
    this package's name.  Discovery imports this package for real, and the unit
    test file holds module references captured at import time, so a module
    object swapped underneath it would make those tests configure one object
    and assert against another.
    """

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}.")
    }
    yield
    for name in [
        name
        for name in sys.modules
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}.")
    ]:
        sys.modules.pop(name, None)
    sys.modules.update(saved)
    importlib.invalidate_caches()


@pytest.fixture(autouse=True)
def _no_name_resolution(monkeypatch):
    """Nothing in this file may resolve a hostname.

    ``socket.getaddrinfo`` is the narrowest chokepoint every real outbound HTTP
    call has to pass: ``TestClient`` speaks ASGI in-process and SQLite is a
    file, so nothing legitimate in this chain resolves a name.  Making it raise
    turns "we believe the seam covers it" into "the suite cannot reach the
    network even if the seam is removed".
    """

    def _refuse(*args, **kwargs):
        raise AssertionError("the test suite must not resolve a hostname")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


class _PostSpy:
    """Stand in for ``client._post`` and record every call it receives."""

    def __init__(self, *, is_circuit: bool = True) -> None:
        self.payload = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_circuit": is_circuit,
                                    "netlist": _NETLIST,
                                    "function": _FUNCTION,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        self.calls: list[tuple[str, bytes, dict, float]] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float) -> bytes:
        self.calls.append((url, body, dict(headers), timeout))
        return self.payload


def _clear_caches() -> None:
    from app.api import deps
    from app.core.config import get_settings
    from app.extensions.bootstrap import default_extension_runtime

    get_settings.cache_clear()
    default_extension_runtime.cache_clear()
    deps.repository.cache_clear()


def _config_text() -> str:
    return textwrap.dedent(
        f"""
        [extensions."{_PLUGIN_ID}"]
        bundle = "{_PACKAGE}.bundle:BUNDLE"
        enabled = true

        [extensions."{_PLUGIN_ID}".settings]
        base_url = "{_BASE_URL}"
        model = "deepseek-flash"
        api_key_env = "{_KEY_ENV}"
        timeout_seconds = 5.0
        max_images_per_source = 4
        """
    ).lstrip()


def _configure(tmp_path, monkeypatch, *, api_key: str | None = "test-key") -> None:
    """Write a real TOML, point ``EXTENSIONS_CONFIG`` at it, clear the caches."""

    config = tmp_path / "extensions.toml"
    config.write_text(_config_text(), encoding="utf-8")
    monkeypatch.setenv("EXTENSIONS_CONFIG", str(config))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    if api_key is None:
        monkeypatch.delenv(_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(_KEY_ENV, api_key)
    _clear_caches()


def _client(tmp_path, monkeypatch, **kwargs) -> TestClient:
    _configure(tmp_path, monkeypatch, **kwargs)
    from app.main import create_app

    return TestClient(create_app())


def _auth(client: TestClient, username: str) -> dict[str, str]:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _notebook(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/notebooks", json={"name": "n"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _install_post_spy(monkeypatch, **kwargs) -> _PostSpy:
    """Replace the loaded package's transport, whichever copy that is.

    Resolved through ``sys.modules`` rather than a module-level import: real
    discovery is what imported this package, so patching an import this file
    happened to bind first could leave the copy actually serving the parse
    dialling for real.
    """

    spy = _PostSpy(**kwargs)
    monkeypatch.setattr(sys.modules[f"{_PACKAGE}.client"], "_post", spy)
    return spy


def _upload_and_parse(client: TestClient, headers: dict, notebook_id: str) -> str:
    """Upload the caption-free schematic and run its parse synchronously.

    The background scheduler is stubbed out and ``POST /sources/{id}/parse`` is
    called instead: manual reparse runs the *same* pipeline on the calling
    thread, so the assertions below read a finished source rather than polling
    a thread the test does not own.
    """

    from app.api import source_routes

    original = source_routes.kg_scheduler.submit_job
    source_routes.kg_scheduler.submit_job = lambda fn, *a, **k: None
    try:
        response = client.post(
            f"/api/notebooks/{notebook_id}/sources",
            files=[("files", ("board.md", _MARKDOWN.encode("utf-8"), "text/markdown"))],
            headers=headers,
        )
    finally:
        source_routes.kg_scheduler.submit_job = original
    assert response.status_code == 200, response.text
    source_id = response.json()[0]["id"]

    parsed = client.post(f"/api/sources/{source_id}/parse", headers=headers)
    assert parsed.status_code == 200, parsed.text
    assert parsed.json()["parse_status"] == "extracted", parsed.text
    return source_id


def _image_element(client: TestClient, headers: dict, source_id: str) -> dict:
    response = client.get(f"/api/sources/{source_id}/elements", headers=headers)
    assert response.status_code == 200, response.text
    images = [row for row in response.json() if row["element_type"] == "image"]
    assert len(images) == 1, response.text
    return images[0]


def _chunk_element_ids(source_id: str) -> set[str]:
    """Element ids the chunk rows of this source cover.

    Read through the repository's own connection rather than a second sqlite
    handle opened on a path this test guessed: the facade is where the
    database URL actually resolves.
    """

    from app.api import deps

    with deps.repository()._connect() as db:
        rows = db.execute(
            "SELECT element_ids FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall()
    return {
        element_id
        for row in rows
        for element_id in json.loads(row["element_ids"] or "[]")
    }


# --------------------------------------------------------------------------
# The enrichment chain, end to end
# --------------------------------------------------------------------------


def test_a_parsed_schematic_carries_the_plugins_netlist_into_the_element(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _client(tmp_path, monkeypatch)
    _install_post_spy(monkeypatch)
    headers = _auth(client, "c00110011")
    notebook_id = _notebook(client, headers)

    source_id = _upload_and_parse(client, headers, notebook_id)
    image = _image_element(client, headers, source_id)

    # 1. The structured half, under the contribution's own name.
    owner = image["metadata"]["extensions"][_CONTRIBUTION_ID]
    assert owner["plugin_id"] == _PLUGIN_ID
    assert owner["plugin_version"] == "0.1.0"
    assert owner["metadata"]["netlist"] == _NETLIST
    assert owner["metadata"]["is_circuit"] is True
    assert owner["metadata"]["model"] == "deepseek-flash"

    # 2. The human-readable half, with the netlist inside a fence so the front
    #    end renders it as a code block rather than reflowing it.
    description = image["metadata"]["description"]
    assert "```spice" in description
    assert _NETLIST in description
    assert description.startswith(f"电路功能：{_FUNCTION}")

    # 3. The retrievable half: the description, whitespace-flattened, appended
    #    to the element's own text.
    assert _FUNCTION in image["text"]

    # 4. And it reached the retrieval corpus.  This image had neither caption
    #    nor description before enrichment, so it had no chunk of its own.
    assert image["id"] in _chunk_element_ids(source_id)


def test_exactly_one_request_went_out_carrying_the_configured_deployment(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The settings binding is real, not defaulted, and the image is inline."""

    client = _client(tmp_path, monkeypatch)
    spy = _install_post_spy(monkeypatch)
    headers = _auth(client, "c00220022")
    notebook_id = _notebook(client, headers)

    _upload_and_parse(client, headers, notebook_id)

    assert len(spy.calls) == 1
    url, body, request_headers, timeout = spy.calls[0]
    assert url == f"{_BASE_URL}/chat/completions"
    assert request_headers["Authorization"] == "Bearer test-key"
    assert timeout == 5.0
    parts = json.loads(body.decode("utf-8"))["messages"][0]["content"]
    assert parts[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(
            base64.b64decode(_PNG_B64)
        ).decode("ascii")
    )


def test_an_image_the_model_rejects_leaves_the_element_untouched(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """``is_circuit: false`` writes nothing — not even a "checked" marker."""

    client = _client(tmp_path, monkeypatch)
    spy = _install_post_spy(monkeypatch, is_circuit=False)
    headers = _auth(client, "c00330033")
    notebook_id = _notebook(client, headers)

    source_id = _upload_and_parse(client, headers, notebook_id)
    image = _image_element(client, headers, source_id)

    assert len(spy.calls) == 1
    assert "extensions" not in image["metadata"]
    assert not image["metadata"].get("description")
    # Still caption-free and description-free, so still outside the corpus.
    assert image["id"] not in _chunk_element_ids(source_id)


# --------------------------------------------------------------------------
# Installed but off
# --------------------------------------------------------------------------


def test_without_the_api_key_the_plugin_is_silent_and_the_parse_still_succeeds(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Step two of the two-step enable: the credential, not the TOML entry.

    The plugin is loaded — its contribution is in the frozen topology — and it
    contributes nothing, because its own availability probe refuses.  A
    deployment that names it but forgets the variable gets its sources ingested
    exactly as before, not a failed parse.
    """

    client = _client(tmp_path, monkeypatch, api_key=None)
    spy = _install_post_spy(monkeypatch)
    headers = _auth(client, "c00440044")
    notebook_id = _notebook(client, headers)

    source_id = _upload_and_parse(client, headers, notebook_id)
    image = _image_element(client, headers, source_id)

    assert spy.calls == []
    assert "extensions" not in image["metadata"]

    # Loaded, not absent: the same runtime still carries this plugin's
    # contribution and its settings binding.
    from app.extensions.bootstrap import default_extension_runtime

    runtime = default_extension_runtime()
    assert _PLUGIN_ID in runtime.plugin_settings
    assert runtime.element_enrichers.has_contributions() is True


def test_the_frozen_topology_registers_the_contribution_at_its_own_point(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Real discovery, real freeze — the id a deployment's TOML produced."""

    from app.extension_sdk import SOURCE_ELEMENT_ENRICHER_POINT

    _configure(tmp_path, monkeypatch)
    from app.extensions.bootstrap import default_extension_runtime

    runtime = default_extension_runtime()
    registered = runtime.registry.contributions(SOURCE_ELEMENT_ENRICHER_POINT)

    assert [item.contribution.declaration.id for item in registered] == [
        _CONTRIBUTION_ID
    ]
    assert [item.plugin_id for item in registered] == [_PLUGIN_ID]
