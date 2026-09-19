"""Safety policy models — allowlists, action classification, and guardrail configuration.

The policy is the configurable boundary between what the agent is permitted to do
and what it must not. It enforces:
- Domain/URL allowlisting (the agent can only interact with declared surfaces)
- Action risk classification (risky actions are blocked or require confirmation)
- PII redaction patterns (sensitive data is never persisted in artifacts or logs)
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cua.models.capability import RiskLevel


class ActionClassification(StrEnum):
    """Policy decision for an action."""

    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    FLAG = "flag"  # allow but log a warning


class AllowlistRule(BaseModel):
    """A rule in the domain/URL allowlist."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(description="URL pattern (glob-style: * matches any segment)")
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["click", "type", "select", "navigate", "extract", "assert", "wait", "scroll"],
        description="Which action types are permitted on this pattern",
    )
    max_risk_level: RiskLevel = Field(
        default=RiskLevel.CAUTIOUS,
        description="Maximum risk level allowed without confirmation",
    )
    description: str = ""


class RedactionPattern(BaseModel):
    """A regex pattern for detecting and redacting sensitive data."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="What this pattern detects (e.g. 'ssn', 'account_number')")
    pattern: str = Field(description="Regex pattern to match")
    replacement: str = Field(default="[REDACTED]", description="What to replace matches with")
    description: str = ""

    def matches(self, text: str) -> bool:
        return bool(re.search(self.pattern, text))

    def redact(self, text: str) -> str:
        return re.sub(self.pattern, self.replacement, text)


class SafetyPolicy(BaseModel):
    """Complete safety policy configuration.

    Loaded from YAML at system startup. Governs what the agent can do,
    where it can do it, and what data it must redact.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="default")
    description: str = Field(default="Default safety policy")

    # Domain/URL allowlist
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Domains the agent may interact with",
    )
    allowlist_rules: list[AllowlistRule] = Field(
        default_factory=list,
        description="URL-level rules with action and risk constraints",
    )

    # Risk handling
    risky_action_policy: ActionClassification = Field(
        default=ActionClassification.REQUIRE_CONFIRMATION,
        description="What to do when a RISKY action is attempted",
    )
    block_irreversible_without_confirmation: bool = Field(
        default=True,
        description="Block actions classified as irreversible unless explicitly confirmed",
    )
    irreversible_keywords: list[str] = Field(
        default_factory=lambda: ["delete", "remove", "transfer", "submit", "confirm", "approve", "close account"],
        description="Keywords in action context that signal irreversibility",
    )

    # PII/secret redaction
    redaction_patterns: list[RedactionPattern] = Field(
        default_factory=list,
        description="Patterns for detecting and redacting sensitive data",
    )

    # Limits
    max_steps_per_run: int = Field(default=50, description="Circuit breaker: max steps before forced stop")
    max_run_duration_seconds: int = Field(default=300, description="Circuit breaker: max wall time")
    max_retries_per_step: int = Field(default=3)

    def is_domain_allowed(self, url: str) -> bool:
        """Check if a URL's domain is in the allowlist."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = parsed.hostname or ""
        return any(
            domain == allowed or domain.endswith(f".{allowed}")
            for allowed in self.allowed_domains
        )

    def classify_action(self, action_type: str, risk_level: RiskLevel, context: str = "") -> ActionClassification:
        """Determine the policy decision for a given action."""
        if risk_level == RiskLevel.RISKY:
            # Check for irreversible keywords
            context_lower = context.lower()
            if self.block_irreversible_without_confirmation and any(
                kw in context_lower for kw in self.irreversible_keywords
            ):
                return ActionClassification.REQUIRE_CONFIRMATION
            return self.risky_action_policy

        if risk_level == RiskLevel.CAUTIOUS:
            return ActionClassification.ALLOW

        return ActionClassification.ALLOW

    def redact_text(self, text: str) -> str:
        """Apply all redaction patterns to a text string."""
        result = text
        for pattern in self.redaction_patterns:
            result = pattern.redact(result)
        return result
