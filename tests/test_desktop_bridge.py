import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import lark_client.desktop_bridge as bridge_module
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


class FakeAppServer:
    def __init__(self, archived_threads=None):
        self.archived_threads = list(archived_threads or [])
        self.list_calls = []
        self.unarchived = []
        self.closed = False

    async def list_threads(self, archived, **kwargs):
        self.list_calls.append((archived, kwargs))
        return list(self.archived_threads) if archived else []

    async def unarchive_thread(self, thread_id):
        self.unarchived.append(thread_id)
        return {"id": thread_id}

    async def close(self):
        self.closed = True


class FakeCardService:
    def __init__(self):
        self.created = []
        self.sent = []
        self.updated = []
        self.active = {}
        self.user_cards = []

    async def create_card(self, content):
        self.created.append(content)
        return "card-%d" % len(self.created)

    async def send_card(self, chat_id, card_id):
        self.sent.append((chat_id, card_id))
        return "message-%d" % len(self.sent)

    async def create_and_send_card_to_user(self, user_id, content, *, message_uuid=None):
        self.user_cards.append((user_id, content, message_uuid))
        return "notification-%d" % len(self.user_cards)

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
    kwargs.setdefault("app_server_client", FakeAppServer())
    return DesktopBridgeManager(
        cards or FakeCardService(),
        ipc or FakeIPC(),
        bindings_path=tmp_path / "bindings.json",
        session_index_path=tmp_path / "session_index.jsonl",
        sessions_dir=tmp_path / "sessions",
        archived_sessions_dir=tmp_path / "archived_sessions",
        state_db_path=tmp_path / "state.sqlite",
        global_state_path=tmp_path / "global-state.json",
        notification_state_path=tmp_path / "notifications.json",
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

    bridge = manager(tmp_path)
    threads = bridge.list_threads()
    assert threads == [{
        "id": "desktop-1",
        "thread_id": "desktop-1",
        "title": "新名称",
        "updated_at": "2026-01-03T00:00:00Z",
        "cwd": "/workspace/desktop",
        "originator": "Codex Desktop",
        "project_name": "自定义项目名",
        "status": "idle",
    }]
    serialized = json.dumps(threads)
    assert "ROLLOUT_PRIVATE_MUST_NOT_LEAK" not in serialized
    assert "PROJECT_PRIVATE_MUST_NOT_LEAK" not in serialized

    internal = bridge.list_threads(None, include_internal=True)
    assert internal[0]["_rollout_path"] == str(
        rollout_dir / "rollout-test-desktop-1.jsonl"
    )


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
async def test_archived_threads_use_app_server_and_can_be_restored(tmp_path):
    archived_dir = tmp_path / "archived_sessions"
    archived_dir.mkdir()
    rollout = archived_dir / "rollout-test-archived-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "archived-1",
            "cwd": "/workspace/archive",
            "originator": "Codex Desktop",
        },
    }) + "\n", encoding="utf-8")
    app_server = FakeAppServer([{
        "id": "archived-1",
        "name": "归档任务",
        "cwd": "/workspace/archive",
        "source": "vscode",
        "path": str(rollout),
        "updatedAt": 1787570000,
        "threadSource": None,
    }])
    bridge = manager(tmp_path, app_server_client=app_server)

    threads = await bridge.get_archived_threads()

    assert threads[0]["thread_id"] == "archived-1"
    assert threads[0]["title"] == "归档任务"
    assert app_server.list_calls[0][0] is True
    assert app_server.list_calls[0][1]["use_state_db_only"] is False
    assert await bridge.unarchive_thread("archived-1")
    assert app_server.unarchived == ["archived-1"]
    await bridge.close()
    assert app_server.closed


@pytest.mark.asyncio
async def test_archived_app_server_failure_bypasses_incomplete_state_db(tmp_path):
    archived_dir = tmp_path / "archived_sessions"
    archived_dir.mkdir()
    rollout = archived_dir / "rollout-old-archived.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "old-archived",
            "cwd": "/workspace/legacy",
            "originator": "Codex Desktop",
        },
    }) + "\n", encoding="utf-8")
    (tmp_path / "session_index.jsonl").write_text(json.dumps({
        "id": "old-archived",
        "thread_name": "旧归档任务",
        "updated_at": "2026-01-01T00:00:00Z",
    }) + "\n", encoding="utf-8")

    # A valid but incomplete DB reproduces the compatibility case: the old
    # archived rollout exists on disk but has not been migrated into threads.
    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.execute("""
        CREATE TABLE threads (
            id TEXT, rollout_path TEXT, updated_at INTEGER, source TEXT,
            cwd TEXT, title TEXT, archived INTEGER, recency_at_ms INTEGER
        )
    """)
    connection.commit()
    connection.close()

    app_server = FakeAppServer()
    app_server.list_threads = AsyncMock(side_effect=RuntimeError("unavailable"))
    bridge = manager(tmp_path, app_server_client=app_server)

    threads = await bridge.get_archived_threads()

    assert [thread["thread_id"] for thread in threads] == ["old-archived"]
    assert threads[0]["title"] == "旧归档任务"
    await bridge.close()


