"""Playwright-based surface adapter for web applications.

This adapter implements the SurfaceAdapter protocol using Playwright for
browser automation. It provides:

- Accessibility tree snapshots as the primary perception mechanism
- Multi-strategy element location (a11y, text, CSS, XPath)
- Screenshot capture for evidence and fallback perception
- CDP endpoint exposure for human-in-the-loop session handoff

The adapter favors accessibility tree perception over raw DOM because:
1. A11y trees are more stable across CSS changes and minor DOM restructuring
2. They provide semantic meaning (role, name) that raw selectors don't
3. They map directly to desktop accessibility APIs (AT-SPI, UIAutomation)
4. They produce smaller, cheaper LLM prompts than raw HTML
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from cua.surfaces.protocol import ElementInfo, PageState


class PlaywrightAdapter:
    """Web surface adapter using Playwright."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp_url: str | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            msg = "Surface not launched. Call launch() first."
            raise RuntimeError(msg)
        return self._page

    async def launch(self, entry_point: str, headless: bool = True) -> None:
        """Launch browser and navigate to entry point."""
        self._playwright = await async_playwright().start()

        # Use chromium with CDP enabled for session handoff support
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=["--remote-debugging-port=0"],  # random port for CDP
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1_280, "height": 720},
            user_agent="CUA-System/0.1 (automation)",
        )
        self._page = await self._context.new_page()
        await self._page.goto(entry_point, wait_until="networkidle", timeout=30_000)

    async def close(self) -> None:
        """Clean up browser resources."""
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def get_state(self, capture_screenshot: bool = False) -> PageState:
        """Capture current page state: URL, title, accessibility tree, and optionally a screenshot."""
        page = self.page

        url = page.url
        title = await page.title()

        # Accessibility tree snapshot — the primary perception mechanism
        a11y_tree = await self._get_accessibility_tree()

        # Find visible error text (common patterns)
        error_text = await self._detect_error_text()

        screenshot_path = None
        if capture_screenshot:
            screenshot_path = f"evidence/screenshots/state_{hash(url) % 10000:04d}.png"
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=screenshot_path)

        return PageState(
            url=url,
            title=title,
            accessibility_tree=a11y_tree,
            screenshot_path=screenshot_path,
            error_text=error_text,
        )

    async def _get_accessibility_tree(self) -> str:
        """Get a text representation of the accessibility tree.

        We serialize the tree to a compact text format that's easy for the LLM
        to parse and reason about, while being much smaller than raw HTML.
        """
        page = self.page
        try:
            snapshot = await page.accessibility.snapshot()  # type: ignore[union-attr]
            if snapshot:
                return self._serialize_a11y_node(snapshot, depth=0)
        except Exception:
            pass
        # Fallback: extract visible text
        return await page.inner_text("body")

    def _serialize_a11y_node(self, node: dict[str, Any], depth: int) -> str:
        """Recursively serialize an accessibility tree node to text."""
        indent = "  " * depth
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")

        # Skip generic/container nodes with no useful info
        if role in ("none", "generic", "paragraph", "group") and not name and not value:
            children = node.get("children", [])
            if children:
                return "\n".join(
                    self._serialize_a11y_node(child, depth) for child in children
                )
            return ""

        parts = [f"{indent}[{role}]"]
        if name:
            parts.append(f'"{name}"')
        if value:
            parts.append(f"value={value!r}")

        # Key attributes
        if node.get("checked") is not None:
            parts.append(f"checked={node['checked']}")
        if node.get("disabled"):
            parts.append("disabled")
        if node.get("required"):
            parts.append("required")

        line = " ".join(parts)
        lines = [line]

        children = node.get("children", [])
        for child in children:
            child_text = self._serialize_a11y_node(child, depth + 1)
            if child_text:
                lines.append(child_text)

        return "\n".join(lines)

    async def _detect_error_text(self) -> str | None:
        """Look for common error patterns on the page."""
        page = self.page
        try:
            # Check for elements with common error styling/patterns
            error_selectors = [
                "font[color='#CC0000']",
                "[style*='color: red']",
                "[style*='color:#CC0000']",
                ".error",
                ".alert-danger",
            ]
            for selector in error_selectors:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = await el.inner_text()
                    if text and text.strip():
                        return text.strip()
        except Exception:
            pass
        return None

    async def click(self, selector: str, timeout_ms: int = 5_000) -> None:
        await self.page.click(selector, timeout=timeout_ms)

    async def type_text(self, selector: str, text: str, timeout_ms: int = 5_000) -> None:
        await self.page.fill(selector, text, timeout=timeout_ms)

    async def clear_field(self, selector: str, timeout_ms: int = 5_000) -> None:
        await self.page.fill(selector, "", timeout=timeout_ms)

    async def select_option(self, selector: str, value: str, timeout_ms: int = 5_000) -> None:
        await self.page.select_option(selector, value, timeout=timeout_ms)

    async def navigate(self, url: str, timeout_ms: int = 10_000) -> None:
        await self.page.goto(url, wait_until="networkidle", timeout=timeout_ms)

    async def press_key(self, key: str) -> None:
        await self.page.keyboard.press(key)

    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        delta = amount if direction == "down" else -amount
        await self.page.mouse.wheel(0, delta)

    async def extract_text(self, selector: str, timeout_ms: int = 5_000) -> str:
        element = await self.page.wait_for_selector(selector, timeout=timeout_ms)
        if element:
            return (await element.inner_text()).strip()
        return ""

    async def wait_for_element(self, selector: str, timeout_ms: int = 10_000) -> bool:
        try:
            await self.page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def wait_for_text(self, text: str, timeout_ms: int = 10_000) -> bool:
        try:
            await self.page.wait_for_function(
                f"document.body.innerText.includes({json.dumps(text)})",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    async def find_elements(self, role: str | None = None) -> list[ElementInfo]:
        """Find all interactive elements using the accessibility tree."""
        page = self.page
        snapshot = await page.accessibility.snapshot()  # type: ignore[union-attr]
        if not snapshot:
            return []

        elements: list[ElementInfo] = []
        self._collect_elements(snapshot, elements, role_filter=role)
        return elements

    def _collect_elements(
        self, node: dict[str, Any], elements: list[ElementInfo], role_filter: str | None = None
    ) -> None:
        """Recursively collect interactive elements from the a11y tree."""
        role = node.get("role", "")
        name = node.get("name", "")

        interactive_roles = {
            "textbox", "button", "link", "combobox", "checkbox", "radio",
            "menuitem", "tab", "slider", "spinbutton", "searchbox",
        }

        if role in interactive_roles:
            if role_filter is None or role == role_filter:
                elements.append(ElementInfo(
                    role=role,
                    name=name,
                    value=node.get("value", ""),
                    text=name,
                    is_enabled=not node.get("disabled", False),
                ))

        for child in node.get("children", []):
            self._collect_elements(child, elements, role_filter)

    async def screenshot(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=path)
        return path

    async def get_cdp_endpoint(self) -> str | None:
        """Get CDP WebSocket URL for human session handoff."""
        if self._browser:
            # In Playwright, we can get CDP session via the browser context
            try:
                cdp_session = await self._context.new_cdp_session(self.page)  # type: ignore[union-attr]
                # The CDP endpoint is available through the browser's connection
                return self._cdp_url
            except Exception:
                pass
        return None

    async def element_exists(self, selector: str) -> bool:
        try:
            element = await self.page.query_selector(selector)
            return element is not None
        except Exception:
            return False

    async def get_page_text(self) -> str:
        return await self.page.inner_text("body")

    # ----- Locator resolution helpers (used by replay engine) -----

    async def find_by_accessibility(self, role: str, name: str, timeout_ms: int = 5_000) -> str | None:
        """Find an element by accessibility role and name. Returns a selector if found."""
        try:
            locator = self.page.get_by_role(role, name=name)  # type: ignore[arg-type]
            await locator.wait_for(timeout=timeout_ms, state="visible")
            return f"role={role}[name={json.dumps(name)}]"
        except Exception:
            return None

    async def find_by_text(self, text: str, exact: bool = False, timeout_ms: int = 5_000) -> str | None:
        """Find an element by visible text content."""
        try:
            locator = self.page.get_by_text(text, exact=exact)
            await locator.first.wait_for(timeout=timeout_ms, state="visible")
            return f"text={json.dumps(text)}"
        except Exception:
            return None

    async def find_by_label(self, label_text: str, timeout_ms: int = 5_000) -> str | None:
        """Find an input element by its associated label text."""
        try:
            locator = self.page.get_by_label(label_text)
            await locator.wait_for(timeout=timeout_ms, state="visible")
            return f"label={json.dumps(label_text)}"
        except Exception:
            return None

    async def click_by_role(self, role: str, name: str, timeout_ms: int = 5_000) -> None:
        """Click an element using Playwright's role-based locator."""
        locator = self.page.get_by_role(role, name=name)  # type: ignore[arg-type]
        await locator.click(timeout=timeout_ms)

    async def fill_by_role(self, role: str, name: str, value: str, timeout_ms: int = 5_000) -> None:
        """Fill an input using Playwright's role-based locator."""
        locator = self.page.get_by_role(role, name=name)  # type: ignore[arg-type]
        await locator.fill(value, timeout=timeout_ms)

    async def get_element_bounds(self, selector: str) -> tuple[float, float, float, float] | None:
        """Get element bounding box (x, y, width, height)."""
        try:
            element = await self.page.query_selector(selector)
            if element:
                box = await element.bounding_box()
                if box:
                    return (box["x"], box["y"], box["width"], box["height"])
        except Exception:
            pass
        return None
