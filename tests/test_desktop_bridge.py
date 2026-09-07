import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import lark_client.desktop_bridge as bridge_module
from lark_client.card_service import CardState
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
            error = (
                self.steer_error
                if isinstance(self.steer_error, str)
                else "no active turn"
            )
            raise DesktopIPCRemoteError("thread-follower-steer-turn", error)

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
        self.uploaded_images = []
        self.message_cards = {}
        self.reuse_updates_succeed = True

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

    async def update_and_reuse_message_card(self, chat_id, message_id, content):
        state = self.message_cards.get(message_id)
        if state is None:
            return False
        state.sequence += 1
        self.updated.append((state.card_id, state.sequence, content))
        if not self.reuse_updates_succeed:
            return False
        self.active[chat_id] = state
        return True

    async def upload_image(self, path):
        self.uploaded_images.append(Path(path))
        return "img_v3_test_%d" % len(self.uploaded_images)

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
async def test_attach_reuses_clicked_card_without_creating_another_message(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    notification = CardState(
        card_id="completion-card",
        message_id="completion-message",
    )
    cards.message_cards["completion-message"] = notification
    bridge = manager(tmp_path, ipc, cards)

    assert await bridge.attach(
        "chat-1",
        "user-1",
        "thread-1",
        reuse_message_id="completion-message",
    )

    assert cards.created == []
    assert cards.sent == []
    assert cards.updated[0][0:2] == ("completion-card", 1)
    assert cards.active["chat-1"] is notification
    assert "公开进度" in json.dumps(cards.updated[0][2], ensure_ascii=False)
    await bridge.close()


@pytest.mark.asyncio
async def test_attach_reuse_failure_falls_back_to_one_new_card(tmp_path):
    ipc = FakeIPC()
    cards = FakeCardService()
    cards.message_cards["completion-message"] = CardState(
        card_id="completion-card",
        message_id="completion-message",
    )
    cards.reuse_updates_succeed = False
    bridge = manager(tmp_path, ipc, cards)

    assert await bridge.attach(
        "chat-1",
        "user-1",
        "thread-1",
        reuse_message_id="completion-message",
    )

    assert cards.updated[0][0] == "completion-card"
    assert len(cards.created) == 1
    assert len(cards.sent) == 1
    assert cards.active["chat-1"].card_id == "card-1"
    await bridge.close()


@pytest.mark.asyncio
async def test_card_uploads_local_markdown_image_once_and_never_exposes_path(tmp_path):
    image_path = tmp_path / "generated preview.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    ipc = FakeIPC()
    cards = FakeCardService()
    bridge = manager(tmp_path, ipc, cards)
    assert await bridge.attach("chat-1", "user-1", "thread-1")
    bridge._thread_metadata["thread-1"] = {"cwd": str(tmp_path)}

    event = snapshot()
    event["change"]["conversationState"]["turns"][0]["items"][-1]["text"] = (
        "已生成：\n![预览图](<{}>)".format(image_path)
    )
    await ipc.emit(event)

    rendered = json.dumps(cards.updated[-1][2], ensure_ascii=False)
    assert cards.uploaded_images == [image_path]
    assert '"tag": "img_combination"' in rendered
    assert "img_v3_test_1" in rendered
    assert str(image_path) not in rendered

    assert await bridge.refresh_card("chat-1")
    assert cards.uploaded_images == [image_path]
    await bridge.close()


def test_card_image_path_must_remain_inside_thread_cwd(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    inside = root / "inside.png"
    inside.write_bytes(b"image")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"image")
    escape = root / "escape.png"
    escape.symlink_to(outside)

    assert bridge_module._resolve_card_image_path(str(inside), str(root)) == inside
    assert bridge_module._resolve_card_image_path("inside.png", str(root)) == inside
    assert bridge_module._resolve_card_image_path(str(outside), str(root)) is None
    assert bridge_module._resolve_card_image_path("../outside.png", str(root)) is None
    assert bridge_module._resolve_card_image_path(str(escape), str(root)) is None


@pytest.mark.asyncio
async def test_concurrent_card_refreshes_share_one_image_upload(tmp_path):
    image_path = tmp_path / "shared.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    cards = FakeCardService()
    upload_calls = []

    async def slow_upload(path):
        upload_calls.append(Path(path))
        await asyncio.sleep(0.02)
        return "img_v3_shared"

    cards.upload_image = slow_upload
    bridge = manager(tmp_path, FakeIPC(), cards)
    bridge._thread_metadata["thread-1"] = {"cwd": str(tmp_path)}
    event = snapshot()
    event["change"]["conversationState"]["turns"][0]["items"][-1]["text"] = (
        "![共享图片](<{}>)".format(image_path)
    )
    state = bridge_module.normalize_conversation_state(
        event["change"]["conversationState"], retain_raw=False
    )
    state["thread_id"] = "thread-1"

    first, second = await asyncio.gather(
        bridge._image_keys_for_card(state, None),
        bridge._image_keys_for_card(state, None),
    )

    assert upload_calls == [image_path]
    assert first == second == {str(image_path): "img_v3_shared"}
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


def test_state_db_uses_desktop_session_index_name_instead_of_query_title(tmp_path):
    rollout_dir = tmp_path / "sessions"
    rollout_dir.mkdir()
    rollout = rollout_dir / "rollout-active-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "active-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")
    (tmp_path / "session_index.jsonl").write_text("\n".join([
        json.dumps({
            "id": "active-1",
            "thread_name": "旧名称",
            "updated_at": "2026-01-01T00:00:00Z",
        }),
        json.dumps({
            "id": "active-1",
            "thread_name": "Desktop GUI 名称",
            "updated_at": "2026-01-02T00:00:00Z",
        }),
    ]) + "\n", encoding="utf-8")

    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.execute("""
        CREATE TABLE threads (
            id TEXT, rollout_path TEXT, updated_at INTEGER, source TEXT,
            cwd TEXT, title TEXT, archived INTEGER, recency_at_ms INTEGER,
            name TEXT
        )
    """)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "active-1", str(rollout), 2, "vscode", "/workspace",
            "这其实是首轮 Query，不是会话名", 0, 2, None,
        ),
    )
    connection.commit()
    connection.close()

    threads = manager(tmp_path).list_threads(None)

    assert threads[0]["title"] == "Desktop GUI 名称"


