"""Filesystem-owned analysis snapshots and failure evidence.

The archive is deliberately outside the user notebook's source directory: a
parser failure must not make the source file disappear or change the user's
notebook.  Only this store writes the archive.  Admin APIs receive projections,
never physical paths, and expose no mutation endpoint.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.domain.model_artifacts import (
    MalformedModelInteraction,
    model_artifact_publication_scope,
    model_artifact_read_scope,
    model_artifact_redaction_scope,
)


ISSUE_CATEGORIES = frozenset({
    "source_parse",
    "spreadsheet_analysis",
    "model_output",
})
ISSUE_STATUSES = frozenset({"open", "resolved"})
MODEL_AREAS = frozenset({
    "ask", "report", "source", "knowledge", "memory", "knowhow", "retrieval"
})


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


class AnalysisArtifactStore:
    """Bounded, content-private analysis artifacts under ``storage_dir``."""

    def __init__(self, storage_dir: Path, *, retention_days: int) -> None:
        self.storage_dir = Path(storage_dir)
        self.retention_days = max(1, int(retention_days))

    @property
    def root(self) -> Path:
        return self.storage_dir / "analysis-artifacts"

    def set_storage_dir(self, value: Path) -> None:
        self.storage_dir = Path(value)

    def _manifest_path(self, notebook_id: str, source_id: str) -> Path:
        return self.root / "spreadsheets" / notebook_id / f"{source_id}.json"

    def _issue_dir(
        self, notebook_id: str, source_id: str, category: str
    ) -> Path:
        if category not in ISSUE_CATEGORIES:
            raise ValueError("unsupported analysis issue category")
        return self.root / "issues" / notebook_id / source_id / category

    def save_spreadsheet_manifest(
        self, notebook_id: str, source_id: str, manifest: dict[str, Any]
    ) -> None:
        _atomic_json(self._manifest_path(notebook_id, source_id), manifest)

    def load_spreadsheet_manifest(
        self, notebook_id: str, source_id: str
    ) -> dict[str, Any] | None:
        path = self._manifest_path(notebook_id, source_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def delete_spreadsheet_manifest(self, notebook_id: str, source_id: str) -> None:
        path = self._manifest_path(notebook_id, source_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._remove_empty_parents(path.parent, self.root / "spreadsheets")

    def record_issue(
        self,
        *,
        notebook_id: str,
        notebook_name: str,
        owner_id: str,
        source_id: str,
        source_title: str,
        file_name: str,
        source_type: str,
        category: str,
        code: str,
        summary: str,
        occurred_at: str,
        source_path: str = "",
        source_hash: str = "",
        archive_file: bool = True,
    ) -> dict[str, Any]:
        issue_dir = self._issue_dir(notebook_id, source_id, category)
        metadata_path = issue_dir / "issue.json"
        previous: dict[str, Any] = {}
        try:
            candidate = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                previous = candidate
        except (OSError, ValueError, TypeError):
            pass
        occurred = _parse_time(occurred_at) or datetime.now(timezone.utc)
        expires = occurred + timedelta(days=self.retention_days)
        artifact_available = False
        payload_path = issue_dir / "payload"
        if archive_file and source_path:
            source = Path(source_path)
            if source.is_file():
                issue_dir.mkdir(parents=True, exist_ok=True)
                temporary = issue_dir / ".payload.tmp"
                try:
                    shutil.copyfile(source, temporary)
                    try:
                        temporary.chmod(0o600)
                    except OSError:
                        pass
                    os.replace(temporary, payload_path)
                    artifact_available = True
                except OSError:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
        if not artifact_available:
            artifact_available = payload_path.is_file()
        issue = {
            "id": f"analysis-{source_id}-{category}",
            "category": category,
            "status": "open",
            "code": str(code)[:80],
            "summary": str(summary)[:500],
            "owner_id": owner_id,
            "notebook_id": notebook_id,
            "notebook_name": str(notebook_name)[:200],
            "source_id": source_id,
            "source_title": str(source_title)[:300],
            "file_name": str(file_name)[:300],
            "source_type": str(source_type)[:40],
            "source_hash": str(source_hash)[:128],
            "created_at": previous.get("created_at") or occurred.isoformat(),
            "updated_at": occurred.isoformat(),
            "resolved_at": "",
            "expires_at": expires.isoformat(),
            "artifact_available": artifact_available,
            "source_deleted": False,
            "notebook_deleted": False,
        }
        _atomic_json(metadata_path, issue)
        return issue

    def record_model_output_issue(
        self, interaction: MalformedModelInteraction
    ) -> dict[str, Any]:
        """Archive an exact rejected structured-model exchange.

        Content lives only in ``artifact.json``.  The issue projection remains
        content-minimal so listing the admin page never bulk-loads prompts or
        responses.
        """
        case_id = secrets.token_urlsafe(16)
        scope_directory = interaction.notebook_id or "_unscoped"
        issue_dir = self._issue_dir(scope_directory, case_id, "model_output")
        occurred = _parse_time(interaction.occurred_at) or datetime.now(timezone.utc)
        expires = occurred + timedelta(days=self.retention_days)
        artifact = {
            "question": interaction.question,
            "messages": list(interaction.messages),
            "schema_hint": interaction.schema_hint,
            "response": interaction.response,
            "workload_id": interaction.workload_id,
            "workload_label": interaction.workload_label,
            "model_area": interaction.model_area,
            "failure_kind": interaction.failure_kind,
            "support_id": interaction.support_id,
            "parent_id": interaction.parent_id,
            "reason": interaction.reason,
            "occurred_at": occurred.isoformat(),
        }
        issue = {
            "id": f"analysis-model-{case_id}",
            "category": "model_output",
            "status": "open",
            "code": "MODEL_OUTPUT_INVALID_JSON_CONTRACT",
            "summary": "模型回答未通过 JSON 协议校验；已保存本次提问与原始回答。",
            "owner_id": interaction.actor_id,
            "notebook_id": interaction.notebook_id,
            "notebook_name": "",
            "source_id": "",
            "source_title": "",
            "file_name": "",
            "source_type": "",
            "source_hash": "",
            "workload_id": interaction.workload_id,
            "workload_label": interaction.workload_label,
            "model_area": interaction.model_area,
            "failure_kind": interaction.failure_kind,
            "support_id": interaction.support_id,
            "parent_id": interaction.parent_id,
            "created_at": occurred.isoformat(),
            "updated_at": occurred.isoformat(),
            "resolved_at": "",
            "expires_at": expires.isoformat(),
            "artifact_available": True,
            "source_deleted": False,
            "notebook_deleted": False,
        }
        with model_artifact_publication_scope(
            interaction.notebook_id
        ) as current_epoch:
            if (
                interaction.lifecycle_epoch is not None
                and interaction.lifecycle_epoch != current_epoch
            ):
                return {}
            try:
                _atomic_json(issue_dir / "artifact.json", artifact)
                _atomic_json(issue_dir / "issue.json", issue)
            except BaseException:
                # A case is discoverable only through issue.json. Never leave a
                # prompt/response behind when publishing that metadata fails.
                shutil.rmtree(issue_dir, ignore_errors=True)
                self._remove_empty_parents(
                    issue_dir.parent,
                    self.root / "issues",
                )
                raise
        return issue

    def load_model_output_artifact(
        self,
        issue_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read one unexpired model artifact by opaque issue id."""
        with model_artifact_read_scope():
            current = now or datetime.now(timezone.utc)
            issue_root = self.root / "issues"
            if not issue_root.is_dir():
                return None
            for metadata_path in list(issue_root.glob("*/*/*/issue.json")):
                try:
                    issue = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(issue, dict) or issue.get("id") != issue_id:
                    continue
                expires = _parse_time(str(issue.get("expires_at") or ""))
                if expires is not None and expires <= current:
                    shutil.rmtree(metadata_path.parent, ignore_errors=True)
                    self._remove_empty_parents(
                        metadata_path.parent.parent,
                        issue_root,
                    )
                    return None
                if issue.get("category") != "model_output":
                    return None
                try:
                    artifact = json.loads(
                        (metadata_path.parent / "artifact.json").read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, ValueError, TypeError):
                    return None
                if not isinstance(artifact, dict):
                    return None
                return {"issue_id": issue_id, **artifact}
        return None

    def resolve_issue(
        self,
        notebook_id: str,
        source_id: str,
        category: str,
        *,
        resolved_at: str,
    ) -> None:
        issue_dir = self._issue_dir(notebook_id, source_id, category)
        metadata_path = issue_dir / "issue.json"
        try:
            issue = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(issue, dict):
            return
        self._delete_payload(issue_dir)
        issue.update({
            "status": "resolved",
            "resolved_at": resolved_at,
            "updated_at": resolved_at,
            "artifact_available": False,
        })
        _atomic_json(metadata_path, issue)

    def redact_source(
        self, notebook_id: str, source_id: str, *, occurred_at: str
    ) -> None:
        with model_artifact_redaction_scope(notebook_id):
            failure: BaseException | None = None
            try:
                self.delete_spreadsheet_manifest(notebook_id, source_id)
            except BaseException as exc:
                failure = exc
            # Model prompts may contain evidence from several sources but the
            # scheduler boundary does not own a trustworthy source-id ledger.
            # Conservatively destroy every retained model payload for this
            # notebook when any source is deleted instead of retaining content
            # that may have come from the deleted source.
            try:
                self._redact_model_outputs_for_notebook(notebook_id, occurred_at)
            except BaseException as exc:
                if failure is None:
                    failure = exc
            source_root = self.root / "issues" / notebook_id / source_id
            if source_root.is_dir():
                try:
                    for metadata_path in list(source_root.glob("*/issue.json")):
                        try:
                            self._redact_issue(
                                metadata_path,
                                occurred_at,
                                notebook_deleted=False,
                            )
                        except BaseException as exc:
                            if failure is None:
                                failure = exc
                finally:
                    shutil.rmtree(source_root, ignore_errors=True)
                    self._remove_empty_parents(
                        source_root.parent,
                        self.root / "issues",
                    )
            if failure is not None:
                raise failure

    def _redact_model_outputs_for_notebook(
        self, notebook_id: str, occurred_at: str
    ) -> None:
        issue_root = self.root / "issues" / notebook_id
        if not issue_root.is_dir():
            return
        failure: BaseException | None = None
        for metadata_path in list(issue_root.glob("*/model_output/issue.json")):
            try:
                self._redact_issue(
                    metadata_path,
                    occurred_at,
                    notebook_deleted=False,
                )
            except BaseException as exc:
                if failure is None:
                    failure = exc
            finally:
                shutil.rmtree(metadata_path.parent.parent, ignore_errors=True)
        self._remove_empty_parents(issue_root, self.root / "issues")
        if failure is not None:
            raise failure

    def redact_notebook(self, notebook_id: str, *, occurred_at: str) -> None:
        with model_artifact_redaction_scope(notebook_id):
            spreadsheet_root = self.root / "spreadsheets" / notebook_id
            if spreadsheet_root.exists():
                shutil.rmtree(spreadsheet_root, ignore_errors=True)
            issue_root = self.root / "issues" / notebook_id
            if not issue_root.is_dir():
                return
            failure: BaseException | None = None
            try:
                for metadata_path in list(issue_root.glob("*/*/issue.json")):
                    try:
                        self._redact_issue(
                            metadata_path,
                            occurred_at,
                            notebook_deleted=True,
                        )
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
            finally:
                shutil.rmtree(issue_root, ignore_errors=True)
            if failure is not None:
                raise failure

    def _redact_issue(
        self, metadata_path: Path, occurred_at: str, *, notebook_deleted: bool
    ) -> None:
        try:
            issue = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(issue, dict):
            return
        category = str(issue.get("category") or "")
        if category not in ISSUE_CATEGORIES:
            return
        had_source = bool(issue.get("source_id"))
        self._delete_payload(metadata_path.parent)
        neutral_id = secrets.token_hex(16)
        issue.update({
            "id": f"analysis-redacted-{neutral_id}",
            "code": "",
            "owner_id": "",
            "notebook_id": "",
            "notebook_name": "",
            "source_id": "",
            "source_title": "",
            "file_name": "",
            "source_type": "",
            "source_hash": "",
            "support_id": "",
            "parent_id": "",
            "summary": "原关联内容已删除；仅保留问题分类与时间信息。",
            "artifact_available": False,
            "source_deleted": had_source,
            "notebook_deleted": notebook_deleted,
            "updated_at": occurred_at,
        })
        redacted_path = (
            self.root / "issues" / "redacted" / neutral_id / category / "issue.json"
        )
        _atomic_json(redacted_path, issue)

    def list_issues(
        self,
        *,
        owner_id: str = "",
        status: str = "",
        category: str = "",
        model_area: str = "",
        limit: int = 200,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if status and status not in ISSUE_STATUSES:
            raise ValueError("unsupported analysis issue status")
        if category and category not in ISSUE_CATEGORIES:
            raise ValueError("unsupported analysis issue category")
        if model_area and model_area not in MODEL_AREAS:
            raise ValueError("unsupported model analysis area")
        current = now or datetime.now(timezone.utc)
        rows: list[dict[str, Any]] = []
        issue_root = self.root / "issues"
        if not issue_root.is_dir():
            return rows
        for metadata_path in list(issue_root.glob("*/*/*/issue.json")):
            try:
                item = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            expires = _parse_time(str(item.get("expires_at") or ""))
            if expires is not None and expires <= current:
                shutil.rmtree(metadata_path.parent, ignore_errors=True)
                self._remove_empty_parents(
                    metadata_path.parent.parent, issue_root
                )
                continue
            if owner_id and item.get("owner_id") != owner_id:
                continue
            if status and item.get("status") != status:
                continue
            if category and item.get("category") != category:
                continue
            if model_area and item.get("model_area") != model_area:
                continue
            rows.append(item)
        rows.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")),
            reverse=True,
        )
        return rows[: max(1, min(int(limit), 500))]

    @staticmethod
    def _delete_payload(issue_dir: Path) -> None:
        for name in ("payload", "artifact.json"):
            try:
                (issue_dir / name).unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _remove_empty_parents(path: Path, stop: Path) -> None:
        current = path
        while current != stop and stop in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
