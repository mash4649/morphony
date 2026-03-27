from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    pending = "pending"
    planning = "planning"
    approved = "approved"
    running = "running"
    paused = "paused"
    suspended = "suspended"
    completed = "completed"
    failed = "failed"
    stopped = "stopped"


class AutonomyLevel(StrEnum):
    plan_only = "plan_only"
    supervised = "supervised"
    autonomous = "autonomous"


class EscalationLevel(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


__all__ = ["TaskState", "AutonomyLevel", "EscalationLevel"]
