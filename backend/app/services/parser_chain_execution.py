"""Core-owned production adapter for the parser ProviderChain.

Provider probes are request-local and inert: they may perform one parser I/O
and pure mapping, but cannot persist assets.  Only the accepted materializer
receives the image persistence collaborator and deletes the previous asset
generation.  Services depend on the domain host port, never the Extension SDK.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from app.domain.extensions import (
    PARSER_BUILTIN_PROVIDER,
    PARSER_CLOUD_PROVIDER,
    PARSER_SELF_HOSTED_PROVIDER,
    ParsedSource,
    ParserAdmission,
    ParserProbe,
    ParserProviderChainHostPort,
    ParserRoute,
    ParserSourceDescriptor,
)
from app.models.sources import SourceElement
from app.services import remote_sources
from app.services.parser_registry import engine_supports_file
from app.services.parsers import (
    MINERU_FALLBACK_WARNING_SUFFIXES,
    MINERU_WORKBOOK_SUFFIXES,
    mineru_content_list_to_elements,
    mineru_label_prefix,
    mineru_workbook_reconciliation,
    parse_builtin_source_file,
)


PARSER_FALLBACK_WARNING_CODE = "parser_fallback"


class _NeverCancelled:
    @staticmethod
    def is_set() -> bool:
        return False


class BuiltinParserChainHost:
    """Compatibility host for direct repository construction.

    The application composition root always injects the frozen extension host.
    This provider-chain-shaped fallback keeps library/test constructors on the
    guaranteed builtin surface without restoring the retired dispatcher.
    """

    def run_application(self, baseline: ParsedSource, *, call: Any) -> ParsedSource:
        try:
            cancelled = call.cancellation.is_set()
            held = call.connection.is_connection_held()
        except Exception:
            return baseline
        if type(cancelled) is not bool or cancelled:
            return baseline
        if type(held) is not bool or held:
            return baseline
        route = call.route(PARSER_BUILTIN_PROVIDER)
        if type(route) is not ParserRoute or not route.allowed:
            return baseline
        proposal = call.probe(PARSER_BUILTIN_PROVIDER)
        if type(proposal) is not ParserProbe or not proposal.accepted:
            return baseline
        decision = call.admit(PARSER_BUILTIN_PROVIDER, proposal.value)
        if type(decision) is not ParserAdmission or not decision.accepted:
            return baseline
        return call.materialize(PARSER_BUILTIN_PROVIDER, proposal.value)


@dataclass(frozen=True)
class _Candidate:
    content_list: Any = None
    images: Any = None


@dataclass(frozen=True)
class _ProposalToken:
    """Opaque identity exposed to a plugin; authoritative raw stays private."""

    contribution_id: str


class ParserChainExecution:
    """One source parse invocation projected into the frozen host topology."""

    def __init__(
        self,
        *,
        host: ParserProviderChainHostPort,
        source_id: str,
        source_kind: str,
        file_path: str,
        file_name: str,
        source_url: str,
        mineru_client: Any,
        cloud_client: Any,
        connection: Any,
        make_persist_image: Callable[[], Any],
        delete_source_images: Callable[[], None],
        event_sink: Callable[[dict[str, object]], None],
    ) -> None:
        self.host = host
        self.source_id = source_id
        self.file_path = file_path
        self.file_name = file_name
        self.source_url = source_url
        self.mineru_client = mineru_client
        self.cloud_client = cloud_client
        self.connection = connection
        self.make_persist_image = make_persist_image
        self.delete_source_images = delete_source_images
        self.event_sink = event_sink
        self.cancellation = _NeverCancelled()
        suffix = ".pdf" if source_kind == "url" else Path(file_name).suffix.lower()
        self.source = ParserSourceDescriptor(source_kind, suffix)
        self._self_hosted_configured = bool(
            getattr(mineru_client, "configured", False)
        )
        self._cloud_configured = bool(getattr(cloud_client, "configured", False))
        self._self_hosted_capable = (
            source_kind == "url"
            or engine_supports_file("mineru_self_hosted", file_name)
        )
        self._cloud_capable = (
            source_kind == "url"
            or engine_supports_file("mineru_cloud", file_name)
        )
        self._self_hosted_allowed = (
            self._self_hosted_configured and self._self_hosted_capable
        )
        self._cloud_allowed = (
            not self._self_hosted_configured
            and self._cloud_configured
            and self._cloud_capable
        )
        self._attempted_self_hosted = False
        self._attempted_cloud = False
        self._mineru_error = ""
        self._temp_path: Path | None = None
        self.materialized = False
        self.assets_pending = False
        self._proposals: dict[str, _ProposalToken] = {}
        self._payloads: dict[str, _Candidate] = {}
        self._probe_started: set[str] = set()
        self._probe_results: dict[str, ParserProbe] = {}

    def run(self) -> ParsedSource:
        baseline = ParsedSource((), "", "")
        try:
            result = self.host.run_application(baseline, call=self)
            if self.materialized is not True:
                # The built-in link is the guaranteed terminal surface.  Host
                # fail-open protects extension isolation, but application
                # ingestion must not relabel an exhausted/blocked/raising
                # chain as a successful empty document. A genuinely empty
                # parser result still sets ``materialized`` and remains valid.
                raise RuntimeError("parser provider chain exhausted")
            return result
        finally:
            if self._temp_path is not None:
                try:
                    self._temp_path.unlink()
                except OSError:
                    pass

    def route(self, contribution_id: str) -> ParserRoute:
        if contribution_id == PARSER_SELF_HOSTED_PROVIDER:
            return ParserRoute(
                self._self_hosted_allowed,
                "private_service",
                "parser_route_prohibited" if not self._self_hosted_allowed else "",
            )
        if contribution_id == PARSER_CLOUD_PROVIDER:
            return ParserRoute(
                self._cloud_allowed,
                "public_cloud",
                "parser_route_prohibited" if not self._cloud_allowed else "",
            )
        if contribution_id == PARSER_BUILTIN_PROVIDER:
            warning = (
                PARSER_FALLBACK_WARNING_CODE
                if (
                    (self._self_hosted_allowed or self._cloud_allowed)
                    and self.source.suffix in MINERU_FALLBACK_WARNING_SUFFIXES
                )
                else ""
            )
            return ParserRoute(True, "local", fallback_warning_code=warning)
        return ParserRoute(False, "local", "unknown_parser_provider")

    def probe(self, contribution_id: str) -> ParserProbe:
        cached = self._probe_results.get(contribution_id)
        if type(cached) is ParserProbe:
            return cached
        if contribution_id in self._probe_started:
            # Claim the request-local slot before any provider I/O. A buggy or
            # adversarial link may call its projected access more than once;
            # neither a recursive call nor a caught first failure may retry the
            # external parser inside one chain invocation.
            return ParserProbe(False, reason_code="parser_probe_already_attempted")
        self._probe_started.add(contribution_id)
        if contribution_id == PARSER_BUILTIN_PROVIDER:
            token = _ProposalToken(contribution_id)
            self._proposals[contribution_id] = token
            self._payloads[contribution_id] = _Candidate()
            result = ParserProbe(True, token)
            self._probe_results[contribution_id] = result
            return result
        try:
            if contribution_id == PARSER_SELF_HOSTED_PROVIDER:
                self._attempted_self_hosted = True
                path = self._local_path()
                content_list, images = self.mineru_client.parse_with_images(
                    str(path), self.file_name or path.name
                )
            elif contribution_id == PARSER_CLOUD_PROVIDER:
                self._attempted_cloud = True
                if self.source.kind == "url":
                    content_list, images = self.cloud_client.parse_url_with_images(
                        self.source_url, data_id=self.source_id
                    )
                else:
                    content_list, images = self.cloud_client.parse_file_with_images(
                        self.file_path, data_id=self.source_id
                    )
            else:
                result = ParserProbe(
                    False, reason_code="unknown_parser_provider"
                )
                self._probe_results[contribution_id] = result
                return result
            # Freeze the authoritative payload before it crosses the probe
            # boundary. Plugins receive only an opaque token; even a mutable
            # client return object changed after probe cannot rewrite an
            # already-admitted workbook or image plan.
            content_list = copy.deepcopy(content_list)
            images = copy.deepcopy(images)
            if self.source.suffix in MINERU_WORKBOOK_SUFFIXES:
                # Workbook acceptance compares the pure, asset-free mapping
                # with the source sheet before commit. Other formats have no
                # semantic admission beyond successful materialization, so
                # they map exactly once below instead of doubling CPU/allocation.
                elements = tuple(
                    mineru_content_list_to_elements(
                        self.source_id,
                        content_list,
                        label_prefix=mineru_label_prefix(self.file_name),
                    )
                )
                if not elements:
                    self._mineru_error = (
                        "MinerU content_list mapped to zero source elements"
                    )
                    result = ParserProbe(
                        False, reason_code="empty_parser_output"
                    )
                    self._probe_results[contribution_id] = result
                    return result
                accepted, rows, total_rows, cells, total_cells = (
                    mineru_workbook_reconciliation(
                        self._local_path(), list(elements)
                    )
                )
                if not accepted:
                    self._mineru_error = (
                        f"MinerU workbook output covered {rows}/{total_rows} rows "
                        f"and {cells}/{total_cells} cells; using openpyxl"
                    )
                    result = ParserProbe(
                        False, reason_code="workbook_coverage_rejected"
                    )
                    self._probe_results[contribution_id] = result
                    return result
            self._mineru_error = str(
                getattr(
                    self.mineru_client
                    if contribution_id == PARSER_SELF_HOSTED_PROVIDER
                    else self.cloud_client,
                    "last_error",
                    "",
                )
                or ""
            )
            candidate = _Candidate(
                content_list=content_list,
                images=images,
            )
            token = _ProposalToken(contribution_id)
            self._proposals[contribution_id] = token
            self._payloads[contribution_id] = candidate
            result = ParserProbe(True, token, reason_code="accepted")
            self._probe_results[contribution_id] = result
            return result
        except Exception as exc:
            client = (
                self.mineru_client
                if contribution_id == PARSER_SELF_HOSTED_PROVIDER
                else self.cloud_client
            )
            self._mineru_error = str(getattr(client, "last_error", "") or exc)
            self._probe_results[contribution_id] = ParserProbe(
                False, reason_code="parser_probe_failed"
            )
            raise

    def admit(self, contribution_id: str, value: Any) -> ParserAdmission:
        if (
            type(value) is not _ProposalToken
            or value.contribution_id != contribution_id
            or self._proposals.get(contribution_id) is not value
        ):
            return ParserAdmission(False, "invalid_parser_candidate")
        return ParserAdmission(True)

    def materialize(self, contribution_id: str, value: Any) -> ParsedSource:
        if (
            type(value) is not _ProposalToken
            or value.contribution_id != contribution_id
            or self._proposals.get(contribution_id) is not value
        ):
            raise TypeError("invalid parser candidate")
        candidate = self._payloads.get(contribution_id)
        if type(candidate) is not _Candidate:
            raise TypeError("missing parser candidate payload")
        self.delete_source_images()
        self.assets_pending = True
        persist_image = self.make_persist_image()
        try:
            if contribution_id == PARSER_BUILTIN_PROVIDER:
                effective_file_name = self.file_name or ""
                if (
                    self.source.kind == "url"
                    and Path(effective_file_name).suffix.lower() != ".pdf"
                ):
                    effective_file_name = f"{effective_file_name or 'source'}.pdf"
                elements = parse_builtin_source_file(
                    self.source_id,
                    str(self._local_path()),
                    effective_file_name or "source.pdf",
                    persist_image=persist_image,
                )
                if self._attempted_cloud:
                    parser_mode = "python_pdf_fallback_after_cloud_error"
                elif self.source.kind == "url" and self._attempted_self_hosted:
                    parser_mode = f"mineru_local({getattr(self.mineru_client, 'mode', '')})"
                else:
                    parser_mode = str(getattr(self.mineru_client, "mode", ""))
                warning = (
                    PARSER_FALLBACK_WARNING_CODE
                    if (
                        (self._attempted_self_hosted or self._attempted_cloud)
                        and self.source.suffix in MINERU_FALLBACK_WARNING_SUFFIXES
                    )
                    else ""
                )
            else:
                elements = mineru_content_list_to_elements(
                    self.source_id,
                    candidate.content_list,
                    label_prefix=mineru_label_prefix(self.file_name),
                    images=candidate.images,
                    persist_image=persist_image,
                )
                if not elements:
                    self._mineru_error = (
                        "MinerU content_list mapped to zero source elements"
                    )
                    raise RuntimeError("empty parser output")
                parser_mode = (
                    "mineru_cloud"
                    if contribution_id == PARSER_CLOUD_PROVIDER
                    else (
                        f"mineru_local({getattr(self.mineru_client, 'mode', '')})"
                        if self.source.kind == "url"
                        else str(getattr(self.mineru_client, "mode", ""))
                    )
                )
                warning = ""
            result = ParsedSource(
                tuple(elements), parser_mode, self._mineru_error, warning
            )
            self.materialized = True
            return result
        except BaseException:
            self.materialized = False
            try:
                self.delete_source_images()
            except BaseException:
                # Preserve the provider/cancellation failure. The ingestion
                # workflow cannot mark this generation committed, and a later
                # retry starts by deleting the same source generation again.
                pass
            else:
                self.assets_pending = False
            raise

    def mark_assets_committed(self) -> None:
        """Transfer the accepted generation to the element transaction."""

        self.assets_pending = False

    def warning(self, warning_code: str) -> None:
        return None

    def event(self, receipt: dict[str, object]) -> None:
        self.event_sink(receipt)

    def _local_path(self) -> Path:
        if self.source.kind == "file":
            return Path(self.file_path)
        if self._temp_path is None:
            fd, tmp = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            self._temp_path = Path(tmp)
            remote_sources.download_pdf(self.source_url, self._temp_path)
        return self._temp_path


__all__ = [
    "BuiltinParserChainHost",
    "PARSER_FALLBACK_WARNING_CODE",
    "ParserChainExecution",
]
