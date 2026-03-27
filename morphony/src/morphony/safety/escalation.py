from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from morphony.config import EscalationConfig
from morphony.events import Event, EventBus, EventType
from morphony.lifecycle import InvalidTransitionError
from morphony.models import EscalationLevel, TaskState

if TYPE_CHECKING:
    from morphony.lifecycle import CheckpointManager, TaskLifecycleManager


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


def _normalize_context(
    context: Mapping[str, object] | None,
) -> dict[str, object]:
    if context is None:
        return {}
    normalized: dict[str, object] = {}
    for key, value in context.items():
        normalized[key] = value
    return normalized


def _empty_context() -> dict[str, object]:
    return {}


def _text_from_context(context: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key, value in context.items():
        parts.append(f"{key}={value}")
    return " ".join(parts).lower()


def _text_has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _level_rank(level: EscalationLevel) -> int:
    if level is EscalationLevel.L1:
        return 1
    if level is EscalationLevel.L2:
        return 2
    return 3


def _max_level(*levels: EscalationLevel) -> EscalationLevel:
    return max(levels, key=_level_rank)


class EscalationRequestStatus(StrEnum):
    auto_proceed = "auto_proceed"
    notified = "notified"
    waiting_approval = "waiting_approval"
    approved = "approved"
    rejected = "rejected"
    paused = "paused"
    suspended = "suspended"


@dataclass(slots=True)
class EscalationRecord:
    request_id: str
    task_id: str
    action_name: str
    level: EscalationLevel
    status: EscalationRequestStatus
    created_at: datetime
    updated_at: datetime
    context: dict[str, object] = field(default_factory=_empty_context)
    timeout_at: datetime | None = None
    reminder_at: datetime | None = None
    auto_suspend_at: datetime | None = None
    policy: str | None = None
    reason: str | None = None
    resolved_at: datetime | None = None
    reminder_sent_at: datetime | None = None
    suspension_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            EscalationRequestStatus.auto_proceed,
            EscalationRequestStatus.approved,
            EscalationRequestStatus.rejected,
            EscalationRequestStatus.paused,
            EscalationRequestStatus.suspended,
        }


