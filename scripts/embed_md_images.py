#!/usr/bin/env python3
"""把 markdown 文件里引用的本地图片内嵌成 base64 data URI, 产出自包含单文件。

用途: 本系统上传 markdown 来源时会解析 `![alt](target)` 图片语法, 但若
`target` 指向一个只存在于作者本机的相对/绝对路径, 上传后系统读不到那张图。
本脚本把这类本地图片就地内嵌成 `data:image/...;base64,...`, 产出的
`.embedded.md` 可以直接上传——系统的 markdown 摄取路径会把 data URI 图片
解码落成资产并显示, 不再依赖外部文件路径。

用法:
    python scripts/embed_md_images.py notes.md
    python scripts/embed_md_images.py notes.md -o notes.out.md
    python scripts/embed_md_images.py notes.md --max-image-mb 8 --force

参数:
    INPUT.md          待处理的 markdown 文件
    -o/--output        输出路径; 默认输入同目录 `<stem>.embedded.md`
    --max-image-mb     单张图片大小上限(MB), 默认 5——与服务端摄取默认上限
                        `MINERU_MAX_IMAGE_BYTES` 一致, 但这是脚本自身独立的
                        防呆参数; 服务端另有自己的上限, 以服务端部署配置为准
    --force            输出文件已存在时允许覆盖(默认拒绝覆盖并报错退出)

内嵌规则:
    - 只改写 markdown 图片语法 `![alt](target)` / `![alt](target "title")`
      (title 部分内嵌后丢弃, 保持实现简单)。
    - target 是本地文件(相对路径按 md 文件所在目录解析, 或绝对路径)时才
      内嵌; http(s):// URL、已是 data: 的、锚点/空 target 一律保持原样跳过。
    - 只内嵌 png/jpg/jpeg/gif/webp(与服务端摄取白名单一致的四种 MIME:
      image/png、image/jpeg、image/gif、image/webp); 其他扩展名保持原样
      跳过并在 stderr 警告。
    - 逐图 fail-open: 文件不存在、超出 --max-image-mb、MIME 不支持, 均保留
      原引用不改写, 在 stderr 打一行警告, 继续处理其余图片。
    - fenced 代码块(``` ... ``` 或 ~~~ ~~~, 逐行状态机跟踪围栏)内的字面量
      不会被改写。
    - 已知边界:
      - 4 空格缩进代码块、行内代码span(`` `...` ``)不特殊处理, 若其中恰好
        写了图片语法也会被改写——这类内容在真实 markdown 里极罕见。
      - 参考式图片语法 `![alt][ref]` 与 Obsidian 内嵌语法 `![[image.png]]`
        不识别, 原样保留、不计入跳过统计(正则本就不匹配它们)。
      - 不校验闭合围栏的长度是否与开启围栏一致(commonmark 要求闭合围栏
        长度 >= 开启围栏)——四个反引号开启、内部出现三个反引号这类极端
        构造可能被当作已闭合处理并改写, 真实 markdown 里极罕见。
      - `<target with spaces>` 这种尖括号包裹的 target 形态不支持, 保持
        原样跳过(不识别、也不计入跳过统计——正则本就不匹配它)。

退出码: 0 表示成功写出输出文件(即便全部图片都被跳过); 非 0 表示输入文件
不存在/不可读, 或输出文件已存在且未加 --force。
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path
from typing import Dict, Match, Tuple

# 与 backend/app/services/knowhow/assets.py 的图片 MIME 白名单一致, 也是
# markdown-it-py `validateLink` 唯一放行内嵌为 data URI 的四种图片 MIME
# (见 backend/app/services/structural_markdown.py 顶部注释)。
ALLOWED_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# 服务端 `SOURCE_UPLOAD_MAX_MB`(backend/app/core/config.py)的默认值。这里只是
# 一条提示阈值, 不是强制上限——本脚本是独立工具, 不读取、也无法读取运行中
# 服务端的实际部署配置; 超过时只在 stderr 提示, 不阻止写出输出文件。
_DEFAULT_SERVER_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

# 组1=alt(允许 `\]` 转义), 组2=target(不含空格/括号, 且不以 `<` 开头 ——
# `<...>` 包裹的 target 形态不支持, 交给下面的负向先行跳过不匹配),
# 组3=可选 title(丢弃)。
_IMG_RE = re.compile(
    r'!\[((?:\\.|[^\[\]])*)\]'
    r'\((?!<)([^()\s]+)(?:\s+"(?:\\.|[^"\\])*")?\)'
)

# CommonMark 允许围栏行最多 3 个前导空格；缩进 ≥4 空格的行属于缩进代码块内容,
# 不能当围栏开启符(否则缩进代码块里的 ``` 字面量会让扫描器误入围栏态, 吞掉
# 其后所有真实图片引用)。
_FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})')


class _Stats:
    def __init__(self) -> None:
        self.embedded = 0
        self.skipped: Dict[str, int] = {
            "url": 0,
            "already_data": 0,
            "anchor": 0,
            "missing": 0,
            "too_large": 0,
            "unsupported_mime": 0,
        }

    def total_skipped(self) -> int:
        return sum(self.skipped.values())


def _resolve_target_path(target: str, base_dir: Path) -> Path:
    p = Path(target)
    if p.is_absolute():
        return p
    return (base_dir / p).resolve()


def _embed_one(match: Match[str], base_dir: Path, max_bytes: int, stats: _Stats) -> str:
    alt = match.group(1)
    target = match.group(2)
    original = match.group(0)

    if target.startswith("data:"):
        stats.skipped["already_data"] += 1
        return original
    if re.match(r"^https?://", target, re.IGNORECASE):
        stats.skipped["url"] += 1
        return original
    if target == "" or target.startswith("#"):
        stats.skipped["anchor"] += 1
        return original

    path = _resolve_target_path(target, base_dir)
    mime, _enc = mimetypes.guess_type(str(path))
    if mime not in ALLOWED_MIMES:
        stats.skipped["unsupported_mime"] += 1
        print(f"警告: 跳过不支持的图片类型 (target={target!r}, mime={mime})", file=sys.stderr)
        return original

    if not path.is_file():
        stats.skipped["missing"] += 1
        print(f"警告: 跳过缺失的图片文件 (target={target!r} -> {path})", file=sys.stderr)
        return original

    try:
        size = path.stat().st_size
    except OSError as exc:
        stats.skipped["missing"] += 1
        print(f"警告: 跳过无法读取的图片文件 (target={target!r}): {exc}", file=sys.stderr)
        return original

    if size > max_bytes:
        stats.skipped["too_large"] += 1
        print(
            f"警告: 跳过超出大小上限的图片 (target={target!r}, "
            f"{size} bytes > {max_bytes} bytes)",
            file=sys.stderr,
        )
        return original

    try:
        data = path.read_bytes()
    except OSError as exc:
        stats.skipped["missing"] += 1
        print(f"警告: 跳过无法读取的图片文件 (target={target!r}): {exc}", file=sys.stderr)
        return original

    b64 = base64.b64encode(data).decode("ascii")
    stats.embedded += 1
    return f"![{alt}](data:{mime};base64,{b64})"


def embed_images(text: str, base_dir: Path, max_bytes: int) -> Tuple[str, _Stats]:
    """按行扫描 text, 跳过 fenced 代码块, 其余行内改写图片引用为 data URI。"""
    stats = _Stats()
    out_lines = []
    # None = 不在围栏内; 否则记录 (围栏字符, 开启长度)。CommonMark 的闭合围栏
    # 必须同字符、长度不短于开启围栏、且除尾随空白外不得有 info 文本——围栏内
    # 一行 ```python 字面量不是合法闭合符, 不能让扫描器提前退出围栏态(否则
    # 其后的代码示例会被当正文改写)。
    fence_open = None

    for line in text.splitlines(keepends=True):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            rest = line[fence_match.end():]
            if fence_open is None:
                fence_open = (marker[0], len(marker))
                out_lines.append(line)
                continue
            if (marker[0] == fence_open[0]
                    and len(marker) >= fence_open[1]
                    and not rest.strip()):
                fence_open = None
            out_lines.append(line)
            continue

        if fence_open is not None:
            out_lines.append(line)
            continue

        new_line = _IMG_RE.sub(
            lambda m: _embed_one(m, base_dir, max_bytes, stats), line
        )
        out_lines.append(new_line)

    return "".join(out_lines), stats


def _format_bytes(size: int) -> str:
    mb = size / (1024 * 1024)
    return f"{mb:.2f} MB" if mb >= 1 else f"{size} B"


def _format_summary(output_path: Path, stats: _Stats, output_size: int) -> str:
    reasons = "、".join(
        f"{name}={count}" for name, count in stats.skipped.items() if count
    )
    reasons_part = f"（{reasons}）" if reasons else ""
    return (
        f"内嵌 {stats.embedded} 张，跳过 {stats.total_skipped()} 张{reasons_part}，"
        f"输出: {output_path}（{_format_bytes(output_size)}）"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 markdown 文件里引用的本地图片内嵌成 base64 data URI",
    )
    parser.add_argument("input", type=Path, help="待处理的 markdown 文件")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出路径")
    parser.add_argument(
        "--max-image-mb",
        type=float,
        default=5,
        help="单张图片大小上限(MB), 默认 5（服务端另有上限, 默认 5MB）",
    )
    parser.add_argument(
        "--force", action="store_true", help="输出文件已存在时允许覆盖"
    )
    args = parser.parse_args(argv)

    input_path: Path = args.input
    if not input_path.is_file():
        print(f"错误: 输入文件不存在或不可读: {input_path}", file=sys.stderr)
        return 2

    try:
        text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"错误: 无法读取输入文件: {exc}", file=sys.stderr)
        return 2

    output_path: Path = args.output or (
        input_path.parent / f"{input_path.stem}.embedded.md"
    )
    if output_path.exists() and not args.force:
        print(
            f"错误: 输出文件已存在: {output_path}（使用 --force 覆盖）",
            file=sys.stderr,
        )
        return 2

    max_bytes = int(args.max_image_mb * 1024 * 1024)
    result_text, stats = embed_images(text, input_path.parent, max_bytes)

    try:
        output_path.write_text(result_text, encoding="utf-8")
    except OSError as exc:
        print(f"错误: 无法写入输出文件: {exc}", file=sys.stderr)
        return 2

    output_size = len(result_text.encode("utf-8"))
    if output_size > _DEFAULT_SERVER_UPLOAD_MAX_BYTES:
        print(
            f"提示: 输出文件 {_format_bytes(output_size)} 超过服务端单文件上传"
            f"上限的常见默认值 {_format_bytes(_DEFAULT_SERVER_UPLOAD_MAX_BYTES)}"
            "（SOURCE_UPLOAD_MAX_MB，实际以服务端部署配置为准），上传前请确认"
            "或改小 --max-image-mb。",
            file=sys.stderr,
        )

    print(_format_summary(output_path, stats, output_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
