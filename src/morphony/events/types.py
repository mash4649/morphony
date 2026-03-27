from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from morphony.models.memory import StrictModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _empty_payload() -> dict[str, Any]:
    return {}


class EventType(StrEnum):
    task_started = "task_started"
    task_completed = "task_completed"
    step_started = "step_started"
    step_completed = "step_completed"
    escalation_triggered = "escalation_triggered"
    conflict_detected = "conflict_detected"
    error_occurred = "error_occurred"
    budget_consumed = "budget_consumed"
    checkpoint_saved = "checkpoint_saved"
    state_changed = "state_changed"


class Event(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str
    event_type: EventType
    timestamp: datetime = Field(default_factory=_utc_now)
    payload: dict[str, Any] = Field(default_factory=_empty_payload)

    @field_validator("timestamp")
    @classmethod
    def _ensure_timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        return value.astimezone(UTC)


__all__ = ["Event", "EventType"]
