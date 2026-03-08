# CC-RLM — Recursive Language Model Gateway

## What This Is

A programmable context engine that sits between Claude Code and a locally-served model. Instead of re-reading files on every query or burning a 1M-token context window, the RLM layer loads the repo as a live REPL workspace, writes code to walk and slice it, and hands the model a minimal, task-specific context pack — like a precompiled header for each request.

**Stack:** Claude Code → CCR → RLM Gateway → Ollama / vLLM → model

---

## The Problem It Solves

| Approach | Problem |
|---|---|
| Re-read files each turn | Slow, wastes tokens, breaks at scale |
| 1M-token context dump | Expensive, incoherent, no structural awareness |
| Vector / semantic search | Blunt instrument — semantic similarity ≠ structural relevance |
| **RLM (this)** | Run code to understand code. Build only what this task needs. |

The key insight: a developer doesn't re-read the whole codebase before each keystroke. They know the shape of the code. They navigate by structure. The RLM layer does the same — it *programs its way* to the relevant slice.

---

## Architecture

```
┌─────────────────────┐
│     Claude Code     │  developer interface
└──────────┬──────────┘
           │  OpenAI-compat API calls
           ▼
┌─────────────────────┐
│        CCR          │  Claude Code Router
│  proxy + interceptor│  rewrites model target
│  injects repo path  │  falls back to Anthropic API
└──────────┬──────────┘
           │  {task, active_file, repo_path}
           ▼
┌──────────────────────────────────────────┐
│             RLM Gateway                  │  the REPL brain
│                                          │
│  1. mount repo as sandboxed workspace    │
│  2. spin up REPL worker                  │
│  3. run walkers (import, symbol, diff)   │
│  4. assemble context pack (< 8K tokens)  │
│  5. build final prompt                   │
└──────────┬───────────────────────────────┘
           │  system = context pack + task
           ▼
┌─────────────────────┐
│        vLLM         │  inference server (OpenAI-compat)
│    MiniMax-M2.5     │  local or remote
└─────────────────────┘
```

---

## The RLM Layer — How It Actually Works

### Persistent Index (SQLite)

On first request, RLM scans the repo and builds an import graph + symbol index. This is persisted to `~/.cc-rlm/store.db` (WAL mode). On subsequent server starts, the graph loads from SQLite — no cold-start rescan needed. Relevance scores (from answer-driven feedback) are also persisted, so the system gets smarter across sessions.

### Pre-Warming (File Watcher)

After the initial scan, a `watchdog` observer monitors the repo. When any code file is saved, `refresh_file()` re-indexes just that file. By the time you ask your next question, the index is already warm.

### Diff-First Relevance

Context ranking starts from files that actually changed (`git diff HEAD`), seeded at score 2.0. The active file gets 1.5. BFS walks the import graph from these seeds, decaying 0.65 per hop. This centers context on what you're working on right now, not just what the active file imports.

### BM25 Semantic Fallback

When the import graph gives fewer than 3 results (new file, no imports yet, cross-language boundary), BM25 kicks in. It scores all indexed files against the task text using an inverted index over symbol names and file stems. Results are normalized below graph scores so structural edges still dominate.

### Symbol-Level AST Slicing

Instead of including the first N lines of a file, `context_pack.py` uses Python's AST to extract only the function/class bodies relevant to the task. It scores each symbol by keyword match and call-graph proximity, then merges adjacent ranges. Non-code files (`.md`, `.json`, `.toml`) are filtered out.

### Session Dedup + Tool-Call Awareness

Files Claude already saw this session (unchanged mtime) are skipped on turns 2+. A `PostToolUse` hook tracks which files Claude reads/edits, and session dedup checks that list first. This saves ~32% additional tokens.

### Answer-Driven Relevance Scoring

After each streamed response, CCR parses the model output for cited symbol names (backtick-quoted, CamelCase). Files that get cited earn hits; files that don't earn misses. After 3+ observations, a multiplier (0.5× – 2.0×) biases future rankings. Persisted to SQLite.

### REPL Workers

Small Python programs that answer structural questions:

| Walker | Question it answers |
|---|---|
| `imports.py` | What does this file import? What imports this file? (Python AST) |
| `symbols.py` | Where is this function/class defined? What does it call? |
| `diff.py` | What changed since last commit? Changed file list? |
| `ts_imports.py` | TypeScript/JS import graph (pure Python, regex, no Node) |

Workers run as subprocesses with 500ms timeout. Results are cached by file mtime.

### Context Pack

**Target: < 8K tokens regardless of repo size.** The pack includes symbol-level slices (not full files), the import graph neighborhood, and the recent diff. Handed to the model as a system prompt preamble.

---

## Component Breakdown

