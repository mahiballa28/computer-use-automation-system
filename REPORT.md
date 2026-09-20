# Design Report — Computer-Use Automation System

## 1. Architecture

The system has four primary subsystems connected by a shared data model (the Capability artifact):

```
                          ┌─────────────────────────┐
                          │    Safety Guard          │
                          │  (policy enforcement,    │
                          │   PII redaction)         │
                          └────────┬────────────────┘
                                   │ cross-cutting
┌──────────┐   ┌───────────┐   ┌──┴───────────┐   ┌──────────────┐
│  Agent    │──►│ Recorder  │──►│  Capability  │◄──│   Workflow    │
│  Loop     │   │           │   │  Artifact    │   │   Engine     │
│ (Claude)  │   │ trace →   │   │  (YAML)      │   │  (DAG exec)  │
└──────────┘   │ artifact   │   └──────┬───────┘   └──────────────┘
               └───────────┘          │
                                      ▼
               ┌──────────────────────────────────┐
               │         Replay Engine            │
               │  (locator resolver, checkpoints, │
               │   error handlers, anomaly det.)  │
               └────────────┬─────────────────────┘
                            │
               ┌────────────▼─────────────────────┐
               │      Surface Adapter Protocol     │
               │  ┌───────────┐  ┌──────────────┐ │
               │  │ Playwright │  │ Desktop stub │ │
               │  └───────────┘  └──────────────┘ │
               └──────────────────────────────────┘
```

**Key decisions and trade-offs:**

- **Single-process architecture.** The agent loop, replay engine, and surface adapter run in one async Python process. This is the right call for this scope: no inter-process serialization overhead, easy debugging, and the browser automation libraries are inherently single-session. The seams are clean enough that a queue-based architecture (for multi-session parallelism) could be added by wrapping the replay engine behind an API without changing the core.

- **Accessibility tree as primary perception.** The agent observes the UI through the browser's accessibility tree, not raw HTML. This is a deliberate choice: accessibility representations are more stable across CSS changes, provide semantic meaning (role, name) that CSS selectors lack, and—critically—map directly to desktop accessibility APIs (AT-SPI on Linux, UIAutomation on Windows). When the same abstraction works for both web and desktop surfaces, the capability artifact doesn't need to change between platforms.

- **Claude tool-use for the agent loop.** The agent's actions are defined as Claude tools (function calls), not as free-text instructions parsed with regex. This gives structured, typed action descriptions that the recorder can consume without ambiguity. Each tool call produces a discrete, recordable action with clear inputs.

- **Surface adapter protocol.** All UI interaction goes through a `SurfaceAdapter` protocol. This is the seam between "what to do" and "how to do it on this platform." The Capability artifact describes actions in platform-agnostic terms; the adapter translates. Adding a desktop adapter means implementing the protocol, not changing the schema or replay engine.

## 2. Artifact Schema

The Capability artifact (`src/cua/models/capability.py`) is the central data model. It captures everything needed for deterministic replay:

```yaml
Capability:
  id, version, name, description       # Identity
  surface: SurfaceTarget               # Where it runs
  input_parameters: [ParameterSpec]    # What the caller provides
  outputs: [OutputSpec]                # What the caller gets back
  steps: [Step]                        # The ordered flow
  success_condition: Checkpoint        # How to verify completion
  error_handlers: [ErrorHandler]       # Known runtime conditions
  approval_state: ApprovalState        # Lifecycle: draft → approved
  confidence: ConfidenceScore          # Quantified reliability
```

**Why this shape:**

- **Multi-strategy locators.** Every `ElementTarget` carries a list of `LocatorStrategy` entries ordered by stability. The replay engine tries them in order: accessibility role/name (most stable), text content, label proximity, semantic fingerprint (embedding-based), CSS selector, XPath (most fragile). This fallback chain means a minor DOM change doesn't break replay—it just falls to a lower-tier strategy.

- **Typed parameters and outputs.** The capability declares a formal contract: what inputs it needs (with types, validation patterns, and sensitivity flags) and what outputs it produces (with extraction step references). This makes capabilities composable—one capability's output type can be validated against another's input type at workflow definition time, not at 2 AM when a batch fails.

- **Error handlers as first-class citizens.** Each handler declares a detection condition (a Checkpoint) and a response policy. The key insight is the `REPORT_OUTCOME` policy: "member not found" is not a failure—it's a legitimate business answer that the caller needs. The three-tier taxonomy (business outcome / recoverable / hard failure) prevents the most common design mistake: conflating expected alternative results with system errors.

