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
