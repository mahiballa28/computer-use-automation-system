# Computer-Use Automation System (CUA)

An AI-powered system that discovers how to complete tasks in legacy UI applications using an LLM, records successful runs as reusable capability artifacts, and replays them deterministically without the model in the loop.

Built for the interface.ai AI Engineer take-home project.

## Quick Start

```bash
# 1. Install dependencies
uv sync
uv run playwright install chromium

# 2. Set your Anthropic API key
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# 3. Start the mock banking app (in a separate terminal)
uv run python mock_bank_app/app.py

# 4. Run discovery — LLM figures out how to complete the goal
uv run cua discover "Look up member M1001 and read their savings balance" \
    --target http://localhost:5001 \
    --headless

# 5. Replay the saved capability deterministically (no LLM)
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M1001"}'

# 6. Replay with a non-existent member (business outcome, not a crash)
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M9999"}'
```

## Architecture

See [REPORT.md](REPORT.md) for the full design write-up.

```
Goal (natural language)
  │
  ▼
┌──────────────┐     ┌───────────┐     ┌──────────────┐
│  Agent Loop  │ ──► │ Recorder  │ ──► │  Capability  │
│  (Claude +   │     │ (trace →  │     │  Artifact    │
│  Playwright) │     │  artifact)│     │  (YAML)      │
└──────────────┘     └───────────┘     └──────┬───────┘
                                              │
                                              ▼
                                       ┌──────────────┐
                                       │  Replay      │
                                       │  Engine      │
                                       │  (no LLM)    │
                                       └──────────────┘
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Accessibility tree as primary perception** | More stable than DOM selectors for legacy apps. Maps to desktop apps via OS a11y APIs. Smaller tokens = cheaper LLM calls. |
| **Multi-strategy locators** | Fallback chain: a11y → text → label → fingerprint → CSS → XPath. Graceful degradation when one strategy breaks. |
| **Three-tier error taxonomy** | Business outcomes (member not found) ≠ recoverable errors (timeout) ≠ hard failures (element missing). Banking compliance requires this distinction. |
| **YAML artifacts** | Human-reviewable, supports comments, cleaner multi-line strings. JSON trivially derivable. |
| **Local embeddings for fingerprinting** | Replay must be fast (5ms vs 500ms API). Deterministic. Free. Works air-gapped. |

### Beyond-Scope AI Features

1. **Semantic Element Fingerprinting** — Embedding-based self-healing locators using sentence-transformers. When exact locators fail, find the matching element via cosine similarity.

2. **Execution Trace Anomaly Detection** — Statistical profiling of replay behavior (timing, values, page states). Detects "succeeded incorrectly" — critical for banking compliance.

3. **Capability Composition (DAG Workflows)** — Chain capabilities into directed acyclic graphs with typed data flow, conditional branching, and compensating actions.

## Project Structure

```
src/cua/
├── models/           # Pydantic models — the artifact schema is the focal point
│   ├── capability.py # THE capability artifact schema
│   ├── execution.py  # Run results, three-tier error taxonomy
│   ├── policy.py     # Safety policy models
│   ├── fingerprint.py # Semantic element fingerprinting (B1)
│   ├── workflow.py   # Workflow DAG models (B3)
│   └── escalation.py # Human-in-the-loop models
├── agent/            # LLM-driven discovery
│   ├── loop.py       # Observe → decide → act loop
│   └── prompts.py    # Claude tool definitions
├── surfaces/         # Surface adapter abstraction
│   ├── protocol.py   # Platform-agnostic protocol
│   └── playwright_adapter.py
├── recording/        # Discovery trace → capability artifact
├── replay/           # Deterministic replay engine
│   ├── executor.py   # The production execution path
│   └── locator_resolver.py # Multi-tier locator resolution
├── safety/           # Guardrails
│   ├── guard.py      # Policy enforcement
│   └── redactor.py   # PII redaction
├── escalation/       # Human-in-the-loop
│   └── handoff.py    # CDP session handoff
├── observability/    # Logging + anomaly detection
│   ├── logger.py     # Structured logging
│   └── anomaly.py    # Execution anomaly detection (B2)
├── orchestration/    # Workflow engine
│   └── engine.py     # DAG executor (B3)
└── cli.py            # CLI entry point

mock_bank_app/        # Legacy banking app (Flask, intentionally hostile HTML)
policies/             # Safety policy YAML files
capabilities/         # Saved capability artifacts
evidence/             # Discovery + replay evidence logs
tests/                # pytest test suite
```

## Running Tests

```bash
uv run pytest tests/ -v
```

## Demo Path

### 1. Discovery Run (LLM-driven)
```bash
uv run cua discover "Look up member M1001 and read their savings balance" \
    --target http://localhost:5001
```

### 2. Replay — Happy Path
```bash
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M1001"}'
```

### 3. Replay — Business Outcome (member not found)
```bash
uv run cua replay capabilities/lookup_member_balance.yaml \
    --params '{"member_id": "M9999"}'
```

### 4. List Capabilities
```bash
uv run cua list
```

### 5. Show Capability Details
```bash
uv run cua show capabilities/lookup_member_balance.yaml
```

## Configuration

| Environment Variable | Description | Required |
|---------------------|-------------|----------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude | Yes (discovery only) |

Replay does not require an API key — it runs deterministically without the LLM.

## Dependencies

- **Python 3.13** — runtime
- **Anthropic Claude** (claude-sonnet-4-20250514) — discovery agent loop
- **Playwright** — browser automation
- **Pydantic** — all data models
- **Flask** — mock banking app
- **sentence-transformers** — local embeddings for fingerprinting (optional)
- **structlog** — structured logging
