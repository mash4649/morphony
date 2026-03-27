from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from morphony.events import AuditLogWriter, Event, EventBus, EventType
from morphony.lifecycle.state_machine import TERMINAL_STATES
from morphony.models import EscalationLevel, TaskState

if TYPE_CHECKING:
    from morphony.lifecycle import CheckpointManager, TaskLifecycleManager

logger = logging.getLogger(__name__)

_ACTION_WINDOW_SIZE = 5


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


def _ensure_non_empty(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _normalize_artifacts(artifacts: list[str] | None) -> list[str]:
    if artifacts is None:
        return []
    normalized: list[str] = []
    for artifact in artifacts:
        normalized.append(artifact)
    return normalized


def _step_id_for_reason(reason: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", reason).strip("._-")
    if not normalized:
        normalized = "owner_kill_switch"
    return f"safety_stop_{normalized}"


def _is_event_bus(value: object) -> bool:
    return isinstance(value, EventBus)


def _event_bus_from_lifecycle_manager(
    lifecycle_manager: "TaskLifecycleManager",
) -> EventBus | None:
    lifecycle_bus = getattr(lifecycle_manager, "_event_bus", None)
    if _is_event_bus(lifecycle_bus):
        return lifecycle_bus
    return None


def _empty_recent_costs() -> list[float]:
    return []


@dataclass(slots=True)
class _TaskActionWindow:
    recent_costs: list[float] = field(default_factory=_empty_recent_costs)
    repeat_signature: tuple[str, str] | None = None
    repeat_count: int = 0


class SafetyController:
    def __init__(
        self,
        event_bus: EventBus,
        lifecycle_manager: "TaskLifecycleManager",
        checkpoint_manager: "CheckpointManager | None" = None,
        audit_log_writer: AuditLogWriter | None = None,
        cost_spike_multiplier: float = 3.0,
        repeat_threshold: int = 5,
    ) -> None:
        if cost_spike_multiplier <= 0:
            raise ValueError("cost_spike_multiplier must be greater than 0")
        if repeat_threshold < 1:
            raise ValueError("repeat_threshold must be greater than 0")

        self._event_bus = event_bus
        self._lifecycle_manager = lifecycle_manager
        self._checkpoint_manager = checkpoint_manager
        self._audit_log_writer = audit_log_writer
        self._cost_spike_multiplier = cost_spike_multiplier
        self._repeat_threshold = repeat_threshold
        self._action_windows: dict[str, _TaskActionWindow] = {}

    def stop_task(
        self,
        task_id: str,
        reason: str = "owner_kill_switch",
        artifacts: list[str] | None = None,
    ) -> None:
        task_id = _ensure_non_empty("task_id", task_id)
        reason = _ensure_non_empty("reason", reason)
        current_state = self._lifecycle_manager.get_task_state(task_id)
        if current_state in TERMINAL_STATES:
            self._action_windows.pop(task_id, None)
            return

        current_time = _utc_now()
        normalized_artifacts = _normalize_artifacts(artifacts)
        stop_event = self._build_stop_event(
            task_id=task_id,
            current_state=current_state,
            reason=reason,
            artifacts=normalized_artifacts,
            timestamp=current_time,
        )

        self._save_checkpoint_best_effort(
            task_id=task_id,
            reason=reason,
            artifacts=normalized_artifacts,
        )
        self._transition_to_stopped_best_effort(task_id=task_id)
        self._publish_stop_event_best_effort(stop_event)
        self._action_windows.pop(task_id, None)

    def record_action(
        self,
        task_id: str,
        tool_name: str,
        tool_input: str,
        cost: float | int,
        now: datetime | None = None,
    ) -> None:
        task_id = _ensure_non_empty("task_id", task_id)
        tool_name = _ensure_non_empty("tool_name", tool_name)
        if isinstance(cost, bool):
            raise TypeError("cost must be a number")
        if cost < 0:
            raise ValueError("cost must not be negative")

        current_state = self._lifecycle_manager.get_task_state(task_id)
        if current_state in TERMINAL_STATES:
            return

        current_time = _coerce_now(now)
        action_window = self._action_windows.setdefault(task_id, _TaskActionWindow())
        action_signature = (tool_name, tool_input)

        repeat_count = 1
        if action_window.repeat_signature == action_signature:
            repeat_count = action_window.repeat_count + 1
        action_window.repeat_signature = action_signature
        action_window.repeat_count = repeat_count

        previous_costs = action_window.recent_costs[-_ACTION_WINDOW_SIZE:]
        action_window.recent_costs.append(float(cost))
        if len(action_window.recent_costs) > _ACTION_WINDOW_SIZE:
            action_window.recent_costs = action_window.recent_costs[-_ACTION_WINDOW_SIZE:]

        anomaly_types: list[str] = []
        cost_window_average = 0.0
        if len(previous_costs) == _ACTION_WINDOW_SIZE:
            cost_window_average = sum(previous_costs) / float(_ACTION_WINDOW_SIZE)
            if float(cost) > cost_window_average * self._cost_spike_multiplier:
                anomaly_types.append("cost_spike")

        if repeat_count >= self._repeat_threshold:
            anomaly_types.append("repeat_loop")

        if not anomaly_types:
            return

        anomaly_reason = self._format_anomaly_reason(anomaly_types)
        escalation_event = self._build_escalation_event(
            task_id=task_id,
            reason=anomaly_reason,
            anomaly_types=anomaly_types,
            tool_name=tool_name,
            tool_input=tool_input,
            cost=float(cost),
            current_state=current_state,
            repeat_count=repeat_count,
            cost_window_average=cost_window_average,
            timestamp=current_time,
        )
        self.stop_task(task_id, reason=anomaly_reason)
        self._publish_escalation_event_best_effort(escalation_event)

    def _transition_to_stopped_best_effort(self, task_id: str) -> None:
        try:
            self._lifecycle_manager.transition(task_id, TaskState.stopped)
        except Exception as exc:
            try:
                current_state = self._lifecycle_manager.get_task_state(task_id)
            except KeyError:
                raise
            if current_state != TaskState.stopped:
                raise
            logger.warning("Task %s stopped, but state change event handling failed: %s", task_id, exc)

    def _save_checkpoint_best_effort(
        self,
        *,
        task_id: str,
        reason: str,
        artifacts: list[str],
    ) -> None:
        if self._checkpoint_manager is None:
            return

        step_id = _step_id_for_reason(reason)
        try:
            self._checkpoint_manager.save_step_completion(
                task_id,
                step_id,
                artifacts=artifacts if artifacts else None,
            )
        except Exception as exc:
            logger.warning("Failed to preserve checkpoint for task %s: %s", task_id, exc)

    def _publish_stop_event_best_effort(self, event: Event) -> None:
        lifecycle_bus = _event_bus_from_lifecycle_manager(self._lifecycle_manager)
        if lifecycle_bus is not self._event_bus:
            self._publish_event_best_effort(event)
        self._write_audit_log_best_effort(event)

    def _publish_escalation_event_best_effort(self, event: Event) -> None:
        self._publish_event_best_effort(event)
        self._write_audit_log_best_effort(event)

    def _publish_event_best_effort(self, event: Event) -> None:
        try:
            self._event_bus.publish_sync(event)
        except Exception as exc:
            logger.warning("Failed to publish %s event for task %s: %s", event.event_type.value, event.task_id, exc)

    def _write_audit_log_best_effort(self, event: Event) -> None:
        if self._audit_log_writer is None:
            return
        try:
            self._audit_log_writer.append(event)
        except Exception as exc:
            logger.warning("Failed to append audit log for task %s: %s", event.task_id, exc)

    def _build_stop_event(
        self,
        *,
        task_id: str,
        current_state: TaskState,
        reason: str,
        artifacts: list[str],
        timestamp: datetime,
    ) -> Event:
        return Event(
            task_id=task_id,
            event_type=EventType.state_changed,
            timestamp=timestamp,
            payload={
                "source": "safety_controller",
                "reason": reason,
                "from_state": current_state.value,
                "to_state": TaskState.stopped.value,
                "artifacts": list(artifacts),
            },
        )

    def _build_escalation_event(
        self,
        *,
        task_id: str,
        reason: str,
        anomaly_types: list[str],
        tool_name: str,
        tool_input: str,
        cost: float,
        current_state: TaskState,
        repeat_count: int,
        cost_window_average: float,
        timestamp: datetime,
    ) -> Event:
        payload: dict[str, object] = {
            "source": "safety_controller",
            "reason": reason,
            "level": EscalationLevel.L3.value,
            "anomaly_types": list(anomaly_types),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cost": cost,
            "task_state": current_state.value,
            "repeat_count": repeat_count,
        }
        if len(anomaly_types) == 1 and anomaly_types[0] == "cost_spike":
            payload["cost_window_average"] = cost_window_average
            payload["cost_spike_multiplier"] = self._cost_spike_multiplier
        elif "cost_spike" in anomaly_types:
            payload["cost_window_average"] = cost_window_average
            payload["cost_spike_multiplier"] = self._cost_spike_multiplier
        if "repeat_loop" in anomaly_types:
            payload["repeat_threshold"] = self._repeat_threshold

        return Event(
            task_id=task_id,
            event_type=EventType.escalation_triggered,
            timestamp=timestamp,
            payload=payload,
        )

    def _format_anomaly_reason(self, anomaly_types: list[str]) -> str:
        ordered_types: list[str] = []
        for anomaly_type in anomaly_types:
            if anomaly_type not in ordered_types:
                ordered_types.append(anomaly_type)
        return "auto_stop_" + "_and_".join(ordered_types)


__all__ = ["SafetyController"]
