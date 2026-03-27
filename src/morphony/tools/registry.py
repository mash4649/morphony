from __future__ import annotations

from dataclasses import dataclass

from morphony.models import Tool
from morphony.safety import EscalationEngine


@dataclass(slots=True)
class ToolRegistrationResult:
    tool_name: str
    registered: bool
    approval_required: bool
    request_id: str | None = None


class ToolRegistry:
    def __init__(
        self,
        escalation_engine: EscalationEngine | None = None,
    ) -> None:
        self._escalation_engine = escalation_engine
        self._tools: dict[str, Tool] = {}
        self._pending: dict[str, tuple[str, Tool]] = {}

    def register(
        self,
        tool: Tool,
        *,
        require_approval: bool = True,
        auto_approve: bool = False,
        task_id: str = "tool_registry",
    ) -> ToolRegistrationResult:
        name = self._ensure_tool_name(tool)
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        if name in self._pending:
            raise ValueError(f"Tool registration is already pending approval: {name}")

        if not require_approval:
            self._tools[name] = tool
            return ToolRegistrationResult(
                tool_name=name,
                registered=True,
                approval_required=False,
            )

        if self._escalation_engine is None:
            raise ValueError("escalation_engine is required when require_approval=True")

        record = self._escalation_engine.request_escalation(
            task_id=task_id,
            action_name="register_tool",
            context={
                "tool_name": name,
                "escalation_level": "L3",
                "requires_approval": True,
            },
        )
        self._pending[name] = (record.request_id, tool)

        if auto_approve:
            self.approve_registration(name, record.request_id)
            return ToolRegistrationResult(
                tool_name=name,
                registered=True,
                approval_required=True,
                request_id=record.request_id,
            )

        return ToolRegistrationResult(
            tool_name=name,
            registered=False,
            approval_required=True,
            request_id=record.request_id,
        )

    def approve_registration(self, tool_name: str, request_id: str) -> None:
        if self._escalation_engine is None:
            raise ValueError("escalation_engine is required to approve registration")
        pending = self._pending.get(tool_name)
        if pending is None:
            raise KeyError(f"No pending registration for tool: {tool_name}")
        pending_request_id, tool = pending
        if pending_request_id != request_id:
            raise ValueError(f"Request id mismatch for tool '{tool_name}'")
        self._escalation_engine.approve(request_id)
        self._tools[tool_name] = tool
        del self._pending[tool_name]

    def reject_registration(self, tool_name: str, reason: str) -> None:
        if not reason:
            raise ValueError("reason must not be empty")
        if self._escalation_engine is None:
            raise ValueError("escalation_engine is required to reject registration")
        pending = self._pending.get(tool_name)
        if pending is None:
            raise KeyError(f"No pending registration for tool: {tool_name}")
        request_id, _tool = pending
        self._escalation_engine.reject(request_id, reason)
        del self._pending[tool_name]

    def get(self, tool_name: str) -> Tool:
        try:
            return self._tools[tool_name]
        except KeyError as exc:
            raise KeyError(f"Tool not registered: {tool_name}") from exc

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def health_check_all(self) -> dict[str, bool]:
        statuses: dict[str, bool] = {}
        for tool_name in self.list_tools():
            tool = self._tools[tool_name]
            try:
                statuses[tool_name] = bool(tool.health_check())
            except Exception:
                statuses[tool_name] = False
        return statuses

    def _ensure_tool_name(self, tool: Tool) -> str:
        name = tool.name
        if not name:
            raise ValueError("tool.name must be a non-empty string")
        return name


__all__ = ["ToolRegistrationResult", "ToolRegistry"]
