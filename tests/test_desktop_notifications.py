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
    _completion_source_eligibility,
    _is_subagent_metadata,
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


def _metadata(thread_id="thread-1", **overrides):
    return json.dumps({
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": thread_id,
            "originator": "Codex Desktop",
            "source": "vscode",
            **overrides,
        },
    }, separators=(",", ":")) + "\n"


@pytest.mark.parametrize("metadata", [
    {"_is_child": True},
    {"parent_thread_id": "root-1"},
    {"parentThreadId": "root-1"},
    {"thread_source": "subagent"},
    {"threadSource": "subagent"},
    {"source": "subagent"},
    {"source": "SUBAGENT"},
    {"source": "subAgentThreadSpawn"},
    {"source": json.dumps("subAgentThreadSpawn")},
    {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "root-1"}}}},
    {"source": json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root-1"}}})},
])
def test_subagent_detection_uses_structured_ownership_metadata(metadata):
    assert _is_subagent_metadata(metadata)


@pytest.mark.parametrize("metadata", [
    {},
    {"source": "vscode"},
    {"source": "appServer", "parent_thread_id": None},
    {"title": "subagent completed", "source": {"note": "subagent"}},
    {"source": "{malformed"},
])
def test_subagent_detection_does_not_guess_from_title_or_unrelated_text(metadata):
    assert not _is_subagent_metadata(metadata)


@pytest.mark.parametrize("metadata", [
    {"parent_thread_id": "thread-1"},
    {"thread_source": "subagent"},
    {"source": "subAgentThreadSpawn"},
    {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "thread-1"}}}},
    {"source": json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "thread-1"}}})},
])
@pytest.mark.asyncio
async def test_monitor_only_notifies_root_after_child_completion(tmp_path, metadata):
    root = tmp_path / "root.jsonl"
    child = tmp_path / "child.jsonl"
    root.write_text(_metadata())
    child.write_text(_metadata("child-1", session_id="thread-1", **metadata))
    sources = lambda: [
        {"thread_id": "thread-1", "_rollout_path": str(root)},
        {"thread_id": "child-1", "_rollout_path": str(child)},
    ]
    cards = FakeCardService()
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json"
    )
    await monitor.register_target("user-1")
    with child.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "child-turn"}))
    with root.open("a") as target:
        target.write(_line({
            "type": "item_completed",
            "thread_id": "thread-1",
            "turn_id": "root-turn",
            "item": {
                "type": "SubAgentActivity", "kind": "completed",
                "agent_thread_id": "child-1", "agent_path": "/root/review",
            },
        }))
    assert await monitor.poll_once() == 0
    assert cards.calls == []
    assert "child-1" not in monitor._cursors

    with root.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "root-turn"}))
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 1
    assert "child-1" not in json.dumps(cards.calls[0][1])


@pytest.mark.asyncio
async def test_monitor_rejects_child_rollout_reusing_root_session_id_without_other_flags(tmp_path):
    child = tmp_path / "child.jsonl"
    child.write_text(_metadata("child-1", session_id="thread-1"))
    sources = lambda: [{"thread_id": "thread-1", "_rollout_path": str(child)}]
    cards = FakeCardService()
    monitor = DesktopCompletionMonitor(
        cards, sources, state_path=tmp_path / "notifications.json"
    )
    await monitor.register_target("user-1")
    with child.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "child-turn"}))

    assert await monitor.poll_once() == 0
    assert cards.calls == []
    assert monitor._cursors == {}


def test_completion_eligibility_accepts_legacy_root_session_id(tmp_path):
    rollout = tmp_path / "legacy.jsonl"
    rollout.write_text(_metadata(id=None))
    source = {"thread_id": "thread-1", "_rollout_path": str(rollout)}

    assert _completion_source_eligibility(source) is True
    rollout.write_text(_metadata(id=None, source={"subagent": "review"}))
    assert _completion_source_eligibility(source) is False


@pytest.mark.parametrize("contents", [
    "",
    '{"type":"session_meta","payload":',
    "{invalid}\n",
    _line({"type": "task_complete", "turn_id": "unattributed"}),
])
def test_completion_eligibility_defers_unverifiable_source(tmp_path, contents):
    rollout = tmp_path / "unknown.jsonl"
    rollout.write_text(contents)

    assert _completion_source_eligibility({
        "thread_id": "thread-1", "_rollout_path": str(rollout),
    }) is None


