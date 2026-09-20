"""Tests for auto-healing locators."""

from cua.models.capability import LocatorType
from cua.replay.auto_healer import AutoHealer


class TestAutoHealer:
    def test_record_heal(self) -> None:
        healer = AutoHealer()
        record = healer.record_heal(
            step_id="step_03",
            failed_strategy=LocatorType.ACCESSIBILITY,
            succeeded_strategy=LocatorType.LABEL_PROXIMITY,
            succeeded_value="td:has-text('Member ID') + td input",
            confidence=0.85,
        )
        assert record.step_id == "step_03"
        assert record.failed_strategy == LocatorType.ACCESSIBILITY

    def test_no_update_below_threshold(self) -> None:
        healer = AutoHealer(heal_threshold=3)
        # Only 2 heals — not enough
        for _ in range(2):
            healer.record_heal(
                step_id="step_01",
                failed_strategy=LocatorType.ACCESSIBILITY,
                succeeded_strategy=LocatorType.TEXT_CONTENT,
                succeeded_value="text='Search'",
                confidence=0.90,
            )
        updates = healer.get_pending_updates()
        assert len(updates) == 0

    def test_update_at_threshold(self) -> None:
        healer = AutoHealer(heal_threshold=3)
        for _ in range(3):
            healer.record_heal(
                step_id="step_01",
                failed_strategy=LocatorType.ACCESSIBILITY,
                succeeded_strategy=LocatorType.LABEL_PROXIMITY,
                succeeded_value="label text",
                confidence=0.85,
            )
        updates = healer.get_pending_updates()
        assert len(updates) == 1
        assert updates[0]["step_id"] == "step_01"
        assert updates[0]["promote_strategy"] == "label_proximity"

    def test_inconsistent_heals_dont_trigger(self) -> None:
        healer = AutoHealer(heal_threshold=3)
        # Alternating strategies — not consistent
        for strat in [LocatorType.TEXT_CONTENT, LocatorType.LABEL_PROXIMITY, LocatorType.TEXT_CONTENT]:
            healer.record_heal(
                step_id="step_01",
                failed_strategy=LocatorType.ACCESSIBILITY,
                succeeded_strategy=strat,
                succeeded_value="some value",
                confidence=0.80,
            )
        updates = healer.get_pending_updates()
        assert len(updates) == 0

    def test_multiple_steps_tracked_independently(self) -> None:
        healer = AutoHealer(heal_threshold=2)
        # step_01 gets 2 consistent heals
        for _ in range(2):
            healer.record_heal("step_01", LocatorType.ACCESSIBILITY,
                               LocatorType.TEXT_CONTENT, "text", 0.9)
        # step_02 gets only 1
        healer.record_heal("step_02", LocatorType.ACCESSIBILITY,
                           LocatorType.CSS_SELECTOR, "css", 0.7)

        updates = healer.get_pending_updates()
        assert len(updates) == 1
        assert updates[0]["step_id"] == "step_01"

    def test_report_generation(self) -> None:
        healer = AutoHealer()
        healer.record_heal("step_01", LocatorType.ACCESSIBILITY,
                           LocatorType.TEXT_CONTENT, "text", 0.9)
        healer.record_heal("step_02", LocatorType.ACCESSIBILITY,
                           LocatorType.LABEL_PROXIMITY, "label", 0.85)

        report = healer.get_report()
        assert report["total_heals"] == 2
        assert report["steps_tracked"] == 2
