# CC-RLM — Executive Summary

## What We Built

A self-improving context engine that sits between Claude Code and a locally-served LLM. Instead of re-reading files or dumping the whole codebase into tokens, CC-RLM maintains a live structural model of the repo — import graph, symbol index, diff state, relevance history — and hands the model only what it needs for this specific task.

**Stack:** Claude Code → CCR (proxy) → RLM Gateway (REPL brain) → Ollama (local) / vLLM (GPU)

---

## The Problem

| Approach | What breaks |
|---|---|
| Re-read files each turn | Slow, wasteful, doesn't scale past a few files |
| 1M-token context dump | Expensive, incoherent, "lost in the middle" |
| Vector / semantic search | Wrong signal — semantic similarity ≠ structural relevance |
| **CC-RLM** | Run code to understand code. Build only what this task needs. Learn what works. |

---

## How It Works

```
You type in Claude Code
        ↓
UserPromptSubmit hook — detects git root, classifies route, writes state file
        ↓
CCR (port 8080) — reads state file, routes: Ollama+RLM / Anthropic / passthrough
        ↓
RLM Gateway (port 8081):
  1. Check persistent SQLite index (warm from previous sessions)
  2. Diff-first relevance: seed BFS from git diff changed files (score 2.0)
  3. BM25 fallback if import graph is sparse (< 3 results)
  4. Apply learned relevance multipliers from answer-driven scoring
  5. Symbol-level AST slicing: extract function/class bodies, not full files
  6. Session dedup: skip files Claude already saw this turn
  7. Assemble context pack (< 8K tokens)
        ↓
Ollama (port 11434) receives enriched prompt, streams response back
        ↓
CCR parses response for cited symbols → updates relevance store (self-improving)
        ↓
Answer appears in Claude Code
```

**No manual configuration. No headers. Works automatically in any git repo.**

---

## Results

| Metric | Value |
|---|---|
| Token reduction | **82%** avg vs naive full-repo injection |
| Recall | **88%** avg (3/4 eval cases at 100%) |
| Context pack size | ~1,800–3,000 tokens |
| Naive baseline | 17,295 tokens across 20 .py files |
| Avg latency | 187ms (target: ~20ms with pre-warming) |
| Session dedup savings | ~32% additional on turn 2+ |
| Setup | `poetry install` + 2 uvicorn commands |

---

## What We Shipped

### Core Engine
| Component | Description |
|---|---|
| `rlm/main.py` | Gateway — walker orchestration, diff-first relevance, BM25 fallback |
| `rlm/context_pack.py` | Symbol-level AST slicing, session dedup, token budget assembly |
| `rlm/repo_index.py` | Incremental live import graph (SQLite-backed, BFS ranking) |
| `rlm/store.py` | Persistent SQLite store — graph + relevance scores survive restarts |
| `rlm/bm25.py` | BM25 semantic fallback — inverted index over symbol names |
| `rlm/watcher.py` | watchdog file watcher — pre-warms index on save |
| `rlm/relevance_store.py` | Answer-driven scoring — learns which files get cited |
| `rlm/cache.py` | mtime-invalidated walker result cache |
| `rlm/session.py` | Session dedup — skip unchanged files across turns |

### Walkers (subprocess isolation)
| Walker | What it answers |
|---|---|
| `rlm/walkers/imports.py` | Python AST import graph: what imports what |
| `rlm/walkers/symbols.py` | Function/class definitions + one-level call graph |
| `rlm/walkers/diff.py` | Uncommitted changes + recent git history |
| `rlm/walkers/ts_imports.py` | TypeScript/JS import graph (pure Python, regex) |

### Routing & Hooks
| Component | Description |
|---|---|
| `ccr/main.py` | FastAPI proxy — enrichment, streaming, feedback loop |
| `ccr/router.py` | Route classification with prompt-driven hints |
| `.claude/hooks/inject_repo_context.py` | UserPromptSubmit — route hints, state file |
| `.claude/hooks/track_tool_reads.py` | PostToolUse — tracks what Claude reads/edits |
| `.claude/hooks/pre_tool_use.py` | Guardrail — blocks writes outside project |

