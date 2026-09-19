"""Execution models — run results, step traces, and the three-tier error taxonomy.

The error taxonomy is a core design decision:

1. BusinessOutcome — The operation completed but produced an alternative result
   that the caller needs to know about. Example: "member not found" is not a
   failure; it's a legitimate answer. The caller decides what to do with it.

2. RecoverableCondition — A transient or known interstitial that the replay
   engine can handle automatically. Example: dismiss a cookie banner, retry
   after a brief timeout, wait for a slow load. The run continues.

3. HardFailure — An unrecoverable state. The element wasn't found after all
   locator strategies, the page is in an unexpected state, or the app returned
   an unhandled error. The run stops and reports a debuggable error.

This separation matters in banking: conflating "account doesn't exist" with
"the system crashed" is a compliance incident. The caller's downstream logic
depends on knowing the difference.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    """Top-level outcome of a capability run."""

    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"  # completed, but with an alternative result
    FAILURE = "failure"
    ESCALATED = "escalated"  # paused for human intervention
    TIMEOUT = "timeout"


class ErrorCategory(StrEnum):
    """Three-tier error classification."""

    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


# ---------------------------------------------------------------------------
# Error detail models
# ---------------------------------------------------------------------------


class BusinessOutcome(BaseModel):
    """An expected alternative result — not a failure.

    Example: searching for a non-existent member returns "no results found."
    The automation succeeded (it searched correctly), but the business result
    is different from the happy path. The caller needs this information.
    """

    model_config = ConfigDict(extra="forbid")

    outcome_name: str = Field(description="Machine-readable name (e.g. 'member_not_found')")
    message: str = Field(description="Human-readable description")
    data: dict[str, Any] = Field(default_factory=dict, description="Any extracted data from the outcome")


class RecoverableCondition(BaseModel):
    """A transient condition that was detected and handled automatically."""

    model_config = ConfigDict(extra="forbid")

    condition_name: str
    message: str
    recovery_action: str = Field(description="What the engine did to recover (e.g. 'dismissed dialog', 'retried')")
    attempts: int = Field(default=1)


class HardFailure(BaseModel):
    """An unrecoverable error — the run cannot continue."""

    model_config = ConfigDict(extra="forbid")

    error_type: str = Field(description="Category (e.g. 'element_not_found', 'unexpected_page', 'timeout')")
    message: str
    step_id: str | None = Field(default=None, description="Which step failed")
    expected: str | None = Field(default=None, description="What was expected")
    observed: str | None = Field(default=None, description="What was actually found")
    screenshot_path: str | None = Field(default=None, description="Path to failure screenshot")
    dom_snapshot_path: str | None = Field(default=None, description="Path to DOM/a11y snapshot at failure")


# ---------------------------------------------------------------------------
# Step-level trace and result
# ---------------------------------------------------------------------------


class StepTrace(BaseModel):
    """Detailed execution trace for a single step — used for observability and anomaly detection."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    started_at: datetime
    completed_at: datetime
    wall_time_ms: float
    action: str
    locator_strategy_used: str | None = Field(
        default=None,
        description="Which locator strategy succeeded (e.g. 'accessibility', 'text_content')",
    )
    locator_attempts: int = Field(default=1, description="How many strategies were tried")
    page_url: str | None = None
    page_title: str | None = None
    extracted_values: dict[str, Any] = Field(default_factory=dict)
    screenshot_path: str | None = None
    recoveries: list[RecoverableCondition] = Field(default_factory=list)


class StepResult(BaseModel):
    """Outcome of executing a single step."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    success: bool
    trace: StepTrace
    error: HardFailure | None = None
    business_outcome: BusinessOutcome | None = None


# ---------------------------------------------------------------------------
# Run-level result
# ---------------------------------------------------------------------------


class RunResult(BaseModel):
    """Complete result of a capability run (discovery or replay).

    This is the contract between the replay engine and its callers.
    A caller inspects `status` first:
    - SUCCESS → read `outputs` for extracted data
    - BUSINESS_OUTCOME → read `business_outcome` for the alternative result
    - FAILURE → read `failure` for debuggable error details
    - ESCALATED → the run is paused, awaiting human intervention
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    capability_id: str
    capability_version: int
    status: RunStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_ms: float | None = None

    # --- Outputs (on success) ---
    outputs: dict[str, Any] = Field(default_factory=dict, description="Extracted output values")

    # --- Error details (mutually exclusive by status) ---
    business_outcome: BusinessOutcome | None = None
    failure: HardFailure | None = None

    # --- Per-step detail ---
    step_results: list[StepResult] = Field(default_factory=list)
    total_steps: int = 0
    completed_steps: int = 0

    # --- Input parameters (redacted) ---
    input_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Input params as provided (sensitive values redacted)",
    )

    # --- Anomaly detection (beyond-scope B2) ---
    anomalies: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Anomalies detected during this run",
    )

    def is_success(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def is_business_outcome(self) -> bool:
        return self.status == RunStatus.BUSINESS_OUTCOME

    def summary(self) -> str:
        """One-line summary for logging."""
        if self.status == RunStatus.SUCCESS:
            return f"SUCCESS: {self.completed_steps}/{self.total_steps} steps, outputs={list(self.outputs.keys())}"
        if self.status == RunStatus.BUSINESS_OUTCOME and self.business_outcome:
            return f"BUSINESS_OUTCOME: {self.business_outcome.outcome_name} — {self.business_outcome.message}"
        if self.status == RunStatus.FAILURE and self.failure:
            return f"FAILURE at step {self.failure.step_id}: {self.failure.error_type} — {self.failure.message}"
        if self.status == RunStatus.ESCALATED:
            return f"ESCALATED at step {self.completed_steps}/{self.total_steps}"
        return f"{self.status}: {self.completed_steps}/{self.total_steps} steps completed"
