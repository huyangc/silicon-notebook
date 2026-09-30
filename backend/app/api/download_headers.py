"""Download response headers shared by the file-download endpoints."""
from __future__ import annotations

import os
import re
from urllib.parse import quote

# Characters no file name may carry on the common desktop file systems, plus
# control characters. Replaced, not dropped, so word boundaries survive.
_UNSAFE_FILE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def safe_download_name(name: str, *, fallback: str, max_chars: int = 120) -> str:
    """A user-supplied title made safe as a file-name stem: path separators,
    reserved and control characters become ``_``, whitespace collapses, and
    the result is bounded. Empty after cleaning -> ``fallback``."""
    cleaned = _UNSAFE_FILE_NAME_CHARS.sub("_", str(name or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:max_chars] or fallback


def attachment_content_disposition(
    filename: str, *, fallback: str, require_letter: bool = True
) -> str:
    """RFC 5987/6266 ``Content-Disposition`` value for a (possibly non-ASCII)
    download filename: an ASCII-sanitized ``filename=`` for clients that do
    not understand the extended form, plus the real UTF-8 name via
    ``filename*=`` (what every modern browser actually uses).

    Needed because Starlette encodes header VALUES as latin-1: a raw Chinese
    title in a bare ``filename="..."`` would 500 (UnicodeEncodeError). When
    the ASCII form's stem keeps no Latin letter (an all-Chinese title leaves
    only separators and digits), the caller's ``fallback`` is used for it
    instead; with ``require_letter=False`` only an EMPTY ASCII form falls
    back (the Knowhow template download's long-standing rule)."""
    ascii_name = re.sub(
        r'[\\"/\r\n\x00-\x1f]',
        "_",
        filename.encode("ascii", "ignore").decode("ascii"),
    ).strip()
    if not ascii_name or (
        require_letter and not re.search(r"[A-Za-z]", os.path.splitext(ascii_name)[0])
    ):
        ascii_name = fallback
    # safe="" so a literal "/" is %2F-escaped too — quote's default
    # (safe="/") would leave it raw, which violates RFC 5987's attr-char
    # grammar and is inconsistent with the fallback branch above.
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'