@pytest.mark.asyncio
async def test_app_server_archived_uses_desktop_session_index_name(tmp_path):
    archived_dir = tmp_path / "archived_sessions"
    archived_dir.mkdir()
    rollout = archived_dir / "rollout-archived-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "archived-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")
    (tmp_path / "session_index.jsonl").write_text(json.dumps({
        "id": "archived-1",
        "thread_name": "已归档 GUI 名称",
        "updated_at": "2026-01-02T00:00:00Z",
    }) + "\n", encoding="utf-8")
    app_server = FakeAppServer([{
        "id": "archived-1",
        "title": "归档前的首轮 Query",
        "name": None,
        "cwd": "/workspace",
        "source": "vscode",
        "path": str(rollout),
        "updatedAt": 2,
    }])
    bridge = manager(tmp_path, app_server_client=app_server)

    threads = await bridge.get_archived_threads()

    assert threads[0]["title"] == "已归档 GUI 名称"


def test_state_db_title_is_used_when_session_index_has_no_thread(tmp_path):
    rollout_dir = tmp_path / "sessions"
    rollout_dir.mkdir()
    rollout = rollout_dir / "rollout-active-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "active-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")

    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.execute("""
        CREATE TABLE threads (
            id TEXT, rollout_path TEXT, updated_at INTEGER, source TEXT,
            cwd TEXT, title TEXT, archived INTEGER, recency_at_ms INTEGER,
            name TEXT
        )
    """)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "active-1", str(rollout), 2, "vscode", "/workspace",
            "数据库回退名称", 0, 2, None,
        ),
    )
    connection.commit()
    connection.close()

    assert manager(tmp_path).list_threads(None)[0]["title"] == "数据库回退名称"


