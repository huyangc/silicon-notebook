"""MinerU adapter for high-fidelity PDF parsing (formulas, tables, layout).

MinerU is heavy and GPU-oriented, so it is kept fully decoupled from this
backend. Nothing here imports torch or MinerU at module load time:

  - "http" mode  -> POST the PDF to a remote `mineru-api` service (the GPU box)
                    and read back its `content_list`.
  - "cli"  mode  -> run the local `mineru` CLI as a subprocess and read the
                    `*_content_list.json` it writes.
  - "off"  mode  -> `configured` is False; callers fall back to pypdf.

The return value is always MinerU's `content_list` (a list of block dicts);
mapping it to `SourceElement`s lives in `parsers.py` so it stays unit-testable
without MinerU installed.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import List

from app.core.config import Settings


class MinerUClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return self.settings.mineru_enabled

    @property
    def mode(self) -> str:
        return (self.settings.mineru_mode or "off").lower()

    def parse(self, file_path: str, file_name: str) -> List[dict]:
        """Return MinerU's content_list for a PDF. Raises on failure."""
        if self.mode == "http":
            return self._parse_http(file_path, file_name)
        if self.mode == "cli":
            return self._parse_cli(file_path, file_name)
        raise RuntimeError("MinerU is not configured")

    # -- HTTP mode (remote mineru-api service) ---------------------------------

    def _parse_http(self, file_path: str, file_name: str) -> List[dict]:
        url = self.settings.mineru_api_url.rstrip("/") + "/file_parse"
        fields = {
            "backend": self.settings.mineru_backend,
            "return_content_list": "true",
            "return_md": "false",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_images": "false",
            "response_format_zip": "false",
            "formula_enable": "true" if self.settings.mineru_formula_enable else "false",
            "table_enable": "true" if self.settings.mineru_table_enable else "false",
        }
        if self.settings.mineru_lang:
            fields["lang_list"] = self.settings.mineru_lang

        content = Path(file_path).read_bytes()
        body, content_type = _encode_multipart(fields, "files", file_name, content)
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        with urllib.request.urlopen(request, timeout=self.settings.mineru_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _extract_content_list(payload)

    # -- CLI mode (local mineru subprocess) ------------------------------------

    def _parse_cli(self, file_path: str, file_name: str) -> List[dict]:
        with tempfile.TemporaryDirectory(prefix="mineru-") as out_dir:
            command = [
                self.settings.mineru_cli_bin,
                "-p",
                file_path,
                "-o",
                out_dir,
                "--backend",
                self.settings.mineru_backend,
                "--formula",
                _bool_flag(self.settings.mineru_formula_enable),
                "--table",
                _bool_flag(self.settings.mineru_table_enable),
            ]
            if self.settings.mineru_lang:
                command += ["--lang", self.settings.mineru_lang]
            # MinerU CLI reads model source from the process env, not our Settings.
            env = {**os.environ}
            if self.settings.mineru_model_source:
                env["MINERU_MODEL_SOURCE"] = self.settings.mineru_model_source
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    timeout=self.settings.mineru_timeout_seconds,
                    env=env,
                )
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
                raise RuntimeError(f"MinerU CLI failed: {stderr}") from exc
            matches = sorted(Path(out_dir).rglob("*_content_list.json"))
            if not matches:
                raise RuntimeError("MinerU CLI produced no content_list.json")
            return json.loads(matches[0].read_text(encoding="utf-8"))


def _bool_flag(value: bool) -> str:
    return "true" if value else "false"


def _extract_content_list(payload: object) -> List[dict]:
    """Pull the content_list out of mineru-api's per-file response dict."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("content_list"), list):
            return payload["content_list"]
        # mineru-api keys the result by filename: {"doc.pdf": {"content_list": [...]}}
        for value in payload.values():
            if isinstance(value, dict) and isinstance(value.get("content_list"), list):
                return value["content_list"]
    raise RuntimeError("MinerU response did not contain a content_list")


def _encode_multipart(
    fields: dict,
    file_field: str,
    file_name: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    """Minimal multipart/form-data encoder (stdlib only)."""
    boundary = f"----silicon-notebook-{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: List[bytes] = []
    for key, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode())
    mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"'.encode()
    )
    parts.append(f"Content-Type: {mime}".encode())
    parts.append(b"")
    body = crlf.join(parts) + crlf + file_bytes + crlf + f"--{boundary}--".encode() + crlf
    return body, f"multipart/form-data; boundary={boundary}"
