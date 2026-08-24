"""Small asynchronous client for the public Codex App Server protocol.

The Desktop bridge uses a private IPC connection for live thread control.  A
separate App Server process is useful for persisted-thread operations that the
private protocol does not expose, such as listing archived threads and moving
one back into the active sessions directory.

App Server's stdio transport is newline-delimited JSON.  This client keeps one
process alive, correlates concurrent requests by ``id``, and reconnects lazily
after a process exits.  It deliberately does not retry mutating requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence


logger = logging.getLogger("CodexAppServer")

DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_PAGE_SIZE = 25
DEFAULT_MAX_THREADS = 5_000
DEFAULT_STREAM_LIMIT = 16 * 1024 * 1024


class CodexAppServerError(Exception):
    """Base error for the App Server subprocess and protocol."""


class CodexAppServerProtocolError(CodexAppServerError):
    """The subprocess returned a malformed or unexpected protocol message."""


class CodexAppServerDisconnected(CodexAppServerError):
    """The App Server subprocess exited while a request was pending."""


class CodexAppServerTimeoutError(CodexAppServerError, TimeoutError):
    """An App Server request did not complete before its deadline."""


class CodexAppServerRemoteError(CodexAppServerError):
    """A JSON-RPC request was rejected by App Server."""

    def __init__(self, method: str, error: Any):
        self.method = method
        self.error = error
        if isinstance(error, Mapping):
            self.code = error.get("code")
            self.message = str(error.get("message") or error)
        else:
            self.code = None
            self.message = str(error)
        super().__init__("Codex App Server request %s failed: %s" % (method, self.message))


ProcessFactory = Callable[..., Awaitable[Any]]


def find_codex_executable() -> str:
    """Resolve the Codex binary, preferring an explicit deployment override."""

    configured = os.environ.get("CODEX_APP_SERVER_BIN", "").strip()
    candidates = [
        configured,
        shutil.which("codex") or "",
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise CodexAppServerDisconnected(
        "cannot find a Codex executable; set CODEX_APP_SERVER_BIN"
    )


class CodexAppServerClient:
    """Persistent async JSONL client for ``codex app-server --stdio``.

    The instance is bound to the first asyncio event loop that uses it.  Calls
    may run concurrently on that loop: writes are serialized and responses are
    delivered to the matching request future, even when they arrive out of
    order.
    """

    def __init__(
        self,
        executable: Optional[str] = None,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_threads: int = DEFAULT_MAX_THREADS,
        stream_limit: int = DEFAULT_STREAM_LIMIT,
        process_factory: Optional[ProcessFactory] = None,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if max_threads <= 0:
            raise ValueError("max_threads must be positive")
        if stream_limit <= 0:
            raise ValueError("stream_limit must be positive")

        self.executable = executable
        self.request_timeout = request_timeout
        self.page_size = page_size
        self.max_threads = max_threads
        self.stream_limit = stream_limit
        self._process_factory = process_factory or asyncio.create_subprocess_exec

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connect_lock: Optional[asyncio.Lock] = None
        self._write_lock: Optional[asyncio.Lock] = None
        self._process: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._methods: Dict[int, str] = {}
        self._next_request_id = 0
        self._ready = asyncio.Event()
        self._generation = 0
        self._closing = False
        self._last_stderr_line = ""
        self._initialize_result: Dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        process = self._process
        return bool(
            self._ready.is_set()
            and process is not None
            and getattr(process, "returncode", None) is None
        )

    @property
    def last_stderr_line(self) -> str:
        """Last bounded stderr line, intended only for diagnostics."""

        return self._last_stderr_line

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._connect_lock = asyncio.Lock()
            self._write_lock = asyncio.Lock()
        elif self._loop is not loop:
            raise RuntimeError("CodexAppServerClient is bound to another event loop")

    async def connect(self) -> Mapping[str, Any]:
        """Start and initialize App Server, or return if already connected."""

        self._bind_loop()
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self.connected:
                return dict(self._initialize_result)

            self._closing = False
            await self._stop_transport()
            executable = self.executable or find_codex_executable()
            try:
                process = await self._process_factory(
                    executable,
                    "app-server",
                    "--stdio",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.stream_limit,
                )
            except Exception as exc:
                raise CodexAppServerDisconnected(
                    "cannot start Codex App Server: %s" % exc
                ) from exc

            if process.stdin is None or process.stdout is None:
                self._terminate_process(process)
                raise CodexAppServerDisconnected(
                    "Codex App Server did not expose stdio pipes"
                )

            self._process = process
            self._generation += 1
            generation = self._generation
            self._reader_task = asyncio.create_task(
                self._read_loop(process, generation),
                name="codex-app-server-reader",
            )
            if process.stderr is not None:
                self._stderr_task = asyncio.create_task(
                    self._drain_stderr(process, generation),
                    name="codex-app-server-stderr",
                )

            try:
                result = await self._request_started_process(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "im_agent_bridge",
                            "title": "IM Agent Bridge",
                            "version": "1",
                        }
                    },
                )
                if not isinstance(result, Mapping):
                    raise CodexAppServerProtocolError(
                        "initialize response must be an object"
                    )
                await self._write_message({"method": "initialized", "params": {}})
                self._initialize_result = dict(result)
                self._ready.set()
                return dict(self._initialize_result)
            except Exception:
                await self._stop_transport()
                raise

    async def close(self) -> None:
        """Close the subprocess and fail all outstanding requests."""

        self._bind_loop()
        assert self._connect_lock is not None
        async with self._connect_lock:
            self._closing = True
            await self._stop_transport()

    async def request(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send one App Server request and return its ``result``."""

        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        await self.connect()
        return await self._request_started_process(method, params, timeout=timeout)

    async def list_threads(
        self,
        archived: bool,
        *,
        source_kinds: Optional[Sequence[str]] = None,
        use_state_db_only: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """List all matching threads by following App Server cursors.

        ``cwd`` is intentionally not accepted here: callers should not
        accidentally restrict history to the sidecar process's working
        directory.  Any product-level project filtering can be applied after
        retrieving the authoritative active/archived partition.
        """

        cursor: Optional[str] = None
        seen_cursors = set()
        seen_ids = set()
        threads: List[Dict[str, Any]] = []

        while True:
            params: Dict[str, Any] = {
                "archived": bool(archived),
                "cursor": cursor,
                "limit": self.page_size,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            }
            if source_kinds is not None:
                params["sourceKinds"] = [str(value) for value in source_kinds]
            if use_state_db_only is not None:
                params["useStateDbOnly"] = bool(use_state_db_only)

            result = await self.request("thread/list", params)
            if not isinstance(result, Mapping) or not isinstance(result.get("data"), list):
                raise CodexAppServerProtocolError(
                    "thread/list response must contain a data array"
                )
            for item in result["data"]:
                if not isinstance(item, Mapping):
                    raise CodexAppServerProtocolError(
                        "thread/list returned a non-object thread"
                    )
                thread_id = item.get("id")
                marker = thread_id if isinstance(thread_id, str) and thread_id else None
                if marker is not None and marker in seen_ids:
                    continue
                if marker is not None:
                    seen_ids.add(marker)
                threads.append(dict(item))
                if len(threads) > self.max_threads:
                    raise CodexAppServerProtocolError(
                        "thread/list exceeded the %d thread safety limit"
                        % self.max_threads
                    )

            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return threads
            if not isinstance(next_cursor, str) or not next_cursor:
                raise CodexAppServerProtocolError(
                    "thread/list returned an invalid nextCursor"
                )
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise CodexAppServerProtocolError(
                    "thread/list returned a repeated nextCursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def unarchive_thread(self, thread_id: str) -> Dict[str, Any]:
        """Restore one archived thread and return the restored Thread object."""

        if not isinstance(thread_id, str) or not thread_id.strip() or "\x00" in thread_id:
            raise ValueError("thread_id must be a non-empty string")
        result = await self.request(
            "thread/unarchive", {"threadId": thread_id.strip()}
        )
        thread = result.get("thread") if isinstance(result, Mapping) else None
        if not isinstance(thread, Mapping):
            raise CodexAppServerProtocolError(
                "thread/unarchive response must contain a thread object"
            )
        return dict(thread)

    async def _request_started_process(
        self,
        method: str,
        params: Optional[Mapping[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        process = self._process
        if process is None or getattr(process, "returncode", None) is not None:
            raise CodexAppServerDisconnected("Codex App Server is not running")

        self._next_request_id += 1
        request_id = self._next_request_id
        assert self._loop is not None
        future = self._loop.create_future()
        self._pending[request_id] = future
        self._methods[request_id] = method
        try:
            await self._write_message(
                {
                    "method": method,
                    "id": request_id,
                    "params": dict(params or {}),
                }
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    self.request_timeout if timeout is None else timeout,
                )
            except asyncio.TimeoutError as exc:
                raise CodexAppServerTimeoutError(
                    "Codex App Server request %s timed out" % method
                ) from exc
        finally:
            self._pending.pop(request_id, None)
            self._methods.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _write_message(self, message: Mapping[str, Any]) -> None:
        assert self._write_lock is not None
        try:
            payload = json.dumps(
                message, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise CodexAppServerProtocolError(
                "request is not JSON serializable"
            ) from exc

        async with self._write_lock:
            process = self._process
            writer = process.stdin if process is not None else None
            if writer is None or getattr(process, "returncode", None) is not None:
                raise CodexAppServerDisconnected("Codex App Server is not running")
            try:
                writer.write(payload)
                await writer.drain()
            except Exception as exc:
                raise CodexAppServerDisconnected(
                    "failed to write to Codex App Server"
                ) from exc

    async def _read_loop(self, process: Any, generation: int) -> None:
        error: Exception = CodexAppServerDisconnected("Codex App Server exited")
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CodexAppServerProtocolError(
                        "Codex App Server emitted invalid JSON"
                    ) from exc
                if not isinstance(message, Mapping):
                    raise CodexAppServerProtocolError(
                        "Codex App Server message must be an object"
                    )
                if "method" in message:
                    # Notifications have no id, while server-initiated requests
                    # may have one.  Neither is a response to a client request,
                    # even if the two peers happen to choose the same id.
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, int):
                    continue
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                method = self._methods.get(request_id, "unknown")
                if "error" in message:
                    future.set_exception(
                        CodexAppServerRemoteError(method, message.get("error"))
                    )
                elif "result" in message:
                    future.set_result(message.get("result"))
                else:
                    future.set_exception(
                        CodexAppServerProtocolError(
                            "response %s has neither result nor error" % request_id
                        )
                    )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            error = exc
        finally:
            if generation == self._generation and not self._closing:
                self._ready.clear()
                self._fail_pending(error)

    async def _drain_stderr(self, process: Any, generation: int) -> None:
        try:
            while generation == self._generation:
                raw = await process.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._last_stderr_line = line[-2_000:]
                    logger.debug("Codex App Server stderr: %s", self._last_stderr_line)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Failed to drain Codex App Server stderr", exc_info=True)

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def _stop_transport(self) -> None:
        self._ready.clear()
        self._initialize_result = {}
        self._fail_pending(CodexAppServerDisconnected("Codex App Server closed"))

        reader_task = self._reader_task
        stderr_task = self._stderr_task
        process = self._process
        self._reader_task = None
        self._stderr_task = None
        self._process = None
        self._generation += 1

        current = asyncio.current_task()
        tasks = [
            task for task in (reader_task, stderr_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()

        if process is not None and getattr(process, "returncode", None) is None:
            self._terminate_process(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _terminate_process(process: Any) -> None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except Exception:
            logger.debug("Failed to terminate Codex App Server", exc_info=True)


__all__ = [
    "CodexAppServerClient",
    "CodexAppServerDisconnected",
    "CodexAppServerError",
    "CodexAppServerProtocolError",
    "CodexAppServerRemoteError",
    "CodexAppServerTimeoutError",
    "find_codex_executable",
]
