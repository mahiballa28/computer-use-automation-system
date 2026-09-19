"""Safety guard — runtime policy enforcement.

The guard sits between the agent/replay engine and the surface adapter.
Every action passes through it before execution. It enforces:

1. Domain allowlisting: is this URL permitted?
2. Action risk classification: is this action type allowed at this risk level?
3. Irreversibility check: does the action context contain irreversible keywords?
4. Step limit: has the run exceeded the circuit breaker?

If an action is blocked, the guard returns a clear reason. If it requires
confirmation, it raises a signal that the escalation system can handle.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from cua.models.capability import RiskLevel
from cua.models.policy import ActionClassification, SafetyPolicy


@dataclass
class GuardDecision:
    """Result of a safety check on a proposed action."""

    allowed: bool
    classification: ActionClassification
    reason: str = ""
    requires_confirmation: bool = False


class SafetyGuard:
    """Runtime safety policy enforcement."""

    def __init__(self, policy: SafetyPolicy) -> None:
        self._policy = policy
        self._step_count = 0

    def check_action(
        self,
        url: str,
        action_type: str,
        risk_level: RiskLevel,
        action_context: str = "",
    ) -> GuardDecision:
        """Evaluate whether a proposed action is permitted under the current policy."""

        # 1. Step limit check
        if self._step_count >= self._policy.max_steps_per_run:
            return GuardDecision(
                allowed=False,
                classification=ActionClassification.BLOCK,
                reason=f"Step limit reached ({self._policy.max_steps_per_run})",
            )

        # 2. Domain allowlist check
        if url and not self._policy.is_domain_allowed(url):
            parsed = urlparse(url)
            return GuardDecision(
                allowed=False,
                classification=ActionClassification.BLOCK,
                reason=f"Domain '{parsed.hostname}' is not in the allowlist",
            )

        # 3. Action classification
        classification = self._policy.classify_action(action_type, risk_level, action_context)

        if classification == ActionClassification.BLOCK:
            return GuardDecision(
                allowed=False,
                classification=classification,
                reason=f"Action '{action_type}' with risk level '{risk_level}' is blocked by policy",
            )

        if classification == ActionClassification.REQUIRE_CONFIRMATION:
            return GuardDecision(
                allowed=False,
                classification=classification,
                reason=f"Action '{action_type}' requires confirmation (risk: {risk_level}, context: {action_context})",
                requires_confirmation=True,
            )

        if classification == ActionClassification.FLAG:
            return GuardDecision(
                allowed=True,
                classification=classification,
                reason=f"Action '{action_type}' is flagged for review",
            )

        return GuardDecision(
            allowed=True,
            classification=ActionClassification.ALLOW,
        )

    def record_step(self) -> None:
        """Increment the step counter."""
        self._step_count += 1

    def reset(self) -> None:
        """Reset step counter for a new run."""
        self._step_count = 0

    @property
    def steps_remaining(self) -> int:
        return max(0, self._policy.max_steps_per_run - self._step_count)
