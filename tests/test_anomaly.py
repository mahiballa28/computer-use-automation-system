"""Tests for execution trace anomaly detection."""

from __future__ import annotations

from cua.observability.anomaly import (
    AnomalyDetector,
    AnomalySeverity,
    AnomalyType,
    CapabilityProfile,
    NormalDistribution,
    ProfileMaturity,
    StepProfile,
)


class TestNormalDistribution:
    """Test Welford's online mean/std computation."""

    def test_single_value(self) -> None:
        dist = NormalDistribution()
        dist.update(100.0)
        assert dist.mean == 100.0
        assert dist.std == 0.0
        assert dist.count == 1

    def test_multiple_values(self) -> None:
        dist = NormalDistribution()
        for v in [10, 20, 30, 40, 50]:
            dist.update(v)
        assert dist.count == 5
        assert abs(dist.mean - 30.0) < 0.01
        assert dist.std > 0
        assert dist.min_value == 10
        assert dist.max_value == 50

    def test_z_score(self) -> None:
        dist = NormalDistribution()
        for v in [100, 102, 98, 101, 99, 100, 103, 97]:
            dist.update(v)
        # A value far from the mean should have a high z-score
        assert dist.z_score(200) > 3.0
        # A value near the mean should have a low z-score
        assert dist.z_score(100) < 1.0


class TestCapabilityProfile:
    """Test profile maturity and update logic."""

    def test_maturity_cold(self) -> None:
        profile = CapabilityProfile(capability_id="test", capability_name="test")
        assert profile.maturity == ProfileMaturity.COLD

    def test_maturity_warm(self) -> None:
        profile = CapabilityProfile(capability_id="test", capability_name="test", successful_runs=10)
        assert profile.maturity == ProfileMaturity.WARM

    def test_maturity_hot(self) -> None:
        profile = CapabilityProfile(capability_id="test", capability_name="test", successful_runs=25)
        assert profile.maturity == ProfileMaturity.HOT

    def test_update_from_trace(self) -> None:
        profile = CapabilityProfile(capability_id="test", capability_name="test")
        trace = [
            {"step_id": "step_01", "wall_time_ms": 100, "page_url": "http://localhost:5001"},
            {"step_id": "step_02", "wall_time_ms": 200, "page_url": "http://localhost:5001/search"},
        ]
        profile.update_from_trace(trace, success=True)
        assert profile.total_runs == 1
        assert profile.successful_runs == 1
        assert "step_01" in profile.step_profiles
        assert profile.step_profiles["step_01"].timing.count == 1

    def test_z_threshold_by_maturity(self) -> None:
        cold = CapabilityProfile(capability_id="test", capability_name="test", successful_runs=3)
        warm = CapabilityProfile(capability_id="test", capability_name="test", successful_runs=10)
        hot = CapabilityProfile(capability_id="test", capability_name="test", successful_runs=25)

        assert cold.z_threshold == float("inf")  # No detection
        assert warm.z_threshold == 4.0
        assert hot.z_threshold == 3.0


class TestAnomalyDetector:
    """Test anomaly detection during replay."""

    def _build_profile(self, n_runs: int = 25) -> CapabilityProfile:
        """Build a profile with N successful runs of consistent timing."""
        profile = CapabilityProfile(capability_id="test", capability_name="test")
        for _ in range(n_runs):
            trace = [
                {"step_id": "step_01", "wall_time_ms": 100 + (_ % 10), "page_url": "http://localhost"},
            ]
            profile.update_from_trace(trace, success=True)
        return profile

    def test_no_anomaly_normal_execution(self) -> None:
        profile = self._build_profile()
        detector = AnomalyDetector(profile)
        anomalies = detector.check_step("step_01", wall_time_ms=105)
        assert len(anomalies) == 0

    def test_timing_anomaly_detected(self) -> None:
        profile = self._build_profile()
        detector = AnomalyDetector(profile)
        # 10x normal timing should be anomalous
        anomalies = detector.check_step("step_01", wall_time_ms=5_000)
        assert len(anomalies) > 0
        assert anomalies[0].type == AnomalyType.TIMING
        assert anomalies[0].severity in (AnomalySeverity.WARNING, AnomalySeverity.CRITICAL)

    def test_no_detection_during_cold_start(self) -> None:
        profile = self._build_profile(n_runs=3)  # Cold
        detector = AnomalyDetector(profile)
        anomalies = detector.check_step("step_01", wall_time_ms=99_999)
        assert len(anomalies) == 0  # No detection during cold start

    def test_unknown_step_no_anomaly(self) -> None:
        profile = self._build_profile()
        detector = AnomalyDetector(profile)
        anomalies = detector.check_step("step_99", wall_time_ms=5_000)
        assert len(anomalies) == 0