def test_empty_session_index_name_falls_back_to_state_db_title(tmp_path):
    rollout_dir = tmp_path / "sessions"
    rollout_dir.mkdir()
    rollout = rollout_dir / "rollout-active-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "active-1", "originator": "Codex Desktop"},
    }) + "\n", encoding="utf-8")
    (tmp_path / "session_index.jsonl").write_text(json.dumps({
        "id": "active-1",
        "thread_name": "",
        "updated_at": "2026-01-02T00:00:00Z",
    }) + "\n", encoding="utf-8")

    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.execute("""
        CREATE TABLE threads (
            id TEXT, rollout_path TEXT, updated_at INTEGER, source TEXT,
            cwd TEXT, title TEXT, archived INTEGER, recency_at_ms INTEGER,
            name TEXT
        )
    """)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "active-1", str(rollout), 2, "vscode", "/workspace",
            "数据库回退名称", 0, 2, None,
        ),
    )
    connection.commit()
    connection.close()

    assert manager(tmp_path).list_threads(None)[0]["title"] == "数据库回退名称"


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
async def test_turn_selection_reuses_clicked_historical_card(tmp_path):
    cards = FakeCardService()
    bridge = manager(tmp_path, cards=cards)
    bridge._bindings = {"chat-1": "thread-1"}
    bridge._states["thread-1"] = {
        "schema_version": 1,
        "schema_known": True,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 2,
        "title": "任务",
        "status": "idle",
        "active_turn_id": None,
        "turns": [{
            "turn_id": "turn-1",
            "status": "completed",
            "user_messages": [{"id": "u1", "kind": "initial", "text": "旧问题"}],
            "agent_messages": [{
                "id": "a1",
                "turn_id": "turn-1",
                "phase": "final_answer",
                "text": "旧回答",
            }],
        }],
        "messages": [],
        "pending": None,
    }
    historical_card = CardState(
        card_id="historical-card",
        message_id="historical-message",
    )
    cards.message_cards["historical-message"] = historical_card

    assert await bridge.select_turn(
        "chat-1",
        "thread-1",
        "turn-1",
        reuse_message_id="historical-message",
    )

    assert cards.created == []
    assert cards.sent == []
    assert cards.updated[0][0:2] == ("historical-card", 1)
    assert cards.active["chat-1"] is historical_card


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
async def test_idle_thread_starts_after_explicit_inactive_steer(tmp_path):
    ipc = FakeIPC()
    bridge = manager(tmp_path, ipc)
    assert await bridge.attach("chat-1", "user-1", "thread-1")
    await ipc.emit(snapshot(status="completed"))
    ipc.steer_error = "NoActiveTurn"

    assert await bridge.send_message("chat-1", "下一轮")

    assert any(call[0:3] == ("start", "thread-1", "下一轮") for call in ipc.calls)
    assert any(call[0] == "steer" for call in ipc.calls)
    await bridge.close()


@pytest.mark.asyncio
async def test_inactive_steer_falls_back_to_start_but_other_errors_do_not(tmp_path):
    ipc = FakeIPC()
    bridge = manager(tmp_path, ipc)
    assert await bridge.attach("chat-1", "user-1", "thread-1")
    await ipc.emit(snapshot(status="inProgress"))

    ipc.steer_error = "active turn already ended"
    assert await bridge.send_message("chat-1", "新一轮")
    assert any(call[0:3] == ("start", "thread-1", "新一轮") for call in ipc.calls)

    ipc.calls.clear()
    ipc.steer_error = "permission denied"
    assert not await bridge.send_message("chat-1", "不要降级")
    assert not any(call[0] == "start" for call in ipc.calls)
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


