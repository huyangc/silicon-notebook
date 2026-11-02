"""Read-only, on-demand symbol navigation for Python and TypeScript sources."""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx"})
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".local", ".next", ".venv", "__pycache__", "node_modules",
    "dist", "build", "coverage", "venv",
})


class NavigationError(ValueError):
    """The requested source cannot be inspected reliably."""


def python_index(text: str, path: Path) -> dict:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise NavigationError(f"Python syntax error: {path}:{exc.lineno}:{exc.offset}") from None
    symbols = []
    import_sites = []

    def visit(node, parents):
        for child in ast.iter_child_nodes(node):
            scope = parents
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                import_sites.append((parents, child))
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = (*parents, child.name)
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                symbols.append({
                    "name": ".".join(scope),
                    "kind": "class" if isinstance(child, ast.ClassDef) else "function",
                    "line": start, "end": child.end_lineno, "node": child,
                })
            visit(child, scope)

    visit(tree, ())
    aliases = []
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            module = "." * statement.level + (statement.module or "")
            owner = _python_module_owner(path, statement.module or "", statement.level)
            for alias in statement.names:
                aliases.append({"name": alias.asname or alias.name, "original": alias.name,
                                "module": module, "owner": str(owner) if owner else ""})
    return {"text": text, "path": path, "tree": tree, "symbols": symbols,
            "aliases": aliases, "import_sites": import_sites}


def _python_module_owner(source: Path, module: str, level: int) -> Path | None:
    source = source.resolve()
    if level:
        base = source.parent
        for _ in range(level - 1):
            base = base.parent
        bases = [base]
    else:
        bases = source.parents
    for base in bases:
        candidate = base.joinpath(*module.split(".")) if module else base
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                return path
    return None


def select_symbol(index: dict, name: str) -> dict:
    exact = [s for s in index["symbols"] if s["name"] == name]
    matches = exact or [s for s in index["symbols"] if s["name"].split(".")[-1] == name]
    if len(matches) != 1:
        names = ", ".join(s["name"] for s in matches)
        message = f"Symbol {'ambiguous: ' + names if matches else 'not found: ' + name}"
        if not matches:
            root, _, suffix = name.partition(".")
            for alias in index.get("aliases", []):
                if alias["name"] == root:
                    owner = alias["owner"] or alias["module"]
                    symbol = ".".join(filter(None, (alias["original"], suffix)))
                    message += f"; imported/re-exported from {alias['module']}; inspect {owner}::{symbol}"
        raise NavigationError(message)
    return matches[0]


def python_symbol(index: dict, symbol: dict) -> dict:
    node = symbol["node"]
    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    imports = []
    scope = tuple(symbol["name"].split("."))
    for owner, statement in index["import_sites"]:
        bindings = {a.asname or a.name.split(".")[0] for a in statement.names}
        if scope[:len(owner)] == owner and bindings & used:
            names = [a for a in statement.names if (a.asname or a.name.split(".")[0]) in used]
            selected = ast.Import(names=names) if isinstance(statement, ast.Import) else ast.ImportFrom(
                module=statement.module, names=names, level=statement.level,
            )
            imports.append(ast.unparse(selected))
    declarations = {s["name"] for s in index["symbols"]}
    dependencies = set()
    for name in used:
        for length in range(len(scope) - 1, -1, -1):
            candidate = ".".join((*scope[:length], name))
            if candidate in declarations:
                if candidate != symbol["name"] and not candidate.startswith(symbol["name"] + "."):
                    dependencies.add(candidate)
                break
    classes = [s["name"] for s in index["symbols"] if s["kind"] == "class"
               and symbol["name"].startswith(s["name"] + ".")]
    if classes:
        owner_class = max(classes, key=len)
        for part in ast.walk(node):
            if isinstance(part, ast.Attribute) and isinstance(part.value, ast.Name) and part.value.id == "self":
                candidate = owner_class + "." + part.attr
                if candidate in declarations and candidate != symbol["name"]:
                    dependencies.add(candidate)
    return {
        **{k: v for k, v in symbol.items() if k != "node"},
        "imports": list(dict.fromkeys(imports)),
        "dependencies": sorted(dependencies),
        "source": "\n".join(index["text"].splitlines()[symbol["line"] - 1:symbol["end"]]),
    }


