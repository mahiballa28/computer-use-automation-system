"""Tests for workflow DAG validation and execution."""

from __future__ import annotations

import pytest

from cua.models.workflow import (
    OnFailurePolicy,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowStepType,
)


class TestWorkflowValidation:
    """Test DAG validation."""

    def test_valid_linear_dag(self) -> None:
        wf = WorkflowDefinition(
            name="test_linear",
            description="A simple linear workflow",
            steps=[
                WorkflowStep(id="a", description="Step A", capability_name="cap_a"),
                WorkflowStep(id="b", description="Step B", capability_name="cap_b", depends_on=["a"]),
                WorkflowStep(id="c", description="Step C", capability_name="cap_c", depends_on=["b"]),
            ],
        )
        errors = wf.validate_dag()
        assert len(errors) == 0

    def test_cycle_detected(self) -> None:
        wf = WorkflowDefinition(
            name="test_cycle",
            description="Workflow with a cycle",
            steps=[
                WorkflowStep(id="a", description="Step A", capability_name="cap_a", depends_on=["c"]),
                WorkflowStep(id="b", description="Step B", capability_name="cap_b", depends_on=["a"]),
                WorkflowStep(id="c", description="Step C", capability_name="cap_c", depends_on=["b"]),
            ],
        )
        errors = wf.validate_dag()
        assert any("cycle" in e.lower() for e in errors)

    def test_missing_dependency(self) -> None:
        wf = WorkflowDefinition(
            name="test_missing",
            description="Workflow with missing dep",
            steps=[
                WorkflowStep(id="a", description="Step A", capability_name="cap_a", depends_on=["nonexistent"]),
            ],
        )
        with pytest.raises(ValueError, match="unknown step"):
            wf.topological_sort()

    def test_duplicate_ids(self) -> None:
        wf = WorkflowDefinition(
            name="test_dup",
            description="Duplicate IDs",
            steps=[
                WorkflowStep(id="a", description="Step A", capability_name="cap_a"),
                WorkflowStep(id="a", description="Step A again", capability_name="cap_b"),
            ],
        )
        errors = wf.validate_dag()
        assert any("duplicate" in e.lower() for e in errors)

    def test_missing_capability_name(self) -> None:
        wf = WorkflowDefinition(
            name="test_no_cap",
            description="No capability name",
            steps=[
                WorkflowStep(id="a", description="Step A", type=WorkflowStepType.CAPABILITY),
            ],
        )
        errors = wf.validate_dag()
        assert any("capability_name" in e.lower() for e in errors)

    def test_topological_sort_order(self) -> None:
        wf = WorkflowDefinition(
            name="test_sort",
            description="Test topological sort",
            steps=[
                WorkflowStep(id="c", description="Step C", capability_name="cap_c", depends_on=["a", "b"]),
                WorkflowStep(id="a", description="Step A", capability_name="cap_a"),
                WorkflowStep(id="b", description="Step B", capability_name="cap_b", depends_on=["a"]),
            ],
        )
        sorted_steps = wf.topological_sort()
        ids = [s.id for s in sorted_steps]
        assert ids.index("a") < ids.index("b")
        assert ids.index("a") < ids.index("c")
        assert ids.index("b") < ids.index("c")

    def test_parallel_branches(self) -> None:
        """Two independent branches that converge."""
        wf = WorkflowDefinition(
            name="test_parallel",
            description="Parallel branches",
            steps=[
                WorkflowStep(id="start", description="Start", capability_name="cap_start"),
                WorkflowStep(id="branch_a", description="Branch A", capability_name="cap_a", depends_on=["start"]),
                WorkflowStep(id="branch_b", description="Branch B", capability_name="cap_b", depends_on=["start"]),
                WorkflowStep(id="merge", description="Merge", capability_name="cap_merge", depends_on=["branch_a", "branch_b"]),
            ],
        )
        errors = wf.validate_dag()
        assert len(errors) == 0
        sorted_steps = wf.topological_sort()
        ids = [s.id for s in sorted_steps]
        assert ids[0] == "start"
        assert ids[-1] == "merge"
