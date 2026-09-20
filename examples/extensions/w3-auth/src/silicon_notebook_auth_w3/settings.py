"""Deployment settings contain environment-variable names, never secrets."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class W3AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_origin_env: str = "W3_LOGIN_ORIGIN"
    client_id_env: str = "W3_CLIENT_ID"
    client_secret_env: str = "W3_CLIENT_SECRET"
    ca_bundle_env: str = "W3_CA_BUNDLE"
    provider_id: str = "w3"
    provider_namespace: str
    configuration_generation: str
    public_label: str = "统一登录"
    pkce_supported: bool = False
    userinfo_auth_mode: Literal["query", "bearer"] = "query"
    subject_field: str = "uid"
    timeout_seconds: float = Field(10.0, gt=0, le=120)

    @field_validator(
        "login_origin_env", "client_id_env", "client_secret_env", "ca_bundle_env"
    )
    @classmethod
    def _validate_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an environment variable name")
        return value

    @field_validator("provider_id", "configuration_generation")
    @classmethod
    def _validate_stable_id(cls, value: str) -> str:
        if not _STABLE_ID.fullmatch(value):
            raise ValueError("must be a stable lowercase identifier")
        return value

    @field_validator("provider_namespace")
    @classmethod
    def _validate_namespace(cls, value: str) -> str:
        if not _NAMESPACE.fullmatch(value):
            raise ValueError("must be a stable identity-source namespace")
        return value

    @field_validator("public_label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("must be non-blank display text without controls")
        return value

    @field_validator("subject_field")
    @classmethod
    def _validate_subject_field(cls, value: str) -> str:
        if not _FIELD_NAME.fullmatch(value):
            raise ValueError("must name one top-level userinfo field")
        return value


__all__ = ["W3AuthSettings"]
