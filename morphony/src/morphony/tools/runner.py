from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from morphony.events import AuditLogWriter, Event, EventBus, EventType
from morphony.models import EscalationLevel, Tool
from morphony.safety import BudgetController, EscalationEngine

from .registry import ToolRegistry


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime value must be timezone-aware")
    return value.astimezone(UTC)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return _utc_now()
    return _ensure_aware(now)


def _ensure_mapping(value: dict[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    return dict(value)


def _coerce_status(value: bool) -> str:
    return "succeeded" if value else "failed"


def _resolved_cost_usd(default_cost_usd: float, output: object | None) -> float:
    if isinstance(output, dict) and "estimated_cost_usd" in output:
        output_mapping = cast(dict[str, object], output)
        raw_estimated_cost = output_mapping["estimated_cost_usd"]
        if isinstance(raw_estimated_cost, (int, float)) and not isinstance(raw_estimated_cost, bool):
            estimated = float(raw_estimated_cost)
            if estimated >= 0:
                return estimated
    return default_cost_usd


@dataclass(slots=True)
class ToolExecutionResult:
    task_id: str
    tool_name: str
    input_payload: dict[str, object]
    output: object | None
    duration_seconds: float
    cost_usd: float
    status: str
    escalation_request_id: str | None = None


class ToolExecutionRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        event_bus: EventBus | None = None,
        audit_log_writer: AuditLogWriter | None = None,
        budget_controller: BudgetController | None = None,
        escalation_engine: EscalationEngine | None = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus
        self._audit_log_writer = audit_log_writer
        self._budget_controller = budget_controller
        self._escalation_engine = escalation_engine

    def execute(
        self,
        task_id: str,
        tool_name: str,
        *,
        tool_input: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> ToolExecutionResult:
        if not task_id:
            raise ValueError("task_id must not be empty")
        if not tool_name:
            raise ValueError("tool_name must not be empty")

        timestamp = _coerce_now(now)
        normalized_input = _ensure_mapping(tool_input)
        tool = self._registry.get(tool_name)
        cost_usd = float(tool.cost_per_call)
        self._emit_event(
            Event(
                task_id=task_id,
                event_type=EventType.step_started,
                timestamp=timestamp,
                payload={
                    "tool_name": tool_name,
                    "input": normalized_input,
                    "source": "tool_execution_runner",
                },
            )
        )

        if not bool(tool.validate(**normalized_input)):
            result = ToolExecutionResult(
                task_id=task_id,
                tool_name=tool_name,
                input_payload=normalized_input,
                output=None,
                duration_seconds=0.0,
                cost_usd=0.0,
                status="validation_failed",
                escalation_request_id=None,
            )
            self._emit_execution_result(task_id, tool, result, timestamp)
            return result

        if self._budget_controller is not None:
            budget_decision = self._budget_controller.enforce_action_budget(
                task_id,
                estimated_cost_usd=cost_usd,
                estimated_elapsed_seconds=0.0,
                estimated_api_calls=1,
                now=timestamp,
            )
            if budget_decision.should_stop:
                result = ToolExecutionResult(
                    task_id=task_id,
                    tool_name=tool_name,
                    input_payload=normalized_input,
                    output=None,
                    duration_seconds=0.0,
                    cost_usd=0.0,
                    status="blocked_budget",
                    escalation_request_id=None,
                )
                self._emit_execution_result(task_id, tool, result, timestamp)
                return result

        escalation_request_id: str | None = None
        if self._escalation_engine is not None:
            escalation_record = self._escalation_engine.request_escalation(
                task_id=task_id,
                action_name=f"execute_{tool_name}",
                context={
                    "risk_level": tool.risk_level.value,
                },
                now=timestamp,
            )
            escalation_request_id = escalation_record.request_id
            if escalation_record.level is EscalationLevel.L3 and not escalation_record.is_terminal:
                result = ToolExecutionResult(
                    task_id=task_id,
                    tool_name=tool_name,
                    input_payload=normalized_input,
                    output=None,
                    duration_seconds=0.0,
                    cost_usd=0.0,
                    status="blocked_escalation",
                    escalation_request_id=escalation_request_id,
                )
                self._emit_execution_result(task_id, tool, result, timestamp)
                return result

        started_at = time.perf_counter()
        output: object | None = None
        execution_ok = False
        try:
            output = tool.execute(**normalized_input)
            execution_ok = True
        finally:
            duration_seconds = max(0.0, time.perf_counter() - started_at)

        result = ToolExecutionResult(
            task_id=task_id,
            tool_name=tool_name,
            input_payload=normalized_input,
            output=output,
            duration_seconds=duration_seconds,
            cost_usd=_resolved_cost_usd(cost_usd, output),
            status=_coerce_status(execution_ok),
            escalation_request_id=escalation_request_id,
        )

        if self._budget_controller is not None:
            self._budget_controller.record_consumption(
                task_id,
                cost_usd=result.cost_usd,
                elapsed_seconds=duration_seconds,
                api_calls=1,
                now=timestamp,
                tool_name=tool_name,
            )

        self._emit_execution_result(task_id, tool, result, timestamp)
        return result

    def _emit_execution_result(
        self,
        task_id: str,
        tool: Tool,
        result: ToolExecutionResult,
        timestamp: datetime,
    ) -> None:
        event_type = EventType.step_completed if result.status == "succeeded" else EventType.error_occurred
        payload: dict[str, Any] = {
            "tool_name": tool.name,
            "input": result.input_payload,
            "output": result.output,
            "duration_seconds": result.duration_seconds,
            "cost_usd": result.cost_usd,
            "status": result.status,
            "escalation_request_id": result.escalation_request_id,
            "source": "tool_execution_runner",
        }
        self._emit_event(
            Event(
                task_id=task_id,
                event_type=event_type,
                timestamp=timestamp,
                payload=payload,
            )
        )

    def _emit_event(self, event: Event) -> None:
        if self._event_bus is not None:
            self._event_bus.publish_sync(event)
        if self._audit_log_writer is not None:
            self._audit_log_writer.append(event)


__all__ = ["ToolExecutionResult", "ToolExecutionRunner"]
