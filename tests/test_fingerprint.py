"""Tests for semantic element fingerprinting."""

from __future__ import annotations

from cua.models.fingerprint import (
    ElementFingerprint,
    SpatialSignature,
    cosine_similarity,
    score_fingerprint_match,
    structural_path_similarity,
)


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        a = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(a, a) - 1.0) < 0.001

    def test_orthogonal_vectors(self) -> None:
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert abs(cosine_similarity(a, b)) < 0.001

    def test_opposite_vectors(self) -> None:
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        assert abs(cosine_similarity(a, b) - (-1.0)) < 0.001

    def test_zero_vector(self) -> None:
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_empty_vectors(self) -> None:
        assert cosine_similarity([], []) == 0.0

    def test_mismatched_lengths(self) -> None:
        assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


class TestStructuralPathSimilarity:
    def test_identical_paths(self) -> None:
        path = "form > div > input"
        assert abs(structural_path_similarity(path, path) - 1.0) < 0.001

    def test_completely_different(self) -> None:
        assert structural_path_similarity("a > b > c", "x > y > z") == 0.0

    def test_partial_overlap(self) -> None:
        sim = structural_path_similarity("form > div > input", "form > table > input")
        assert 0 < sim < 1  # Shares "form" and "input"

    def test_empty_path(self) -> None:
        assert structural_path_similarity("", "form > input") == 0.0


class TestSpatialSignature:
    def test_distance_to_self(self) -> None:
        sig = SpatialSignature(x=0.5, y=0.5, width=0.2, height=0.1)
        assert sig.distance_to(sig) == 0.0

    def test_distance_to_other(self) -> None:
        a = SpatialSignature(x=0.0, y=0.0, width=0.1, height=0.1)
        b = SpatialSignature(x=1.0, y=1.0, width=0.1, height=0.1)
        dist = a.distance_to(b)
        assert dist > 0


class TestFingerprintMatching:
    def test_identical_fingerprints(self) -> None:
        fp = ElementFingerprint(
            id="fp1",
            step_id="s1",
            visible_text="Member ID",
            structural_path="form > div > input",
            element_type="input[role=textbox]",
            text_embedding=[1.0, 0.0, 0.0],
            context_embedding=[0.0, 1.0, 0.0],
            spatial=SpatialSignature(x=0.1, y=0.2, width=0.3, height=0.05),
        )
        match = score_fingerprint_match(fp, fp)
        assert match.overall_score > 0.9

    def test_different_fingerprints(self) -> None:
        fp1 = ElementFingerprint(
            id="fp1",
            step_id="s1",
            visible_text="Member ID",
            structural_path="form > div > input",
            element_type="input[role=textbox]",
            text_embedding=[1.0, 0.0, 0.0],
            context_embedding=[0.0, 1.0, 0.0],
        )
        fp2 = ElementFingerprint(
            id="fp2",
            step_id="s2",
            visible_text="Transfer Amount",
            structural_path="table > tr > td > input",
            element_type="input[role=textbox]",
            text_embedding=[0.0, 0.0, 1.0],
            context_embedding=[0.0, 0.0, 1.0],
        )
        match = score_fingerprint_match(fp1, fp2)
        assert match.overall_score < 0.5

    def test_similar_fingerprints(self) -> None:
        """Elements with same type and similar text should score well."""
        fp1 = ElementFingerprint(
            id="fp1",
            step_id="s1",
            visible_text="Member ID",
            element_type="input[role=textbox]",
            text_embedding=[0.9, 0.1, 0.0],
        )
        fp2 = ElementFingerprint(
            id="fp2",
            step_id="s2",
            visible_text="Member Number",
            element_type="input[role=textbox]",
            text_embedding=[0.85, 0.15, 0.05],
        )
        match = score_fingerprint_match(fp1, fp2)
        assert match.type_match is True
        assert match.text_similarity > 0.5

    def test_content_hash_stability(self) -> None:
        fp1 = ElementFingerprint(
            id="fp1", step_id="s1", visible_text="Test", structural_path="div > input", element_type="input"
        )
        fp2 = ElementFingerprint(
            id="fp2", step_id="s1", visible_text="Test", structural_path="div > input", element_type="input"
        )
        assert fp1.content_hash() == fp2.content_hash()
