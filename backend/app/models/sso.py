"""Public, supplier-independent authentication messages."""
from pydantic import BaseModel, ConfigDict, Field


class SsoComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)


class SsoChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pending_id: str = Field(min_length=1)


class SsoLink(SsoChoice):
    login_name: str
    password: str
