"""Capability artifact schema — the core data model of the CUA system.

A Capability is a typed, versioned, parameterized description of a UI workflow
that was discovered by an LLM and can be replayed deterministically without one.
It is the contract between the discovery agent and the replay engine, and between
the automation system and its callers (AI agents, orchestrators, humans).

Design principles:
- Every interacted element has a multi-strategy locator with a fallback chain.
  The primary strategy targets accessibility semantics (role + name), which are
  more stable than DOM selectors on legacy surfaces and generalize to desktop
  apps via OS accessibility APIs.
- Input parameters are typed and declared up front so callers know the contract.
  Sensitive parameters (credentials, PII) are flagged for redaction.
- Output fields declare what data the capability extracts and which step does it.
- Error handlers are first-class: the capability declares what runtime conditions
  it knows about and how to respond (retry, skip, escalate, or report as a
  business outcome).
- The artifact is serialized to YAML for human reviewability and versioned with
  a monotonic integer so diffs between versions are meaningful.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LocatorType(StrEnum):
    """How an element is identified on a surface.

    Ordered from most stable/semantic to most fragile/structural.
    The replay engine tries strategies in this priority order.
    """

    ACCESSIBILITY = "accessibility"  # a11y role + name (e.g. role=textbox, name="Member ID")
    TEXT_CONTENT = "text_content"  # visible text match (exact or contains)
    LABEL_PROXIMITY = "label_proximity"  # element adjacent to a label with given text
    CSS_SELECTOR = "css_selector"  # CSS selector (fragile on legacy surfaces)
    XPATH = "xpath"  # XPath expression (positional fallback)
    SEMANTIC_FINGERPRINT = "semantic_fingerprint"  # embedding-based match (see fingerprint.py)


class ActionType(StrEnum):
    """Atomic action types the agent can perform on a surface."""

    CLICK = "click"
    TYPE = "type"  # type text into an input
    CLEAR = "clear"  # clear an input field
    SELECT = "select"  # select a dropdown option
    NAVIGATE = "navigate"  # go to a URL
    WAIT = "wait"  # wait for a condition
    EXTRACT = "extract"  # read data from the page
    ASSERT = "assert"  # verify a condition holds
    SCROLL = "scroll"  # scroll to make element visible
    PRESS_KEY = "press_key"  # press a keyboard key (Enter, Tab, etc.)


class RiskLevel(StrEnum):
    """Risk classification for an action.

    Determines how the safety guard treats the action:
    - SAFE: read-only, always allowed (navigate, extract, assert)
    - CAUTIOUS: input that changes form state but is not submitted (type, select)
    - RISKY: irreversible or side-effecting (submit, delete, transfer, confirm)
    """

    SAFE = "safe"
    CAUTIOUS = "cautious"
    RISKY = "risky"


class ErrorPolicy(StrEnum):
    """What to do when a step or error handler condition is triggered."""

    FAIL = "fail"  # stop the run, report hard failure
    RETRY = "retry"  # retry the step with backoff
    SKIP = "skip"  # skip this step, continue
    ESCALATE = "escalate"  # pause and request human intervention
    REPORT_OUTCOME = "report_outcome"  # report as a business outcome (not a failure)


class CheckpointType(StrEnum):
    """How to verify that a step achieved its expected effect."""

    URL_MATCH = "url_match"  # current URL matches a pattern
    ELEMENT_VISIBLE = "element_visible"  # an element is present and visible
    ELEMENT_ABSENT = "element_absent"  # an element is NOT present
    TEXT_PRESENT = "text_present"  # specific text is visible on the page
    TEXT_ABSENT = "text_absent"  # specific text is NOT visible
    PAGE_TITLE = "page_title"  # page title matches


class ApprovalState(StrEnum):
    """Lifecycle state of a capability artifact.

    - DRAFT: recorded but not yet validated by successful replays
    - VALIDATED: has passed N successful replays, eligible for review
    - APPROVED: reviewed and approved for unattended production use
    - DEPRECATED: superseded by a newer version, should not be used
    """

    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


# ---------------------------------------------------------------------------
# Locator and targeting
# ---------------------------------------------------------------------------


class LocatorStrategy(BaseModel):
    """A single strategy for finding an element on a surface.

    The replay engine tries strategies in the order they appear in the
    ElementTarget.strategies list. Each strategy has a confidence score
    from recording (how uniquely it identified the element during discovery)
    to help prioritize and to inform fallback decisions.
    """

    model_config = ConfigDict(extra="forbid")

    type: LocatorType
    value: str = Field(description="The locator value — interpretation depends on type")
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Additional attributes for compound locators (e.g. role + name for a11y)",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="How reliably this locator identified the element during recording",
    )


class ElementTarget(BaseModel):
    """Multi-strategy locator for a UI element.

    Encapsulates *how* an element is found, decoupled from *what* action is
    performed on it. The strategies list is ordered by preference (most
    stable first). During replay, the locator resolver tries each strategy
    until one succeeds or all are exhausted.

    The `description` field is the LLM's semantic understanding of what this
    element is — useful for human review and for the semantic fingerprint
    fallback.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="Human-readable description (e.g. 'Member ID search field')")
    strategies: list[LocatorStrategy] = Field(min_length=1)
    fingerprint_id: str | None = Field(
        default=None,
        description="Reference to a semantic fingerprint in the companion fingerprints file",
    )


