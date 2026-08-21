"""Startup-frozen catalog host for trusted Agent tool provider bundles."""
from __future__ import annotations

import inspect
import json
import keyword
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from app.domain.agent_tools import (
    AGENT_TOOL_SCOPE_POLICIES,
    AgentExecutionContext,
    AgentToolAccessPolicy,
    AgentToolParameter,
    AgentToolValueKind,
    PublishedAgentTool,
    validate_agent_tool_arguments,
)
from app.extension_sdk import (
    AGENT_TOOL_DESCRIPTION_MAX_CHARS,
    AGENT_TOOL_NAME_MAX_CHARS,
    AGENT_TOOL_PARAMETERS_MAX,
    AGENT_TOOL_PROVIDER_POINT,
    AGENT_TOOL_RESULT_MAX_BYTES,
    AGENT_TOOL_RESULT_MAX_DEPTH,
    AgentToolAvailabilityContext,
    AgentToolDescriptor,
    AvailabilityStatus,
    ContributionKind,
)
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")
_PARAMETER_NAME = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED_PARAMETER_NAMES = frozenset({"ctx"})


@dataclass(frozen=True, slots=True)
class _FrozenAgentTool:
    public: PublishedAgentTool
    invoke: Callable[[str, AgentExecutionContext, Mapping[str, object]], object]


