"""Tests for the intelligent capability registry with TF-IDF search."""

from cua.models.capability import (
    ActionType,
    ApprovalState,
    Capability,
    Checkpoint,
    CheckpointType,
    OutputSpec,
    ParameterSpec,
    RiskLevel,
    Step,
    SurfaceTarget,
)
from cua.orchestration.capability_registry import CapabilityRegistry, _tokenize


def _make_capability(
    id: str, name: str, description: str, tags: list[str] | None = None
) -> Capability:
    """Create a minimal capability for testing."""
    return Capability(
        id=id,
        version=1,
        name=name,
        description=description,
        surface=SurfaceTarget(
            surface_type="web",
            entry_point="http://localhost",
            app_id="test",
        ),
        input_parameters=[
            ParameterSpec(name="member_id", type="string", description="Member ID"),
        ],
        outputs=[
            OutputSpec(
                name="balance",
                type="string",
                description="Account balance",
                extraction_step_id="step_01",
            ),
        ],
        steps=[
            Step(
                id="step_01",
                description="Extract balance",
                action=ActionType.EXTRACT,
                risk_level=RiskLevel.SAFE,
            ),
        ],
        success_condition=Checkpoint(
            description="Done", type=CheckpointType.TEXT_PRESENT, value="Balance"
        ),
        approval_state=ApprovalState.DRAFT,
        tags=tags or [],
    )


class TestTokenizer:
    def test_basic_tokenization(self) -> None:
        tokens = _tokenize("Look up member savings balance")
        assert "look" in tokens
        assert "member" in tokens
        assert "savings" in tokens
        assert "balance" in tokens

    def test_stopwords_removed(self) -> None:
        tokens = _tokenize("the member is in the system")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "in" not in tokens
        assert "member" in tokens


class TestCapabilityRegistry:
    def test_add_and_search(self) -> None:
        registry = CapabilityRegistry()
        registry.add_capability(
            _make_capability("cap-1", "lookup_balance", "Look up member savings balance",
                             tags=["balance", "member"]),
        )
        registry.add_capability(
            _make_capability("cap-2", "transfer_funds", "Transfer money between accounts",
                             tags=["transfer", "funds"]),
        )

        results = registry.search("savings balance lookup")
        assert len(results) > 0
        assert results[0].capability_id == "cap-1"

    def test_search_by_description(self) -> None:
        registry = CapabilityRegistry()
        registry.add_capability(
            _make_capability("cap-1", "check_balance", "Check the current savings balance for a credit union member"),
        )
        registry.add_capability(
            _make_capability("cap-2", "open_account", "Open a new checking account for a member"),
        )

        results = registry.search("savings balance")
        assert results[0].capability_id == "cap-1"

    def test_empty_query_returns_nothing(self) -> None:
        registry = CapabilityRegistry()
        registry.add_capability(
            _make_capability("cap-1", "test", "test description"),
        )
        results = registry.search("")
        assert len(results) == 0

    def test_no_match_returns_empty(self) -> None:
        registry = CapabilityRegistry()
        registry.add_capability(
            _make_capability("cap-1", "balance", "Check balance"),
        )
        results = registry.search("xyz789 nonexistent")
        assert len(results) == 0

    def test_list_all(self) -> None:
        registry = CapabilityRegistry()
        registry.add_capability(_make_capability("cap-1", "a", "first"))
        registry.add_capability(_make_capability("cap-2", "b", "second"))

        all_caps = registry.list_all()
        assert len(all_caps) == 2

    def test_max_results_limit(self) -> None:
        registry = CapabilityRegistry()
        for i in range(10):
            registry.add_capability(
                _make_capability(f"cap-{i}", f"capability_{i}", f"test capability number {i} for member balance"),
            )

        results = registry.search("member balance", max_results=3)
        assert len(results) <= 3

    def test_load_from_directory(self) -> None:
        registry = CapabilityRegistry()
        count = registry.load_from_directory("capabilities/")
        assert count >= 1  # At least lookup_member_balance.yaml

        results = registry.search("member balance")
        assert len(results) > 0
