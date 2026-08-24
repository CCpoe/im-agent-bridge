import asyncio
import json

import pytest

from lark_client.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
    CodexAppServerProtocolError,
    CodexAppServerRemoteError,
    CodexAppServerTimeoutError,
)


class FakeStdin:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        for raw in data.splitlines():
            if raw:
                self.process.receive(json.loads(raw.decode("utf-8")))

    async def drain(self):
        await asyncio.sleep(0)


class FakeProcess:
    def __init__(self, handler=None):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode = None
        self.handler = handler
        self.messages = []
        self.terminated = False
        self.killed = False
        self._waited = asyncio.Event()

    def receive(self, message):
        self.messages.append(message)
        if message.get("method") == "initialize":
            self.respond(message["id"], {"userAgent": "fake", "codexHome": "/tmp"})
        elif self.handler is not None:
            self.handler(self, message)

    def respond(self, request_id, result):
        self.stdout.feed_data(
            json.dumps({"id": request_id, "result": result}).encode() + b"\n"
        )

    def reject(self, request_id, code=-32600, message="rejected"):
        self.stdout.feed_data(json.dumps({
            "id": request_id,
            "error": {"code": code, "message": message},
        }).encode() + b"\n")

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._waited.set()

    def kill(self):
        self.killed = True
        self.terminate()

    async def wait(self):
        await self._waited.wait()
        return self.returncode

    def exit(self, returncode=1):
        self.returncode = returncode
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._waited.set()


class FakeProcessFactory:
    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.calls = []
        self.processes = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        handler = self.handlers.pop(0) if self.handlers else None
        process = FakeProcess(handler)
        self.processes.append(process)
        return process


