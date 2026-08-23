"""用户名校验 + 密码哈希（纯标准库，无新依赖）。

Sunk to app.domain in B3 (pure stdlib helpers, zero app.services/
app.repositories dependency). ``app.services.auth_utils`` re-exports every
name here unchanged for existing importers.
"""
from __future__ import annotations

import hashlib
import re
import secrets

# 单个小写字母 + 字面 "00" + 6 位数字，如 a00123456。
USERNAME_RE = re.compile(r"^[a-z]00\d{6}$")

_PBKDF2_ITERATIONS = 200_000


def normalize_username(username: str) -> str:
    """去空白 + 转小写（唯一性 / 登录大小写不敏感的归一化键）。"""
    return (username or "").strip().lower()


def is_valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match((username or "").strip()))


def hash_password(
    password: str, *, salt: str | None = None, iterations: int = _PBKDF2_ITERATIONS
) -> tuple[str, str, int]:
    """返回 (hash_hex, salt_hex, iterations)。salt 缺省随机生成。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return dk.hex(), salt, iterations


def verify_password(password: str, password_hash: str, salt: str, iterations: int) -> bool:
    if not password_hash or not salt or iterations <= 0:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return secrets.compare_digest(dk.hex(), password_hash)
