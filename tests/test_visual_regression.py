"""Tests for visual regression detection."""

from cua.observability.visual_regression import (
    VisualRegressionDetector,
    compute_average_hash,
    compute_difference_hash,
    hamming_distance,
    hash_similarity,
)


class TestPerceptualHashing:
    def test_identical_images_same_hash(self) -> None:
        data = b"identical image content here" * 100
        h1 = compute_average_hash(data)
        h2 = compute_average_hash(data)
        assert h1 == h2

    def test_different_images_different_hash(self) -> None:
        h1 = compute_average_hash(b"image A content" * 100)
        h2 = compute_average_hash(b"image B content" * 100)
        assert h1 != h2

    def test_dhash_identical(self) -> None:
        data = b"test image bytes" * 50
        h1 = compute_difference_hash(data)
        h2 = compute_difference_hash(data)
        assert h1 == h2

    def test_hamming_distance_identical(self) -> None:
        assert hamming_distance("ff", "ff") == 0

    def test_hamming_distance_one_bit(self) -> None:
        assert hamming_distance("0f", "0e") == 1

    def test_hamming_distance_all_different(self) -> None:
        assert hamming_distance("ff", "00") == 8

    def test_hash_similarity_identical(self) -> None:
        assert hash_similarity("abcd", "abcd") == 1.0

    def test_hash_similarity_empty(self) -> None:
        assert hash_similarity("", "abcd") == 0.0


class TestVisualRegressionDetector:
    def test_first_screenshot_becomes_baseline(self) -> None:
        detector = VisualRegressionDetector()
        diff = detector.compare("step_01", b"first screenshot" * 100)
        assert diff.similarity == 1.0
        assert not diff.is_anomaly

    def test_identical_screenshot_no_anomaly(self) -> None:
        detector = VisualRegressionDetector()
        data = b"consistent page" * 100
        detector.set_baseline("step_01", data)
        diff = detector.compare("step_01", data)
        assert diff.similarity == 1.0
        assert not diff.is_anomaly

    def test_different_screenshot_detects_anomaly(self) -> None:
        detector = VisualRegressionDetector(anomaly_threshold=0.95)
        detector.set_baseline("step_01", b"original page" * 100)
        diff = detector.compare("step_01", b"changed page layout" * 100)
        assert diff.similarity < 1.0
        assert diff.is_anomaly

    def test_report_generation(self) -> None:
        detector = VisualRegressionDetector()
        detector.set_baseline("step_01", b"page1" * 100)
        detector.compare("step_01", b"page1" * 100)
        detector.compare("step_01", b"different" * 100)

        report = detector.get_report()
        assert report["total_comparisons"] == 2
        assert len(report["diffs"]) == 2