def test_active_thread_list_excludes_archived_rollouts(tmp_path):
    (tmp_path / "session_index.jsonl").write_text("\n".join([
        json.dumps({"id": "active-1", "thread_name": "活跃", "updated_at": "2026-01-02Z"}),
        json.dumps({"id": "archived-1", "thread_name": "归档", "updated_at": "2026-01-01Z"}),
    ]) + "\n", encoding="utf-8")
    active_dir = tmp_path / "sessions" / "2026" / "01" / "02"
    active_dir.mkdir(parents=True)
    (active_dir / "rollout-active-1.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "active-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")
    archived_dir = tmp_path / "archived_sessions"
    archived_dir.mkdir()
    (archived_dir / "rollout-archived-1.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "archived-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")

    assert [item["thread_id"] for item in manager(tmp_path).list_threads(None)] == [
        "active-1"
    ]


def test_active_thread_list_excludes_archived_rows_in_state_db(tmp_path):
    active_dir = tmp_path / "sessions"
    archived_dir = tmp_path / "archived_sessions"
    active_dir.mkdir()
    archived_dir.mkdir()
    active_rollout = active_dir / "rollout-active-1.jsonl"
    archived_rollout = archived_dir / "rollout-archived-1.jsonl"
    for path, thread_id in (
        (active_rollout, "active-1"),
        (archived_rollout, "archived-1"),
    ):
        path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "originator": "Codex Desktop",
            },
        }) + "\n", encoding="utf-8")

    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.execute("""
        CREATE TABLE threads (
            id TEXT, rollout_path TEXT, updated_at INTEGER, source TEXT,
            cwd TEXT, title TEXT, archived INTEGER, recency_at_ms INTEGER
        )
    """)
    connection.executemany(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("active-1", str(active_rollout), 2, "vscode", "/active", "活跃", 0, 2),
            ("archived-1", str(archived_rollout), 1, "vscode", "/old", "归档", 1, 1),
        ],
    )
    connection.commit()
    connection.close()

    bridge = manager(tmp_path)
    assert [item["thread_id"] for item in bridge.list_threads(None)] == ["active-1"]
    assert [item["thread_id"] for item in bridge.list_archived_threads(None)] == [
        "archived-1"
    ]


@pytest.mark.asyncio
async def test_unarchive_is_idempotent_under_concurrent_callbacks(tmp_path):
    archived_dir = tmp_path / "archived_sessions"
    archived_dir.mkdir()
    rollout = archived_dir / "rollout-archived-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "archived-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowAppServer(FakeAppServer):
        async def unarchive_thread(self, thread_id):
            self.unarchived.append(thread_id)
            rollout.unlink()
            entered.set()
            await release.wait()
            return {"id": thread_id}

    app_server = SlowAppServer()
    bridge = manager(tmp_path, app_server_client=app_server)
    first = asyncio.create_task(bridge.unarchive_thread("archived-1"))
    await entered.wait()
    second = asyncio.create_task(bridge.unarchive_thread("archived-1"))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [True, True]
    assert app_server.unarchived == ["archived-1"]
    await bridge.close()


@pytest.mark.asyncio
async def test_close_always_closes_app_server_when_ipc_disconnect_fails(tmp_path):
    class BrokenDisconnectIPC(FakeIPC):
        async def disconnect(self):
            raise RuntimeError("disconnect failed")

    app_server = FakeAppServer()
    bridge = manager(
        tmp_path,
        ipc=BrokenDisconnectIPC(),
        app_server_client=app_server,
    )

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await bridge.close()
    assert app_server.closed