# ---------------------------------------------------------------------------
# Steps, checkpoints, and error handlers
# ---------------------------------------------------------------------------


class Checkpoint(BaseModel):
    """A verifiable condition that confirms the UI reached an expected state.

    Checkpoints are the mechanism that makes replay trustworthy: rather than
    assuming a click worked, we assert the expected effect. Failed checkpoints
    are the primary signal that something went wrong.
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    type: CheckpointType
    value: str = Field(description="The expected value (URL pattern, text, element description)")
    timeout_ms: int = Field(default=5_000, description="How long to wait for the condition")


class ErrorHandler(BaseModel):
    """A declared handler for a known runtime condition.

    Error handlers let the capability express domain knowledge: "if the page
    shows 'Member not found', that's a business outcome, not a crash" or
    "if a session timeout dialog appears, dismiss it and retry."

    Detection uses the same checkpoint mechanism — the condition is a
    Checkpoint that, when matched, triggers the handler's action.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Identifier for this handler (e.g. 'member_not_found')")
    description: str
    detection: Checkpoint = Field(description="How to detect this condition")
    action: ErrorPolicy
    message_template: str = Field(
        description="Message to include in the result when triggered. May reference outputs via {name}."
    )
    max_retries: int = Field(default=3, description="For RETRY actions, how many attempts")


class Step(BaseModel):
    """A single atomic action in a capability flow.

    Steps are the unit of execution: each step targets one element, performs
    one action, and optionally verifies one checkpoint. The replay engine
    executes steps sequentially and reports per-step results.

    Parameters can reference input variables via {{param_name}} syntax in
    string values within the `parameters` dict.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique step identifier within this capability")
    description: str = Field(description="What this step does, in plain English")
    action: ActionType
    target: ElementTarget | None = Field(
        default=None,
        description="Element to act on. None for navigate, wait, or page-level actions.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific parameters. May contain {{input_param}} references.",
    )
    checkpoint: Checkpoint | None = Field(
        default=None,
        description="Post-action assertion. If present and fails, the step fails.",
    )
    error_handlers: list[ErrorHandler] = Field(
        default_factory=list,
        description="Step-level error handlers, checked before capability-level handlers.",
    )
    timeout_ms: int = Field(default=10_000, description="Max time to complete this step")
    risk_level: RiskLevel = Field(default=RiskLevel.SAFE)
    on_error: ErrorPolicy = Field(
        default=ErrorPolicy.FAIL,
        description="Default error policy if no handler matches",
    )
    retry_config: RetryConfig | None = None
    wait_before_ms: int = Field(default=0, description="Delay before executing (for debounce/animation)")


class RetryConfig(BaseModel):
    """Configuration for step retry behavior."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1)
    backoff_ms: int = Field(default=1_000, description="Initial backoff between retries")
    backoff_multiplier: float = Field(default=2.0, description="Exponential backoff factor")


# ---------------------------------------------------------------------------
# Capability contract: inputs and outputs
# ---------------------------------------------------------------------------


class ParameterSpec(BaseModel):
    """A typed input parameter for the capability.

    Callers must supply all required parameters. Sensitive parameters
    are redacted in logs and never persisted in artifacts.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = Field(description="JSON Schema type: string, integer, number, boolean")
    description: str
    required: bool = True
    default: Any = None
    sensitive: bool = Field(
        default=False,
        description="If true, value is redacted in logs and evidence (e.g. SSN, account number)",
    )
    validation_pattern: str | None = Field(
        default=None,
        description="Optional regex pattern for input validation",
    )


class OutputSpec(BaseModel):
    """A typed output field that the capability extracts.

    Each output is tied to a specific extraction step and declares
    its expected type. The replay engine populates these from the
    step results and returns them in the RunResult.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = Field(description="JSON Schema type of the extracted value")
    description: str
    extraction_step_id: str = Field(description="Which step extracts this value")
    sensitive: bool = Field(default=False, description="If true, value is redacted in logs")


# ---------------------------------------------------------------------------
# Surface targeting and multi-tenant support
# ---------------------------------------------------------------------------