def _module_matches(module: str, source: Path, target: Path, level: int = 0) -> bool:
    if target.suffix != ".py":
        return False
    if level:
        base = source.parent
        for _ in range(level - 1):
            base = base.parent
        candidate = base.joinpath(*module.split(".")) if module else base
        return target in (candidate.with_suffix(".py"), candidate / "__init__.py")
    suffix = "/" + module.replace(".", "/")
    return bool(module) and (
        target.with_suffix("").as_posix().endswith(suffix)
        or target.as_posix().endswith(suffix + "/__init__.py")
    )


def python_references(index: dict, target: Path, target_name: str) -> list[dict]:
    """Resolve explicit imports and unshadowed syntactic name/attribute accesses."""
    results = []
    source = index["path"].resolve()

    def scan(scope, parents, inherited):
        statements = []

        def collect(node):
            for child in ast.iter_child_nodes(node):
                statements.append(child)
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    collect(child)

        collect(scope)
        blocked = {n.id for n in statements if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
        blocked.update(n.name for n in statements if isinstance(n, ast.ExceptHandler) and n.name)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            blocked.update(n.arg for n in ast.walk(scope.args) if isinstance(n, ast.arg))
        local_defs = [n for n in statements if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        blocked.update(n.name for n in local_defs)
        bindings = {name: value for name, value in inherited.items() if name not in blocked}
        for child in local_defs:
            if source == target:
                bindings[child.name] = ".".join((*parents, child.name))
        for node in statements:
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for alias in node.names:
                binding = alias.asname or alias.name.split(".")[0]
                resolved = None
                if isinstance(node, ast.Import):
                    if _module_matches(alias.name, source, target):
                        resolved = "" if alias.asname else "@" + alias.name
                elif _module_matches(node.module or "", source, target, node.level):
                    resolved = alias.name
                elif _module_matches(
                    ".".join(filter(None, (node.module, alias.name))), source, target, node.level
                ):
                    resolved = ""
                if resolved is not None:
                    if binding not in blocked:
                        bindings[binding] = resolved
                    if resolved in ("", target_name, target_name.split(".")[0]) or resolved.startswith("@"):
                        results.append({"line": node.lineno, "scope": ".".join(parents) or "<module>",
                                        "kind": "import", "binding": binding})
        for node in statements:
            if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(node.ctx, ast.Load):
                continue
            parts = []
            cursor = node
            while isinstance(cursor, ast.Attribute):
                parts.append(cursor.attr)
                cursor = cursor.value
            if not isinstance(cursor, ast.Name) or cursor.id not in bindings:
                continue
            parts.reverse()
            binding = bindings[cursor.id]
            if binding.startswith("@"):
                dotted = ".".join([cursor.id, *parts])
                prefix = binding[1:] + "."
                resolved = dotted.removeprefix(prefix) if dotted.startswith(prefix) else None
            else:
                resolved = ".".join(filter(None, (binding, *parts)))
            if resolved == target_name:
                results.append({"line": node.lineno, "scope": ".".join(parents) or "<module>",
                                "kind": "reference"})
        for child in local_defs:
            # Class locals are not the lexical environment of their methods.
            scan(child, (*parents, child.name), inherited if isinstance(scope, ast.ClassDef) else bindings)

    scan(index["tree"], (), {})
    return list({(r["line"], r["scope"], r["kind"]): r for r in results}.values())


def source_files(roots: list[Path]) -> list[Path]:
    found = set()
    for root in roots:
        if root.is_symlink():
            raise NavigationError(f"Symlink search root is excluded: {root}")
        root = root.resolve()
        if not root.exists():
            raise NavigationError(f"Search root not found: {root}")
        if set(root.parts) & EXCLUDED_DIRECTORIES:
            raise NavigationError(f"Excluded search root: {root}")
        pending = [root]
        while pending:
            path = pending.pop()
            if path.is_symlink():
                continue
            if path.is_dir():
                if path.name not in EXCLUDED_DIRECTORIES:
                    pending.extend(path.iterdir())
            elif path.suffix in SOURCE_SUFFIXES:
                found.add(path)
    return sorted(found)


def typescript(request: dict) -> dict:
    try:
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "code_context_ts.cjs")],
            input=json.dumps(request), capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        raise NavigationError("Node.js is required for TypeScript navigation") from None
    if result.returncode:
        raise NavigationError(result.stderr.strip() or "TypeScript inspection failed")
    return json.loads(result.stdout)


