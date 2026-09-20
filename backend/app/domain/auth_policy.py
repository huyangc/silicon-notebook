"""Authentication sunset policy values and safe rejection codes."""
AUTH_MODES = ("local", "dual", "binding_required", "sso_only", "retired")
AUTH_CUTOVER_MIN_ADMINS = 2
AUTH_INVENTORY_PAGE_SIZE = 100
AUTH_INVENTORY_PAGE_MAX = 200


class AuthStoreError(ValueError):
    """A stable content-free authentication rejection code."""