def make_client(factory, **kwargs):
    return CodexAppServerClient(
        "/fake/codex",
        request_timeout=0.1,
        process_factory=factory,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_initialize_and_list_threads_follows_cursors_and_deduplicates():
    def handler(process, message):
        if message.get("method") != "thread/list":
            return
        cursor = message["params"].get("cursor")
        if cursor is None:
            process.respond(message["id"], {
                "data": [{"id": "thread-1"}, {"id": "thread-2"}],
                "nextCursor": "next-page",
            })
        else:
            process.respond(message["id"], {
                "data": [{"id": "thread-2"}, {"id": "thread-3"}],
                "nextCursor": None,
            })

    factory = FakeProcessFactory([handler])
    client = make_client(factory, page_size=2)

    threads = await client.list_threads(
        True,
        source_kinds=["vscode", "exec"],
        use_state_db_only=True,
    )

    assert [item["id"] for item in threads] == ["thread-1", "thread-2", "thread-3"]
    process = factory.processes[0]
    assert process.messages[0]["method"] == "initialize"
    assert process.messages[1] == {"method": "initialized", "params": {}}
    requests = [item for item in process.messages if item.get("method") == "thread/list"]
    assert requests[0]["params"] == {
        "archived": True,
        "cursor": None,
        "limit": 2,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "sourceKinds": ["vscode", "exec"],
        "useStateDbOnly": True,
    }
    assert requests[1]["params"]["cursor"] == "next-page"
    assert "cwd" not in requests[0]["params"]

    await client.close()
    assert process.terminated


@pytest.mark.asyncio
async def test_concurrent_requests_are_correlated_out_of_order():
    held = {}

    def handler(process, message):
        if message.get("method") in {"first", "second"}:
            held[message["method"]] = (process, message["id"])

    client = make_client(FakeProcessFactory([handler]))
    first = asyncio.create_task(client.request("first", {"value": 1}))
    second = asyncio.create_task(client.request("second", {"value": 2}))
    for _ in range(20):
        if len(held) == 2:
            break
        await asyncio.sleep(0)

    held["second"][0].respond(held["second"][1], {"value": 2})
    held["first"][0].respond(held["first"][1], {"value": 1})
    assert await first == {"value": 1}
    assert await second == {"value": 2}
    await client.close()


@pytest.mark.asyncio
async def test_server_message_with_colliding_id_is_not_a_response():
    held = {}

    def handler(process, message):
        if message.get("method") == "waiting":
            held["process"] = process
            held["id"] = message["id"]

    client = make_client(FakeProcessFactory([handler]))
    pending = asyncio.create_task(client.request("waiting"))
    for _ in range(20):
        if held:
            break
        await asyncio.sleep(0)

    held["process"].stdout.feed_data(json.dumps({
        "method": "some/server/request",
        "id": held["id"],
        "params": {},
    }).encode() + b"\n")
    await asyncio.sleep(0)
    assert not pending.done()
    held["process"].respond(held["id"], {"ok": True})
    assert await pending == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_unarchive_returns_thread_and_preserves_remote_error():
    def handler(process, message):
        if message.get("method") != "thread/unarchive":
            return
        if message["params"]["threadId"] == "thread-ok":
            process.respond(message["id"], {"thread": {"id": "thread-ok"}})
        else:
            process.reject(message["id"], message="no archived rollout found")

    client = make_client(FakeProcessFactory([handler]))
    assert await client.unarchive_thread(" thread-ok ") == {"id": "thread-ok"}
    with pytest.raises(CodexAppServerRemoteError) as raised:
        await client.unarchive_thread("thread-missing")
    assert raised.value.code == -32600
    assert "no archived rollout" in raised.value.message
    await client.close()


@pytest.mark.asyncio
async def test_timeout_cleans_pending_request():
    client = make_client(FakeProcessFactory([lambda process, message: None]))
    with pytest.raises(CodexAppServerTimeoutError):
        await client.request("never-answered", timeout=0.01)
    assert client._pending == {}
    assert client._methods == {}
    await client.close()


@pytest.mark.asyncio
async def test_disconnect_fails_pending_and_next_request_restarts_process():
    first_request_seen = asyncio.Event()

    def first_handler(process, message):
        if message.get("method") == "will-disconnect":
            first_request_seen.set()

    def second_handler(process, message):
        if message.get("method") == "after-restart":
            process.respond(message["id"], {"ok": True})

    factory = FakeProcessFactory([first_handler, second_handler])
    client = make_client(factory)
    pending = asyncio.create_task(client.request("will-disconnect"))
    await first_request_seen.wait()
    factory.processes[0].exit()
    with pytest.raises(CodexAppServerDisconnected):
        await pending

    assert await client.request("after-restart") == {"ok": True}
    assert len(factory.processes) == 2
    await client.close()


@pytest.mark.asyncio
async def test_stderr_is_drained_and_malformed_stdout_fails_requests():
    request_seen = asyncio.Event()

    def handler(process, message):
        if message.get("method") == "malformed":
            process.stderr.feed_data(b"diagnostic warning\n")
            request_seen.set()

    factory = FakeProcessFactory([handler])
    client = make_client(factory)
    pending = asyncio.create_task(client.request("malformed"))
    await request_seen.wait()
    factory.processes[0].stdout.feed_data(b"not-json\n")
    with pytest.raises(CodexAppServerProtocolError):
        await pending
    for _ in range(20):
        if client.last_stderr_line:
            break
        await asyncio.sleep(0)
    assert client.last_stderr_line == "diagnostic warning"
    await client.close()


@pytest.mark.asyncio
async def test_repeated_cursor_and_thread_limit_fail_closed():
    def repeated(process, message):
        if message.get("method") == "thread/list":
            process.respond(message["id"], {"data": [], "nextCursor": "same"})

    client = make_client(FakeProcessFactory([repeated]))
    with pytest.raises(CodexAppServerProtocolError, match="repeated nextCursor"):
        await client.list_threads(False)
    await client.close()

    def too_many(process, message):
        if message.get("method") == "thread/list":
            process.respond(message["id"], {
                "data": [{"id": "one"}, {"id": "two"}],
                "nextCursor": None,
            })

    client = make_client(FakeProcessFactory([too_many]), max_threads=1)
    with pytest.raises(CodexAppServerProtocolError, match="safety limit"):
        await client.list_threads(False)
    await client.close()