def test_rollout_status_tracks_running_failure_and_recovery(tmp_path):
    bridge = manager(tmp_path)
    rollout_dir = tmp_path / "sessions" / "2026" / "01" / "03"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / "rollout-test-thread-1.jsonl"
    rollout.write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {"id": "thread-1"}}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-1",
        }}),
    ]) + "\n", encoding="utf-8")

    assert bridge._rollout_status(str(rollout)) == "running"

    with rollout.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"type": "response_item", "payload": {
            "type": "message", "text": '示例："type":"task_complete"',
        }}) + "\n")
    assert bridge._rollout_status(str(rollout)) == "running"

    with rollout.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete",
            "turn_id": "turn-1",
            "error": {"message": "failed"},
        }}) + "\n")
    assert bridge._rollout_status(str(rollout)) == "failed"
    assert bridge._seed_state_from_rollout("thread-1")["status"] == "failed"

    with rollout.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-2",
        }}) + "\n")
        target.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-2", "error": {},
        }}) + "\n")
    assert bridge._rollout_status(str(rollout)) == "failed"

    with rollout.open("a", encoding="utf-8") as target:
        target.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-3",
        }}) + "\n")
        target.write(json.dumps({"type": "event_msg", "payload": {
            "type": "turn_aborted", "turn_id": "turn-3",
        }}) + "\n")
    assert bridge._rollout_status(str(rollout)) == "idle"


def test_rollout_seed_preserves_bounded_turn_queries_and_responses(tmp_path):
    bridge = manager(tmp_path)
    rollout_dir = tmp_path / "sessions" / "2026" / "01" / "03"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / "rollout-test-thread-1.jsonl"
    rollout.write_text("\n".join([
        json.dumps({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-1",
        }}),
        json.dumps({"type": "event_msg", "timestamp": "u-1", "payload": {
            "type": "user_message", "message": "第一轮问题",
        }}),
        json.dumps({"type": "event_msg", "timestamp": "a-1", "payload": {
            "type": "agent_message", "phase": "final_answer", "message": "第一轮回答",
        }}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-1",
        }}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-2",
        }}),
        json.dumps({"type": "event_msg", "timestamp": "u-2", "payload": {
            "type": "user_message", "message": "第二轮问题",
        }}),
        json.dumps({"type": "event_msg", "timestamp": "u-3", "payload": {
            "type": "user_message", "message": "第二轮补充",
        }}),
        json.dumps({"type": "event_msg", "timestamp": "a-2", "payload": {
            "type": "agent_message", "phase": "commentary", "message": "第二轮处理中",
        }}),
    ]) + "\n", encoding="utf-8")

    state = bridge._seed_state_from_rollout("thread-1")

    assert state["status"] == "running"
    assert state["active_turn_id"] == "turn-2"
    assert [turn["turn_id"] for turn in state["turns"]] == ["turn-1", "turn-2"]
    assert state["turns"][0]["user_messages"][0]["text"] == "第一轮问题"
    assert state["turns"][0]["agent_messages"][0]["text"] == "第一轮回答"
    assert [message["kind"] for message in state["turns"][1]["user_messages"]] == [
        "initial", "steering",
    ]
    assert state["turns"][1]["agent_messages"][0]["text"] == "第二轮处理中"


def test_rollout_seed_recovers_latest_query_outside_initial_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "ROLLOUT_SEED_TAIL_BYTES", 512)
    monkeypatch.setattr(bridge_module, "ROLLOUT_QUERY_SCAN_BYTES", 16 * 1024)
    rollout_dir = tmp_path / "sessions" / "2026" / "01" / "03"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / "rollout-test-thread-1.jsonl"
    records = [
        {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-source",
        }},
        {"type": "event_msg", "timestamp": "user-1", "payload": {
            "type": "user_message", "message": "超出初始尾窗的 Query",
        }},
        {"type": "event_msg", "timestamp": "user-2", "payload": {
            "type": "user_message", "message": "Query 的补充条件",
        }},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "output": "x" * 4096,
        }},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-source",
        }},
        {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-active",
        }},
        {"type": "event_msg", "timestamp": "agent-live", "payload": {
            "type": "agent_message", "phase": "commentary", "message": "当前轮进度",
        }},
    ]
    rollout.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    bridge = manager(tmp_path)

    state = bridge._seed_state_from_rollout("thread-1")

    assert state["active_turn_id"] == "turn-active"
    assert state["turns"][-1]["turn_id"] == "turn-active"
    assert [message["text"] for message in state["turns"][-1]["user_messages"]] == [
        "超出初始尾窗的 Query", "Query 的补充条件",
    ]
    assert [message["kind"] for message in state["turns"][-1]["user_messages"]] == [
        "initial", "steering",
    ]


def test_live_desktop_state_takes_precedence_over_rollout_status(tmp_path):
    index = tmp_path / "session_index.jsonl"
    index.write_text(json.dumps({
        "id": "desktop-1",
        "thread_name": "运行任务",
        "updated_at": "2026-01-03T00:00:00Z",
    }) + "\n", encoding="utf-8")
    rollout_dir = tmp_path / "sessions" / "2026" / "01" / "03"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-test-desktop-1.jsonl").write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {
            "id": "desktop-1",
            "cwd": "/workspace/desktop",
            "originator": "Codex Desktop",
        }}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-old",
        }}),
    ]) + "\n", encoding="utf-8")
    bridge = manager(tmp_path)
    bridge._states["desktop-1"] = {"status": "waiting_input"}

    assert bridge.list_threads()[0]["status"] == "running"