@pytest.mark.parametrize("marker", [
    {"source": "subAgentThreadSpawn"},
    {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}},
    {"source": json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root"}}})},
    {"thread_source": "subagent"},
    {"threadSource": "subagent"},
    {"parent_thread_id": "root"},
])
def test_catalog_rejects_structured_subagent_sources(tmp_path, marker):
    bridge = manager(tmp_path)
    path = tmp_path / "child.jsonl"
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "child-1", "originator": "Codex Desktop", **marker},
    }) + "\n")
    rows = [{"id": "child-1", "path": str(path), "source": "vscode"}]
    for archived in (False, True):
        assert bridge._normalize_catalog_rows(rows, archived=archived, include_internal=True) == []
        assert bridge._normalize_catalog_rows(
            [{"id": "child-1", **marker}], archived=archived, include_internal=True,
        ) == []


def test_catalog_rejects_root_row_pointing_to_another_rollout(tmp_path):
    path = tmp_path / "child.jsonl"
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "child-1", "session_id": "root-1", "originator": "Codex Desktop"},
    }) + "\n")
    assert manager(tmp_path)._normalize_catalog_rows(
        [{"id": "root-1", "path": str(path), "source": "vscode"}],
        archived=False, include_internal=True,
    ) == []


def test_rollout_metadata_never_aliases_child_session_id_to_root(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "rollout-test-root-1.jsonl"
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "child-1", "session_id": "root-1", "originator": "Codex Desktop",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root-1"}}},
        },
    }) + "\n")
    bridge = manager(tmp_path)
    assert bridge._rollout_metadata({"root-1"}) == {}
    assert bridge._seed_state_from_rollout("root-1")["status"] == "unknown"


def _subagent_activity(turn_id, kind, *, root="thread-1", child="child-1"):
    return {"type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": root, "turn_id": turn_id,
        "item": {
            "type": "SubAgentActivity", "id": "activity-" + kind,
            "agent_thread_id": child, "agent_path": "/root/check", "kind": kind,
            "prompt": "PRIVATE_PROMPT", "output": "PRIVATE_OUTPUT",
        },
    }}


def test_rollout_subagents_stay_in_parent_turn_and_do_not_complete_parent(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-old"}},
        _subagent_activity("turn-old", "started"),
        _subagent_activity("turn-old", "completed"),
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-old"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}},
        _subagent_activity("turn-live", "interacted"),
        _subagent_activity("turn-live", "completed", root="another-root", child="foreign-child"),
    ]
    (sessions / "rollout-test-thread-1.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n"
    )
    bridge = manager(tmp_path)
    monkeypatch.setattr(bridge._subagent_status_reader, "get_status", lambda *args: "running")
    state = bridge._seed_state_from_rollout("thread-1")
    assert state["status"] == "running"
    assert state["active_turn_id"] == "turn-live"
    assert state["turns"][0]["sub_agents"] == [{
        "thread_id": "child-1", "agent_path": "/root/check", "status": "completed",
    }]
    assert state["turns"][1]["sub_agents"] == [{
        "thread_id": "child-1", "agent_path": "/root/check", "status": "running",
    }]
    rendered = json.dumps(state)
    assert "PRIVATE_" not in rendered
    assert "foreign-child" not in rendered


def _subagent_parent_state():
    return {
        "schema_version": 1, "schema_known": True, "thread_id": "thread-1",
        "title": "主任务", "status": "running", "active_turn_id": "turn-live",
        "messages": [], "pending": None,
        "turns": [{
            "turn_id": turn_id, "status": status,
            "user_messages": [], "agent_messages": [],
            "sub_agents": [{"thread_id": "child-1", "agent_path": "/root/check", "status": status}],
        } for turn_id, status in (("turn-old", "completed"), ("turn-live", "running"))],
    }


