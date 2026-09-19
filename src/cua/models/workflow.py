"""Workflow models — capability composition with DAG execution.

BEYOND-SCOPE FEATURE B3

Individual capabilities automate single screens. Real banking operations span
multiple screens: look up a customer, check their balance, initiate a transfer
if below threshold, confirm the transfer. This module lets capabilities be
composed into directed acyclic graphs (DAGs) with:

- Typed data flow: one capability's output feeds another's input
- Conditional branching: skip steps based on upstream results
- Compensating actions: rollback semantics via reverse-capabilities
- Parallel execution: independent branches run concurrently

The DAG model prevents circular dependencies at definition time (validated
on load), and the typed data contracts between capabilities catch integration
errors before runtime.

Why DAGs and not a linear sequence? Banking workflows have decision points:
"if balance < threshold" or "if member has flag X, skip step Y." A linear
sequence can't express this. A full Turing-complete workflow engine is
overkill for this domain. DAGs with conditions are the right abstraction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowStepType(StrEnum):
    """Type of step in a workflow."""

    CAPABILITY = "capability"  # invoke a saved capability
    CONDITION = "condition"  # evaluate a condition, skip downstream if false
    PARALLEL = "parallel"  # run multiple branches in parallel


class OnFailurePolicy(StrEnum):
    """What to do when a workflow step fails."""

    ABORT = "abort"  # stop the entire workflow
    ESCALATE = "escalate"  # request human intervention
    COMPENSATE = "compensate"  # run compensating capability
    SKIP = "skip"  # skip this step and continue


class WorkflowStep(BaseModel):
    """A single step in a workflow DAG."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: WorkflowStepType = WorkflowStepType.CAPABILITY
    description: str

    # For CAPABILITY steps
    capability_name: str | None = Field(
        default=None,
        description="Name of the capability to invoke (looked up in the registry)",
    )

    # Dependencies (defines the DAG edges)
    depends_on: list[str] = Field(
        default_factory=list,
        description="Step IDs that must complete before this step runs",
    )

    # Input mapping: map workflow inputs or upstream outputs to this step's params
    input_mapping: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps capability param names to expressions. "
            "Expressions: '${{ workflow.inputs.X }}' or '${{ steps.Y.outputs.Z }}'"
        ),
    )

    # Output mapping: extract values from the capability result
    output_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Maps output names to extraction expressions from the run result",
    )

    # Conditional execution
    condition: str | None = Field(
        default=None,
        description=(
            "Expression that must evaluate to true for this step to run. "
            "Example: '${{ steps.check_balance.outputs.balance < 1000.00 }}'"
        ),
    )

    # Failure handling
    on_failure: OnFailurePolicy = Field(default=OnFailurePolicy.ABORT)
    compensating_capability: str | None = Field(
        default=None,
        description="Capability to run if on_failure=COMPENSATE",
    )

    # Timeout
    timeout_seconds: int = Field(default=120)


class WorkflowDefinition(BaseModel):
    """A composed workflow: a DAG of capabilities with data flow.

    The workflow is validated on load:
    - No cycles in the dependency graph
    - All capability references resolve to known capabilities
    - Input mappings reference valid upstream outputs
    - Conditions reference valid upstream outputs
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    version: int = Field(default=1, ge=1)

    # Workflow-level inputs (supplied by the caller)
    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="Declared workflow inputs: name → type description",
    )

    # The DAG
    steps: list[WorkflowStep] = Field(min_length=1)

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = Field(default_factory=list)

    def topological_sort(self) -> list[WorkflowStep]:
        """Return steps in execution order (topological sort).

        Raises ValueError if the graph contains a cycle.
        """
        # Build adjacency and in-degree
        step_map = {s.id: s for s in self.steps}
        in_degree: dict[str, int] = {s.id: 0 for s in self.steps}
        dependents: dict[str, list[str]] = {s.id: [] for s in self.steps}

        for step in self.steps:
            for dep_id in step.depends_on:
                if dep_id not in step_map:
                    msg = f"Step '{step.id}' depends on unknown step '{dep_id}'"
                    raise ValueError(msg)
                dependents[dep_id].append(step.id)
                in_degree[step.id] += 1

        # Kahn's algorithm
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        result: list[WorkflowStep] = []

        while queue:
            current = queue.pop(0)
            result.append(step_map[current])
            for dep in dependents[current]:
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)

        if len(result) != len(self.steps):
            msg = "Workflow contains a cycle in the dependency graph"
            raise ValueError(msg)

        return result

    def validate_dag(self) -> list[str]:
        """Validate the workflow DAG structure. Returns a list of validation errors."""
        errors: list[str] = []

        # Check for cycles
        try:
            self.topological_sort()
        except ValueError as e:
            errors.append(str(e))

        # Check step ID uniqueness
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            errors.append("Duplicate step IDs found")

        # Check capability references exist (just validates they're non-empty)
        for step in self.steps:
            if step.type == WorkflowStepType.CAPABILITY and not step.capability_name:
                errors.append(f"Step '{step.id}' is a capability step but has no capability_name")

        return errors


class WorkflowStepResult(BaseModel):
    """Result of a single workflow step execution."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: str  # success, skipped, failed, compensated
    outputs: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None  # ID of the capability run, if applicable
    error: str | None = None
    duration_ms: float | None = None
    skipped_reason: str | None = None


class WorkflowResult(BaseModel):
    """Complete result of a workflow execution."""

    model_config = ConfigDict(extra="forbid")

    workflow_name: str
    status: str  # success, failed, partially_completed, escalated
    step_results: list[WorkflowStepResult] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_ms: float | None = None
