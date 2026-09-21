# Computer-Use Automation System

A system that uses an LLM to **discover** how to complete tasks in legacy UI applications, **records** successful runs as typed capability artifacts, and **replays** them deterministically — without the model in the loop.

Built for the interface.ai AI Engineer take-home assessment.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Running the Demo](#running-the-demo)
- [Running Tests](#running-tests)
- [Architecture](#architecture)
- [Design Report](#design-report)
- [Project Structure](#project-structure)

---

## How It Works

The system operates in two phases:

**Phase 1 — Discovery** (one-time, requires LLM)
A Claude-powered agent navigates a target application through Playwright, observing the UI via its accessibility tree. It figures out how to accomplish a natural-language goal ("look up member M1001 and read their savings balance"), and the system records every action as a structured **capability artifact** (YAML).

**Phase 2 — Replay** (repeatable, no LLM needed)
The replay engine re-executes the recorded capability deterministically. It resolves UI elements through a six-tier locator fallback chain (accessibility > text > label > semantic fingerprint > CSS > XPath), handles expected error conditions (e.g. "member not found" is a business outcome, not a crash), and produces structured results.

> A pre-recorded capability artifact and real evidence logs from a successful discovery run are included in this repo. **You can run replay and tests immediately without an API key.**

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.13+ | [python.org](https://www.python.org/downloads/) or `brew install python@3.13` |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

No other global dependencies are required. All Python packages are managed by `uv`.

---

## Setup

```bash
# Clone the repository
git clone https://github.com/mahiballa28/computer-use-automation-system.git
cd computer-use-automation-system

# Install all dependencies (creates a virtual environment automatically)
uv sync

# Install the Chromium browser for Playwright
uv run playwright install chromium
```

**API key setup** (required only for discovery — skip this if you only want to run replay and tests):

```bash
cp .env.example .env
# Edit .env and add your Anthropic API key:
# ANTHROPIC_API_KEY=sk-ant-...your-key-here
```

---

## Running the Demo

### Option A: Replay only (no API key needed)

A pre-recorded capability artifact is included at `capabilities/lookup_member_balance.yaml`. You can replay it immediately against the mock banking app.

**Step 1 — Start the mock banking app** (keep this terminal open):
```bash
uv run python mock_bank_app/app.py
```

**Step 2 — Replay the happy path** (member found, balance returned):
```bash
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M1001"}'
```

**Step 3 — Replay with a non-existent member** (business outcome, not a crash):
```bash
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M9999"}'
```

**Step 4 — Explore the CLI:**
```bash
uv run cua list                                           # List saved capabilities
uv run cua show capabilities/lookup_member_balance.yaml   # Inspect artifact details
```

### Option B: Full discovery + replay (requires API key)

With an API key configured in `.env`, you can run the LLM-driven discovery from scratch:

```bash
# Discovery — Claude navigates the app and records its actions
uv run cua discover "Look up member M1001 and read their savings balance" \
    --target http://localhost:5001 \
    --headless

# Then replay the newly discovered capability
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M1001"}'
```

### Evidence

Real evidence from a completed discovery + replay cycle is committed in `evidence/`:

| File | Contents |
|------|----------|
| `evidence/discovery_run_857bcb18.json` | Structured event log from LLM-driven discovery (22 events) |
| `evidence/replay_run_1ba5b4ab.json` | Successful replay log (18 events) |
| `evidence/replay_run_c4f471e1.json` | Business-outcome replay — member not found (12 events) |
| `evidence/screenshots/` | Screenshots captured during discovery |

---

## Running Tests

```bash
uv run pytest tests/ -v
```

105 tests across 12 test files. All tests run without an API key and without a running browser — external dependencies are mocked at the boundary.

Test coverage includes: artifact schema validation, multi-tier locator resolution, replay engine execution, safety policy enforcement, PII redaction, anomaly detection, visual regression hashing, audit trail integrity, capability search, adaptive timing, auto-healing locators, and workflow DAG validation.

---

## Architecture

```
Goal (natural language)
  |
  v
+--------------+     +-----------+     +--------------+
|  Agent Loop  | --> | Recorder  | --> |  Capability  |
|  (Claude +   |     | (trace -> |     |  Artifact    |
|  Playwright) |     |  artifact)|     |  (YAML)      |
+--------------+     +-----------+     +------+-------+
                                              |
                                              v
                                       +--------------+
                                       |  Replay      |
                                       |  Engine      |
                                       |  (no LLM)    |
                                       +--------------+
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Accessibility tree as primary perception** | More stable than DOM selectors for legacy apps. Maps to desktop apps via OS a11y APIs. Smaller tokens = cheaper LLM calls. |
| **Multi-strategy locators** | Six-tier fallback chain: a11y > text > label > fingerprint > CSS > XPath. Graceful degradation when one strategy breaks. |
| **Three-tier error taxonomy** | Business outcomes (member not found) != recoverable errors (timeout) != hard failures (element missing). Banking compliance requires this distinction. |
| **YAML artifacts** | Human-reviewable, supports comments, cleaner multi-line strings. JSON trivially derivable. |
| **Local embeddings for fingerprinting** | Replay must be fast (~5ms vs ~500ms API). Deterministic. Free. Works air-gapped. |

### Beyond-Scope AI Features

Eight additional features not requested by the assignment. Each addresses a real production problem in banking UI automation:

1. **Semantic Element Fingerprinting** — Embedding-based self-healing locators using sentence-transformers. Finds matching elements via cosine similarity without LLM calls (~5ms). Works offline/air-gapped. (`src/cua/models/fingerprint.py`, `src/cua/replay/locator_resolver.py`)

2. **Execution Trace Anomaly Detection** — Statistical profiling (timing distributions, element counts, value ranges). Detects "succeeded incorrectly" — catches silent failures that pass all checkpoints. (`src/cua/observability/anomaly.py`)

3. **Capability Composition (DAG Workflows)** — Chain capabilities into DAGs with typed data flow, conditional branching, and compensating actions for rollback. (`src/cua/models/workflow.py`, `src/cua/orchestration/engine.py`)

4. **Visual Regression Detection** — Perceptual hashing (aHash + dHash) detects unexpected UI changes between runs. Zero external dependencies. (`src/cua/observability/visual_regression.py`)

5. **Tamper-Evident Audit Trail** — SHA-256 hash chain for every action. Any modification breaks the chain. Supports SOX/FFIEC compliance. (`src/cua/observability/audit_trail.py`)

6. **TF-IDF Capability Discovery** — Natural language search over the capability registry. Find capabilities by intent, not exact name. (`src/cua/orchestration/capability_registry.py`)

7. **Adaptive Timing Prediction** — EMA-based step timing profiles. Computes adaptive timeouts and detects degrading steps. (`src/cua/replay/adaptive_timing.py`)

8. **Auto-Healing Locators** — When a fallback locator succeeds consistently, auto-promotes it in the capability YAML. (`src/cua/replay/auto_healer.py`)

---

## Design Report

See **[REPORT.md](REPORT.md)** for the full design write-up covering:

1. Architecture
2. Artifact Schema
3. Determinism & Error Handling
4. Heterogeneity & Multi-Tenant
5. Escalation & Handoff
6. Safety
7. Cuts (what was deliberately left out and why)
8. Beyond-Scope AI Features (detailed technical depth)

---

## Project Structure

```
src/cua/
  models/                  # Pydantic data models
    capability.py          # The capability artifact schema (focal point)
    execution.py           # Run results, three-tier error taxonomy
    policy.py              # Safety policy models
    fingerprint.py         # Semantic element fingerprinting
    workflow.py            # Workflow DAG models
    escalation.py          # Human-in-the-loop models
  agent/                   # LLM-driven discovery
    loop.py                # Observe > decide > act loop
    prompts.py             # Claude tool definitions
  surfaces/                # Surface adapter abstraction
    protocol.py            # Platform-agnostic protocol
    playwright_adapter.py  # Playwright implementation
  recording/               # Discovery trace > capability artifact
    recorder.py
  replay/                  # Deterministic replay engine
    executor.py            # The production execution path
    locator_resolver.py    # Multi-tier locator resolution
    adaptive_timing.py     # EMA-based timing prediction
    auto_healer.py         # Self-updating locator strategies
  safety/                  # Guardrails
    guard.py               # Policy enforcement
    redactor.py            # PII redaction
  escalation/              # Human-in-the-loop
    handoff.py             # CDP session handoff
  observability/           # Logging + anomaly detection
    logger.py              # Structured logging
    anomaly.py             # Execution anomaly detection
    visual_regression.py   # Perceptual hash visual diffing
    audit_trail.py         # Cryptographic hash chain audit log
  orchestration/           # Workflow engine
    engine.py              # DAG executor
    capability_registry.py # TF-IDF capability search
  cli.py                   # CLI entry point

mock_bank_app/             # Legacy banking app (Flask, intentionally hostile HTML)
policies/                  # Safety policy YAML files
capabilities/              # Saved capability artifacts
evidence/                  # Discovery + replay evidence logs
tests/                     # 105 tests across 12 test files
```

---

## Configuration

| Environment Variable | Description | Required |
|---------------------|-------------|----------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude | Discovery only |
| `ANTHROPIC_MODEL` | Model ID (default: `claude-haiku-4-5-20251001`) | No |
| `HEADLESS` | Run browser headless (default: `true`) | No |
| `LOG_LEVEL` | Logging level (default: `INFO`) | No |
| `LOG_FORMAT` | `json` or `console` (default: `json`) | No |

Replay does not require an API key — it runs deterministically without the LLM.

## Dependencies

- **Python 3.13** — runtime
- **Anthropic Claude** (`claude-haiku-4-5-20251001`) — discovery agent loop
- **Playwright** — browser automation
- **Pydantic v2** — all data models, strict typing
- **Flask** — mock banking app
- **sentence-transformers** — local embeddings for fingerprinting (optional)
- **structlog** — structured JSON logging
- **typer** — CLI framework
- **Rich** — terminal output formatting

## License

MIT
