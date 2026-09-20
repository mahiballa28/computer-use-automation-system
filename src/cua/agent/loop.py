"""Goal-driven agent loop — the discovery engine.

This is the core of Phase 1: given a natural language goal and a target surface,
run an LLM-driven observe → decide → act loop until the goal is met or a
stopping condition is hit.

The loop:
1. Observe: capture the current page state (accessibility tree + URL + errors)
2. Decide: send the state to Claude, which returns a tool call (the next action)
3. Act: execute the tool call via the surface adapter
4. Record: log the action and its result for later artifact generation
5. Repeat until: goal_complete, stuck, max_steps, or timeout

The trace produced by this loop is consumed by the Recorder to generate a
structured Capability artifact.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic

from cua.agent.prompts import AGENT_TOOLS, SYSTEM_PROMPT
from cua.config import Settings
from cua.observability.logger import RunLogger
from cua.safety.guard import SafetyGuard
from cua.surfaces.playwright_adapter import PlaywrightAdapter


@dataclass
class AgentAction:
    """A single action taken by the agent during discovery."""

    step_index: int
    tool_name: str
    tool_input: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Filled in after execution
    success: bool = True
    error: str | None = None
    page_url_before: str | None = None
    page_url_after: str | None = None
    page_title_after: str | None = None
    accessibility_tree_before: str | None = None
    screenshot_path: str | None = None
    duration_ms: float = 0.0


@dataclass
class DiscoveryTrace:
    """Complete trace of a discovery run — consumed by the Recorder."""

    run_id: str
    goal: str
    target_url: str
    actions: list[AgentAction] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    stuck_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    total_llm_calls: int = 0
    total_tokens: int = 0


class AgentLoop:
    """LLM-driven discovery agent.

    Runs an observe → decide → act loop against a live surface until the
    goal is achieved or a stopping condition is reached.
    """

    def __init__(
        self,
        settings: Settings,
        surface: PlaywrightAdapter,
        guard: SafetyGuard,
        logger: RunLogger,
    ) -> None:
        self._settings = settings
        self._surface = surface
        self._guard = guard
        self._logger = logger
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
        self._messages: list[dict[str, Any]] = []
        self._trace: DiscoveryTrace | None = None

    async def run(self, goal: str, target_url: str) -> DiscoveryTrace:
        """Execute the discovery loop.

        Returns a DiscoveryTrace that the Recorder can convert to a Capability.
        """
        run_id = str(uuid.uuid4())
        self._trace = DiscoveryTrace(
            run_id=run_id,
            goal=goal,
            target_url=target_url,
        )
        self._messages = []
        self._guard.reset()

        self._logger.info("discovery_start", goal=goal, target_url=target_url)

        try:
            # Navigate to the target
            await self._surface.navigate(target_url)
            await self._run_loop(goal)
        except Exception as e:
            self._logger.step_error("agent", "exception", str(e))
            self._trace.stuck_reason = f"Unhandled exception: {e}"
        finally:
            self._trace.completed_at = datetime.now(timezone.utc)

        self._logger.run_complete(
            "success" if self._trace.success else "failed",
            steps=len(self._trace.actions),
            outputs=list(self._trace.outputs.keys()),
        )

        return self._trace

    async def _run_loop(self, goal: str) -> None:
        """The core observe → decide → act loop."""
        assert self._trace is not None

        max_steps = self._settings.max_agent_steps
        start_time = time.time()

        for step_index in range(max_steps):
            # Timeout check
            elapsed = time.time() - start_time
            if elapsed > self._settings.agent_timeout_seconds:
                self._trace.stuck_reason = f"Timeout after {elapsed:.0f}s"
                return

            # OBSERVE: get current page state
            state = await self._surface.get_state(capture_screenshot=True)

            # Build the prompt with current state
            error_context = ""
            if state.error_text:
                error_context = f"⚠️ Error visible on page: {state.error_text}"

            user_content = SYSTEM_PROMPT.format(
                goal=goal,
                url=state.url or "unknown",
                title=state.title,
                accessibility_tree=state.accessibility_tree,
                error_context=error_context,
            )

            # For subsequent turns, include the result of the last action
            if step_index == 0:
                self._messages = [{"role": "user", "content": user_content}]
            else:
                self._messages.append({"role": "user", "content": user_content})

            # DECIDE: ask Claude what to do next
            response = self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=1_024,
                system="You are a precise UI automation agent. Use tools to interact with the application. One action per turn.",
                messages=self._messages,
                tools=AGENT_TOOLS,  # type: ignore[arg-type]
            )

            self._trace.total_llm_calls += 1
            self._trace.total_tokens += response.usage.input_tokens + response.usage.output_tokens

            # Extract the tool call from the response
            tool_use = None
            assistant_text = ""
            for block in response.content:
                if block.type == "tool_use":
                    tool_use = block
                elif block.type == "text":
                    assistant_text = block.text

            if not tool_use:
                # No tool call — the model is either confused or done
                self._logger.info("no_tool_call", text=assistant_text[:200])
                if "complete" in assistant_text.lower() or "goal" in assistant_text.lower():
                    self._trace.success = True
                    return
                continue

            # Record the assistant's response for conversation continuity
            self._messages.append({"role": "assistant", "content": response.content})

            # ACT: execute the tool call
            action = AgentAction(
                step_index=step_index,
                tool_name=tool_use.name,
                tool_input=dict(tool_use.input),  # type: ignore[arg-type]
                page_url_before=state.url,
                accessibility_tree_before=state.accessibility_tree,
                screenshot_path=state.screenshot_path,
            )

            action_start = time.time()
            tool_result = await self._execute_tool(tool_use.name, tool_use.input)  # type: ignore[arg-type]
            action.duration_ms = (time.time() - action_start) * 1_000

            # Get post-action state
            post_state = await self._surface.get_state()
            action.page_url_after = post_state.url
            action.page_title_after = post_state.title

            if "error" in tool_result.lower():
                action.success = False
                action.error = tool_result

            self._trace.actions.append(action)
            self._guard.record_step()

            self._logger.step_complete(
                step_id=f"step_{step_index}",
                success=action.success,
                duration_ms=action.duration_ms,
                tool=tool_use.name,
            )

            # Send tool result back to Claude
            self._messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": tool_result,
                    }
                ],
            })

            # Check for terminal actions
            if tool_use.name == "mark_goal_complete":
                self._trace.success = True
                outputs = tool_use.input.get("outputs", {})  # type: ignore[union-attr]
                if isinstance(outputs, dict):
                    self._trace.outputs = outputs
                return

            if tool_use.name == "mark_stuck":
                self._trace.stuck_reason = tool_use.input.get("reason", "Unknown")  # type: ignore[union-attr]
                return

        # If we get here, we hit max steps
        self._trace.stuck_reason = f"Max steps reached ({max_steps})"

    async def _execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool call on the surface. Returns a text result for the LLM."""
        try:
            if tool_name == "click_element":
                return await self._do_click(tool_input)
            elif tool_name == "type_text":
                return await self._do_type(tool_input)
            elif tool_name == "select_option":
                return await self._do_select(tool_input)
            elif tool_name == "navigate_to":
                return await self._do_navigate(tool_input)
            elif tool_name == "extract_data":
                return await self._do_extract(tool_input)
            elif tool_name == "assert_page_state":
                return await self._do_assert(tool_input)
            elif tool_name == "press_key":
                return await self._do_press_key(tool_input)
            elif tool_name == "wait_for_page":
                return await self._do_wait(tool_input)
            elif tool_name == "mark_goal_complete":
                return f"Goal marked complete. Summary: {tool_input.get('summary', '')}"
            elif tool_name == "mark_stuck":
                return f"Marked as stuck. Reason: {tool_input.get('reason', '')}"
            else:
                return f"Error: Unknown tool '{tool_name}'"
        except Exception as e:
            return f"Error executing {tool_name}: {e}"

    async def _do_click(self, input: dict[str, Any]) -> str:
        role = input.get("role", "")
        name = input.get("name", "")

        try:
            await self._surface.click_by_role(role, name)
            # Wait briefly for page to react
            await self._surface.page.wait_for_load_state("networkidle", timeout=5_000)
            return f"Clicked {role} '{name}' successfully."
        except Exception as e:
            # Try alternative: find by text
            try:
                locator = self._surface.page.get_by_text(name, exact=False)
                await locator.first.click(timeout=5_000)
                return f"Clicked element with text '{name}' (fallback)."
            except Exception:
                return f"Error: Could not find or click {role} '{name}'. Error: {e}"

    async def _do_type(self, input: dict[str, Any]) -> str:
        role = input.get("role", "")
        name = input.get("name", "")
        text = input.get("text", "")

        # Strategy 1: fill by role + accessible name
        try:
            await self._surface.fill_by_role(role, name, text)
            return f"Typed '{text}' into {role} '{name}'."
        except Exception:
            pass

        # Strategy 2: find by label text proximity
        try:
            locator = self._surface.page.get_by_label(name, exact=False)
            await locator.fill(text, timeout=5_000)
            return f"Typed '{text}' into field labeled '{name}' (label fallback)."
        except Exception:
            pass

        # Strategy 3: find input near text content
        try:
            clean_name = name.rstrip(":").strip()
            # Find the text node, then the nearest input
            locator = self._surface.page.locator(
                f"td:has(font:text-is('{name}')) + td input, "
                f"td:has-text('{clean_name}') + td input, "
                f"input[name*='{clean_name.lower()}']"
            )
            await locator.first.fill(text, timeout=5_000)
            return f"Typed '{text}' into input near '{name}' (proximity fallback)."
        except Exception:
            pass

        # Strategy 4: try all visible text inputs in order
        try:
            inputs = self._surface.page.locator("input[type='text'], input:not([type])")
            count = await inputs.count()
            for i in range(count):
                el = inputs.nth(i)
                if await el.is_visible():
                    await el.fill(text, timeout=3_000)
                    return f"Typed '{text}' into visible text input #{i} (scan fallback)."
        except Exception as e:
            return f"Error: Could not type into {role} '{name}'. All strategies failed. Last error: {e}"

        return f"Error: Could not find any suitable input for '{name}'."

    async def _do_select(self, input: dict[str, Any]) -> str:
        role = input.get("role", "")
        name = input.get("name", "")
        value = input.get("value", "")

        try:
            locator = self._surface.page.get_by_role(role, name=name)  # type: ignore[arg-type]
            await locator.select_option(value, timeout=5_000)
            return f"Selected '{value}' in {role} '{name}'."
        except Exception as e:
            return f"Error: Could not select '{value}' in {role} '{name}'. Error: {e}"

    async def _do_navigate(self, input: dict[str, Any]) -> str:
        url = input.get("url", "")

        # Safety check
        decision = self._guard.check_action(url, "navigate", "safe")  # type: ignore[arg-type]
        if not decision.allowed:
            return f"Blocked: {decision.reason}"

        try:
            await self._surface.navigate(url)
            return f"Navigated to {url}."
        except Exception as e:
            return f"Error: Could not navigate to {url}. Error: {e}"

    async def _do_extract(self, input: dict[str, Any]) -> str:
        output_name = input.get("output_name", "")
        value = input.get("value", "")

        # Store in trace outputs
        if self._trace:
            self._trace.outputs[output_name] = value

        return f"Extracted {output_name}='{value}'."

    async def _do_assert(self, input: dict[str, Any]) -> str:
        condition = input.get("condition", "")
        evidence = input.get("evidence", "")
        return f"Assertion confirmed: {condition}. Evidence: {evidence}"

    async def _do_press_key(self, input: dict[str, Any]) -> str:
        key = input.get("key", "")
        try:
            await self._surface.press_key(key)
            return f"Pressed {key}."
        except Exception as e:
            return f"Error pressing {key}: {e}"

    async def _do_wait(self, input: dict[str, Any]) -> str:
        wait_for = input.get("wait_for", "")
        timeout = input.get("timeout_seconds", 5)
        try:
            await self._surface.page.wait_for_load_state("networkidle", timeout=timeout * 1_000)
            return f"Page loaded. Waited for: {wait_for}"
        except Exception:
            return f"Timeout waiting for: {wait_for}"
