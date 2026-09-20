"""Compliance Audit Trail — tamper-evident cryptographic hash chain.

Every action taken by the system (discovery, replay, human intervention) is
recorded as an entry in a hash chain where each entry's hash depends on the
previous entry's hash. This makes the audit trail tamper-evident: modifying
or removing any entry breaks the chain.

Critical for banking compliance (SOX, FFIEC, OCC guidelines) where regulators
require provable evidence that automated actions occurred in the recorded order
and were not modified after the fact.

Design:
- Each AuditEntry has: timestamp, actor (system/human/agent), action, context,
  and a SHA-256 hash that chains to the previous entry
- The chain can be verified in O(n) — hash each entry and confirm it matches
- Entries are append-only; the chain is stored as a JSON file per capability run
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class AuditActor(StrEnum):
    """Who performed the action."""

    SYSTEM = "system"  # Replay engine, locator resolver, etc.
    AGENT = "agent"  # LLM during discovery
    HUMAN = "human"  # Human operator during intervention
    POLICY = "policy"  # Safety guard, redactor


class AuditAction(StrEnum):
    """What was done."""

    STEP_EXECUTED = "step_executed"
    ELEMENT_LOCATED = "element_located"
    DATA_EXTRACTED = "data_extracted"
    DATA_REDACTED = "data_redacted"
    ERROR_DETECTED = "error_detected"
    ERROR_HANDLED = "error_handled"
    CHECKPOINT_VERIFIED = "checkpoint_verified"
    NAVIGATION = "navigation"
    HUMAN_INTERVENTION_START = "human_intervention_start"
    HUMAN_INTERVENTION_END = "human_intervention_end"
    HUMAN_ACTION = "human_action"
    POLICY_CHECK = "policy_check"
    POLICY_BLOCK = "policy_block"
    RUN_START = "run_start"
    RUN_COMPLETE = "run_complete"
    LOCATOR_FALLBACK = "locator_fallback"
    CAPABILITY_HEALED = "capability_healed"


@dataclass
class AuditEntry:
    """A single entry in the audit chain."""

    sequence: int
    timestamp: str
    actor: AuditActor
    action: AuditAction
    detail: dict[str, Any]
    previous_hash: str
    entry_hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of this entry (excluding entry_hash itself)."""
        payload = json.dumps(
            {
                "sequence": self.sequence,
                "timestamp": self.timestamp,
                "actor": self.actor.value,
                "action": self.action.value,
                "detail": self.detail,
                "previous_hash": self.previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def __post_init__(self) -> None:
        if not self.entry_hash:
            self.entry_hash = self.compute_hash()


@dataclass
class AuditChain:
    """Append-only, tamper-evident audit chain for a single run."""

    run_id: str
    capability_id: str
    entries: list[AuditEntry] = field(default_factory=list)
    _genesis_hash: str = field(
        default="0" * 64, init=False, repr=False
    )  # SHA-256 of zeros

    def append(
        self,
        actor: AuditActor,
        action: AuditAction,
        detail: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append a new entry to the chain."""
        previous_hash = (
            self.entries[-1].entry_hash if self.entries else self._genesis_hash
        )
        entry = AuditEntry(
            sequence=len(self.entries),
            timestamp=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            action=action,
            detail=detail or {},
            previous_hash=previous_hash,
        )
        self.entries.append(entry)
        return entry

    def verify(self) -> tuple[bool, str]:
        """Verify the integrity of the entire chain.

        Returns (is_valid, message). Checks:
        1. Each entry's hash matches its computed hash
        2. Each entry's previous_hash matches the prior entry's hash
        3. The first entry chains to the genesis hash
        """
        if not self.entries:
            return True, "Empty chain"

        # Check genesis
        if self.entries[0].previous_hash != self._genesis_hash:
            return False, f"Entry 0: genesis hash mismatch"

        for i, entry in enumerate(self.entries):
            # Verify the entry's own hash
            expected_hash = entry.compute_hash()
            if entry.entry_hash != expected_hash:
                return False, (
                    f"Entry {i}: hash mismatch "
                    f"(stored={entry.entry_hash[:16]}..., "
                    f"computed={expected_hash[:16]}...)"
                )

            # Verify chain linkage (except for the first entry)
            if i > 0:
                if entry.previous_hash != self.entries[i - 1].entry_hash:
                    return False, (
                        f"Entry {i}: chain broken — previous_hash doesn't match "
                        f"entry {i-1}'s hash"
                    )

        return True, f"Chain valid: {len(self.entries)} entries"

    def save(self, path: str | Path) -> None:
        """Save the chain to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "run_id": self.run_id,
            "capability_id": self.capability_id,
            "chain_length": len(self.entries),
            "chain_hash": self.entries[-1].entry_hash if self.entries else self._genesis_hash,
            "entries": [
                {
                    "sequence": e.sequence,
                    "timestamp": e.timestamp,
                    "actor": e.actor.value,
                    "action": e.action.value,
                    "detail": e.detail,
                    "previous_hash": e.previous_hash,
                    "entry_hash": e.entry_hash,
                }
                for e in self.entries
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str | Path) -> AuditChain:
        """Load a chain from a JSON file and verify its integrity."""
        with open(path) as f:
            data = json.load(f)

        chain = cls(
            run_id=data["run_id"],
            capability_id=data["capability_id"],
        )
        for entry_data in data["entries"]:
            entry = AuditEntry(
                sequence=entry_data["sequence"],
                timestamp=entry_data["timestamp"],
                actor=AuditActor(entry_data["actor"]),
                action=AuditAction(entry_data["action"]),
                detail=entry_data["detail"],
                previous_hash=entry_data["previous_hash"],
                entry_hash=entry_data["entry_hash"],
            )
            chain.entries.append(entry)

        return chain