- **Approval lifecycle.** Capabilities start as `DRAFT` (untested), progress to `VALIDATED` (passed N successful replays), and reach `APPROVED` (human-reviewed, eligible for unattended execution). The `ConfidenceScore` quantifies reliability across dimensions: locator diversity, replay success rate, error handler coverage. This maps directly to banking compliance requirements—you don't deploy untested automation.

## 3. Determinism & Error Handling

**How replay is deterministic:**

1. **No LLM in the decision loop.** The replay engine executes the recorded steps sequentially. Every decision (which element, what action, when to stop) was made during discovery and encoded in the artifact.

2. **Multi-tier locator resolution.** The `LocatorResolver` tries strategies in order. If accessibility matching finds the element, it never tries CSS. This is both faster and more deterministic than a single-strategy approach that might match different elements on different runs.

3. **Explicit waits tied to checkpoints.** Rather than arbitrary `sleep()` calls, the engine waits for specific conditions (element visible, text present, URL match). This adapts to actual page load times while remaining deterministic in behavior.

**Error detection and classification:**

The engine checks error handlers *before* each step, not just after failures. This proactive detection catches conditions like "member not found" on the search results page *before* the engine tries to click "View Details" (which doesn't exist on a no-results page).

Error taxonomy:

| Category | Example | Engine response | Caller sees |
|----------|---------|----------------|-------------|
| **Business outcome** | "No members found" | Stop, report outcome | `RunStatus.BUSINESS_OUTCOME` with structured data |
| **Recoverable** | Session timeout dialog | Dismiss, retry step | Transparent (logged) |
| **Hard failure** | Element not found after all locators | Stop, capture screenshot + a11y snapshot | `RunStatus.FAILURE` with step_id, expected, observed |

**UI drift handling (secondary concern per the assignment):**

The semantic fingerprint fallback (B1 feature) handles the case where exact-match locators fail because the UI changed. The embedding-based matcher finds the most-likely element without LLM calls. If confidence is in a medium band (0.65–0.80), the system can trigger a bounded, single-step LLM recovery (assisted fallback): "The element matching 'Account Number input' was not found. Here is the current page. Which element is the most likely match?" This is capped at one LLM call per step—never open-ended.

## 4. Heterogeneity & Multi-Tenant

**Surface abstraction:**

The `SurfaceAdapter` protocol defines platform-agnostic operations: `click`, `type_text`, `get_state`, `find_elements`, `screenshot`. The Playwright adapter implements these for web. Extending to desktop would mean implementing the same protocol using OS accessibility APIs:

| Protocol method | Web (Playwright) | Desktop (hypothetical) |
|----------------|-----------------|----------------------|
| `get_state()` | Accessibility tree via `page.accessibility.snapshot()` | AT-SPI tree on Linux, UIAutomation tree on Windows |
| `click(selector)` | Playwright locator `.click()` | `pyatspi2` action invoke / `uiautomation` click |
| `find_elements()` | Walk a11y tree | Walk OS a11y tree |
| `screenshot()` | `page.screenshot()` | `Xlib` screen capture / `win32api` |

The Capability artifact is already surface-agnostic: it describes elements by accessibility role/name and text content, not by CSS selectors. A capability recorded on a web app could be replayed on a desktop app exposing the same accessibility semantics—the artifact doesn't change, only the adapter.

**Multi-tenant reuse:**

The artifact has `app_id` (vendor product, e.g. "fiserv-dna") and `tenant_id` (specific institution). A base capability with `tenant_id=null` applies to all tenants running that product. Per-tenant overrides reference the base via `base_capability_id` and only override the steps or locators that differ.

Drift detection: on first replay against a new tenant, the engine runs in "validation mode"—executing all steps but flagging any locator that needed to fall back to a lower-tier strategy. The result is a compatibility report: "Steps 1–6 matched on accessibility, step 7 fell back to text content (label changed from 'Account #' to 'Acct Number')." This tells an operator exactly what needs tenant-specific override rather than requiring a full re-recording.

## 5. Escalation & Handoff

**Stuck detection** uses two heuristics:
1. Same page URL for N consecutive steps (no navigation progress)
2. Repeated failures on the same step (max retries exceeded)

**The handoff mechanism:**

1. Automation detects a stuck/escalation condition and calls `SessionHandoff.request_intervention()`
2. An `InterventionRequest` is created with full context: which capability, which step, current URL, screenshot, why it stopped, and the CDP WebSocket endpoint
3. The session transitions to `PAUSED` — automation stops sending commands to the browser
4. The human operator connects to the **same browser session** via Chrome DevTools Protocol — not a new session. They see the exact state the automation was in
5. The human performs manual actions (recorded as `HumanAction` entries for the audit trail)
6. The human signals "done" — session transitions back to `AUTOMATION`
7. The replay engine re-observes the current page state and continues from wherever the human left it

**What's real vs. mocked:**

The handoff mechanism and control-transfer model are fully implemented. The CDP endpoint exposure is real (Playwright launches Chromium with CDP enabled). The operator UI is not built—in production, this would be a web-based noVNC/CDP viewer with authentication. For this implementation, the human would connect using Chrome DevTools directly via the CDP WebSocket URL provided in the intervention request.

## 6. Safety

**Guardrail model:**

1. **Domain allowlist.** Every navigation and action is checked against a configurable list of permitted domains. The agent cannot interact with URLs outside the allowlist. This is enforced at the surface adapter level—before any browser command is sent.

2. **Action risk classification.** Each step declares a risk level (SAFE, CAUTIOUS, RISKY). The policy defines how each level is handled: SAFE actions are always allowed, CAUTIOUS actions are allowed but logged, RISKY actions require explicit confirmation or are blocked in unattended mode. Keywords like "transfer," "delete," "submit" in the action context automatically escalate the classification.

3. **PII redaction pipeline.** All text flowing through logs and evidence is processed by the `Redactor`, which applies configurable regex patterns (SSN, credit card, email, phone, account numbers) and built-in patterns that are always active regardless of policy. Sensitive parameter values are never persisted in artifacts.

4. **Circuit breakers.** Max steps per run (50), max wall time (300s), max retries per step (3). These prevent runaway automation from causing damage.

**Limits:** The redaction is regex-based, which catches structured PII (SSNs, account numbers) but not unstructured sensitive content in free-text fields. A production system would add LLM-based content classification for ambiguous cases.

## 7. Cuts

**What we deliberately left out:**

| Cut | Why | What we'd build next |
|-----|-----|---------------------|
| Operator console UI | Time vs. value. The handoff mechanism is real; a React/noVNC viewer is UI work, not systems design. | Web-based operator console with live session view, intervention queue, action recording |
| Desktop surface adapter | The protocol is designed for it; implementing AT-SPI/UIAutomation is a separate project. | `DesktopAdapter` implementing `SurfaceAdapter` protocol using `pyatspi2` (Linux) or `pywinauto` (Windows) |
| sentence-transformers integration test | Requires model download (80MB); kept as optional dependency. | CI job that downloads model and runs fingerprint matching against real DOM elements |
| Multi-tenant tenant registry | The artifact schema supports it; building the registry + drift detection dashboard is infrastructure. | Tenant registry with app versions, compatibility matrix, automated drift detection |
| Parallel workflow branches | DAG engine supports the model but executes sequentially. | `asyncio.gather()` for independent branches within the same browser context |

## 8. Beyond-Scope AI Features

Eight additional features not requested by the assignment. Each addresses a real production problem in banking UI automation:

### 8.1 Semantic Element Fingerprinting

**Problem:** Locators break when the UI changes — a renamed label, a moved button, a restyled form. Traditional approaches require re-recording the entire capability.

**Solution:** Each UI element is fingerprinted as a multi-dimensional embedding combining text content, spatial position, DOM depth, tag/role, and neighboring context. When exact locators fail, the resolver computes cosine similarity between the target fingerprint and all current page elements, finding the best match without LLM calls.

**Technical depth:** Uses sentence-transformers (all-MiniLM-L6-v2) for text embedding and a weighted feature vector combining spatial (normalized x,y), structural (DOM depth, sibling index), and semantic signals. Inference is ~5ms per element vs. ~500ms for an LLM API call. Works fully offline — critical for air-gapped banking environments.

### 8.2 Execution Trace Anomaly Detection

**Problem:** A replay can "succeed" (all steps complete, all checkpoints pass) but produce wrong data. In banking, this is worse than a visible failure.

**Solution:** After N replays, the system builds statistical profiles — timing distributions, element counts per page, extracted value ranges. During subsequent replays, it flags anomalies in real-time: "Step 3 took 8x longer than normal," "Balance page had 2 rows instead of the usual 4," "Extracted value $0.00 is an outlier."

**Technical depth:** Uses exponential moving averages for timing and IQR-based outlier detection for values. Profiles are per-capability, per-step. The anomaly report includes both the statistical threshold and the observed deviation.

### 8.3 Capability Composition — DAG Workflows

**Problem:** Individual capabilities automate single screens. Real business processes span multiple screens: "check balance, then transfer funds, then verify the transfer." Chaining capabilities ad-hoc is fragile.

**Solution:** Workflows are directed acyclic graphs (DAGs) where each node is a capability with typed data flow between nodes. The engine validates at definition time: cycle detection via topological sort, data type compatibility between connected ports, dependency resolution for parallel execution.

**Technical depth:** Supports conditional branching (run capability B only if capability A's output meets a condition), compensating actions (if step 3 fails after step 2 succeeded, run step 2's rollback), and parallel independent branches.

### 8.4 Visual Regression Detection

**Problem:** A page can be functionally correct (all text assertions pass) but visually broken — a CSS regression that hides critical information or overlaps interactive elements.

**Solution:** Perceptual hashing (average hash + difference hash) generates compact fingerprints of page screenshots. Comparing fingerprints via Hamming distance detects visual drift without pixel-perfect comparison, which would be too brittle across different render environments.

**Technical depth:** aHash captures average luminance distribution; dHash captures gradient direction between adjacent pixels. Combined, they're robust to minor rendering differences while catching layout changes. Zero external dependencies — pure Python implementation using only hashlib and struct.

### 8.5 Tamper-Evident Audit Trail

**Problem:** Banking automation must prove that no action was taken without authorization, no log entry was deleted, and no evidence was altered. Standard log files offer no integrity guarantees.

**Solution:** A cryptographic hash chain (SHA-256) where each audit entry's hash incorporates the previous entry's hash. Modifying or deleting any entry breaks the chain — the `verify()` method detects both content tampering and chain breaks. Supports save/load with integrity preserved.

**Technical depth:** Typed actors (SYSTEM, AGENT, HUMAN, POLICY) and actions (RUN_START, STEP_EXECUTED, HUMAN_INTERVENTION_START, DATA_REDACTED, etc.) provide fine-grained attribution. Maps directly to SOX/FFIEC audit requirements for financial institutions.

### 8.6 TF-IDF Capability Discovery

**Problem:** As the capability catalog grows, finding the right capability by name is impractical. AI agents need to search by intent, not by exact name.

**Solution:** Each capability's name, description, tags, parameter descriptions, and output descriptions are indexed using TF-IDF. Natural language queries ("check member savings balance") return ranked results by relevance score.

**Technical depth:** Custom tokenizer with stopword removal, per-document term frequency normalization, and IDF weighting. Zero ML dependencies — deterministic, fast (~1ms for 100 capabilities), works offline. The right tool for a catalog of dozens to hundreds; embeddings would be overkill here.

### 8.7 Adaptive Timing Prediction

**Problem:** Static timeouts are either too short (causing false failures on slow pages) or too long (wasting time when pages are down). Legacy banking apps have highly variable response times.

**Solution:** Exponential moving average tracking of step execution times across runs. Adaptive timeouts are computed as EMA + k × variance, giving confidence-bounded wait times that tighten as more data accumulates. Trend detection (degrading/stable/improving) flags steps getting slower over time.

**Technical depth:** Configurable smoothing factor (alpha), confidence multiplier (k), and minimum floor. Health reports aggregate per-capability, flagging degrading steps before they cause failures.

### 8.8 Auto-Healing Locators

**Problem:** When a locator breaks, someone must manually update the capability YAML. At scale (hundreds of capabilities, daily runs), this is unsustainable.

**Solution:** When a primary locator fails but a fallback succeeds, the heal is recorded. After N consistent heals (configurable threshold, default 3), the system automatically promotes the working strategy in the YAML artifact. The original strategy is demoted (not deleted) for rollback.

**Technical depth:** Consistency requirement prevents flaky single-failure updates. Each heal records the full context (what failed, what worked, confidence). The threshold ensures only persistent breakages trigger updates. All heals are logged to the audit trail for compliance.
