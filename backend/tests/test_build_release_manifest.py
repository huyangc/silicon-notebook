"""`scripts/build_release_manifest.py`:hermetic,每个用例在 tmp 目录里 `git init` 自建仓库,
不依赖本仓库历史。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_release_manifest.py"
_spec = importlib.util.spec_from_file_location("build_release_manifest", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["build_release_manifest"] = gen
_spec.loader.exec_module(gen)

# Strip every inherited GIT_* (GIT_DIR/GIT_INDEX_FILE from hooks or `rebase -x` would point
# the test git at the real repo), then add back only what the tests need.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    # tmp_path's parent: a plain dir under tmp_path must not discover an enclosing repo
    "GIT_CEILING_DIRECTORIES": str(Path(tempfile.gettempdir()).resolve()),
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        env=GIT_ENV, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def note_file(body: str = "", *, level: str = "feature", audience: str = "all", title: str = "标题") -> str:
    """A well-formed note file."""
    return f"---\nlevel: {level}\naudience: {audience}\ntitle: {title}\n---\n{body}"


def commit(repo: Path, message: str, files: dict[str, str | bytes | None]) -> str:
    for rel, content in files.items():
        path = repo / rel
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    return root


def run(repo: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--version", "v-test", "--out", str(out)],
        env=GIT_ENV, capture_output=True, text=True,
    )


def build(repo: Path, tmp_path: Path) -> dict:
    out = tmp_path / "out" / "release-manifest.json"
    proc = run(repo, out)
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_linear_history_orders_notes_and_excludes_readme(repo, tmp_path):
    commit(repo, "c1", {"a.txt": "1"})
    c2 = commit(repo, "c2", {"release-notes/zeta.md": note_file("  第二条说明。\n\n", level="fix", audience="admin", title="第二条"),
                             "release-notes/README.md": "guide"})
    c3 = commit(repo, "c3", {"release-notes/alpha.md": note_file("第三条"), "release-notes/skip.txt": "x",
                             "release-notes/sub/deep.md": "nested"})
    head = commit(repo, "c4", {"b.txt": "2"})

    data = build(repo, tmp_path)

    assert data["schema"] == 2
    assert data["build"] == {"version": "v-test", "sha": head, "ordinal": 4}
    assert data["notes"] == [
        {"id": "zeta", "ordinal": 2, "sha": c2, "level": "fix", "audience": "admin",
         "title": "第二条", "body": "第二条说明。"},
        {"id": "alpha", "ordinal": 3, "sha": c3, "level": "feature", "audience": "all",
         "title": "标题", "body": "第三条"},
    ]
    raw = (tmp_path / "out" / "release-manifest.json").read_text(encoding="utf-8")
    assert "第二条说明。" in raw  # ensure_ascii=False


def test_same_ordinal_sorts_by_id(repo, tmp_path):
    commit(repo, "c1", {"release-notes/b.md": note_file("B"), "release-notes/a.md": note_file("A")})
    data = build(repo, tmp_path)
    assert [n["id"] for n in data["notes"]] == ["a", "b"]
    assert {n["ordinal"] for n in data["notes"]} == {1}


def test_later_edit_keeps_original_add_ordinal(repo, tmp_path):
    added = commit(repo, "c1", {"release-notes/n.md": note_file("old")})
    commit(repo, "c2", {"x": "1"})
    commit(repo, "c3", {"release-notes/n.md": note_file("new")})
    note = build(repo, tmp_path)["notes"][0]
    assert (note["ordinal"], note["sha"], note["body"]) == (1, added, "new")


def test_delete_then_readd_uses_most_recent_add(repo, tmp_path):
    commit(repo, "c1", {"release-notes/n.md": note_file("v1")})
    commit(repo, "c2", {"release-notes/n.md": None})
    readd = commit(repo, "c3", {"release-notes/n.md": note_file("v2")})
    note = build(repo, tmp_path)["notes"][0]
    assert (note["ordinal"], note["sha"]) == (3, readd)


def test_note_merged_through_merge_commit_is_attributed_to_the_merge(repo, tmp_path):
    commit(repo, "c1", {"a": "1"})
    git(repo, "checkout", "-q", "-b", "feature")
    side = commit(repo, "f1", {"release-notes/feat.md": note_file("特性")})
    git(repo, "checkout", "-q", "master")
    commit(repo, "c2", {"b": "2"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge = git(repo, "rev-parse", "HEAD")

    data = build(repo, tmp_path)

    note = data["notes"][0]
    assert note["sha"] == merge != side
    assert note["ordinal"] == 3 == data["build"]["ordinal"]


def test_uncommitted_and_untracked_notes_are_ignored_and_body_comes_from_head(repo, tmp_path):
    commit(repo, "c1", {"release-notes/n.md": note_file("committed")})
    (repo / "release-notes" / "n.md").write_text(note_file("dirty worktree edit"), encoding="utf-8")
    (repo / "release-notes" / "untracked.md").write_text(note_file("nope"), encoding="utf-8")
    git(repo, "add", "release-notes/n.md")
    (repo / "release-notes" / "staged.md").write_text(note_file("staged only"), encoding="utf-8")
    git(repo, "add", "release-notes/staged.md")

    notes = build(repo, tmp_path)["notes"]

    assert [(n["id"], n["body"]) for n in notes] == [("n", "committed")]


def test_no_notes_directory_yields_empty_notes(repo, tmp_path):
    commit(repo, "c1", {"a": "1"})
    assert build(repo, tmp_path)["notes"] == []


def test_empty_body_is_allowed(repo, tmp_path):
    commit(repo, "c1", {"release-notes/blank.md": note_file(" \n\n")})
    assert build(repo, tmp_path)["notes"][0]["body"] == ""


_BAD_NOTES = {
    "empty-file": " \n\n",
    "no-header": "只有正文,没有文件头\n",
    "unterminated": "---\nlevel: feature\naudience: all\ntitle: t\n",
    "missing-title": "---\nlevel: feature\naudience: all\n---\n正文",
    "missing-level": "---\naudience: all\ntitle: t\n---\n正文",
    "missing-audience": "---\nlevel: fix\ntitle: t\n---\n正文",
    "bad-level": "---\nlevel: urgent\naudience: all\ntitle: t\n---\n",
    "bad-audience": "---\nlevel: fix\naudience: staff\ntitle: t\n---\n",
    "empty-title": "---\nlevel: fix\naudience: all\ntitle:\n---\n",
    "empty-quoted-title": "---\nlevel: fix\naudience: all\ntitle: \"\"\n---\n",
    "duplicate-key": "---\nlevel: fix\nlevel: fix\naudience: all\ntitle: t\n---\n",
    "unknown-key": "---\nlevel: fix\naudience: all\ntitle: t\ntags: x\n---\n",
    "no-colon": "---\nlevel fix\naudience: all\ntitle: t\n---\n",
}


@pytest.mark.parametrize("name", sorted(_BAD_NOTES))
def test_bad_note_header_fails_naming_the_file_and_writes_nothing(repo, tmp_path, name):
    commit(repo, "c1", {f"release-notes/{name}.md": _BAD_NOTES[name]})
    out = tmp_path / "out" / "release-manifest.json"
    proc = run(repo, out)
    assert proc.returncode != 0
    assert f"release-notes/{name}.md" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not out.exists()


def test_header_tolerates_crlf_bom_blank_lines_colons_and_paired_quotes(repo, tmp_path):
    text = (
        "\ufeff---\r\nlevel:  change \r\n\r\naudience: all\r\n"
        "title: \"导出: 现已支持\"\r\n---\r\n\r\n第一行\r\n第二行\r\n\r\n"
    )
    commit(repo, "c1", {"release-notes/n.md": text.encode("utf-8"), "release-notes/q.md":
                        note_file(title="'单引号'"), "release-notes/h.md": note_file("a\n---\nb", title="x")})
    notes = {n["id"]: n for n in build(repo, tmp_path)["notes"]}
    assert (notes["n"]["level"], notes["n"]["title"]) == ("change", "导出: 现已支持")
    assert notes["n"]["body"] == "第一行\r\n第二行".replace("\r\n", "\n")
    assert notes["q"]["title"] == "单引号"
    assert notes["h"]["body"] == "a\n---\nb"  # 正文里的 --- 不是文件头结束


@pytest.mark.parametrize("name", ["has space.md", "-lead.md", "中文.md"])
def test_unsafe_note_id_fails_naming_the_file(repo, tmp_path, name):
    commit(repo, "c1", {f"release-notes/{name}": note_file("text")})
    proc = run(repo, tmp_path / "m.json")
    assert proc.returncode != 0
    assert name in proc.stderr


def test_shallow_clone_fails(repo, tmp_path):
    commit(repo, "c1", {"a": "1"})
    commit(repo, "c2", {"release-notes/n.md": note_file("x")})
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
        env=GIT_ENV, capture_output=True, text=True, check=True,
    )
    out = tmp_path / "m.json"
    proc = run(shallow, out)
    assert proc.returncode != 0
    assert "浅克隆" in proc.stderr
    assert not out.exists()


def test_rename_counts_as_a_new_add_even_with_follow_and_renames_configured(repo, tmp_path):
    commit(repo, "c1", {"release-notes/old.md": note_file("内容足够长的一条说明,便于重命名检测。\n第二行。")})
    commit(repo, "c2", {"x": "1"})
    git(repo, "config", "log.follow", "true")
    git(repo, "config", "diff.renames", "true")
    git(repo, "mv", "release-notes/old.md", "release-notes/new.md")
    git(repo, "commit", "-q", "-m", "rename")
    renamed = git(repo, "rev-parse", "HEAD")

    note = build(repo, tmp_path)["notes"][0]

    assert (note["id"], note["ordinal"], note["sha"]) == ("new", 3, renamed)


def test_output_file_is_world_readable(repo, tmp_path):
    commit(repo, "c1", {"release-notes/n.md": note_file("x")})
    build(repo, tmp_path)
    mode = stat.S_IMODE((tmp_path / "out" / "release-manifest.json").stat().st_mode)
    assert mode == 0o644


def test_non_utf8_note_fails_naming_the_file(repo, tmp_path):
    commit(repo, "c1", {"release-notes/bad.md": b"\xff\xfe not utf8"})
    proc = run(repo, tmp_path / "m.json")
    assert proc.returncode != 0
    assert "release-notes/bad.md" in proc.stderr and "UTF-8" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_utf8_bom_is_stripped(repo, tmp_path):
    commit(repo, "c1", {"release-notes/n.md": b"\xef\xbb\xbf" + note_file("你好\n").encode("utf-8")})
    assert build(repo, tmp_path)["notes"][0]["body"] == "你好"


def test_inherited_git_dir_is_ignored(repo, tmp_path):
    commit(repo, "c1", {"release-notes/n.md": note_file("x")})
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "master")
    out = tmp_path / "m.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--version", "v", "--out", str(out)],
        env={**GIT_ENV, "GIT_DIR": str(other / ".git")}, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["notes"][0]["id"] == "n"


def test_repo_that_is_not_the_toplevel_is_refused(repo, tmp_path):
    commit(repo, "c1", {"a": "1"})
    sub = repo / "unpacked"
    sub.mkdir()
    proc = run(sub, tmp_path / "m.json")
    assert proc.returncode != 0
    assert "仓库根目录" in proc.stderr


def test_not_a_git_repo_fails(tmp_path):
    empty = tmp_path / "plain"
    empty.mkdir()
    proc = run(empty, tmp_path / "m.json")
    assert proc.returncode != 0


def test_pack_script_generates_manifest_and_skips_it_without_git():
    pack = (ROOT / "scripts" / "pack.sh").read_text(encoding="utf-8")
    assert 'scripts/build_release_manifest.py' in pack
    assert '"$STAGE/release-manifest.json"' in pack
    assert '"$PACK_PYTHON" "$ROOT_DIR/scripts/build_release_manifest.py"' in pack
    assert 'rev-parse --show-toplevel' in pack  # not a repo root -> loud warning + no manifest
    # generated before the slow frontend build so failures surface early
    assert pack.index("build_release_manifest.py") < pack.index("构建前端(standalone)")


def test_gitignore_covers_the_generated_manifest():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/release-manifest.json" in lines