| Component | Role | Port | Tech |
| --- | --- | --- | --- |
| **CCR** | Proxy, routing, auth, fallback, feedback loop | 8080 | Python / FastAPI |
| **RLM Gateway** | REPL orchestration, index, context assembly | 8081 | Python / FastAPI |
| **REPL workers** | Walkers, symbol extractors | subprocess pool | Python |
| **SQLite store** | Persistent import graph + relevance scores | — | `~/.cc-rlm/store.db` |
| **File watcher** | Pre-warming on save | — | watchdog |
| **Ollama** | Local model inference (dev/demo) | 11434 | Ollama |
| **vLLM** | GPU inference server (production) | 8000 | vLLM |
| **qwen2.5-coder:7b** | Default local model | — | Ollama pull |

---

## Data Flow (end to end)

```
1. Dev types in Claude Code
2. UserPromptSubmit hook fires — detects git root from CWD, writes /tmp/cc-rlm-state.json
3. CCR intercepts the API call
4. CCR reads repo_path + active_file from state file (no manual headers needed)
5. CCR calls RLM Gateway: POST /context  {task, active_file, repo_path}
6. RLM mounts workspace (idempotent — already mounted if same repo)
7. RLM dispatches walker jobs to subprocess pool (imports, symbols, diff — concurrent)
8. Workers return structured JSON results
9. context_pack.py assembles pack: active file first, then imports, symbol graph, diff
10. Pack enforced at 8K token budget
11. RLM calls Ollama/vLLM: POST /v1/chat/completions  (streaming)
12. Model streams tokens back through RLM → CCR → Claude Code
```

---

## What This Is / Is Not

**Is:**
- A programmable context engine — runs code to understand code
- A thin, stateless proxy layer (workspaces are cheap mounts, not sessions)
- Local-first — runs on dev machine or on-prem GPU node
- Model-agnostic — point CCR at any vLLM-served model

**Is not:**
- A RAG pipeline (no vector DB, no embedding index)
- An agent framework (no tool-calling loop, no multi-step planning)
- A 1M-token context manager
- A replacement for Claude — CCR falls back to Anthropic API for tasks outside the repo scope

---

## Build Phases

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | CCR proxy passthrough | Done |
| 1 | RLM stub, end-to-end flow | Done |
| 2 | Live walkers (imports, symbols, diff) | Done |
| 3 | Context pack optimizer, token budget | Done |
| 4 | Git-aware diff, active file fix, hook injection | Done |
| 5 | Symbol-level slicing, session dedup, walker cache | Done |
| 6 | TS/JS walker, incremental live repo index | Done |
| 7 | Prompt-driven routing (UserPromptSubmit hints) | Done |
| 8 | Diff-first context, tool-call awareness, answer-driven scoring | Done |
| 9 | Eval harness (4 cases, token reduction + recall) | Done |
| 10 | Persistent SQLite index + relevance scores | Done |
| 11 | Pre-warming on file save (watchdog) | Done |
| 12 | BM25 semantic fallback | Done |

---

## Test Results (Phases 0–4 validated)

Tested locally against the CC-RLM codebase itself. No GPU required for RLM validation.

**Individual walkers (all passing):**

| Walker | Result |
| --- | --- |
| `imports.py` | Resolved `rlm.config`, `rlm.context_pack`, `rlm.workspace` to file paths via AST |
| `symbols.py` | Extracted 7 symbols from `context_pack.py` with correct call graphs |
| `diff.py` | Returned full initial commit diff, branch name, and changed file list |

**End-to-end tests (full stack with Ollama):**

```text
Test 1 — with explicit headers (curl)
  Task:         "add retry logic to workspace mount"
  Active file:  rlm/workspace.py
  Token count:  2,397  (budget: 8,000)
  Slices:       rlm/config.py, rlm/main.py
  Symbols:      resolve_repo_path, mount, run_walker (with call graphs)
  Has diff:     true
  Model:        qwen2.5-coder:7b via Ollama — streaming tokens confirmed

Test 2 — no headers, hook state file only (Claude Code mode)
  Token count:  1,782 tokens
  Repo:         auto-detected via git rev-parse from CWD
  Active file:  auto-detected via git diff --name-only
  Model:        qwen2.5-coder:7b — streaming tokens confirmed
```

~1,800 tokens for a real coding task. Naive full-repo dump of the same codebase: ~15,000+ tokens. **84–88% reduction.** The model referenced file names by name in its response without being told explicitly.

---

## Key Design Decisions

**Why subprocess workers, not in-process?**
Isolation. A buggy walker crashes the worker, not the gateway. Workers can be restarted without downtime.

**Why < 8K token budget?**
MiniMax-M2.5 performs best with a sharp, dense context. A 100K dump with 95% irrelevant content is worse than 8K of exactly the right thing. Budget is configurable.

