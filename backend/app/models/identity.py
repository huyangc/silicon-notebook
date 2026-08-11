from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    username: str = ""
    memory_mode: str = "manual"
    domain_focus: List[str] = Field(default_factory=list)
    ui_mode: str = "auto"


class UiModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ui_mode: Literal["auto", "advanced"]


class AgentProfile(BaseModel):
    id: str
    owner_id: str
    name: str
    description: str = ""
    status: Literal["active", "revoked"] = "active"
    created_at: str
    updated_at: str


class AgentProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class AgentProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal["active", "revoked"]] = None


class AgentTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_profile_id: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None


class AgentTokenSummary(BaseModel):
    id: str
    agent_profile_id: str
    profile_name: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None
    created_at: str


class AgentTokenIssued(BaseModel):
    id: str
    token: str
    agent_profile_id: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    created_at: str


class AgentPrincipal(BaseModel):
    profile_id: str
    profile_name: str
    owner_id: str
    scopes: List[str] = Field(default_factory=list)
    default_notebook_id: str = Field(min_length=1)
    notebook_ids: List[str] = Field(default_factory=list)
    token_id: str


class AuthRequest(BaseModel):
    username: str
    password: str


class AuthResult(BaseModel):
    token: str
    user: UserProfile


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_password: str
    new_password: str
