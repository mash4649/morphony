from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .memory import StrictModel


class TrustScore(StrictModel):
    category: str
    score: float = Field(ge=0.0, le=1.0)
    task_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    avg_owner_rating: float | None = Field(default=None, ge=0.0)
    last_updated: datetime


__all__ = ["TrustScore"]
