"""Shared test fixtures."""

from __future__ import annotations

import pytest
import yaml

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
from cua.models.policy import SafetyPolicy


@pytest.fixture
def sample_capability() -> Capability:
    """A complete sample capability for testing."""
    return Capability(
        id="test-cap-001",
        version=1,
        name="lookup_member_balance",
        description="Look up a member by ID and read their savings balance",
        surface=SurfaceTarget(
            surface_type="web",
            entry_point="http://localhost:5001",
        ),
        input_parameters=[
            ParameterSpec(
                name="member_id",
                type="string",
                description="Member ID to look up",
                required=True,
                validation_pattern=r"^M\d{4}$",
            ),
        ],
        outputs=[
            OutputSpec(
                name="savings_balance",
                type="string",
                description="Current savings account balance",
                extraction_step_id="step_04",
            ),
            OutputSpec(
                name="member_name",
                type="string",
                description="Full name of the member",
                extraction_step_id="step_03",
            ),
        ],
        steps=[
            Step(
                id="step_00",
                description="Navigate to login page and log in",
                action=ActionType.TYPE,
                target=ElementTarget(
                    description="Username input field",
                    strategies=[
                        LocatorStrategy(
                            type=LocatorType.ACCESSIBILITY,
                            value="textbox[name='Username:']",
                            attributes={"role": "textbox", "name": "Username:"},
                            confidence=0.95,
                        ),
                    ],
                ),
                parameters={"text": "operator"},
                risk_level=RiskLevel.CAUTIOUS,
            ),
            Step(
                id="step_01",
                description="Click Sign In button",
                action=ActionType.CLICK,
                target=ElementTarget(
                    description="Sign In button",
                    strategies=[
                        LocatorStrategy(
                            type=LocatorType.ACCESSIBILITY,
                            value="button[name='Sign In']",
                            attributes={"role": "button", "name": "Sign In"},
                            confidence=0.95,
                        ),
                    ],
                ),
                checkpoint=Checkpoint(
                    description="Dashboard loads after login",
                    type=CheckpointType.TEXT_PRESENT,
                    value="Welcome",
                ),
                risk_level=RiskLevel.SAFE,
            ),
            Step(
                id="step_02",
                description="Navigate to member search and search for the member",
                action=ActionType.CLICK,
                target=ElementTarget(
                    description="Member Search link",
                    strategies=[
                        LocatorStrategy(
                            type=LocatorType.ACCESSIBILITY,
                            value="link[name='Member Search']",
                            attributes={"role": "link", "name": "Member Search"},
                            confidence=0.90,
                        ),
                        LocatorStrategy(
                            type=LocatorType.TEXT_CONTENT,
                            value="Member Search",
                            confidence=0.80,
                        ),
                    ],
                ),
                risk_level=RiskLevel.SAFE,
            ),
            Step(
                id="step_02b",
                description="Type the member ID into the search field",
                action=ActionType.TYPE,
                target=ElementTarget(
                    description="Member ID search input",
                    strategies=[
                        LocatorStrategy(
                            type=LocatorType.ACCESSIBILITY,
                            value="textbox[name='Member ID or Name:']",
                            attributes={"role": "textbox", "name": "Member ID or Name:"},
                            confidence=0.90,
                        ),
                        LocatorStrategy(
                            type=LocatorType.LABEL_PROXIMITY,
                            value="Member ID or Name:",
                            confidence=0.70,
                        ),
                    ],
                ),
                parameters={"text": "{{member_id}}"},
                risk_level=RiskLevel.CAUTIOUS,
            ),
            Step(
                id="step_02c",
                description="Click Search button",
                action=ActionType.CLICK,
                target=ElementTarget(
                    description="Search button",
                    strategies=[
                        LocatorStrategy(
                            type=LocatorType.ACCESSIBILITY,
                            value="button[name='Search']",
                            attributes={"role": "button", "name": "Search"},
                            confidence=0.95,
                        ),
                    ],
                ),
                checkpoint=Checkpoint(
                    description="Search results appear",
                    type=CheckpointType.TEXT_PRESENT,
                    value="result(s) found",
                ),
                risk_level=RiskLevel.SAFE,
            ),
            Step(
                id="step_02d",
                description="Click View Details for the member",
                action=ActionType.CLICK,
                target=ElementTarget(
                    description="View Details link for the member",
                    strategies=[
                        LocatorStrategy(
                            type=LocatorType.ACCESSIBILITY,
                            value="link[name='View Details']",
                            attributes={"role": "link", "name": "View Details"},
                            confidence=0.85,
                        ),
                        LocatorStrategy(
                            type=LocatorType.TEXT_CONTENT,
                            value="View Details",
                            confidence=0.75,
                        ),
                    ],
                ),
                checkpoint=Checkpoint(
                    description="Member detail page loads",
                    type=CheckpointType.TEXT_PRESENT,
                    value="Member Detail",
                ),
                risk_level=RiskLevel.SAFE,
            ),
            Step(
                id="step_03",
                description="Extract member name",
                action=ActionType.EXTRACT,
                target=None,
                parameters={"output_name": "member_name"},
                risk_level=RiskLevel.SAFE,
            ),
            Step(
                id="step_04",
                description="Extract savings balance",
                action=ActionType.EXTRACT,
                target=None,
                parameters={"output_name": "savings_balance"},
                risk_level=RiskLevel.SAFE,
            ),
        ],
        success_condition=Checkpoint(
            description="Member detail page with account information is displayed",
            type=CheckpointType.TEXT_PRESENT,
            value="Member Detail",
        ),
        error_handlers=[
            ErrorHandler(
                name="member_not_found",
                description="No member matches the search",
                detection=Checkpoint(
                    description="No results message visible",
                    type=CheckpointType.TEXT_PRESENT,
                    value="No members found",
                ),
                action=ErrorPolicy.REPORT_OUTCOME,
                message_template="Member not found for ID: {{member_id}}",
            ),
            ErrorHandler(
                name="session_expired",
                description="Session has timed out",
                detection=Checkpoint(
                    description="Login page with session expired message",
                    type=CheckpointType.TEXT_PRESENT,
                    value="Session expired",
                ),
                action=ErrorPolicy.ESCALATE,
                message_template="Session expired during execution",
            ),
        ],
        approval_state=ApprovalState.DRAFT,
        recorded_from_run_id="test-run-001",
    )


@pytest.fixture
def sample_policy() -> SafetyPolicy:
    """A sample safety policy for testing."""
    return SafetyPolicy.model_validate(
        yaml.safe_load("""
name: test
description: Test safety policy
allowed_domains:
  - localhost
  - 127.0.0.1
allowlist_rules:
  - pattern: "http://localhost:5001/*"
    allowed_actions: [click, type, select, navigate, extract, assert, wait]
    max_risk_level: cautious
risky_action_policy: require_confirmation
block_irreversible_without_confirmation: true
irreversible_keywords: [delete, transfer, submit, confirm]
redaction_patterns:
  - name: ssn
    pattern: '\\b\\d{3}-\\d{2}-\\d{4}\\b'
    replacement: "[SSN REDACTED]"
  - name: email
    pattern: '\\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}\\b'
    replacement: "[EMAIL REDACTED]"
max_steps_per_run: 50
max_run_duration_seconds: 300
max_retries_per_step: 3
""")
    )