**Why not persist the context pack?**
Repo state changes constantly. Stale packs are worse than fresh ones. Stateless = correct by construction. Mount caching handles the repeated-read cost.

**Why CCR as a separate service from RLM?**
Separation of concerns. CCR handles auth, routing, fallback logic, and Claude Code compatibility. RLM handles only context. Either can be replaced independently.

**Why Ollama for local dev?**
Ollama runs on Apple Silicon without CUDA. OpenAI-compatible API at port 11434. `qwen2.5-coder:7b` is a strong code-capable model that fits in 8GB VRAM. For production with a GPU, swap `CCR_VLLM_URL` to point at a vLLM server serving MiniMax-M2.5 — no other changes needed.

---

## Configuration

```env
# CCR
CCR_PORT=8080
CCR_RLM_URL=http://localhost:8081
CCR_VLLM_URL=http://localhost:11434    # Ollama; change to :8000 for vLLM
CCR_MODEL_OVERRIDE=qwen2.5-coder:7b   # rewrites model field; empty = pass through
CCR_ANTHROPIC_FALLBACK_KEY=sk-ant-...  # used when not in a git repo
CCR_FALLBACK_ENABLED=true

# RLM Gateway
RLM_PORT=8081
RLM_TOKEN_BUDGET=8000
RLM_WORKER_POOL_SIZE=4
RLM_WALKER_TIMEOUT_MS=500
RLM_STORE_PATH=~/.cc-rlm/store.db     # persistent SQLite index + scores
RLM_CACHE_ENABLED=true                 # mtime-invalidated walker cache
RLM_SESSION_DEDUP_ENABLED=true         # skip unchanged files across turns
RLM_REPO_INDEX_ENABLED=true            # live import graph
RLM_BM25_ENABLED=true                  # BM25 fallback for sparse graphs
```

---

## File Structure

```
CC-RLM/
├── spec.md                     ← this file
├── exec-summary.md             ← executive summary
├── pyproject.toml              ← Python 3.13, shared deps
├── .env.example
├── .env                        ← local secrets (gitignored)
├── .gitignore
│
├── .claude/
│   ├── settings.json           ← hook registration (3 hooks)
│   ├── hooks/
│   │   ├── inject_repo_context.py  ← UserPromptSubmit: route hints + state file
│   │   ├── track_tool_reads.py     ← PostToolUse: tracks Read/Edit/Write
│   │   └── pre_tool_use.py         ← blocks writes outside project root
│   └── skills/
│       ├── test-rlm.md         ← local test walkthrough
│       ├── add-walker.md       ← how to add a new walker
│       └── eval.md             ← A/B quality evaluation guide
│
├── docs/adr/
│   ├── 001-repl-over-rag.md
│   ├── 002-subprocess-walkers.md
│   └── 003-token-budget.md
│
├── tests/
│   └── eval/
│       ├── run_eval.py         ← eval harness: token reduction + recall + latency
│       └── cases.json          ← 4 test cases with expected files
│
├── ccr/                        ← Claude Code Router (proxy)
│   ├── __init__.py
│   ├── main.py                 ← FastAPI app, streaming, feedback loop
│   ├── router.py               ← classify(), get_route_hint(), prompt-driven routing
│   ├── config.py               ← settings (pydantic-settings, CCR_ prefix)
│   └── CLAUDE.md               ← module context for AI
│
└── rlm/                        ← RLM Gateway (REPL brain)
    ├── __init__.py
    ├── main.py                 ← FastAPI app, /context + /feedback + /health
    ├── config.py               ← settings + feature flags (RLM_ prefix)
    ├── workspace.py            ← repo mount, run_walker() dispatcher
    ├── context_pack.py         ← symbol-level slicing, session dedup, assembly
    ├── repo_index.py           ← live import graph (SQLite-backed, BFS ranking)
    ├── store.py                ← SQLite persistence (import_graph + relevance)
    ├── bm25.py                 ← BM25 semantic fallback (inverted index)
    ├── watcher.py              ← watchdog file watcher (pre-warming on save)
    ├── relevance_store.py      ← answer-driven scoring (SQLite-backed)
    ├── cache.py                ← mtime-invalidated walker result cache
    ├── session.py              ← session dedup tracker
    ├── CLAUDE.md               ← module context for AI
    └── walkers/
        ├── __init__.py
        ├── imports.py          ← Python AST import graph walker
        ├── symbols.py          ← symbol/call graph walker
        ├── diff.py             ← git diff walker
        ├── ts_imports.py       ← TypeScript/JS import walker (pure Python)
        └── CLAUDE.md           ← walker authoring guide
```
