from typing import Literal


class BuiltinAdminDemotionError(ValueError):
    """The seeded recovery administrator must always retain admin access."""


class SelfDemotionError(ValueError):
    """An administrator cannot remove the authority of the active request."""


class PasswordMismatchError(ValueError):
    """自助改密时旧密码校验失败。"""


class BuiltinAdminPasswordError(ValueError):
    """内置管理员(user-local)的密码由部署配置派生:每次启动 seed 都会按
    settings.admin_password 重写它(见 sqlite/migrations 与 postgres/bundle 的
    seed 路径),在线改密会在下次重启被静默回滚,因此两条改密路径都显式拒绝。"""


class AgentTokenInactiveError(Exception):
    """一个已不接受访问配置变更的 Agent token:要么它自己已撤销,要么它所属的
    Agent Profile 已停用。两种状态在存储层用同一个动作(UPDATE ... WHERE
    revoked_at IS NULL,或 profile 状态判断)发现,但对用户是两句不同的中文
    文案,所以用 ``reason`` 区分,由调用方(API 路由)映射成各自的 409。"""

    def __init__(self, reason: Literal["revoked", "profile_disabled"]) -> None:
        super().__init__(reason)
        self.reason = reason
