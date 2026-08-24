import json
import os
import stat
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lark_client.desktop_notifications import (
    DesktopCompletionMonitor,
    _complete_file_offset,
)
from lark_client.card_service import CardService, CardState


class FakeCardService:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def create_and_send_card_to_user(self, user_id, card, *, message_uuid=None):
        self.calls.append((user_id, card, message_uuid))
        if self.results:
            return self.results.pop(0)
        return "message-1"


def _line(payload):
    return json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "event_msg",
        "payload": payload,
    }, separators=(",", ":")) + "\n"


@pytest.mark.asyncio
async def test_first_registration_uses_pre_snapshot_cutoff_but_activates_afterward(
    tmp_path, monkeypatch
):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_complete", "turn_id": "old"}))
    sources = lambda: [{
            "thread_id": "thread-1",
            "_rollout_path": str(rollout),
        }]

    monitor = DesktopCompletionMonitor(
        FakeCardService(),
        sources,
        state_path=tmp_path / "notifications.json",
        poll_interval=3600,
    )
    snapshot = monitor._snapshot_cursors
    observations = []

    def snapshot_with_race(cutoff):
        observations.append((monitor.has_targets, cutoff, time.time()))
        cursors = snapshot(cutoff)
        with rollout.open("a") as target:
            target.write(_line({"type": "task_complete", "turn_id": "during-prime"}))
        return cursors

    monkeypatch.setattr(monitor, "_snapshot_cursors", snapshot_with_race)

    assert await monitor.register_target("user-1")
    assert observations[0][0] is False
    assert monitor._targets["user-1"] == observations[0][1]
    assert observations[0][1] <= observations[0][2]
    assert await monitor.poll_once() == 1


@pytest.mark.asyncio
async def test_completion_written_before_file_stat_is_not_lost(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_complete", "turn_id": "old"}))
    time.sleep(0.01)
    cards = FakeCardService()
    wrote_during_snapshot = False

    def sources():
        nonlocal wrote_during_snapshot
        if not wrote_during_snapshot:
            wrote_during_snapshot = True
            with rollout.open("a") as target:
                target.write(_line({
                    "type": "task_complete",
                    "turn_id": "before-stat",
                }))
        return [{
            "thread_id": "thread-1",
            "title": "任务一",
            "project_name": "项目甲",
            "_rollout_path": str(rollout),
        }]

    monitor = DesktopCompletionMonitor(
        cards,
        sources,
        state_path=tmp_path / "notifications.json",
        poll_interval=3600,
    )

    assert await monitor.register_target("user-1")
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 1
    assert "before-stat" not in json.dumps(cards.calls[0][1], ensure_ascii=False)


def test_complete_file_offset_uses_the_supplied_size_boundary(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    prefix = b'{"type":"event_msg","payload":{"type":"task_complete"'
    rollout.write_bytes(b"old\n" + prefix)
    snapshot_size = rollout.stat().st_size
    with rollout.open("ab") as target:
        target.write(b'}}\n')

    # The snapshot ended in the middle of a record, so the safe cursor is the
    # previous newline, not either the old or current EOF.
    assert _complete_file_offset(rollout, snapshot_size) == len(b"old\n")


@pytest.mark.asyncio
async def test_monitor_skips_history_then_notifies_each_new_completion(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_complete", "turn_id": "old"}))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "任务一",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    cards = FakeCardService()
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json", poll_interval=3600
    )

    assert await monitor.register_target("user-1")
    assert await monitor.poll_once() == 0

    with rollout.open("a") as target:
        target.write(_line({"type": "task_started", "turn_id": "turn-1"}))
        target.write(_line({"type": "task_complete", "turn_id": "turn-1"}))
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 1
    user_id, card, message_uuid = cards.calls[0]
    assert user_id == "user-1"
    assert card["header"]["template"] == "green"
    assert "连接此 Session" in json.dumps(card, ensure_ascii=False)
    assert len(message_uuid) == 36

    with rollout.open("a") as target:
        target.write(_line({"type": "task_started", "turn_id": "turn-2"}))
        target.write(_line({
            "type": "task_complete", "turn_id": "turn-2", "error": {},
        }))
    assert await monitor.poll_once() == 1
    assert cards.calls[-1][1]["header"]["template"] == "red"
    assert "执行失败" in json.dumps(cards.calls[-1][1], ensure_ascii=False)

    restarted_cards = FakeCardService()
    restarted = DesktopCompletionMonitor(
        restarted_cards,
        sources,
        state_path=tmp_path / "notifications.json",
        poll_interval=3600,
    )
    assert await restarted.poll_once() == 0
    assert restarted_cards.calls == []


@pytest.mark.asyncio
async def test_monitor_retries_failed_delivery_without_duplicate_success(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_started", "turn_id": "turn-1"}))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "任务一",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    cards = FakeCardService([None, "message-1"])
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json", poll_interval=3600
    )
    await monitor.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "turn-1"}))

    assert await monitor.poll_once() == 0
    pending = next(iter(monitor._pending.values()))
    pending["next_attempt_at"] = 0
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 2
    assert cards.calls[0][2] == cards.calls[1][2]
    assert monitor._pending == {}


