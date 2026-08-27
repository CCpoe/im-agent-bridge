import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import lark_client.lark_handler as handler_module
from lark_client.lark_handler import LarkHandler


class FakeDesktop:
    def __init__(self, *, attached=False):
        self.started = False
        self.has_bindings = attached
        self.attached = attached
        self.sent = []
        self.attached_threads = []
        self.detached = []
        self.stopped = []
        self.list_limits = []
        self.notification_targets = []
        self.archived = False
        self.unarchived = []
        self.turn_pages = []
        self.attach_kwargs = []

    async def start(self):
        self.started = True
        return True

    def is_attached(self, chat_id):
        return self.attached

    def binding_for(self, chat_id):
        return "thread-1" if self.attached else None

    def state_for_chat(self, chat_id):
        return None

    def list_threads(self, limit=20):
        self.list_limits.append(limit)
        return [{"thread_id": "thread-1", "title": "Desktop task"}]

    async def get_archived_threads(self):
        return [{"thread_id": "archived-1", "title": "Archived task"}]

    async def register_notification_target(self, user_id):
        self.notification_targets.append(user_id)
        return True

    def is_archived_thread(self, thread_id):
        return self.archived

    async def unarchive_thread(self, thread_id):
        self.unarchived.append(thread_id)
        self.archived = False
        return True

    async def select_turn(self, chat_id, expected_thread_id, target_turn_id):
        self.turn_pages.append((chat_id, expected_thread_id, target_turn_id))
        return True

    async def send_message(self, chat_id, text, client_message_id=None):
        self.sent.append((chat_id, text, client_message_id))
        return True

    async def attach(self, chat_id, user_id, thread_id, **kwargs):
        self.attached = True
        self.attached_threads.append((chat_id, user_id, thread_id))
        self.attach_kwargs.append(kwargs)
        return True

    async def detach(self, chat_id):
        self.attached = False
        self.detached.append(chat_id)

    async def interrupt(self, chat_id, expected_turn_id=None):
        self.stopped.append((chat_id, expected_turn_id))
        return True

    async def close(self):
        pass


def make_handler(desktop):
    handler = LarkHandler.__new__(LarkHandler)
    handler._desktop = desktop
    handler._desktop_start_task = None
    handler._desktop_attaching_chats = set()
    handler._health_check_task = MagicMock()
    handler._health_check_task.done.return_value = False
    handler._bridges = {}
    handler._chat_sessions = {}
    handler._chat_bindings = {}
    handler._detached_slices = {}
    handler._poller = MagicMock()
    handler._starting_sessions = set()
    handler._group_chat_ids = set()
    handler._save_chat_bindings = MagicMock()
    return handler


@pytest.mark.asyncio
async def test_plain_message_routes_to_attached_desktop(monkeypatch):
    desktop = FakeDesktop(attached=True)
    handler = make_handler(desktop)
    monkeypatch.setattr(handler_module, "_track_stats", lambda *args, **kwargs: None)

    await handler.handle_message("user-1", "chat-1", "继续执行", chat_type="p2p")

    assert desktop.sent == [("chat-1", "继续执行", None)]


@pytest.mark.asyncio
async def test_desktop_command_attaches_codex_uri(monkeypatch):
    desktop = FakeDesktop()
    handler = make_handler(desktop)
    monkeypatch.setattr(handler_module, "_track_stats", lambda *args, **kwargs: None)

    await handler.handle_message(
        "user-1",
        "chat-1",
        "/desktop codex://threads/thread-1",
        chat_type="p2p",
    )

    assert desktop.attached_threads == [
        ("chat-1", "user-1", "codex://threads/thread-1")
    ]


@pytest.mark.asyncio
async def test_desktop_list_passes_page_and_loads_enough_threads(monkeypatch):
    desktop = FakeDesktop()
    handler = make_handler(desktop)
    handler._send_or_update_card = AsyncMock()

    await handler._cmd_desktop_list(
        "user-1", "chat-1", message_id="message-1", page=3
    )

    assert desktop.list_limits == [None]
    assert desktop.notification_targets == ["user-1"]
    handler._send_or_update_card.assert_awaited_once()
    chat_id, card, message_id = handler._send_or_update_card.await_args.args
    assert chat_id == "chat-1"
    assert message_id == "message-1"
    assert "第 1/1 页" in str(card)


