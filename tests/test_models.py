"""Tests for the artifact schema and model serialization."""

from __future__ import annotations

import json

import yaml

from cua.models.capability import (
    ActionType,
    ApprovalState,
    Capability,
    Checkpoint,
    CheckpointType,
    ElementTarget,
    LocatorStrategy,
    LocatorType,
    ParameterSpec,
    RiskLevel,
    Step,
    SurfaceTarget,
)
from cua.models.execution import (
    BusinessOutcome,
    ErrorCategory,
    HardFailure,
    RunResult,
    RunStatus,
)


class TestCapabilitySchema:
    """Test the core artifact schema."""

    def test_roundtrip_yaml(self, sample_capability: Capability) -> None:
        """Capability serializes to YAML and back without data loss."""
        data = sample_capability.model_dump(mode="json")
        yaml_str = yaml.dump(data, default_flow_style=False)
        loaded = yaml.safe_load(yaml_str)
        restored = Capability.model_validate(loaded)

        assert restored.name == sample_capability.name
        assert restored.id == sample_capability.id
        assert len(restored.steps) == len(sample_capability.steps)
        assert len(restored.input_parameters) == len(sample_capability.input_parameters)
        assert len(restored.outputs) == len(sample_capability.outputs)

    def test_roundtrip_json(self, sample_capability: Capability) -> None:
        """Capability serializes to JSON and back."""
        json_str = sample_capability.model_dump_json()
        restored = Capability.model_validate_json(json_str)
        assert restored.name == sample_capability.name
        assert len(restored.steps) == len(sample_capability.steps)

    def test_parameter_validation(self, sample_capability: Capability) -> None:
        """Input parameters are validated against declared patterns."""
        # Valid member ID
        resolved = sample_capability.resolve_parameters({"member_id": "M1001"})
        assert resolved["member_id"] == "M1001"

        # Invalid member ID
        import pytest

        with pytest.raises(ValueError, match="does not match pattern"):
            sample_capability.resolve_parameters({"member_id": "INVALID"})

        # Missing required parameter
        with pytest.raises(ValueError, match="Required parameter"):
            sample_capability.resolve_parameters({})

    def test_interpolation(self, sample_capability: Capability) -> None:
        """Parameter interpolation replaces {{param}} placeholders."""
        result = sample_capability.interpolate_value(
            "Search for {{member_id}} in the system",
            {"member_id": "M1001"},
        )
        assert result == "Search for M1001 in the system"

    def test_interpolation_missing_param(self, sample_capability: Capability) -> None:
        """Missing params are left as-is in interpolation."""
        result = sample_capability.interpolate_value(
            "Hello {{unknown}}",
            {"member_id": "M1001"},
        )
        assert result == "Hello {{unknown}}"

    def test_step_lookup(self, sample_capability: Capability) -> None:
        """Steps can be looked up by ID."""
        step = sample_capability.get_step("step_01")
        assert step is not None
        assert step.action == ActionType.CLICK

        missing = sample_capability.get_step("nonexistent")
        assert missing is None

    def test_locator_strategy_ordering(self, sample_capability: Capability) -> None:
        """Locator strategies are stored in priority order."""
        search_step = sample_capability.get_step("step_02")
        assert search_step is not None
        assert search_step.target is not None
        strategies = search_step.target.strategies
        assert len(strategies) == 2
        assert strategies[0].type == LocatorType.ACCESSIBILITY
        assert strategies[1].type == LocatorType.TEXT_CONTENT
        assert strategies[0].confidence > strategies[1].confidence


class TestRunResult:
    """Test the execution result models."""

    def test_success_result(self) -> None:
        result = RunResult(
            capability_id="cap-001",
            capability_version=1,
            status=RunStatus.SUCCESS,
            outputs={"balance": "$15,230.89"},
            total_steps=5,
            completed_steps=5,
        )
        assert result.is_success()
        assert not result.is_business_outcome()
        assert "SUCCESS" in result.summary()

    def test_business_outcome_result(self) -> None:
        result = RunResult(
            capability_id="cap-001",
            capability_version=1,
            status=RunStatus.BUSINESS_OUTCOME,
            business_outcome=BusinessOutcome(
                outcome_name="member_not_found",
                message="No member found with ID: M9999",
            ),
            total_steps=5,
            completed_steps=2,
        )
        assert result.is_business_outcome()
        assert "member_not_found" in result.summary()

    def test_failure_result(self) -> None:
        result = RunResult(
            capability_id="cap-001",
            capability_version=1,
            status=RunStatus.FAILURE,
            failure=HardFailure(
                error_type="element_not_found",
                message="Could not find Search button",
                step_id="step_02",
                expected="button 'Search'",
                observed="No matching elements",
            ),
            total_steps=5,
            completed_steps=1,
        )
        assert not result.is_success()
        assert "FAILURE" in result.summary()
        assert result.failure is not None
        assert result.failure.step_id == "step_02"

    def test_error_taxonomy_distinct(self) -> None:
        """The three error categories are distinct."""
        categories = set(ErrorCategory)
        assert len(categories) == 3
        assert ErrorCategory.BUSINESS_OUTCOME in categories
        assert ErrorCategory.RECOVERABLE in categories
        assert ErrorCategory.HARD_FAILURE in categories