@pytest.mark.parametrize("child_status", ["running", "completed", "failed", "interrupted", None])
def test_child_status_enrichment_is_latest_only_and_does_not_mutate_parent(tmp_path, monkeypatch, child_status):
    bridge = manager(tmp_path)
    monkeypatch.setattr(bridge._subagent_status_reader, "get_status", lambda *args: child_status)
    original = _subagent_parent_state()
    saved = copy.deepcopy(original)
    enriched = bridge._with_subagent_statuses(original)
    assert original == saved
    assert enriched["status"] == "running"
    assert enriched["active_turn_id"] == "turn-live"
    assert enriched["turns"][0]["sub_agents"][0]["status"] == "completed"
    assert enriched["turns"][1]["sub_agents"][0]["status"] == (child_status or "unknown")


@pytest.mark.asyncio
async def test_child_completion_refresh_reuses_cards_and_never_sends_notification(tmp_path, monkeypatch):
    cards = FakeCardService()
    bridge = manager(tmp_path, cards=cards)
    bridge._bindings = {"history-chat": "thread-1", "live-chat": "thread-1"}
    bridge._turn_views = {"history-chat": "turn-old"}
    bridge._states["thread-1"] = _subagent_parent_state()
    for chat_id in bridge._bindings:
        cards.active[chat_id] = CardState(card_id=chat_id, message_id=chat_id)
    monkeypatch.setattr(bridge._subagent_status_reader, "get_status", lambda *args: "failed")
    await bridge._refresh_subagent_states()
    assert len(cards.updated) == 2
    assert cards.created == cards.sent == cards.user_cards == []
    state = bridge._states["thread-1"]
    assert state["status"] == "running"
    assert state["turns"][0]["sub_agents"][0]["status"] == "completed"
    assert state["turns"][1]["sub_agents"][0]["status"] == "failed"
    rendered = {card_id: json.dumps(card, ensure_ascii=False) for card_id, _, card in cards.updated}
    assert "check · 已完成" in rendered["history-chat"]
    assert "check · 异常" not in rendered["history-chat"]
    assert "check · 异常" in rendered["live-chat"]
    await bridge._refresh_subagent_states()
    assert len(cards.updated) == 2


@pytest.mark.asyncio
async def test_subagent_refresh_task_is_singleton_and_closes(tmp_path):
    bridge = manager(tmp_path)
    bridge._started = True
    bridge._bindings = {"chat-1": "thread-1"}
    bridge._states["thread-1"] = _subagent_parent_state()
    bridge._ensure_subagent_refresh_started()
    task = bridge._subagent_refresh_task
    assert task is not None
    bridge._ensure_subagent_refresh_started()
    assert bridge._subagent_refresh_task is task
    await bridge.close()
    assert task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_only", [False, True])
async def test_slow_subagent_lookup_cannot_drop_concurrent_ipc_patch(tmp_path, monkeypatch, reasoning_only):
    bridge = manager(tmp_path)
    bridge._bindings = {"chat-1": "thread-1"}
    bridge._publish_card = AsyncMock(return_value=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_lookup(function, state):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return dict(state)

    monkeypatch.setattr(bridge_module.asyncio, "to_thread", delayed_lookup)
    first = asyncio.create_task(bridge._on_state_change(snapshot()))
    await entered.wait()
    patch = (
        {"op": "replace", "path": ["turns", 0, "items", 0, "summary"], "value": ["PRIVATE_REASONING"]}
        if reasoning_only else
        {"op": "add", "path": ["turns", 0, "items", 2], "value": {
            "id": "agent-2", "type": "agentMessage", "phase": "commentary", "text": "后续进度",
        }}
    )
    await bridge._on_state_change({"conversationId": "thread-1", "change": {
        "type": "patches", "baseRevision": 1, "revision": 2,
        "patches": [patch],
    }})
    release.set()
    await first
    state = bridge._states["thread-1"]
    assert state["revision"] == 2
    assert [message["text"] for message in state["messages"]] == (
        ["公开进度"] if reasoning_only else ["公开进度", "后续进度"]
    )
    bridge._publish_card.assert_awaited_once()
    assert bridge._publish_card.await_args.args[1]["revision"] == 2
