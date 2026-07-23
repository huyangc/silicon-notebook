from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from app.repositories.sqlite.database import SqliteDatabase


def _checked_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("checked_at must be an offset-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("checked_at must be an offset-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class ModelStatusStore:
    """Deployment-wide latest model-service health observations."""

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def get_all(self) -> dict[str, dict[str, object]]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT service_id, config_fingerprint, status, latency_ms, code, "
                "trigger, support_id, checked_at FROM system_model_service_status"
            ).fetchall()
        return {
            row["service_id"]: {
                "config_fingerprint": row["config_fingerprint"],
                "status": row["status"],
                "latency_ms": row["latency_ms"],
                "code": row["code"],
                "trigger": row["trigger"],
                "support_id": row["support_id"],
                "checked_at": row["checked_at"],
            }
            for row in rows
        }

    def record(
        self,
        *,
        service_id: str,
        config_fingerprint: str,
        status: Literal["ok", "error"],
        latency_ms: int,
        code: str,
        trigger: Literal["manual_test", "observed_failure", "recovery_probe"],
        support_id: str,
        checked_at: str,
    ) -> None:
        normalized_at = _checked_at(checked_at)
        with self.database.write() as db:
            db.execute(
                "INSERT INTO system_model_service_status "
                "(service_id, config_fingerprint, status, latency_ms, code, trigger, "
                "support_id, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(service_id) DO UPDATE SET "
                "config_fingerprint = excluded.config_fingerprint, "
                "status = excluded.status, latency_ms = excluded.latency_ms, "
                "code = excluded.code, trigger = excluded.trigger, "
                "support_id = excluded.support_id, checked_at = excluded.checked_at "
                "WHERE excluded.checked_at > system_model_service_status.checked_at "
                "OR (excluded.checked_at = system_model_service_status.checked_at AND "
                "(CASE WHEN excluded.trigger = 'observed_failure' THEN 3 "
                "      WHEN excluded.status = 'error' THEN 2 ELSE 1 END) > "
                "(CASE WHEN system_model_service_status.trigger = 'observed_failure' THEN 3 "
                "      WHEN system_model_service_status.status = 'error' THEN 2 ELSE 1 END))",
                (
                    str(service_id),
                    str(config_fingerprint),
                    status,
                    max(0, int(latency_ms)),
                    str(code),
                    trigger,
                    str(support_id),
                    normalized_at,
                ),
            )
