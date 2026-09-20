"""Tests for compliance audit trail with cryptographic hash chain."""

import json
import tempfile
from pathlib import Path

from cua.observability.audit_trail import (
    AuditAction,
    AuditActor,
    AuditChain,
    AuditEntry,
)


class TestAuditEntry:
    def test_hash_deterministic(self) -> None:
        entry = AuditEntry(
            sequence=0,
            timestamp="2026-01-01T00:00:00Z",
            actor=AuditActor.SYSTEM,
            action=AuditAction.STEP_EXECUTED,
            detail={"step_id": "step_01"},
            previous_hash="0" * 64,
        )
        assert entry.entry_hash == entry.compute_hash()

    def test_different_data_different_hash(self) -> None:
        common = dict(
            sequence=0,
            timestamp="2026-01-01T00:00:00Z",
            actor=AuditActor.SYSTEM,
            previous_hash="0" * 64,
        )
        e1 = AuditEntry(action=AuditAction.STEP_EXECUTED, detail={"step": "a"}, **common)
        e2 = AuditEntry(action=AuditAction.STEP_EXECUTED, detail={"step": "b"}, **common)
        assert e1.entry_hash != e2.entry_hash


class TestAuditChain:
    def test_empty_chain_valid(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        valid, msg = chain.verify()
        assert valid

    def test_append_creates_chain(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_START, {"goal": "test"})
        chain.append(AuditActor.SYSTEM, AuditAction.STEP_EXECUTED, {"step_id": "step_01"})
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_COMPLETE, {"status": "success"})

        assert len(chain.entries) == 3
        # Each entry chains to the previous
        assert chain.entries[1].previous_hash == chain.entries[0].entry_hash
        assert chain.entries[2].previous_hash == chain.entries[1].entry_hash

    def test_chain_verification_passes(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_START)
        chain.append(AuditActor.AGENT, AuditAction.STEP_EXECUTED, {"step": 1})
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_COMPLETE)

        valid, msg = chain.verify()
        assert valid
        assert "3 entries" in msg

    def test_tampered_entry_detected(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_START)
        chain.append(AuditActor.SYSTEM, AuditAction.STEP_EXECUTED, {"step": 1})

        # Tamper with the first entry
        chain.entries[0].detail["step"] = "tampered"

        valid, msg = chain.verify()
        assert not valid
        assert "hash mismatch" in msg

    def test_broken_chain_detected(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_START)
        chain.append(AuditActor.SYSTEM, AuditAction.STEP_EXECUTED)

        # Break the chain linkage — since previous_hash is part of the hash
        # input, this also invalidates the entry's own hash, which is detected first
        chain.entries[1].previous_hash = "deadbeef" * 8

        valid, msg = chain.verify()
        assert not valid
        assert "hash mismatch" in msg or "chain broken" in msg

    def test_save_and_load_roundtrip(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_START)
        chain.append(AuditActor.HUMAN, AuditAction.HUMAN_ACTION, {"action": "click"})
        chain.append(AuditActor.POLICY, AuditAction.DATA_REDACTED, {"field": "ssn"})

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            chain.save(f.name)
            loaded = AuditChain.load(f.name)

        assert len(loaded.entries) == 3
        valid, msg = loaded.verify()
        assert valid

    def test_human_intervention_tracked(self) -> None:
        chain = AuditChain(run_id="run-1", capability_id="cap-1")
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_START)
        chain.append(AuditActor.SYSTEM, AuditAction.HUMAN_INTERVENTION_START,
                     {"reason": "stuck", "step_id": "step_05"})
        chain.append(AuditActor.HUMAN, AuditAction.HUMAN_ACTION,
                     {"action": "typed_credentials"})
        chain.append(AuditActor.SYSTEM, AuditAction.HUMAN_INTERVENTION_END)
        chain.append(AuditActor.SYSTEM, AuditAction.RUN_COMPLETE)

        valid, _ = chain.verify()
        assert valid
        assert chain.entries[2].actor == AuditActor.HUMAN
