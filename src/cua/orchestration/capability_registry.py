"""Intelligent Capability Registry — TF-IDF based natural language search.

Allows AI agents to discover capabilities by natural language query rather than
knowing exact capability names. This is the "agent-facing capability interface"
stretch goal — an AI agent can search "check member savings balance" and get
back the matching capability with its typed contract.

Implementation uses TF-IDF (Term Frequency-Inverse Document Frequency) for
ranking without any external ML dependencies. Each capability's name,
description, tags, parameter descriptions, and output descriptions are indexed.

Why TF-IDF instead of embeddings?
- Zero dependencies (no model download, no GPU, no API call)
- Deterministic and fast (~1ms for 100 capabilities)
- Works air-gapped (banking environments)
- Good enough for a catalog of dozens to hundreds of capabilities
- Embeddings (B1 feature) are reserved for element fingerprinting where
  semantic similarity matters more
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cua.models.capability import Capability


@dataclass
class CapabilityIndex:
    """Indexed capability for search."""

    capability_id: str
    name: str
    description: str
    file_path: str
    tags: list[str]
    input_params: list[str]
    output_names: list[str]
    # TF-IDF fields
    terms: list[str] = field(default_factory=list)
    tf: dict[str, float] = field(default_factory=dict)


@dataclass
class SearchResult:
    """A capability search result with relevance score."""

    capability_id: str
    name: str
    description: str
    file_path: str
    score: float
    tags: list[str]
    input_params: list[str]
    output_names: list[str]


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase terms, removing stopwords."""
    stopwords = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "it", "this", "that", "are", "was",
        "be", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "can", "not", "no", "so", "if", "then", "than",
    }
    # Split on non-alphanumeric, lowercase, filter stopwords and short tokens
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in stopwords and len(t) > 1]


class CapabilityRegistry:
    """Searchable registry of capability artifacts.

    Usage:
        registry = CapabilityRegistry()
        registry.load_from_directory("capabilities/")

        results = registry.search("check member savings balance")
        for result in results:
            print(f"{result.name}: {result.score:.2f}")
    """

    def __init__(self) -> None:
        self._index: list[CapabilityIndex] = []
        self._idf: dict[str, float] = {}
        self._indexed = False

    def load_from_directory(self, directory: str | Path) -> int:
        """Load and index all capability YAML files from a directory."""
        directory = Path(directory)
        count = 0
        for yaml_file in sorted(directory.glob("*.yaml")):
            if yaml_file.name.startswith("."):
                continue
            try:
                self._load_capability(yaml_file)
                count += 1
            except Exception:
                continue

        self._build_idf()
        self._indexed = True
        return count

    def add_capability(self, capability: Capability, file_path: str = "") -> None:
        """Add a single capability to the index."""
        text_parts = [
            capability.name,
            capability.description,
            " ".join(capability.tags),
        ]
        for param in capability.input_parameters:
            text_parts.append(param.name)
            text_parts.append(param.description)
        for output in capability.outputs:
            text_parts.append(output.name)
            text_parts.append(output.description)

        full_text = " ".join(text_parts)
        terms = _tokenize(full_text)

        # Compute term frequency
        tf: dict[str, float] = {}
        for term in terms:
            tf[term] = tf.get(term, 0) + 1
        # Normalize by document length
        doc_len = len(terms) if terms else 1
        tf = {term: count / doc_len for term, count in tf.items()}

        entry = CapabilityIndex(
            capability_id=capability.id,
            name=capability.name,
            description=capability.description,
            file_path=file_path,
            tags=capability.tags,
            input_params=[p.name for p in capability.input_parameters],
            output_names=[o.name for o in capability.outputs],
            terms=terms,
            tf=tf,
        )
        self._index.append(entry)
        self._indexed = False  # Need to rebuild IDF

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Search capabilities by natural language query using TF-IDF scoring."""
        if not self._indexed:
            self._build_idf()
            self._indexed = True

        query_terms = _tokenize(query)
        if not query_terms:
            return []

        results: list[tuple[float, CapabilityIndex]] = []

        for entry in self._index:
            score = self._score_document(query_terms, entry)
            if score > 0:
                results.append((score, entry))

        # Sort by score descending
        results.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchResult(
                capability_id=entry.capability_id,
                name=entry.name,
                description=entry.description,
                file_path=entry.file_path,
                score=round(score, 4),
                tags=entry.tags,
                input_params=entry.input_params,
                output_names=entry.output_names,
            )
            for score, entry in results[:max_results]
        ]

    def list_all(self) -> list[SearchResult]:
        """List all indexed capabilities."""
        return [
            SearchResult(
                capability_id=entry.capability_id,
                name=entry.name,
                description=entry.description,
                file_path=entry.file_path,
                score=1.0,
                tags=entry.tags,
                input_params=entry.input_params,
                output_names=entry.output_names,
            )
            for entry in self._index
        ]

    def _load_capability(self, yaml_file: Path) -> None:
        """Load a YAML file and add it to the index."""
        with open(yaml_file) as f:
            data = yaml.safe_load(f)

        capability = Capability.model_validate(data)
        self.add_capability(capability, str(yaml_file))

    def _build_idf(self) -> None:
        """Build IDF (Inverse Document Frequency) across all documents."""
        n_docs = len(self._index)
        if n_docs == 0:
            return

        # Count how many documents contain each term
        doc_freq: dict[str, int] = {}
        for entry in self._index:
            unique_terms = set(entry.terms)
            for term in unique_terms:
                doc_freq[term] = doc_freq.get(term, 0) + 1

        # IDF = log(N / df) + 1 (smoothed)
        self._idf = {
            term: math.log(n_docs / df) + 1
            for term, df in doc_freq.items()
        }

    def _score_document(
        self, query_terms: list[str], entry: CapabilityIndex
    ) -> float:
        """Compute TF-IDF cosine similarity between query and document."""
        score = 0.0
        for term in query_terms:
            if term in entry.tf:
                tf = entry.tf[term]
                idf = self._idf.get(term, 1.0)
                score += tf * idf

        return score
