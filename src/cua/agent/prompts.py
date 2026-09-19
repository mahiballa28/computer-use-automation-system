"""System prompts and tool definitions for the LLM agent loop.

The agent uses Claude's tool-use (function-calling) capability to interact
with the UI surface. Each action is a tool call that the agent loop translates
into a surface adapter method call.

The system prompt is carefully designed to:
1. Give the LLM context about what it's automating (banking back-office)
2. Explain the observation format (accessibility tree)
3. Define the available actions as tools
4. Set clear boundaries (safety, when to stop, when to escalate)
"""

from __future__ import annotations

SYSTEM_PROMPT = """You are an automation agent operating a legacy banking application.
Your job is to accomplish a specific goal by interacting with the application's UI.

## How you perceive the application

You receive the current state as an accessibility tree — a structured representation
of all interactive elements on the page, showing their roles (button, textbox, link,
etc.), names, and values. This is like what a screen reader sees.

Example:
  [textbox] "Member ID" value=""
  [button] "Search"
  [link] "View Details"

## How you act

You use tools to interact with the application. Each tool performs one atomic action.
After each action, you receive the updated page state and decide the next action.

## Important rules

1. OBSERVE before you act. Read the accessibility tree carefully before deciding.
2. ONE action per turn. Do not try to do multiple things at once.
3. VERIFY results. After important actions, use assert_page_state to confirm.
4. STOP when done. Call mark_goal_complete with the extracted data when the goal is achieved.
5. STOP if stuck. Call mark_stuck if you cannot make progress after 3 attempts.
6. NEVER enter real credentials, SSNs, or PII. Use the provided test data only.
7. PREFER semantic targeting. Describe elements by their role and label, not position.

## Your goal

{goal}

## Current state

URL: {url}
Page title: {title}

Accessibility tree:
{accessibility_tree}

{error_context}"""

# Tool definitions for Claude tool-use
AGENT_TOOLS = [
    {
        "name": "click_element",
        "description": (
            "Click on an element. Describe the element by its role and visible text/label. "
            "Example: role='button', name='Search' or role='link', name='View Details'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Accessibility role: button, link, textbox, combobox, checkbox, etc.",
                },
                "name": {
                    "type": "string",
                    "description": "Accessible name or visible text of the element",
                },
                "description": {
                    "type": "string",
                    "description": "Why you are clicking this element (for recording)",
                },
            },
            "required": ["role", "name", "description"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text into an input field. First identify the field by role and name, "
            "then provide the text to type. The field will be cleared first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Usually 'textbox' or 'searchbox'",
                },
                "name": {
                    "type": "string",
                    "description": "Accessible name or label of the input field",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type into the field",
                },
                "description": {
                    "type": "string",
                    "description": "Why you are typing this (for recording)",
                },
                "is_parameter": {
                    "type": "boolean",
                    "description": "True if this text comes from the goal's input parameters (e.g. a member ID)",
                    "default": False,
                },
                "parameter_name": {
                    "type": "string",
                    "description": "If is_parameter=true, the name of the parameter (e.g. 'member_id')",
                },
            },
            "required": ["role", "name", "text", "description"],
        },
    },
    {
        "name": "select_option",
        "description": "Select an option from a dropdown/combobox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Usually 'combobox'",
                },
                "name": {
                    "type": "string",
                    "description": "Accessible name of the select element",
                },
                "value": {
                    "type": "string",
                    "description": "The option value to select",
                },
                "description": {
                    "type": "string",
                    "description": "Why you are selecting this option",
                },
            },
            "required": ["role", "name", "value", "description"],
        },
    },
    {
        "name": "navigate_to",
        "description": "Navigate directly to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to",
                },
                "description": {
                    "type": "string",
                    "description": "Why you are navigating here",
                },
            },
            "required": ["url", "description"],
        },
    },
    {
        "name": "extract_data",
        "description": (
            "Extract a piece of data from the current page. "
            "Specify what to look for and give it a name for the output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "output_name": {
                    "type": "string",
                    "description": "Name for this extracted value (e.g. 'savings_balance', 'member_name')",
                },
                "description": {
                    "type": "string",
                    "description": "What you are extracting and where you see it",
                },
                "value": {
                    "type": "string",
                    "description": "The extracted value as you see it on the page",
                },
                "sensitive": {
                    "type": "boolean",
                    "description": "True if this is sensitive data (SSN, full account number, etc.)",
                    "default": False,
                },
            },
            "required": ["output_name", "description", "value"],
        },
    },
    {
        "name": "assert_page_state",
        "description": "Verify that the page is in an expected state. Use this after important actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "description": "What you expect to be true (e.g. 'Member detail page is displayed')",
                },
                "evidence": {
                    "type": "string",
                    "description": "What you see that confirms the condition (e.g. 'Page title shows Member Detail')",
                },
            },
            "required": ["condition", "evidence"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key (Enter, Tab, Escape, etc.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press: Enter, Tab, Escape, Backspace, etc.",
                },
                "description": {
                    "type": "string",
                    "description": "Why you are pressing this key",
                },
            },
            "required": ["key", "description"],
        },
    },
    {
        "name": "wait_for_page",
        "description": "Wait for the page to load or for specific content to appear.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wait_for": {
                    "type": "string",
                    "description": "What to wait for (e.g. 'page to finish loading', 'search results to appear')",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait",
                    "default": 5,
                },
            },
            "required": ["wait_for"],
        },
    },
    {
        "name": "mark_goal_complete",
        "description": (
            "Signal that the goal has been achieved. Include all extracted outputs. "
            "Call this ONLY when you have confirmed the goal is fully met."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was accomplished",
                },
                "outputs": {
                    "type": "object",
                    "description": "Key-value map of all extracted data",
                },
            },
            "required": ["summary", "outputs"],
        },
    },
    {
        "name": "mark_stuck",
        "description": (
            "Signal that you cannot make progress toward the goal. "
            "Use this when you've tried multiple approaches and none worked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why you are stuck and what you tried",
                },
                "last_error": {
                    "type": "string",
                    "description": "The most recent error or unexpected state you encountered",
                },
            },
            "required": ["reason"],
        },
    },
]
