from __future__ import annotations

# Pure re-export: the actual definitions live in ``app.domain.cancellation``
# (a stable, dependency-free layer) so that ``app.core``/``app.models`` can
# import cancellation primitives without reaching into ``app.services`` --
# see ``scripts/architecture_boundary_baseline.json`` :: core_models_service_imports
# (now empty). Every existing importer of ``app.services.cancellation`` keeps
# working unchanged; only the canonical definition moved.
from app.domain.cancellation import (  # noqa: F401
    AskCancelled,
    CancelEvent,
    raise_if_cancelled,
    sleep_or_cancel,
)

__all__ = ["AskCancelled", "CancelEvent", "raise_if_cancelled", "sleep_or_cancel"]
