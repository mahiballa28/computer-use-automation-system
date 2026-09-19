"""Execution trace anomaly detection — statistical profiling of replay behavior.

BEYOND-SCOPE FEATURE B2

After N successful replays of a capability, this module builds statistical
profiles of "normal" execution: timing per step, element counts, expected
text patterns, value ranges. On subsequent replays, it flags anomalies
in real-time.

Why this matters for banking:
A replay that completes but processes the wrong number of records, takes
10x longer than normal, or lands on a page with unexpected content is
WORSE than a replay that fails loudly. Silent failures in banking =
compliance incidents. Anomaly detection is the difference between
"did it succeed?" and "did it succeed CORRECTLY?"

Architecture:
- CapabilityProfile: statistical model of a capability's normal behavior
- AnomalyDetector: scores each step during replay against the profile
- ProfileMaturity: tracks how much data the profile has (cold-start handling)

The profile matures over time:
- First 5 runs: learning-only, no anomaly flags
- 5-20 runs: active with wide thresholds (z > 4.0)
- 20+ runs: tightened thresholds (z > 3.0)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AnomalyType(StrEnum):
    """Type of anomaly detected."""

    TIMING = "timing"
    VALUE_RANGE = "value_range"
    TEXT_PATTERN = "text_pattern"
    MISSING_ELEMENT = "missing_element"


class AnomalySeverity(StrEnum):
    """How severe the anomaly is."""

    INFO = "info"  # notable but not actionable
    WARNING = "warning"  # worth investigating
    CRITICAL = "critical"  # likely indicates a real problem


class Anomaly(BaseModel):
    """A detected anomaly during replay execution."""

    model_config = ConfigDict(extra="forbid")

    type: AnomalyType
    severity: AnomalySeverity
    step_id: str
    message: str
    expected: str
    observed: str
    z_score: float | None = None  # for statistical anomalies
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NormalDistribution(BaseModel):
    """Simple normal distribution for statistical profiling."""

    model_config = ConfigDict(extra="forbid")

    mean: float = 0.0
    std: float = 0.0
    count: int = 0
    min_value: float = float("inf")
    max_value: float = float("-inf")

    def update(self, value: float) -> None:
        """Online update of mean and std (Welford's algorithm)."""
        self.count += 1
        self.min_value = min(self.min_value, value)
        self.max_value = max(self.max_value, value)

        if self.count == 1:
            self.mean = value
            self.std = 0.0
            self._m2 = 0.0
        else:
            delta = value - self.mean
            self.mean += delta / self.count
            delta2 = value - self.mean
            self._m2 = getattr(self, "_m2", 0.0) + delta * delta2
            self.std = math.sqrt(self._m2 / self.count) if self.count > 1 else 0.0

    def z_score(self, value: float) -> float:
        """Compute z-score for a value against this distribution."""
        if self.std == 0.0 or self.count < 3:
            return 0.0
        return abs(value - self.mean) / self.std

    _m2: float = 0.0  # For Welford's algorithm


class StepProfile(BaseModel):
    """Statistical profile of a single step's normal behavior."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    timing: NormalDistribution = Field(default_factory=NormalDistribution)
    page_text_hashes: list[str] = Field(
        default_factory=list,
        description="Hashes of page text observed at this step (for pattern detection)",
    )
    expected_url_patterns: list[str] = Field(default_factory=list)
    value_distributions: dict[str, NormalDistribution] = Field(default_factory=dict)


class ProfileMaturity(StrEnum):
    """How mature (reliable) the capability profile is."""

    COLD = "cold"  # < 5 runs, learning only
    WARM = "warm"  # 5-20 runs, wide thresholds
    HOT = "hot"  # 20+ runs, tight thresholds


class CapabilityProfile(BaseModel):
    """Statistical model of a capability's normal execution behavior.

    Built incrementally from replay traces. Used by the AnomalyDetector
    to flag deviations during subsequent replays.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    capability_name: str
    step_profiles: dict[str, StepProfile] = Field(default_factory=dict)
    total_runs: int = 0
    successful_runs: int = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def maturity(self) -> ProfileMaturity:
        if self.successful_runs < 5:
            return ProfileMaturity.COLD
        if self.successful_runs < 20:
            return ProfileMaturity.WARM
        return ProfileMaturity.HOT

    @property
    def z_threshold(self) -> float:
        """Z-score threshold based on profile maturity."""
        if self.maturity == ProfileMaturity.COLD:
            return float("inf")  # No detection during cold start
        if self.maturity == ProfileMaturity.WARM:
            return 4.0
        return 3.0

    def update_from_trace(self, step_traces: list[dict[str, Any]], success: bool) -> None:
        """Update the profile with data from a new replay run."""
        self.total_runs += 1
        if success:
            self.successful_runs += 1

        for trace in step_traces:
            step_id = trace.get("step_id", "")
            if step_id not in self.step_profiles:
                self.step_profiles[step_id] = StepProfile(step_id=step_id)

            profile = self.step_profiles[step_id]

            # Update timing distribution
            wall_time = trace.get("wall_time_ms", 0.0)
            if wall_time > 0:
                profile.timing.update(wall_time)

            # Track URL patterns
            url = trace.get("page_url", "")
            if url and url not in profile.expected_url_patterns:
                profile.expected_url_patterns.append(url)

            # Update value distributions for extracted values
            for key, value in trace.get("extracted_values", {}).items():
                if isinstance(value, (int, float)):
                    if key not in profile.value_distributions:
                        profile.value_distributions[key] = NormalDistribution()
                    profile.value_distributions[key].update(float(value))

        self.last_updated = datetime.now(timezone.utc)


class AnomalyDetector:
    """Detects anomalies during replay by comparing against a capability profile."""

    def __init__(self, profile: CapabilityProfile) -> None:
        self._profile = profile

    def check_step(
        self,
        step_id: str,
        wall_time_ms: float,
        page_url: str | None = None,
        extracted_values: dict[str, Any] | None = None,
    ) -> list[Anomaly]:
        """Check a single step execution for anomalies.

        Returns a list of detected anomalies (empty if all normal).
        """
        if self._profile.maturity == ProfileMaturity.COLD:
            return []  # Not enough data to detect anomalies

        anomalies: list[Anomaly] = []
        step_profile = self._profile.step_profiles.get(step_id)

        if not step_profile:
            return []  # No profile for this step yet

        threshold = self._profile.z_threshold

        # Check timing
        if step_profile.timing.count >= 3:
            z = step_profile.timing.z_score(wall_time_ms)
            if z > threshold:
                severity = AnomalySeverity.CRITICAL if z > threshold + 2 else AnomalySeverity.WARNING
                anomalies.append(Anomaly(
                    type=AnomalyType.TIMING,
                    severity=severity,
                    step_id=step_id,
                    message=f"Step timing anomaly: {wall_time_ms:.0f}ms (expected ~{step_profile.timing.mean:.0f}ms ± {step_profile.timing.std:.0f}ms)",
                    expected=f"{step_profile.timing.mean:.0f}ms ± {step_profile.timing.std:.0f}ms",
                    observed=f"{wall_time_ms:.0f}ms",
                    z_score=z,
                ))

        # Check extracted values
        if extracted_values:
            for key, value in extracted_values.items():
                if isinstance(value, (int, float)) and key in step_profile.value_distributions:
                    dist = step_profile.value_distributions[key]
                    if dist.count >= 3:
                        z = dist.z_score(float(value))
                        if z > threshold:
                            anomalies.append(Anomaly(
                                type=AnomalyType.VALUE_RANGE,
                                severity=AnomalySeverity.WARNING,
                                step_id=step_id,
                                message=f"Extracted value '{key}' is anomalous: {value} (expected ~{dist.mean:.2f} ± {dist.std:.2f})",
                                expected=f"{dist.mean:.2f} ± {dist.std:.2f}",
                                observed=str(value),
                                z_score=z,
                            ))

        return anomalies

    def save_profile(self, path: str | Path) -> None:
        """Save the profile to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self._profile.model_dump(mode="json"), f, indent=2)

    @staticmethod
    def load_profile(path: str | Path) -> CapabilityProfile:
        """Load a profile from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        return CapabilityProfile.model_validate(data)
