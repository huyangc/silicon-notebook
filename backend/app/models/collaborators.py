"""Named sharing ("点名共享") wire models (share-edit P1-T6).

A notebook's named grants are ``notebook_grants`` rows with
``principal_type='user'``: the editors the owner added, and legacy named
``viewer`` / ``admin`` rows. Together with ``GET /notebooks/{id}/grants``
(group and everyone grants) they account for every grant row of the notebook.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel


class CollaboratorItem(BaseModel):
    """One named grant, as the owner sees it."""

    id: str
    user_id: str
    username: str
    display_name: str = ""
    # "editor" for a named editor; a legacy named row keeps its own role
    # ("viewer" / "admin") so the owner can see and remove it.
    role: str
    granted_by_username: str = ""
    created_at: str = ""


class EditorName(BaseModel):
    """Decision D17: what an editor sees of the other editors."""

    username: str
    display_name: str = ""


class CollaboratorList(BaseModel):
    """``GET /notebooks/{id}/collaborators``. The owner gets every named grant
    (``items``); a caller holding a named editor grant gets the editors' names
    only (``editors``). ``view`` says which of the two this is."""

    view: Literal["owner", "editor"]
    items: List[CollaboratorItem] = []
    editors: List[EditorName] = []


class CollaboratorPutResult(BaseModel):
    """``PUT /notebooks/{id}/collaborators/{user_id}``. ``changed=False``: the
    user already was an editor (idempotent, nobody is notified again)."""

    grant: CollaboratorItem
    changed: bool
    previous_role: Optional[str] = None
