"""Prepare the shared service/CLI environment before starting Python.

This module deliberately imports no application code.  ``exec_python`` replaces
the launcher, so Python builds its import path from the prepared environment and
the launched command retains the launcher's PID, signals, and exit status.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import NoReturn


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE_VARIABLE = "SILICON_NOTEBOOK_ENV_FILE"


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _dotenv_environment(root: Path, environ: dict[str, str]) -> dict[str, str]:
    override = environ.get(ENV_FILE_VARIABLE)
    if override is not None and not override.strip():
        environ[ENV_FILE_VARIABLE] = ""
        return {}
    env_path = Path(override.strip()) if override is not None else root / ".env"
    if not env_path.is_absolute():
        env_path = root / env_path
    env_path = env_path.resolve()
    # Settings also reads this variable.  Pass the same absolute selection to
    # prevent a command's working directory from selecting a different file.
    if override is not None:
        environ[ENV_FILE_VARIABLE] = str(env_path)
    return _read_dotenv(env_path, missing_ok=override is None)


def _read_dotenv(
    env_path: Path, *, missing_ok: bool = False, defer_file_errors: bool = False,
) -> dict[str, str]:
    try:
        with env_path.open(encoding="utf-8") as stream:
            # python-dotenv interprets dotenv syntax and ${...} references; it
            # never evaluates shell substitutions or executes shell commands.
            from dotenv import dotenv_values

            values = dotenv_values(stream=stream)
    except FileNotFoundError:
        if missing_ok or defer_file_errors:
            return {}
        _fail("无法读取指定的环境配置文件，请检查 SILICON_NOTEBOOK_ENV_FILE 和文件读取权限。")
    except (OSError, UnicodeError):
        if defer_file_errors:
            return {}
        _fail("无法读取环境配置文件，请检查文件编码和读取权限。")
    except ImportError:
        _fail("缺少环境配置依赖，请使用已安装后端依赖的 Python 解释器。")
    return {key: value for key, value in values.items() if value is not None}


def _selected_path_values(path_env_file: Path | None) -> dict[str, str]:
    if path_env_file is None:
        return {}
    if not path_env_file.is_absolute():
        raise ValueError("absolute configuration selection required")
    return _read_dotenv(path_env_file, defer_file_errors=True)


def _python_paths(root: Path, environ: dict[str, str], file_values: dict[str, str]) -> list[str]:
    paths = [str(root / "backend")]
    for source in (environ.get("PYTHONPATH", ""), file_values.get("PYTHONPATH", "")):
        for entry in source.split(os.pathsep):
            if not entry:
                continue
            path = Path(entry)
            if not path.is_absolute():
                path = root / path
            paths.append(str(path.resolve()))
    return list(dict.fromkeys(paths))


def prepare_python_path(
    *, root: Path | None = None, path_env_file: Path | None = None,
) -> None:
    """Add import paths inside a script bootstrap before application imports.

    The command owns configuration loading and selects an absolute dotenv path;
    no selection means no dotenv read. Only PYTHONPATH and sys.path change, so
    services restarted by this command retain the original configuration inputs.
    """
    try:
        launch_root = (root or ROOT).resolve()
        paths = _python_paths(launch_root, dict(os.environ), _selected_path_values(path_env_file))
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)
        sys.path[:] = list(dict.fromkeys([*paths, *sys.path]))
    except (OSError, ValueError):
        _fail("无法准备 Python 模块搜索路径，请检查环境配置和模块搜索路径。")


def build_python_environment(
    *,
    root: Path | None = None,
    path_env_file: Path | None = None,
    paths_only: bool = False,
) -> dict[str, str]:
    """Prepare a child environment without mutating the launcher process.

    Existing process variables win over dotenv values, except that PYTHONPATH
    includes both sources after the backend path. Relative search paths are
    rooted at the repository, and empty entries never add the caller's CWD.
    Python arguments and the caller's working directory are preserved.

    Commands that own configuration loading can request ``paths_only`` with an
    explicitly selected absolute ``path_env_file``. Only that file's PYTHONPATH
    is added; every other environment variable remains untouched. With no file,
    there is no dotenv fallback. File access errors are left to the command's
    own configuration validation.
    """
    try:
        launch_root = (root or ROOT).resolve()
        environ = dict(os.environ)
        if paths_only:
            file_values = _selected_path_values(path_env_file)
            merged = dict(environ)
        else:
            if path_env_file is not None:
                raise ValueError("configuration selection requires paths-only mode")
            file_values = _dotenv_environment(launch_root, environ)
            # Keep the explicitly selected file authoritative even if its contents
            # themselves contain SILICON_NOTEBOOK_ENV_FILE.
            selected_file = environ.get(ENV_FILE_VARIABLE)
            merged = {**file_values, **environ}
            if selected_file is None:
                merged.pop(ENV_FILE_VARIABLE, None)
        merged["PYTHONPATH"] = os.pathsep.join(_python_paths(launch_root, environ, file_values))
        return merged
    except (OSError, ValueError):
        _fail("无法启动 Python 命令，请检查解释器、环境配置和模块搜索路径。")


def exec_python(
    arguments: list[str],
    *,
    root: Path | None = None,
    path_env_file: Path | None = None,
    paths_only: bool = False,
) -> NoReturn:
    """Replace this process, preserving the command's PID, arguments and signals."""
    environment = build_python_environment(
        root=root, path_env_file=path_env_file, paths_only=paths_only,
    )
    try:
        os.execvpe(sys.executable, [sys.executable, *arguments], environment)
    except (OSError, ValueError):
        _fail("无法启动 Python 命令，请检查解释器、环境配置和模块搜索路径。")


if __name__ == "__main__":
    exec_python(sys.argv[1:])
