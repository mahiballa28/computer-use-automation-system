"""Surface adapter protocol — the abstraction boundary between the automation
engine and the platform-specific UI interaction layer.

This protocol defines how the system perceives and acts on a surface. A "surface"
is any interactive application: a web app in a browser, a desktop application,
a legacy terminal emulator, etc.

The key design decision is that capabilities (artifacts) are surface-agnostic:
they describe WHAT to do (click an element described as "Member ID field"),
not HOW to do it on a specific platform. The surface adapter translates between
the abstract operations and the platform-specific API.

This means:
- The same capability artifact can be replayed by different adapters
- Adding desktop support means writing a new adapter, not changing the schema
- The agent loop and replay engine are decoupled from Playwright/Selenium/etc.

Implemented adapters:
- PlaywrightAdapter: web applications via Playwright (primary, fully implemented)

Designed but not implemented (documented in REPORT.md):
- DesktopAdapter: native desktop apps via OS accessibility APIs (AT-SPI / UIAutomation)
- LegacyWebAdapter: hostile web surfaces via screenshot + coordinate targeting
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ElementInfo:
    """Information about a UI element, returned by the surface adapter.

    Platform-agnostic representation of an interactive element.
    """

    role: str  # accessibility role (e.g. "textbox", "button", "link")
    name: str  # accessible name (e.g. "Member ID", "Search", "Sign In")
    value: str = ""  # current value (for inputs)
    text: str = ""  # visible text content
    tag: str = ""  # HTML tag or native control type
    bounds: tuple[float, float, float, float] | None = None  # (x, y, width, height) in viewport coords
    attributes: dict[str, str] | None = None  # platform-specific attributes
    children_count: int = 0
    is_visible: bool = True
    is_enabled: bool = True
    is_focused: bool = False

    @property
    def description(self) -> str:
        """Human-readable one-line description."""
        parts = [self.role]
        if self.name:
            parts.append(f'"{self.name}"')
        if self.value:
            parts.append(f"value={self.value!r}")
        return " ".join(parts)


@dataclass
class PageState:
    """Current state of the surface, as perceived by the automation engine."""

    url: str | None = None  # current URL (web) or window title (desktop)
    title: str = ""
    accessibility_tree: str = ""  # text representation of the a11y tree
    elements: list[ElementInfo] | None = None  # all interactive elements
    screenshot_path: str | None = None  # path to screenshot file, if captured
    viewport_size: tuple[int, int] = (1_280, 720)
    error_text: str | None = None  # any visible error message on the page


@runtime_checkable
class SurfaceAdapter(Protocol):
    """Protocol for surface adapters — the boundary between platform and logic.

    Every method that interacts with the surface must handle its own timeouts
    and raise appropriate exceptions on failure. The adapter should not retry
    on its own — that's the replay engine's responsibility.
    """

    async def launch(self, entry_point: str, headless: bool = True) -> None:
        """Open the surface at the given entry point (URL, path, etc.)."""
        ...

    async def close(self) -> None:
        """Clean up and close the surface."""
        ...

    async def get_state(self, capture_screenshot: bool = False) -> PageState:
        """Observe the current state of the surface."""
        ...

    async def click(self, selector: str, timeout_ms: int = 5_000) -> None:
        """Click an element identified by a platform-specific selector."""
        ...

    async def type_text(self, selector: str, text: str, timeout_ms: int = 5_000) -> None:
        """Type text into an element."""
        ...

    async def clear_field(self, selector: str, timeout_ms: int = 5_000) -> None:
        """Clear the contents of an input field."""
        ...

    async def select_option(self, selector: str, value: str, timeout_ms: int = 5_000) -> None:
        """Select an option from a dropdown/select element."""
        ...

    async def navigate(self, url: str, timeout_ms: int = 10_000) -> None:
        """Navigate to a URL or activate a window."""
        ...

    async def press_key(self, key: str) -> None:
        """Press a keyboard key (Enter, Tab, Escape, etc.)."""
        ...

    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        """Scroll the surface."""
        ...

    async def extract_text(self, selector: str, timeout_ms: int = 5_000) -> str:
        """Extract visible text from an element."""
        ...

    async def wait_for_element(self, selector: str, timeout_ms: int = 10_000) -> bool:
        """Wait for an element to appear. Returns True if found, False on timeout."""
        ...

    async def wait_for_text(self, text: str, timeout_ms: int = 10_000) -> bool:
        """Wait for specific text to appear on the page."""
        ...

    async def find_elements(self, role: str | None = None) -> list[ElementInfo]:
        """Find all interactive elements, optionally filtered by role."""
        ...

    async def screenshot(self, path: str) -> str:
        """Capture a screenshot and save to path. Returns the path."""
        ...

    async def get_cdp_endpoint(self) -> str | None:
        """Get the CDP WebSocket endpoint for human session handoff.

        Returns None if the surface doesn't support CDP (e.g. desktop adapter).
        """
        ...

    async def element_exists(self, selector: str) -> bool:
        """Check if an element exists on the surface (no wait)."""
        ...

    async def get_page_text(self) -> str:
        """Get all visible text on the page."""
        ...
