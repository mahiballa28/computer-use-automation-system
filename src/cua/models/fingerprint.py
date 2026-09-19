"""Semantic element fingerprinting — embedding-based self-healing locators.

BEYOND-SCOPE FEATURE B1

The hardest problem in UI automation is locator brittleness. CSS selectors break
when the DOM changes. XPaths break when structure shifts. Test IDs don't exist
on legacy apps. Even accessibility labels change across versions.

This module solves that with a learned representation: during recording, we
compute a multi-signal semantic fingerprint for every interacted element. During
replay, when the primary locator fails, we find the most likely matching element
on the (potentially changed) page via cosine similarity — without calling the LLM.

The fingerprint combines:
- Text embedding: sentence-transformers on visible text + aria-label + placeholder
- Context embedding: text of parent and surrounding elements
- Spatial signature: normalized position (x, y, w, h) relative to viewport
- Structural path: simplified DOM ancestry (e.g. "form > div > input")
- Element type: tag + role + input type

At replay time, we enumerate all interactive elements on the page, compute their
fingerprints, and find the nearest neighbor to the recorded fingerprint. If the
similarity exceeds a threshold, we use it. If it's in a medium-confidence band,
we trigger the assisted fallback (bounded LLM recovery).

Why local embeddings instead of LLM calls?
- Replay must be fast: ~5ms local vs ~500ms API per element
- Replay must be deterministic: no model randomness in the loop
- Cost: thousands of replays × elements/page = millions of calls vs free
- Works offline / air-gapped (common in banking environments)
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SpatialSignature(BaseModel):
    """Normalized position of an element relative to the viewport."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(description="Left edge, normalized 0-1")
    y: float = Field(description="Top edge, normalized 0-1")
    width: float = Field(description="Width, normalized 0-1")
    height: float = Field(description="Height, normalized 0-1")

    def distance_to(self, other: SpatialSignature) -> float:
        """Euclidean distance in normalized coordinate space."""
        return math.sqrt(
            (self.x - other.x) ** 2
            + (self.y - other.y) ** 2
            + (self.width - other.width) ** 2
            + (self.height - other.height) ** 2
        )


