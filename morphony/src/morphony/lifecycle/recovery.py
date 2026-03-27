from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FailureClass(StrEnum):
    transient = "transient"
    permanent = "permanent"
    fatal = "fatal"


_TRANSIENT_BACKOFF_SECONDS: tuple[int, ...] = (1, 2, 4)


def _empty_str_list() -> list[str]:
    return []


def _empty_budget_delta() -> dict[str, float | int]:
    return {}


def transient_backoff_seconds(attempt: int) -> int | None:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if attempt <= len(_TRANSIENT_BACKOFF_SECONDS):
        return _TRANSIENT_BACKOFF_SECONDS[attempt - 1]
    return None


def promote_transient_failure(attempt: int) -> bool:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return attempt > len(_TRANSIENT_BACKOFF_SECONDS)


@dataclass(slots=True)
class RecoveryDecision:
    task_id: str
    step_id: str
    classification: FailureClass
    action: str
    checkpoint_path: str
    error_message: str
    attempt: int | None = None
    retry_delay_seconds: int | None = None
    skip_step: bool = False
    alternative_trial: bool = False
    promoted_to_permanent: bool = False
    preserve_partial_artifacts: bool = False
    l3_escalation: bool = False
    artifacts: list[str] = field(default_factory=_empty_str_list)


@dataclass(slots=True)
class ResumeDecision:
    task_id: str
    checkpoint_path: str
    checkpoint_version: int
    resume_after_step_id: str | None
    completed_steps: list[str] = field(default_factory=_empty_str_list)
    skipped_steps: list[str] = field(default_factory=_empty_str_list)
    partial_artifacts: list[str] = field(default_factory=_empty_str_list)
    budget_delta: dict[str, float | int] = field(default_factory=_empty_budget_delta)
    last_failed_step_id: str | None = None


__all__ = [
    "FailureClass",
    "RecoveryDecision",
    "ResumeDecision",
    "promote_transient_failure",
    "transient_backoff_seconds",
]
