#!/usr/bin/env python3
"""Check deployment plugin imports and settings without composing the application."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查部署插件的导入与配置；不连接数据库、不启动服务。"
    )
    parser.parse_args(argv)

    # Help remains usable without backend dependencies or a valid deployment.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    try:
        from pydantic import ValidationError
        from pydantic_settings import SettingsError

        from app.core.config import Settings
        from app.extensions.discovery import (
            ExtensionDiscoveryError,
            discover_deployment_extensions,
        )
    except ImportError:
        print("检查失败：缺少后端依赖，请使用已安装后端依赖的 Python 环境。", file=sys.stderr)
        return 2

    try:
        settings = Settings()
    except (ValidationError, SettingsError, OSError, UnicodeError):
        print("检查失败：应用配置无法读取或校验，请检查环境文件和配置项。", file=sys.stderr)
        return 2

    try:
        extensions = discover_deployment_extensions(settings.extensions_config)
    except ExtensionDiscoveryError as exc:
        # Invalid ids and key names may contain arbitrary input, including paths.
        plugin = (
            exc.plugin_id
            if re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", exc.plugin_id)
            else "未识别"
        )
        detail = f"插件={plugin} 原因={exc.reason}"
        if exc.exception_type and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", exc.exception_type):
            detail += f" 异常类型={exc.exception_type}"
        print(f"检查失败：{detail}", file=sys.stderr)
        if exc.exception_type == "ModuleNotFoundError":
            print(
                "请检查插件目录是否已加入 PYTHONPATH，以及插件及其依赖是否安装在当前 Python 环境。",
                file=sys.stderr,
            )
        else:
            print("请核对部署插件配置及插件版本后重试。", file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print("检查失败：插件配置无法读取，请检查文件权限和 UTF-8 编码。", file=sys.stderr)
        return 2

    if not extensions:
        print("检查通过：没有已启用的部署插件（0 个）。")
    else:
        print(f"检查通过：{len(extensions)} 个部署插件：" + "、".join(item.plugin_id for item in extensions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
