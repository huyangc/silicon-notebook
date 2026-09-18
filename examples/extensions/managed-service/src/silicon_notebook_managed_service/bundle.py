"""A lifecycle-only bundle: deliberately no UI or application contributions."""

from dataclasses import dataclass

from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest


@dataclass(frozen=True)
class ManagedServiceBundle:
    manifest: ExtensionManifest

    def register(self, registrar) -> None:
        """There are no contributions and no service-start side effects."""


BUNDLE = ManagedServiceBundle(
    ExtensionManifest(
        id="examples.managed_service",
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="配套服务示例",
        trust="deployment",
        contributions=(),
        requires=(),
        provides=(),
        ui_contributions=(),
    )
)
