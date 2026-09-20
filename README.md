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

Five original features not requested by the assignment — each solves a real production problem:

1. **Semantic Element Fingerprinting** — Embedding-based self-healing locators using sentence-transformers. When exact locators fail, find the matching element via cosine similarity without LLM calls (~5ms vs ~500ms API). Works offline/air-gapped. (`src/cua/models/fingerprint.py`, `src/cua/replay/locator_resolver.py`)

2. **Execution Trace Anomaly Detection** — Statistical profiling of replay behavior (timing distributions, element counts, value ranges). Detects "succeeded incorrectly" — catches silent failures that pass all checkpoints but produce wrong data. Critical for banking compliance. (`src/cua/observability/anomaly.py`)

3. **Capability Composition (DAG Workflows)** — Chain capabilities into directed acyclic graphs with typed data flow, conditional branching, and compensating actions for rollback. Topological sort validates at definition time. (`src/cua/models/workflow.py`, `src/cua/orchestration/engine.py`)

4. **Visual Regression Detection** — Perceptual hashing (aHash + dHash) detects unexpected UI changes between replay runs without pixel-perfect comparison. Flags anomalies when a page's visual fingerprint drifts beyond a configurable threshold. Zero external dependencies — pure Python implementation. (`src/cua/observability/visual_regression.py`)

5. **Tamper-Evident Audit Trail** — Cryptographic hash chain (SHA-256) for every action during discovery and replay. Each entry chains to the previous via its hash — any modification breaks the chain. Supports SOX/FFIEC compliance requirements for financial automation audit logs. Save/load with integrity verification. (`src/cua/observability/audit_trail.py`)

6. **TF-IDF Capability Discovery** — Natural language search over the capability registry using TF-IDF scoring. AI agents can find capabilities by intent ("check member savings balance") rather than knowing exact names. Zero ML dependencies — pure Python tokenizer + IDF weighting. (`src/cua/orchestration/capability_registry.py`)

7. **Adaptive Timing Prediction** — Exponential moving average profiling of step execution times across runs. Computes adaptive timeouts (tighter than static defaults), detects degrading steps, and generates health reports. Prevents both premature timeouts and unbounded waits. (`src/cua/replay/adaptive_timing.py`)

8. **Auto-Healing Locators** — When a primary locator fails but a fallback succeeds, the system records the "heal". After N consistent heals (configurable threshold), it automatically updates the capability YAML to promote the working strategy. Turns locator maintenance from manual to automated. (`src/cua/replay/auto_healer.py`)

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
│   ├── locator_resolver.py # Multi-tier locator resolution
│   ├── adaptive_timing.py  # EMA-based timing prediction
│   └── auto_healer.py      # Self-updating locator strategies
├── safety/           # Guardrails
│   ├── guard.py      # Policy enforcement
│   └── redactor.py   # PII redaction
├── escalation/       # Human-in-the-loop
│   └── handoff.py    # CDP session handoff
├── observability/    # Logging + anomaly detection
│   ├── logger.py     # Structured logging
│   ├── anomaly.py    # Execution anomaly detection (B2)
│   ├── visual_regression.py # Perceptual hash visual diffing
│   └── audit_trail.py      # Cryptographic hash chain audit log
├── orchestration/    # Workflow engine
│   ├── engine.py     # DAG executor (B3)
│   └── capability_registry.py # TF-IDF capability search
└── cli.py            # CLI entry point

mock_bank_app/        # Legacy banking app (Flask, intentionally hostile HTML)
policies/             # Safety policy YAML files
capabilities/         # Saved capability artifacts
evidence/             # Discovery + replay evidence logs
tests/                # pytest test suite
```

## Running Tests

```bash
uv run pytest tests/ -v   # 105 tests across 12 test files
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
- **Anthropic Claude** (claude-haiku-4-5-20251001) — discovery agent loop
- **Playwright** — browser automation
- **Pydantic** — all data models
- **Flask** — mock banking app
- **sentence-transformers** — local embeddings for fingerprinting (optional)
- **structlog** — structured logging
