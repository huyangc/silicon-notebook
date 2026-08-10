"""单元测试：`scripts/embed_md_images.py`（部署侧独立脚本，不属于 app 包）。

按文件路径直接 import（同 test_mineru_probe.py）。纯标准库脚本，不触网。
"""
from __future__ import annotations

import base64
import importlib.util
import pathlib
import sys

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "embed_md_images.py"
)
_spec = importlib.util.spec_from_file_location("embed_md_images", _SCRIPT_PATH)
embed_md_images = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["embed_md_images"] = embed_md_images
_spec.loader.exec_module(embed_md_images)

# 1x1 png 像素，最小合法 png 字节
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_embed_relative_png(tmp_path):
    """相对路径 png 成功内嵌，data URI 可解码回原字节。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md = tmp_path / "notes.md"
    md.write_text("![alt](img.png)\n", encoding="utf-8")

    text, stats = embed_md_images.embed_images(
        md.read_text(encoding="utf-8"), tmp_path, 5 * 1024 * 1024
    )

    assert stats.embedded == 1
    assert stats.total_skipped() == 0
    assert "data:image/png;base64," in text
    b64 = text.split("data:image/png;base64,", 1)[1].split(")", 1)[0]
    assert base64.b64decode(b64) == _PNG_BYTES


def test_url_and_data_uri_kept_as_is(tmp_path):
    """http URL 与既有 data URI 保持原样，不计入内嵌或跳过。"""
    md_text = (
        "![remote](https://example.com/a.png)\n"
        "![already](data:image/png;base64,QUJD)\n"
    )
    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert text == md_text
    assert stats.embedded == 0
    assert stats.skipped["url"] == 1
    assert stats.skipped["already_data"] == 1


def test_missing_too_large_unsupported_mime(tmp_path):
    """缺失文件、超限文件、不支持扩展名均原样保留，摘要计数按原因分类。"""
    (tmp_path / "big.png").write_bytes(_PNG_BYTES)
    (tmp_path / "note.txt").write_text("not an image", encoding="utf-8")
    md_text = (
        "![missing](gone.png)\n"
        "![big](big.png)\n"
        "![text](note.txt)\n"
    )

    text, stats = embed_md_images.embed_images(md_text, tmp_path, max_bytes=1)

    assert stats.embedded == 0
    assert stats.skipped["missing"] == 1
    assert stats.skipped["too_large"] == 1
    assert stats.skipped["unsupported_mime"] == 1
    assert "![missing](gone.png)" in text
    assert "![big](big.png)" in text
    assert "![text](note.txt)" in text


def test_fenced_code_block_not_rewritten(tmp_path):
    """fenced code block 内的图片语法字面量不被改写。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md_text = (
        "before\n"
        "```\n"
        "![x](img.png)\n"
        "```\n"
        "![y](img.png)\n"
    )

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 1
    assert "```\n![x](img.png)\n```" in text
    assert "![y](data:image/png;base64," in text


def test_indented_code_backticks_do_not_open_fence(tmp_path):
    """codex R1 P2: 缩进 ≥4 空格的 ``` 是缩进代码块内容不是围栏开启符
    (CommonMark 围栏最多 3 个前导空格)——误判会吞掉其后的真实图片引用。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md_text = (
        "before\n"
        "\n"
        "    ```\n"
        "\n"
        "![y](img.png)\n"
    )

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 1
    assert "![y](data:image/png;base64," in text
    assert "    ```\n" in text


def test_info_string_line_inside_fence_does_not_close_it(tmp_path):
    """codex R2 P2: 围栏内的 ```python 字面量带 info 文本, 不是合法 CommonMark
    闭合符——不能让扫描器提前退出围栏态去改写围栏内的代码示例。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    # 同长度围栏:开启 ``` 后一行 ```python 与开启符等长但带 info 文本,
    # 只有「无 info 才算闭合」这半条守卫能挡住它(长度守卫另测)。
    md_text = (
        "```\n"
        "```python\n"
        "![x](img.png)\n"
        "```\n"
        "![y](img.png)\n"
    )

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 1
    assert "![x](img.png)" in text
    assert "![y](data:image/png;base64," in text


