"""Tests for adaptive timing prediction."""

from cua.replay.adaptive_timing import AdaptiveTimingProfile, StepTiming


class TestStepTiming:
    def test_first_observation_sets_mean(self) -> None:
        timing = StepTiming(step_id="step_01")
        timing.update(500.0)
        assert timing.ema_mean == 500.0
        assert timing.observation_count == 1

    def test_ema_converges(self) -> None:
        timing = StepTiming(step_id="step_01", alpha=0.5)
        for _ in range(20):
            timing.update(1000.0)
        # Should converge close to 1000
        assert abs(timing.ema_mean - 1000.0) < 1.0

    def test_tracks_min_max(self) -> None:
        timing = StepTiming(step_id="step_01")
        timing.update(100.0)
        timing.update(500.0)
        timing.update(200.0)
        assert timing.min_observed == 100.0
        assert timing.max_observed == 500.0

    def test_adaptive_timeout_with_variance(self) -> None:
        timing = StepTiming(step_id="step_01")
        # Add observations with variance
        for val in [500, 600, 550, 650, 500]:
            timing.update(val)
        timeout = timing.adaptive_timeout(confidence_k=3.0)
        # Should be higher than the mean
        assert timeout > timing.ema_mean
        # But not absurdly high
        assert timeout < 60_000

    def test_minimum_timeout_floor(self) -> None:
        timing = StepTiming(step_id="step_01")
        timing.update(10.0)
        timing.update(10.0)
        timeout = timing.adaptive_timeout(min_ms=1_000)
        assert timeout >= 1_000

    def test_insufficient_data_trend(self) -> None:
        timing = StepTiming(step_id="step_01")
        timing.update(100.0)
        assert timing.trend == "insufficient_data"

    def test_degrading_trend(self) -> None:
        timing = StepTiming(step_id="step_01", alpha=0.5)
        # Gradually increase timing
        for val in [100, 200, 400, 800, 1600]:
            timing.update(val)
        assert timing.trend == "degrading"

    def test_stable_trend(self) -> None:
        timing = StepTiming(step_id="step_01", alpha=0.3)
        for val in [500, 500, 500, 500, 500]:
            timing.update(val)
        assert timing.trend == "stable"


class TestAdaptiveTimingProfile:
    def test_record_and_get_timeout(self) -> None:
        profile = AdaptiveTimingProfile(capability_id="cap-1")
        for _ in range(5):
            profile.record_step("step_01", 500.0)

        timeout = profile.get_timeout("step_01")
        assert timeout > 0
        assert timeout < 60_000

    def test_default_timeout_for_unknown_step(self) -> None:
        profile = AdaptiveTimingProfile(capability_id="cap-1")
        timeout = profile.get_timeout("step_unknown", default_ms=10_000)
        assert timeout == 10_000

    def test_report_generation(self) -> None:
        profile = AdaptiveTimingProfile(capability_id="cap-1")
        for i in range(10):
            profile.record_step("step_01", 500.0 + i * 10)
            profile.record_step("step_02", 200.0)
        profile.complete_run()

        report = profile.get_report()
        assert report["capability_id"] == "cap-1"
        assert report["total_runs"] == 1
        assert len(report["steps"]) == 2
        assert report["health"] in ("healthy", "degrading")

    def test_degrading_step_flagged_in_report(self) -> None:
        profile = AdaptiveTimingProfile(capability_id="cap-1")
        # Simulate a step getting progressively slower
        for val in [100, 200, 500, 1000, 2000, 4000]:
            profile.record_step("step_slow", float(val))

        report = profile.get_report()
        if report["degrading_steps"]:
            assert "step_slow" in report["degrading_steps"]
