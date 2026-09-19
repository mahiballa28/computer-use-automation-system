"""Structured logging for the CUA system.

All log entries are structured JSON with consistent fields:
- run_id: ties all events in a single run together
- component: which subsystem emitted the event (agent, replay, safety, etc.)
- step_id: if inside a step execution
- event: machine-readable event name
- (+ any additional context fields)

The redactor is applied as a structlog processor so sensitive data is
scrubbed before it reaches any output (file, console, etc.).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from cua.safety.redactor import Redactor


def configure_logging(
    log_level: str = "INFO",
    log_format: str = "json",
    log_file: str | None = None,
    redactor: Redactor | None = None,
) -> None:
    """Configure structlog with CUA-specific processors."""

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    # Add redaction processor if we have a redactor
    if redactor:
        processors.append(_make_redaction_processor(redactor))

    processors.append(structlog.processors.StackInfoRenderer())
    processors.append(structlog.processors.format_exc_info)

    if log_format == "console":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.get_level_from_name(log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(
            file=open(log_file, "a") if log_file else sys.stderr  # noqa: SIM115
        ),
        cache_logger_on_first_use=True,
    )


def _make_redaction_processor(redactor: Redactor) -> Any:
    """Create a structlog processor that redacts sensitive data in log events."""

    def redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in event_dict.items():
            if isinstance(value, str):
                event_dict[key] = redactor.redact(value)
        return event_dict

    return redact_processor


def get_logger(component: str, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Get a bound logger for a specific component."""
    return structlog.get_logger(component=component, **initial_context)


class RunLogger:
    """Convenience wrapper for logging an entire run with consistent context."""

    def __init__(self, run_id: str, capability_id: str, component: str = "replay") -> None:
        self.run_id = run_id
        self.capability_id = capability_id
        self._log = get_logger(component, run_id=run_id, capability_id=capability_id)
        self._events: list[dict[str, Any]] = []

    def step_start(self, step_id: str, action: str, description: str) -> None:
        event = {
            "event": "step_start",
            "step_id": step_id,
            "action": action,
            "description": description,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._events.append(event)
        self._log.info("step_start", step_id=step_id, action=action, description=description)

    def step_complete(self, step_id: str, success: bool, duration_ms: float, **extra: Any) -> None:
        event = {
            "event": "step_complete",
            "step_id": step_id,
            "success": success,
            "duration_ms": duration_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        self._events.append(event)
        self._log.info("step_complete", step_id=step_id, success=success, duration_ms=f"{duration_ms:.1f}", **extra)

    def step_error(self, step_id: str, error_type: str, message: str, **extra: Any) -> None:
        event = {
            "event": "step_error",
            "step_id": step_id,
            "error_type": error_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        self._events.append(event)
        self._log.error("step_error", step_id=step_id, error_type=error_type, message=message, **extra)

    def run_complete(self, status: str, **extra: Any) -> None:
        event = {
            "event": "run_complete",
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        self._events.append(event)
        self._log.info("run_complete", status=status, **extra)

    def info(self, event: str, **extra: Any) -> None:
        entry = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **extra}
        self._events.append(entry)
        self._log.info(event, **extra)

    def save_evidence(self, path: str) -> None:
        """Save the accumulated event log to a JSON file."""
        evidence_path = Path(path)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with open(evidence_path, "w") as f:
            json.dump(
                {
                    "run_id": self.run_id,
                    "capability_id": self.capability_id,
                    "events": self._events,
                },
                f,
                indent=2,
            )

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events