class EscalationEngine:
    def __init__(
        self,
        event_bus: EventBus,
        lifecycle_manager: "TaskLifecycleManager | None" = None,
        checkpoint_manager: "CheckpointManager | None" = None,
        config: EscalationConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._lifecycle_manager = lifecycle_manager
        self._checkpoint_manager = checkpoint_manager
        self._config = config if config is not None else EscalationConfig()
        self._records: dict[str, EscalationRecord] = {}

    def classify_action(
        self,
        action_name: str,
        context: dict[str, object] | None = None,
    ) -> EscalationLevel:
        normalized_action = action_name.strip().lower()
        normalized_context = _normalize_context(context)
        context_text = _text_from_context(normalized_context)

        explicit_level = self._explicit_level(normalized_context)
        inferred_level = self._infer_level(normalized_action, context_text, normalized_context)
        if explicit_level is None:
            return inferred_level
        return _max_level(explicit_level, inferred_level)

    def request_escalation(
        self,
        task_id: str,
        action_name: str,
        context: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> EscalationRecord:
        if not task_id:
            raise ValueError("task_id must not be empty")
        if not action_name:
            raise ValueError("action_name must not be empty")

        current_time = _coerce_now(now)
        normalized_context = _normalize_context(context)
        level = self.classify_action(action_name, normalized_context)
        request_id = uuid4().hex

        record = EscalationRecord(
            request_id=request_id,
            task_id=task_id,
            action_name=action_name,
            level=level,
            status=EscalationRequestStatus.auto_proceed
            if level is EscalationLevel.L1
            else (
                EscalationRequestStatus.notified
                if level is EscalationLevel.L2
                else EscalationRequestStatus.waiting_approval
            ),
            created_at=current_time,
            updated_at=current_time,
            context=normalized_context,
        )

        if level is EscalationLevel.L2:
            record.timeout_at = current_time + timedelta(
                minutes=self._config.l2_timeout_minutes,
            )
            record.policy = self._config.l2_timeout_policy
        elif level is EscalationLevel.L3:
            record.reminder_at = current_time + timedelta(
                minutes=self._config.l3_reminder_minutes,
            )
            record.auto_suspend_at = current_time + timedelta(
                hours=self._config.l3_auto_suspend_hours,
            )

        if level is EscalationLevel.L1:
            record.resolved_at = current_time
        self._records[request_id] = record
        self._publish_escalation_event(
            record,
            current_time,
            phase="requested",
            reason="classification",
        )
        return record

    def process_timeouts(self, now: datetime | None = None) -> list[EscalationRecord]:
        current_time = _coerce_now(now)
        updated_records: list[EscalationRecord] = []

        for record in sorted(
            self._records.values(),
            key=lambda item: (item.created_at, item.request_id),
        ):
            if record.is_terminal:
                continue

            changed = False

            if (
                record.level is EscalationLevel.L2
                and record.status is EscalationRequestStatus.notified
                and record.timeout_at is not None
                and current_time >= record.timeout_at
            ):
                changed = True
                policy = record.policy or self._config.l2_timeout_policy
                if policy == "auto_proceed":
                    record.status = EscalationRequestStatus.auto_proceed
                    record.resolved_at = current_time
                    record.reason = "l2_timeout_auto_proceed"
                elif policy == "pause":
                    record.status = EscalationRequestStatus.paused
                    record.resolved_at = current_time
                    record.reason = "l2_timeout_pause"
                    self._best_effort_transition(record.task_id, TaskState.paused)
                else:
                    record.level = EscalationLevel.L3
                    record.status = EscalationRequestStatus.waiting_approval
                    record.timeout_at = None
                    record.policy = None
                    record.reason = "l2_timeout_escalate"
                    record.reminder_at = current_time + timedelta(
                        minutes=self._config.l3_reminder_minutes,
                    )
                    record.auto_suspend_at = current_time + timedelta(
                        hours=self._config.l3_auto_suspend_hours,
                    )
                    record.updated_at = current_time
                    self._publish_escalation_event(
                        record,
                        current_time,
                        phase="l2_timeout",
                        reason="policy_escalate",
                    )
                    updated_records.append(record)
                    continue

                record.updated_at = current_time
                self._publish_escalation_event(
                    record,
                    current_time,
                    phase="l2_timeout",
                    reason=record.reason,
                    policy=policy,
                )
                updated_records.append(record)
                continue

            if (
                record.level is EscalationLevel.L3
                and record.status is EscalationRequestStatus.waiting_approval
            ):
                if (
                    record.reminder_at is not None
                    and record.reminder_sent_at is None
                    and current_time >= record.reminder_at
                ):
                    changed = True
                    record.reminder_sent_at = current_time
                    self._publish_escalation_event(
                        record,
                        current_time,
                        phase="l3_reminder",
                        reason="reminder_due",
                    )

                if (
                    record.auto_suspend_at is not None
                    and current_time >= record.auto_suspend_at
                ):
                    changed = True
                    record.status = EscalationRequestStatus.suspended
                    record.suspension_at = current_time
                    record.resolved_at = current_time
                    record.reason = "l3_auto_suspend"
                    self._preserve_partial_artifacts(record)
                    self._best_effort_transition(record.task_id, TaskState.suspended)
                    self._publish_escalation_event(
                        record,
                        current_time,
                        phase="l3_auto_suspend",
                        reason="approval_timeout",
                    )

            if changed:
                record.updated_at = current_time
                updated_records.append(record)

        return updated_records

    def approve(self, request_id: str) -> EscalationRecord:
        record = self._get_record(request_id)
        if record.is_terminal and record.status not in {
            EscalationRequestStatus.paused,
            EscalationRequestStatus.suspended,
        }:
            return record

        current_time = _utc_now()
        record.status = EscalationRequestStatus.approved
        record.updated_at = current_time
        record.resolved_at = current_time
        record.reason = "approved"
        self._best_effort_transition(record.task_id, TaskState.running)
        return record

    def reject(self, request_id: str, reason: str) -> EscalationRecord:
        if not reason:
            raise ValueError("reason must not be empty")
        record = self._get_record(request_id)
        current_time = _utc_now()
        record.status = EscalationRequestStatus.rejected
        record.updated_at = current_time
        record.resolved_at = current_time
        record.reason = reason
        self._best_effort_transition(record.task_id, TaskState.paused)
        return record

    def _explicit_level(
        self,
        context: Mapping[str, object],
    ) -> EscalationLevel | None:
        seen_explicit_key = False
        for key in ("escalation_level", "risk_level"):
            raw_value = context.get(key)
            if raw_value is None:
                continue
            seen_explicit_key = True
            if isinstance(raw_value, EscalationLevel):
                return raw_value
            if isinstance(raw_value, int) and not isinstance(raw_value, bool) and 1 <= raw_value <= 3:
                return EscalationLevel(f"L{raw_value}")
            if isinstance(raw_value, str):
                normalized = raw_value.strip().upper()
                if normalized in EscalationLevel.__members__:
                    return EscalationLevel[normalized]
                if normalized in {"1", "L1"}:
                    return EscalationLevel.L1
                if normalized in {"2", "L2"}:
                    return EscalationLevel.L2
                if normalized in {"3", "L3"}:
                    return EscalationLevel.L3
        return EscalationLevel.L3 if seen_explicit_key else None

    def _infer_level(
        self,
        action_name: str,
        context_text: str,
        context: Mapping[str, object],
    ) -> EscalationLevel:
        if self._has_l3_signal(action_name, context_text, context):
            return EscalationLevel.L3
        if self._has_l2_signal(action_name, context_text, context):
            return EscalationLevel.L2
        if self._has_l1_signal(action_name, context_text, context):
            return EscalationLevel.L1
        return EscalationLevel.L3

    def _has_l1_signal(
        self,
        action_name: str,
        context_text: str,
        context: Mapping[str, object],
    ) -> bool:
        low_risk_actions = (
            "web_search",
            "web fetch",
            "web_fetch",
            "browse_web",
            "fetch_web",
            "read_web",
            "open_web",
            "search_web",
            "search",
            "lookup",
            "llm_analyze",
            "analyze",
            "summarize",
            "draft_report",
            "report_render",
            "extract",
            "parse",
            "fact_check",
        )
        low_risk_context = (
            "web_search",
            "web_fetch",
            "search",
            "fetch",
            "browse",
            "read",
            "analyze",
            "summarize",
            "draft",
            "report_render",
        )

        if _text_has_any(action_name, low_risk_actions):
            return True
        if _text_has_any(context_text, low_risk_context):
            return True

        scope = context.get("scope")
        if isinstance(scope, str) and scope.lower() in {"draft", "research", "search"}:
            return True

        return False

    def _has_l2_signal(
        self,
        action_name: str,
        context_text: str,
        context: Mapping[str, object],
    ) -> bool:
        medium_risk_actions = (
            "adjust_plan",
            "change_plan",
            "expand_scope",
            "broaden_search",
            "widen_search",
            "follow_up_search",
            "notify",
            "review",
            "api_call",
            "external_api",
            "call_api",
            "request_review",
        )
        medium_risk_context = (
            "expand scope",
            "scope expansion",
            "broaden",
            "widen",
            "external api",
            "api call",
            "notify",
            "notification",
            "review",
            "needs review",
            "timeout",
            "policy",
            "escalate",
        )

        if _text_has_any(action_name, medium_risk_actions):
            return True
        if _text_has_any(context_text, medium_risk_context):
            return True

        if self._context_flag(context, "needs_review", "requires_review", "scope_expansion"):
            return True

        return False

    def _has_l3_signal(
        self,
        action_name: str,
        context_text: str,
        context: Mapping[str, object],
    ) -> bool:
        high_risk_actions = (
            "final_report",
            "finalize_report",
            "finalise_report",
            "finalize",
            "finalise",
            "publish_report",
            "publish",
            "submit_report",
            "submit",
            "deliver_report",
            "deliver",
            "approve_report",
            "approve",
            "confirm_report",
            "confirm",
            "sign_off",
            "handoff",
            "release",
            "commit",
            "merge",
            "deploy",
            "export_final",
            "write_final",
            "lock",
            "close",
            "send",
        )
        high_risk_context = (
            "final report",
            "final",
            "finalized",
            "finalised",
            "publish",
            "published",
            "submit",
            "submitted",
            "deliver",
            "approved",
            "approval required",
            "requires approval",
            "irreversible",
            "non reversible",
            "external side effect",
            "side effect",
            "public release",
            "production",
            "secret",
            "confidential",
            "pii",
            "hand off",
        )

        if _text_has_any(action_name, high_risk_actions):
            return True
        if _text_has_any(context_text, high_risk_context):
            return True

        if self._context_flag(
            context,
            "is_final",
            "final",
            "finalized",
            "finalised",
            "approved",
            "approval_required",
            "requires_approval",
            "irreversible",
            "external_side_effect",
            "publish",
            "submit",
            "deliver",
            "handoff",
            "secret",
            "confidential",
            "pii",
        ):
            return True

        return False

    def _context_flag(self, context: Mapping[str, object], *keys: str) -> bool:
        for key in keys:
            value = context.get(key)
            if isinstance(value, bool):
                if value:
                    return True
                continue
            if isinstance(value, str) and value.strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "l3",
                "high",
                "final",
                "approved",
                "required",
                "required_for_final",
            }:
                return True
        return False

    def _get_record(self, request_id: str) -> EscalationRecord:
        try:
            return self._records[request_id]
        except KeyError as exc:
            raise KeyError(f"Unknown escalation request: {request_id}") from exc

    def _best_effort_transition(self, task_id: str, to_state: TaskState) -> None:
        if self._lifecycle_manager is None:
            return

        try:
            current_state = self._lifecycle_manager.get_task_state(task_id)
        except KeyError:
            return

        if current_state == to_state:
            return

        try:
            self._lifecycle_manager.transition(task_id, to_state)
        except (InvalidTransitionError, KeyError):
            return

    def _checkpoint_path(self, task_id: str) -> str | None:
        if self._checkpoint_manager is None:
            return None
        try:
            return str(self._checkpoint_manager.checkpoint_file_for_task(task_id))
        except Exception:
            return None

    def _publish_escalation_event(
        self,
        record: EscalationRecord,
        timestamp: datetime,
        *,
        phase: str,
        reason: str | None,
        policy: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": record.request_id,
            "action_name": record.action_name,
            "level": record.level.value,
            "status": record.status.value,
            "phase": phase,
            "reason": reason,
            "policy": policy or record.policy,
            "context": dict(record.context),
            "timeout_at": record.timeout_at,
            "reminder_at": record.reminder_at,
            "auto_suspend_at": record.auto_suspend_at,
        }
        checkpoint_path = self._checkpoint_path(record.task_id)
        if checkpoint_path is not None:
            payload["checkpoint_path"] = checkpoint_path

        self._event_bus.publish_sync(
            Event(
                task_id=record.task_id,
                event_type=EventType.escalation_triggered,
                timestamp=timestamp,
                payload=payload,
            )
        )

    def _preserve_partial_artifacts(self, record: EscalationRecord) -> None:
        if self._checkpoint_manager is None:
            return

        raw_artifacts = record.context.get("partial_artifacts")
        artifacts: list[str] = []
        if isinstance(raw_artifacts, list):
            for item in cast(list[object], raw_artifacts):
                if isinstance(item, str):
                    artifacts.append(item)
        elif isinstance(raw_artifacts, str):
            artifacts.append(raw_artifacts)

        try:
            self._checkpoint_manager.save_step_completion(
                record.task_id,
                f"l3_auto_suspend_{record.request_id}",
                artifacts=artifacts if artifacts else None,
            )
        except Exception:
            return


__all__ = [
    "EscalationEngine",
    "EscalationRecord",
    "EscalationRequestStatus",
]
