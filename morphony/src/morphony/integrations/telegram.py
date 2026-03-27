from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from morphony.config import AutonomyLevel, load_config
from morphony.lifecycle import TaskLifecycleManager
from morphony.models import TaskState


_MAX_MESSAGE_LENGTH = 3900


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_command(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, stripped
    first_line = stripped.splitlines()[0]
    match = re.match(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", first_line)
    if not match:
        return None, stripped
    command = match.group(1).casefold()
    args = match.group(2) or ""
    return command, args.strip()


def _split_message(text: str, limit: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0

    for line in text.splitlines():
        line_with_newline = f"{line}\n"
        line_length = len(line_with_newline)
        if current_lines and current_length + line_length > limit:
            chunks.append("".join(current_lines).rstrip("\n"))
            current_lines = [line_with_newline]
            current_length = line_length
            continue
        current_lines.append(line_with_newline)
        current_length += line_length

    if current_lines:
        chunks.append("".join(current_lines).rstrip("\n"))

    return chunks


class TelegramBridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    token: str = Field(min_length=1)
    allowed_chat_ids: list[int] = Field(default_factory=list)
    api_base_url: str = Field(default="https://api.telegram.org")
    poll_timeout_seconds: int = Field(default=20, ge=0, le=60)
    request_timeout_seconds: float = Field(default=30.0, gt=0.0)
    lifecycle_store: Path = Field(default=Path("runtime/lifecycle.json"))
    task_registry: Path = Field(default=Path("runtime/tasks.json"))
    config_file: Path | None = None
    bot_display_name: str = Field(default="Morphony")

    @model_validator(mode="after")
    def _validate(self) -> "TelegramBridgeConfig":
        if not self.token.strip():
            raise ValueError("token must not be empty")
        if not self.api_base_url.strip():
            raise ValueError("api_base_url must not be empty")
        if not (
            self.api_base_url.startswith("http://")
            or self.api_base_url.startswith("https://")
        ):
            raise ValueError("api_base_url must start with http:// or https://")
        return self


@dataclass(slots=True)
class TelegramInboundMessage:
    update_id: int
    chat_id: int
    message_id: int
    text: str
    sender_id: int | None = None
    username: str | None = None
    command: str | None = None
    arguments: str = ""


def _load_task_registry(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        return {}

    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Invalid task registry at {path}: expected JSON object")

    registry: dict[str, dict[str, object]] = {}
    for raw_task_id, raw_payload in parsed.items():
        if not isinstance(raw_task_id, str) or not isinstance(raw_payload, dict):
            continue
        registry[raw_task_id] = raw_payload
    return registry


def _save_task_registry(path: Path, registry: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(f"{serialized}\n", encoding="utf-8")


def _register_task(path: Path, task_id: str, goal: str, now: datetime) -> None:
    registry = _load_task_registry(path)
    registry[task_id] = {
        "goal": goal,
        "created_at": _timestamp_text(now),
    }
    _save_task_registry(path, registry)


def _task_goal(registry: dict[str, dict[str, object]], task_id: str) -> str | None:
    payload = registry.get(task_id)
    if payload is None:
        return None
    goal = payload.get("goal")
    if not isinstance(goal, str):
        return None
    return goal


class TelegramTaskBridge:
    """Long-poll Telegram updates and map them to Morphony task interactions."""

    def __init__(
        self,
        config: TelegramBridgeConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._agent_config = load_config(config.config_file)
        self._lifecycle = TaskLifecycleManager(config.lifecycle_store)

    async def __aenter__(self) -> "TelegramTaskBridge":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    @property
    def api_base(self) -> str:
        base = self.config.api_base_url.rstrip("/")
        return f"{base}/bot{self.config.token}"

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        self._client = httpx.AsyncClient(base_url=self.api_base, timeout=timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        if self._owns_client:
            self._client = None

    async def serve(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        once: bool = False,
    ) -> None:
        offset: int | None = None
        while True:
            offset = await self.poll_once(offset=offset)
            if once:
                return
            await asyncio.sleep(poll_interval_seconds)

    async def poll_once(self, *, offset: int | None = None) -> int | None:
        params: dict[str, object] = {"timeout": self.config.poll_timeout_seconds}
        if offset is not None:
            params["offset"] = offset

        response = await self._client_or_create().get("getUpdates", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Telegram getUpdates returned an unexpected payload")

        next_offset = offset
        raw_updates = payload.get("result", [])
        if not isinstance(raw_updates, list):
            raise RuntimeError("Telegram getUpdates result must be a list")

        for raw_update in raw_updates:
            if not isinstance(raw_update, dict):
                continue
            update_id = _coerce_int(raw_update.get("update_id"))
            if update_id is not None:
                candidate_offset = update_id + 1
                if next_offset is None or candidate_offset > next_offset:
                    next_offset = candidate_offset

            inbound = self._parse_update(raw_update)
            if inbound is None:
                continue
            if not self._is_allowed_chat(inbound.chat_id):
                continue

            try:
                reply = await self._handle_message(inbound)
            except Exception as exc:  # pragma: no cover - defensive path
                reply = f"Error handling message: {exc}"

            if not reply:
                continue
            await self.send_message(
                inbound.chat_id,
                reply,
                reply_to_message_id=inbound.message_id,
            )

        return next_offset

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        payload_base: dict[str, object] = {"chat_id": chat_id}
        if reply_to_message_id is not None:
            payload_base["reply_to_message_id"] = reply_to_message_id

        client = self._client_or_create()
        for chunk in _split_message(text):
            payload = dict(payload_base)
            payload["text"] = chunk
            response = await client.post("sendMessage", json=payload)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("Telegram sendMessage returned an unexpected payload")

    def _is_allowed_chat(self, chat_id: int) -> bool:
        return not self.config.allowed_chat_ids or chat_id in self.config.allowed_chat_ids

    def _parse_update(self, payload: dict[str, object]) -> TelegramInboundMessage | None:
        message = payload.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_id = _coerce_int(chat.get("id"))
        if chat_id is None:
            return None

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            text = message.get("caption")
        if not isinstance(text, str) or not text.strip():
            return None

        message_id = _coerce_int(message.get("message_id"))
        if message_id is None:
            return None

        from_user = message.get("from")
        sender_id: int | None = None
        username: str | None = None
        if isinstance(from_user, dict):
            sender_id = _coerce_int(from_user.get("id"))
            raw_username = from_user.get("username")
            if isinstance(raw_username, str) and raw_username.strip():
                username = raw_username.strip()

        command, arguments = _parse_command(text)
        return TelegramInboundMessage(
            update_id=_coerce_int(payload.get("update_id")) or 0,
            chat_id=chat_id,
            message_id=message_id,
            text=text.strip(),
            sender_id=sender_id,
            username=username,
            command=command,
            arguments=arguments,
        )

    async def _handle_message(self, message: TelegramInboundMessage) -> str | None:
        if message.command in {"start", "help"}:
            return self._help_text()
        if message.command == "tasks":
            return self._task_list_text()
        if message.command == "status":
            return self._status_text(message.arguments)
        if message.command == "run":
            goal = message.arguments or message.text
            return self._create_task_text(goal)
        if message.command is not None:
            return self._help_text()
        return self._create_task_text(message.text)

    def _help_text(self) -> str:
        return "\n".join(
            [
                f"{self.config.bot_display_name} Telegram bridge is ready.",
                "",
                "Commands:",
                "/run <goal>   Create a new task from a goal",
                "/status       List current tasks",
                "/status <id>  Show one task",
                "/tasks        Alias for /status",
                "/help         Show this help",
                "",
                "Send plain text to create a new task.",
            ]
        )

    def _task_list_text(self) -> str:
        states = self._lifecycle.list_task_states()
        registry = _load_task_registry(self.config.task_registry)
        if not states:
            return "No tasks found. Send a goal to create one."

        lines = ["Current tasks:"]
        for task_id in sorted(states):
            goal = _task_goal(registry, task_id) or "(no goal)"
            lines.append(f"- {task_id}: {states[task_id].value} | {goal}")
        return "\n".join(lines)

    def _status_text(self, task_id: str) -> str:
        task_ref = task_id.strip()
        if not task_ref:
            return self._task_list_text()

        try:
            state = self._lifecycle.get_task_state(task_ref)
            history = self._lifecycle.get_transition_history(task_ref)
        except KeyError:
            return f"Unknown task: {task_ref}"

        registry = _load_task_registry(self.config.task_registry)
        goal = _task_goal(registry, task_ref)
        lines = [
            f"Task: {task_ref}",
            f"State: {state.value}",
        ]
        if goal:
            lines.append(f"Goal: {goal}")
        lines.append(f"Transitions: {len(history)}")
        return "\n".join(lines)

    def _create_task_text(self, goal: str) -> str:
        normalized_goal = goal.strip()
        if not normalized_goal:
            return "Goal text must not be empty."

        task_id = f"task-{uuid4().hex[:12]}"
        if self._agent_config.autonomy_level == AutonomyLevel.plan_only:
            self._lifecycle.submit_task(task_id, start_immediately=False)
            self._lifecycle.transition(task_id, TaskState.planning)
        else:
            self._lifecycle.submit_task(task_id)

        _register_task(self.config.task_registry, task_id, normalized_goal, _utc_now())
        state = self._lifecycle.get_task_state(task_id)
        return "\n".join(
            [
                f"Created task '{task_id}'",
                f"State: {state.value}",
                f"Goal: {normalized_goal}",
            ]
        )

