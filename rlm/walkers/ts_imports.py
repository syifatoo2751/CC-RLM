"""
TypeScript / JavaScript import graph walker.

Handles .ts, .tsx, .js, .jsx files. Pure Python — no Node.js required.
Uses regex to parse import/require/export statements, then resolves relative
paths to actual files in the repo.

Runs as a subprocess. Outputs JSON to stdout.

Usage:
  python -m rlm.walkers.ts_imports --repo /path/to/repo --file src/foo.ts

Output:
  {
    "imports": ["abs/path/to/dep.ts", ...],
    "imported_by": ["abs/path/to/importer.ts", ...],
    "resolved": {"./utils": "abs/path/utils.ts", ...}
  }
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

# Patterns that capture the module specifier string
_IMPORT_PATTERNS = [
    # import ... from '...'  /  import '...'
    re.compile(r"""(?:^|\s)import\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]""", re.MULTILINE),
    # export ... from '...'
    re.compile(r"""(?:^|\s)export\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]""", re.MULTILINE),
    # require('...')  /  require("...")
    re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    # dynamic import('...')
    re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
]

_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs")


def _is_relative(spec: str) -> bool:
    return spec.startswith("./") or spec.startswith("../")


def _resolve_specifier(spec: str, source_file: Path, repo: Path) -> str | None:
    """
    Try to resolve a module specifier to an absolute file path.
    Only resolves relative imports (./foo, ../bar) — skips node_modules.
    """
    if not _is_relative(spec):
        return None  # External package — skip

    base_dir = source_file.parent
    raw = (base_dir / spec).resolve()

    # Try exact match first
    if raw.exists() and raw.is_file():
        return str(raw)

    # Try adding extensions
    for ext in _TS_EXTENSIONS:
        candidate = raw.with_suffix(ext)
        if candidate.exists():
            return str(candidate)

    # Try index file
    for ext in _TS_EXTENSIONS:
        candidate = raw / ("index" + ext)
        if candidate.exists():
            return str(candidate)

    return None


def get_imports(file_path: Path) -> dict[str, str]:
    """
    Parse a TS/JS file and return {specifier: resolved_abs_path} for all
    relative imports that could be resolved to a file in the repo.
    """
    try:
        source = file_path.read_text(errors="replace")
    except OSError:
        return {}

    repo = _find_repo_root(file_path)
    resolved: dict[str, str] = {}

    for pattern in _IMPORT_PATTERNS:
        for m in pattern.finditer(source):
            spec = m.group(1)
            if not spec:
                continue
            abs_path = _resolve_specifier(spec, file_path, repo)
            if abs_path:
                resolved[spec] = abs_path

    return resolved


def find_importers(target_file: Path, repo: Path) -> list[str]:
    """
    Walk the repo to find all TS/JS files that import target_file.
    Uses the target's basename (without extension) as a heuristic to
    limit full parsing to plausible candidates.
    """
    target_stem = target_file.stem
    importers = []
    skip_dirs = {".venv", "__pycache__", ".git", "node_modules", "dist", ".next", "build"}

    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if not any(fname.endswith(ext) for ext in _TS_EXTENSIONS):
                continue
            p = Path(root) / fname
            if p == target_file:
                continue

            # Quick heuristic: skip files that don't even mention the stem
            try:
                source = p.read_text(errors="replace")
            except OSError:
                continue
            if target_stem not in source:
                continue

            # Full parse for confirmed candidates
            resolved = get_imports(p)
            if str(target_file) in resolved.values():
                importers.append(str(p))

    return importers


def _find_repo_root(start: Path) -> Path:
    """Walk up from start looking for package.json or .git."""
    p = start if start.is_dir() else start.parent
    for _ in range(10):
        if (p / ".git").exists() or (p / "package.json").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return start.parent


def run(repo: str, file: str) -> dict:
    repo_path = Path(repo)
    file_path = Path(file) if file else None

    if not file_path or not file_path.exists():
        return {"imports": [], "imported_by": [], "resolved": {}}

    if file_path.suffix not in _TS_EXTENSIONS:
        # Not a TS/JS file — return empty so the caller falls back to py walker
        return {"imports": [], "imported_by": [], "resolved": {}, "skipped": True}

    resolved = get_imports(file_path)
    imported_by = find_importers(file_path, repo_path)

    return {
        "imports": list(resolved.values()),
        "imported_by": imported_by,
        "resolved": resolved,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file", default="")
    args = parser.parse_args()
    result = run(args.repo, args.file)
    print(json.dumps(result))
