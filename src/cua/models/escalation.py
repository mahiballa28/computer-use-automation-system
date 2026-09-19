"""Escalation models — intervention requests and session handoff state.

When the system can't safely proceed, it must bring a human into the loop.
This module defines the data models for that handoff:

- InterventionRequest: carries enough context for a human to understand
  what happened and what to do (which capability, which step, current state,
  why it stopped).

- SessionState: tracks who is in control of the live browser session
  (automation or human) and the handoff protocol.

The handoff mechanism uses Chrome DevTools Protocol (CDP) remote debugging:
the automation pauses, exposes the browser's CDP endpoint, and the human
connects to the same browser session (not a new one). When done, they signal
resume and the automation picks up from the current state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SessionController(StrEnum):
    """Who currently controls the live browser session."""

    AUTOMATION = "automation"
    HUMAN = "human"
    PAUSED = "paused"  # transitioning, nobody is acting


class EscalationReason(StrEnum):
    """Why the system is requesting human intervention."""

    STUCK = "stuck"  # agent can't make progress
    ELEMENT_NOT_FOUND = "element_not_found"  # all locator strategies exhausted
    UNEXPECTED_STATE = "unexpected_state"  # page is in a state no handler covers
    RISKY_ACTION = "risky_action"  # action requires human confirmation
    MAX_RETRIES = "max_retries"  # retry limit exceeded
    POLICY_VIOLATION = "policy_violation"  # action would violate safety policy
    EXPLICIT_ESCALATION = "explicit_escalation"  # step is configured to always escalate


class InterventionRequest(BaseModel):
    """A request for human intervention in a running automation session.

    Contains enough context for the operator to understand the situation
    and take action without needing to reconstruct what happened.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    capability_id: str
    capability_name: str

    # Where we stopped
    current_step_id: str
    current_step_description: str
    completed_steps: int
    total_steps: int

    # Why we stopped
    reason: EscalationReason
    reason_detail: str

    # Current state for the operator
    current_url: str | None = None
    screenshot_path: str | None = None
    page_title: str | None = None

    # Connection info
    cdp_endpoint: str | None = Field(
        default=None,
        description="Chrome DevTools Protocol WebSocket URL for connecting to the live session",
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class HumanAction(BaseModel):
    """Records what a human did during their control of the session.

    Preserves evidence across the handoff so the audit trail is complete.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action_type: str = Field(description="click, type, navigate, or 'custom' for complex actions")
    description: str = Field(description="What the human did, in plain English")
    url_before: str | None = None
    url_after: str | None = None
    screenshot_path: str | None = None


class SessionState(BaseModel):
    """Tracks the current state of the automation/human handoff.

    The lifecycle:
    1. AUTOMATION: automation is executing steps normally
    2. → PAUSED: automation detects a stuck/escalation condition, stops acting
    3. → HUMAN: operator connects via CDP, takes control
    4. → PAUSED: operator signals they're done
    5. → AUTOMATION: automation resumes from current state

    Both directions preserve context: the intervention request carries state
    forward to the human, and the human's actions are recorded for the
    automation's audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    controller: SessionController = Field(default=SessionController.AUTOMATION)
    intervention: InterventionRequest | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)

    def request_handoff(self, intervention: InterventionRequest) -> None:
        """Transition from AUTOMATION to PAUSED, preparing for human takeover."""
        self.controller = SessionController.PAUSED
        self.intervention = intervention

    def accept_handoff(self) -> None:
        """Human takes control of the session."""
        self.controller = SessionController.HUMAN

    def return_control(self, operator_id: str | None = None) -> None:
        """Human returns control to automation."""
        self.controller = SessionController.PAUSED
        if self.intervention:
            self.intervention.resolved_at = datetime.now(timezone.utc)
            self.intervention.resolved_by = operator_id

    def resume_automation(self) -> None:
        """Automation resumes execution."""
        self.controller = SessionController.AUTOMATION

    def record_human_action(self, action: HumanAction) -> None:
        """Record an action taken by the human operator."""
        self.human_actions.append(action)
