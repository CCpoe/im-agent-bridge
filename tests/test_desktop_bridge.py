import asyncio
import json
from pathlib import Path

import pytest

from lark_client.desktop_bridge import DesktopBridgeManager
from lark_client.desktop_ipc import DesktopIPCRemoteError


class FakeIPC:
    def __init__(self):
        self.connected = False
        self.client_id = None
        self.listener = None
        self.calls = []
        self.steer_error = False

    def add_state_listener(self, listener):
        self.listener = listener

    def remove_state_listener(self, listener):
        if self.listener == listener:
            self.listener = None

    async def connect(self):
        self.connected = True
        self.client_id = "follower-1"
        self.calls.append(("connect",))
        return self.client_id

    async def disconnect(self):
        self.connected = False
        self.calls.append(("disconnect",))

    async def discover_owner(self, thread_id):
        self.calls.append(("discover", thread_id))
        return "owner-1"

    async def follow(self, thread_id, **kwargs):
        self.calls.append(("follow", thread_id, kwargs))
        return kwargs.get("owner_client_id", "owner-1")

    async def unfollow(self, thread_id, **kwargs):
        self.calls.append(("unfollow", thread_id, kwargs))

    async def request(self, method, params, **kwargs):
        self.calls.append(("request", method, params, kwargs))
        return {"ok": True}

    async def start_turn(self, thread_id, text, client_user_message_id=None):
        self.calls.append(("start", thread_id, text, client_user_message_id))

    async def steer_turn(self, thread_id, text, cwd, client_user_message_id=None):
        self.calls.append(("steer", thread_id, text, cwd, client_user_message_id))
        if self.steer_error:
            raise DesktopIPCRemoteError("thread-follower-steer-turn", "no active turn")

    async def interrupt(self, thread_id, expected_turn_id=None):
        self.calls.append(("interrupt", thread_id, expected_turn_id))

    async def command_approval(self, thread_id, request_id, decision):
        self.calls.append(("command_approval", thread_id, request_id, decision))

    async def file_approval(self, thread_id, request_id, decision):
        self.calls.append(("file_approval", thread_id, request_id, decision))

    async def permissions_approval(self, thread_id, request_id, response):
        self.calls.append(("permissions_approval", thread_id, request_id, response))

    async def submit_user_input(self, thread_id, request_id, response):
        self.calls.append(("input", thread_id, request_id, response))

    async def emit(self, params):
        result = self.listener(params)
        if asyncio.iscoroutine(result):
            await result


class FakeCardService:
    def __init__(self):
        self.created = []
        self.sent = []
        self.updated = []
        self.active = {}

    async def create_card(self, content):
        self.created.append(content)
        return "card-%d" % len(self.created)

    async def send_card(self, chat_id, card_id):
        self.sent.append((chat_id, card_id))
        return "message-%d" % len(self.sent)

    async def update_card(self, card_id, sequence, content):
        self.updated.append((card_id, sequence, content))
        return True

    def get_active_card(self, chat_id):
        return self.active.get(chat_id)

    def set_active_card(self, chat_id, state):
        self.active[chat_id] = state

    def clear_active_card(self, chat_id):
        self.active.pop(chat_id, None)


def snapshot(status="inProgress", requests=None):
    return {
        "conversationId": "thread-1",
        "hostId": "local",
        "change": {
            "type": "snapshot",
            "revision": 1,
            "conversationState": {
                "id": "thread-1",
                "title": "Desktop task",
                "threadRuntimeStatus": {
                    "type": "active" if status == "inProgress" else "idle"
                },
                "turns": [{
                    "turnId": "turn-1",
                    "status": status,
                    "items": [
                        {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "content": ["PRIVATE_REASONING"],
                        },
                        {
                            "id": "agent-1",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "公开进度",
                        },
                    ],
                }],
                "requests": requests or [],
            },
        },
    }