@pytest.mark.asyncio
async def test_first_registration_uses_pre_snapshot_cutoff_but_activates_afterward(
    tmp_path, monkeypatch
):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_metadata() + _line({"type": "task_complete", "turn_id": "old"}))
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
    rollout.write_text(_metadata() + _line({"type": "task_complete", "turn_id": "old"}))
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
    rollout.write_text(_metadata() + _line({"type": "task_complete", "turn_id": "old"}))
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
    rendered = json.dumps(card, ensure_ascii=False)
    assert "<font color='codex_status_success_text'>**执行完成**</font>" in rendered
    assert card["config"]["summary"]["content"]
    assert "NEXT" in rendered
    assert "重新连接" in rendered
    assert "连接此 Session" not in rendered
    assert len(message_uuid) == 36

    with rollout.open("a") as target:
        target.write(_line({"type": "task_started", "turn_id": "turn-2"}))
        target.write(_line({
            "type": "task_complete", "turn_id": "turn-2", "error": {},
    }))
    assert await monitor.poll_once() == 1
    failed_card = json.dumps(cards.calls[-1][1], ensure_ascii=False)
    assert "<font color='codex_status_failure_text'>**执行失败**</font>" in failed_card
    assert "执行失败" in failed_card

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
    rollout.write_text(_metadata() + _line({"type": "task_started", "turn_id": "turn-1"}))
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
    assert pending["source_path"] == str(rollout)
    assert pending["source_thread_id"] == "thread-1"
    pending["next_attempt_at"] = 0
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 2
    assert cards.calls[0][2] == cards.calls[1][2]
    assert monitor._pending == {}


@pytest.mark.asyncio
async def test_monitor_replays_persisted_pending_delivery_after_restart(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_metadata() + _line({"type": "task_started", "turn_id": "turn-1"}))
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


@pytest.mark.parametrize("restore_root_source", [False, True])
@pytest.mark.asyncio
async def test_monitor_discards_persisted_child_outbox_after_restart(tmp_path, restore_root_source):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_metadata())
    sources = lambda: [{"thread_id": "thread-1", "_rollout_path": str(rollout)}]
    state_path = tmp_path / "notifications.json"
    first = DesktopCompletionMonitor(FakeCardService([None]), sources, state_path=state_path)
    await first.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "legacy-child-turn"}))
    assert await first.poll_once() == 0
    assert first._pending
    for pending in first._pending.values():
        pending.pop("source_path", None)
        pending.pop("source_thread_id", None)
    first._save_state()

    # 模拟旧版本误把复用根 session_id 的子 rollout 作为根通知来源入箱。
    rollout.write_text(_metadata(
        "child-1", session_id="thread-1",
        source={"subagent": {"thread_spawn": {"parent_thread_id": "thread-1"}}},
    ))
    root = tmp_path / "real-root.jsonl"
    root.write_text(_metadata())
    current_sources = (
        [{"thread_id": "thread-1", "_rollout_path": str(root)}]
        if restore_root_source else []
    )
    cards = FakeCardService()
    restarted = DesktopCompletionMonitor(cards, lambda: current_sources, state_path=state_path)
    next(iter(restarted._pending.values()))["next_attempt_at"] = 0
    assert await restarted.poll_once() == 0
    assert restarted._pending == {}
    assert cards.calls == []
    assert json.loads(state_path.read_text())["pending"] == {}


@pytest.mark.asyncio
async def test_verified_pending_recovers_after_root_moves_to_archive(tmp_path):
    rollout = tmp_path / "root.jsonl"
    archived = tmp_path / "archived-root.jsonl"
    rollout.write_text(_metadata())
    sources = lambda: [{"thread_id": "thread-1", "_rollout_path": str(rollout)}]
    state_path = tmp_path / "notifications.json"
    first = DesktopCompletionMonitor(FakeCardService([None]), sources, state_path=state_path)
    await first.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "root-turn"}))
    assert await first.poll_once() == 0
    rollout.rename(archived)

    cards = FakeCardService()
    restarted = DesktopCompletionMonitor(cards, lambda: [{
        "thread_id": "thread-1", "_rollout_path": str(archived),
    }], state_path=state_path)
    pending = next(iter(restarted._pending.values()))
    pending["next_attempt_at"] = 0
    assert await restarted.poll_once() == 1
    assert len(cards.calls) == 1
    assert pending["source_path"] == str(rollout)
    assert restarted._pending == {}


