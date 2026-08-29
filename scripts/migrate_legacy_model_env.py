#!/usr/bin/env python3
"""Migrate the retired role-based model settings in ``.env``.

Dry-run is the default.  ``--apply`` creates a credential-free system model
service TOML, rewrites only the managed assignments in ``.env``, and first
backs up every file it replaces.  Secret values are never written to TOML or
printed to stdout/stderr.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Mapping, Sequence

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import Settings  # noqa: E402
from app.services.model_registry import (  # noqa: E402
    WORKLOADS,
    SystemModelServiceRegistry,
)


class MigrationError(ValueError):
    pass


ROLE_ORDER = ("general", "reasoning", "rewrite", "kg", "embedding", "rerank")
ROLE_LABELS = {
    "general": "通用模型",
    "reasoning": "推理模型",
    "rewrite": "查询改写模型",
    "kg": "知识抽取模型",
    "embedding": "向量模型",
    "rerank": "重排模型",
}
ROLE_SECRET_ENVS = {
    "general": "SYSTEM_GENERAL_API_KEY",
    "reasoning": "SYSTEM_REASONING_API_KEY",
    "rewrite": "SYSTEM_REWRITE_API_KEY",
    "kg": "SYSTEM_KG_API_KEY",
    "embedding": "SYSTEM_EMBEDDING_API_KEY",
    "rerank": "SYSTEM_RERANK_API_KEY",
}
REASONING_WORKLOADS = frozenset({
    "reasoning_agent",
    "report_outline",
    "report_section",
    "report_summary",
})
REWRITE_WORKLOADS = frozenset({
    "query_rewrite",
    "report_sufficiency",
    "knowhow_optimize",
    "knowhow_reformat",
    "knowhow_complete",
})
KG_WORKLOADS = frozenset({
    "kg_extract",
    "kg_refine",
    "kg_glean",
    "kg_merge_review",
    "kg_concept_description",
    "kg_community_summary",
    "kg_conflict_review",
})

LEGACY_ACTIVATION_KEYS = frozenset({
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_MODEL",
    "REASONING_LLM_BASE_URL",
    "REASONING_LLM_API_KEY",
    "REASONING_LLM_MODEL",
    "REWRITE_LLM_BASE_URL",
    "REWRITE_LLM_API_KEY",
    "REWRITE_LLM_MODEL",
    "KG_LLM_BASE_URL",
    "KG_LLM_API_KEY",
    "KG_LLM_MODEL",
    "EMBED_PROVIDER",
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBED_MODEL",
    "RERANK_BASE_URL",
    "RERANK_API_KEY",
    "RERANK_MODEL",
    "RERANK_API_STYLE",
})
LEGACY_CAPACITY_KEYS = frozenset({
    "KG_EXTRACT_WORKERS",
    "KG_ASK_RESERVE",
    "EMBED_CONCURRENCY",
})
NEW_MANAGED_KEYS = frozenset({
    "MODEL_SERVICES_CONFIG",
    *ROLE_SECRET_ENVS.values(),
})
MANAGED_ENV_KEYS = frozenset({
    *LEGACY_ACTIVATION_KEYS,
    *LEGACY_CAPACITY_KEYS,
    "USER_MODEL_CONFIG_POLICY",
    *NEW_MANAGED_KEYS,
})
_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*="
)
_OPENAI_RERANK_ALIASES = frozenset({
    "openai", "vllm", "cohere", "compatible"
})


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    display_name: str
    kind: str
    protocol: str
    base_url: str
    model: str
    api_key_env: str
    api_key: str
    max_concurrency: int
    roles: tuple[str, ...]

    @property
    def physical_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.kind,
            self.protocol,
            self.base_url.rstrip("/"),
            self.model,
            self.api_key,
        )


@dataclass(frozen=True)
class MigrationPlan:
    services: tuple[ServiceSpec, ...]
    bindings: Mapping[str, str]
    warnings: tuple[str, ...]
    toml_text: str

    @property
    def secrets(self) -> dict[str, str]:
        return {service.api_key_env: service.api_key for service in self.services}


def _value(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    return str(value).strip() if value is not None else ""


def _positive_int(
    values: Mapping[str, object], key: str, default: int, *, allow_zero: bool = False
) -> int:
    raw = _value(values, key)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise MigrationError(f"{key} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        relation = "non-negative" if allow_zero else "positive"
        raise MigrationError(f"{key} must be {relation}")
    return parsed


def parse_capacity_overrides(items: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        role, separator, raw_value = item.partition("=")
        role = role.strip().lower()
        if not separator or role not in ROLE_ORDER:
            raise MigrationError(
                "--max-concurrency must use ROLE=N; ROLE is one of "
                + ", ".join(ROLE_ORDER)
            )
        try:
            value = int(raw_value.strip())
        except ValueError as exc:
            raise MigrationError(
                f"--max-concurrency {role} must be a positive integer"
            ) from exc
        if value <= 0:
            raise MigrationError(
                f"--max-concurrency {role} must be a positive integer"
            )
        result[role] = value
    return result


def _candidate(
    role: str,
    *,
    kind: str,
    protocol: str,
    base_url: str,
    model: str,
    api_key: str,
    maximum: int,
) -> ServiceSpec:
    return ServiceSpec(
        id=role,
        display_name=ROLE_LABELS[role],
        kind=kind,
        protocol=protocol,
        base_url=base_url.rstrip("/"),
        model=model,
        api_key_env=ROLE_SECRET_ENVS[role],
        api_key=api_key,
        max_concurrency=maximum,
        roles=(role,),
    )


def build_migration_plan(
    values: Mapping[str, object],
    capacity_overrides: Mapping[str, int] | None = None,
) -> MigrationPlan:
    overrides = dict(capacity_overrides or {})
    warnings: list[str] = []
    kg_workers = _positive_int(values, "KG_EXTRACT_WORKERS", 16)
    ask_reserve = _positive_int(
        values, "KG_ASK_RESERVE", 64, allow_zero=True
    )
    embed_workers = _positive_int(values, "EMBED_CONCURRENCY", 8)
    inferred = {
        "general": kg_workers + ask_reserve,
        "reasoning": kg_workers + ask_reserve,
        "rewrite": kg_workers + ask_reserve,
        "kg": kg_workers,
        "embedding": embed_workers,
        "rerank": embed_workers,
    }
    capacities = {role: overrides.get(role, inferred[role]) for role in ROLE_ORDER}

    candidates: dict[str, ServiceSpec] = {}
    general_parts = (
        _value(values, "OPENAI_COMPAT_BASE_URL"),
        _value(values, "OPENAI_COMPAT_API_KEY"),
        _value(values, "OPENAI_COMPAT_MODEL"),
    )
    if all(general_parts):
        candidates["general"] = _candidate(
            "general",
            kind="chat",
            protocol="openai",
            base_url=general_parts[0],
            api_key=general_parts[1],
            model=general_parts[2],
            maximum=capacities["general"],
        )
    elif any(general_parts):
        warnings.append(
            "OPENAI_COMPAT_* was only partially configured; general chat remains unbound"
        )

    for role, prefix in (("reasoning", "REASONING_LLM"), ("kg", "KG_LLM")):
        parts = (
            _value(values, f"{prefix}_BASE_URL"),
            _value(values, f"{prefix}_API_KEY"),
            _value(values, f"{prefix}_MODEL"),
        )
        if all(parts):
            candidates[role] = _candidate(
                role,
                kind="chat",
                protocol="openai",
                base_url=parts[0],
                api_key=parts[1],
                model=parts[2],
                maximum=capacities[role],
            )
        elif any(parts):
            warnings.append(
                f"{prefix}_* was only partially configured; {role} workloads "
                "keep the legacy fallback to general"
            )

    rewrite_model = _value(values, "REWRITE_LLM_MODEL")
    rewrite_base = _value(values, "REWRITE_LLM_BASE_URL")
    rewrite_key = _value(values, "REWRITE_LLM_API_KEY")
    if rewrite_model:
        effective_base = rewrite_base or general_parts[0]
        effective_key = rewrite_key or general_parts[1]
        if effective_base and effective_key:
            candidates["rewrite"] = _candidate(
                "rewrite",
                kind="chat",
                protocol="openai",
                base_url=effective_base,
                api_key=effective_key,
                model=rewrite_model,
                maximum=capacities["rewrite"],
            )
        else:
            warnings.append(
                "REWRITE_LLM_MODEL had no complete effective endpoint/key; "
                "query rewrite remains unbound"
            )
    elif rewrite_base or rewrite_key:
        warnings.append(
            "REWRITE_LLM_BASE_URL/API_KEY had no REWRITE_LLM_MODEL and were ignored"
        )

    embed_provider = _value(values, "EMBED_PROVIDER").lower()
    embed_parts = (
        _value(values, "EMBED_BASE_URL"),
        _value(values, "EMBED_API_KEY"),
        _value(values, "EMBED_MODEL"),
    )
    if embed_provider == "dashscope" and all(embed_parts):
        candidates["embedding"] = _candidate(
            "embedding",
            kind="embedding",
            protocol="dashscope",
            base_url=embed_parts[0],
            api_key=embed_parts[1],
            model=embed_parts[2],
            maximum=capacities["embedding"],
        )
    elif embed_provider or any(embed_parts):
        warnings.append(
            "legacy embedding was not a complete EMBED_PROVIDER=dashscope "
            "configuration; embedding workloads remain unbound"
        )

    rerank_model = _value(values, "RERANK_MODEL")
    rerank_key = _value(values, "RERANK_API_KEY")
    rerank_base = (
        _value(values, "RERANK_BASE_URL")
        or "https://dashscope.aliyuncs.com/api/v1"
    )
    if rerank_model or rerank_key:
        raw_style = _value(values, "RERANK_API_STYLE").lower() or "dashscope"
        if raw_style in _OPENAI_RERANK_ALIASES:
            protocol = "openai"
        else:
            protocol = "dashscope"
            if raw_style != "dashscope":
                warnings.append(
                    f"unknown legacy RERANK_API_STYLE={raw_style!r} was treated "
                    "as dashscope, matching the retired runtime"
                )
        if rerank_model and rerank_key and rerank_base:
            candidates["rerank"] = _candidate(
                "rerank",
                kind="rerank",
                protocol=protocol,
                base_url=rerank_base,
                api_key=rerank_key,
                model=rerank_model,
                maximum=capacities["rerank"],
            )
        else:
            warnings.append(
                "RERANK_MODEL/API_KEY/BASE_URL was incomplete; rerank remains unbound"
            )

    if not candidates:
        raise MigrationError("no complete legacy model service configuration was found")

    services: list[ServiceSpec] = []
    role_targets: dict[str, str] = {}
    for role in ROLE_ORDER:
        candidate = candidates.get(role)
        if candidate is None:
            continue
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(services)
                if existing.physical_key == candidate.physical_key
            ),
            None,
        )
        if duplicate_index is None:
            services.append(candidate)
            role_targets[role] = candidate.id
            continue
        existing = services[duplicate_index]
        if (
            role in overrides
            and any(existing_role in overrides for existing_role in existing.roles)
            and existing.max_concurrency != candidate.max_concurrency
        ):
            raise MigrationError(
                "roles bound to the same physical service must use the same "
                "--max-concurrency value"
            )
        shared_maximum = min(existing.max_concurrency, candidate.max_concurrency)
        if shared_maximum != existing.max_concurrency or shared_maximum != candidate.max_concurrency:
            warnings.append(
                f"{', '.join((*existing.roles, role))} share one physical service; "
                f"using conservative max_concurrency={shared_maximum}"
            )
        services[duplicate_index] = replace(
            existing,
            max_concurrency=shared_maximum,
            roles=(*existing.roles, role),
        )
        role_targets[role] = existing.id

    general_target = role_targets.get("general")
    reasoning_target = role_targets.get("reasoning", general_target)
    rewrite_target = role_targets.get("rewrite", general_target)
    kg_target = role_targets.get("kg", general_target)
    bindings: dict[str, str] = {}
    for workload_id, workload in WORKLOADS.items():
        target: str | None
        if workload.kind == "embedding":
            target = role_targets.get("embedding")
        elif workload.kind == "rerank":
            target = role_targets.get("rerank")
        elif workload_id in REASONING_WORKLOADS:
            target = reasoning_target
        elif workload_id in REWRITE_WORKLOADS:
            target = rewrite_target
        elif workload_id in KG_WORKLOADS:
            target = kg_target
        else:
            target = general_target
        if target is not None:
            bindings[workload_id] = target

    warnings.append(
        "max_concurrency values were inferred from retired local concurrency "
        "settings; verify them against each physical service's real capacity"
    )
    toml_text = render_toml(tuple(services), bindings)
    plan = MigrationPlan(tuple(services), bindings, tuple(warnings), toml_text)
    validate_plan(plan)
    return plan


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_toml(
    services: Sequence[ServiceSpec], bindings: Mapping[str, str]
) -> str:
    lines = [
        "# Generated by scripts/migrate_legacy_model_env.py.",
        "# Secrets remain in .env; review every max_concurrency before production.",
        "",
    ]
    for service in services:
        lines.extend([
            f"[services.{service.id}]",
            f"display_name = {_toml_string(service.display_name)}",
            f"kind = {_toml_string(service.kind)}",
            f"protocol = {_toml_string(service.protocol)}",
            f"base_url = {_toml_string(service.base_url)}",
            f"model = {_toml_string(service.model)}",
            f"api_key_env = {_toml_string(service.api_key_env)}",
            f"max_concurrency = {service.max_concurrency}",
            "",
        ])
    lines.append("[bindings]")
    lines.extend(
        f"{workload_id} = {_toml_string(service_id)}"
        for workload_id, service_id in bindings.items()
    )
    return "\n".join(lines) + "\n"


def validate_plan(plan: MigrationPlan) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".toml", delete=False
    ) as handle:
        handle.write(plan.toml_text)
        temporary = Path(handle.name)
    try:
        registry = SystemModelServiceRegistry.load(
            Settings(_env_file=None, model_services_config=str(temporary)),
            plan.secrets,
        )
        if {service.id for service in registry.services()} != {
            service.id for service in plan.services
        }:
            raise MigrationError("generated service registry failed validation")
    finally:
        temporary.unlink(missing_ok=True)


def _dotenv_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise MigrationError("managed .env values must be single-line text")
    if "${" in value:
        raise MigrationError(
            "a resolved secret still contains ${...}; replace that indirection "
            "with its effective value before migration"
        )
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_migrated_env(
    original: str,
    *,
    model_config_value: str,
    plan: MigrationPlan,
) -> str:
    kept: list[str] = []
    for line in original.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if match and match.group("key") in MANAGED_ENV_KEYS:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [
        "",
        "# System-owned model services (migrated from retired role-based settings).",
        f"MODEL_SERVICES_CONFIG={_dotenv_quote(model_config_value)}",
    ]
    for service in plan.services:
        block.append(f"{service.api_key_env}={_dotenv_quote(service.api_key)}")
    return "\n".join((*kept, *block)) + "\n"


def _next_backup_path(path: Path) -> Path:
    base = path.with_name(path.name + ".pre-system-models.bak")
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = base.with_name(f"{base.name}.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
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
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _secure_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination_handle:
            with source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _is_unmodified_example(path: Path) -> bool:
    example = ROOT / "model-services.example.toml"
    return example.is_file() and path.read_bytes() == example.read_bytes()


def apply_migration(
    *,
    env_path: Path,
    output_path: Path,
    model_config_value: str,
    plan: MigrationPlan,
    force: bool,
) -> tuple[Path, Path | None]:
    if env_path.resolve() == output_path.resolve():
        raise MigrationError("--env and --output must be different files")
    if output_path.exists() and not force and not _is_unmodified_example(output_path):
        raise MigrationError(
            f"output already exists: {output_path}; pass --force to replace it"
        )
    original_bytes = env_path.read_bytes()
    try:
        original = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError("legacy .env must be UTF-8") from exc
    migrated = render_migrated_env(
        original,
        model_config_value=model_config_value,
        plan=plan,
    )
    env_backup = _next_backup_path(env_path)
    _secure_backup(env_path, env_backup)
    output_backup: Path | None = None
    if output_path.exists():
        output_backup = _next_backup_path(output_path)
        _secure_backup(output_path, output_backup)
    _atomic_write(output_path, plan.toml_text, mode=0o600)
    _atomic_write(env_path, migrated, mode=0o600)
    return env_backup, output_backup


def _model_config_value(output_path: Path) -> str:
    resolved = output_path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate retired model variables from .env to system services"
    )
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".local/model-services.toml"
    )
    parser.add_argument(
        "--max-concurrency",
        action="append",
        default=[],
        metavar="ROLE=N",
        help="override inferred capacity for general/reasoning/rewrite/kg/embedding/rerank",
    )
    parser.add_argument("--apply", action="store_true", help="write files")
    parser.add_argument(
        "--force", action="store_true", help="replace an existing output TOML"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    env_path = args.env.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    try:
        if not env_path.is_file():
            raise MigrationError(f"legacy .env does not exist: {env_path}")
        values = dotenv_values(env_path)
        overrides = parse_capacity_overrides(args.max_concurrency)
        plan = build_migration_plan(values, overrides)
        print(
            f"migration plan: {len(plan.services)} services, "
            f"{len(plan.bindings)} workload bindings"
        )
        for service in plan.services:
            roles = ",".join(service.roles)
            print(
                f"  {service.id}: kind={service.kind} roles={roles} "
                f"max_concurrency={service.max_concurrency}"
            )
        for warning in plan.warnings:
            print(f"WARNING: {warning}")
        if not args.apply:
            print("dry-run only: no files were written; rerun with --apply")
            return 0
        env_backup, output_backup = apply_migration(
            env_path=env_path,
            output_path=output_path,
            model_config_value=_model_config_value(output_path),
            plan=plan,
            force=args.force,
        )
        print(f"wrote model service config: {output_path}")
        print(f"updated environment file: {env_path}")
        print(f"backup: {env_backup}")
        if output_backup is not None:
            print(f"backup: {output_backup}")
        return 0
    except (MigrationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