def manager(tmp_path, ipc=None, cards=None, **kwargs):
    return DesktopBridgeManager(
        cards or FakeCardService(),
        ipc or FakeIPC(),
        bindings_path=tmp_path / "bindings.json",
        session_index_path=tmp_path / "session_index.jsonl",
        sessions_dir=tmp_path / "sessions",
        global_state_path=tmp_path / "global-state.json",
        reconnect_interval=0.01,
        card_update_interval=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_attach_persists_only_binding_and_updates_one_card(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    bridge = manager(tmp_path, ipc, cards)

    assert await bridge.attach("chat-1", "user-1", "codex://threads/thread-1")
    assert bridge.binding_for("chat-1") == "thread-1"
    assert bridge.is_attached("chat-1")
    assert json.loads((tmp_path / "bindings.json").read_text()) == {
        "chat-1": "thread-1"
    }
    assert len(cards.created) == 1
    assert cards.sent == [("chat-1", "card-1")]
    assert not any(call[0] == "request" for call in ipc.calls)

    await ipc.emit(snapshot())
    assert len(cards.created) == 1
    assert len(cards.updated) == 1
    rendered = json.dumps(cards.updated[0][2], ensure_ascii=False)
    assert "公开进度" in rendered
    assert "PRIVATE_REASONING" not in rendered
    persisted = (tmp_path / "bindings.json").read_text()
    assert "conversationState" not in persisted
    assert "PRIVATE_REASONING" not in persisted

    await bridge.close()


@pytest.mark.asyncio
async def test_message_actions_approval_input_and_detach(tmp_path):
    ipc = FakeIPC()
    bridge = manager(tmp_path, ipc)
    assert await bridge.attach("chat-1", "user-1", "thread-1")

    await ipc.emit(snapshot())
    assert await bridge.send_message("chat-1", "补充要求")
    assert any(call[0:3] == ("steer", "thread-1", "补充要求") for call in ipc.calls)

    await ipc.emit(snapshot(status="completed", requests=[{
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"reason": "需要运行测试"},
    }]))
    assert await bridge.handle_approval(
        "chat-1", "command_execution", "approval-1", "accept"
    )
    assert ("command_approval", "thread-1", "approval-1", "accept") in ipc.calls

    await ipc.emit(snapshot(status="completed", requests=[{
        "id": "permissions-1",
        "method": "item/permissions/requestApproval",
        "params": {
            "reason": "需要更多权限",
            "permissions": {"network": {"enabled": True}},
        },
    }]))
    assert await bridge.handle_approval(
        "chat-1", "permissions", "permissions-1", "accept"
    )
    assert (
        "permissions_approval",
        "thread-1",
        "permissions-1",
        {"permissions": {"network": {"enabled": True}}, "scope": "turn"},
    ) in ipc.calls
    assert await bridge.handle_approval(
        "chat-1", "permissions", "permissions-1", "decline"
    )
    assert len([call for call in ipc.calls if call[0] == "permissions_approval"]) == 1

    await ipc.emit(snapshot(status="completed", requests=[{
        "id": "input-1",
        "method": "item/tool/requestUserInput",
        "params": {"questions": [{
            "id": "environment",
            "question": "选择环境",
            "options": [{"label": "测试"}],
        }]},
    }]))
    assert await bridge.handle_input("chat-1", "user_input", "input-1", "测试")
    assert (
        "input",
        "thread-1",
        "input-1",
        {"answers": {"environment": {"answers": ["测试"]}}},
    ) in ipc.calls

    await ipc.emit(snapshot(status="completed"))
    ipc.steer_error = True
    assert await bridge.send_message("chat-1", "新任务")
    assert any(call[0:3] == ("start", "thread-1", "新任务") for call in ipc.calls)
    assert await bridge.interrupt("chat-1")
    assert ("interrupt", "thread-1", None) in ipc.calls

    await bridge.detach("chat-1")
    assert not bridge.is_attached("chat-1")
    assert json.loads((tmp_path / "bindings.json").read_text()) == {}
    assert any(call[0:2] == ("unfollow", "thread-1") for call in ipc.calls)
    await bridge.close()


def test_list_threads_merges_latest_index_with_safe_rollout_metadata(tmp_path):
    index = tmp_path / "session_index.jsonl"
    index.write_text("\n".join([
        json.dumps({"id": "desktop-1", "thread_name": "旧名称", "updated_at": "2026-01-01T00:00:00Z"}),
        "not-json",
        json.dumps({"id": "desktop-1", "thread_name": "新名称", "updated_at": "2026-01-03T00:00:00Z"}),
        json.dumps({"id": "cli-1", "thread_name": "CLI", "updated_at": "2026-01-02T00:00:00Z"}),
    ]), encoding="utf-8")
    rollout_dir = tmp_path / "sessions" / "2026" / "01" / "03"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-test-desktop-1.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "desktop-1",
            "timestamp": "2026-01-03T00:00:00Z",
            "cwd": "/workspace/desktop",
            "originator": "Codex Desktop",
            "git": {"repository_url": "git@example.com:team/fallback.git"},
            "private": "ROLLOUT_PRIVATE_MUST_NOT_LEAK",
        },
    }) + "\n", encoding="utf-8")
    (rollout_dir / "rollout-test-cli-1.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "cli-1",
            "timestamp": "2026-01-02T00:00:00Z",
            "cwd": "/workspace/cli",
            "originator": "codex_cli_rs",
        },
    }) + "\n", encoding="utf-8")
    (tmp_path / "global-state.json").write_text(json.dumps({
        "thread-project-assignments": {
            "desktop-1": {"projectKind": "local", "projectId": "project-1"},
        },
        "local-projects": {
            "project-1": {
                "id": "project-1",
                "name": "自定义项目名",
                "rootPaths": ["/workspace/desktop", "/workspace/desktop-worktree"],
                "private": "PROJECT_PRIVATE_MUST_NOT_LEAK",
            },
        },
    }), encoding="utf-8")

    threads = manager(tmp_path).list_threads()
    assert threads == [{
        "id": "desktop-1",
        "thread_id": "desktop-1",
        "title": "新名称",
        "updated_at": "2026-01-03T00:00:00Z",
        "cwd": "/workspace/desktop",
        "originator": "Codex Desktop",
        "project_name": "自定义项目名",
    }]
    serialized = json.dumps(threads)
    assert "ROLLOUT_PRIVATE_MUST_NOT_LEAK" not in serialized
    assert "PROJECT_PRIVATE_MUST_NOT_LEAK" not in serialized