@pytest.mark.parametrize("keep_old_cursor", [False, True])
@pytest.mark.asyncio
async def test_unverified_legacy_pending_cannot_borrow_restored_root_source(tmp_path, keep_old_cursor):
    rollout = tmp_path / "old-source.jsonl"
    restored = tmp_path / "restored-root.jsonl"
    rollout.write_text(_metadata())
    sources = lambda: [{"thread_id": "thread-1", "_rollout_path": str(rollout)}]
    state_path = tmp_path / "notifications.json"
    first = DesktopCompletionMonitor(FakeCardService([None]), sources, state_path=state_path)
    await first.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "old-turn"}))
    assert await first.poll_once() == 0
    for pending in first._pending.values():
        pending.pop("source_path", None)
        pending.pop("source_thread_id", None)
    if not keep_old_cursor:
        first._cursors.clear()
    first._save_state()
    rollout.rename(restored)

    cards = FakeCardService()
    restarted = DesktopCompletionMonitor(cards, lambda: [{
        "thread_id": "thread-1", "_rollout_path": str(restored),
    }], state_path=state_path)
    pending = next(iter(restarted._pending.values()))
    pending["next_attempt_at"] = 0
    assert await restarted.poll_once() == 0
    assert await restarted.poll_once() == 0
    assert restarted._pending
    assert pending["source_path"] == (str(rollout) if keep_old_cursor else None)
    assert "source_thread_id" not in pending
    assert cards.calls == []


@pytest.mark.asyncio
async def test_legacy_pending_can_verify_original_root_before_later_move(tmp_path):
    rollout = tmp_path / "root.jsonl"
    archived = tmp_path / "archived-root.jsonl"
    rollout.write_text(_metadata())
    source_path = [rollout]
    sources = lambda: [{"thread_id": "thread-1", "_rollout_path": str(source_path[0])}]
    state_path = tmp_path / "notifications.json"
    first = DesktopCompletionMonitor(FakeCardService([None]), sources, state_path=state_path)
    await first.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "root-turn"}))
    assert await first.poll_once() == 0
    for pending in first._pending.values():
        pending.pop("source_path", None)
        pending.pop("source_thread_id", None)
    first._save_state()

    cards = FakeCardService([None, "archived-delivery"])
    restarted = DesktopCompletionMonitor(cards, sources, state_path=state_path)
    pending = next(iter(restarted._pending.values()))
    pending["next_attempt_at"] = 0
    assert await restarted.poll_once() == 0
    assert pending["source_thread_id"] == "thread-1"
    rollout.rename(archived)
    source_path[0] = archived
    pending["next_attempt_at"] = 0
    assert await restarted.poll_once() == 1
    assert pending["source_path"] == str(rollout)
    assert len(cards.calls) == 2


@pytest.mark.asyncio
async def test_monitor_preserves_pending_when_metadata_unreadable_and_retries_on_recovery(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_metadata())
    sources = lambda: [{"thread_id": "thread-1", "_rollout_path": str(rollout)}]
    state_path = tmp_path / "notifications.json"
    first = DesktopCompletionMonitor(FakeCardService([None]), sources, state_path=state_path)
    await first.register_target("user-1")
    with rollout.open("a") as target:
        target.write(_line({"type": "task_complete", "turn_id": "root-turn"}))
    assert await first.poll_once() == 0
    saved_contents = rollout.read_text()
    rollout.write_text('{"type":"session_meta","payload":')
    cards = FakeCardService()
    restarted = DesktopCompletionMonitor(cards, sources, state_path=state_path)
    next(iter(restarted._pending.values()))["next_attempt_at"] = 0

    assert await restarted.poll_once() == 0
    assert restarted._pending
    assert cards.calls == []

    rollout.write_text(saved_contents)
    assert await restarted.poll_once() == 1
    assert restarted._pending == {}
    assert len(cards.calls) == 1


