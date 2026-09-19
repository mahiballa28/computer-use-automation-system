"""Session handoff — pause automation, hand the live session to a human, resume.

The handoff mechanism uses Chrome DevTools Protocol (CDP):
1. Automation pauses and stops sending commands to the browser
2. The CDP WebSocket endpoint is exposed to the human operator
3. The human connects to the SAME browser session (not a new one)
4. The human performs manual actions, which are observed
5. The human signals "done" and automation resumes from current state

This preserves:
- Session state (cookies, auth tokens, form state)
- The browser instance (same tabs, same DOM state)
- Evidence continuity (what the human did is recorded)

Production design note:
In a real deployment, the CDP endpoint would be exposed through a secure
WebSocket proxy (not directly), with authentication and RBAC. The operator
UI would be a lightweight web-based VNC/noVNC viewer connected to the CDP
session. For this implementation, we expose the raw CDP endpoint and provide
a CLI-based handoff flow.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog

from cua.models.escalation import (
    EscalationReason,
    HumanAction,
    InterventionRequest,
    SessionController,
    SessionState,
)
from cua.surfaces.playwright_adapter import PlaywrightAdapter

logger = structlog.get_logger(component="escalation")


class SessionHandoff:
    """Manages the handoff of browser session control between automation and humans."""

    def __init__(self, surface: PlaywrightAdapter) -> None:
        self._surface = surface
        self._state = SessionState()
        self._resume_event: asyncio.Event = asyncio.Event()

    @property
    def session_state(self) -> SessionState:
        return self._state

    async def request_intervention(
        self,
        run_id: str,
        capability_id: str,
        capability_name: str,
        step_id: str,
        step_description: str,
        completed_steps: int,
        total_steps: int,
        reason: EscalationReason,
        reason_detail: str,
    ) -> InterventionRequest:
        """Create an intervention request and pause the automation.

        This is the entry point for escalation. The automation calls this
        when it detects a condition it can't handle.
        """
        # Capture current state for the operator
        current_url = self._surface.page.url
        page_title = await self._surface.page.title()

        # Take a screenshot for context
        screenshot_path = f"evidence/escalation_{step_id}.png"
        try:
            await self._surface.screenshot(screenshot_path)
        except Exception:
            screenshot_path = None

        # Get CDP endpoint
        cdp_endpoint = await self._surface.get_cdp_endpoint()

        intervention = InterventionRequest(
            id=str(uuid.uuid4()),
            run_id=run_id,
            capability_id=capability_id,
            capability_name=capability_name,
            current_step_id=step_id,
            current_step_description=step_description,
            completed_steps=completed_steps,
            total_steps=total_steps,
            reason=reason,
            reason_detail=reason_detail,
            current_url=current_url,
            screenshot_path=screenshot_path,
            page_title=page_title,
            cdp_endpoint=cdp_endpoint,
        )

        # Transition to PAUSED
        self._state.request_handoff(intervention)
        self._resume_event.clear()

        logger.warning(
            "intervention_requested",
            intervention_id=intervention.id,
            reason=reason.value,
            step_id=step_id,
            url=current_url,
        )

        return intervention

    async def wait_for_human(self, timeout_seconds: int = 300) -> bool:
        """Wait for the human to take control and then signal completion.

        Returns True if the human completed their actions, False on timeout.
        """
        logger.info("waiting_for_human", timeout=timeout_seconds)

        # In a production system, this would listen for a WebSocket message
        # from the operator UI. For this implementation, we wait for the
        # resume event to be set (by signal_human_complete).
        try:
            await asyncio.wait_for(self._resume_event.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            logger.warning("human_handoff_timeout", timeout=timeout_seconds)
            return False

    def accept_handoff(self) -> None:
        """Human operator takes control of the session."""
        self._state.accept_handoff()
        logger.info("human_accepted_handoff")

    def record_human_action(self, action_type: str, description: str) -> None:
        """Record an action taken by the human operator."""
        action = HumanAction(
            action_type=action_type,
            description=description,
        )
        self._state.record_human_action(action)
        logger.info("human_action", action_type=action_type, description=description)

    def signal_human_complete(self, operator_id: str | None = None) -> None:
        """Human signals they are done — automation can resume."""
        self._state.return_control(operator_id)
        self._resume_event.set()
        logger.info("human_handoff_complete", operator=operator_id)

    async def resume_automation(self) -> None:
        """Resume automated execution after human handoff."""
        self._state.resume_automation()

        # Re-observe the page state after human's changes
        state = await self._surface.get_state(capture_screenshot=True)
        logger.info(
            "automation_resumed",
            url=state.url,
            title=state.title,
            human_actions=len(self._state.human_actions),
        )


class StuckDetector:
    """Detects when the automation is stuck and should escalate.

    Heuristics:
    - Same page URL for N consecutive steps
    - Repeated failures on the same step
    - No progress toward checkpoint for N steps
    - Timeout approaching
    """

    def __init__(self, max_same_url_steps: int = 5, max_step_failures: int = 3) -> None:
        self._max_same_url_steps = max_same_url_steps
        self._max_step_failures = max_step_failures
        self._url_history: list[str] = []
        self._step_failure_counts: dict[str, int] = {}

    def record_step(self, url: str, step_id: str, success: bool) -> None:
        """Record a step execution for stuck detection."""
        self._url_history.append(url)
        if not success:
            self._step_failure_counts[step_id] = self._step_failure_counts.get(step_id, 0) + 1

    def is_stuck(self) -> tuple[bool, str]:
        """Check if the automation appears to be stuck. Returns (is_stuck, reason)."""
        # Same URL for too many steps
        if len(self._url_history) >= self._max_same_url_steps:
            recent = self._url_history[-self._max_same_url_steps:]
            if len(set(recent)) == 1:
                return True, f"No page change in {self._max_same_url_steps} consecutive steps"

        # Repeated failures on the same step
        for step_id, count in self._step_failure_counts.items():
            if count >= self._max_step_failures:
                return True, f"Step '{step_id}' has failed {count} times"

        return False, ""

    def reset(self) -> None:
        self._url_history.clear()
        self._step_failure_counts.clear()