class SurfaceTarget(BaseModel):
    """Describes the target surface (application) this capability operates on.

    The `surface_type` field enables the surface adapter abstraction:
    the same artifact schema works for web, legacy web, and desktop surfaces
    by swapping the adapter implementation.

    For multi-tenant reuse, `app_id` identifies the vendor product (e.g.
    "fiserv-dna") while `tenant_id` identifies the specific instance.
    A base capability with tenant_id=None applies to all tenants running
    that app; tenant-specific overrides reference the base via `base_capability_id`.
    """

    model_config = ConfigDict(extra="forbid")

    surface_type: str = Field(default="web", description="web | legacy_web | desktop")
    entry_point: str = Field(description="URL, app path, or executable path")
    app_id: str | None = Field(default=None, description="Vendor product identifier for multi-tenant reuse")
    tenant_id: str | None = Field(default=None, description="Specific tenant/institution identifier")
    app_version: str | None = Field(default=None, description="Known app version at recording time")


# ---------------------------------------------------------------------------
# Confidence scoring (stretch goal: approval gating)
# ---------------------------------------------------------------------------


class ConfidenceScore(BaseModel):
    """Quantified confidence in a capability's reliability.

    Computed from replay history, locator diversity, and error handler coverage.
    Used to gate unattended production execution: only APPROVED capabilities
    with score >= threshold run autonomously.
    """

    model_config = ConfigDict(extra="forbid")

    overall: float = Field(ge=0.0, le=1.0)
    locator_diversity: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of steps with >= 2 locator strategies",
    )
    replay_success_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="Successful replays / total replay attempts",
    )
    error_handler_coverage: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of known error conditions with handlers",
    )
    total_replays: int = Field(default=0)
    last_success_at: datetime | None = None


# ---------------------------------------------------------------------------
# The Capability artifact
# ---------------------------------------------------------------------------


class Capability(BaseModel):
    """A recorded, reusable, agent-invocable UI automation capability.

    This is the central artifact of the CUA system. It captures everything
    needed to replay a UI workflow deterministically:

    - WHO can call it: any AI agent or orchestrator that supplies the input params
    - WHAT it does: ordered steps with actions, targets, and checkpoints
    - WHERE it runs: the target surface (app, URL, tenant)
    - WHAT it needs: typed input parameters with validation
    - WHAT it returns: typed output fields extracted during execution
    - WHAT can go wrong: error handlers for known runtime conditions
    - HOW MUCH to trust it: confidence score and approval state

    The artifact is serialized to YAML for human reviewability and stored
    alongside optional companion files (fingerprints, anomaly profiles).
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identity ---
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version: int = Field(default=1, ge=1)
    name: str = Field(description="Machine-friendly name (e.g. 'lookup_member_balance')")
    description: str = Field(description="Human-readable description of what this capability does")

    # --- Surface target ---
    surface: SurfaceTarget

    # --- Contract ---
    input_parameters: list[ParameterSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)

    # --- Flow ---
    steps: list[Step] = Field(min_length=1)

    # --- Success condition ---
    success_condition: Checkpoint = Field(
        description="The final checkpoint that confirms the goal was achieved"
    )

    # --- Error handling ---
    error_handlers: list[ErrorHandler] = Field(
        default_factory=list,
        description="Capability-level error handlers (checked after step-level handlers)",
    )

    # --- Lifecycle & trust ---
    approval_state: ApprovalState = Field(default=ApprovalState.DRAFT)
    confidence: ConfidenceScore | None = None

    # --- Provenance ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_from_run_id: str | None = Field(
        default=None,
        description="ID of the discovery run that produced this artifact",
    )

    # --- Multi-tenant ---
    base_capability_id: str | None = Field(
        default=None,
        description="For tenant-specific overrides, the base capability this derives from",
    )
    tags: list[str] = Field(default_factory=list)

    def get_step(self, step_id: str) -> Step | None:
        """Look up a step by its ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def resolve_parameters(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Validate and resolve input parameters against the declared spec.

        Raises ValueError if a required parameter is missing or if a value
        fails validation_pattern matching.
        """
        import re

        resolved: dict[str, Any] = {}
        for spec in self.input_parameters:
            if spec.name in inputs:
                value = inputs[spec.name]
                if spec.validation_pattern and isinstance(value, str):
                    if not re.match(spec.validation_pattern, value):
                        msg = f"Parameter '{spec.name}' value '{value}' does not match pattern '{spec.validation_pattern}'"
                        raise ValueError(msg)
                resolved[spec.name] = value
            elif spec.required:
                if spec.default is not None:
                    resolved[spec.name] = spec.default
                else:
                    msg = f"Required parameter '{spec.name}' not provided"
                    raise ValueError(msg)
            elif spec.default is not None:
                resolved[spec.name] = spec.default
        return resolved

    def interpolate_value(self, template: str, params: dict[str, Any]) -> str:
        """Replace {{param_name}} placeholders with actual parameter values."""
        import re

        def replacer(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key in params:
                return str(params[key])
            return match.group(0)

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)
