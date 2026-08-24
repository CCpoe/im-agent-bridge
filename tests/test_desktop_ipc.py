import asyncio
import inspect
import json
import struct
from pathlib import Path

import pytest

from lark_client.desktop_ipc import (
    DesktopIPCClient,
    DesktopIPCProtocolError,
    DesktopIPCTimeoutError,
    METHOD_COMMAND_APPROVAL,
    METHOD_FILE_APPROVAL,
    METHOD_INTERRUPT_TURN,
    METHOD_LOAD_COMPLETE_HISTORY,
    METHOD_OWNER_DISCOVERY,
    METHOD_PERMISSIONS_APPROVAL,
    METHOD_START_TURN,
    METHOD_STEER_TURN,
    METHOD_SUBMIT_MCP_ELICITATION,
    METHOD_SUBMIT_USER_INPUT,
    THREAD_INTERRUPT_VERSION,
    THREAD_INTERRUPT_LEGACY_VERSION,
    encode_frame,
    read_frame,
)


def decode_frame(data):
    size = struct.unpack("<I", data[:4])[0]
    assert size == len(data) - 4
    return json.loads(data[4:].decode("utf-8"))


class FakeWriter:
    def __init__(self, peer, reader):
        self.peer = peer
        self.reader = reader
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ConnectionError("closed")
        self.peer.receive(decode_frame(data), self.reader)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class FakePeer:
    def __init__(self, *, auto_respond=True):
        self.auto_respond = auto_respond
        self.messages = []
        self.readers = []
        self.connection_count = 0

    async def connect(self, path):
        assert path == "/does/not/connect/real/codex.sock"
        self.connection_count += 1
        reader = asyncio.StreamReader()
        self.readers.append(reader)
        return reader, FakeWriter(self, reader)

    def receive(self, message, reader):
        self.messages.append(message)
        if message.get("type") != "request":
            return
        if message["method"] == "initialize":
            self.respond(
                message,
                reader=reader,
                result={"clientId": "sidecar-%d" % self.connection_count},
                handled_by="broker",
            )
        elif self.auto_respond:
            handled_by = (
                "desktop-owner"
                if message["method"] == METHOD_OWNER_DISCOVERY
                else message.get("targetClientId", "desktop-owner")
            )
            self.respond(
                message,
                reader=reader,
                result={"ok": True},
                handled_by=handled_by,
            )

    def respond(
        self,
        request,
        *,
        reader=None,
        result=None,
        handled_by="desktop-owner",
        result_type="success",
        error=None,
    ):
        response = {
            "type": "response",
            "requestId": request["requestId"],
            "resultType": result_type,
            "method": request["method"],
            "handledByClientId": handled_by,
        }
        if result_type == "success":
            response["result"] = {} if result is None else result
        else:
            response["error"] = error
        (reader or self.readers[-1]).feed_data(encode_frame(response))

    def push(self, message, reader=None):
        (reader or self.readers[-1]).feed_data(encode_frame(message))

    def requests(self, method=None):
        result = [m for m in self.messages if m.get("type") == "request"]
        if method is not None:
            result = [m for m in result if m.get("method") == method]
        return result


