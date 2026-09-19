"""Tests for safety guard and PII redaction."""

from __future__ import annotations

from cua.models.capability import RiskLevel
from cua.models.policy import ActionClassification, SafetyPolicy
from cua.safety.guard import SafetyGuard
from cua.safety.redactor import Redactor


class TestSafetyGuard:
    """Test policy enforcement."""

    def test_allowed_domain(self, sample_policy: SafetyPolicy) -> None:
        guard = SafetyGuard(sample_policy)
        decision = guard.check_action(
            "http://localhost:5001/members/search",
            "click",
            RiskLevel.SAFE,
        )
        assert decision.allowed

    def test_blocked_domain(self, sample_policy: SafetyPolicy) -> None:
        guard = SafetyGuard(sample_policy)
        decision = guard.check_action(
            "http://evil.com/phishing",
            "click",
            RiskLevel.SAFE,
        )
        assert not decision.allowed
        assert "not in the allowlist" in decision.reason

    def test_risky_action_requires_confirmation(self, sample_policy: SafetyPolicy) -> None:
        guard = SafetyGuard(sample_policy)
        decision = guard.check_action(
            "http://localhost:5001/members/M1001/transfer/confirm",
            "click",
            RiskLevel.RISKY,
            action_context="confirm transfer",
        )
        assert not decision.allowed
        assert decision.requires_confirmation

    def test_step_limit(self, sample_policy: SafetyPolicy) -> None:
        guard = SafetyGuard(sample_policy)
        # Exhaust step limit
        for _ in range(50):
            guard.record_step()
        decision = guard.check_action(
            "http://localhost:5001/dashboard",
            "click",
            RiskLevel.SAFE,
        )
        assert not decision.allowed
        assert "Step limit" in decision.reason

    def test_reset(self, sample_policy: SafetyPolicy) -> None:
        guard = SafetyGuard(sample_policy)
        for _ in range(50):
            guard.record_step()
        assert guard.steps_remaining == 0
        guard.reset()
        assert guard.steps_remaining == 50


class TestRedactor:
    """Test PII redaction."""

    def test_redact_ssn(self, sample_policy: SafetyPolicy) -> None:
        redactor = Redactor(sample_policy)
        text = "Member SSN: 123-45-6789"
        redacted = redactor.redact(text)
        assert "123-45-6789" not in redacted
        assert "[SSN REDACTED]" in redacted

    def test_redact_email(self, sample_policy: SafetyPolicy) -> None:
        redactor = Redactor(sample_policy)
        text = "Contact: alice.johnson@email.com"
        redacted = redactor.redact(text)
        assert "alice.johnson@email.com" not in redacted
        assert "[EMAIL REDACTED]" in redacted

    def test_redact_dict(self, sample_policy: SafetyPolicy) -> None:
        redactor = Redactor(sample_policy)
        data = {
            "name": "Alice Johnson",
            "ssn": "123-45-6789",
            "email": "alice@test.com",
            "balance": 15230.89,
        }
        redacted = redactor.redact_dict(data, sensitive_keys={"ssn"})
        assert redacted["ssn"] == "[REDACTED]"
        assert "alice@test.com" not in redacted["email"]
        assert redacted["balance"] == 15230.89

    def test_builtin_patterns(self) -> None:
        """Built-in patterns work without a policy."""
        redactor = Redactor()
        text = "SSN: 123-45-6789, Card: 4111-1111-1111-1111"
        redacted = redactor.redact(text)
        assert "123-45-6789" not in redacted
        assert "4111-1111-1111-1111" not in redacted

    def test_has_sensitive_data(self, sample_policy: SafetyPolicy) -> None:
        redactor = Redactor(sample_policy)
        assert redactor.has_sensitive_data("SSN: 123-45-6789")
        assert not redactor.has_sensitive_data("Hello world")

    def test_detected_patterns(self, sample_policy: SafetyPolicy) -> None:
        redactor = Redactor(sample_policy)
        patterns = redactor.detected_patterns("SSN: 123-45-6789, email: test@test.com")
        assert "ssn_full" in patterns or "ssn" in patterns
        assert "email" in patterns
