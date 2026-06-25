"""MinerU adapter for high-fidelity PDF parsing (formulas, tables, layout).

MinerU is heavy and GPU-oriented, so it is kept fully decoupled from this
backend. Nothing here imports torch or MinerU at module load time:

  - "http" mode  -> POST the PDF to a remote `mineru-api` service (the GPU box)
                    and read back its `content_list`.
  - "cli"  mode  -> run MinerU's Python API (`do_parse` / `read_fn`) in an
                    isolated subprocess and read the `*_content_list.json` it writes.
  - "off"  mode  -> `configured` is False; callers fall back to pypdf.

The return value is always MinerU's `content_list` (a list of block dicts);
mapping it to `SourceElement`s lives in `parsers.py` so it stays unit-testable
without MinerU installed.
"""

from __future__ import annotations

import json
import mimetypes
import os
import signal
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import List

from app.core.config import Settings


class MinerUClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return self.settings.mineru_enabled

    @property
    def mode(self) -> str:
        return (self.settings.mineru_mode or "off").lower()

    def parse(self, file_path: str, file_name: str) -> List[dict]:
        """Return MinerU's content_list for a PDF. Raises on failure."""
        self.last_error = ""
        try:
            if self.mode == "http":
                return self._parse_http(file_path, file_name)
            if self.mode == "cli":
                return self._parse_cli(file_path, file_name)
            raise RuntimeError("MinerU is not configured")
        except Exception as exc:
            self.last_error = str(exc)
            raise

    # -- HTTP mode (remote mineru-api service) ---------------------------------

    def _parse_http(self, file_path: str, file_name: str) -> List[dict]:
        url = self.settings.mineru_api_url.rstrip("/") + "/file_parse"
        fields = {
            "backend": self.settings.mineru_backend,
            "parse_method": self.settings.mineru_parse_method,
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
        # The VLM client backends need the standalone VLM server's URL.
        if self.settings.mineru_vlm_server_url:
            fields["server_url"] = self.settings.mineru_vlm_server_url

        content = Path(file_path).read_bytes()
        body, content_type = _encode_multipart(fields, "files", file_name, content)
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        with urllib.request.urlopen(request, timeout=self.settings.mineru_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _extract_content_list(payload)

    # -- CLI mode (local MinerU Python API subprocess) -------------------------

    def _parse_cli(self, file_path: str, file_name: str) -> List[dict]:
        suffix = Path(file_name).suffix.lower()
        with tempfile.TemporaryDirectory(prefix="mineru-") as out_dir:
            if suffix in {".docx", ".pptx"}:
                command = self._office_cli_command(file_path, out_dir)
            else:
                command = self._pdf_cli_command(file_path, file_name, out_dir)
            env = {**os.environ}
            if self.settings.mineru_model_source:
                env["MINERU_MODEL_SOURCE"] = self.settings.mineru_model_source
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=(os.name != "nt"),
            )
            try:
                stdout, stderr = process.communicate(
                    timeout=self.settings.mineru_timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _terminate_process(process)
                detail = _tail_process_output(stdout, stderr)
                suffix_msg = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"MinerU Python API timed out after "
                    f"{self.settings.mineru_timeout_seconds}s{suffix_msg}"
                ) from exc
            if process.returncode != 0:
                detail = _tail_process_output(stdout, stderr)
                raise RuntimeError(f"MinerU Python API failed: {detail}")
            matches = sorted(Path(out_dir).rglob("*_content_list.json"))
            if not matches:
                raise RuntimeError("MinerU Python API produced no content_list.json")
            return json.loads(matches[0].read_text(encoding="utf-8"))

    def _office_cli_command(self, file_path: str, out_dir: str) -> List[str]:
        # MinerU CLI 原生解析 office：mineru -p <file> -o <dir> -l <lang> -b <backend>
        # 注：本路径不传 formula/table/vlm_server_url；若依赖 vlm-http-client 后端，
        # 请走 http 模式（mineru-api /file_parse 会代为转发 server_url）。
        return [
            "mineru",
            "-p", str(file_path),
            "-o", str(out_dir),
            "-l", self.settings.mineru_lang or "ch",
            "-b", self.settings.mineru_backend or "pipeline",
        ]

    def _pdf_cli_command(self, file_path: str, file_name: str, out_dir: str) -> List[str]:
        config = {
            "file_path": file_path,
            "file_name": file_name,
            "out_dir": out_dir,
            "backend": self.settings.mineru_backend,
            "parse_method": self.settings.mineru_parse_method or "auto",
            "lang": self.settings.mineru_lang or "ch",
            "formula_enable": self.settings.mineru_formula_enable,
            "table_enable": self.settings.mineru_table_enable,
            "model_source": self.settings.mineru_model_source,
            "vlm_server_url": self.settings.mineru_vlm_server_url,
        }
        script_path = Path(out_dir) / "run_mineru_parse.py"
        config_path = Path(out_dir) / "mineru_config.json"
        script_path.write_text(_DO_PARSE_SCRIPT, encoding="utf-8")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return [sys.executable, str(script_path), str(config_path)]


_DO_PARSE_SCRIPT = r"""
import json
import os
import sys
from pathlib import Path



def main() -> None:
    from mineru.cli.common import do_parse, read_fn
    from mineru.utils.enum_class import MakeMode

    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    model_source = cfg.get("model_source")
    if model_source:
        os.environ["MINERU_MODEL_SOURCE"] = model_source

    pdf_path = Path(cfg["file_path"])
    pdf_name = cfg.get("file_name") or pdf_path.name
    pdf_stem = Path(pdf_name).stem or pdf_path.stem
    pdf_bytes = read_fn(pdf_path)

    do_parse(
        output_dir=cfg["out_dir"],
        pdf_file_names=[pdf_stem],
        pdf_bytes_list=[pdf_bytes],
        p_lang_list=[cfg.get("lang") or "ch"],
        backend=cfg.get("backend") or "pipeline",
        parse_method=cfg.get("parse_method") or "auto",
        formula_enable=bool(cfg.get("formula_enable", True)),
        table_enable=bool(cfg.get("table_enable", True)),
        f_draw_layout_bbox=False,
        f_draw_span_bbox=False,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=False,
        f_dump_orig_pdf=False,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        start_page_id=0,
        end_page_id=None,
        image_analysis=True,
        client_side_output_generation=False,
    )


if __name__ == "__main__":
    main()
"""


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


def _terminate_process(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        return process.communicate()


def _tail_process_output(
    stdout: bytes | None,
    stderr: bytes | None,
    limit: int = 1000,
) -> str:
    stdout_text = (stdout or b"").decode("utf-8", "replace")[-limit:].strip()
    stderr_text = (stderr or b"").decode("utf-8", "replace")[-limit:].strip()
    return "\n".join(part for part in (stderr_text, stdout_text) if part)


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