class ElementFingerprint(BaseModel):
    """Multi-signal semantic fingerprint of a UI element.

    Stored alongside the capability artifact. Used by the locator resolver
    as a fallback when exact-match locators fail.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique fingerprint ID, referenced by ElementTarget.fingerprint_id")
    step_id: str = Field(description="Which step this fingerprint belongs to")

    # --- Text signals ---
    visible_text: str = Field(default="", description="Text content of the element")
    aria_label: str = Field(default="", description="Aria label if present")
    placeholder: str = Field(default="", description="Placeholder text if present")
    text_embedding: list[float] = Field(
        default_factory=list,
        description="Embedding of combined text signals (visible_text + aria_label + placeholder)",
    )

    # --- Context signals ---
    ancestor_text: str = Field(default="", description="Visible text of parent/grandparent elements")
    sibling_text: str = Field(default="", description="Text of preceding and following siblings")
    context_embedding: list[float] = Field(
        default_factory=list,
        description="Embedding of ancestor_text + sibling_text",
    )

    # --- Structural signals ---
    structural_path: str = Field(default="", description="Simplified DOM ancestry: 'form > div > input'")
    element_type: str = Field(default="", description="Tag + role + type: 'input[type=text][role=textbox]'")

    # --- Spatial signals ---
    spatial: SpatialSignature | None = None

    # --- Semantic description (LLM-generated once during recording) ---
    semantic_description: str = Field(
        default="",
        description="LLM-generated one-line description: 'Member ID search input field'",
    )

    def text_for_embedding(self) -> str:
        """Combine text signals into a single string for embedding."""
        parts = [self.visible_text, self.aria_label, self.placeholder, self.semantic_description]
        return " ".join(p for p in parts if p).strip()

    def context_for_embedding(self) -> str:
        """Combine context signals for embedding."""
        parts = [self.ancestor_text, self.sibling_text]
        return " ".join(p for p in parts if p).strip()

    def content_hash(self) -> str:
        """Stable hash of non-embedding fields for change detection."""
        content = f"{self.visible_text}|{self.aria_label}|{self.structural_path}|{self.element_type}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class FingerprintMatch(BaseModel):
    """Result of matching a fingerprint against a candidate element."""

    model_config = ConfigDict(extra="forbid")

    candidate_index: int
    text_similarity: float = Field(ge=0.0, le=1.0)
    context_similarity: float = Field(ge=0.0, le=1.0)
    spatial_distance: float = Field(ge=0.0)
    structural_similarity: float = Field(ge=0.0, le=1.0)
    type_match: bool
    overall_score: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Similarity computation (pure Python + math, no numpy required for scoring)
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 if either vector is zero-length or has zero magnitude.
    """
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def structural_path_similarity(path_a: str, path_b: str) -> float:
    """Compare two structural paths using longest common subsequence ratio."""
    parts_a = path_a.split(" > ")
    parts_b = path_b.split(" > ")
    if not parts_a or not parts_b:
        return 0.0

    # LCS length
    m, n = len(parts_a), len(parts_b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if parts_a[i - 1] == parts_b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    lcs_len = dp[m][n]
    return (2 * lcs_len) / (m + n)


def score_fingerprint_match(
    recorded: ElementFingerprint,
    candidate: ElementFingerprint,
    weights: dict[str, float] | None = None,
) -> FingerprintMatch:
    """Score how well a candidate element matches a recorded fingerprint.

    Default weights reflect that text content is the strongest signal for
    banking forms, spatial position is stable across minor CSS changes,
    and structural path handles framework re-renders.
    """
    w = weights or {
        "text": 0.40,
        "context": 0.20,
        "spatial": 0.15,
        "structural": 0.15,
        "type": 0.10,
    }

    text_sim = cosine_similarity(recorded.text_embedding, candidate.text_embedding)
    context_sim = cosine_similarity(recorded.context_embedding, candidate.context_embedding)

    spatial_dist = 0.0
    spatial_score = 0.0
    if recorded.spatial and candidate.spatial:
        spatial_dist = recorded.spatial.distance_to(candidate.spatial)
        # Convert distance to a 0-1 similarity (closer = higher)
        spatial_score = max(0.0, 1.0 - spatial_dist * 2)

    struct_sim = structural_path_similarity(recorded.structural_path, candidate.structural_path)
    type_match = recorded.element_type == candidate.element_type

    overall = (
        w["text"] * text_sim
        + w["context"] * context_sim
        + w["spatial"] * spatial_score
        + w["structural"] * struct_sim
        + w["type"] * (1.0 if type_match else 0.0)
    )

    return FingerprintMatch(
        candidate_index=0,
        text_similarity=text_sim,
        context_similarity=context_sim,
        spatial_distance=spatial_dist,
        structural_similarity=struct_sim,
        type_match=type_match,
        overall_score=min(1.0, overall),
    )


# ---------------------------------------------------------------------------
# Embedding provider (abstract — implementations inject the model)
# ---------------------------------------------------------------------------


class EmbeddingProvider:
    """Computes text embeddings for fingerprinting.

    The default implementation uses sentence-transformers (all-MiniLM-L6-v2)
    for local, fast, free inference. Falls back to a zero-vector stub if the
    model is not available (allows the system to run without ML dependencies
    for basic testing).
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._available: bool | None = None

    def _load_model(self) -> None:
        if self._available is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._available = True
        except ImportError:
            self._available = False

    def embed(self, text: str) -> list[float]:
        """Compute embedding for a text string. Returns zero vector if model unavailable."""
        self._load_model()
        if not self._available or not self._model or not text.strip():
            return [0.0] * 384  # all-MiniLM-L6-v2 dimension

        embedding = self._model.encode(text, convert_to_numpy=True)
        return embedding.tolist()  # type: ignore[no-any-return]

    @property
    def is_available(self) -> bool:
        self._load_model()
        return self._available or False

    @property
    def dimension(self) -> int:
        return 384
