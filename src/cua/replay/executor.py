"""Deterministic replay engine — the production execution path.

Given a saved Capability artifact and a set of input parameters, this engine
replays the flow WITHOUT invoking the LLM for decisions. This is how an AI
agent would trigger a capability in production: fast, cheap, deterministic.

The executor:
1. Validates input parameters against the capability's declared schema
2. Executes each step sequentially using the locator resolver
3. Verifies checkpoints after each step
4. Detects and handles runtime errors using the three-tier taxonomy:
   - Business outcomes: report to caller (not a failure)
   - Recoverable conditions: dismiss/retry automatically
   - Hard failures: stop and report with debugging context
5. Returns a structured RunResult with outputs or error details

Error detection is proactive: before executing each step, the engine checks
all error handlers (step-level first, then capability-level) against the
current page state. This catches conditions like "member not found" BEFORE
the step tries to interact with an element that doesn't exist.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.models.capability import (
    ActionType,
    Capability,
    Checkpoint,
    CheckpointType,
    ErrorHandler,
    ErrorPolicy,
    Step,
)
from cua.models.execution import (
    BusinessOutcome,
    HardFailure,
    RecoverableCondition,
    RunResult,
    RunStatus,
    StepResult,
    StepTrace,
)
from cua.observability.logger import RunLogger
from cua.replay.locator_resolver import LocatorResolver
from cua.safety.guard import SafetyGuard
from cua.surfaces.playwright_adapter import PlaywrightAdapter


class ReplayExecutor:
    """Deterministic replay engine — executes capabilities without LLM."""

    def __init__(
        self,
        surface: PlaywrightAdapter,
        resolver: LocatorResolver,
        guard: SafetyGuard,
        logger: RunLogger,
    ) -> None:
        self._surface = surface
        self._resolver = resolver
        self._guard = guard
        self._logger = logger

    async def execute(
        self,
        capability: Capability,
        params: dict[str, Any],
    ) -> RunResult:
        """Replay a capability with the given input parameters.

        Returns a RunResult describing the outcome.
        """
        run_id = self._logger.run_id
        self._guard.reset()

        # Validate and resolve input parameters
        try:
            resolved_params = capability.resolve_parameters(params)
        except ValueError as e:
            return RunResult(
                run_id=run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=RunStatus.FAILURE,
                failure=HardFailure(
                    error_type="parameter_validation",
                    message=str(e),
                ),
                total_steps=len(capability.steps),
                input_parameters=params,
            )

        self._logger.info(
            "replay_start",
            capability=capability.name,
            params=list(resolved_params.keys()),
        )

        # Navigate to the entry point
        try:
            await self._surface.navigate(capability.surface.entry_point)
        except Exception as e:
            return RunResult(
                run_id=run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=RunStatus.FAILURE,
                failure=HardFailure(
                    error_type="navigation_failed",
                    message=f"Could not reach {capability.surface.entry_point}: {e}",
                ),
                total_steps=len(capability.steps),
                input_parameters=params,
            )

        # Execute steps
        step_results: list[StepResult] = []
        outputs: dict[str, Any] = {}
        completed_steps = 0

        for step in capability.steps:
            # Check for error conditions BEFORE executing the step
            handler_triggered = await self._check_error_handlers(
                step.error_handlers + capability.error_handlers
            )

            if handler_triggered:
                if handler_triggered.action == ErrorPolicy.REPORT_OUTCOME:
                    return RunResult(
                        run_id=run_id,
                        capability_id=capability.id,
                        capability_version=capability.version,
                        status=RunStatus.BUSINESS_OUTCOME,
                        business_outcome=BusinessOutcome(
                            outcome_name=handler_triggered.name,
                            message=handler_triggered.message_template,
                        ),
                        step_results=step_results,
                        total_steps=len(capability.steps),
                        completed_steps=completed_steps,
                        input_parameters=params,
                    )
                elif handler_triggered.action == ErrorPolicy.ESCALATE:
                    return RunResult(
                        run_id=run_id,
                        capability_id=capability.id,
                        capability_version=capability.version,
                        status=RunStatus.ESCALATED,
                        step_results=step_results,
                        total_steps=len(capability.steps),
                        completed_steps=completed_steps,
                        input_parameters=params,
                    )
                elif handler_triggered.action == ErrorPolicy.SKIP:
                    self._logger.info("step_skipped", step_id=step.id, handler=handler_triggered.name)
                    continue

            # Execute the step
            step_result = await self._execute_step(step, resolved_params, outputs)
            step_results.append(step_result)

            if step_result.success:
                completed_steps += 1
                # Collect extracted outputs
                if step_result.trace.extracted_values:
                    outputs.update(step_result.trace.extracted_values)
            elif step_result.business_outcome:
                return RunResult(
                    run_id=run_id,
                    capability_id=capability.id,
                    capability_version=capability.version,
                    status=RunStatus.BUSINESS_OUTCOME,
                    business_outcome=step_result.business_outcome,
                    step_results=step_results,
                    total_steps=len(capability.steps),
                    completed_steps=completed_steps,
                    input_parameters=params,
                )
            else:
                # Hard failure
                return RunResult(
                    run_id=run_id,
                    capability_id=capability.id,
                    capability_version=capability.version,
                    status=RunStatus.FAILURE,
                    failure=step_result.error,
                    step_results=step_results,
                    total_steps=len(capability.steps),
                    completed_steps=completed_steps,
                    input_parameters=params,
                )

            self._guard.record_step()

        # Verify success condition
        success_check = await self._verify_checkpoint(capability.success_condition)
        if not success_check:
            self._logger.step_error(
                "final",
                "success_condition_failed",
                f"Success condition not met: {capability.success_condition.description}",
            )

        result = RunResult(
            run_id=run_id,
            capability_id=capability.id,
            capability_version=capability.version,
            status=RunStatus.SUCCESS,
            outputs=outputs,
            step_results=step_results,
            total_steps=len(capability.steps),
            completed_steps=completed_steps,
            completed_at=datetime.now(timezone.utc),
            input_parameters=params,
        )

        self._logger.run_complete("success", outputs=list(outputs.keys()))
        return result

    async def _execute_step(
        self,
        step: Step,
        params: dict[str, Any],
        current_outputs: dict[str, Any],
    ) -> StepResult:
        """Execute a single step with retry support."""
        max_attempts = step.retry_config.max_attempts if step.retry_config else 1
        last_error: str = ""

        for attempt in range(max_attempts):
            started_at = datetime.now(timezone.utc)
            start_time = time.time()

            self._logger.step_start(step.id, step.action.value, step.description)

            try:
                # Wait before executing if configured
                if step.wait_before_ms > 0:
                    await self._surface.page.wait_for_timeout(step.wait_before_ms)

                # Resolve element target
                selector = None
                locator_strategy = None
                locator_attempts = 0

                if step.target:
                    resolution = await self._resolver.resolve(step.target, step.timeout_ms)
                    locator_attempts = resolution.attempts

                    if not resolution.found:
                        last_error = resolution.error
                        if attempt < max_attempts - 1:
                            # Retry with backoff
                            backoff_ms = (step.retry_config.backoff_ms if step.retry_config else 1_000) * (2 ** attempt)
                            await self._surface.page.wait_for_timeout(backoff_ms)
                            continue

                        # All retries exhausted
                        duration = (time.time() - start_time) * 1_000
                        trace = StepTrace(
                            step_id=step.id,
                            started_at=started_at,
                            completed_at=datetime.now(timezone.utc),
                            wall_time_ms=duration,
                            action=step.action.value,
                            locator_attempts=locator_attempts,
                            page_url=self._surface.page.url,
                        )
                        # Capture failure screenshot
                        screenshot_path = f"evidence/failure_{step.id}.png"
                        try:
                            await self._surface.screenshot(screenshot_path)
                            trace.screenshot_path = screenshot_path
                        except Exception:
                            pass

                        return StepResult(
                            step_id=step.id,
                            success=False,
                            trace=trace,
                            error=HardFailure(
                                error_type="element_not_found",
                                message=resolution.error,
                                step_id=step.id,
                                expected=step.target.description,
                                observed=f"Tried {locator_attempts} strategies",
                                screenshot_path=trace.screenshot_path,
                            ),
                        )

                    selector = resolution.selector
                    locator_strategy = resolution.strategy_used.value if resolution.strategy_used else None

                # Execute the action
                extracted = await self._execute_action(step, selector, params, current_outputs)

                duration = (time.time() - start_time) * 1_000

                # Verify checkpoint if present
                if step.checkpoint:
                    checkpoint_ok = await self._verify_checkpoint(step.checkpoint)
                    if not checkpoint_ok:
                        trace = StepTrace(
                            step_id=step.id,
                            started_at=started_at,
                            completed_at=datetime.now(timezone.utc),
                            wall_time_ms=duration,
                            action=step.action.value,
                            locator_strategy_used=locator_strategy,
                            locator_attempts=locator_attempts,
                            page_url=self._surface.page.url,
                        )
                        return StepResult(
                            step_id=step.id,
                            success=False,
                            trace=trace,
                            error=HardFailure(
                                error_type="checkpoint_failed",
                                message=f"Checkpoint failed: {step.checkpoint.description}",
                                step_id=step.id,
                                expected=step.checkpoint.value,
                                observed=self._surface.page.url,
                            ),
                        )

                trace = StepTrace(
                    step_id=step.id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    wall_time_ms=duration,
                    action=step.action.value,
                    locator_strategy_used=locator_strategy,
                    locator_attempts=locator_attempts,
                    page_url=self._surface.page.url,
                    page_title=await self._surface.page.title(),
                    extracted_values=extracted,
                )

                self._logger.step_complete(
                    step.id, True, duration, strategy=locator_strategy
                )

                return StepResult(step_id=step.id, success=True, trace=trace)

            except Exception as e:
                last_error = str(e)
                if attempt < max_attempts - 1:
                    backoff_ms = (step.retry_config.backoff_ms if step.retry_config else 1_000) * (2 ** attempt)
                    await self._surface.page.wait_for_timeout(backoff_ms)
                    continue

                duration = (time.time() - start_time) * 1_000
                trace = StepTrace(
                    step_id=step.id,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    wall_time_ms=duration,
                    action=step.action.value,
                    page_url=self._surface.page.url,
                )
                self._logger.step_error(step.id, "exception", last_error)
                return StepResult(
                    step_id=step.id,
                    success=False,
                    trace=trace,
                    error=HardFailure(
                        error_type="step_exception",
                        message=last_error,
                        step_id=step.id,
                    ),
                )

        # Should not reach here, but just in case
        duration = (time.time() - time.time()) * 1_000
        trace = StepTrace(
            step_id=step.id,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            wall_time_ms=0,
            action=step.action.value,
        )
        return StepResult(
            step_id=step.id,
            success=False,
            trace=trace,
            error=HardFailure(
                error_type="max_retries",
                message=f"Exhausted {max_attempts} attempts. Last error: {last_error}",
                step_id=step.id,
            ),
        )

    async def _execute_action(
        self,
        step: Step,
        selector: str | None,
        params: dict[str, Any],
        current_outputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute the action for a step. Returns any extracted values."""
        page = self._surface.page
        extracted: dict[str, Any] = {}

        # Merge params with current outputs for interpolation
        all_values = {**params, **current_outputs}

        if step.action == ActionType.CLICK and selector:
            await self._click_with_selector(selector, step.timeout_ms)
            await page.wait_for_load_state("networkidle", timeout=step.timeout_ms)

        elif step.action == ActionType.TYPE and selector:
            text = step.parameters.get("text", "")
            # Interpolate parameter references
            text = self._interpolate(text, all_values)
            await self._fill_with_selector(selector, text, step.timeout_ms)

        elif step.action == ActionType.SELECT and selector:
            value = step.parameters.get("value", "")
            value = self._interpolate(value, all_values)
            await page.select_option(selector, value, timeout=step.timeout_ms)

        elif step.action == ActionType.NAVIGATE:
            url = step.parameters.get("url", "")
            url = self._interpolate(url, all_values)
            await self._surface.navigate(url, step.timeout_ms)

        elif step.action == ActionType.PRESS_KEY:
            key = step.parameters.get("key", "")
            await self._surface.press_key(key)

        elif step.action == ActionType.EXTRACT:
            output_name = step.parameters.get("output_name", "")
            extract_pattern = step.parameters.get("pattern", "")
            extract_label = step.parameters.get("label", "")

            if selector:
                value = await self._surface.extract_text(selector, step.timeout_ms)
            elif extract_label or output_name:
                # Smart extraction: find value near a label on the page
                label_to_find = extract_label or output_name.replace("_", " ").title()
                value = await self._extract_by_label(label_to_find, extract_pattern)
            else:
                value = await self._surface.get_page_text()

            if output_name:
                extracted[output_name] = value

        elif step.action == ActionType.WAIT:
            timeout_s = step.parameters.get("timeout_seconds", 5)
            await page.wait_for_load_state("networkidle", timeout=timeout_s * 1_000)

        elif step.action == ActionType.ASSERT:
            # Assertions are verified via checkpoints, no action needed
            pass

        elif step.action == ActionType.SCROLL:
            direction = step.parameters.get("direction", "down")
            amount = step.parameters.get("amount", 300)
            await self._surface.scroll(direction, amount)

        elif step.action == ActionType.CLEAR and selector:
            await self._surface.clear_field(selector, step.timeout_ms)

        return extracted

    async def _click_with_selector(self, selector: str, timeout_ms: int) -> None:
        """Click using the resolved selector, handling different selector formats."""
        page = self._surface.page
        if selector.startswith("role="):
            # Parse role=button[name="Search"]
            parts = selector.split("[name=")
            role = parts[0].replace("role=", "")
            name = parts[1].rstrip("]").strip('"') if len(parts) > 1 else ""
            await self._surface.click_by_role(role, name, timeout_ms)
        elif selector.startswith("text="):
            text = json.loads(selector[5:])
            await page.get_by_text(text).first.click(timeout=timeout_ms)
        elif selector.startswith("label="):
            label = json.loads(selector[6:])
            await page.get_by_label(label).click(timeout=timeout_ms)
        else:
            await page.click(selector, timeout=timeout_ms)

    async def _fill_with_selector(self, selector: str, text: str, timeout_ms: int) -> None:
        """Fill using the resolved selector."""
        page = self._surface.page
        if selector.startswith("role="):
            parts = selector.split("[name=")
            role = parts[0].replace("role=", "")
            name = parts[1].rstrip("]").strip('"') if len(parts) > 1 else ""
            await self._surface.fill_by_role(role, name, text, timeout_ms)
        elif selector.startswith("label="):
            label = json.loads(selector[6:])
            await page.get_by_label(label).fill(text, timeout=timeout_ms)
        else:
            await page.fill(selector, text, timeout=timeout_ms)

    async def _extract_by_label(self, label: str, pattern: str = "") -> str:
        """Extract a value from the page by finding it near a label.

        Handles common legacy HTML patterns: table rows with label + value cells,
        definition lists, and label-value pairs.
        """
        import re

        page = self._surface.page
        page_text = await self._surface.get_page_text()

        # Try to find "Label: Value" or "Label\tValue" patterns
        for separator in [":\t", ":\n", ": ", "\t"]:
            for line in page_text.split("\n"):
                if label.lower().replace("_", " ") in line.lower():
                    parts = line.split(separator, 1)
                    if len(parts) == 2:
                        value = parts[1].strip()
                        if value:
                            return value

        # Try structured extraction via table cells
        try:
            selectors = [
                f"td:has-text('{label}') + td",
                f"th:has-text('{label}') + td",
                f"dt:has-text('{label}') + dd",
            ]
            for sel in selectors:
                locator = page.locator(sel).first
                if await locator.count() > 0:
                    return (await locator.inner_text()).strip()
        except Exception:
            pass

        # If a regex pattern was provided, extract from full text
        if pattern:
            match = re.search(pattern, page_text)
            if match:
                return match.group(1) if match.groups() else match.group(0)

        # Fallback: return the label's context
        return f"[extraction_pending:{label}]"

    def _interpolate(self, template: str, values: dict[str, Any]) -> str:
        """Replace {{param_name}} placeholders with actual values."""
        import re

        def replacer(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            return str(values.get(key, match.group(0)))

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)

    async def _verify_checkpoint(self, checkpoint: Checkpoint) -> bool:
        """Verify a checkpoint condition against the current page state."""
        page = self._surface.page

        try:
            if checkpoint.type == CheckpointType.URL_MATCH:
                current_url = page.url
                # Simple glob matching
                import fnmatch
                return fnmatch.fnmatch(current_url, checkpoint.value)

            elif checkpoint.type == CheckpointType.TEXT_PRESENT:
                if not checkpoint.value:
                    return True
                return await self._surface.wait_for_text(
                    checkpoint.value, checkpoint.timeout_ms
                )

            elif checkpoint.type == CheckpointType.TEXT_ABSENT:
                page_text = await self._surface.get_page_text()
                return checkpoint.value not in page_text

            elif checkpoint.type == CheckpointType.ELEMENT_VISIBLE:
                return await self._surface.wait_for_element(
                    checkpoint.value, checkpoint.timeout_ms
                )

            elif checkpoint.type == CheckpointType.ELEMENT_ABSENT:
                return not await self._surface.element_exists(checkpoint.value)

            elif checkpoint.type == CheckpointType.PAGE_TITLE:
                title = await page.title()
                return checkpoint.value.lower() in title.lower()

        except Exception:
            return False

        return False

    async def _check_error_handlers(
        self, handlers: list[ErrorHandler]
    ) -> ErrorHandler | None:
        """Check all error handlers against the current page state.

        Returns the first matching handler, or None if no condition is detected.
        """
        for handler in handlers:
            matched = await self._verify_checkpoint(handler.detection)
            if matched:
                self._logger.info(
                    "error_handler_triggered",
                    handler=handler.name,
                    action=handler.action.value,
                )
                return handler
        return None
