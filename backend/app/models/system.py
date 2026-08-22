from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ParserEngineCapability(BaseModel):
    """Sanitized automatic parser capability exposed to signed-in clients."""

    model_config = ConfigDict(extra="forbid")

    id: Literal["mineru_self_hosted", "mineru_cloud", "builtin"]
    priority: int = Field(gt=0)
    execution: Literal["local", "private_service", "public_cloud"]
    file_extensions: list[str]
    capabilities: list[
        Literal[
            "structured_text",
            "headings",
            "layout",
            "tables",
            "formulas",
            "images",
            "ocr",
        ]
    ]
    supports_url: bool
    fallback: bool
    available: bool
    unavailable_reason: Literal[
        "disabled", "missing_endpoint", "missing_credentials"
    ] | None = None


class SystemConfiguration(BaseModel):
    """Non-sensitive, authenticated configuration required by the web client."""

    model_config = ConfigDict(extra="forbid")

    source_upload_max_bytes: int = Field(gt=0)
    source_upload_max_files_per_batch: int = Field(gt=0)
    supported_source_extensions: list[str]
    parser_engines: list[ParserEngineCapability]
    # Browser outline editor mirrors the server validation rail so a deployment
    # override never turns into a surprising 422 after the user finishes editing.
    report_max_sections: int = Field(gt=0)
    report_max_subqueries_per_section: int = Field(gt=0)
    # /dev/logs「活动」tab 的能力位，直接反映 Settings.user_activity_view_enabled——
    # 关闭时前端不应默认进一个会全部 404 的视图（见 dev/logs/page.tsx 与 system-api.ts
    # 的消费逻辑）。
    user_activity_view_enabled: bool
    # markdown 压缩包/文件夹上传的图片内联预检护栏（直接反映
    # Settings.mineru_max_image_bytes / mineru_max_images_per_source）：前端在配对
    # 阶段拿它们提前判定「这张图会被服务端跳过」，避免只能等上传后才发现。真正的
    # 护栏仍在服务端（这里只是预检镜像），源见 md-bundle.ts 的 InlineOptions。
    # 转发的 Settings 字段本身无约束——`MINERU_MAX_IMAGES_PER_SOURCE=0` /
    # `MINERU_MAX_IMAGE_BYTES=0` 是合法部署值（等价「一张不存」），挂 `gt=0` 会让
    # 这类部署每次请求都 500。前端 `positiveIntOrNull` 既有口径本来就把「非正数」
    # 归一成「没有可用上限，不做本地预检」，接得住 0，这里不必再收窄。
    source_image_max_bytes: int
    source_image_max_per_source: int
    # 部署级图片存储总开关，直接反映 Settings.mineru_return_images——它门控所有来源
    # 类型的图片持久化，关闭时压缩包/文件夹上传里的图片内联对前端毫无意义（服务端
    # 会静默丢弃），前端据此跳过内联并提示用户。
    source_images_enabled: bool
    # Agentic Memory P1（T6）：「AI 对这个库的理解」入口的能力位，直接反映
    # Settings.agent_profile_enabled——与四个理解端点同一批上线，前端据此决定要不要
    # 渲染入口按钮（见 system-api.ts 的解析逻辑：字段缺失按 false 处理，不是 true——
    # 这个字段与端点是同批新增的，缺失只可能是旧后端，不存在「端点在但字段缺」的组合）。
    agent_profile_enabled: bool
    # Agentic Memory P3（B-Profile，T6）：「我的回答偏好」入口的能力位，直接反映
    # Settings.user_search_profile_enabled——字段本批只随 GET /system/config 下发，
    # 前端消费（system-api.ts 的解析逻辑）从 T9 起才接线：届时字段缺失按 true 处理，
    # 与 agent_profile_enabled 的「缺失按 false」相反——这里默认值本身就是开启，
    # 旧后端缺这个字段不该把入口隐藏成一个「看起来关闭了」的状态；真正的写路径
    # 仍由 PATCH /me/search-profile 的 409 兜底。
    user_search_profile_enabled: bool
