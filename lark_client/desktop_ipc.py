"""Codex Desktop 本地 IPC 客户端。

Desktop 在 ``~/.codex/ipc/ipc.sock`` 暴露一个仅限本机的 Unix socket。
协议使用 4 字节小端长度前缀，帧内容为 UTF-8 JSON。本模块只负责协议和连接；
上层仍需决定哪些飞书用户能够查看或操作线程。

该协议目前是 Codex Desktop 的内部协议，版本常量集中在本文件中，便于应用升级后
做兼容性调整。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import struct
import threading
import time
import uuid
from concurrent.futures import Future as ConcurrentFuture
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple


logger = logging.getLogger("DesktopIPC")

MAX_MESSAGE_SIZE = 256 * 1024 * 1024
INITIALIZING_CLIENT_ID = "initializing-client"
DEFAULT_HOST_ID = "local"

INITIALIZE_VERSION = 0
THREAD_REQUEST_VERSION = 1
THREAD_STATE_VERSION = 11
THREAD_INTERRUPT_LEGACY_VERSION = 3
THREAD_INTERRUPT_VERSION = 4

METHOD_INITIALIZE = "initialize"
METHOD_OWNER_DISCOVERY = "thread-owner-discovery"
METHOD_FOLLOWING_CHANGED = "thread-stream-following-changed"
METHOD_FOLLOWING_STATUS_REQUESTED = "thread-stream-following-status-requested"
METHOD_STATE_CHANGED = "thread-stream-state-changed"
METHOD_START_TURN = "thread-follower-start-turn"
METHOD_STEER_TURN = "thread-follower-steer-turn"
METHOD_INTERRUPT_TURN = "thread-follower-interrupt-turn"
METHOD_LOAD_COMPLETE_HISTORY = "thread-follower-load-complete-history"
METHOD_COMMAND_APPROVAL = "thread-follower-command-approval-decision"
METHOD_FILE_APPROVAL = "thread-follower-file-approval-decision"
METHOD_PERMISSIONS_APPROVAL = (
    "thread-follower-permissions-request-approval-response"
)
METHOD_SUBMIT_USER_INPUT = "thread-follower-submit-user-input"
METHOD_SUBMIT_MCP_ELICITATION = (
    "thread-follower-submit-mcp-server-elicitation-response"
)

StateCallback = Callable[[Dict[str, Any]], Any]
BroadcastCallback = Callable[[Dict[str, Any]], Any]
Connector = Callable[[str], Awaitable[Tuple[Any, Any]]]


class DesktopIPCError(Exception):
    """Desktop IPC 错误基类。"""


class DesktopIPCProtocolError(DesktopIPCError):
    """收到无效或不受支持的 IPC 帧。"""


class DesktopIPCDisconnected(DesktopIPCError):
    """IPC 连接在请求完成前断开。"""


class DesktopIPCTimeoutError(DesktopIPCError, TimeoutError):
    """IPC 请求超时。"""


class DesktopIPCRemoteError(DesktopIPCError):
    """Desktop owner 返回了错误响应。"""

    def __init__(self, method: str, error: Any):
        self.method = method
        self.error = error
        super().__init__("Desktop IPC request %s failed: %s" % (method, error))


def encode_frame(
    message: Mapping[str, Any], max_message_size: int = MAX_MESSAGE_SIZE
) -> bytes:
    """把一个 IPC envelope 编码为长度前缀帧。"""

    try:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DesktopIPCProtocolError("message is not JSON serializable") from exc
    if len(payload) > max_message_size:
        raise DesktopIPCProtocolError(
            "message exceeds %d byte limit" % max_message_size
        )
    return struct.pack("<I", len(payload)) + payload


async def read_frame(
    reader: Any, max_message_size: int = MAX_MESSAGE_SIZE
) -> Dict[str, Any]:
    """从 asyncio 风格 reader 读取并校验一个 IPC envelope。"""

    header = await reader.readexactly(4)
    (size,) = struct.unpack("<I", header)
    if size > max_message_size:
        raise DesktopIPCProtocolError(
            "incoming message is %d bytes; limit is %d"
            % (size, max_message_size)
        )
    try:
        decoded = json.loads((await reader.readexactly(size)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesktopIPCProtocolError("incoming frame is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise DesktopIPCProtocolError("incoming JSON must be an object")
    return decoded


async def _open_unix_connection(path: str) -> Tuple[Any, Any]:
    return await asyncio.open_unix_connection(path=path)


class DesktopIPCClient:
    """异步 Codex Desktop IPC follower。

    公共协程可由多个 asyncio task 并发调用；帧写入会串行化，请求通过 requestId
    独立关联。若调用方位于其他线程，可使用 :meth:`request_threadsafe` 把通用请求
    投递到该实例所属的事件循环。
    """

    def __init__(
        self,
        socket_path: Optional[Path] = None,
        *,
        client_type: str = "feishu-sidecar",
        request_timeout: float = 10.0,
        connect_timeout: float = 10.0,
        reconnect: bool = True,
        reconnect_delay: float = 0.5,
        max_reconnect_delay: float = 10.0,
        resync_delay: float = 0.1,
        resync_cooldown: float = 2.0,
        cache_state: bool = True,
        max_message_size: int = MAX_MESSAGE_SIZE,
        on_state_change: Optional[StateCallback] = None,
        connector: Optional[Connector] = None,
    ):
        if request_timeout <= 0 or connect_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if reconnect_delay < 0 or max_reconnect_delay < reconnect_delay:
            raise ValueError("invalid reconnect delay")
        if resync_delay < 0 or resync_cooldown < 0:
            raise ValueError("invalid resync timing")
        if not 0 < max_message_size <= 0xFFFFFFFF:
            raise ValueError("invalid max_message_size")

        configured_socket = os.environ.get("CODEX_DESKTOP_IPC_PATH", "").strip()
        self.socket_path = Path(
            socket_path or configured_socket or Path.home() / ".codex/ipc/ipc.sock"
        ).expanduser()
        self.client_type = client_type
        self.request_timeout = request_timeout
        self.connect_timeout = connect_timeout
        self.reconnect_enabled = reconnect
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.resync_delay = resync_delay
        self.resync_cooldown = resync_cooldown
        self.cache_state = cache_state
        self.max_message_size = max_message_size
        self._connector = connector or _open_unix_connection

        self.client_id: Optional[str] = None
        self._reader: Any = None
        self._writer: Any = None
        self._reader_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._callback_tasks: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connect_lock: Optional[asyncio.Lock] = None
        self._write_lock: Optional[asyncio.Lock] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._ready = asyncio.Event()
        self._closing = False
        self._generation = 0
        self._data_lock = threading.RLock()

        self._owners: Dict[Tuple[str, str], str] = {}
        self._followed: set = set()
        self._state_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._resync_tasks: Dict[Tuple[str, str], asyncio.Task] = {}
        self._last_resync_at: Dict[Tuple[str, str], float] = {}
        self._resync_attempts: Dict[Tuple[str, str], int] = {}
        self._patch_only: set = set()
        self._state_callbacks: List[StateCallback] = []
        self._broadcast_callbacks: Dict[str, List[BroadcastCallback]] = {}
        if on_state_change is not None:
            self._state_callbacks.append(on_state_change)

    @property
    def connected(self) -> bool:
        return self._ready.is_set() and self._writer is not None

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._connect_lock = asyncio.Lock()
            self._write_lock = asyncio.Lock()
        elif self._loop is not loop:
            raise RuntimeError(
                "DesktopIPCClient is bound to another event loop; "
                "use request_threadsafe from other threads"
            )

    async def connect(self) -> str:
        """连接 socket 并完成 initialize；已连接时直接返回 clientId。"""

        self._bind_loop()
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self.connected and self.client_id is not None:
                return self.client_id

            self._closing = False
            await self._close_transport(cancel_reader=True)
            try:
                reader, writer = await asyncio.wait_for(
                    self._connector(str(self.socket_path)),
                    timeout=self.connect_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise DesktopIPCTimeoutError("Desktop IPC connect timed out") from exc
            except Exception as exc:
                raise DesktopIPCDisconnected(
                    "cannot connect to Desktop IPC at %s: %s"
                    % (self.socket_path, exc)
                ) from exc

            self._reader = reader
            self._writer = writer
            self.client_id = None
            self._generation += 1
            generation = self._generation
            self._reader_task = asyncio.create_task(
                self._read_loop(generation), name="desktop-ipc-reader"
            )

            try:
                response = await self._request_envelope(
                    METHOD_INITIALIZE,
                    {"clientType": self.client_type},
                    version=INITIALIZE_VERSION,
                    source_client_id=INITIALIZING_CLIENT_ID,
                    timeout=self.request_timeout,
                )
                result = response.get("result")
                client_id = result.get("clientId") if isinstance(result, dict) else None
                if not isinstance(client_id, str) or not client_id:
                    raise DesktopIPCProtocolError(
                        "initialize response did not contain clientId"
                    )
                self.client_id = client_id
                self._ready.set()
                await self._restore_following()
                return client_id
            except Exception:
                await self._close_transport(cancel_reader=True)
                raise

    async def disconnect(self) -> None:
        """主动断开并停止自动重连。之后可再次调用 connect。"""

        self._bind_loop()
        self._closing = True
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if reconnect_task is not None and reconnect_task is not asyncio.current_task():
            reconnect_task.cancel()
            try:
                await reconnect_task
            except asyncio.CancelledError:
                pass
        await self._close_transport(cancel_reader=True)
        self._fail_pending(DesktopIPCDisconnected("Desktop IPC disconnected"))

        callback_tasks = list(self._callback_tasks)
        for task in callback_tasks:
            task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        await self._cancel_resync_tasks()

    async def wait_until_connected(self, timeout: Optional[float] = None) -> str:
        """等待 initialize 完成。"""

        self._bind_loop()
        wait_timeout = self.connect_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(self._ready.wait(), wait_timeout)
        except asyncio.TimeoutError as exc:
            raise DesktopIPCTimeoutError("Desktop IPC reconnect timed out") from exc
        if self.client_id is None:
            raise DesktopIPCDisconnected("Desktop IPC is not initialized")
        return self.client_id

    def request_threadsafe(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        version: int = THREAD_REQUEST_VERSION,
        target_client_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ConcurrentFuture:
        """从非事件循环线程安全地提交一个通用 IPC 请求。"""

        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("DesktopIPCClient has no running owner event loop")
        coroutine = self.request(
            method,
            params,
            version=version,
            target_client_id=target_client_id,
            timeout=timeout,
        )
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def request(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        version: int = THREAD_REQUEST_VERSION,
        target_client_id: Optional[str] = None,
        timeout: Optional[float] = None,
        remote_timeout_ms: Optional[int] = None,
    ) -> Any:
        """发送请求并返回 response.result。"""

        response = await self.request_response(
            method,
            params,
            version=version,
            target_client_id=target_client_id,
            timeout=timeout,
            remote_timeout_ms=remote_timeout_ms,
        )
        return response.get("result")

    async def request_response(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        version: int = THREAD_REQUEST_VERSION,
        target_client_id: Optional[str] = None,
        timeout: Optional[float] = None,
        remote_timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发送请求并返回完整 response envelope。"""

        self._bind_loop()
        if not self.connected:
            await self.connect()
        return await self._request_envelope(
            method,
            dict(params or {}),
            version=version,
            target_client_id=target_client_id,
            timeout=timeout,
            remote_timeout_ms=remote_timeout_ms,
        )

    async def broadcast(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        version: int,
        target_client_ids: Optional[List[str]] = None,
    ) -> None:
        """发送无需响应的 broadcast envelope。"""

        self._bind_loop()
        if not self.connected:
            await self.connect()
        envelope: Dict[str, Any] = {
            "type": "broadcast",
            "method": method,
            "sourceClientId": self.client_id,
            "params": dict(params),
            "version": version,
        }
        if target_client_ids is not None:
            envelope["targetClientIds"] = list(target_client_ids)
        await self._send_envelope(envelope)

    async def discover_owner(
        self, conversation_id: str, *, host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = None,
    ) -> str:
        """发现持有 Desktop 线程 writer 的 clientId。"""

        response = await self.request_response(
            METHOD_OWNER_DISCOVERY,
            {"hostId": host_id, "conversationId": conversation_id},
            version=THREAD_REQUEST_VERSION,
            timeout=timeout,
        )
        owner_id = response.get("handledByClientId")
        if not isinstance(owner_id, str) or not owner_id:
            raise DesktopIPCProtocolError(
                "thread owner response did not contain handledByClientId"
            )
        with self._data_lock:
            self._owners[(host_id, conversation_id)] = owner_id
        return owner_id

    async def set_following(
        self,
        conversation_id: str,
        following: bool,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
    ) -> str:
        """通知 owner 开始或停止向当前 client 推送线程状态。"""

        key = (host_id, conversation_id)
        with self._data_lock:
            owner_id = owner_client_id or self._owners.get(key) or ""
            if not following:
                self._followed.discard(key)
                self._state_cache.pop(key, None)
                self._resync_attempts.pop(key, None)
                self._patch_only.discard(key)
                self._cancel_resync_locked(key)
        await self.broadcast(
            METHOD_FOLLOWING_CHANGED,
            {
                "conversationId": conversation_id,
                "hostId": host_id,
                "following": bool(following),
            },
            version=THREAD_REQUEST_VERSION,
            target_client_ids=None,
        )
        with self._data_lock:
            if following:
                self._followed.add(key)
        return owner_id

    async def follow(
        self,
        conversation_id: str,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
    ) -> str:
        return await self.set_following(
            conversation_id,
            True,
            owner_client_id=owner_client_id,
            host_id=host_id,
        )

    async def unfollow(
        self,
        conversation_id: str,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
    ) -> str:
        return await self.set_following(
            conversation_id,
            False,
            owner_client_id=owner_client_id,
            host_id=host_id,
        )

    async def start_turn(
        self,
        conversation_id: str,
        prompt: str,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        turn_start_params: Optional[Mapping[str, Any]] = None,
        client_user_message_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """在空闲 Desktop 线程中开始一个 turn。"""

        turn_params = dict(turn_start_params or {})
        turn_params.setdefault("input", [self._text_input(prompt)])
        if client_user_message_id:
            turn_params.setdefault("clientUserMessageId", client_user_message_id)
        return await self._owner_request(
            METHOD_START_TURN,
            conversation_id,
            {
                "conversationId": conversation_id,
                "turnStartParams": turn_params,
            },
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def steer_turn(
        self,
        conversation_id: str,
        prompt: str,
        cwd: str,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        client_user_message_id: Optional[str] = None,
        restore_message: Optional[Mapping[str, Any]] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """向运行中的 Desktop turn 发送 steer 消息。"""

        message_id = client_user_message_id or str(uuid.uuid4())
        if restore_message is None:
            restore: Dict[str, Any] = {
                "id": message_id,
                "text": prompt,
                "cwd": cwd,
                "createdAt": int(time.time() * 1000),
                "context": {
                    "prompt": prompt,
                    "addedFiles": [],
                    "fileAttachments": [],
                    "ideContext": None,
                    "imageAttachments": [],
                    "workspaceRoots": [cwd],
                },
            }
        else:
            restore = dict(restore_message)
        return await self._owner_request(
            METHOD_STEER_TURN,
            conversation_id,
            {
                "conversationId": conversation_id,
                "clientUserMessageId": message_id,
                "input": [self._text_input(prompt)],
                "attachments": list(attachments or []),
                "restoreMessage": restore,
            },
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def interrupt(
        self,
        conversation_id: str,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        mode: str = "user-stop",
        expected_turn_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        params: Dict[str, Any] = {
            "conversationId": conversation_id,
            "mode": mode,
        }
        if expected_turn_id is not None:
            params["expectedTurnId"] = expected_turn_id
        return await self._owner_request(
            METHOD_INTERRUPT_TURN,
            conversation_id,
            params,
            version=(
                THREAD_INTERRUPT_VERSION
                if expected_turn_id is not None
                else THREAD_INTERRUPT_LEGACY_VERSION
            ),
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def load_complete_history(
        self,
        conversation_id: str,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = 305.0,
    ) -> Any:
        """请求 owner 加载完整线程历史。

        Desktop 端最长可能等待约五分钟，因此该操作使用独立的 305 秒默认超时，
        不受普通 IPC 请求的短超时影响。
        """

        return await self._owner_request(
            METHOD_LOAD_COMPLETE_HISTORY,
            conversation_id,
            {"conversationId": conversation_id},
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def command_approval(
        self,
        conversation_id: str,
        request_id: str,
        decision: Any,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = None,
    ) -> Any:
        return await self._owner_request(
            METHOD_COMMAND_APPROVAL,
            conversation_id,
            {
                "conversationId": conversation_id,
                "requestId": request_id,
                "decision": decision,
            },
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def file_approval(
        self,
        conversation_id: str,
        request_id: str,
        decision: Any,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = None,
    ) -> Any:
        return await self._owner_request(
            METHOD_FILE_APPROVAL,
            conversation_id,
            {
                "conversationId": conversation_id,
                "requestId": request_id,
                "decision": decision,
            },
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def permissions_approval(
        self,
        conversation_id: str,
        request_id: str,
        response: Any,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = None,
    ) -> Any:
        return await self._response_request(
            METHOD_PERMISSIONS_APPROVAL,
            conversation_id,
            request_id,
            response,
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def submit_user_input(
        self,
        conversation_id: str,
        request_id: str,
        response: Any,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = None,
    ) -> Any:
        return await self._response_request(
            METHOD_SUBMIT_USER_INPUT,
            conversation_id,
            request_id,
            response,
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def submit_mcp_server_elicitation_response(
        self,
        conversation_id: str,
        request_id: str,
        response: Any,
        *,
        owner_client_id: Optional[str] = None,
        host_id: str = DEFAULT_HOST_ID,
        timeout: Optional[float] = None,
    ) -> Any:
        return await self._response_request(
            METHOD_SUBMIT_MCP_ELICITATION,
            conversation_id,
            request_id,
            response,
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    def add_state_listener(self, callback: StateCallback) -> None:
        with self._data_lock:
            if callback not in self._state_callbacks:
                self._state_callbacks.append(callback)

    def remove_state_listener(self, callback: StateCallback) -> None:
        with self._data_lock:
            if callback in self._state_callbacks:
                self._state_callbacks.remove(callback)

    def add_broadcast_listener(
        self, method: str, callback: BroadcastCallback
    ) -> None:
        with self._data_lock:
            callbacks = self._broadcast_callbacks.setdefault(method, [])
            if callback not in callbacks:
                callbacks.append(callback)

    def remove_broadcast_listener(
        self, method: str, callback: BroadcastCallback
    ) -> None:
        with self._data_lock:
            callbacks = self._broadcast_callbacks.get(method, [])
            if callback in callbacks:
                callbacks.remove(callback)

    def state_for(
        self, conversation_id: str, *, host_id: str = DEFAULT_HOST_ID
    ) -> Optional[Dict[str, Any]]:
        """返回最新 snapshot/patches 合成状态的副本。"""

        with self._data_lock:
            cached = self._state_cache.get((host_id, conversation_id))
            return copy.deepcopy(cached) if cached is not None else None

    async def _response_request(
        self,
        method: str,
        conversation_id: str,
        request_id: str,
        response: Any,
        *,
        owner_client_id: Optional[str],
        host_id: str,
        timeout: Optional[float],
    ) -> Any:
        return await self._owner_request(
            method,
            conversation_id,
            {
                "conversationId": conversation_id,
                "requestId": request_id,
                "response": response,
            },
            owner_client_id=owner_client_id,
            host_id=host_id,
            timeout=timeout,
        )

    async def _owner_request(
        self,
        method: str,
        conversation_id: str,
        params: Mapping[str, Any],
        *,
        owner_client_id: Optional[str],
        host_id: str,
        timeout: Optional[float],
        version: int = THREAD_REQUEST_VERSION,
    ) -> Any:
        requested_owner = owner_client_id
        for attempt in range(2):
            owner_id = await self._owner_for(
                conversation_id, host_id, requested_owner
            )
            try:
                return await self.request(
                    method,
                    params,
                    version=version,
                    target_client_id=owner_id,
                    timeout=timeout,
                )
            except DesktopIPCRemoteError as exc:
                error_text = str(exc.error).lower()
                if attempt or not any(
                    marker in error_text
                    for marker in ("no-client-found", "client-disconnected")
                ):
                    raise
                with self._data_lock:
                    self._owners.pop((host_id, conversation_id), None)
                requested_owner = None
        raise DesktopIPCDisconnected("Desktop owner unavailable")

    async def _owner_for(
        self,
        conversation_id: str,
        host_id: str,
        owner_client_id: Optional[str],
    ) -> str:
        if owner_client_id:
            with self._data_lock:
                self._owners[(host_id, conversation_id)] = owner_client_id
            return owner_client_id
        with self._data_lock:
            cached = self._owners.get((host_id, conversation_id))
        if cached:
            return cached
        return await self.discover_owner(conversation_id, host_id=host_id)

    @staticmethod
    def _text_input(prompt: str) -> Dict[str, Any]:
        return {"type": "text", "text": prompt, "text_elements": []}

    async def _request_envelope(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        version: int,
        source_client_id: Optional[str] = None,
        target_client_id: Optional[str] = None,
        timeout: Optional[float] = None,
        remote_timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        if self._writer is None:
            raise DesktopIPCDisconnected("Desktop IPC is not connected")
        loop = asyncio.get_running_loop()
        request_id = str(uuid.uuid4())
        future = loop.create_future()
        self._pending[request_id] = future
        envelope: Dict[str, Any] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": source_client_id or self.client_id,
            "version": version,
            "method": method,
            "params": dict(params),
        }
        if target_client_id is not None:
            envelope["targetClientId"] = target_client_id
        if remote_timeout_ms is not None:
            envelope["timeoutMs"] = int(remote_timeout_ms)

        try:
            await self._send_envelope(envelope)
            wait_timeout = self.request_timeout if timeout is None else timeout
            try:
                response = await asyncio.wait_for(future, timeout=wait_timeout)
            except asyncio.TimeoutError as exc:
                raise DesktopIPCTimeoutError(
                    "Desktop IPC request %s timed out" % method
                ) from exc
        finally:
            self._pending.pop(request_id, None)

        if response.get("resultType") == "error":
            raise DesktopIPCRemoteError(method, response.get("error"))
        if response.get("resultType") != "success":
            raise DesktopIPCProtocolError(
                "response has invalid resultType for %s" % method
            )
        return response

    async def _send_envelope(self, envelope: Mapping[str, Any]) -> None:
        self._bind_loop()
        assert self._write_lock is not None
        async with self._write_lock:
            writer = self._writer
            if writer is None:
                raise DesktopIPCDisconnected("Desktop IPC is not connected")
            try:
                writer.write(encode_frame(envelope, self.max_message_size))
                await writer.drain()
            except Exception as exc:
                raise DesktopIPCDisconnected(
                    "failed to write Desktop IPC frame: %s" % exc
                ) from exc

    async def _read_loop(self, generation: int) -> None:
        error: Optional[BaseException] = None
        try:
            while generation == self._generation and not self._closing:
                message = await read_frame(self._reader, self.max_message_size)
                await self._handle_message(message)
        except asyncio.CancelledError:
            return
        except asyncio.IncompleteReadError as exc:
            error = DesktopIPCDisconnected("Desktop IPC socket closed")
            error.__cause__ = exc
        except Exception as exc:
            error = exc
            logger.warning("Desktop IPC read loop stopped: %s", exc)
        finally:
            if generation == self._generation and not self._closing:
                await self._transport_lost(error)

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "response":
            request_id = message.get("requestId")
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)
            return

        if message_type == "client-discovery-request":
            request_id = message.get("requestId")
            if not isinstance(request_id, str):
                raise DesktopIPCProtocolError(
                    "client-discovery-request has no requestId"
                )
            await self._send_envelope(
                {
                    "type": "client-discovery-response",
                    "requestId": request_id,
                    "response": {"canHandle": False},
                }
            )
            return

        if message_type != "broadcast":
            logger.debug("Ignoring unsupported Desktop IPC message: %r", message_type)
            return

        method = message.get("method")
        if method == METHOD_FOLLOWING_STATUS_REQUESTED:
            params = message.get("params")
            source_client_id = message.get("sourceClientId")
            if isinstance(params, dict) and isinstance(source_client_id, str):
                await self._reply_following_status(params, source_client_id)

        if method == METHOD_STATE_CHANGED:
            params = message.get("params")
            if isinstance(params, dict):
                source_client_id = message.get("sourceClientId")
                if isinstance(source_client_id, str) and source_client_id:
                    conversation_id = params.get("conversationId")
                    host_id = params.get("hostId")
                    if isinstance(conversation_id, str) and isinstance(host_id, str):
                        with self._data_lock:
                            self._owners[(host_id, conversation_id)] = source_client_id
                self._accept_state_change(params)
                with self._data_lock:
                    state_callbacks = list(self._state_callbacks)
                self._dispatch_callbacks(state_callbacks, params)

        with self._data_lock:
            callbacks = list(self._broadcast_callbacks.get(str(method), []))
        if callbacks:
            self._dispatch_callbacks(callbacks, message)

    def _accept_state_change(self, params: Dict[str, Any]) -> None:
        conversation_id = params.get("conversationId")
        host_id = params.get("hostId")
        change = params.get("change")
        if (
            not isinstance(conversation_id, str)
            or not isinstance(host_id, str)
            or not isinstance(change, dict)
        ):
            return

        key = (host_id, conversation_id)
        change_type = change.get("type")
        revision = change.get("revision")
        with self._data_lock:
            if not self.cache_state:
                if change_type == "snapshot":
                    self._state_cache[key] = {"revision": revision, "state": None}
                    self._resync_attempts.pop(key, None)
                    self._patch_only.discard(key)
                    self._cancel_resync_locked(key)
                elif change_type == "patches":
                    cached = self._state_cache.get(key)
                    if cached is None or cached.get("revision") != change.get("baseRevision"):
                        self._schedule_resync_locked(key)
                    else:
                        cached["revision"] = revision
                    self._patch_only.add(key)
                return
            if change_type == "snapshot":
                self._state_cache[key] = {
                    "revision": revision,
                    "state": copy.deepcopy(change.get("conversationState")),
                }
                self._resync_attempts.pop(key, None)
                self._patch_only.discard(key)
                self._cancel_resync_locked(key)
                return

            if change_type != "patches":
                return
            cached = self._state_cache.get(key)
            if cached is None or cached.get("revision") != change.get("baseRevision"):
                # 不把 patches 套到未知/过期 snapshot 上，避免悄悄制造错误状态。
                self._state_cache.pop(key, None)
                self._schedule_resync_locked(key)
                return
            try:
                state = copy.deepcopy(cached.get("state"))
                for patch in change.get("patches") or []:
                    state = self._apply_patch(state, patch)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                logger.warning("Cannot apply Desktop Immer patches: %s", exc)
                self._state_cache.pop(key, None)
                self._schedule_resync_locked(key)
                return
            self._state_cache[key] = {"revision": revision, "state": state}

    async def _reply_following_status(
        self, params: Mapping[str, Any], target_client_id: str
    ) -> None:
        conversation_id = params.get("conversationId")
        host_id = params.get("hostId")
        if not isinstance(conversation_id, str) or not isinstance(host_id, str):
            return
        with self._data_lock:
            is_followed = (host_id, conversation_id) in self._followed
        if not is_followed:
            return
        await self.broadcast(
            METHOD_FOLLOWING_CHANGED,
            {
                "conversationId": conversation_id,
                "hostId": host_id,
                "following": True,
            },
            version=THREAD_REQUEST_VERSION,
            target_client_ids=[target_client_id],
        )

    @staticmethod
    def _apply_patch(root: Any, patch: Mapping[str, Any]) -> Any:
        op = patch.get("op")
        path = patch.get("path")
        if op not in ("add", "replace", "remove") or not isinstance(path, list):
            raise ValueError("invalid Immer patch")
        if not path:
            return None if op == "remove" else copy.deepcopy(patch.get("value"))

        parent = root
        for segment in path[:-1]:
            if isinstance(parent, list):
                if not isinstance(segment, int):
                    raise TypeError("array path segment must be int")
                parent = parent[segment]
            elif isinstance(parent, dict):
                parent = parent[segment]
            else:
                raise TypeError("patch path traverses a scalar")

        leaf = path[-1]
        if isinstance(parent, list):
            if leaf == "-" and op == "add":
                parent.append(copy.deepcopy(patch.get("value")))
            elif not isinstance(leaf, int):
                raise TypeError("array path segment must be int")
            elif op == "add":
                parent.insert(leaf, copy.deepcopy(patch.get("value")))
            elif op == "replace":
                parent[leaf] = copy.deepcopy(patch.get("value"))
            else:
                del parent[leaf]
        elif isinstance(parent, dict):
            if op == "remove":
                del parent[leaf]
            else:
                parent[leaf] = copy.deepcopy(patch.get("value"))
        else:
            raise TypeError("patch parent is a scalar")
        return root

    def _dispatch_callbacks(
        self, callbacks: List[Callable[[Dict[str, Any]], Any]], payload: Dict[str, Any]
    ) -> None:
        for callback in list(callbacks):
            try:
                result = callback(payload)
                if inspect.isawaitable(result):
                    task = asyncio.create_task(result)
                    self._callback_tasks.add(task)
                    task.add_done_callback(self._callback_done)
            except Exception:
                logger.exception("Desktop IPC callback failed")

    def _callback_done(self, task: asyncio.Task) -> None:
        self._callback_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Desktop IPC async callback failed")

    async def _transport_lost(self, error: Optional[BaseException]) -> None:
        self._ready.clear()
        self.client_id = None
        with self._data_lock:
            self._owners.clear()
        await self._cancel_resync_tasks()
        await self._close_transport(cancel_reader=False)
        self._fail_pending(
            error
            if isinstance(error, Exception)
            else DesktopIPCDisconnected("Desktop IPC connection lost")
        )
        if self.reconnect_enabled and not self._closing:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(), name="desktop-ipc-reconnect"
            )

    async def _reconnect_loop(self) -> None:
        delay = self.reconnect_delay
        while not self._closing and not self.connected:
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Desktop IPC reconnect failed: %s", exc)
                delay = min(
                    self.max_reconnect_delay,
                    max(self.reconnect_delay, delay * 2 or 0.05),
                )

    async def _restore_following(self) -> None:
        """initialize 后恢复断线前的 follower 订阅。"""

        with self._data_lock:
            followed = list(self._followed)
        for host_id, conversation_id in followed:
            if self._closing or not self.connected:
                return
            try:
                await self.broadcast(
                    METHOD_FOLLOWING_CHANGED,
                    {
                        "conversationId": conversation_id,
                        "hostId": host_id,
                        "following": True,
                    },
                    version=THREAD_REQUEST_VERSION,
                    target_client_ids=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 一个已关闭的线程不能阻止其余线程恢复订阅。
                logger.warning(
                    "Cannot restore Desktop thread subscription %s: %s",
                    conversation_id,
                    exc,
                )

    def _schedule_resync_locked(self, key: Tuple[str, str]) -> None:
        """合并连续 revision gap，并在冷却期后请求新 snapshot。"""

        existing = self._resync_tasks.get(key)
        if existing is not None and not existing.done():
            return
        attempts = self._resync_attempts.get(key, 0)
        if attempts >= 2:
            self._patch_only.add(key)
            return
        now = time.monotonic()
        last = self._last_resync_at.get(key, 0.0)
        delay = max(self.resync_delay, last + self.resync_cooldown - now)
        self._last_resync_at[key] = now + delay
        self._resync_attempts[key] = attempts + 1
        task = asyncio.create_task(
            self._resync_following(key, delay),
            name="desktop-ipc-resync-%s" % key[1],
        )
        self._resync_tasks[key] = task
        task.add_done_callback(lambda completed, item=key: self._resync_done(item, completed))

    async def _resync_following(
        self, key: Tuple[str, str], delay: float
    ) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        if self._closing:
            return
        host_id, conversation_id = key
        await self.broadcast(
            METHOD_FOLLOWING_CHANGED,
            {
                "conversationId": conversation_id,
                "hostId": host_id,
                "following": True,
            },
            version=THREAD_REQUEST_VERSION,
            target_client_ids=None,
        )
        with self._data_lock:
            self._followed.add(key)

    def _resync_done(
        self, key: Tuple[str, str], task: asyncio.Task
    ) -> None:
        with self._data_lock:
            if self._resync_tasks.get(key) is task:
                self._resync_tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception(
                "Failed to request fresh Desktop snapshot for %s", key[1]
            )

    def _cancel_resync_locked(self, key: Tuple[str, str]) -> None:
        task = self._resync_tasks.pop(key, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _cancel_resync_tasks(self) -> None:
        with self._data_lock:
            tasks = list(self._resync_tasks.values())
            self._resync_tasks.clear()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        pending = []
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
                pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _close_transport(self, *, cancel_reader: bool) -> None:
        self._ready.clear()
        reader_task = self._reader_task
        self._reader_task = None
        if (
            cancel_reader
            and reader_task is not None
            and reader_task is not asyncio.current_task()
        ):
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            try:
                writer.close()
                wait_closed = getattr(writer, "wait_closed", None)
                if wait_closed is not None:
                    await wait_closed()
            except Exception:
                pass

    def _fail_pending(self, error: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)


# 两个名称都保留，方便上层按 client 或 bridge 语义导入。
CodexDesktopIPC = DesktopIPCClient
DesktopIPCBridge = DesktopIPCClient


__all__ = [
    "CodexDesktopIPC",
    "DesktopIPCBridge",
    "DesktopIPCClient",
    "DesktopIPCDisconnected",
    "DesktopIPCError",
    "DesktopIPCProtocolError",
    "DesktopIPCRemoteError",
    "DesktopIPCTimeoutError",
    "MAX_MESSAGE_SIZE",
    "encode_frame",
    "read_frame",
]
