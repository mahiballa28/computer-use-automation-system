"""Adaptive Wait Strategy — ML-based timing prediction for replay.

Instead of fixed timeouts (which are either too long for fast apps or too short
for slow ones), this module learns timing distributions from past replays and
adapts wait times per step.

Uses exponential moving averages (EMA) to track:
- Mean response time per step
- Variance (for computing confidence intervals)
- Trend detection (is this step getting slower over time?)

The adaptive timeout for a step is:
    timeout = EMA_mean + k * EMA_std

where k is a confidence multiplier (default: 3.0 for 99.7% coverage under
normal distribution assumptions).

Why this matters for banking:
- Legacy systems have highly variable response times
- Fixed 10s timeouts waste time on fast steps and fail on legitimately slow ones
- Trend detection catches backend degradation before it causes failures
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepTiming:
    """Exponential moving average tracker for a single step's timing."""

    step_id: str
    alpha: float = 0.3  # EMA smoothing factor (higher = more weight on recent)
    ema_mean: float = 0.0
    ema_variance: float = 0.0
    observation_count: int = 0
    min_observed: float = float("inf")
    max_observed: float = 0.0
    # Trend tracking
    _prev_ema: float = 0.0

    @property
    def ema_std(self) -> float:
        """Standard deviation from EMA variance."""
        return self.ema_variance ** 0.5

    @property
    def trend(self) -> str:
        """Whether timing is trending up (degrading), down (improving), or stable."""
        if self.observation_count < 3:
            return "insufficient_data"
        diff = self.ema_mean - self._prev_ema
        if diff > self.ema_std * 0.5:
            return "degrading"
        elif diff < -self.ema_std * 0.5:
            return "improving"
        return "stable"

    def update(self, duration_ms: float) -> None:
        """Update the EMA with a new timing observation."""
        self.observation_count += 1
        self.min_observed = min(self.min_observed, duration_ms)
        self.max_observed = max(self.max_observed, duration_ms)
        self._prev_ema = self.ema_mean

        if self.observation_count == 1:
            self.ema_mean = duration_ms
            self.ema_variance = 0.0
        else:
            # EMA update
            delta = duration_ms - self.ema_mean
            self.ema_mean += self.alpha * delta
            # EMA variance (Welford-style with EMA)
            self.ema_variance = (1 - self.alpha) * (
                self.ema_variance + self.alpha * delta * delta
            )

    def adaptive_timeout(self, confidence_k: float = 3.0, min_ms: float = 1_000) -> float:
        """Compute an adaptive timeout based on learned timing distribution.

        Returns timeout in milliseconds. Uses mean + k*std with a minimum floor.
        """
        if self.observation_count < 2:
            return min_ms  # Not enough data, use minimum

        timeout = self.ema_mean + confidence_k * self.ema_std
        # Apply a floor and a ceiling
        return max(min_ms, min(timeout, 60_000))


@dataclass
class AdaptiveTimingProfile:
    """Timing profile for an entire capability across multiple replays."""

    capability_id: str
    step_timings: dict[str, StepTiming] = field(default_factory=dict)
    total_runs: int = 0

    def record_step(self, step_id: str, duration_ms: float) -> StepTiming:
        """Record a timing observation for a step."""
        if step_id not in self.step_timings:
            self.step_timings[step_id] = StepTiming(step_id=step_id)

        timing = self.step_timings[step_id]
        timing.update(duration_ms)
        return timing

    def get_timeout(self, step_id: str, default_ms: float = 10_000) -> float:
        """Get the adaptive timeout for a step, or the default if no data."""
        if step_id in self.step_timings:
            return self.step_timings[step_id].adaptive_timeout()
        return default_ms

    def complete_run(self) -> None:
        """Mark that a complete run has finished."""
        self.total_runs += 1

    def get_report(self) -> dict[str, Any]:
        """Generate a timing report for the capability."""
        steps: list[dict[str, Any]] = []
        degrading_steps: list[str] = []

        for step_id, timing in sorted(self.step_timings.items()):
            step_report = {
                "step_id": step_id,
                "mean_ms": round(timing.ema_mean, 1),
                "std_ms": round(timing.ema_std, 1),
                "min_ms": round(timing.min_observed, 1),
                "max_ms": round(timing.max_observed, 1),
                "adaptive_timeout_ms": round(timing.adaptive_timeout(), 1),
                "observations": timing.observation_count,
                "trend": timing.trend,
            }
            steps.append(step_report)

            if timing.trend == "degrading":
                degrading_steps.append(step_id)

        return {
            "capability_id": self.capability_id,
            "total_runs": self.total_runs,
            "steps": steps,
            "degrading_steps": degrading_steps,
            "health": "degrading" if degrading_steps else "healthy",
        }
