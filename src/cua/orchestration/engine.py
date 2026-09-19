"""Workflow DAG executor — runs composed capabilities with data flow.

BEYOND-SCOPE FEATURE B3

Executes a WorkflowDefinition (a DAG of capability steps) by:
1. Topologically sorting the steps
2. Evaluating conditions (skip steps whose conditions are false)
3. Resolving input mappings (wire upstream outputs to downstream inputs)
4. Executing each capability via the replay engine
5. Collecting outputs and passing them downstream
6. Handling failures with compensating actions

The executor shares a single browser session across all capabilities in
the workflow, preserving state (cookies, auth, navigation) between steps.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from cua.models.workflow import (
    OnFailurePolicy,
    WorkflowDefinition,
    WorkflowResult,
    WorkflowStepResult,
    WorkflowStepType,
)

logger = structlog.get_logger(component="orchestration")


class WorkflowEngine:
    """Executes workflow DAGs composed of capability invocations."""

    def __init__(self, capability_executor: Any) -> None:
        """Initialize with a replay executor for running individual capabilities.

        The capability_executor must have an `execute(capability, params)` method.
        """
        self._executor = capability_executor
        self._capability_registry: dict[str, Any] = {}

    def register_capability(self, name: str, capability: Any) -> None:
        """Register a capability by name so workflow steps can reference it."""
        self._capability_registry[name] = capability

    async def execute(
        self,
        workflow: WorkflowDefinition,
        inputs: dict[str, Any],
    ) -> WorkflowResult:
        """Execute a workflow with the given inputs."""
        # Validate the DAG
        errors = workflow.validate_dag()
        if errors:
            return WorkflowResult(
                workflow_name=workflow.name,
                status="failed",
                step_results=[],
                outputs={},
                completed_at=datetime.now(timezone.utc),
            )

        # Topologically sort steps
        sorted_steps = workflow.topological_sort()

        # Context: accumulated step outputs for data flow
        step_outputs: dict[str, dict[str, Any]] = {}
        step_results: list[WorkflowStepResult] = []
        workflow_outputs: dict[str, Any] = {}

        start_time = time.time()

        logger.info("workflow_start", name=workflow.name, steps=len(sorted_steps))

        for step in sorted_steps:
            step_start = time.time()

            # Evaluate condition
            if step.condition:
                condition_met = self._evaluate_condition(
                    step.condition, inputs, step_outputs
                )
                if not condition_met:
                    step_results.append(WorkflowStepResult(
                        step_id=step.id,
                        status="skipped",
                        skipped_reason=f"Condition not met: {step.condition}",
                    ))
                    logger.info("step_skipped", step_id=step.id, condition=step.condition)
                    continue

            # Resolve input mappings
            resolved_inputs = self._resolve_inputs(
                step.input_mapping, inputs, step_outputs
            )

            # Execute based on step type
            if step.type == WorkflowStepType.CAPABILITY:
                if not step.capability_name:
                    step_results.append(WorkflowStepResult(
                        step_id=step.id,
                        status="failed",
                        error="No capability_name specified",
                    ))
                    continue

                capability = self._capability_registry.get(step.capability_name)
                if not capability:
                    step_results.append(WorkflowStepResult(
                        step_id=step.id,
                        status="failed",
                        error=f"Capability '{step.capability_name}' not found in registry",
                    ))

                    if step.on_failure == OnFailurePolicy.ABORT:
                        break
                    continue

                # Execute the capability
                try:
                    run_result = await self._executor.execute(capability, resolved_inputs)
                    step_duration = (time.time() - step_start) * 1_000

                    if run_result.is_success():
                        # Extract outputs using the mapping
                        extracted = self._extract_outputs(
                            step.output_mapping, run_result.outputs
                        )
                        step_outputs[step.id] = {"outputs": extracted}
                        workflow_outputs.update(extracted)

                        step_results.append(WorkflowStepResult(
                            step_id=step.id,
                            status="success",
                            outputs=extracted,
                            run_id=run_result.run_id,
                            duration_ms=step_duration,
                        ))
                        logger.info(
                            "step_complete",
                            step_id=step.id,
                            status="success",
                            outputs=list(extracted.keys()),
                        )

                    elif run_result.is_business_outcome():
                        outcome = run_result.business_outcome
                        step_outputs[step.id] = {
                            "outputs": {},
                            "business_outcome": outcome.outcome_name if outcome else "unknown",
                        }
                        step_results.append(WorkflowStepResult(
                            step_id=step.id,
                            status="business_outcome",
                            run_id=run_result.run_id,
                            error=outcome.message if outcome else "Business outcome",
                            duration_ms=step_duration,
                        ))

                        if step.on_failure == OnFailurePolicy.ABORT:
                            break

                    else:
                        # Failure
                        error_msg = run_result.failure.message if run_result.failure else "Unknown failure"
                        step_results.append(WorkflowStepResult(
                            step_id=step.id,
                            status="failed",
                            run_id=run_result.run_id,
                            error=error_msg,
                            duration_ms=step_duration,
                        ))

                        if step.on_failure == OnFailurePolicy.ABORT:
                            break
                        elif step.on_failure == OnFailurePolicy.COMPENSATE:
                            await self._run_compensation(step, inputs, step_outputs)

                except Exception as e:
                    step_results.append(WorkflowStepResult(
                        step_id=step.id,
                        status="failed",
                        error=str(e),
                        duration_ms=(time.time() - step_start) * 1_000,
                    ))
                    if step.on_failure == OnFailurePolicy.ABORT:
                        break

        # Determine overall status
        failed = any(r.status == "failed" for r in step_results)
        all_success = all(r.status in ("success", "skipped") for r in step_results)
        total_duration = (time.time() - start_time) * 1_000

        status = "success" if all_success else ("failed" if failed else "partially_completed")

        result = WorkflowResult(
            workflow_name=workflow.name,
            status=status,
            step_results=step_results,
            outputs=workflow_outputs,
            completed_at=datetime.now(timezone.utc),
            duration_ms=total_duration,
        )

        logger.info("workflow_complete", name=workflow.name, status=status, duration_ms=f"{total_duration:.0f}")
        return result

    def _resolve_inputs(
        self,
        mapping: dict[str, str],
        workflow_inputs: dict[str, Any],
        step_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve input mapping expressions to concrete values."""
        resolved: dict[str, Any] = {}
        for param_name, expression in mapping.items():
            resolved[param_name] = self._evaluate_expression(
                expression, workflow_inputs, step_outputs
            )
        return resolved

    def _extract_outputs(
        self, mapping: dict[str, str], run_outputs: dict[str, Any]
    ) -> dict[str, Any]:
        """Extract outputs from a run result using the output mapping."""
        extracted: dict[str, Any] = {}
        for output_name, expression in mapping.items():
            # Simple path extraction: $.extracted.field_name
            if expression.startswith("$.extracted."):
                field = expression[len("$.extracted."):]
                extracted[output_name] = run_outputs.get(field)
            else:
                extracted[output_name] = run_outputs.get(expression, expression)
        return extracted

    def _evaluate_expression(
        self,
        expression: str,
        workflow_inputs: dict[str, Any],
        step_outputs: dict[str, dict[str, Any]],
    ) -> Any:
        """Evaluate a ${{ ... }} expression."""
        # Extract the expression content
        match = re.match(r"^\$\{\{\s*(.+?)\s*\}\}$", expression)
        if not match:
            return expression  # Literal value

        expr = match.group(1)

        # workflow.inputs.X
        if expr.startswith("workflow.inputs."):
            key = expr[len("workflow.inputs."):]
            return workflow_inputs.get(key)

        # steps.Y.outputs.Z
        step_match = re.match(r"steps\.(\w+)\.outputs\.(\w+)", expr)
        if step_match:
            step_id = step_match.group(1)
            output_key = step_match.group(2)
            step_data = step_outputs.get(step_id, {})
            return step_data.get("outputs", {}).get(output_key)

        return expression

    def _evaluate_condition(
        self,
        condition: str,
        workflow_inputs: dict[str, Any],
        step_outputs: dict[str, dict[str, Any]],
    ) -> bool:
        """Evaluate a condition expression. Returns True if the condition is met."""
        # Extract expression
        match = re.match(r"^\$\{\{\s*(.+?)\s*\}\}$", condition)
        if not match:
            return bool(condition)

        expr = match.group(1)

        # Simple comparison: steps.X.outputs.Y < 1000.00
        comp_match = re.match(r"steps\.(\w+)\.outputs\.(\w+)\s*([<>=!]+)\s*(.+)", expr)
        if comp_match:
            step_id = comp_match.group(1)
            output_key = comp_match.group(2)
            operator = comp_match.group(3)
            threshold_str = comp_match.group(4).strip()

            step_data = step_outputs.get(step_id, {})
            value = step_data.get("outputs", {}).get(output_key)

            if value is None:
                return False

            try:
                threshold = float(threshold_str)
                value_f = float(value)
                if operator == "<":
                    return value_f < threshold
                elif operator == ">":
                    return value_f > threshold
                elif operator in ("==", "="):
                    return value_f == threshold
                elif operator in ("!=", "<>"):
                    return value_f != threshold
                elif operator == "<=":
                    return value_f <= threshold
                elif operator == ">=":
                    return value_f >= threshold
            except (ValueError, TypeError):
                return False

        return True

    async def _run_compensation(
        self,
        step: Any,
        inputs: dict[str, Any],
        step_outputs: dict[str, dict[str, Any]],
    ) -> None:
        """Run a compensating capability for rollback semantics."""
        if not step.compensating_capability:
            return

        comp_capability = self._capability_registry.get(step.compensating_capability)
        if not comp_capability:
            logger.warning(
                "compensation_not_found",
                step_id=step.id,
                compensating=step.compensating_capability,
            )
            return

        logger.info("running_compensation", step_id=step.id, compensating=step.compensating_capability)
        try:
            await self._executor.execute(comp_capability, inputs)
        except Exception as e:
            logger.error("compensation_failed", step_id=step.id, error=str(e))
