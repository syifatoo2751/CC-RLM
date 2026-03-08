"""
Answer-driven relevance store.

Learns which files are actually useful per task by parsing model responses
for symbol citations. Files whose symbols appear in answers score higher
next time; files whose content was ignored score lower.

No human labels needed — the model's own answers are the ground truth.

Storage: JSON file at /tmp/cc-rlm-relevance.json (survives server restarts,
clears on machine reboot — intentional: stale scores are worse than cold start).

Score semantics:
  - Multiplier applied to base relevance score in context_pack.assemble()
  - Range: 0.5 (consistently unused) to 2.0 (consistently cited)
  - Needs at least 3 observations before multiplier diverges from 1.0
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from rlm import store as sqlite_store

log = logging.getLogger("rlm.relevance_store")

MIN_OBSERVATIONS = 3  # don't skew until we have enough data

# In-memory cache: {repo_path: {file_path: {hits: int, total: int}}}
# Populated lazily from SQLite on first access per repo.
_store: dict[str, dict[str, dict]] = {}


def _ensure_loaded(repo_path: str) -> None:
    """Load relevance data from SQLite if not already in memory."""
    if repo_path not in _store:
        _store[repo_path] = sqlite_store.load_relevance(repo_path)


def get_multiplier(repo_path: str, file_path: str) -> float:
    """
    Return a score multiplier (0.5–2.0) based on observed citation rate.
    Returns 1.0 (neutral) if not enough data.
    """
    _ensure_loaded(repo_path)
    entry = _store.get(repo_path, {}).get(file_path, {})
    total = entry.get("total", 0)
    if total < MIN_OBSERVATIONS:
        return 1.0
    hit_rate = entry.get("hits", 0) / total
    # Linear map: hit_rate 0 → 0.5, hit_rate 1 → 2.0
    return 0.5 + hit_rate * 1.5


def record(repo_path: str, files_in_pack: list[str], response_text: str) -> None:
    """
    Parse the model response for cited identifiers and update file scores.

    Citation heuristics:
    - Backtick-quoted identifiers: `classify`, `Route.FALLBACK`
    - CamelCase tokens that match a file's stem or known symbol
    - File stems mentioned verbatim
    """
    if not response_text or not files_in_pack:
        return
    _ensure_loaded(repo_path)

    # Extract cited names from the response
    # Backtick-quoted (highest confidence — model is explicitly referencing code)
    backtick_names = set(re.findall(r'`([a-zA-Z_][a-zA-Z0-9_.]*)`', response_text))
    # CamelCase tokens (class names, type names)
    camel_names = set(re.findall(r'\b([A-Z][a-zA-Z0-9]+)\b', response_text))
    # snake_case identifiers that look like function names
    snake_names = set(re.findall(r'\b([a-z][a-z0-9_]{2,})\b', response_text))

    cited = backtick_names | camel_names | (snake_names & backtick_names)  # snake only if also backtick-cited

    repo_scores = _store.setdefault(repo_path, {})

    for file_path in files_in_pack:
        p = Path(file_path)
        stem = p.stem
        # A file is "cited" if its stem, or any qualified name containing the stem, appears
        file_cited = (
            stem in cited
            or stem in response_text
            or any(stem in name for name in backtick_names)
        )
        entry = repo_scores.setdefault(file_path, {"hits": 0, "total": 0})
        entry["total"] += 1
        if file_cited:
            entry["hits"] += 1
            log.debug("Relevance hit: %s (rate %.0f%%)", p.name, entry["hits"] / entry["total"] * 100)
        else:
            log.debug("Relevance miss: %s (rate %.0f%%)", p.name, entry["hits"] / entry["total"] * 100)

        # Persist incrementally — each file saved immediately
        sqlite_store.save_relevance(repo_path, file_path, entry["hits"], entry["total"])


def stats(repo_path: str) -> dict:
    _ensure_loaded(repo_path)
    repo_scores = _store.get(repo_path, {})
    if not repo_scores:
        return {"files_tracked": 0}
    learned = {
        p: {"mult": round(get_multiplier(repo_path, p), 2), **v}
        for p, v in repo_scores.items()
        if v.get("total", 0) >= MIN_OBSERVATIONS
    }
    return {"files_tracked": len(repo_scores), "learned": len(learned), "scores": learned}


