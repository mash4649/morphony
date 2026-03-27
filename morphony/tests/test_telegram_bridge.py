from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from morphony.integrations.telegram import TelegramBridgeConfig, TelegramTaskBridge
from morphony.lifecycle import TaskLifecycleManager
from morphony.models import TaskState


def _make_update(
    update_id: int,
    *,
    chat_id: int = 123,
    message_id: int = 1,
    text: str = "hello morphony",
    username: str = "alice",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 7, "username": username},
            "date": 1710000000,
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_bridge_creates_task_and_replies_with_id(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    task_registry = tmp_path / "runtime" / "tasks.json"
    requests: list[tuple[str, dict[str, object] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [_make_update(1)]})
        if request.url.path.endswith("/sendMessage"):
            requests.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        return httpx.Response(404, json={"ok": False})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.telegram.org/botTEST")
    bridge = TelegramTaskBridge(
        TelegramBridgeConfig(
            token="TEST",
            lifecycle_store=lifecycle_store,
            task_registry=task_registry,
        ),
        client=client,
    )

    try:
        next_offset = await bridge.poll_once(offset=1)
        assert next_offset == 2
    finally:
        await bridge.aclose()

    assert requests
    assert requests[0][0].endswith("/sendMessage")
    payload = requests[0][1]
    assert payload is not None
    assert isinstance(payload["text"], str)
    assert "Created task" in payload["text"]

    manager = TaskLifecycleManager(lifecycle_store)
    task_states = manager.list_task_states()
    assert len(task_states) == 1
    task_id = next(iter(task_states))
    assert task_states[task_id] == TaskState.running


@pytest.mark.asyncio
async def test_bridge_supports_status_and_help_commands(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    task_registry = tmp_path / "runtime" / "tasks.json"
    manager = TaskLifecycleManager(lifecycle_store)
    manager.submit_task("task-existing", start_immediately=False)
    manager.transition("task-existing", TaskState.planning)
    task_registry.write_text(
        """
        {
          "task-existing": {
            "goal": "prepare release notes",
            "created_at": "2026-03-28T00:00:00Z"
          }
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        _make_update(1, text="/status task-existing"),
                        _make_update(2, text="/help"),
                    ],
                },
            )
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        return httpx.Response(404, json={"ok": False})

    sent_payloads: list[dict[str, object]] = []

    async def capture_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return await handler(request)
        if request.url.path.endswith("/sendMessage"):
            sent_payloads.append(json.loads(request.content.decode("utf-8")))
            return await handler(request)
        return httpx.Response(404, json={"ok": False})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(capture_handler),
        base_url="https://api.telegram.org/botTEST",
    )
    bridge = TelegramTaskBridge(
        TelegramBridgeConfig(
            token="TEST",
            lifecycle_store=lifecycle_store,
            task_registry=task_registry,
        ),
        client=client,
    )

    try:
        await bridge.poll_once(offset=1)
    finally:
        await bridge.aclose()

    assert len(sent_payloads) == 2
    assert "Task: task-existing" in sent_payloads[0]["text"]
    assert "State: planning" in sent_payloads[0]["text"]
    assert "/run <goal>" in sent_payloads[1]["text"]


@pytest.mark.asyncio
async def test_bridge_filters_disallowed_chat_ids(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    task_registry = tmp_path / "runtime" / "tasks.json"
    sent_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(
                200,
                json={"ok": True, "result": [_make_update(1, chat_id=999)]},
            )
        if request.url.path.endswith("/sendMessage"):
            sent_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        return httpx.Response(404, json={"ok": False})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.telegram.org/botTEST",
    )
    bridge = TelegramTaskBridge(
        TelegramBridgeConfig(
            token="TEST",
            allowed_chat_ids=[123],
            lifecycle_store=lifecycle_store,
            task_registry=task_registry,
        ),
        client=client,
    )

    try:
        await bridge.poll_once(offset=1)
    finally:
        await bridge.aclose()

    assert sent_payloads == []
    assert TaskLifecycleManager(lifecycle_store).list_task_states() == {}