def test_project_name_fallbacks_are_safe_and_worktree_aware(tmp_path):
    bridge = manager(tmp_path)
    catalog = {
        "assignments": {"assigned": "project-1"},
        "projects": {
            "project-1": {"name": "工作台", "root_paths": ["/repo/main"]},
            "project-2": {"name": "子项目", "root_paths": ["/repo/main/sub"]},
        },
        "projectless": {"projectless"},
    }

    assert bridge._project_name_for_thread(
        "assigned", "/tmp/random-worktree", None, catalog
    ) == "工作台"
    assert bridge._project_name_for_thread(
        "by-root", "/repo/main/sub/service", None, catalog
    ) == "子项目"
    assert bridge._project_name_for_thread(
        "boundary", "/repo/main2", "git@example.com:team/repository.git", catalog
    ) == "repository"
    assert bridge._project_name_for_thread(
        "projectless", "/repo/main", None, catalog
    ) == "无项目"
    assert bridge._project_name_for_thread(
        "cwd-only", "/deleted/path/local-project", None, {}
    ) == "local-project"
    assert bridge._project_name_for_thread("unknown", None, None, {}) == "未知项目"


def test_bad_global_state_falls_back_to_repository_name(tmp_path):
    bridge = manager(tmp_path)
    (tmp_path / "global-state.json").write_text("not-json", encoding="utf-8")

    catalog = bridge._load_project_catalog()

    assert bridge._project_name_for_thread(
        "thread-1", None, "https://example.com/team/fallback.git", catalog
    ) == "fallback"


@pytest.mark.asyncio
async def test_revision_gap_enters_safe_patch_only_without_snapshot_storm(tmp_path):
    ipc = FakeIPC()
    bridge = manager(tmp_path, ipc)
    assert await bridge.attach("chat-1", "user-1", "thread-1")
    before = len(ipc.calls)
    await ipc.emit({
        "conversationId": "thread-1",
        "hostId": "local",
        "change": {
            "type": "patches",
            "baseRevision": 99,
            "revision": 100,
            "patches": [],
        },
    })
    assert bridge.state_for_chat("chat-1")["patch_only"] is True
    assert not any(call[0] == "request" for call in ipc.calls[before:])
    await bridge.close()


@pytest.mark.asyncio
async def test_burst_updates_are_coalesced_and_latest_card_wins(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    bridge = DesktopBridgeManager(
        cards,
        ipc,
        bindings_path=tmp_path / "bindings.json",
        session_index_path=tmp_path / "session_index.jsonl",
        sessions_dir=tmp_path / "sessions",
        reconnect_interval=0.01,
        card_update_interval=0.02,
    )
    assert await bridge.attach("chat-1", "user-1", "thread-1")

    for index in range(20):
        await ipc.emit({
            "conversationId": "thread-1",
            "hostId": "local",
            "change": {
                "type": "patches",
                "baseRevision": index,
                "revision": index + 1,
                "patches": [{
                    "op": "replace",
                    "path": ["turnHistory", "history", "entitiesByKey", "turn-1", "items", 0],
                    "value": {
                        "id": "agent-1",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": f"进度-{index}",
                    },
                }],
            },
        })

    await asyncio.sleep(0.12)
    assert 1 <= len(cards.updated) <= 4
    assert "进度-19" in json.dumps(cards.updated[-1][2], ensure_ascii=False)
    await bridge.close()


@pytest.mark.asyncio
async def test_duplicate_client_message_id_is_sent_once(tmp_path):
    ipc = FakeIPC()
    bridge = manager(tmp_path, ipc)
    assert await bridge.attach("chat-1", "user-1", "thread-1")
    await ipc.emit(snapshot())

    assert await bridge.send_message("chat-1", "hello", client_message_id="om-1")
    assert await bridge.send_message("chat-1", "hello", client_message_id="om-1")

    steer_calls = [call for call in ipc.calls if call[0] == "steer"]
    assert len(steer_calls) == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_detach_cancels_pending_card_flush(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    bridge = DesktopBridgeManager(
        cards,
        ipc,
        bindings_path=tmp_path / "bindings.json",
        session_index_path=tmp_path / "session_index.jsonl",
        sessions_dir=tmp_path / "sessions",
        card_update_interval=0.05,
    )
    assert await bridge.attach("chat-1", "user-1", "thread-1")
    before = len(cards.updated)
    await ipc.emit(snapshot())
    await bridge.detach("chat-1")
    await asyncio.sleep(0.08)
    assert len(cards.updated) == before
    assert "chat-1" not in cards.active
    await bridge.close()
