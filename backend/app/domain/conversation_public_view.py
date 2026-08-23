"""Public-conversation-page constants sunk from app.services.conversation_public_view in B3.

Only ``MAX_TURNS`` moves — it's the one name app.repositories (ask_state_store,
both backends) imports directly (a safety ceiling on how many turns one
public page renders). The rest of the public-conversation projection stays in
app.services.conversation_public_view (it depends on request/response models
and other services). ``app.services.conversation_public_view`` re-exports
this name unchanged for existing importers.
"""
from __future__ import annotations

# Safety ceiling on how many turns one public page renders. A conversation's
# turn count is bounded by how many times a user asked, so this almost never
# binds; it exists so a pathological conversation cannot balloon one anonymous
# response. Truncation is disclosed (``truncated_turns``) rather than silent.
MAX_TURNS = 500
