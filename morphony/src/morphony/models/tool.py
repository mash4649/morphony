from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from .enums import EscalationLevel


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    risk_level: EscalationLevel
    cost_per_call: Decimal
    is_reversible: bool

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def validate(self, *args: Any, **kwargs: Any) -> bool:
        ...

    def health_check(self) -> bool:
        ...


__all__ = ["Tool"]
