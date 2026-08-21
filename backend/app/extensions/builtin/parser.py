"""Thin built-in links for the dormant parser ProviderChain topology."""
from __future__ import annotations

from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    PARSER_BUILTIN_ACCESS_CAPABILITY,
    PARSER_CLOUD_ACCESS_CAPABILITY,
    PARSER_PROVIDER_CHAIN_POINT,
    PARSER_SELF_HOSTED_ACCESS_CAPABILITY,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
    ParserExtensionContext,
    ProviderChainResult,
)


PARSER_SELF_HOSTED_CONTRIBUTION_ID = "parser.mineru_self_hosted"
PARSER_CLOUD_CONTRIBUTION_ID = "parser.mineru_cloud"
PARSER_BUILTIN_CONTRIBUTION_ID = "parser.builtin"

_SELF_HOSTED = ContributionDeclaration(
    PARSER_SELF_HOSTED_CONTRIBUTION_ID,
    PARSER_PROVIDER_CHAIN_POINT,
    ContributionKind.PROVIDER_CHAIN,
    before=(PARSER_CLOUD_CONTRIBUTION_ID,),
)
_CLOUD = ContributionDeclaration(
    PARSER_CLOUD_CONTRIBUTION_ID,
    PARSER_PROVIDER_CHAIN_POINT,
    ContributionKind.PROVIDER_CHAIN,
    after=(PARSER_SELF_HOSTED_CONTRIBUTION_ID,),
    before=(PARSER_BUILTIN_CONTRIBUTION_ID,),
)
_BUILTIN = ContributionDeclaration(
    PARSER_BUILTIN_CONTRIBUTION_ID,
    PARSER_PROVIDER_CHAIN_POINT,
    ContributionKind.PROVIDER_CHAIN,
    after=(PARSER_CLOUD_CONTRIBUTION_ID,),
)


class _DelegatingParserLink:
    """A plugin sees only the request-bound probe for its own chain link."""

    @staticmethod
    def probe(context: ParserExtensionContext) -> ProviderChainResult:
        return context.access.probe()


@dataclass(frozen=True)
class SelfHostedParserBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="parser.mineru_self_hosted",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Self-hosted MinerU parser",
        trust="builtin",
        contributions=(_SELF_HOSTED,),
        requires=(PARSER_SELF_HOSTED_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_provider_chain_link(
            ExtensionContribution(_SELF_HOSTED, _DelegatingParserLink())
        )


@dataclass(frozen=True)
class CloudParserBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="parser.mineru_cloud",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="MinerU cloud parser",
        trust="builtin",
        contributions=(_CLOUD,),
        requires=(PARSER_CLOUD_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_provider_chain_link(
            ExtensionContribution(_CLOUD, _DelegatingParserLink())
        )


@dataclass(frozen=True)
class BuiltinParserBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="parser.builtin",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Built-in parser fallback",
        trust="builtin",
        contributions=(_BUILTIN,),
        requires=(PARSER_BUILTIN_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_provider_chain_link(
            ExtensionContribution(_BUILTIN, _DelegatingParserLink())
        )


PARSER_SELF_HOSTED_BUNDLE = SelfHostedParserBundle()
PARSER_CLOUD_BUNDLE = CloudParserBundle()
PARSER_BUILTIN_BUNDLE = BuiltinParserBundle()


__all__ = [
    "PARSER_BUILTIN_BUNDLE",
    "PARSER_BUILTIN_CONTRIBUTION_ID",
    "PARSER_CLOUD_BUNDLE",
    "PARSER_CLOUD_CONTRIBUTION_ID",
    "PARSER_SELF_HOSTED_BUNDLE",
    "PARSER_SELF_HOSTED_CONTRIBUTION_ID",
]