class AgentToolProviderHost:
    """Validate provider descriptors once and expose immutable public tools."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        trusted_plugin_ids: frozenset[str] = frozenset(),
    ) -> None:
        if type(trusted_plugin_ids) is not frozenset or any(
            type(item) is not str for item in trusted_plugin_ids
        ):
            raise ExtensionRegistryError("invalid trusted Agent tool plugin set")
        self._registry = registry
        frozen: list[_FrozenAgentTool] = []
        names: set[str] = set()
        for registered in registry.contributions(AGENT_TOOL_PROVIDER_POINT):
            declaration = registered.contribution.declaration
            manifest = next(
                item for item in registry.manifests() if item.id == registered.plugin_id
            )
            if declaration.kind is not ContributionKind.CONTRIBUTOR:
                raise ExtensionRegistryError(
                    "agent tool providers must use contributor contributions"
                )
            if (
                registered.plugin_id not in trusted_plugin_ids
                or type(manifest.trust) is not str
                or manifest.trust != "builtin"
            ):
                raise ExtensionRegistryError(
                    "agent tool providers must be explicitly trusted built-ins"
                )
            implementation = registered.contribution.implementation
            try:
                tools_method = getattr(implementation, "tools", None)
                invoke_method = getattr(implementation, "invoke", None)
            except Exception as exc:
                raise ExtensionRegistryError(
                    "invalid agent tool provider implementation"
                ) from exc
            if (
                not callable(tools_method)
                or not callable(invoke_method)
                or inspect.iscoroutinefunction(tools_method)
                or inspect.iscoroutinefunction(invoke_method)
            ):
                raise ExtensionRegistryError(
                    "invalid agent tool provider implementation"
                )
            try:
                descriptors = tools_method()
            except Exception as exc:
                raise ExtensionRegistryError(
                    "agent tool provider discovery failed"
                ) from exc
            if type(descriptors) is not tuple or not descriptors:
                raise ExtensionRegistryError("agent tool provider must declare tools")
            for descriptor in descriptors:
                public = self._validate_descriptor(
                    descriptor,
                    plugin_id=registered.plugin_id,
                    contribution_id=declaration.id,
                )
                if public.name in names:
                    raise ExtensionRegistryError(
                        f"duplicate agent tool name {public.name!r}"
                    )
                names.add(public.name)
                frozen.append(_FrozenAgentTool(public, invoke_method))
        self._tools = tuple(frozen)
        self._by_identity = {id(item.public): item for item in self._tools}

    @property
    def public_tools(self) -> tuple[PublishedAgentTool, ...]:
        return tuple(item.public for item in self._tools)

    def available(self, tool: PublishedAgentTool) -> bool:
        frozen = self._by_identity.get(id(tool))
        if frozen is None or frozen.public is not tool:
            return False
        context = AgentToolAvailabilityContext(
            tool.plugin_id, tool.contribution_id, tool.name
        )
        result = self._registry.availability(tool.contribution_id, context)
        return result.status is AvailabilityStatus.AVAILABLE

    def invoke(
        self,
        tool: PublishedAgentTool,
        context: AgentExecutionContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        frozen = self._by_identity.get(id(tool))
        if frozen is None or frozen.public is not tool:
            raise RuntimeError("invalid_agent_tool")
        if type(context) is not AgentExecutionContext:
            raise RuntimeError("invalid_agent_tool_context")
        if (
            type(context.actor_id) is not str
            or not context.actor_id
            or type(context.notebook_id) is not str
            or not context.notebook_id
            or type(context.plugin_id) is not str
            or type(context.tool_name) is not str
            or context.plugin_id != tool.plugin_id
            or context.tool_name != tool.name
        ):
            raise RuntimeError("invalid_agent_tool_context")
        if type(arguments) is not MappingProxyType:
            raise RuntimeError("invalid_agent_tool_arguments")
        try:
            validate_agent_tool_arguments(tool, arguments)
        except ValueError as exc:
            raise RuntimeError("invalid_agent_tool_arguments") from exc
        try:
            value = frozen.invoke(tool.name, context, arguments)
        except Exception:
            # A trusted provider is still an optional public-plane component.
            # Never expose its exception text, credentials, or implementation
            # details through FastMCP's error response.
            raise RuntimeError("agent_tool_failed") from None
        return _validated_result(value)

    @staticmethod
    def _validate_descriptor(
        descriptor: object,
        *,
        plugin_id: str,
        contribution_id: str,
    ) -> PublishedAgentTool:
        if type(descriptor) is not AgentToolDescriptor:
            raise ExtensionRegistryError("invalid agent tool descriptor")
        name = descriptor.name
        description = descriptor.description
        if (
            type(name) is not str
            or not name
            or len(name) > AGENT_TOOL_NAME_MAX_CHARS
            or _TOOL_NAME.fullmatch(name) is None
        ):
            raise ExtensionRegistryError("invalid agent tool name")
        if (
            type(description) is not str
            or not description
            or len(description) > AGENT_TOOL_DESCRIPTION_MAX_CHARS
            or any(ord(char) < 32 for char in description)
        ):
            raise ExtensionRegistryError("invalid agent tool description")
        try:
            description.encode("utf-8")
        except UnicodeError as exc:
            raise ExtensionRegistryError("invalid agent tool description") from exc
        if (
            type(descriptor.required_scope) is not str
            or descriptor.required_scope not in AGENT_TOOL_SCOPE_POLICIES
        ):
            raise ExtensionRegistryError("unknown agent tool scope")
        if (
            type(descriptor.access_policy) is not AgentToolAccessPolicy
            or descriptor.access_policy
            is not AGENT_TOOL_SCOPE_POLICIES[descriptor.required_scope]
        ):
            raise ExtensionRegistryError("invalid agent tool access policy")
        parameters = descriptor.parameters
        if (
            type(parameters) is not tuple
            or len(parameters) > AGENT_TOOL_PARAMETERS_MAX
        ):
            raise ExtensionRegistryError("invalid agent tool parameters")
        seen: set[str] = set()
        for parameter in parameters:
            if type(parameter) is not AgentToolParameter:
                raise ExtensionRegistryError("invalid agent tool parameter")
            if (
                type(parameter.name) is not str
                or _PARAMETER_NAME.fullmatch(parameter.name) is None
                or keyword.iskeyword(parameter.name)
                or parameter.name in _RESERVED_PARAMETER_NAMES
                or parameter.name in seen
                or type(parameter.kind) is not AgentToolValueKind
            ):
                raise ExtensionRegistryError("invalid agent tool parameter")
            seen.add(parameter.name)
        return PublishedAgentTool(
            plugin_id=plugin_id,
            contribution_id=contribution_id,
            name=name,
            description=description,
            required_scope=descriptor.required_scope,
            access_policy=descriptor.access_policy,
            parameters=parameters,
        )


def build_agent_tool_provider_host(
    registry: ExtensionRegistry,
    *,
    trusted_plugin_ids: frozenset[str] = frozenset(),
) -> AgentToolProviderHost:
    return AgentToolProviderHost(
        registry,
        trusted_plugin_ids=trusted_plugin_ids,
    )


def _validated_result(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise RuntimeError("invalid_agent_tool_result")

    def copy(item: object, depth: int, remaining: int) -> tuple[object, int]:
        if depth > AGENT_TOOL_RESULT_MAX_DEPTH:
            raise RuntimeError("invalid_agent_tool_result")
        if remaining < 1:
            raise RuntimeError("invalid_agent_tool_result")
        if item is None or type(item) in {bool, int}:
            if type(item) is int and item.bit_length() > remaining * 4:
                raise RuntimeError("invalid_agent_tool_result")
            try:
                size = len(json.dumps(item, separators=(",", ":")))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid_agent_tool_result") from exc
            if size > remaining:
                raise RuntimeError("invalid_agent_tool_result")
            return item, size
        if type(item) is str:
            if len(item) > remaining:
                raise RuntimeError("invalid_agent_tool_result")
            try:
                size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            except UnicodeError as exc:
                raise RuntimeError("invalid_agent_tool_result") from exc
            if size > remaining:
                raise RuntimeError("invalid_agent_tool_result")
            return item, size
        if type(item) is float:
            if not math.isfinite(item):
                raise RuntimeError("invalid_agent_tool_result")
            size = len(json.dumps(item))
            if size > remaining:
                raise RuntimeError("invalid_agent_tool_result")
            return item, size
        if type(item) is list:
            total = 2 + max(0, len(item) - 1)
            if total > remaining:
                raise RuntimeError("invalid_agent_tool_result")
            result_list: list[object] = []
            for child in item:
                copied, size = copy(child, depth + 1, remaining - total)
                total += size
                result_list.append(copied)
            return result_list, total
        if type(item) is dict:
            total = 2 + max(0, len(item) - 1)
            if total > remaining:
                raise RuntimeError("invalid_agent_tool_result")
            result: dict[str, object] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise RuntimeError("invalid_agent_tool_result")
                if len(key) > remaining - total:
                    raise RuntimeError("invalid_agent_tool_result")
                try:
                    key_size = len(
                        json.dumps(key, ensure_ascii=False).encode("utf-8")
                    )
                except UnicodeError as exc:
                    raise RuntimeError("invalid_agent_tool_result") from exc
                total += key_size + 1
                if total > remaining:
                    raise RuntimeError("invalid_agent_tool_result")
                copied, size = copy(child, depth + 1, remaining - total)
                total += size
                result[key] = copied
            return result, total
        raise RuntimeError("invalid_agent_tool_result")

    result, measured_size = copy(value, 0, AGENT_TOOL_RESULT_MAX_BYTES)
    if type(result) is not dict:  # pragma: no cover - guarded by the root check
        raise RuntimeError("invalid_agent_tool_result")
    try:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("invalid_agent_tool_result") from exc
    if (
        measured_size != len(encoded)
        or len(encoded) > AGENT_TOOL_RESULT_MAX_BYTES
    ):
        raise RuntimeError("invalid_agent_tool_result")
    return result
