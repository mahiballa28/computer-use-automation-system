"""Auto-Healing Locators — self-updating capability artifacts.

When a locator strategy fails but a fallback succeeds during replay, this
module records the "heal" — which strategy failed and which succeeded — and
can automatically update the capability artifact to promote the working
strategy and demote or replace the broken one.

This turns locator maintenance from a manual task into an automated one:
instead of an operator discovering a broken capability and re-recording it,
the system heals itself on the fly and records the change for review.

Design:
- Each heal is recorded with full context (what failed, what worked, confidence)
- Heals accumulate across runs — a single fallback doesn't trigger an update
- After N consistent heals (default: 3), the artifact is updated
- All heals are logged to the audit trail for compliance
- The original strategy is preserved (demoted, not deleted) for rollback

Why not just always use the best strategy?
- A11y locators are preferred because they're semantically meaningful
- A temporary DOM glitch shouldn't permanently change the strategy order
- Requiring N consistent heals prevents flaky auto-updates
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
import yaml

from cua.models.capability import LocatorStrategy, LocatorType

logger = structlog.get_logger(component="auto_healer")


@dataclass
class HealRecord:
    """Record of a single locator heal event."""

    step_id: str
    failed_strategy: LocatorType
    succeeded_strategy: LocatorType
    succeeded_value: str
    confidence: float
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class StepHealHistory:
    """Accumulated heal history for a single step."""

    step_id: str
    heals: list[HealRecord] = field(default_factory=list)

    @property
    def consistent_heal_count(self) -> int:
        """Count consecutive heals with the same succeeded strategy."""
        if not self.heals:
            return 0
        latest_strategy = self.heals[-1].succeeded_strategy
        count = 0
        for heal in reversed(self.heals):
            if heal.succeeded_strategy == latest_strategy:
                count += 1
            else:
                break
        return count

    @property
    def recommended_strategy(self) -> LocatorType | None:
        """The strategy that should be promoted, if consistent enough."""
        if self.consistent_heal_count >= 3:
            return self.heals[-1].succeeded_strategy
        return None


class AutoHealer:
    """Tracks locator heals and can update capability artifacts.

    Usage:
        healer = AutoHealer(heal_threshold=3)

        # During replay, when a fallback is used
        healer.record_heal(
            step_id="step_03",
            failed_strategy=LocatorType.ACCESSIBILITY,
            succeeded_strategy=LocatorType.LABEL_PROXIMITY,
            succeeded_value="td:has-text('Member ID') + td input",
            confidence=0.85,
        )

        # After replay, check if any artifacts should be updated
        updates = healer.get_pending_updates()
        for update in updates:
            healer.apply_update(capability_path, update)
    """

    def __init__(self, heal_threshold: int = 3) -> None:
        self._threshold = heal_threshold
        self._history: dict[str, StepHealHistory] = {}

    def record_heal(
        self,
        step_id: str,
        failed_strategy: LocatorType,
        succeeded_strategy: LocatorType,
        succeeded_value: str,
        confidence: float,
    ) -> HealRecord:
        """Record a locator heal event."""
        if step_id not in self._history:
            self._history[step_id] = StepHealHistory(step_id=step_id)

        record = HealRecord(
            step_id=step_id,
            failed_strategy=failed_strategy,
            succeeded_strategy=succeeded_strategy,
            succeeded_value=succeeded_value,
            confidence=confidence,
        )
        self._history[step_id].heals.append(record)

        logger.info(
            "heal_recorded",
            step_id=step_id,
            failed=failed_strategy.value,
            succeeded=succeeded_strategy.value,
            consistent_count=self._history[step_id].consistent_heal_count,
        )
        return record

    def get_pending_updates(self) -> list[dict[str, Any]]:
        """Get list of steps that have accumulated enough heals for an update."""
        updates = []
        for step_id, history in self._history.items():
            if history.consistent_heal_count < self._threshold:
                continue
            recommended = history.heals[-1].succeeded_strategy
            if recommended:
                latest = history.heals[-1]
                updates.append({
                    "step_id": step_id,
                    "promote_strategy": recommended.value,
                    "promote_value": latest.succeeded_value,
                    "confidence": latest.confidence,
                    "heal_count": history.consistent_heal_count,
                    "demote_strategy": latest.failed_strategy.value,
                })
        return updates

    def apply_update(self, capability_path: str, update: dict[str, Any]) -> bool:
        """Apply a heal update to a capability YAML file.

        Promotes the working strategy and demotes the broken one.
        Returns True if the file was modified.
        """
        try:
            with open(capability_path) as f:
                data = yaml.safe_load(f)

            # Find the step
            steps = data.get("steps", [])
            target_step = None
            for step in steps:
                if step.get("id") == update["step_id"]:
                    target_step = step
                    break

            if not target_step or not target_step.get("target"):
                return False

            strategies = target_step["target"].get("strategies", [])
            if not strategies:
                return False

            # Find the strategy to promote
            promote_idx = None
            demote_idx = None
            for i, strat in enumerate(strategies):
                if strat.get("type") == update["promote_strategy"]:
                    promote_idx = i
                if strat.get("type") == update["demote_strategy"]:
                    demote_idx = i

            if promote_idx is not None and demote_idx is not None:
                # Swap positions — promote the working strategy
                strategies[promote_idx], strategies[demote_idx] = (
                    strategies[demote_idx],
                    strategies[promote_idx],
                )

                logger.info(
                    "capability_healed",
                    step_id=update["step_id"],
                    promoted=update["promote_strategy"],
                    demoted=update["demote_strategy"],
                    file=capability_path,
                )

                # Add a comment about the heal
                target_step["target"]["_healed"] = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": (
                        f"Auto-healed after {update['heal_count']} consistent "
                        f"fallbacks from {update['demote_strategy']} to "
                        f"{update['promote_strategy']}"
                    ),
                }

                with open(capability_path, "w") as f:
                    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

                return True

        except Exception as e:
            logger.error("heal_apply_failed", error=str(e), step_id=update["step_id"])

        return False

    def get_report(self) -> dict[str, Any]:
        """Generate a heal report for the current session."""
        return {
            "total_heals": sum(len(h.heals) for h in self._history.values()),
            "steps_tracked": len(self._history),
            "pending_updates": len(self.get_pending_updates()),
            "steps": {
                step_id: {
                    "total_heals": len(history.heals),
                    "consistent_count": history.consistent_heal_count,
                    "recommended": (
                        history.recommended_strategy.value
                        if history.recommended_strategy
                        else None
                    ),
                }
                for step_id, history in self._history.items()
            },
        }
