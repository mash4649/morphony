# Morphony

## Tagline
CLI-first autonomous research agent foundation scaffolding.

---

# Demo

```bash
uv sync --all-extras
uv run agent --help
uv run agent run "Investigate a product launch plan"
uv run agent status
uv run agent review evaluate <task_id>
```

The main loop is CLI-driven:

`goal` -> `run` -> `status/review/improve` -> `checkpoint/memory` -> `summary`

---

# TL;DR

Morphony solves **the gap between a raw LLM call and a reproducible autonomous research workflow**.

Without:
- ad-hoc scripts
- unclear task state
- scattered memory files
- no checkpoint/recovery path
- no review or improvement loop

With:
- a typed task lifecycle
- CLI commands for run, status, approve, reject, pause, resume, review, and memory operations
- persistent episodic and semantic memory
- checkpoints and recovery
- tool registration and execution controls
- progress reporting and plan tracking

Think of it as:
- **Git for agent task state**
- **An operating shell for autonomous research workflows**

---

# Why this exists

Most agent demos stop at a prompt and a response. That is not enough for real work.

Morphony exists because production-ish agent workflows need:

- explicit task state transitions
- resumable execution
- persistent memory
- checkpointed progress
- safety controls for approval and escalation
- review and improvement after execution
- a CLI that makes every state visible

The problem is structural: once a workflow spans multiple steps, retries, approvals, and memory writes, the logic stops fitting in a single prompt.

---

# What it does

```text
Goal -> Lifecycle Manager -> Tools / Memory / Review -> Checkpoints -> Output
                │                │         │               │
                │                │         │               └─ resumable state
                │                │         └─ episodic + semantic knowledge
                │                └─ search, fetch, analysis, report tools
                └─ task state, approvals, queue, budget, events
```

Morphony provides:

- task lifecycle management
- queue orchestration
- review and self-evaluation loops
- episodic and semantic memory stores
- memory extraction and import/migration support
- tool registration and execution wrappers
- checkpoints and recovery support
- status, log, health, config, and version commands

---

# Quick Start

## Install

```bash
uv sync --all-extras
```

## Run

```bash
uv run agent --help
```

## Example

```bash
uv run agent run "Summarize the current project status"
uv run agent status
uv run agent memory list
uv run agent review assess <task_id>
```

---

# Features

- Typed CLI for task lifecycle, review, memory, tools, and configuration
- Persistent task state with checkpoints and resume support
- Episodic and semantic memory stores with extraction and import support
- Review, self-evaluation, and improvement loops
- Queue runner for starting pending tasks
- Tool registry with approval-aware registration
- Config loading with runtime overrides
- Progress and roadmap documentation for the whole plan

---

# Architecture

```text
CLI
 ├─ lifecycle
 │   ├─ run / status / approve / reject / pause / resume
 │   └─ checkpoints / queue / audit log / feedback
 ├─ memory
 │   ├─ episodic store
 │   ├─ semantic store
 │   └─ extraction / import / migration
 ├─ review
 │   ├─ assess
 │   ├─ evaluate
 │   └─ improve
 ├─ tools
 │   ├─ registry
 │   ├─ built-in tools
 │   └─ plugin tools
 └─ config
     └─ YAML config + runtime overrides
```

The project is intentionally CLI-first. State is stored in files and validated through tests.

---

# Use Cases

- Run a research task with checkpoints and visible progress
- Review a finished task and decide whether it is approved
- Import memory batches from JSON or YAML
- Start the next queued task automatically
- Register tools with a controlled approval flow
- Inspect configuration and health from the terminal

---

# Comparison

| Feature | Existing scripts | Morphony |
| ------- | ---------------- | -------- |
| Task state | ad-hoc | typed lifecycle + queue |
| Resume | manual | checkpoint-backed |
| Memory | scattered files | episodic + semantic stores |
| Review | none or manual | review / evaluate / improve |
| Tool control | direct calls | registry + approval flow |
| Visibility | low | `status`, `log`, `health`, `config` |

---

# Ecosystem Position

| Category | Tools | Relation |
| -------- | ----- | -------- |
| Agent framework | Morphony | CLI-first workflow engine for autonomous research |
| Package manager | `uv` | Install, test, and run the project |
| CLI framework | Typer | Implements the command surface |
| Data validation | Pydantic | Typed config and models |
| Persistence | SQLite files | Task state, checkpoints, and memory |

---

# Roadmap

- Phase 4: trust and autonomy expansion
- Phase 5: multi-domain support
- Phase 6: multi-agent collaboration
- Phase 7: collective intelligence and self-improvement

---

# Documentation

| Topic | Link |
| ----- | ---- |
| Project docs root | [docs/README.md](docs/README.md) |
| PLAN index | [docs/PLAN/README.md](docs/PLAN/README.md) |
| Progress report | [docs/PLAN/8.進捗報告.txt](docs/PLAN/8.進捗報告.txt) |
| Roadmap | [docs/PLAN/7.ロードマップ.txt](docs/PLAN/7.ロードマップ.txt) |
| Current version and license | [VERSION.txt](VERSION.txt) |

---

# Contributing

Changes should stay small, typed, and testable.

- keep scope narrow
- add or update tests when behavior changes
- run the relevant pytest subset before merging
- update docs when the workflow or CLI changes

---

# License

MIT License.

Current version: `0.1.0`
