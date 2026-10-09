"""Chinese copy for the English ``ValueError`` texts that can reach an Agent.

Shared validators (``app.core.memory_inputs``, ``app.core.json_safety``) and a
few services speak English because their other callers (pydantic request
models, logs) do. Every Agent-facing message on the MCP surface is Chinese, so
the registration wrapper translates the known shapes here at the boundary; an
unknown English ``ValueError`` is incidental and becomes ``internal`` -- an
English detail never reaches the Agent.
"""
from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), template)
    for pattern, template in (
        (r"^(\w+) must be a string$", "{0} 必须是字符串"),
        (r"^(\w+) must not be blank$", "{0} 不能为空"),
        (r"^(\w+) must be nonblank$", "{0} 不能为空"),
        (r"^(\w+) may contain at most (\d+) characters$", "{0} 最多 {1} 个字符"),
        (r"^(\w+) may contain at most (\d+) values$", "{0} 最多 {1} 项"),
        (r"^(\w+) must be a list$", "{0} 必须是列表"),
        (r"^(\w+) must be an object$", "{0} 必须是对象"),
        (r"^each evidence reference must be an object$",
         "evidence_refs 的每一项都必须是对象"),
        (r"^(\w+) serialized size may not exceed (\d+) bytes$",
         "{0} 序列化后不能超过 {1} 字节"),
        (r"^(\w+) must not contain non-finite numbers$",
         "{0} 不能包含非有限数值（NaN 或 Infinity）"),
        (r"^(\w+) must be JSON serializable$", "{0} 必须能序列化为 JSON"),
    )
)

GENERIC_INVALID = "参数不合法，请检查参数后重试"


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def chinese_value_error(message: str) -> str | None:
    """The Agent-facing Chinese text for one ``ValueError`` message, or
    ``None`` when it is neither approved copy nor a known validator shape.

    Approved copy STARTS with Chinese. A message that merely contains a CJK
    character somewhere (``int("一")``'s ``invalid literal ...: '一'``) is a
    library's own text and is not passed through."""
    text = message.strip()
    if text and has_cjk(text[0]):
        return text
    for pattern, template in _PATTERNS:
        match = pattern.match(text)
        if match:
            return template.format(*match.groups())
    return None