@pytest.mark.asyncio
async def test_turn_selection_is_per_chat_and_send_returns_to_latest(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    bridge = manager(tmp_path, ipc, cards)
    bridge._bindings = {"chat-1": "thread-1", "chat-2": "thread-1"}
    bridge._states["thread-1"] = {
        "schema_version": 1,
        "schema_known": True,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 2,
        "title": "任务",
        "status": "idle",
        "active_turn_id": None,
        "turns": [
            {
                "turn_id": "turn-1",
                "status": "completed",
                "user_messages": [{"id": "u1", "kind": "initial", "text": "旧问题"}],
                "agent_messages": [{
                    "id": "a1", "turn_id": "turn-1",
                    "phase": "final_answer", "text": "旧回答",
                }],
            },
            {
                "turn_id": "turn-2",
                "status": "completed",
                "user_messages": [{"id": "u2", "kind": "initial", "text": "新问题"}],
                "agent_messages": [{
                    "id": "a2", "turn_id": "turn-2",
                    "phase": "final_answer", "text": "新回答",
                }],
            },
        ],
        "messages": [],
        "pending": None,
    }

    assert await bridge.select_turn("chat-1", "thread-1", "turn-1")
    assert await bridge.select_turn("chat-2", "thread-1", "turn-1")
    assert bridge._turn_views == {"chat-1": "turn-1", "chat-2": "turn-1"}
    rendered = json.dumps(cards.created[-1], ensure_ascii=False)
    assert "旧问题" in rendered and "新问题" not in rendered

    assert await bridge.send_message("chat-1", "继续")
    assert bridge._turn_views == {"chat-2": "turn-1"}
    latest = json.dumps(cards.updated[-1][2], ensure_ascii=False)
    assert "新问题" in latest and "旧问题" not in latest


@pytest.mark.asyncio
async def test_public_turn_only_change_updates_card(tmp_path):
    cards = FakeCardService()
    bridge = manager(tmp_path, cards=cards)
    bridge._bindings = {"chat-1": "thread-1"}
    bridge._states["thread-1"] = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 1,
        "title": "任务",
        "status": "idle",
        "active_turn_id": None,
        "turns": [],
        "messages": [],
        "pending": None,
    }

    await bridge._on_state_change({
        "conversationId": "thread-1",
        "hostId": "local",
        "change": {
            "type": "patches",
            "baseRevision": 1,
            "revision": 2,
            "patches": [{
                "op": "add",
                "path": ["turnHistory", "history", "entitiesByKey", "turn-1"],
                "value": {
                    "turnId": "turn-1",
                    "status": "unknown",
                    "items": [{
                        "id": "user-1",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "只变更 turn"}],
                    }],
                },
            }],
        },
    })

    assert len(cards.created) == 1
    rendered = json.dumps(cards.created[0], ensure_ascii=False)
    assert "只变更 turn" in rendered


@pytest.mark.asyncio
async def test_pending_input_send_also_returns_card_to_latest_turn(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    bridge = manager(tmp_path, ipc, cards)
    bridge._bindings = {"chat-1": "thread-1"}
    bridge._turn_views = {"chat-1": "turn-1"}
    bridge._states["thread-1"] = {
        "schema_version": 1,
        "schema_known": True,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 2,
        "title": "任务",
        "status": "waiting_input",
        "active_turn_id": "turn-2",
        "turns": [
            {
                "turn_id": "turn-1", "status": "completed",
                "user_messages": [{"id": "u1", "kind": "initial", "text": "旧问题"}],
                "agent_messages": [],
            },
            {
                "turn_id": "turn-2", "status": "running",
                "user_messages": [{"id": "u2", "kind": "initial", "text": "新问题"}],
                "agent_messages": [],
            },
        ],
        "messages": [],
        "pending": {
            "kind": "input",
            "request_kind": "user_input",
            "request_id": "request-1",
            "question_id": "question-1",
        },
    }

    assert await bridge.send_message("chat-1", "回答")
    assert bridge._turn_views == {}
    assert ("input", "thread-1", "request-1", {
        "answers": {"question-1": {"answers": ["回答"]}},
    }) in ipc.calls
    rendered = json.dumps(cards.created[-1], ensure_ascii=False)
    assert "新问题" in rendered and "旧问题" not in rendered


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
