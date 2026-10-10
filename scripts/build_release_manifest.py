#!/usr/bin/env python3
"""Generate ``release-manifest.json`` for an offline package (called by scripts/pack.sh).

The manifest lets a running deployment tell each user which hand-written release notes
(``release-notes/<id>.md``) they have not seen yet. Versions are compared by **mainline
ordinal**: the position of the commit in ``git rev-list --first-parent HEAD`` (count = ordinal). master is merged by rebase,
so the ordinal grows monotonically along master's first-parent history.

  * build.ordinal  — ordinal of HEAD.
  * note.ordinal   — ordinal of the first-parent commit that (most recently) *added* the
                     note file. A note that arrived through a merge commit is attributed
                     to that merge commit (``git log -m --first-parent``). Editing a note
                     later does not move it; deleting and re-adding it does.
  * note.level / audience / title — from the file header (see below).
  * note.body      — everything after the header, read from ``git show HEAD:<path>``
                     (never the working tree), stripped; may be empty.

Note file format (hand-parsed, no YAML dependency)::

    ---
    level: feature        # feature | change | fix | internal
    audience: all         # all | admin
    title: 报告可以导出为 Word 文件
    ---
    optional markdown body

All three header keys are required, may not repeat, and unknown keys are rejected.
Length limits on title/body are NOT enforced here (a too-long note must never make a
production start lose the whole manifest); backend/tests/test_release_notes.py guards them.

Failure is loud and non-zero: shallow repo (ordinals would be wrong), missing/malformed
note header, unsafe note id, or any git error. Nothing is written on failure.

Only ``release-notes/*.md`` directly under the directory are notes; ``README.md`` is the
authoring guide and is excluded. Note ids (file name minus ``.md``) must match
``[A-Za-z0-9][A-Za-z0-9._-]*`` so they are safe as URL/JSON/DOM keys.

Usage: python scripts/build_release_manifest.py --repo <root> --version <VERSION> --out <path>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

NOTES_DIR = "release-notes"
README_NAME = "README.md"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SCHEMA = 2
LEVELS = ("feature", "change", "fix", "internal")
AUDIENCES = ("all", "admin")
_HEADER_KEYS = ("level", "audience", "title")


class ManifestError(Exception):
    """A condition that must fail the package build."""


# Host git config must not change results: renames count as a new add (README), and
# quoting/signature/follow settings must not alter the parsed log. `-m --first-parent
# --diff-filter=A --name-only` diffs a merge against its first parent only and emits no
# patch text; that combination is what we rely on instead of `--diff-merges=first-parent`
# (git >= 2.31; Ubuntu 20.04 ships 2.25). Verified on git 2.54; `-m` and `--first-parent`
# on `git log` predate 2.25.
_GIT_PINS = (
    "-c", "core.quotePath=false",
    "-c", "diff.renames=false",
    "-c", "log.follow=false",
    "-c", "log.showRoot=true",
    "-c", "log.showSignature=false",
)
_MARK = "@@release-manifest-commit@@"


def _git_bytes(repo: Path, *args: str) -> bytes:
    # An inherited GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE would point git elsewhere.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(
            ["git", *_GIT_PINS, "-C", str(repo), *args],
            capture_output=True, check=False, env=env,
        )
    except OSError as exc:
        raise ManifestError(f"git 无法执行: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ManifestError(f"git {' '.join(args[:3])} 失败: {detail}")
    return proc.stdout


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", "replace")


def _require_toplevel(repo: Path) -> None:
    top = _git(repo, "rev-parse", "--show-toplevel").strip()
    if os.path.realpath(top) != os.path.realpath(repo):
        raise ManifestError(f"{repo} 不是 git 仓库根目录(所在仓库根为 {top})")


def _ordinals(repo: Path) -> dict[str, int]:
    """sha -> mainline ordinal, from one first-parent listing (oldest commit = 1)."""
    shas = _git(repo, "rev-list", "--first-parent", "HEAD").split()
    return {sha: len(shas) - i for i, sha in enumerate(shas)}


def _latest_adds(repo: Path) -> dict[str, str]:
    """path -> first-parent commit that most recently added it (single git pass)."""
    out = _git(
        repo, "log", "-m", "--first-parent", "--diff-filter=A", "--name-only",
        f"--format={_MARK}%H", "--", f"{NOTES_DIR}/",
    )
    adds: dict[str, str] = {}
    sha = ""
    for line in out.splitlines():
        if line.startswith(_MARK):
            sha = line[len(_MARK):]
        elif line and sha:
            adds.setdefault(line, sha)  # newest commit is listed first
    return adds


def _note_paths(repo: Path) -> list[str]:
    raw = _git(repo, "ls-tree", "-z", "HEAD", f"{NOTES_DIR}/")
    paths: list[str] = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if meta.split()[1] != "blob" or not path.endswith(".md"):
            continue
        if path == f"{NOTES_DIR}/{README_NAME}":
            continue
        paths.append(path)
    return paths


def parse_note_text(text: str, path: str) -> dict:
    """Split a note file into ``{level, audience, title, body}``; raise ``ManifestError``."""
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    if lines[0] != "---":
        raise ManifestError(f"{path}: 说明缺少文件头(文件须以 --- 开头)")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ManifestError(f"{path}: 说明文件头没有以单独一行 --- 结束") from None
    header: dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        key, sep, value = raw.partition(":")
        key, value = key.strip(), value.strip()
        if not sep:
            raise ManifestError(f"{path}: 文件头行格式应为 key: value")
        if key not in _HEADER_KEYS:
            raise ManifestError(f"{path}: 文件头出现未知键 {key!r}")
        if key in header:
            raise ManifestError(f"{path}: 文件头键 {key} 重复")
        header[key] = value
    for key in _HEADER_KEYS:
        if key not in header:
            raise ManifestError(f"{path}: 文件头缺少必填键 {key}")
    if header["level"] not in LEVELS:
        raise ManifestError(f"{path}: level 只能是 {' | '.join(LEVELS)}")
    if header["audience"] not in AUDIENCES:
        raise ManifestError(f"{path}: audience 只能是 {' | '.join(AUDIENCES)}")
    title = header["title"]
    if len(title) >= 2 and title[0] == title[-1] and title[0] in "\"'":
        title = title[1:-1].strip()
    if not title:
        raise ManifestError(f"{path}: title 不能为空")
    body = "\n".join(lines[end + 1 :]).strip()
    return {"level": header["level"], "audience": header["audience"], "title": title, "body": body}


def _note(repo: Path, path: str, adds: dict[str, str], ordinals: dict[str, int]) -> dict:
    note_id = path[len(NOTES_DIR) + 1 : -len(".md")]
    if not SAFE_ID.match(note_id):
        raise ManifestError(f"{path}: 说明文件名不合规(只允许字母数字与 . _ -,且以字母数字开头)")
    try:
        text = _git_bytes(repo, "show", f"HEAD:{path}").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{path}: 说明不是合法的 UTF-8 文本") from exc
    parsed = parse_note_text(text, path)
    sha = adds.get(path)
    if sha is None or sha not in ordinals:
        raise ManifestError(f"{path}: 在主线历史里找不到引入它的提交")
    return {"id": note_id, "ordinal": ordinals[sha], "sha": sha, **parsed}


def build_manifest(repo: Path, version: str) -> dict:
    _require_toplevel(repo)
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise ManifestError("仓库是浅克隆:主线序号需要完整 git 历史(请 git fetch --unshallow)")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    ordinals = _ordinals(repo)
    paths = _note_paths(repo)
    adds = _latest_adds(repo) if paths else {}
    notes = [_note(repo, path, adds, ordinals) for path in paths]
    notes.sort(key=lambda n: (n["ordinal"], n["id"]))
    return {
        "schema": SCHEMA,
        "build": {"version": version, "sha": sha, "ordinal": ordinals[sha]},
        "notes": notes,
    }


def write_atomic(out: Path, manifest: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".release-manifest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o644)  # mkstemp is 0600; the backend may run as another user
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(args.repo, args.version)
        write_atomic(args.out, manifest)
    except ManifestError as exc:
        print(f"build_release_manifest: 失败 — {exc}", file=sys.stderr)
        return 1
    print(
        f"build_release_manifest: {len(manifest['notes'])} 条说明, "
        f"build ordinal {manifest['build']['ordinal']} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