### Testing & Docs
| Component | Description |
|---|---|
| `tests/eval/run_eval.py` | Eval harness: token reduction + recall + latency |
| `tests/eval/cases.json` | 4 test cases with expected files |
| `docs/adr/` | 3 architecture decision records |
| `CLAUDE.md` (root + modules) | Scoped AI context per layer |

---

## Prompt-Driven Routing

The hook analyzes your prompt and picks the best route automatically:

| Prefix | Route |
|---|---|
| `/claude` or `/anthropic` | Anthropic API (full Claude reasoning) |
| `/local` or `/ollama` | Ollama direct, no context enrichment |
| `/repo` | Force RLM enrichment |
| (none, knowledge Q) | Auto → Anthropic |
| (none, code signal) | Auto → Ollama + RLM enrichment |

---

## Self-Improving Loop

CC-RLM gets smarter with use:

1. **First request**: full repo scan → SQLite index → BFS ranking
2. **Each response**: parse model output for cited symbols → update hit/miss rates per file
3. **Next request**: relevance multipliers (0.5× – 2.0×) bias context toward files that get cited
4. **Server restart**: SQLite index + relevance scores load instantly (no cold start)
5. **File save**: watchdog triggers `refresh_file()` → index pre-warmed before you ask

---

## Key Design Decisions

- **REPL walkers, not vector search** — structural facts beat semantic similarity for code. ([ADR-001](docs/adr/001-repl-over-rag.md))
- **Subprocess isolation** — walker crashes don't bring down the gateway. ([ADR-002](docs/adr/002-subprocess-walkers.md))
- **8K token budget** — forces precision; better answers than a full-file dump. ([ADR-003](docs/adr/003-token-budget.md))
- **Diff-first seeding** — BFS starts from `git diff` changed files, not just the active file
- **SQLite persistence** — graph + scores survive restarts; compound interest on every session
- **BM25 fallback** — closes the recall gap for files with no import edges

---

## Configuration

```env
# CCR
CCR_VLLM_URL=http://localhost:11434    # Ollama (or :8000 for vLLM)
CCR_MODEL_OVERRIDE=qwen2.5-coder:7b
CCR_FALLBACK_ENABLED=true
CCR_ANTHROPIC_FALLBACK_KEY=sk-ant-...

# RLM Gateway
RLM_TOKEN_BUDGET=8000
RLM_WALKER_TIMEOUT_MS=500
RLM_STORE_PATH=~/.cc-rlm/store.db     # persistent index + scores
RLM_CACHE_ENABLED=true
RLM_SESSION_DEDUP_ENABLED=true
RLM_REPO_INDEX_ENABLED=true
RLM_BM25_ENABLED=true
```

---

## What's Next — Bigger Ideas

### Near-term (proven patterns, ready to build)
- **Anthropic fallback format translation** — proper OpenAI → `/v1/messages` schema so `/claude` prefix works cleanly
- **Cross-language graph edges** — Python calls to JS/TS (and vice versa) via shared symbol names
- **Eval CI** — run eval harness on every PR; regression gate on recall and token budget

### Medium-term (compound improvements)
- **Multi-repo awareness** — monorepo support; cross-package import graph
- **Branch-aware context** — weight files by branch recency; feature branch files rank higher
- **Adaptive token budget** — scale budget up/down based on task complexity signal

### Longer-term (big ideas)
- **Agentic context loops** — model requests additional context mid-response; RLM fulfills on the fly
- **Team relevance pooling** — aggregate relevance scores across developers; shared index of "what matters"
- **Language-agnostic AST** — tree-sitter based walkers covering any language with one framework

---

## How to Run

```bash
# 1. Pull a model
ollama pull qwen2.5-coder:7b

# 2. Install deps
cd /path/to/CC-RLM
poetry install

# 3. Start services
poetry run uvicorn rlm.main:app --port 8081 &
poetry run uvicorn ccr.main:app --port 8080 &

# 4. Point Claude Code at CCR
export ANTHROPIC_BASE_URL=http://localhost:8080

# 5. Use Claude Code normally
claude
```

The hook fires on every prompt. The context pack builds in < 200ms. The index gets smarter every turn. The model sees exactly what it needs.
