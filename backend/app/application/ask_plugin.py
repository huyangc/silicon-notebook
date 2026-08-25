"""Immutable ownership transfer for one deployment Ask engine turn."""
from __future__ import annotations

from dataclasses import dataclass

from app.models.ask import AskResponse


@dataclass(frozen=True, slots=True)
class PreparedPluginAsk:
    mode_id: str
    notebook_id: str
    question: str
    conversation_id: str
    user_id: str
    job_id: str
    asked_at: str


@dataclass(frozen=True, slots=True)
class PluginResponseDraft:
    mode_id: str
    notebook_id: str
    question: str
    conversation_id: str
    user_id: str
    job_id: str
    asked_at: str
    response: AskResponse


__all__ = ["PluginResponseDraft", "PreparedPluginAsk"]
