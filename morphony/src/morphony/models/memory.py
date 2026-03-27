from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import TaskState


def _empty_str_list() -> list[str]:
    return []


def _empty_step_list() -> list[dict[str, Any]]:
    return []


def _empty_metadata() -> dict[str, Any]:
    return {}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EpisodicMemory(StrictModel):
    version: int = Field(default=1, ge=1)
    task_id: str
    goal: str
    plan: list[str] = Field(default_factory=_empty_str_list)
    steps: list[dict[str, Any]] = Field(default_factory=_empty_step_list)
    result: Any | None = None
    execution_state: TaskState
    metadata: dict[str, Any] = Field(default_factory=_empty_metadata)


class SemanticMemory(StrictModel):
    version: int = Field(default=1, ge=1)
    pattern_id: str
    category: str
    pattern: str
    conditions: list[str] = Field(default_factory=_empty_str_list)
    actions: list[str] = Field(default_factory=_empty_str_list)
    success_rate: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=_empty_metadata)


__all__ = ["StrictModel", "EpisodicMemory", "SemanticMemory"]
