"""Public, supplier-independent authentication messages."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.auth_provider import (
    AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS,
    is_auth_provider_stable_id,
)


class BindingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str


class SsoComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)


class BindingConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pending_id: str = Field(min_length=1)


class AuthenticationPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["dual", "binding_required", "sso_only", "retired"]
    expected_revision: int = Field(ge=0)
    allow_rollback: bool = False


class AuthenticationAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"]


class AuthenticationGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["enroll", "recover", "replace"]
    subject: str = Field(min_length=1)
    target_user_id: str = ""


class AuthenticationGrantStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["enroll", "recover", "replace"]
    grant_token: str = Field(min_length=1)


class AuthenticationConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    configuration_generation: str = Field(
        min_length=1,
        max_length=AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS,
    )

    @field_validator("configuration_generation")
    @classmethod
    def validate_configuration_generation(cls, value: str) -> str:
        if not is_auth_provider_stable_id(
            value,
            max_chars=AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS,
        ):
            raise ValueError("configuration_generation must be a stable identifier")
        return value