@pytest.mark.asyncio
async def test_archived_list_and_unarchive_refresh_in_place():
    desktop = FakeDesktop()
    handler = make_handler(desktop)
    handler._send_or_update_card = AsyncMock()

    await handler._cmd_desktop_archived(
        "user-1", "chat-1", message_id="message-1", page=2
    )

    assert desktop.notification_targets == ["user-1"]
    chat_id, card, message_id = handler._send_or_update_card.await_args.args
    assert (chat_id, message_id) == ("chat-1", "message-1")
    assert "Codex Desktop 已归档" in str(card)

    handler._cmd_desktop_archived = AsyncMock()
    await handler._cmd_desktop_unarchive(
        "user-1", "chat-1", "archived-1", message_id="message-1", page=2
    )
    assert desktop.unarchived == ["archived-1"]
    handler._cmd_desktop_archived.assert_awaited_once_with(
        "user-1", "chat-1", message_id="message-1", page=2
    )


@pytest.mark.asyncio
async def test_archived_session_is_unarchived_before_attach(monkeypatch):
    desktop = FakeDesktop()
    desktop.archived = True
    handler = make_handler(desktop)
    monkeypatch.setattr(handler_module, "_track_stats", lambda *args, **kwargs: None)

    await handler._cmd_desktop_attach("user-1", "chat-1", "archived-1")

    assert desktop.unarchived == ["archived-1"]
    assert desktop.attached_threads == [("chat-1", "user-1", "archived-1")]


@pytest.mark.asyncio
async def test_desktop_attach_loads_cold_task_with_bundle_id_and_retries(monkeypatch):
    desktop = FakeDesktop()
    desktop.attach = AsyncMock(side_effect=[False, False, True])
    handler = make_handler(desktop)
    popen = MagicMock()
    sleep = AsyncMock()
    monkeypatch.setattr(handler_module.sys, "platform", "darwin")
    monkeypatch.setattr(handler_module.subprocess, "Popen", popen)
    monkeypatch.setattr(handler_module.asyncio, "sleep", sleep)

    await handler._cmd_desktop_attach("user-1", "chat-1", "thread-cold")

    assert desktop.attach.await_count == 3
    assert [call.args for call in desktop.attach.await_args_list] == [
        ("chat-1", "user-1", "thread-cold"),
        ("chat-1", "user-1", "thread-cold"),
        ("chat-1", "user-1", "thread-cold"),
    ]
    assert [call.kwargs for call in desktop.attach.await_args_list] == [
        {"owner_timeout": 0.75},
        {"owner_timeout": 0.75},
        {"owner_timeout": 0.75},
    ]
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0]
    popen.assert_called_once_with(
        [
            "open",
            "-b",
            "com.openai.codex",
            "codex://threads/thread-cold",
        ],
        stdout=handler_module.subprocess.DEVNULL,
        stderr=handler_module.subprocess.DEVNULL,
    )


@pytest.mark.asyncio
async def test_duplicate_desktop_attach_is_ignored_while_first_is_running():
    desktop = FakeDesktop()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_attach(*args, **kwargs):
        entered.set()
        await release.wait()
        return True

    desktop.attach = AsyncMock(side_effect=slow_attach)
    handler = make_handler(desktop)
    first = asyncio.create_task(
        handler._cmd_desktop_attach("user-1", "chat-1", "thread-1")
    )
    await entered.wait()
    await handler._cmd_desktop_attach("user-1", "chat-1", "thread-1")
    release.set()
    await first

    assert desktop.attach.await_count == 1


@pytest.mark.asyncio
async def test_desktop_turn_page_routes_to_bound_thread():
    desktop = FakeDesktop(attached=True)
    handler = make_handler(desktop)

    await handler.handle_desktop_turn_page("chat-1", "thread-1", "turn-old")

    assert desktop.turn_pages == [("chat-1", "thread-1", "turn-old")]


@pytest.mark.asyncio
async def test_escape_interrupts_desktop_instead_of_sending_terminal_key():
    desktop = FakeDesktop(attached=True)
    handler = make_handler(desktop)

    await handler.send_raw_key("user-1", "chat-1", "esc")

    assert desktop.stopped == [("chat-1", None)]


@pytest.mark.asyncio
async def test_stale_desktop_card_cannot_send_to_new_binding(monkeypatch):
    desktop = FakeDesktop(attached=True)
    handler = make_handler(desktop)
    fake_cards = MagicMock()
    fake_cards.send_text = AsyncMock()
    monkeypatch.setattr(handler_module, "card_service", fake_cards)

    await handler.forward_to_desktop(
        "user-1", "chat-1", "old-thread", "不应发送"
    )

    assert desktop.sent == []
    fake_cards.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_cli_attach_preserves_desktop_binding(monkeypatch):
    desktop = FakeDesktop(attached=True)
    handler = make_handler(desktop)

    class DisconnectedBridge:
        def __init__(self, *args, **kwargs):
            self.running = False

        async def connect(self):
            return False

        async def disconnect(self):
            pass

    monkeypatch.setattr(handler_module, "SessionBridge", DisconnectedBridge)

    result = await handler._attach("chat-1", "missing", user_id="user-1")

    assert result is False
    assert desktop.detached == []