@pytest.mark.asyncio
async def test_monitor_retries_only_remaining_target(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_metadata() + _line({"type": "task_started", "turn_id": "turn-1"}))
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
    rollout.write_text(_metadata() + _line({
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
    rollout.write_text(_metadata() + _line({"type": "task_complete", "turn_id": "truncated"}))
    assert await monitor.poll_once() == 1

    # New inode at the same path.
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(_metadata() + _line({"type": "task_complete", "turn_id": "replaced"}))
    os.replace(replacement, rollout)
    assert await monitor.poll_once() == 1
    assert len(cards.calls) == 2
    assert cards.calls[0][2] != cards.calls[1][2]


@pytest.mark.asyncio
async def test_monitor_waits_for_complete_json_line(tmp_path):
    rollout = tmp_path / "rollout-thread-1.jsonl"
    rollout.write_text(_metadata() + _line({"type": "task_started", "turn_id": "turn-1"}))
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
    rollout.write_text(_metadata() + _line({"type": "task_started", "turn_id": "turn-large"}))
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
    rendered = json.dumps(cards.calls[0][1], ensure_ascii=False)
    assert "<font color='codex_status_success_text'>**执行完成**</font>" in rendered
    assert cards.calls[0][1]["config"]["summary"]["content"]


def test_notification_state_file_is_private(tmp_path):
    monitor = DesktopCompletionMonitor(
        FakeCardService(), lambda: [], state_path=tmp_path / "notifications.json"
    )
    monitor._targets["user-1"] = 1.0
    monitor._save_state()

    mode = stat.S_IMODE((tmp_path / "notifications.json").stat().st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_private_notification_uses_open_id_uuid_preserves_active_and_tracks_message():
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
    assert service._cards_by_message_id["message-1"].card_id == "notification-card"
    assert service._cards_by_message_id["message-1"].message_id == "message-1"


@pytest.mark.asyncio
async def test_notification_card_is_promoted_only_after_successful_update():
    service = CardService.__new__(CardService)
    existing = CardState(card_id="active-card", message_id="active-message")
    notification = CardState(card_id="notification-card", message_id="notification-message")
    service.client = SimpleNamespace()
    service._active_cards = {"chat-1": existing}
    service._cards_by_message_id = {"notification-message": notification}
    service.update_card = AsyncMock(return_value=False)

    assert not await service.update_and_reuse_message_card(
        "chat-1", "notification-message", {"schema": "2.0"}
    )
    assert service.get_active_card("chat-1") is existing

    service.update_card = AsyncMock(return_value=True)
    assert await service.update_and_reuse_message_card(
        "chat-1", "notification-message", {"schema": "2.0"}
    )
    assert service.get_active_card("chat-1") is notification


@pytest.mark.asyncio
async def test_old_notification_card_is_recovered_by_message_id_after_restart():
    class MessageAPI:
        def get(self, request):
            assert request.message_id == "notification-message"
            item = SimpleNamespace(
                message_id="notification-message",
                chat_id="chat-1",
                body=SimpleNamespace(content=json.dumps({
                    "type": "card",
                    "data": {"card_id": "notification-card"},
                })),
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(items=[item]),
            )

    service = CardService.__new__(CardService)
    service.client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=MessageAPI()))
    )
    service._active_cards = {}
    service._cards_by_message_id = {}
    service.update_card = AsyncMock(return_value=True)

    assert await service.update_and_reuse_message_card(
        "chat-1", "notification-message", {"schema": "2.0"}
    )
    assert service.get_active_card("chat-1").card_id == "notification-card"
    assert service._cards_by_message_id["notification-message"].sequence > 1


@pytest.mark.asyncio
async def test_recovered_notification_card_must_belong_to_callback_chat():
    class MessageAPI:
        def get(self, request):
            item = SimpleNamespace(
                message_id="notification-message",
                chat_id="different-chat",
                body=SimpleNamespace(content=json.dumps({
                    "type": "card",
                    "data": {"card_id": "notification-card"},
                })),
            )
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(items=[item]),
            )

    service = CardService.__new__(CardService)
    service.client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=MessageAPI()))
    )
    service._active_cards = {}
    service._cards_by_message_id = {}
    service.update_card = AsyncMock(return_value=True)

    assert not await service.update_and_reuse_message_card(
        "chat-1", "notification-message", {"schema": "2.0"}
    )
    service.update_card.assert_not_awaited()
    assert service._active_cards == {}
