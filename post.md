# I built a context engine that cuts Claude Code's token usage by 82% — and it gets smarter every turn

## The problem every AI coding tool gets wrong

You're three files deep in a refactor. You ask your AI assistant to update a function signature. It either:

1. **Re-reads everything** — slow, wasteful, doesn't scale past 10 files
2. **Dumps the whole repo into a 1M-token context** — expensive, and the model loses track halfway through
3. **Runs a vector search** — returns semantically similar but structurally irrelevant files

None of these match how *you* actually navigate code. You don't re-read the whole codebase before each edit. You follow imports, check the call graph, glance at the recent diff. You know the *shape* of the code.

So I built a system that does the same thing — programmatically, in under 200ms.

## What CC-RLM does

CC-RLM is a context engine that sits between Claude Code and a local LLM (Ollama). Instead of semantic search or brute-force context windows, it maintains a **live structural model** of your repo:

- **Import graph** — what imports what, updated incrementally
- **Symbol index** — every function/class, their call targets, their line ranges
- **Diff state** — what actually changed since last commit
- **Relevance history** — which files the model actually *cited* in past answers

From these, it assembles a **< 8K token context pack** — symbol-level AST slices, not full files — and hands only that to the model.

```
You type in Claude Code
    ↓
Hook detects git root, classifies your prompt, writes state file
    ↓
Proxy reads state, routes to Ollama + RLM enrichment (or Anthropic for knowledge Qs)
    ↓
RLM Gateway:
  1. Load persistent index from SQLite (warm from previous sessions)
  2. Seed BFS from git diff changed files (score 2.0) + active file (1.5)
  3. BM25 fallback if import graph is sparse
  4. Apply learned relevance multipliers
  5. AST-slice only the relevant function/class bodies
  6. Skip files Claude already saw this turn
  7. Assemble pack (< 8K tokens)
    ↓
Model gets exactly what it needs. Nothing else.
```

**No configuration. No headers. Works automatically in any git repo.**

## The numbers

| Metric | Value |
| --- | --- |
| Token reduction | **82%** vs naive full-repo injection |
| Recall | **88%** (correct files included in context) |
| Context pack | ~2K tokens vs 17K naive baseline |
| Session dedup | ~32% additional savings on turn 2+ |
| Latency | < 200ms per context build |

Tested against itself — 4 eval cases, measuring token cost and whether the right files made it into the pack.

## The part that surprised me: it learns

The system parses every model response for cited symbol names. Files that get referenced earn hits. Files that don't earn misses. After a few turns, a relevance multiplier (0.5x to 2.0x) biases future context toward what actually matters.

These scores persist to SQLite. Restart the server, switch branches, come back tomorrow — the system remembers what worked.

The compounding effect:
1. **First request** — full scan, cold index
2. **Each response** — parse citations, update hit rates
3. **Next request** — context biased toward proven-useful files
4. **Server restart** — index + scores load from SQLite, no cold start
5. **File save** — watchdog triggers incremental re-index before you ask

## Key design decisions

**Structure over semantics.** Import graphs and call chains tell you what's relevant to a code task. Embedding similarity tells you what looks similar. These are different things. For "update this function signature," you want the callers, not files with similar docstrings.

**AST slicing, not line ranges.** The context pack includes specific function/class bodies extracted by Python's AST, scored by keyword match and call-graph proximity. Adjacent ranges get merged. A 500-line file might contribute 40 lines to the pack.

**Diff-first ranking.** BFS starts from files that actually changed (`git diff HEAD`), not just the active file. This centers context on your current work — the files you touched 5 minutes ago are almost certainly relevant.

**BM25 as fallback, not primary.** When the import graph gives < 3 results (new file, cross-language boundary), an inverted index over symbol names kicks in. But graph edges always dominate — structural facts are more reliable than text matching.

**Session dedup.** A PostToolUse hook tracks what Claude reads/edits. Files it already saw (unchanged mtime) get skipped on subsequent turns. This is free token savings with zero recall cost.

## What it's not

- Not a RAG pipeline (no vector DB, no embeddings)
- Not an agent framework (no tool-calling loops)
- Not a 1M-token context manager
- Not a replacement for Claude — falls back to Anthropic API for non-code questions

## What's honest about what doesn't work yet

- Anthropic fallback needs format translation (OpenAI → Messages API)
- Cross-boundary recall is ~50% for files with no direct import edges (BM25 helps but doesn't fully close the gap)
- Walker cache starts at ~33% hit rate cold; climbs with use
- Python-first — TS/JS walker exists but is regex-based, not full AST

## Stack

```
Claude Code → CCR (FastAPI proxy, port 8080)
           → RLM Gateway (REPL brain, port 8081)
           → Ollama (qwen2.5-coder:7b, port 11434)

Persistence: SQLite (~/.cc-rlm/store.db)
File watching: watchdog
Language: Python 3.13
```

## Try it

```bash
ollama pull qwen2.5-coder:7b
cd CC-RLM && poetry install
poetry run uvicorn rlm.main:app --port 8081 &
poetry run uvicorn ccr.main:app --port 8080 &
export ANTHROPIC_BASE_URL=http://localhost:8080
claude
```

Prompt prefixes for routing: `/claude` forces Anthropic, `/local` forces Ollama without enrichment, `/repo` forces RLM enrichment. No prefix = auto-detect.

## Where this goes

The immediate wins are proven and built. The interesting longer-term ideas:

- **Agentic context loops** — model requests additional context mid-response; RLM fulfills on the fly
- **Team relevance pooling** — shared index across a team; "what files matter for this area of the codebase" as collective knowledge
- **Language-agnostic AST** — tree-sitter walkers covering any language with one framework

The core insight is simple: **code relevance is structural, not semantic.** If you build your context engine around the actual graph of the code — imports, calls, diffs, citations — you get better answers with fewer tokens. And if you persist what works, it compounds.
