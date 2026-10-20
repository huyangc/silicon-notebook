"""Helpers for the hash-locked Python dependency files.

``scripts/lock_python_deps.sh`` compiles ``backend/requirements.in`` and
``backend/requirements-dev.in`` with pip-tools and stamps each generated lock
file with the fingerprint of the ``.in`` file it was compiled from;
``backend/tests/test_python_lockfiles.py`` recomputes it, so editing a ``.in``
file without relocking turns the gate red. The fingerprint covers what changes
resolution (requirement names, extras, version ranges, markers and option
lines such as ``-c``) and ignores comments, blank lines, order and spacing.

``scripts/pack.sh`` uses ``wheelhouse`` to describe the wheels it built from
the runtime lock: a wheel PyPI publishes keeps its locked hash; a wheel built
on the packaging host from a hash-verified sdist (hnswlib) cannot match a
PyPI hash, so its own hash is recorded instead. ``packaging/install.sh``
installs the offline bundle from that file with ``--require-hashes``.

Standard library only: the lock script runs it with its pip-tools venv, the
packaging host with ``PACK_PYTHON`` and the gate with the test environment.

    python scripts/python_lockfiles.py stamp <input.in> <lock.txt>
    python scripts/python_lockfiles.py wheelhouse <lock.txt> <wheelhouse dir> <out.txt>
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import sys


STAMP_PREFIX = "# input fingerprint (scripts/python_lockfiles.py): sha256:"
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*)$")
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$")
_HASH = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _canonical_line(line: str) -> str:
    if line.startswith("-"):
        return " ".join(line.split())
    match = _NAME.match(line)
    if match is None:
        raise ValueError(f"unsupported requirement line: {line!r}")
    name, extras, rest = match.groups()
    specifier, _, marker = rest.partition(";")
    extras_part = ",".join(sorted(
        canonical_name(extra.strip()) for extra in (extras or "").split(",")
        if extra.strip()
    ))
    specifier_part = ",".join(sorted(
        "".join(part.split()) for part in specifier.split(",") if part.strip()
    ))
    marker_part = " ".join(marker.split())
    return f"{canonical_name(name)}[{extras_part}]{specifier_part};{marker_part}"


def input_lines(path: Path) -> list[str]:
    """The resolution-relevant lines of a ``.in`` file, canonicalized."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.partition(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        lines.append(_canonical_line(line))
    return sorted(lines)


def input_fingerprint(path: Path) -> str:
    payload = "\n".join(input_lines(path)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stamp_line(path: Path) -> str:
    return f"{STAMP_PREFIX}{input_fingerprint(path)}"


def recorded_fingerprint(lock: Path) -> str | None:
    found = [
        line.removeprefix(STAMP_PREFIX)
        for line in lock.read_text(encoding="utf-8").splitlines()
        if line.startswith(STAMP_PREFIX)
    ]
    return found[0] if len(found) == 1 else None


def stamp(source: Path, lock: Path) -> None:
    kept = [
        line
        for line in lock.read_text(encoding="utf-8").splitlines()
        if not line.startswith(STAMP_PREFIX)
    ]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append(stamp_line(source))
    lock.write_text("\n".join(kept) + "\n", encoding="utf-8")


@dataclass
class LockEntry:
    name: str
    version: str
    hashes: set[str] = field(default_factory=set)
    via: list[str] = field(default_factory=list)


def parse_lock(path: Path) -> dict[str, LockEntry]:
    """Read a pip-compile ``--generate-hashes`` file strictly.

    Every statement must be ``name==version`` followed only by sha256 hashes;
    anything else (an index URL, an editable, an unhashed or unpinned line) is
    a lock this repository does not produce and raises ``ValueError``.
    ``via`` holds pip-compile's annotation of what requires each package.
    """
    entries: dict[str, LockEntry] = {}
    current: LockEntry | None = None
    in_via = False
    statement: list[str] = []

    def fail(message: str) -> None:
        raise ValueError(f"{path.name}: {message}")

    def flush() -> None:
        nonlocal current
        if not statement:
            return
        words = " ".join(statement).replace("\\", " ").split()
        statement.clear()
        pin = _PIN.match(words[0])
        if pin is None:
            fail(f"not an exact pin: {words[0]!r}")
        name = canonical_name(pin.group(1))
        if name in entries:
            fail(f"{name} appears twice")
        current = LockEntry(name, pin.group(2))
        for word in words[1:]:
            digest = _HASH.match(word)
            if digest is None:
                fail(f"{name}: unexpected {word!r}")
            current.hashes.add(digest.group(1))
        if not current.hashes:
            fail(f"{name} has no --hash")
        entries[name] = current

    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if raw.startswith("    #") and current is not None and not statement:
            text = stripped.lstrip("#").strip()
            if text.startswith("via"):
                in_via = True
                rest = text.removeprefix("via").strip()
                if rest:
                    current.via.append(rest)
            elif in_via and text:
                current.via.append(text)
            continue
        if not stripped or stripped.startswith("#"):
            flush()
            in_via = False
            continue
        if raw.startswith(" "):
            if not statement:
                fail(f"dangling continuation {raw!r}")
            statement.append(stripped)
        else:
            flush()
            in_via = False
            statement.append(stripped)
        if not stripped.endswith("\\"):
            flush()
    flush()
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def wheelhouse_lock(lock: Path, wheelhouse: Path) -> tuple[str, list[str]]:
    """Pin every runtime-lock package to the one wheel ``pip wheel`` left for it.

    Returns the requirements text and the names of the wheels that were built
    locally (their hash is the built file's, not a PyPI one).
    """
    wheels: dict[tuple[str, str], list[Path]] = {}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        name, version = wheel.name.split("-")[:2]
        wheels.setdefault((canonical_name(name), version), []).append(wheel)
    lines = [
        "# Generated by scripts/pack.sh from backend/requirements.txt: one hashed",
        "# pin per locked package, naming the exact wheel in this wheelhouse.",
    ]
    built: list[str] = []
    for name, entry in sorted(parse_lock(lock).items()):
        found = wheels.get((name, entry.version), [])
        if len(found) != 1:
            raise ValueError(
                f"wheelhouse has {len(found)} wheels for {name}=={entry.version}; expected one"
            )
        digest = _sha256(found[0])
        if digest not in entry.hashes:
            built.append(found[0].name)
        lines.append(f"{name}=={entry.version} \\")
        lines.append(f"    --hash=sha256:{digest}")
    return "\n".join(lines) + "\n", built


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "stamp":
        stamp(Path(argv[1]), Path(argv[2]))
        return 0
    if len(argv) == 4 and argv[0] == "wheelhouse":
        text, built = wheelhouse_lock(Path(argv[1]), Path(argv[2]))
        Path(argv[3]).write_text(text, encoding="utf-8")
        for name in built:
            print(f"built locally from its hash-verified sdist: {name}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