def inspect(path: Path, *, details: bool = True) -> dict:
    if path.suffix not in SOURCE_SUFFIXES:
        raise NavigationError(f"Unsupported source type: {path.suffix}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return python_index(text, path)
    return typescript({"action": "index", "path": str(path), "text": text, "details": details})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("outline", "symbol", "references"))
    parser.add_argument("file", type=Path)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--root", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        path = args.file.resolve()
        index = inspect(path, details=args.action == "symbol")
        if args.action == "outline":
            print(f"{path}: {len(index['symbols'])} symbols (ranges are diagnostic)")
            for item in index["symbols"]:
                print(f"{item['kind']} {item['name']} [{item['line']}:{item['end']}]")
            return 0
        if not args.name:
            raise NavigationError("symbol and references require a qualified symbol name")
        symbol = select_symbol(index, args.name)
        if args.action == "symbol":
            result = python_symbol(index, symbol) if path.suffix == ".py" else symbol
            print(f"{path}::{result['name']} [{result['line']}:{result['end']}]")
            print("Visible import candidates used syntactically (no type/call-graph resolution):")
            print("\n".join(result["imports"]) or "(none)")
            print("Local dependency candidates: " + (", ".join(result["dependencies"]) or "(none)"))
            print("Complete symbol source:\n" + result["source"])
            return 0
        if not args.root:
            raise NavigationError("references requires at least one explicit --root")
        files = source_files(args.root)
        confirmed = []
        candidates = []
        pattern = re.compile(r"\b" + re.escape(symbol["name"].split(".")[-1]) + r"\b")
        ts_files = [str(p) for p in files if p.suffix != ".py"]
        ts_results = typescript({"action": "references", "target": str(path), "name": symbol["name"],
                                 "files": ts_files}) if ts_files else {}
        for file in files:
            text = file.read_text(encoding="utf-8")
            refs = python_references(python_index(text, file), path, symbol["name"]) if file.suffix == ".py" else ts_results[str(file)]
            for ref in refs:
                confirmed.append({"file": str(file), **ref})
            confirmed_lines = {r["line"] for r in refs}
            for number, line in enumerate(text.splitlines(), 1):
                if number not in confirmed_lines and pattern.search(line):
                    candidates.append({"file": str(file), "line": number})
        print(f"Searched {len(files)} source files in explicit roots; symlinks excluded; excluded directories: {', '.join(sorted(EXCLUDED_DIRECTORIES))}")
        print("Static imports/direct references (lexical names only; not a full type/call graph):")
        for ref in confirmed:
            print(f"{ref['file']}:{ref['line']} {ref['kind']} {ref['scope']}")
        print("Text candidates (includes definitions, strings, comments, unresolved receivers):")
        for candidate in candidates:
            print(f"{candidate['file']}:{candidate['line']}")
        print("Unresolved: dynamic dispatch/imports, re-export chains, lambdas/comprehension scopes, and runtime module search paths.")
        return 0
    except (NavigationError, OSError, UnicodeError) as exc:
        print(f"code-context: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
