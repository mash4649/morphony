from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from morphony.config import AgentConfig, BudgetConfig, BudgetDailyConfig, BudgetMonthlyConfig, BudgetTaskConfig
from morphony.events import AuditLogReader, AuditLogWriter, Event, EventBus, EventType
from morphony.models import EscalationLevel
from morphony.safety import BudgetController, EscalationEngine
from morphony.tools import ToolExecutionRunner, ToolRegistry


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@dataclass(slots=True)
class DummyTool:
    name: str
    description: str
    risk_level: EscalationLevel
    cost_per_call: Decimal
    is_reversible: bool = True
    valid: bool = True
    healthy: bool = True
    execute_calls: int = 0

    def execute(self, *args: object, **kwargs: object) -> object:
        self.execute_calls += 1
        return {"args": list(args), "kwargs": dict(kwargs), "tool": self.name}

    def validate(self, *args: object, **kwargs: object) -> bool:
        _ = args, kwargs
        return self.valid

    def health_check(self) -> bool:
        return self.healthy


def _small_budget(cost_limit: float) -> BudgetConfig:
    return BudgetConfig(
        task=BudgetTaskConfig(cost_usd=cost_limit, time_minutes=60, api_calls=100),
        daily=BudgetDailyConfig(cost_usd=cost_limit * 10, time_hours=24),
        monthly=BudgetMonthlyConfig(cost_usd=cost_limit * 100),
    )


def test_tool_registry_register_get_and_health_checks() -> None:
    registry = ToolRegistry()
    up_tool = DummyTool(
        name="web_search",
        description="search tool",
        risk_level=EscalationLevel.L1,
        cost_per_call=Decimal("0.1"),
        healthy=True,
    )
    down_tool = DummyTool(
        name="report_render",
        description="render tool",
        risk_level=EscalationLevel.L1,
        cost_per_call=Decimal("0.1"),
        healthy=False,
    )

    first = registry.register(up_tool, require_approval=False)
    second = registry.register(down_tool, require_approval=False)

    assert first.registered is True
    assert second.registered is True
    assert registry.get("web_search") is up_tool
    assert registry.list_tools() == ["report_render", "web_search"]

    statuses = registry.health_check_all()
    assert statuses["web_search"] is True
    assert statuses["report_render"] is False


def test_tool_registration_runs_l3_approval_flow() -> None:
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, events.append)
    escalation_engine = EscalationEngine(event_bus=bus)
    registry = ToolRegistry(escalation_engine=escalation_engine)
    tool = DummyTool(
        name="web_fetch",
        description="fetch tool",
        risk_level=EscalationLevel.L1,
        cost_per_call=Decimal("0.1"),
    )

    result = registry.register(
        tool,
        require_approval=True,
        auto_approve=False,
        task_id="task-tool-registration",
    )

    assert result.registered is False
    assert result.approval_required is True
    assert result.request_id is not None
    assert registry.is_registered("web_fetch") is False
    assert events
    payload = _as_dict(events[-1].payload)
    assert payload["level"] == EscalationLevel.L3.value

    registry.approve_registration("web_fetch", result.request_id)
    assert registry.is_registered("web_fetch") is True


def test_tool_execution_runner_applies_validation_budget_and_logging(tmp_path: Path) -> None:
    bus = EventBus()
    captured_events: list[Event] = []
    bus.subscribe_all(captured_events.append)
    escalation_engine = EscalationEngine(event_bus=bus)
    budget_controller = BudgetController(
        event_bus=bus,
        config=AgentConfig().budget,
    )
    registry = ToolRegistry(escalation_engine=escalation_engine)
    tool = DummyTool(
        name="web_search",
        description="search tool",
        risk_level=EscalationLevel.L1,
        cost_per_call=Decimal("0.25"),
        valid=True,
    )
    registry.register(tool, require_approval=False)

    audit_log = tmp_path / "audit.log"
    runner = ToolExecutionRunner(
        registry,
        event_bus=bus,
        audit_log_writer=AuditLogWriter(audit_log),
        budget_controller=budget_controller,
        escalation_engine=escalation_engine,
    )
    result = runner.execute(
        "task-tools-exec",
        "web_search",
        tool_input={"query": "morphony"},
        now=datetime(2026, 3, 26, 10, 0, tzinfo=UTC),
    )

    assert result.status == "succeeded"
    assert result.cost_usd == 0.25
    assert result.duration_seconds >= 0.0
    assert tool.execute_calls == 1
    assert any(event.event_type == EventType.step_started for event in captured_events)
    assert any(event.event_type == EventType.step_completed for event in captured_events)
    assert any(event.event_type == EventType.budget_consumed for event in captured_events)

    audit_events = AuditLogReader(audit_log).iter_events(task_id="task-tools-exec")
    assert any(event.event_type == EventType.step_completed for event in audit_events)


def test_tool_execution_runner_blocks_when_budget_would_stop() -> None:
    bus = EventBus()
    escalation_engine = EscalationEngine(event_bus=bus)
    budget_controller = BudgetController(
        event_bus=bus,
        config=_small_budget(0.05),
    )
    registry = ToolRegistry(escalation_engine=escalation_engine)
    tool = DummyTool(
        name="web_search",
        description="search tool",
        risk_level=EscalationLevel.L1,
        cost_per_call=Decimal("1.0"),
        valid=True,
    )
    registry.register(tool, require_approval=False)

    runner = ToolExecutionRunner(
        registry,
        event_bus=bus,
        budget_controller=budget_controller,
        escalation_engine=escalation_engine,
    )
    result = runner.execute(
        "task-tools-budget-block",
        "web_search",
        tool_input={"query": "too expensive"},
        now=datetime(2026, 3, 26, 10, 10, tzinfo=UTC),
    )

    assert result.status == "blocked_budget"
    assert result.output is None
    assert result.cost_usd == 0.0
    assert tool.execute_calls == 0

