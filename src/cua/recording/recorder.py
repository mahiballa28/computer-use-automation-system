"""Recorder — converts a discovery trace into a structured Capability artifact.

After the agent loop completes a goal successfully, the trace contains a sequence
of raw actions (tool calls, page states, screenshots). The Recorder's job is to
post-process this trace into a clean, reusable Capability:

1. Extract the meaningful steps (filter out retries, failed attempts, assertions)
2. Generate multi-strategy locators for each element target
3. Identify input parameters (values that came from the goal)
4. Identify output fields (data the agent extracted)
5. Generate checkpoints for key state transitions
6. Add error handlers for known conditions
7. Serialize to YAML

The result is a Capability that can be replayed deterministically.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from cua.agent.loop import AgentAction, DiscoveryTrace
from cua.models.capability import (
    ActionType,
    ApprovalState,
    Capability,
    Checkpoint,
    CheckpointType,
    ElementTarget,
    ErrorHandler,
    ErrorPolicy,
    LocatorStrategy,
    LocatorType,
    OutputSpec,
    ParameterSpec,
    RiskLevel,
    Step,
    SurfaceTarget,
)


# Mapping from agent tool names to ActionType
TOOL_TO_ACTION: dict[str, ActionType] = {
    "click_element": ActionType.CLICK,
    "type_text": ActionType.TYPE,
    "select_option": ActionType.SELECT,
    "navigate_to": ActionType.NAVIGATE,
    "press_key": ActionType.PRESS_KEY,
    "extract_data": ActionType.EXTRACT,
    "assert_page_state": ActionType.ASSERT,
    "wait_for_page": ActionType.WAIT,
}


class Recorder:
    """Converts a DiscoveryTrace into a Capability artifact."""

    def record(self, trace: DiscoveryTrace) -> Capability:
        """Transform a successful discovery trace into a Capability.

        Only call this on successful traces (trace.success == True).
        """
        if not trace.success:
            msg = "Cannot record a capability from a failed trace"
            raise ValueError(msg)

        # Filter to meaningful actions (skip mark_goal_complete, mark_stuck, retries)
        meaningful_actions = [
            a for a in trace.actions
            if a.tool_name not in ("mark_goal_complete", "mark_stuck")
            and a.success
        ]

        # Build steps
        steps: list[Step] = []
        for i, action in enumerate(meaningful_actions):
            step = self._action_to_step(action, i)
            if step:
                steps.append(step)

        # Extract input parameters from type_text actions flagged as parameters
        input_parameters = self._extract_parameters(trace)

        # Extract outputs from extract_data actions
        outputs = self._extract_outputs(trace, steps)

        # Build success condition from the last page state
        last_action = meaningful_actions[-1] if meaningful_actions else None
        success_condition = self._build_success_condition(trace, last_action)

        # Build error handlers for common conditions
        error_handlers = self._build_error_handlers(trace)

        # Generate a name from the goal
        name = self._goal_to_name(trace.goal)

        capability = Capability(
            id=str(uuid.uuid4()),
            version=1,
            name=name,
            description=trace.goal,
            surface=SurfaceTarget(
                surface_type="web",
                entry_point=trace.target_url,
            ),
            input_parameters=input_parameters,
            outputs=outputs,
            steps=steps,
            success_condition=success_condition,
            error_handlers=error_handlers,
            approval_state=ApprovalState.DRAFT,
            recorded_from_run_id=trace.run_id,
        )

        return capability

    def save(self, capability: Capability, directory: str | Path) -> Path:
        """Save a capability to a YAML file."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        filename = f"{capability.name}.yaml"
        filepath = directory / filename

        # Serialize to dict, then YAML
        data = capability.model_dump(mode="json")
        with open(filepath, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        return filepath

    def _action_to_step(self, action: AgentAction, index: int) -> Step | None:
        """Convert a single agent action to a capability step."""
        tool_name = action.tool_name
        tool_input = action.tool_input

        action_type = TOOL_TO_ACTION.get(tool_name)
        if not action_type:
            return None

        step_id = f"step_{index:02d}"
        description = tool_input.get("description", f"Action: {tool_name}")

        # Build element target for actions that target specific elements
        target = None
        if tool_name in ("click_element", "type_text", "select_option"):
            target = self._build_element_target(tool_input)

        # Build parameters dict
        parameters: dict[str, Any] = {}
        if tool_name == "type_text":
            text = tool_input.get("text", "")
            if tool_input.get("is_parameter"):
                param_name = tool_input.get("parameter_name", "param")
                parameters["text"] = f"{{{{{param_name}}}}}"  # {{param_name}}
            else:
                parameters["text"] = text
        elif tool_name == "select_option":
            parameters["value"] = tool_input.get("value", "")
        elif tool_name == "navigate_to":
            parameters["url"] = tool_input.get("url", "")
        elif tool_name == "press_key":
            parameters["key"] = tool_input.get("key", "")
        elif tool_name == "extract_data":
            parameters["output_name"] = tool_input.get("output_name", "")
            parameters["value_pattern"] = ".*"  # to be refined
        elif tool_name == "wait_for_page":
            parameters["timeout_seconds"] = tool_input.get("timeout_seconds", 5)

        # Determine risk level
        risk_level = RiskLevel.SAFE
        if action_type in (ActionType.TYPE, ActionType.SELECT, ActionType.CLEAR):
            risk_level = RiskLevel.CAUTIOUS
        if action_type == ActionType.CLICK and any(
            kw in description.lower()
            for kw in ("submit", "confirm", "transfer", "delete", "approve")
        ):
            risk_level = RiskLevel.RISKY

        # Build checkpoint for navigation-changing actions
        checkpoint = None
        if action.page_url_after and action.page_url_before and action.page_url_after != action.page_url_before:
            checkpoint = Checkpoint(
                description=f"Page navigated to {action.page_url_after}",
                type=CheckpointType.URL_MATCH,
                value=self._url_to_pattern(action.page_url_after),
            )

        return Step(
            id=step_id,
            description=description,
            action=action_type,
            target=target,
            parameters=parameters,
            checkpoint=checkpoint,
            risk_level=risk_level,
            timeout_ms=max(int(action.duration_ms * 3), 5_000),  # 3x observed duration, min 5s
        )

    def _build_element_target(self, tool_input: dict[str, Any]) -> ElementTarget:
        """Build a multi-strategy element target from the agent's tool input."""
        role = tool_input.get("role", "")
        name = tool_input.get("name", "")
        description = tool_input.get("description", f"{role} '{name}'")

        strategies: list[LocatorStrategy] = []

        # Primary: accessibility role + name
        if role and name:
            strategies.append(LocatorStrategy(
                type=LocatorType.ACCESSIBILITY,
                value=f"{role}[name={name!r}]",
                attributes={"role": role, "name": name},
                confidence=0.95,
            ))

        # Secondary: text content match
        if name:
            strategies.append(LocatorStrategy(
                type=LocatorType.TEXT_CONTENT,
                value=name,
                confidence=0.80,
            ))

        # Tertiary: label proximity (for inputs)
        if role in ("textbox", "combobox", "searchbox") and name:
            strategies.append(LocatorStrategy(
                type=LocatorType.LABEL_PROXIMITY,
                value=name,
                confidence=0.70,
            ))

        return ElementTarget(
            description=description,
            strategies=strategies,
        )

    def _extract_parameters(self, trace: DiscoveryTrace) -> list[ParameterSpec]:
        """Identify input parameters from the trace.

        Looks for type_text actions where is_parameter was set by the agent,
        and infers parameters from values that look like they came from the goal.
        """
        params: list[ParameterSpec] = []
        seen_names: set[str] = set()

        for action in trace.actions:
            if action.tool_name == "type_text" and action.tool_input.get("is_parameter"):
                param_name = action.tool_input.get("parameter_name", "param")
                if param_name not in seen_names:
                    seen_names.add(param_name)
                    params.append(ParameterSpec(
                        name=param_name,
                        type="string",
                        description=action.tool_input.get("description", ""),
                        required=True,
                        sensitive=False,
                    ))

        return params

    def _extract_outputs(self, trace: DiscoveryTrace, steps: list[Step]) -> list[OutputSpec]:
        """Identify output fields from extract_data actions."""
        outputs: list[OutputSpec] = []
        seen_names: set[str] = set()

        for action in trace.actions:
            if action.tool_name == "extract_data":
                output_name = action.tool_input.get("output_name", "")
                if output_name and output_name not in seen_names:
                    seen_names.add(output_name)
                    # Find the corresponding step
                    step_id = "unknown"
                    for step in steps:
                        if step.action == ActionType.EXTRACT and step.parameters.get("output_name") == output_name:
                            step_id = step.id
                            break

                    outputs.append(OutputSpec(
                        name=output_name,
                        type="string",
                        description=action.tool_input.get("description", ""),
                        extraction_step_id=step_id,
                        sensitive=action.tool_input.get("sensitive", False),
                    ))

        return outputs

    def _build_success_condition(self, trace: DiscoveryTrace, last_action: AgentAction | None) -> Checkpoint:
        """Build the final success condition."""
        if last_action and last_action.page_title_after:
            return Checkpoint(
                description="Verify the goal page is reached",
                type=CheckpointType.PAGE_TITLE,
                value=last_action.page_title_after,
            )

        # Fallback: check outputs exist
        output_names = list(trace.outputs.keys())
        if output_names:
            return Checkpoint(
                description=f"Verify extraction of: {', '.join(output_names)}",
                type=CheckpointType.TEXT_PRESENT,
                value=str(list(trace.outputs.values())[0]) if trace.outputs else "",
            )

        return Checkpoint(
            description="Goal completed (generic)",
            type=CheckpointType.TEXT_PRESENT,
            value="",
        )

    def _build_error_handlers(self, trace: DiscoveryTrace) -> list[ErrorHandler]:
        """Build error handlers for common runtime conditions."""
        handlers: list[ErrorHandler] = [
            ErrorHandler(
                name="member_not_found",
                description="Member search returns no results",
                detection=Checkpoint(
                    description="No results message is visible",
                    type=CheckpointType.TEXT_PRESENT,
                    value="No members found",
                ),
                action=ErrorPolicy.REPORT_OUTCOME,
                message_template="Member not found for the given search criteria",
            ),
            ErrorHandler(
                name="session_expired",
                description="Session has timed out, redirect to login",
                detection=Checkpoint(
                    description="Login page is displayed",
                    type=CheckpointType.TEXT_PRESENT,
                    value="Session expired",
                ),
                action=ErrorPolicy.ESCALATE,
                message_template="Session expired during execution",
            ),
            ErrorHandler(
                name="permission_denied",
                description="Access denied to the requested resource",
                detection=Checkpoint(
                    description="Permission denied error is shown",
                    type=CheckpointType.TEXT_PRESENT,
                    value="Access Denied",
                ),
                action=ErrorPolicy.REPORT_OUTCOME,
                message_template="Permission denied: {error_detail}",
            ),
            ErrorHandler(
                name="validation_error",
                description="Form validation error",
                detection=Checkpoint(
                    description="Validation error message visible",
                    type=CheckpointType.TEXT_PRESENT,
                    value="required",
                ),
                action=ErrorPolicy.FAIL,
                message_template="Validation error: {error_detail}",
            ),
        ]
        return handlers

    def _goal_to_name(self, goal: str) -> str:
        """Convert a natural language goal to a machine-friendly name."""
        # Extract key verbs and nouns
        name = goal.lower()
        name = re.sub(r"[^a-z0-9\s]", "", name)
        words = name.split()
        # Take first 5 meaningful words
        stop_words = {"a", "an", "the", "and", "or", "for", "to", "in", "of", "their", "this", "that"}
        meaningful = [w for w in words if w not in stop_words][:5]
        return "_".join(meaningful) if meaningful else "unnamed_capability"

    def _url_to_pattern(self, url: str) -> str:
        """Convert a concrete URL to a pattern for checkpoint matching."""
        # Replace specific IDs with wildcards
        pattern = re.sub(r"/M\d{4}", "/M*", url)
        pattern = re.sub(r"/\d+", "/*", pattern)
        return pattern
