from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.config import Settings


ParserExecution = Literal["local", "private_service", "public_cloud"]
ParserCapability = Literal[
    "structured_text",
    "headings",
    "layout",
    "tables",
    "formulas",
    "images",
    "ocr",
]


@dataclass(frozen=True)
class ParserEngineDefinition:
    """Stable parser capability declaration shared by routing and the UI receipt.

    Availability is deployment-specific and is computed separately below.  The
    registry deliberately contains no credentials, endpoints, paths, or free-form
    exception text because its sanitized projection is returned to every signed-in
    browser.
    """

    id: str
    file_extensions: tuple[str, ...]
    capabilities: tuple[ParserCapability, ...]
    supports_url: bool = False
    fallback: bool = False


PARSER_ENGINES: tuple[ParserEngineDefinition, ...] = (
    ParserEngineDefinition(
        id="mineru_self_hosted",
        file_extensions=("pdf", "docx", "pptx", "xlsx", "xlsm"),
        capabilities=(
            "structured_text",
            "headings",
            "layout",
            "tables",
            "formulas",
            "images",
            "ocr",
        ),
        supports_url=True,
    ),
    ParserEngineDefinition(
        id="mineru_cloud",
        file_extensions=("pdf", "docx", "pptx", "xlsx", "xlsm"),
        capabilities=(
            "structured_text",
            "headings",
            "layout",
            "tables",
            "formulas",
            "images",
            "ocr",
        ),
        supports_url=True,
    ),
    ParserEngineDefinition(
        id="builtin",
        file_extensions=(
            "pdf",
            "md",
            "markdown",
            "zip",
            "docx",
            "pptx",
            "csv",
            "xlsx",
            "xlsm",
            "xls",
        ),
        capabilities=("structured_text", "headings", "layout", "tables"),
        fallback=True,
    ),
)

_ENGINE_BY_ID = {engine.id: engine for engine in PARSER_ENGINES}

# The built-in parser is the minimum guaranteed ingestion surface.  Upload
# validation derives from it so enabling or disabling an optional engine never
# makes an already accepted source format disappear.
SUPPORTED_SOURCE_EXTENSIONS: tuple[str, ...] = _ENGINE_BY_ID[
    "builtin"
].file_extensions
SUPPORTED_SOURCE_SUFFIXES = frozenset(f".{ext}" for ext in SUPPORTED_SOURCE_EXTENSIONS)


def source_extension(file_name: str) -> str:
    return Path(file_name).suffix.lower().lstrip(".")


def engine_supports_file(engine_id: str, file_name: str) -> bool:
    engine = _ENGINE_BY_ID[engine_id]
    return source_extension(file_name) in engine.file_extensions


def builtin_parser_id(file_name: str) -> str:
    """Resolve the built-in parser handler for a validated source filename."""

    extension = source_extension(file_name)
    if extension in {"md", "markdown"}:
        return "markdown"
    if extension == "zip":
        return "markdown_bundle"
    if extension in {"xlsx", "xlsm"}:
        return "xlsx"
    if extension == "xls":
        return "xls"
    if extension in {"pdf", "docx", "pptx", "csv"}:
        return extension
    return "plain_text"


def parser_engine_capabilities(settings: Settings) -> list[dict[str, object]]:
    """Return the sanitized, ordered automatic-parser strategy for the browser."""

    mode = (settings.mineru_mode or "off").strip().lower()
    rows: list[dict[str, object]] = []
    for priority, engine in enumerate(PARSER_ENGINES, start=1):
        if engine.id == "mineru_self_hosted":
            available = settings.mineru_enabled
            execution: ParserExecution = (
                "private_service" if mode == "http" else "local"
            )
            unavailable_reason = (
                None
                if available
                else "missing_endpoint" if mode == "http" else "disabled"
            )
        elif engine.id == "mineru_cloud":
            available = settings.mineru_cloud_enabled
            execution = "public_cloud"
            unavailable_reason = None if available else "missing_credentials"
        else:
            available = True
            execution = "local"
            unavailable_reason = None
        rows.append(
            {
                "id": engine.id,
                "priority": priority,
                "execution": execution,
                "file_extensions": list(engine.file_extensions),
                "capabilities": list(engine.capabilities),
                "supports_url": engine.supports_url,
                "fallback": engine.fallback,
                "available": available,
                "unavailable_reason": unavailable_reason,
            }
        )
    return rows
