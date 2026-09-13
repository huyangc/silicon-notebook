"""Deployment-configurable settings for the circuit-diagram sample plugin.

Core computes the accepted key set from ``model_fields`` itself, so
``extra="forbid"`` here is a second lock on the same door rather than the door.
It is kept because this model is also usable standalone: a plugin author can
validate a candidate TOML table without the backend present.

Two shapes are worth pointing at, because they are what an in-house variant
copies:

* **``api_key_env`` is the name of an environment variable, never a key.**  A
  credential that sat in the deployment TOML would be read back by
  ``/admin/extensions``, printed by a config dump, and committed by whoever
  copied the file.  The plugin reads ``os.environ`` at the moment it dials and
  never stores the value, so the key exists in exactly one place the operator
  already manages.
* **``configure`` may only store values.**  It runs inside startup
  composition, before the registry freezes and before the service is ready, so
  no client object, thread or connection is built from these settings here —
  see the deployment-extensions SOP §3.2.

:func:`classify_kwargs` at the bottom is the one place these settings are
mapped onto the transport's keyword arguments, for the same reason the arXiv
sample has exactly one such function: :mod:`.client` deliberately knows nothing
about this model, so something above it has to name the transport's
parameters, and two call sites spelling that mapping by hand is how a plugin
ends up sending one deployment's model from one path and the default from
another.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

# POSIX-portable environment variable name.  Validated rather than trusted
# because a typo here is indistinguishable at runtime from "the operator did
# not export the key": both make ``os.environ.get`` answer ``None``, and the
# plugin then reports itself unavailable forever with nothing to point at.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The model id travels inside a JSON request body and is persisted into element
# metadata, so it is held to the same "no control characters" rail a header
# value would be.
_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


class CircuitDiagramSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = Field(30.0, gt=0, le=120)
    max_images_per_source: int = Field(8, ge=1, le=64)
    # 4 MiB.  The floor is a sanity bound rather than a protocol one; the
    # ceiling is DeepSeek's own documented per-image limit (32 MiB), so a
    # deployment cannot configure this plugin into sending an image the
    # endpoint is guaranteed to refuse.
    max_image_bytes: int = Field(4 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    prompt_language: Literal["zh", "en"] = "zh"

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        """Reject anything that is not a plain ``http(s)://host[/path]`` URL.

        ``base_url`` is deployment configuration, not user input, but it still
        crosses a trust boundary: :mod:`.client` appends ``/chat/completions``
        to it and hands the result to :func:`urllib.request.urlopen`.  This
        mirrors core's fail-fast validation of ``MCP_PUBLIC_URL`` rather than
        trusting a TOML author not to paste a ``file://`` path or a stray
        ``#fragment``.  A query string is refused for a sharper reason: the
        endpoint path is appended, so a query already present would end up in
        the middle of the URL rather than at its end.

        The trailing slash is stripped here, once, so every consumer can join
        with a single ``/`` and no call site has to guess whether the
        configured value ended with one.
        """

        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("base_url must be an absolute http:// or https:// URL")
        if not parsed.netloc:
            raise ValueError("base_url must include a host")
        if parsed.query:
            raise ValueError("base_url must not include a query string")
        if parsed.fragment:
            raise ValueError("base_url must not include a fragment")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        if any(char in _CONTROL_CHARS for char in value):
            raise ValueError("model must not contain control characters")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _validate_api_key_env(cls, value: str) -> str:
        """An environment variable *name*, and a fail-fast one.

        Refusing a malformed name at startup is the difference between an
        operator seeing their typo and an operator seeing a plugin that is
        silently disabled in every deployment, forever.
        """

        if not _ENV_NAME.fullmatch(value):
            raise ValueError(
                "api_key_env must be an environment variable name, not a key"
            )
        return value


def chat_completions_url(settings: CircuitDiagramSettings) -> str:
    """The one endpoint this plugin calls.

    A function rather than a settings field: the endpoint path is part of the
    OpenAI-compatible protocol :mod:`.client` speaks, not something a
    deployment chooses, so it belongs beside the mapping below rather than in
    the operator's TOML.
    """

    return f"{settings.base_url}/chat/completions"


def classify_kwargs(
    settings: CircuitDiagramSettings, *, api_key: str
) -> dict[str, object]:
    """Map deployment settings onto :func:`.client.classify`'s arguments.

    ``api_key`` is an argument rather than a setting because it is read from
    the environment at dial time and never stored — see this module's
    docstring.  ``post`` is deliberately absent: it is a test seam on the
    transport, not a deployment setting, so production callers never pass one.
    """

    return {
        "url": chat_completions_url(settings),
        "model": settings.model,
        "api_key": api_key,
        "timeout_seconds": settings.timeout_seconds,
        "language": settings.prompt_language,
    }
