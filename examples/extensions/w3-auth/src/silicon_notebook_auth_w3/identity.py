"""Strict projection from W3 userinfo JSON to the provider-neutral identity."""
from __future__ import annotations

from app.extension_sdk import ExternalIdentity


def external_identity(
    payload: object,
    *,
    provider_namespace: str,
    subject_field: str,
) -> ExternalIdentity:
    if type(payload) is not dict:
        raise ValueError("userinfo_not_object")
    uid = _identity_string(payload.get("uid"))
    if uid is None:
        raise ValueError("userinfo_uid_invalid")
    subject = _identity_string(payload.get(subject_field))
    if subject is None:
        raise ValueError("userinfo_subject_invalid")
    display_name = (
        _display_string(payload.get("displayNameCn"))
        or _display_string(payload.get("displayName"))
        or uid
    )
    return ExternalIdentity(
        provider_namespace=provider_namespace,
        subject=subject,
        username=uid,
        display_name=display_name,
    )


def _identity_string(value: object) -> str | None:
    if type(value) is not str or not value or value != value.strip():
        return None
    return value


def _display_string(value: object) -> str | None:
    if type(value) is not str or not value.strip():
        return None
    return value


__all__ = ["external_identity"]
