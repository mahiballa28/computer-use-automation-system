"""PII and sensitive data redaction.

All data flowing through the CUA system — logs, evidence, artifacts — must be
scrubbed of regulated financial data before persistence. This module implements
a pipeline of regex-based redaction patterns loaded from the safety policy.

The redactor is applied at three points:
1. Agent loop logging (during discovery)
2. Artifact recording (extracted values marked as sensitive)
3. Evidence capture (screenshots are NOT redacted — they're stored with
   restricted access; text logs ARE redacted)
"""

from __future__ import annotations

import re
from typing import Any

from cua.models.policy import SafetyPolicy


class Redactor:
    """Applies PII/secret redaction patterns from the safety policy."""

    # Built-in patterns that are always active, regardless of policy
    BUILTIN_PATTERNS: list[tuple[str, str, str]] = [
        # (name, pattern, replacement)
        ("ssn_full", r"\b\d{3}-\d{2}-\d{4}\b", "[SSN REDACTED]"),
        ("credit_card", r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[CARD REDACTED]"),
        ("routing_number", r"\b\d{9}\b(?=.*(?:routing|aba|transit))", "[ROUTING REDACTED]"),
    ]

    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self._patterns: list[tuple[str, re.Pattern[str], str]] = []

        # Load built-in patterns
        for name, pattern, replacement in self.BUILTIN_PATTERNS:
            self._patterns.append((name, re.compile(pattern, re.IGNORECASE), replacement))

        # Load policy patterns
        if policy:
            for p in policy.redaction_patterns:
                self._patterns.append((p.name, re.compile(p.pattern), p.replacement))

    def redact(self, text: str) -> str:
        """Apply all redaction patterns to a text string."""
        result = text
        for _name, pattern, replacement in self._patterns:
            result = pattern.sub(replacement, result)
        return result

    def redact_dict(self, data: dict[str, Any], sensitive_keys: set[str] | None = None) -> dict[str, Any]:
        """Redact values in a dictionary.

        If sensitive_keys is provided, those keys' values are fully replaced
        with [REDACTED] regardless of pattern matching. All string values
        are also run through pattern-based redaction.
        """
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            if sensitive_keys and key in sensitive_keys:
                redacted[key] = "[REDACTED]"
            elif isinstance(value, str):
                redacted[key] = self.redact(value)
            elif isinstance(value, dict):
                redacted[key] = self.redact_dict(value, sensitive_keys)
            elif isinstance(value, list):
                redacted[key] = [
                    self.redact(item) if isinstance(item, str)
                    else self.redact_dict(item, sensitive_keys) if isinstance(item, dict)
                    else item
                    for item in value
                ]
            else:
                redacted[key] = value
        return redacted

    def has_sensitive_data(self, text: str) -> bool:
        """Check if text contains any sensitive patterns."""
        for _name, pattern, _replacement in self._patterns:
            if pattern.search(text):
                return True
        return False

    def detected_patterns(self, text: str) -> list[str]:
        """Return names of all patterns that matched in the text."""
        matches = []
        for name, pattern, _replacement in self._patterns:
            if pattern.search(text):
                matches.append(name)
        return matches
