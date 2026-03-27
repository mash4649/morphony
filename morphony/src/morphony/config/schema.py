from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from morphony.models import AutonomyLevel


class _ConfigBase(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EscalationConfig(_ConfigBase):
    l2_timeout_minutes: int = Field(default=15, gt=0)
    l2_timeout_policy: Literal["auto_proceed", "escalate", "pause"] = "escalate"
    l3_reminder_minutes: int = Field(default=30, gt=0)
    l3_auto_suspend_hours: int = Field(default=24, gt=0)


class BudgetTaskConfig(_ConfigBase):
    cost_usd: float = Field(default=5.0, ge=0.0)
    time_minutes: int = Field(default=30, gt=0)
    api_calls: int = Field(default=100, gt=0)


class BudgetDailyConfig(_ConfigBase):
    cost_usd: float = Field(default=20.0, ge=0.0)
    time_hours: float = Field(default=4.0, gt=0.0)


class BudgetMonthlyConfig(_ConfigBase):
    cost_usd: float = Field(default=200.0, ge=0.0)


class BudgetConfig(_ConfigBase):
    task: BudgetTaskConfig = Field(default_factory=BudgetTaskConfig)
    daily: BudgetDailyConfig = Field(default_factory=BudgetDailyConfig)
    monthly: BudgetMonthlyConfig = Field(default_factory=BudgetMonthlyConfig)


class ImprovementConfig(_ConfigBase):
    max_iterations: int = Field(default=3, gt=0)
    trigger_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    completion_threshold: float = Field(default=0.9, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> "ImprovementConfig":
        if self.completion_threshold < self.trigger_threshold:
            raise ValueError("improvement.completion_threshold must be >= trigger_threshold")
        return self


class MemoryConfig(_ConfigBase):
    hot_episodes: int = Field(default=3, gt=0)
    semantic_max_per_category: int = Field(default=50, gt=0)
    inactive_threshold_days: int = Field(default=90, gt=0)


class SafetyConfig(_ConfigBase):
    sandbox_enabled: bool = True
    kill_switch_enabled: bool = True


class AgentConfig(_ConfigBase):
    autonomy_level: AutonomyLevel = Field(default=AutonomyLevel.supervised)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    improvement: ImprovementConfig = Field(default_factory=ImprovementConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
