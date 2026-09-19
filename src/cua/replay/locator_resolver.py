"""Multi-tier locator resolver — finds elements during replay using a fallback chain.

This is where the rubber meets the road for deterministic replay. The resolver
tries multiple strategies to find an element, in order of stability:

Tier 1: Accessibility (role + name) — most stable, semantic
Tier 2: Text content — visible text match
Tier 3: Label proximity — input near a label with matching text
Tier 4: Semantic fingerprint — embedding-based similarity (B1 feature)
Tier 5: CSS selector — structural, fragile
Tier 6: XPath — positional, most fragile

If all tiers fail, the resolver can optionally trigger an assisted LLM fallback
(bounded to a single recovery attempt) before escalating to human intervention.

Why this order?
- Accessibility: browsers guarantee this is stable if the app semantics don't change
- Text: visible text is what humans use to identify elements, fairly stable
- Label proximity: works even when the input itself has no accessible name
- Semantic fingerprint: catches cases where text/labels changed but the element's
  purpose is the same (our beyond-scope ML feature)
- CSS/XPath: last resort, breaks when DOM structure changes
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog

from cua.models.capability import ElementTarget, LocatorStrategy, LocatorType
from cua.models.fingerprint import (
    ElementFingerprint,
    EmbeddingProvider,
    cosine_similarity,
    score_fingerprint_match,
)
from cua.surfaces.playwright_adapter import PlaywrightAdapter

logger = structlog.get_logger(component="locator_resolver")


@dataclass
class ResolutionResult:
    """Result of resolving an element target to a concrete selector."""

    found: bool
    selector: str = ""
    strategy_used: LocatorType | None = None
    attempts: int = 0
    confidence: float = 0.0
    error: str = ""


class LocatorResolver:
    """Resolves ElementTargets to concrete selectors during replay."""

    def __init__(
        self,
        surface: PlaywrightAdapter,
        fingerprints: dict[str, ElementFingerprint] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        match_threshold: float = 0.80,
        assisted_threshold: float = 0.65,
    ) -> None:
        self._surface = surface
        self._fingerprints = fingerprints or {}
        self._embedding_provider = embedding_provider
        self._match_threshold = match_threshold
        self._assisted_threshold = assisted_threshold

    async def resolve(self, target: ElementTarget, timeout_ms: int = 5_000) -> ResolutionResult:
        """Try each locator strategy in order until one succeeds.

        Returns a ResolutionResult indicating whether the element was found,
        which strategy worked, and the concrete selector to use.
        """
        attempts = 0

        for strategy in target.strategies:
            attempts += 1
            result = await self._try_strategy(strategy, timeout_ms)
            if result.found:
                result.attempts = attempts
                return result

        # Tier 4: Semantic fingerprint fallback (if available)
        if target.fingerprint_id and target.fingerprint_id in self._fingerprints:
            attempts += 1
            result = await self._try_fingerprint_match(
                self._fingerprints[target.fingerprint_id], timeout_ms
            )
            if result.found:
                result.attempts = attempts
                return result

        return ResolutionResult(
            found=False,
            attempts=attempts,
            error=f"All {attempts} locator strategies failed for: {target.description}",
        )

    async def _try_strategy(self, strategy: LocatorStrategy, timeout_ms: int) -> ResolutionResult:
        """Try a single locator strategy."""
        try:
            if strategy.type == LocatorType.ACCESSIBILITY:
                return await self._resolve_accessibility(strategy, timeout_ms)
            elif strategy.type == LocatorType.TEXT_CONTENT:
                return await self._resolve_text_content(strategy, timeout_ms)
            elif strategy.type == LocatorType.LABEL_PROXIMITY:
                return await self._resolve_label(strategy, timeout_ms)
            elif strategy.type == LocatorType.CSS_SELECTOR:
                return await self._resolve_css(strategy, timeout_ms)
            elif strategy.type == LocatorType.XPATH:
                return await self._resolve_xpath(strategy, timeout_ms)
            else:
                return ResolutionResult(found=False, error=f"Unknown strategy: {strategy.type}")
        except Exception as e:
            return ResolutionResult(found=False, error=str(e))

    async def _resolve_accessibility(self, strategy: LocatorStrategy, timeout_ms: int) -> ResolutionResult:
        """Resolve by accessibility role + name."""
        attrs = strategy.attributes
        role = attrs.get("role", "")
        name = attrs.get("name", "")

        if not role or not name:
            return ResolutionResult(found=False, error="Accessibility strategy requires role and name")

        selector = await self._surface.find_by_accessibility(role, name, timeout_ms)
        if selector:
            return ResolutionResult(
                found=True,
                selector=selector,
                strategy_used=LocatorType.ACCESSIBILITY,
                confidence=strategy.confidence,
            )
        return ResolutionResult(found=False, error=f"No element with role={role}, name={name!r}")

    async def _resolve_text_content(self, strategy: LocatorStrategy, timeout_ms: int) -> ResolutionResult:
        """Resolve by visible text content."""
        selector = await self._surface.find_by_text(strategy.value, timeout_ms=timeout_ms)
        if selector:
            return ResolutionResult(
                found=True,
                selector=selector,
                strategy_used=LocatorType.TEXT_CONTENT,
                confidence=strategy.confidence,
            )
        return ResolutionResult(found=False, error=f"No element with text: {strategy.value!r}")

    async def _resolve_label(self, strategy: LocatorStrategy, timeout_ms: int) -> ResolutionResult:
        """Resolve by label proximity (input near a label)."""
        selector = await self._surface.find_by_label(strategy.value, timeout_ms)
        if selector:
            return ResolutionResult(
                found=True,
                selector=selector,
                strategy_used=LocatorType.LABEL_PROXIMITY,
                confidence=strategy.confidence,
            )
        return ResolutionResult(found=False, error=f"No input with label: {strategy.value!r}")

    async def _resolve_css(self, strategy: LocatorStrategy, timeout_ms: int) -> ResolutionResult:
        """Resolve by CSS selector."""
        found = await self._surface.wait_for_element(strategy.value, timeout_ms)
        if found:
            return ResolutionResult(
                found=True,
                selector=strategy.value,
                strategy_used=LocatorType.CSS_SELECTOR,
                confidence=strategy.confidence,
            )
        return ResolutionResult(found=False, error=f"CSS selector not found: {strategy.value}")

    async def _resolve_xpath(self, strategy: LocatorStrategy, timeout_ms: int) -> ResolutionResult:
        """Resolve by XPath."""
        try:
            element = await self._surface.page.wait_for_selector(
                f"xpath={strategy.value}", timeout=timeout_ms
            )
            if element:
                return ResolutionResult(
                    found=True,
                    selector=f"xpath={strategy.value}",
                    strategy_used=LocatorType.XPATH,
                    confidence=strategy.confidence,
                )
        except Exception:
            pass
        return ResolutionResult(found=False, error=f"XPath not found: {strategy.value}")

    async def _try_fingerprint_match(
        self, fingerprint: ElementFingerprint, timeout_ms: int
    ) -> ResolutionResult:
        """Try to find an element using semantic fingerprint matching.

        This is the beyond-scope B1 feature: when all exact-match locators fail,
        enumerate all interactive elements, compute their fingerprints, and find
        the nearest neighbor by embedding similarity.
        """
        if not self._embedding_provider or not self._embedding_provider.is_available:
            return ResolutionResult(
                found=False,
                strategy_used=LocatorType.SEMANTIC_FINGERPRINT,
                error="Embedding provider not available",
            )

        # Enumerate all interactive elements on the page
        elements = await self._surface.find_elements()
        if not elements:
            return ResolutionResult(found=False, error="No interactive elements found on page")

        best_score = 0.0
        best_index = -1

        for i, elem in enumerate(elements):
            # Build a candidate fingerprint
            candidate = ElementFingerprint(
                id=f"candidate_{i}",
                step_id="",
                visible_text=elem.text or elem.name,
                aria_label=elem.name,
                element_type=f"{elem.tag}[role={elem.role}]" if elem.tag else elem.role,
            )

            # Compute embeddings for the candidate
            candidate_text = candidate.text_for_embedding()
            if candidate_text:
                candidate.text_embedding = self._embedding_provider.embed(candidate_text)

            # Score the match
            match_result = score_fingerprint_match(fingerprint, candidate)
            if match_result.overall_score > best_score:
                best_score = match_result.overall_score
                best_index = i

        if best_score >= self._match_threshold and best_index >= 0:
            elem = elements[best_index]
            # Build a Playwright selector from the matched element
            selector = f"role={elem.role}[name={json.dumps(elem.name)}]"
            return ResolutionResult(
                found=True,
                selector=selector,
                strategy_used=LocatorType.SEMANTIC_FINGERPRINT,
                confidence=best_score,
            )

        if best_score >= self._assisted_threshold:
            # Medium confidence — could trigger assisted fallback
            return ResolutionResult(
                found=False,
                strategy_used=LocatorType.SEMANTIC_FINGERPRINT,
                confidence=best_score,
                error=f"Fingerprint match below threshold ({best_score:.2f} < {self._match_threshold}), "
                      f"but above assisted threshold ({self._assisted_threshold}). "
                      f"Assisted LLM fallback could be triggered.",
            )

        return ResolutionResult(
            found=False,
            strategy_used=LocatorType.SEMANTIC_FINGERPRINT,
            confidence=best_score,
            error=f"No fingerprint match above threshold (best: {best_score:.2f})",
        )