@pytest.mark.asyncio
async def test_monitor_replays_persisted_pending_delivery_after_restart(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_started", "turn_id": "turn-1"}))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "任务一",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    state_path = tmp_path / "notifications.json"
    first_cards = FakeCardService([None])
    first = DesktopCompletionMonitor(
        first_cards, sources, state_path=state_path, poll_interval=3600
    )
    await first.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "turn-1"}))
    assert await first.poll_once() == 0
    first_uuid = first_cards.calls[0][2]

    restarted_cards = FakeCardService(["message-after-restart"])
    restarted = DesktopCompletionMonitor(
        restarted_cards, sources, state_path=state_path, poll_interval=3600
    )
    pending = next(iter(restarted._pending.values()))
    pending["next_attempt_at"] = 0

    assert await restarted.poll_once() == 1
    assert restarted_cards.calls[0][2] == first_uuid
    assert restarted._pending == {}


@pytest.mark.asyncio
async def test_monitor_retries_only_remaining_target(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_started", "turn_id": "turn-1"}))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "任务一",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    cards = FakeCardService(["message-user-1", None, "message-user-2"])
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json", poll_interval=3600
    )
    await monitor.register_target("user-1")
    await monitor.register_target("user-2")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "turn-1"}))

    assert await monitor.poll_once() == 1
    pending = next(iter(monitor._pending.values()))
    assert pending["targets"] == ["user-2"]
    pending["next_attempt_at"] = 0
    assert await monitor.poll_once() == 1
    assert [call[0] for call in cards.calls] == ["user-1", "user-2", "user-2"]
    assert cards.calls[1][2] == cards.calls[2][2]


@pytest.mark.asyncio
async def test_monitor_recovers_from_truncate_and_inode_replacement(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({
        "type": "task_started",
        "turn_id": "old",
        "padding": "x" * 4096,
    }))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "任务一",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    cards = FakeCardService()
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json", poll_interval=3600
    )
    await monitor.register_target("user-1")

    # Same inode, shorter file after truncate.
    rollout.write_text(_line({"type": "task_complete", "turn_id": "truncated"}))
    assert await monitor.poll_once() == 1

    # New inode at the same path.
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(_line({"type": "task_complete", "turn_id": "replaced"}))
    os.replace(replacement, rollout)
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 2
    assert cards.calls[0][2] != cards.calls[1][2]


@pytest.mark.asyncio
async def test_monitor_waits_for_complete_json_line(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_started", "turn_id": "turn-1"}))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "任务一",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    cards = FakeCardService()
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json", poll_interval=3600
    )
    await monitor.register_target("user-1")
    completion = _line({"type": "task_complete", "turn_id": "turn-1"})
    with rollout.open("a") as target:
        target.write(completion[:-1])
    assert await monitor.poll_once() == 0
    with rollout.open("a") as target:
        target.write("\n")
    assert await monitor.poll_once() == 1


@pytest.mark.asyncio
async def test_monitor_streams_completion_record_larger_than_sixteen_megabytes(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_line({"type": "task_started", "turn_id": "turn-large"}))
    sources = lambda: [{
        "thread_id": "thread-1",
        "title": "大回复任务",
        "project_name": "项目甲",
        "_rollout_path": str(rollout),
    }]
    cards = FakeCardService()
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json", poll_interval=3600
    )
    await monitor.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({
            "type": "task_complete",
            "turn_id": "turn-large",
            "last_agent_message": (
                'distractors: "type":"not-an-event", "error":{"message":"fake"} '
                + "x" * (16 * 1024 * 1024 + 1024)
            ),
        }))

    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 1
    assert cards.calls[0][1]["header"]["template"] == "green"


def test_notification_state_file_is_private(tmp_path):
    monitor = DesktopCompletionMonitor(
        FakeCardService(), lambda: [], state_path=tmp_path / "notifications.json"
    )
    monitor._targets["user-1"] = 1.0
    monitor._save_state()

    mode = stat.S_IMODE((tmp_path / "notifications.json").stat().st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_private_notification_uses_open_id_uuid_and_preserves_active_card():
    captured = []

    class MessageAPI:
        def create(self, request):
            captured.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="message-1"),
            )

    service = CardService.__new__(CardService)
    service.client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=MessageAPI()))
    )
    service._active_cards = {
        "chat-1": CardState(card_id="active-card", message_id="active-message")
    }
    service._cards_by_message_id = {}
    service.create_card = AsyncMock(return_value="notification-card")

    message_id = await service.create_and_send_card_to_user(
        "open-user-1", {"schema": "2.0"}, message_uuid="stable-message-uuid"
    )

    assert message_id == "message-1"
    assert captured[0].receive_id_type == "open_id"
    assert captured[0].request_body.receive_id == "open-user-1"
    assert captured[0].request_body.uuid == "stable-message-uuid"
    assert service.get_active_card("chat-1").card_id == "active-card"
    assert service._cards_by_message_id == {}
