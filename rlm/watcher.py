"""
Pre-warming file watcher.

Uses watchdog to monitor repo directories. When a file is saved, immediately
re-indexes it in the RepoIndex so context is already assembled when the user
asks their next question.

Target: context latency ~20ms instead of ~187ms (no subprocess needed —
        index is already warm).

Usage (started from rlm/main.py lifespan):
    from rlm.watcher import start_watching, stop_watching
    start_watching(repo_path)   # call when a new repo is mounted
    stop_watching(repo_path)    # call on shutdown (or repo unmount)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger("rlm.watcher")

# {repo_path: Observer}
_observers: dict[str, object] = {}

_WATCH_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"}
_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", ".mypy_cache"}


def start_watching(repo_path: str) -> bool:
    """
    Start watching a repo for file changes.
    Returns False if watchdog is not installed or already watching.
    """
    if repo_path in _observers:
        return False

    try:
        from watchdog.observers import Observer  # type: ignore[import-untyped]
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
    except ImportError:
        log.warning("watchdog not installed — pre-warming disabled. Run: pip install watchdog")
        return False

    from rlm import repo_index

    class _Handler(FileSystemEventHandler):
        def __init__(self, repo: str):
            self._repo = repo

        def on_modified(self, event):
            self._handle(event.src_path)

        def on_created(self, event):
            self._handle(event.src_path)

        def _handle(self, path: str):
            p = Path(path)
            if p.suffix not in _WATCH_EXTENSIONS:
                return
            # Skip files inside ignored directories
            for part in p.parts:
                if part in _SKIP_DIRS:
                    return
            idx = repo_index._indexes.get(self._repo)
            if idx:
                changed = idx.refresh_file(path)
                if changed:
                    log.debug("Pre-warmed: %s", p.name)

    observer = Observer()
    observer.schedule(_Handler(repo_path), repo_path, recursive=True)
    observer.daemon = True
    observer.start()
    _observers[repo_path] = observer
    log.info("Watching for changes: %s", repo_path)
    return True


def stop_watching(repo_path: str) -> None:
    observer = _observers.pop(repo_path, None)
    if observer:
        observer.stop()
        observer.join(timeout=2.0)
        log.info("Stopped watching: %s", repo_path)


def stop_all() -> None:
    for repo_path in list(_observers.keys()):
        stop_watching(repo_path)
