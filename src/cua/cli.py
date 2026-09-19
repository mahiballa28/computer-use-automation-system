"""CLI entry point for the CUA system.

Commands:
  cua discover  — Run an LLM-driven discovery to complete a goal
  cua replay    — Deterministically replay a saved capability
  cua list      — List saved capabilities
  cua show      — Show a capability's details
  cua workflow  — Execute a workflow DAG
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cua.config import Settings, get_settings

app = typer.Typer(
    name="cua",
    help="Computer-Use Automation System — LLM discovery, deterministic replay, human escalation",
    no_args_is_help=True,
)
console = Console()


def _load_policy(settings: Settings) -> Any:
    """Load the default safety policy."""
    from cua.models.policy import SafetyPolicy

    policy_path = settings.policies_dir / "default.yaml"
    if policy_path.exists():
        with open(policy_path) as f:
            data = yaml.safe_load(f)
        return SafetyPolicy.model_validate(data)
    return SafetyPolicy()


@app.command()
def discover(
    goal: str = typer.Argument(help="Natural language goal to accomplish"),
    target: str = typer.Option("http://localhost:5001", help="Target application URL"),
    policy: str = typer.Option("policies/default.yaml", help="Safety policy file"),
    headless: bool = typer.Option(True, help="Run browser in headless mode"),
    output_dir: str = typer.Option("capabilities", help="Directory to save the capability artifact"),
    evidence_dir: str = typer.Option("evidence", help="Directory to save run evidence"),
) -> None:
    """Run an LLM-driven discovery to complete a goal, then save the capability artifact."""
    settings = get_settings()
    settings.headless = headless

    if not settings.anthropic_api_key:
        console.print("[red]Error: ANTHROPIC_API_KEY not set. Add it to .env or environment.[/red]")
        raise typer.Exit(1)

    console.print(Panel(f"[bold]Discovery Run[/bold]\nGoal: {goal}\nTarget: {target}", title="CUA System"))

    asyncio.run(_run_discovery(settings, goal, target, policy, output_dir, evidence_dir))


async def _run_discovery(
    settings: Settings,
    goal: str,
    target: str,
    policy_path: str,
    output_dir: str,
    evidence_dir: str,
) -> None:
    from cua.agent.loop import AgentLoop
    from cua.models.policy import SafetyPolicy
    from cua.observability.logger import RunLogger, configure_logging
    from cua.recording.recorder import Recorder
    from cua.safety.guard import SafetyGuard
    from cua.safety.redactor import Redactor
    from cua.surfaces.playwright_adapter import PlaywrightAdapter

    # Load policy
    with open(policy_path) as f:
        policy_data = yaml.safe_load(f)
    policy = SafetyPolicy.model_validate(policy_data)

    # Setup
    redactor = Redactor(policy)
    configure_logging(settings.log_level, "console", redactor=redactor)

    import uuid

    run_id = str(uuid.uuid4())
    run_logger = RunLogger(run_id, "discovery", component="agent")

    guard = SafetyGuard(policy)
    surface = PlaywrightAdapter()

    try:
        await surface.launch(target, headless=settings.headless)

        agent = AgentLoop(settings, surface, guard, run_logger)
        trace = await agent.run(goal, target)

        if trace.success:
            console.print("\n[green]Discovery succeeded![/green]")
            console.print(f"Steps taken: {len(trace.actions)}")
            console.print(f"Outputs: {json.dumps(trace.outputs, indent=2)}")

            # Record the capability
            recorder = Recorder()
            capability = recorder.record(trace)
            filepath = recorder.save(capability, output_dir)

            console.print(f"\n[bold]Capability saved:[/bold] {filepath}")
            console.print(f"Name: {capability.name}")
            console.print(f"ID: {capability.id}")
            console.print(f"Steps: {len(capability.steps)}")
            console.print(f"Inputs: {[p.name for p in capability.input_parameters]}")
            console.print(f"Outputs: {[o.name for o in capability.outputs]}")
        else:
            console.print(f"\n[red]Discovery failed:[/red] {trace.stuck_reason}")

        # Save evidence
        evidence_path = Path(evidence_dir) / f"discovery_run_{run_id[:8]}.json"
        run_logger.save_evidence(str(evidence_path))
        console.print(f"Evidence saved: {evidence_path}")

    finally:
        await surface.close()


@app.command()
def replay(
    capability_path: str = typer.Argument(help="Path to the capability YAML file"),
    params: str = typer.Option("{}", help="Input parameters as JSON string"),
    headless: bool = typer.Option(True, help="Run browser in headless mode"),
    detect_anomalies: bool = typer.Option(False, help="Enable anomaly detection"),
    evidence_dir: str = typer.Option("evidence", help="Directory to save run evidence"),
) -> None:
    """Deterministically replay a saved capability."""
    settings = get_settings()
    settings.headless = headless

    # Parse params
    try:
        input_params = json.loads(params)
    except json.JSONDecodeError:
        console.print("[red]Error: Invalid JSON in --params[/red]")
        raise typer.Exit(1)

    # Load capability
    try:
        with open(capability_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        console.print(f"[red]Error: Capability not found: {capability_path}[/red]")
        raise typer.Exit(1)

    from cua.models.capability import Capability

    capability = Capability.model_validate(data)

    console.print(Panel(
        f"[bold]Replay Run[/bold]\n"
        f"Capability: {capability.name}\n"
        f"Params: {json.dumps(input_params)}\n"
        f"Steps: {len(capability.steps)}",
        title="CUA System",
    ))

    asyncio.run(_run_replay(settings, capability, input_params, detect_anomalies, evidence_dir))


async def _run_replay(
    settings: Settings,
    capability: Any,
    params: dict[str, Any],
    detect_anomalies: bool,
    evidence_dir: str,
) -> None:
    from cua.models.policy import SafetyPolicy
    from cua.observability.logger import RunLogger, configure_logging
    from cua.replay.executor import ReplayExecutor
    from cua.replay.locator_resolver import LocatorResolver
    from cua.safety.guard import SafetyGuard
    from cua.safety.redactor import Redactor
    from cua.surfaces.playwright_adapter import PlaywrightAdapter

    # Load policy
    policy = _load_policy(settings)
    redactor = Redactor(policy)
    configure_logging(settings.log_level, "console", redactor=redactor)

    import uuid

    run_id = str(uuid.uuid4())
    run_logger = RunLogger(run_id, capability.id, component="replay")

    guard = SafetyGuard(policy)
    surface = PlaywrightAdapter()

    try:
        await surface.launch(capability.surface.entry_point, headless=settings.headless)

        resolver = LocatorResolver(surface)
        executor = ReplayExecutor(surface, resolver, guard, run_logger)

        result = await executor.execute(capability, params)

        # Display results
        if result.is_success():
            console.print("\n[green]Replay succeeded![/green]")
            console.print(f"Completed: {result.completed_steps}/{result.total_steps} steps")
            if result.outputs:
                console.print("\n[bold]Outputs:[/bold]")
                for key, value in result.outputs.items():
                    console.print(f"  {key}: {value}")
        elif result.is_business_outcome():
            console.print(f"\n[yellow]Business Outcome:[/yellow] {result.business_outcome}")
            if result.business_outcome:
                console.print(f"  Name: {result.business_outcome.outcome_name}")
                console.print(f"  Message: {result.business_outcome.message}")
        else:
            console.print(f"\n[red]Replay failed:[/red]")
            if result.failure:
                console.print(f"  Error: {result.failure.error_type}")
                console.print(f"  Message: {result.failure.message}")
                console.print(f"  Step: {result.failure.step_id}")
                if result.failure.screenshot_path:
                    console.print(f"  Screenshot: {result.failure.screenshot_path}")

        # Anomaly detection
        if detect_anomalies and result.anomalies:
            console.print("\n[yellow]Anomalies detected:[/yellow]")
            for anomaly in result.anomalies:
                console.print(f"  [{anomaly.get('severity', 'info')}] {anomaly.get('message', '')}")

        # Display summary
        console.print(f"\n{result.summary()}")

        # Save evidence
        evidence_path = Path(evidence_dir) / f"replay_run_{run_id[:8]}.json"
        run_logger.save_evidence(str(evidence_path))
        console.print(f"Evidence saved: {evidence_path}")

    finally:
        await surface.close()


@app.command(name="list")
def list_capabilities(
    directory: str = typer.Option("capabilities", help="Capabilities directory"),
) -> None:
    """List all saved capabilities."""
    from cua.models.capability import Capability

    cap_dir = Path(directory)
    if not cap_dir.exists():
        console.print(f"[yellow]No capabilities directory: {directory}[/yellow]")
        return

    table = Table(title="Saved Capabilities")
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Steps")
    table.add_column("Inputs")
    table.add_column("Outputs")
    table.add_column("Status")
    table.add_column("File")

    for yaml_file in sorted(cap_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            cap = Capability.model_validate(data)
            table.add_row(
                cap.name,
                str(cap.version),
                str(len(cap.steps)),
                ", ".join(p.name for p in cap.input_parameters),
                ", ".join(o.name for o in cap.outputs),
                cap.approval_state.value,
                yaml_file.name,
            )
        except Exception as e:
            table.add_row(yaml_file.name, "?", "?", "?", "?", f"[red]Error: {e}[/red]", yaml_file.name)

    console.print(table)


@app.command()
def show(
    capability_path: str = typer.Argument(help="Path to the capability YAML file"),
) -> None:
    """Show detailed information about a capability."""
    from cua.models.capability import Capability

    with open(capability_path) as f:
        data = yaml.safe_load(f)
    cap = Capability.model_validate(data)

    console.print(Panel(
        f"[bold]{cap.name}[/bold] (v{cap.version})\n"
        f"ID: {cap.id}\n"
        f"Description: {cap.description}\n"
        f"Status: {cap.approval_state.value}\n"
        f"Surface: {cap.surface.entry_point}\n"
        f"Created: {cap.created_at}",
        title="Capability Detail",
    ))

    # Input parameters
    if cap.input_parameters:
        table = Table(title="Input Parameters")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Required")
        table.add_column("Sensitive")
        table.add_column("Description")
        for p in cap.input_parameters:
            table.add_row(p.name, p.type, str(p.required), str(p.sensitive), p.description)
        console.print(table)

    # Outputs
    if cap.outputs:
        table = Table(title="Outputs")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Extraction Step")
        table.add_column("Description")
        for o in cap.outputs:
            table.add_row(o.name, o.type, o.extraction_step_id, o.description)
        console.print(table)

    # Steps
    table = Table(title=f"Steps ({len(cap.steps)})")
    table.add_column("#")
    table.add_column("Action")
    table.add_column("Target")
    table.add_column("Risk")
    table.add_column("Description")
    for i, step in enumerate(cap.steps):
        target_desc = step.target.description if step.target else "—"
        table.add_row(
            step.id,
            step.action.value,
            target_desc[:40],
            step.risk_level.value,
            step.description[:50],
        )
    console.print(table)


if __name__ == "__main__":
    app()