def make_client(peer, **kwargs):
    return DesktopIPCClient(
        Path("/does/not/connect/real/codex.sock"),
        connector=peer.connect,
        reconnect_delay=0,
        max_reconnect_delay=0,
        request_timeout=0.2,
        connect_timeout=0.2,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_little_endian_json_framing_and_size_limit():
    message = {"type": "broadcast", "params": {"text": "飞书"}}
    frame = encode_frame(message)
    assert struct.unpack("<I", frame[:4])[0] == len(frame) - 4

    reader = asyncio.StreamReader()
    reader.feed_data(frame[:3])
    read_task = asyncio.create_task(read_frame(reader))
    await asyncio.sleep(0)
    assert not read_task.done()
    reader.feed_data(frame[3:])
    assert await read_task == message

    oversized = asyncio.StreamReader()
    oversized.feed_data(struct.pack("<I", 11))
    with pytest.raises(DesktopIPCProtocolError):
        await read_frame(oversized, max_message_size=10)
    with pytest.raises(DesktopIPCProtocolError):
        encode_frame({"payload": "too large"}, max_message_size=5)


@pytest.mark.asyncio
async def test_initialize_and_automatic_negative_discovery_response():
    peer = FakePeer()
    client = make_client(peer)
    assert await client.connect() == "sidecar-1"

    initialize = peer.requests("initialize")[0]
    assert initialize["sourceClientId"] == "initializing-client"
    assert initialize["version"] == 0
    assert initialize["params"] == {"clientType": "feishu-sidecar"}

    peer.push(
        {
            "type": "client-discovery-request",
            "requestId": "discovery-1",
            "request": {"method": METHOD_OWNER_DISCOVERY},
        }
    )
    await asyncio.sleep(0.01)
    assert peer.messages[-1] == {
        "type": "client-discovery-response",
        "requestId": "discovery-1",
        "response": {"canHandle": False},
    }
    await client.disconnect()


@pytest.mark.asyncio
async def test_concurrent_request_correlation_and_timeout_cleanup():
    peer = FakePeer(auto_respond=False)
    client = make_client(peer)
    await client.connect()

    first_task = asyncio.create_task(client.request("first", {"n": 1}))
    second_task = asyncio.create_task(client.request("second", {"n": 2}))
    await asyncio.sleep(0.01)
    first = peer.requests("first")[0]
    second = peer.requests("second")[0]

    peer.respond(second, result={"value": 2})
    peer.respond(first, result={"value": 1})
    assert await first_task == {"value": 1}
    assert await second_task == {"value": 2}

    with pytest.raises(DesktopIPCTimeoutError):
        await client.request("never-answered", timeout=0.01)
    assert client._pending == {}
    await client.disconnect()


@pytest.mark.asyncio
async def test_owner_follow_start_and_steer_wire_protocol(monkeypatch):
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()

    assert await client.discover_owner("thread-1") == "desktop-owner"
    owner_request = peer.requests(METHOD_OWNER_DISCOVERY)[0]
    assert owner_request["version"] == 1
    assert owner_request["params"] == {
        "hostId": "local",
        "conversationId": "thread-1",
    }

    await client.follow("thread-1")
    follow = peer.messages[-1]
    assert follow == {
        "type": "broadcast",
        "method": "thread-stream-following-changed",
        "sourceClientId": "sidecar-1",
        "params": {
            "conversationId": "thread-1",
            "hostId": "local",
            "following": True,
        },
        "version": 1,
    }

    await client.start_turn("thread-1", "开始处理")
    start = peer.requests(METHOD_START_TURN)[0]
    assert start["targetClientId"] == "desktop-owner"
    assert start["params"] == {
        "conversationId": "thread-1",
        "turnStartParams": {
            "input": [
                {"type": "text", "text": "开始处理", "text_elements": []}
            ]
        },
    }

    monkeypatch.setattr("lark_client.desktop_ipc.time.time", lambda: 1234.5)
    await client.steer_turn(
        "thread-1",
        "补充要求",
        "/workspace",
        client_user_message_id="user-message-id",
    )
    steer = peer.requests(METHOD_STEER_TURN)[0]
    params = steer["params"]
    assert steer["targetClientId"] == "desktop-owner"
    assert params["clientUserMessageId"] == "user-message-id"
    assert params["input"] == [
        {"type": "text", "text": "补充要求", "text_elements": []}
    ]
    assert params["attachments"] == []
    assert params["restoreMessage"]["text"] == "补充要求"
    assert params["restoreMessage"]["id"] == "user-message-id"
    assert params["restoreMessage"]["cwd"] == "/workspace"
    assert params["restoreMessage"]["createdAt"] == 1234500
    assert params["restoreMessage"]["context"]["workspaceRoots"] == [
        "/workspace"
    ]
    await client.disconnect()


@pytest.mark.asyncio
async def test_snapshot_and_immer_patch_callbacks_build_cached_state():
    peer = FakePeer()
    callback_events = []
    client = make_client(peer, on_state_change=callback_events.append)
    await client.connect()

    snapshot = {
        "type": "broadcast",
        "method": "thread-stream-state-changed",
        "sourceClientId": "desktop-owner",
        "version": 11,
        "params": {
            "conversationId": "thread-1",
            "hostId": "local",
            "change": {
                "type": "snapshot",
                "revision": 4,
                "conversationState": {
                    "status": "inProgress",
                    "items": [{"text": "old"}],
                },
            },
        },
    }
    peer.push(snapshot)
    peer.push(
        {
            "type": "broadcast",
            "method": "thread-stream-state-changed",
            "sourceClientId": "desktop-owner",
            "version": 11,
            "params": {
                "conversationId": "thread-1",
                "hostId": "local",
                "change": {
                    "type": "patches",
                    "baseRevision": 4,
                    "revision": 5,
                    "patches": [
                        {
                            "op": "replace",
                            "path": ["items", 0, "text"],
                            "value": "new",
                        },
                        {
                            "op": "add",
                            "path": ["items", 1],
                            "value": {"text": "second"},
                        },
                        {"op": "remove", "path": ["status"]},
                    ],
                },
            },
        }
    )
    await asyncio.sleep(0.01)

    assert [event["change"]["type"] for event in callback_events] == [
        "snapshot",
        "patches",
    ]
    assert client.state_for("thread-1") == {
        "revision": 5,
        "state": {"items": [{"text": "new"}, {"text": "second"}]},
    }
    await client.disconnect()


@pytest.mark.asyncio
async def test_interrupt_and_all_interactive_response_methods():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()
    owner = "desktop-owner"

    await client.interrupt(
        "thread-1",
        owner_client_id=owner,
        expected_turn_id="turn-1",
    )
    await client.command_approval(
        "thread-1", "command-1", "accept", owner_client_id=owner
    )
    await client.file_approval(
        "thread-1", "file-1", "decline", owner_client_id=owner
    )
    await client.permissions_approval(
        "thread-1", "permissions-1", {"approved": True}, owner_client_id=owner
    )
    await client.submit_user_input(
        "thread-1", "input-1", {"answer": "yes"}, owner_client_id=owner
    )
    await client.submit_mcp_server_elicitation_response(
        "thread-1", "mcp-1", {"action": "accept"}, owner_client_id=owner
    )

    action_requests = peer.requests()[1:]
    assert [request["method"] for request in action_requests] == [
        METHOD_INTERRUPT_TURN,
        METHOD_COMMAND_APPROVAL,
        METHOD_FILE_APPROVAL,
        METHOD_PERMISSIONS_APPROVAL,
        METHOD_SUBMIT_USER_INPUT,
        METHOD_SUBMIT_MCP_ELICITATION,
    ]
    assert action_requests[0]["version"] == THREAD_INTERRUPT_VERSION
    assert all(request["version"] == 1 for request in action_requests[1:])
    assert all(request["targetClientId"] == owner for request in action_requests)
    assert action_requests[0]["params"] == {
        "conversationId": "thread-1",
        "mode": "user-stop",
        "expectedTurnId": "turn-1",
    }
    assert action_requests[1]["params"]["decision"] == "accept"
    assert action_requests[2]["params"]["decision"] == "decline"
    assert action_requests[3]["params"]["response"] == {"approved": True}
    assert action_requests[4]["params"]["response"] == {"answer": "yes"}
    assert action_requests[5]["params"]["response"] == {"action": "accept"}
    await client.disconnect()


@pytest.mark.asyncio
async def test_interrupt_without_expected_turn_uses_legacy_v3():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()

    await client.interrupt("thread-1", owner_client_id="desktop-owner")

    request = peer.requests(METHOD_INTERRUPT_TURN)[0]
    assert request["version"] == THREAD_INTERRUPT_LEGACY_VERSION
    assert "expectedTurnId" not in request["params"]
    await client.disconnect()


@pytest.mark.asyncio
async def test_load_complete_history_uses_owner_and_long_default_timeout():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()

    result = await client.load_complete_history(
        "thread-1", owner_client_id="desktop-owner"
    )
    request = peer.requests(METHOD_LOAD_COMPLETE_HISTORY)[0]
    assert result == {"ok": True}
    assert request["version"] == 1
    assert request["targetClientId"] == "desktop-owner"
    assert request["params"] == {"conversationId": "thread-1"}
    assert (
        inspect.signature(client.load_complete_history)
        .parameters["timeout"]
        .default
        == 305.0
    )
    await client.disconnect()


@pytest.mark.asyncio
async def test_stale_owner_is_rediscovered_once_for_write_request():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()
    assert await client.discover_owner("thread-1") == "desktop-owner"
    peer.auto_respond = False

    task = asyncio.create_task(client.start_turn("thread-1", "hello"))
    await asyncio.sleep(0.01)
    first = peer.requests(METHOD_START_TURN)[0]
    peer.respond(first, result_type="error", error="no-client-found")

    await asyncio.sleep(0.01)
    discovery = peer.requests(METHOD_OWNER_DISCOVERY)[-1]
    peer.respond(discovery, result={}, handled_by="desktop-owner-2")

    await asyncio.sleep(0.01)
    retry = peer.requests(METHOD_START_TURN)[-1]
    assert retry["targetClientId"] == "desktop-owner-2"
    peer.respond(retry, result={"ok": True}, handled_by="desktop-owner-2")
    assert await task == {"ok": True}
    await client.disconnect()


@pytest.mark.asyncio
async def test_unexpected_eof_reconnects_and_reinitializes():
    peer = FakePeer()
    client = make_client(peer)
    assert await client.connect() == "sidecar-1"

    peer.readers[0].feed_eof()
    for _ in range(50):
        if peer.connection_count >= 2 and client.connected:
            break
        await asyncio.sleep(0.01)

    assert peer.connection_count == 2
    assert client.client_id == "sidecar-2"
    assert len(peer.requests("initialize")) == 2
    await client.disconnect()


@pytest.mark.asyncio
async def test_reconnect_rediscovers_owner_and_restores_following():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()
    await client.follow("thread-1")
    assert len(
        [
            message
            for message in peer.messages
            if message.get("method") == "thread-stream-following-changed"
        ]
    ) == 1

    peer.readers[0].feed_eof()
    for _ in range(50):
        follows = [
            message
            for message in peer.messages
            if message.get("method") == "thread-stream-following-changed"
        ]
        if peer.connection_count >= 2 and len(follows) >= 2 and client.connected:
            break
        await asyncio.sleep(0.01)

    owner_requests = peer.requests(METHOD_OWNER_DISCOVERY)
    assert len(owner_requests) == 0
    assert len(follows) == 2
    assert follows[-1]["sourceClientId"] == "sidecar-2"
    assert "targetClientIds" not in follows[-1]
    assert follows[-1]["params"]["following"] is True
    await client.disconnect()


@pytest.mark.asyncio
async def test_revision_gap_throttles_snapshot_refollow_request():
    peer = FakePeer()
    callback_events = []
    client = make_client(
        peer,
        on_state_change=callback_events.append,
        resync_delay=0,
        resync_cooldown=1,
    )
    await client.connect()
    await client.follow("thread-1")

    gap_broadcast = {
        "type": "broadcast",
        "method": "thread-stream-state-changed",
        "sourceClientId": "desktop-owner",
        "version": 11,
        "params": {
            "conversationId": "thread-1",
            "hostId": "local",
            "change": {
                "type": "patches",
                "baseRevision": 50,
                "revision": 51,
                "patches": [
                    {"op": "replace", "path": ["status"], "value": "done"}
                ],
            },
        },
    }
    # 两个连续 gap 只应安排一次 owner discovery + following:true。
    peer.push(gap_broadcast)
    peer.push(gap_broadcast)
    await asyncio.sleep(0.02)

    owner_requests = peer.requests(METHOD_OWNER_DISCOVERY)
    follows = [
        message
        for message in peer.messages
        if message.get("method") == "thread-stream-following-changed"
    ]
    assert len(owner_requests) == 0  # gap resync 是无定向广播，无需 discovery
    assert len(follows) == 2
    assert follows[-1]["params"]["following"] is True
    assert "targetClientIds" not in follows[-1]
    assert client.state_for("thread-1") is None
    assert len(callback_events) == 2
    await client.disconnect()


@pytest.mark.asyncio
async def test_following_status_request_is_answered_only_for_followed_thread():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()
    await client.follow("thread-1")
    initial_message_count = len(peer.messages)

    peer.push(
        {
            "type": "broadcast",
            "method": "thread-stream-following-status-requested",
            "sourceClientId": "desktop-owner-new",
            "version": 1,
            "params": {"conversationId": "thread-1", "hostId": "local"},
        }
    )
    await asyncio.sleep(0.01)
    reply = peer.messages[-1]
    assert len(peer.messages) == initial_message_count + 1
    assert reply["method"] == "thread-stream-following-changed"
    assert reply["targetClientIds"] == ["desktop-owner-new"]
    assert reply["params"] == {
        "conversationId": "thread-1",
        "hostId": "local",
        "following": True,
    }

    message_count = len(peer.messages)
    peer.push(
        {
            "type": "broadcast",
            "method": "thread-stream-following-status-requested",
            "sourceClientId": "desktop-owner-new",
            "version": 1,
            "params": {"conversationId": "not-followed", "hostId": "local"},
        }
    )
    await asyncio.sleep(0.01)
    assert len(peer.messages) == message_count
    await client.disconnect()


@pytest.mark.asyncio
async def test_state_broadcast_refreshes_owner_client_id():
    peer = FakePeer()
    client = make_client(peer)
    await client.connect()
    await client.follow("thread-1")

    peer.push(
        {
            "type": "broadcast",
            "method": "thread-stream-state-changed",
            "sourceClientId": "replacement-owner",
            "version": 11,
            "params": {
                "conversationId": "thread-1",
                "hostId": "local",
                "change": {
                    "type": "snapshot",
                    "revision": 1,
                    "conversationState": {"status": "idle"},
                },
            },
        }
    )
    await asyncio.sleep(0.01)
    await client.start_turn("thread-1", "继续")
    assert (
        peer.requests(METHOD_START_TURN)[-1]["targetClientId"]
        == "replacement-owner"
    )
    await client.disconnect()


@pytest.mark.asyncio
async def test_missing_snapshot_stops_resync_after_two_attempts():
    peer = FakePeer()
    client = make_client(
        peer,
        resync_delay=0,
        resync_cooldown=0,
    )
    await client.connect()
    await client.follow("thread-1")

    gap = {
        "type": "broadcast",
        "method": "thread-stream-state-changed",
        "sourceClientId": "desktop-owner",
        "version": 11,
        "params": {
            "conversationId": "thread-1",
            "hostId": "local",
            "change": {
                "type": "patches",
                "baseRevision": 500,
                "revision": 501,
                "patches": [],
            },
        },
    }
    for _ in range(5):
        peer.push(gap)
        await asyncio.sleep(0.01)

    follows = [
        message
        for message in peer.messages
        if message.get("method") == "thread-stream-following-changed"
    ]
    assert len(follows) == 3  # 初次 follow + 最多两次 resync
    assert all("targetClientIds" not in message for message in follows)
    assert client.state_for("thread-1") is None
    await client.disconnect()
