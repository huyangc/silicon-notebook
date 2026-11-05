"""Display-only evidence; internal source/element identities never cross here."""
from typing import Literal

from pydantic import BaseModel


class PublicEvidence(BaseModel):
    kind: Literal["original", "knowledge", "memory"]
    text: str
    quote: str = ""
    location: str = ""
    truncated: bool = False