def test_shorter_fence_line_does_not_close_longer_opener(tmp_path):
    """CommonMark: 闭合围栏长度不得短于开启围栏。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md_text = (
        "````\n"
        "```\n"
        "![x](img.png)\n"
        "````\n"
    )

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 0
    assert "![x](img.png)" in text


def test_blockquote_fenced_code_not_rewritten(tmp_path):
    """codex R5 P2: blockquote 内的 fenced 代码示例(行带 `>` 前缀)同样受
    「围栏内不改写」保护; 引用块外的图片照常内嵌。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md_text = (
        "> ```\n"
        "> ![sample](img.png)\n"
        "> ```\n"
        "\n"
        "![y](img.png)\n"
    )

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 1
    assert "> ![sample](img.png)" in text
    assert "![y](data:image/png;base64," in text


def test_url_encoded_path_resolved(tmp_path):
    """codex R4 P2: `my%20plot.png` 这类 URL 转义路径解码后解析本地文件;
    字面量文件存在时仍取字面量。"""
    (tmp_path / "my plot.png").write_bytes(_PNG_BYTES)
    md_text = "![plot](my%20plot.png)\n"

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 1
    assert "data:image/png;base64," in text

    # 字面量优先:真有名为 my%20plot.png 的文件时不解码。
    (tmp_path / "my%20plot.png").write_bytes(_PNG_BYTES)
    text2, stats2 = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)
    assert stats2.embedded == 1


def test_title_form_embedded(tmp_path):
    """`![alt](img.png "标题")` 形态可被内嵌。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md_text = '![alt](img.png "标题")\n'

    text, stats = embed_md_images.embed_images(md_text, tmp_path, 5 * 1024 * 1024)

    assert stats.embedded == 1
    assert "data:image/png;base64," in text
    b64 = text.split("data:image/png;base64,", 1)[1].split(")", 1)[0]
    assert base64.b64decode(b64) == _PNG_BYTES


def test_cli_default_output_and_force(tmp_path, capsys):
    """默认输出路径与 --force 覆盖行为；输出已存在且无 --force 时非零退出。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md = tmp_path / "notes.md"
    md.write_text("![alt](img.png)\n", encoding="utf-8")

    rc = embed_md_images.main([str(md)])
    assert rc == 0
    default_out = tmp_path / "notes.embedded.md"
    assert default_out.is_file()
    assert "data:image/png;base64," in default_out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "内嵌 1 张" in captured.out

    # 无 --force 再次运行应拒绝覆盖，非零退出
    rc2 = embed_md_images.main([str(md)])
    assert rc2 != 0

    # 加 --force 允许覆盖
    rc3 = embed_md_images.main([str(md), "--force"])
    assert rc3 == 0


def test_cli_missing_input_nonzero_exit(tmp_path):
    rc = embed_md_images.main([str(tmp_path / "does-not-exist.md")])
    assert rc != 0


def test_cli_custom_output_path(tmp_path):
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md = tmp_path / "notes.md"
    md.write_text("![alt](img.png)\n", encoding="utf-8")
    out = tmp_path / "custom-out.md"

    rc = embed_md_images.main([str(md), "-o", str(out)])
    assert rc == 0
    assert out.is_file()


def test_cli_summary_includes_output_size(tmp_path, capsys):
    """摘要行报告输出文件体积（大输出触发 stderr 提示的场景不易在单测中构造
    50MB+ 数据，故此处只覆盖体积一定会出现在摘要里这一部分）。"""
    (tmp_path / "img.png").write_bytes(_PNG_BYTES)
    md = tmp_path / "notes.md"
    md.write_text("![alt](img.png)\n", encoding="utf-8")

    rc = embed_md_images.main([str(md)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "B）" in captured.out or "MB）" in captured.out


def test_format_bytes_helper():
    assert embed_md_images._format_bytes(500) == "500 B"
    assert embed_md_images._format_bytes(2 * 1024 * 1024) == "2.00 MB"


def test_all_images_skipped_still_exit_zero(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("![remote](https://example.com/a.png)\n", encoding="utf-8")

    rc = embed_md_images.main([str(md)])
    assert rc == 0
    out = tmp_path / "notes.embedded.md"
    assert out.is_file()
