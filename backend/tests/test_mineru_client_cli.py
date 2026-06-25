import json
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.services.mineru_client import MinerUClient


def _cli_client(monkeypatch):
    # 仓库约定：用 setenv + Settings() 构造（pydantic-settings 读 env，非 kwargs）。
    monkeypatch.setenv("MINERU_MODE", "cli")
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setenv("MINERU_LANG", "ch")
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "huggingface")
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "30")
    return MinerUClient(Settings())


class FakePopen:
    """假子进程：把 content_list.json 写进命令里 -o / out_dir 指向的目录。"""

    captured_cmd = []

    def __init__(self, cmd, **kwargs):
        FakePopen.captured_cmd = cmd
        out_dir = Path(cmd[cmd.index("-o") + 1]) if "-o" in cmd else _out_dir_from_pdf_cmd(cmd)
        (out_dir / "doc_content_list.json").write_text(
            json.dumps([{"type": "text", "text": "ok", "page_idx": 0}]), encoding="utf-8"
        )

    def communicate(self, timeout=None):
        return (b"", b"")

    @property
    def returncode(self):
        return 0


def _out_dir_from_pdf_cmd(cmd):
    # PDF 路径命令是 [python, script, config.json]；out_dir 写在 config 里。
    config = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
    return Path(config["out_dir"])


def test_cli_office_uses_mineru_command(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"PK\x03\x04stub")
    out = _cli_client(monkeypatch).parse(str(docx), "doc.docx")
    assert FakePopen.captured_cmd[0] == "mineru"
    assert "-p" in FakePopen.captured_cmd and "-o" in FakePopen.captured_cmd
    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]


def test_cli_pdf_still_uses_do_parse_script(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    out = _cli_client(monkeypatch).parse(str(pdf), "doc.pdf")
    assert FakePopen.captured_cmd[0] != "mineru"  # [python, run_mineru_parse.py, config.json]
    assert FakePopen.captured_cmd[1].endswith("run_mineru_parse.py")
    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]
