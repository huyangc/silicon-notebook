#!/usr/bin/env python3
"""Prepare selected-source graph data offline, then enable invisible shadow.

The API and all background writers must already be stopped. Database work is
completed and independently audited before the requested env file is changed.
The content-free receipt is informational; every rerun revalidates authoritative
database/artifact state and relies on the durable per-source/per-notebook ledgers
to skip or resume committed work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import audit_source_facts as source_fact_audit
from pydantic import ValidationError

from app.core.config import Settings
from app.core.database_url import database_identity, redact_database_url
from app.services.batch_ingest import (
    run_backfill_source_facts,
    run_backfill_source_index,
)
from app.services.maintenance_cli import open_maintenance_cli_repository


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CAMPAIGN_VERSION = 1
ROLLOUT_ENV = {
    "SOURCE_SUBGRAPH_PPR_ENABLED": "true",
    "SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED": "true",
    "SOURCE_PARTITIONED_PPR_ENABLED": "true",
    "SELECTED_SOURCE_GRAPH_ROLLOUT_MODE": "shadow",
}
_ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$"
)


class PreparationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                # The atomic replace is already committed in this process.
                # Directory fsync support varies by filesystem; do not report
                # a false rollout failure after the env file has changed.
                pass
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _database_fingerprint(database_url: str) -> str:
    safe_identity = redact_database_url(database_url)
    return hashlib.sha256(safe_identity.encode("utf-8")).hexdigest()


def _load_receipt(path: Path, fingerprint: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "campaign_version": CAMPAIGN_VERSION,
            "database_fingerprint": fingerprint,
            "attempt": 0,
            "status": "pending",
            "failure_code": "",
            "env_updated": False,
            "notebooks": {},
            "created_at": _now(),
            "updated_at": _now(),
        }
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError("receipt_invalid") from exc
    try:
        valid = (
            isinstance(receipt, dict)
            and int(receipt.get("campaign_version", 0)) == CAMPAIGN_VERSION
            and receipt.get("database_fingerprint") == fingerprint
            and isinstance(receipt.get("notebooks", {}), dict)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise PreparationError("receipt_identity_mismatch")
    receipt.setdefault("notebooks", {})
    return receipt


def render_enabled_env(original: str) -> str:
    """Replace active assignments once and preserve unrelated env content."""
    rendered: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines():
        match = _ASSIGNMENT.match(line)
        key = match.group("key") if match else ""
        if key not in ROLLOUT_ENV:
            rendered.append(line)
            continue
        if key in seen:
            continue
        rendered.append(f"{key}={ROLLOUT_ENV[key]}")
        seen.add(key)
    missing = [key for key in ROLLOUT_ENV if key not in seen]
    if missing:
        if rendered and rendered[-1] != "":
            rendered.append("")
        rendered.append(
            "# Selected-source graph rollout (managed by prepare_selected_source_graph.py)"
        )
        rendered.extend(f"{key}={ROLLOUT_ENV[key]}" for key in missing)
    return "\n".join(rendered).rstrip("\n") + "\n"


def _enable_env(path: Path) -> None:
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise PreparationError("env_read_failed") from exc
    _atomic_write(path, render_enabled_env(original))


def _visible_source_count(maintenance: Any, notebook_id: str) -> int:
    count = 0
    after_id = ""
    while True:
        page = maintenance.user_source_ids_page(
            notebook_id, after_id=after_id, limit=500
        )
        if not page:
            return count
        count += len(page)
        after_id = page[-1]
        if len(page) < 500:
            return count


def _audit_notebooks(settings: Settings, notebook_ids: Sequence[str]) -> dict[str, Any]:
    identity = database_identity(settings.database_url)
    connection, placeholder, dialect = (
        source_fact_audit._sqlite(identity.database)
        if identity.scheme == "sqlite"
        else source_fact_audit._postgres(settings.database_url)
    )
    totals = {"sources": 0, "persisted_facts": 0, "expected_objects": 0}
    try:
        for notebook_id in notebook_ids:
            report = source_fact_audit.audit(
                connection, placeholder, notebook_id, 0, dialect
            )
            if any(
                status != "complete" and int(count) > 0
                for status, count in report["status_counts"].items()
            ):
                raise PreparationError("source_fact_audit_incomplete")
            for key in totals:
                totals[key] += int(report[key])
    finally:
        connection.close()
    return totals


def _settings(env_file: Path) -> Settings:
    return Settings(
        _env_file=env_file if env_file.exists() else None,
        SOURCE_SUBGRAPH_PPR_ENABLED=True,
        SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED=True,
        SOURCE_PARTITIONED_PPR_ENABLED=True,
        SELECTED_SOURCE_GRAPH_ROLLOUT_MODE="shadow",
    )


def run_preparation(
    *,
    settings: Settings,
    env_file: Path,
    receipt_path: Path,
    confirm_service_stopped: bool,
) -> dict[str, Any]:
    if not confirm_service_stopped:
        raise PreparationError("service_stop_confirmation_required")
    fingerprint = _database_fingerprint(settings.database_url)
    receipt = _load_receipt(receipt_path, fingerprint)
    receipt.update(
        {
            "attempt": int(receipt.get("attempt", 0)) + 1,
            "status": "running",
            "failure_code": "",
            "env_updated": False,
            "updated_at": _now(),
        }
    )
    try:
        _write_receipt(receipt_path, receipt)
    except OSError as exc:
        raise PreparationError("receipt_write_failed") from exc
    active_phase = "open_repository"
    active_notebook = ""
    try:
        with open_maintenance_cli_repository(
            settings,
            confirm_service_stopped=True,
            capability="portable",
        ) as repository:
            targets = repository.maintenance.all_notebook_ids()
            receipt["notebook_count"] = len(targets)
            for notebook_id in targets:
                active_notebook = notebook_id
                notebook_state = receipt["notebooks"].setdefault(notebook_id, {})

                active_phase = "source_index"
                index_result = run_backfill_source_index(
                    repository, notebook_id, all_notebooks=False
                )
                notebook_state.update(
                    {
                        "phase": "source_index_complete",
                        "source_index_objects": int(index_result["objects"]),
                        "source_index_rows": int(index_result["rows"]),
                        "updated_at": _now(),
                    }
                )
                _write_receipt(receipt_path, receipt)

                active_phase = "source_facts"
                fact_result = run_backfill_source_facts(
                    repository, notebook_id, all_notebooks=False, force=False
                )
                fact_state = fact_result["notebooks"][0]
                noncomplete = sum(
                    int(fact_state.get(key, 0))
                    for key in ("incomplete", "busy", "no_generation", "failed")
                )
                if noncomplete:
                    raise PreparationError("source_fact_backfill_incomplete")
                notebook_state.update(
                    {
                        "phase": "source_facts_complete",
                        "source_fact_sources": int(fact_state["sources"]),
                        "source_facts_written": int(fact_state["facts_written"]),
                        "updated_at": _now(),
                    }
                )
                _write_receipt(receipt_path, receipt)

                active_phase = "artifacts"
                visible_sources = _visible_source_count(
                    repository.maintenance, notebook_id
                )
                artifact = repository.maintenance.selected_source_graph_artifact_status(
                    notebook_id
                )
                if (
                    not artifact["ready"]
                    or int(artifact["unavailable_sources"])
                    or int(artifact["published_sources"]) != visible_sources
                ):
                    repository.build_scale_index(notebook_id)
                    artifact = (
                        repository.maintenance.selected_source_graph_artifact_status(
                            notebook_id
                        )
                    )
                if (
                    not artifact["ready"]
                    or int(artifact["unavailable_sources"])
                    or int(artifact["published_sources"]) != visible_sources
                ):
                    raise PreparationError("partition_artifact_incomplete")
                notebook_state.update(
                    {
                        "phase": "artifacts_complete",
                        "indexed_nodes": int(artifact["n_nodes"]),
                        "partitioned_sources": int(artifact["published_sources"]),
                        "updated_at": _now(),
                    }
                )
                _write_receipt(receipt_path, receipt)

            active_phase = "audit"
            receipt["audit"] = _audit_notebooks(settings, targets)
            for notebook_id in targets:
                receipt["notebooks"][notebook_id]["phase"] = "audited"
            receipt["updated_at"] = _now()
            _write_receipt(receipt_path, receipt)

        active_phase = "env_update"
        _enable_env(env_file)
        receipt.update(
            {
                "status": "complete",
                "failure_code": "",
                "failed_phase": "",
                "failed_notebook_id": "",
                "env_updated": True,
                "completed_at": _now(),
                "updated_at": _now(),
            }
        )
        receipt["receipt_updated"] = True
        try:
            _write_receipt(receipt_path, receipt)
        except OSError:
            # The receipt is explicitly non-authoritative. Once the atomic env
            # replacement commits, a receipt-only failure must not turn a
            # successful activation into a contradictory failed exit.
            receipt["receipt_updated"] = False
        return receipt
    except Exception as exc:
        code = exc.code if isinstance(exc, PreparationError) else f"{active_phase}_failed"
        receipt.update(
            {
                "status": "failed",
                "failure_code": code,
                "failed_phase": active_phase,
                "failed_notebook_id": active_notebook,
                "env_updated": False,
                "updated_at": _now(),
            }
        )
        try:
            _write_receipt(receipt_path, receipt)
        except OSError:
            pass
        raise PreparationError(code) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env",
        help="deployment env file to update only after all DB/artifact checks pass",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="content-free resumable receipt (default: STORAGE_DIR/maintenance/...)",
    )
    parser.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="required assertion that API and every background writer are stopped",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_file = args.env_file.resolve()
    if not env_file.is_file():
        print("prepare-selected-source-graph failed: env_file_missing", file=sys.stderr)
        return 1
    try:
        settings = _settings(env_file)
    except (OSError, UnicodeError, ValidationError):
        print("prepare-selected-source-graph failed: settings_invalid", file=sys.stderr)
        return 1
    receipt = (
        args.receipt.resolve()
        if args.receipt is not None
        else Path(settings.storage_dir)
        / "maintenance"
        / "selected-source-graph-deployment.json"
    )
    try:
        result = run_preparation(
            settings=settings,
            env_file=env_file,
            receipt_path=receipt,
            confirm_service_stopped=args.confirm_service_stopped,
        )
    except PreparationError as exc:
        print(f"prepare-selected-source-graph failed: {exc.code}", file=sys.stderr)
        return 1
    print(
        "prepare-selected-source-graph complete: "
        f"notebooks={result.get('notebook_count', 0)} receipt={receipt} "
        f"receipt_updated={str(result.get('receipt_updated', False)).lower()}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
