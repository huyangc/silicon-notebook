from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Closed value domains for the per-user search/answer style preference
# document. They LIVE here (the models layer) rather than in
# ``app.services.search_profile`` because the domain-model boundary guard
# (``test_model_domain_boundaries``) forbids ``app.models`` importing
# ``app.services`` — and both layers need these: the wire models below
# validate requests against them, and the service module validates stored
# documents against the SAME sets (it imports them from here; services →
# models is the allowed direction). Definitions and prose contract stay
# documented in ``app.services.search_profile``'s module docstring.
ANSWER_LANGUAGE_VALUES: frozenset[str] = frozenset({"auto", "zh", "en"})
ANSWER_SHAPE_VALUES: frozenset[str] = frozenset(
    {"auto", "bullets", "table_first", "prose"}
)
ANSWER_DETAIL_VALUES: frozenset[str] = frozenset({"auto", "concise", "detailed"})
#: Bounds for the user-authored ``domain_terms`` list. Exact values are
#: registered only in ``docs/product-and-api*.md`` (numeric-limit red line).
SEARCH_PROFILE_DOMAIN_TERMS_MAX = 10
SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS = 32


class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    username: str = ""
    memory_mode: str = "manual"
    domain_focus: List[str] = Field(default_factory=list)
    ui_mode: str = "auto"
    #: Agentic Memory P3 (T6) — the per-user search/answer style preference
    #: document (``user_profiles.search_profile_json``, parsed). ``None``
    #: means "the user has never set a preference and no inference job has
    #: ever written one" — same NULL-means-unset contract as ``ui_mode``,
    #: but represented as an optional structured dict instead of a string
    #: default, since a per-field ``{value, origin, updated_at}`` shape has
    #: no single sentinel to fall back to. See
    #: ``app.services.search_profile`` for the document contract.
    search_profile: Optional[Dict[str, Any]] = None


class UiModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ui_mode: Literal["auto", "advanced"]


#: Wire-level Literal restatements of search_profile.py's closed value
#: domains, kept in sync by the assertions below — same "duplicate as a
#: Literal, then assert the two agree" idiom `agent_profile.py`'s
#: `UnderstandingLabel` already uses, so a value added in one place and not
#: the other fails at import time instead of the wire model silently
#: accepting (or rejecting) a value the store-layer validator disagrees with.
AnswerLanguage = Literal["auto", "zh", "en"]
AnswerShape = Literal["auto", "bullets", "table_first", "prose"]
AnswerDetail = Literal["auto", "concise", "detailed"]
assert set(AnswerLanguage.__args__) == ANSWER_LANGUAGE_VALUES, (
    "AnswerLanguage and ANSWER_LANGUAGE_VALUES must name exactly the same values"
)
assert set(AnswerShape.__args__) == ANSWER_SHAPE_VALUES, (
    "AnswerShape and ANSWER_SHAPE_VALUES must name exactly the same values"
)
assert set(AnswerDetail.__args__) == ANSWER_DETAIL_VALUES, (
    "AnswerDetail and ANSWER_DETAIL_VALUES must name exactly the same values"
)


class SearchProfileUpdate(BaseModel):
    """``PATCH /me/search-profile`` request body.

    Every field is ``Optional`` with a default of ``None`` — but ``None`` is
    NOT "leave unchanged". The route handler distinguishes "field omitted
    from the request body" (leave that field alone) from "field sent as
    ``null``" (clear that field) via ``model_fields_set``, which pydantic
    populates with exactly the field names that were present in the payload
    regardless of what value they carried. A field absent from
    ``model_fields_set`` is never touched; one present with value ``None``
    is passed through to ``search_profile.merge_field`` as a clear.
    """

    model_config = ConfigDict(extra="forbid")

    answer_language: Optional[AnswerLanguage] = None
    answer_shape: Optional[AnswerShape] = None
    answer_detail: Optional[AnswerDetail] = None
    domain_terms: Optional[List[str]] = None

    @field_validator("domain_terms")
    @classmethod
    def _validate_domain_terms(cls, value: "List[str] | None") -> "List[str] | None":
        if value is None:
            return value
        if len(value) > SEARCH_PROFILE_DOMAIN_TERMS_MAX:
            raise ValueError(
                f"at most {SEARCH_PROFILE_DOMAIN_TERMS_MAX} domain terms are allowed"
            )
        cleaned: List[str] = []
        for term in value:
            trimmed = term.strip()
            if not trimmed or len(trimmed) > SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS:
                raise ValueError(
                    "each domain term must be 1.."
                    f"{SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS} characters after trimming"
                )
            cleaned.append(trimmed)
        return cleaned


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
