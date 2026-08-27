"""把 Codex Desktop 线程桥接到每个飞书聊天的一张可更新卡片。

只渲染 :mod:`desktop_card` 生成的白名单投影。Desktop 的
``conversationState`` 仅在内存中用于应用 Immer 补丁，绝不会写入绑定文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mmap
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .card_service import CardState
from .codex_app_server import CodexAppServerClient
from .desktop_card import (
    NORMALIZED_VERSION,
    build_desktop_card,
    normalize_conversation_state,
    normalize_patch_only_update,
)
from .desktop_notifications import DesktopCompletionMonitor
from .desktop_ipc import DEFAULT_HOST_ID, DesktopIPCClient, DesktopIPCRemoteError


logger = logging.getLogger("DesktopBridge")

ROLLOUT_SEED_TAIL_BYTES = 8 * 1024 * 1024
ROLLOUT_QUERY_SCAN_BYTES = 64 * 1024 * 1024
ROLLOUT_QUERY_LINE_BYTES = 1024 * 1024

class DesktopBridgeManager:
    """管理聊天与 Desktop 线程的绑定和实时卡片。"""

    def __init__(
        self,
        card_service: Any,
        ipc_client: Optional[DesktopIPCClient] = None,
        *,
        bindings_path: Optional[Path] = None,
        session_index_path: Optional[Path] = None,
        sessions_dir: Optional[Path] = None,
        archived_sessions_dir: Optional[Path] = None,
        state_db_path: Optional[Path] = None,
        global_state_path: Optional[Path] = None,
        notification_state_path: Optional[Path] = None,
        notification_poll_interval: float = 2.0,
        app_server_client: Optional[Any] = None,
        reconnect_interval: float = 1.0,
        card_update_interval: float = 0.5,
    ) -> None:
        self.card_service = card_service
        self.ipc = ipc_client or DesktopIPCClient(cache_state=False)
        self.bindings_path = Path(
            bindings_path
            or Path.home() / ".remote-claude" / "desktop_chat_bindings.json"
        )
        self.session_index_path = Path(
            session_index_path or Path.home() / ".codex" / "session_index.jsonl"
        )
        self.sessions_dir = Path(sessions_dir or Path.home() / ".codex" / "sessions")
        self.archived_sessions_dir = Path(
            archived_sessions_dir or Path.home() / ".codex" / "archived_sessions"
        )
        self.state_db_path = Path(state_db_path) if state_db_path else None
        self.global_state_path = Path(
            global_state_path or Path.home() / ".codex" / ".codex-global-state.json"
        )
        self.app_server = app_server_client or CodexAppServerClient()
        self.reconnect_interval = max(0.05, reconnect_interval)
        self.card_update_interval = max(0.0, card_update_interval)

        self._bindings: Dict[str, str] = self._load_bindings()
        self._users: Dict[str, str] = {}
        self._turn_views: Dict[str, Optional[str]] = {}
        self._states: Dict[str, Dict[str, Any]] = {}
        self._thread_metadata: Dict[str, Dict[str, Any]] = {}
        self._rollout_status_cache: Dict[str, tuple[int, int, str]] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._operation_locks: Dict[str, asyncio.Lock] = {}
        self._unarchive_locks: Dict[str, asyncio.Lock] = {}
        self._resolved_requests: Dict[tuple[str, str, str, str, str], float] = {}
        self._recent_messages: Dict[tuple[str, str], float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_card_states: Dict[str, Mapping[str, Any]] = {}
        self._card_flush_tasks: Dict[str, asyncio.Task] = {}
        self._start_lock: Optional[asyncio.Lock] = None
        self._started = False
        self._closed = False
        self.notifications = DesktopCompletionMonitor(
            card_service,
            self._notification_sources,
            state_path=notification_state_path,
            poll_interval=notification_poll_interval,
        )

    # -- 生命周期 ------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    @property
    def has_bindings(self) -> bool:
        return bool(self._bindings) or self.notifications.has_targets

    async def start(self) -> bool:
        """连接 Desktop 并恢复所有持久化的跟随关系。"""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            self.notifications.start()
            if self._started and not self._closed:
                return bool(self.ipc.connected)
            self._closed = False
            self.ipc.add_state_listener(self._on_state_change)
            try:
                await self.ipc.connect()
            except Exception:
                self.ipc.remove_state_listener(self._on_state_change)
                logger.exception("无法连接 Codex Desktop IPC")
                return False

            self._started = True
            for chat_id, thread_id in list(self._bindings.items()):
                state = self._seed_state_from_rollout(thread_id)
                self._states[thread_id] = state
                await self._publish_card(chat_id, state, replace=True)
            for thread_id in set(self._bindings.values()):
                await self._follow_and_request_snapshot(thread_id, quiet=True)
            return True

    async def close(self) -> None:
        self._closed = True
        self._started = False
        await self.notifications.close()
        background = [
            task for task in self._background_tasks
            if task is not asyncio.current_task()
        ]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._background_tasks.clear()
        card_tasks = list(self._card_flush_tasks.values())
        for task in card_tasks:
            task.cancel()
        if card_tasks:
            await asyncio.gather(*card_tasks, return_exceptions=True)
        self._card_flush_tasks.clear()
        self._pending_card_states.clear()
        self.ipc.remove_state_listener(self._on_state_change)
        try:
            await self.ipc.disconnect()
        finally:
            await self.app_server.close()

    # -- 绑定 ----------------------------------------------------------

    async def attach(
        self,
        chat_id: str,
        user_id: str,
        thread_id: str,
        *,
        owner_timeout: Optional[float] = None,
    ) -> bool:
        chat_id = str(chat_id).strip()
        thread_id = _clean_thread_id(thread_id)
        if not chat_id or not thread_id:
            return False
        if not self._started and not await self.start():
            return False

        await self._cancel_card_flush(chat_id)

        previous = self._bindings.get(chat_id)
        try:
            if owner_timeout is None:
                owner = await self.ipc.discover_owner(thread_id)
            else:
                owner = await self.ipc.discover_owner(
                    thread_id, timeout=owner_timeout
                )
        except Exception:
            if owner_timeout is None:
                logger.exception(
                    "无法将聊天 %s 绑定到 Desktop 线程 %s", chat_id, thread_id
                )
            else:
                logger.debug(
                    "Desktop 线程尚未加载: %s", thread_id, exc_info=True
                )
            return False

        # follow 可能立即触发 snapshot，先建立内存绑定才能接住该事件。
        self._bindings[chat_id] = thread_id
        self._turn_views.pop(chat_id, None)
        if user_id:
            self._users[chat_id] = str(user_id)
        state = self._states.get(thread_id) or self._seed_state_from_rollout(thread_id)
        self._states[thread_id] = state
        await self._publish_card(chat_id, state, replace=True)
        try:
            await self.ipc.follow(thread_id, owner_client_id=owner)
        except Exception:
            if previous is None:
                self._bindings.pop(chat_id, None)
            else:
                self._bindings[chat_id] = previous
            logger.exception("无法跟随 Desktop 线程 %s", thread_id)
            return False

        self._save_bindings()

        if previous and previous != thread_id and previous not in self._bindings.values():
            try:
                await self.ipc.unfollow(previous)
            except Exception:
                logger.debug("无法取消跟随之前的 Desktop 线程", exc_info=True)

        return True

    async def detach(self, chat_id: str) -> None:
        await self._cancel_card_flush(chat_id)
        thread_id = self._bindings.pop(chat_id, None)
        self._users.pop(chat_id, None)
        self._turn_views.pop(chat_id, None)
        self._save_bindings()
        if thread_id and thread_id not in self._bindings.values():
            try:
                await self.ipc.unfollow(thread_id)
            except Exception:
                logger.debug("无法取消跟随已解绑的 Desktop 线程", exc_info=True)
            self._states.pop(thread_id, None)
        clear = getattr(self.card_service, "clear_active_card", None)
        if callable(clear):
            clear(chat_id)

    def is_attached(self, chat_id: str) -> bool:
        return chat_id in self._bindings

    def binding_for(self, chat_id: str) -> Optional[str]:
        return self._bindings.get(chat_id)

    def state_for_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        thread_id = self._bindings.get(chat_id)
        state = self._states.get(thread_id) if thread_id else None
        return dict(state) if isinstance(state, Mapping) else None

    async def refresh_card(self, chat_id: str) -> bool:
        state = self.state_for_chat(chat_id)
        return await self._publish_card(chat_id, state) if state is not None else False

    async def register_notification_target(self, user_id: str) -> bool:
        """Register a Lark user for Desktop turn completion notifications."""

        registered = await self.notifications.register_target(user_id)
        if registered:
            self.notifications.start()
        return registered

    async def select_turn(
        self, chat_id: str, expected_thread_id: str, target_turn_id: str
    ) -> bool:
        """Select one historical turn for a chat without changing thread state."""

        thread_id = self._bindings.get(chat_id)
        target_turn_id = str(target_turn_id or "").strip()
        if thread_id != _clean_thread_id(expected_thread_id) or not target_turn_id:
            return False
        state = self._states.get(thread_id)
        if not isinstance(state, Mapping):
            return False
        turns = state.get("turns")
        if not isinstance(turns, list) or not any(
            isinstance(turn, Mapping) and turn.get("turn_id") == target_turn_id
            for turn in turns
        ):
            return False
        self._turn_views[chat_id] = target_turn_id
        return await self._publish_card(chat_id, state)

    # -- 线程发现 ------------------------------------------------------

    def list_threads(
        self, limit: Optional[int] = 20, *, include_internal: bool = False
    ) -> List[Dict[str, Any]]:
        """仅使用安全的索引和元数据字段返回最近的 Desktop 会话。"""

        if limit is not None and limit <= 0:
            return []
        state_rows = self._state_db_threads(archived=False)
        if state_rows is not None:
            results = self._normalize_catalog_rows(
                state_rows, archived=False, include_internal=include_internal
            )
            return results if limit is None else results[:limit]
        indexed = self._read_session_index()
        archived_ids = set(self._archived_rollout_metadata())

        metadata = self._rollout_metadata(set(indexed))
        project_catalog = self._load_project_catalog()
        self._thread_metadata.update(metadata)
        results: List[Dict[str, Any]] = []
        for item in indexed.values():
            if item["id"] in archived_ids:
                continue
            meta = metadata.get(item["id"])
            if meta:
                item.update(meta)
            # 已确定不是 Desktop 创建的会话不会有 Desktop owner。
            if (
                item.get("originator") not in (None, "Codex Desktop")
                or item.get("_is_child")
            ):
                continue
            if not item.get("cwd"):
                item["cwd"] = (
                    project_catalog["workspace_hints"].get(item["id"])
                    or project_catalog["projectless_output_dirs"].get(item["id"])
                )
            item["project_name"] = self._project_name_for_thread(
                item["id"],
                item.get("cwd"),
                item.get("_git_repository_url"),
                project_catalog,
            )
            live_state = self._states.get(item["id"])
            live_status = (
                live_state.get("status") if isinstance(live_state, Mapping) else None
            )
            item["status"] = _list_status(live_status)
            if item["status"] == "idle" and live_status in (None, "unknown"):
                item["status"] = self._rollout_status(item.get("_rollout_path"))
            item.pop("_git_repository_url", None)
            item.pop("_is_child", None)
            if not include_internal:
                item.pop("_rollout_path", None)
            results.append(item)
        results.sort(
            key=lambda item: (item.get("_activity_mtime", 0), item["updated_at"]),
            reverse=True,
        )
        for item in results:
            item.pop("_activity_mtime", None)
            if not include_internal:
                item.pop("_rollout_path", None)
        return results if limit is None else results[:limit]

    def list_archived_threads(
        self, limit: Optional[int] = 20, *, include_internal: bool = False
    ) -> List[Dict[str, Any]]:
        """Return archived top-level Desktop sessions for the Lark picker."""

        if limit is not None and limit <= 0:
            return []
        state_rows = self._state_db_threads(archived=True)
        if state_rows is not None:
            results = self._normalize_catalog_rows(
                state_rows, archived=True, include_internal=include_internal
            )
            return results if limit is None else results[:limit]
        return self._list_archived_threads_from_filesystem(
            limit, include_internal=include_internal
        )

    def _list_archived_threads_from_filesystem(
        self, limit: Optional[int] = 20, *, include_internal: bool = False
    ) -> List[Dict[str, Any]]:
        """Scan archived rollouts without consulting the optional state DB."""

        if limit is not None and limit <= 0:
            return []
        indexed = self._read_session_index()
        metadata = self._archived_rollout_metadata()
        project_catalog = self._load_project_catalog()
        results: List[Dict[str, Any]] = []
        for thread_id, meta in metadata.items():
            if meta.get("originator") != "Codex Desktop" or meta.get("_is_child"):
                continue
            indexed_item = indexed.get(thread_id) or {}
            cwd = meta.get("cwd") or indexed_item.get("cwd")
            updated_at = indexed_item.get("updated_at")
            if not isinstance(updated_at, str) or not updated_at:
                updated_at = _iso_timestamp(meta.get("_activity_mtime"))
            item = {
                "id": thread_id,
                "thread_id": thread_id,
                "title": _safe_title(indexed_item.get("title")),
                "updated_at": updated_at,
                "cwd": cwd if isinstance(cwd, str) else None,
                "originator": "Codex Desktop",
                "project_name": self._project_name_for_thread(
                    thread_id,
                    cwd,
                    meta.get("_git_repository_url"),
                    project_catalog,
                ),
                "status": self._rollout_status(meta.get("_rollout_path")),
                "_activity_mtime": meta.get("_activity_mtime", 0),
                "_rollout_path": meta.get("_rollout_path"),
            }
            results.append(item)
        results.sort(
            key=lambda item: (item.get("_activity_mtime", 0), item["updated_at"]),
            reverse=True,
        )
        for item in results:
            item.pop("_activity_mtime", None)
            if not include_internal:
                item.pop("_rollout_path", None)
        return results if limit is None else results[:limit]

    async def get_archived_threads(self) -> List[Dict[str, Any]]:
        """Use the official App Server list API, with a filesystem fallback."""

        try:
            raw_threads = await self.app_server.list_threads(
                True,
                source_kinds=["vscode", "exec", "appServer"],
                # Let App Server scan archived rollouts and repair missing
                # metadata.  State-DB-only mode can silently omit sessions
                # created by or migrated from older Codex versions.
                use_state_db_only=False,
            )
            return await asyncio.to_thread(
                self._normalize_app_server_threads, raw_threads, True
            )
        except Exception:
            logger.exception("Codex App Server 归档列表失败，使用本地索引回退")
            # Do not route through list_archived_threads here: a present but
            # incomplete state DB is one reason App Server may have failed to
            # provide the old archived rollouts in the first place.
            return await asyncio.to_thread(
                self._list_archived_threads_from_filesystem, None
            )

    def is_archived_thread(self, thread_id: str) -> bool:
        return _clean_thread_id(thread_id) in self._archived_rollout_metadata()

    async def unarchive_thread(self, thread_id: str) -> bool:
        thread_id = _clean_thread_id(thread_id)
        if not thread_id:
            return False
        lock = self._unarchive_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            # Recheck after acquiring the per-thread lock.  Feishu can deliver
            # the same card callback more than once, and a second click should
            # observe the first successful restore rather than report failure.
            if not self.is_archived_thread(thread_id):
                return True
            try:
                await self.app_server.unarchive_thread(thread_id)
            except Exception:
                # A concurrent Desktop action can win outside this process.
                # Treat the desired final state as success when the archived
                # rollout disappeared before App Server returned its error.
                if not self.is_archived_thread(thread_id):
                    return True
                logger.exception("无法移出归档 Desktop 线程 %s", thread_id)
                return False
            old = self._thread_metadata.pop(thread_id, None)
            if isinstance(old, Mapping):
                old_path = old.get("_rollout_path")
                if isinstance(old_path, str):
                    self._rollout_status_cache.pop(old_path, None)
            return True

    def _read_session_index(self) -> Dict[str, Dict[str, Any]]:
        indexed: Dict[str, Dict[str, Any]] = {}
        try:
            with self.session_index_path.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(item, Mapping):
                        continue
                    thread_id = _clean_thread_id(item.get("id"))
                    updated_at = item.get("updated_at")
                    if not thread_id or not isinstance(updated_at, str):
                        continue
                    old = indexed.get(thread_id)
                    if old is None or updated_at >= old["updated_at"]:
                        indexed[thread_id] = {
                            "id": thread_id,
                            "thread_id": thread_id,
                            # Preserve an empty index name as empty so callers
                            # can fall back to App Server/state DB metadata.
                            "title": _safe_label(item.get("thread_name"), 200),
                            "updated_at": updated_at,
                            "cwd": None,
                            "originator": None,
                        }
        except OSError:
            return {}
        return indexed

    def _rollout_metadata(self, wanted: set[str]) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {
            thread_id: dict(self._thread_metadata[thread_id])
            for thread_id in wanted
            if thread_id in self._thread_metadata
        }
        missing = wanted - set(found)
        if not missing or not self.sessions_dir.exists():
            return found
        try:
            if len(missing) <= 4:
                paths = (
                    path
                    for thread_id in missing
                    for path in self.sessions_dir.glob(f"**/rollout-*-{thread_id}.jsonl")
                )
            else:
                paths = self.sessions_dir.glob("**/rollout-*.jsonl")
            for path in paths:
                try:
                    with path.open("r", encoding="utf-8") as source:
                        first = source.readline()
                    record = json.loads(first)
                    payload = record.get("payload") if isinstance(record, Mapping) else None
                    if not isinstance(payload, Mapping):
                        continue
                    item_id = _clean_thread_id(payload.get("id"))
                    session_id = _clean_thread_id(payload.get("session_id"))
                    # 子 Agent 的 rollout 常复用根 session_id；仅允许自身 id
                    # 与索引线程一致的 rollout 为该线程提供元数据。
                    thread_id = item_id if item_id in missing else None
                    if thread_id is None and session_id in missing and not payload.get("parent_thread_id"):
                        thread_id = session_id
                    if thread_id is None:
                        continue
                    timestamp = payload.get("timestamp")
                    previous = found.get(thread_id)
                    if previous and isinstance(timestamp, str) and timestamp < previous.get("_timestamp", ""):
                        continue
                    cwd = payload.get("cwd")
                    originator = payload.get("originator")
                    git = payload.get("git")
                    repository_url = (
                        git.get("repository_url") if isinstance(git, Mapping) else None
                    )
                    found[thread_id] = {
                        "cwd": cwd if isinstance(cwd, str) else None,
                        "originator": originator if isinstance(originator, str) else None,
                        "_is_child": bool(payload.get("parent_thread_id")),
                        "_git_repository_url": (
                            repository_url if isinstance(repository_url, str) else None
                        ),
                        "_rollout_path": str(path),
                        "_timestamp": timestamp if isinstance(timestamp, str) else "",
                        "_activity_mtime": path.stat().st_mtime,
                    }
                except (OSError, json.JSONDecodeError, TypeError):
                    continue
        except OSError:
            return found
        for value in found.values():
            value.pop("_timestamp", None)
        self._thread_metadata.update(found)
        return found

    def _archived_rollout_metadata(self) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        if not self.archived_sessions_dir.exists():
            return found
        try:
            paths = self.archived_sessions_dir.glob("**/rollout-*.jsonl")
            for path in paths:
                metadata = _read_rollout_metadata_file(path)
                thread_id = _clean_thread_id(metadata.get("thread_id"))
                if not thread_id:
                    continue
                previous = found.get(thread_id)
                modified = path.stat().st_mtime
                if previous and modified <= previous.get("_activity_mtime", 0):
                    continue
                metadata["_activity_mtime"] = modified
                metadata["_rollout_path"] = str(path)
                found[thread_id] = metadata
        except OSError:
            return found
        return found

    def _normalize_app_server_threads(
        self, raw_threads: Any, archived: bool, include_internal: bool = False
    ) -> List[Dict[str, Any]]:
        return self._normalize_catalog_rows(
            raw_threads, archived=archived, include_internal=include_internal
        )

    def _state_db_threads(self, *, archived: bool) -> Optional[List[Dict[str, Any]]]:
        path = self.state_db_path
        if path is None:
            candidates = sorted(
                self.session_index_path.parent.glob("state_*.sqlite"),
                key=lambda candidate: candidate.stat().st_mtime,
                reverse=True,
            )
            path = candidates[0] if candidates else None
        if path is None or not path.is_file():
            return None
        try:
            uri = "file:{}?mode=ro".format(str(path).replace("?", "%3F"))
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                connection.execute("PRAGMA query_only = ON")
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)")
                }
                required = {
                    "id", "rollout_path", "updated_at", "source", "cwd", "title", "archived"
                }
                if not required.issubset(columns):
                    return None
                name_expr = "name" if "name" in columns else "NULL"
                updated_ms_expr = "updated_at_ms" if "updated_at_ms" in columns else "NULL"
                recency_expr = "recency_at_ms" if "recency_at_ms" in columns else "updated_at"
                thread_source_expr = "thread_source" if "thread_source" in columns else "NULL"
                git_origin_expr = "git_origin_url" if "git_origin_url" in columns else "NULL"
                query = f"""
                    SELECT id, rollout_path, title, cwd, source, updated_at,
                           {updated_ms_expr}, {thread_source_expr}, {git_origin_expr},
                           {name_expr}
                    FROM threads
                    WHERE archived = ?
                    ORDER BY {recency_expr} DESC, id DESC
                """
                rows = connection.execute(query, (1 if archived else 0,)).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            logger.debug("无法读取 Codex state 数据库", exc_info=True)
            return None
        names = [
            "id", "path", "title", "cwd", "source", "updatedAt",
            "updatedAtMs", "threadSource", "gitOriginUrl", "name",
        ]
        return [dict(zip(names, row)) for row in rows]

    def _normalize_catalog_rows(
        self, rows: Any, *, archived: bool, include_internal: bool
    ) -> List[Dict[str, Any]]:
        project_catalog = self._load_project_catalog()
        # ``threads.title`` is often the initial user query and can remain
        # stale after a rename.  Desktop writes the current sidebar/session
        # label to session_index.jsonl, so prefer that value for display.
        indexed = self._read_session_index()
        results: List[Dict[str, Any]] = []
        if not isinstance(rows, list):
            return results
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            thread_id = _clean_thread_id(raw.get("id"))
            if (
                not thread_id
                or raw.get("threadSource") == "subagent"
                or raw.get("parentThreadId")
            ):
                continue
            path = _path_value(raw.get("path"))
            metadata = _read_rollout_metadata_file(path) if path is not None else {}
            if metadata.get("_is_child"):
                continue
            originator = metadata.get("originator")
            source = raw.get("source")
            if originator not in (None, "Codex Desktop"):
                continue
            if originator is None and source != "vscode":
                continue
            cwd = raw.get("cwd") if isinstance(raw.get("cwd"), str) else metadata.get("cwd")
            repository_url = raw.get("gitOriginUrl") or metadata.get("_git_repository_url")
            live_state = self._states.get(thread_id)
            live_status = (
                live_state.get("status") if isinstance(live_state, Mapping) else None
            )
            status = _list_status(live_status)
            if status == "idle" and live_status in (None, "unknown"):
                status = self._rollout_status(str(path) if path is not None else None)
            updated_at = _iso_timestamp(raw.get("updatedAtMs") or raw.get("updatedAt"))
            indexed_title = (indexed.get(thread_id) or {}).get("title")
            item = {
                "id": thread_id,
                "thread_id": thread_id,
                "title": _safe_title(
                    indexed_title or raw.get("name") or raw.get("title")
                ),
                "updated_at": updated_at,
                "cwd": cwd if isinstance(cwd, str) else None,
                "originator": originator or "Codex Desktop",
                "project_name": self._project_name_for_thread(
                    thread_id, cwd, repository_url, project_catalog
                ),
                "status": "idle" if archived and status == "running" else status,
                "_rollout_path": str(path) if path is not None else None,
            }
            results.append(item)
            self._thread_metadata[thread_id] = {
                "cwd": item["cwd"],
                "originator": item["originator"],
                "_git_repository_url": repository_url,
                "_rollout_path": item["_rollout_path"],
            }
        if not include_internal:
            for item in results:
                item.pop("_rollout_path", None)
        return results

    def _notification_sources(self) -> List[Mapping[str, Any]]:
        return [
            *self.list_threads(None, include_internal=True),
            *self.list_archived_threads(None, include_internal=True),
        ]

    def _rollout_status(self, raw_path: Any) -> str:
        """Return the last persisted turn status without parsing whole rollouts."""

        if not isinstance(raw_path, str) or not raw_path:
            return "idle"
        path = Path(raw_path)
        try:
            with path.open("rb") as source:
                stat = os.fstat(source.fileno())
                cache_key = str(path)
                cached = self._rollout_status_cache.get(cache_key)
                if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                    return cached[2]
                if stat.st_size <= 0:
                    status = "idle"
                else:
                    with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                        status = "idle"
                        end = stat.st_size
                        while end > 0:
                            if data[end - 1:end] == b"\n":
                                end -= 1
                            start = data.rfind(b"\n", 0, end) + 1
                            prefix = data[start:min(end, start + 4096)]
                            if b"task_" in prefix or b"turn_aborted" in prefix:
                                candidate = _rollout_record_status(data[start:end])
                                if candidate is not None:
                                    status = candidate
                                    break
                            end = start - 1
        except (OSError, ValueError):
            status = "idle"
            stat = None

        if stat is not None:
            self._rollout_status_cache[str(path)] = (
                stat.st_size,
                stat.st_mtime_ns,
                status,
            )
            if len(self._rollout_status_cache) > 512:
                oldest = next(iter(self._rollout_status_cache))
                self._rollout_status_cache.pop(oldest, None)
        return status

    def _load_project_catalog(self) -> Dict[str, Any]:
        """读取 Desktop 项目映射，只保留展示项目名所需的字段。"""

        empty = {
            "assignments": {},
            "projects": {},
            "projectless": set(),
            "workspace_hints": {},
            "projectless_output_dirs": {},
        }
        try:
            data = json.loads(self.global_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return empty
        if not isinstance(data, Mapping):
            return empty

        raw_projects = data.get("local-projects")
        projects: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_projects, Mapping):
            for project_id, raw_project in raw_projects.items():
                if not isinstance(project_id, str) or not isinstance(raw_project, Mapping):
                    continue
                name = _safe_label(raw_project.get("name"), 120)
                roots = raw_project.get("rootPaths")
                projects[project_id] = {
                    "name": name,
                    "root_paths": [
                        root for root in roots or []
                        if isinstance(root, str) and root.strip()
                    ] if isinstance(roots, list) else [],
                }

        raw_assignments = data.get("thread-project-assignments")
        assignments: Dict[str, str] = {}
        if isinstance(raw_assignments, Mapping):
            for thread_id, raw_assignment in raw_assignments.items():
                if not isinstance(thread_id, str) or not isinstance(raw_assignment, Mapping):
                    continue
                project_id = raw_assignment.get("projectId")
                if raw_assignment.get("projectKind") == "local" and isinstance(project_id, str):
                    assignments[thread_id] = project_id

        raw_projectless = data.get("projectless-thread-ids")
        projectless = {
            thread_id for thread_id in raw_projectless or []
            if isinstance(thread_id, str)
        } if isinstance(raw_projectless, list) else set()

        return {
            "assignments": assignments,
            "projects": projects,
            "projectless": projectless,
            "workspace_hints": _safe_string_map(data.get("thread-workspace-root-hints")),
            "projectless_output_dirs": _safe_string_map(
                data.get("thread-projectless-output-directories")
            ),
        }

    @staticmethod
    def _project_name_for_thread(
        thread_id: str,
        cwd: Any,
        repository_url: Any,
        catalog: Mapping[str, Any],
    ) -> str:
        assignments = catalog.get("assignments")
        projects = catalog.get("projects")
        if isinstance(assignments, Mapping) and isinstance(projects, Mapping):
            project_id = assignments.get(thread_id)
            project = projects.get(project_id)
            if isinstance(project, Mapping):
                name = _safe_label(project.get("name"), 120)
                if name:
                    return name

        projectless = catalog.get("projectless")
        if isinstance(projectless, set) and thread_id in projectless:
            return "无项目"

        cwd_text = cwd.strip() if isinstance(cwd, str) else ""
        if cwd_text and isinstance(projects, Mapping):
            matched_name = ""
            matched_depth = -1
            candidate = _resolved_path(cwd_text)
            for project in projects.values():
                if not isinstance(project, Mapping):
                    continue
                name = _safe_label(project.get("name"), 120)
                roots = project.get("root_paths")
                if not name or not isinstance(roots, list):
                    continue
                for root in roots:
                    resolved_root = _resolved_path(root)
                    if candidate is None or resolved_root is None:
                        continue
                    try:
                        candidate.relative_to(resolved_root)
                    except ValueError:
                        continue
                    depth = len(resolved_root.parts)
                    if depth > matched_depth:
                        matched_name = name
                        matched_depth = depth
            if matched_name:
                return matched_name

        repository_name = _repository_name(repository_url)
        if repository_name:
            return repository_name
        if cwd_text:
            return _safe_label(Path(cwd_text).name, 120) or "未知项目"
        return "未知项目"

    # -- 用户操作 ------------------------------------------------------

    async def send_message(
        self, chat_id: str, text: str, client_message_id: Optional[str] = None
    ) -> bool:
        thread_id = self._bindings.get(chat_id)
        text = str(text).strip()
        if not thread_id or not text:
            return False
        fingerprint = (
            str(client_message_id)
            if client_message_id
            else "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        message_key = (chat_id, fingerprint)
        now = time.monotonic()
        previous_at = self._recent_messages.get(message_key)
        dedupe_window = 3600.0 if client_message_id else 10.0
        if previous_at is not None and now - previous_at < dedupe_window:
            return True
        self._recent_messages[message_key] = now
        if len(self._recent_messages) > 4096:
            oldest = sorted(self._recent_messages, key=self._recent_messages.get)[:1024]
            for key in oldest:
                self._recent_messages.pop(key, None)
        lock = self._operation_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                state = self._states.get(thread_id, {})
                pending = state.get("pending") if isinstance(state, Mapping) else None
                if isinstance(pending, Mapping) and pending.get("kind") == "input":
                    if pending.get("unsupported"):
                        self._recent_messages.pop(message_key, None)
                        return False
                    accepted = await self.handle_input(
                        chat_id,
                        str(pending.get("request_kind") or "user_input"),
                        pending.get("request_id"),
                        text,
                        question_id=pending.get("question_id"),
                        _lock_held=True,
                    )
                    if not accepted:
                        self._recent_messages.pop(message_key, None)
                    else:
                        await self._return_to_latest_turn(chat_id, thread_id)
                    return accepted
                try:
                    await self.ipc.steer_turn(
                        thread_id,
                        text,
                        self._cwd_for_thread(thread_id),
                        client_user_message_id=client_message_id,
                    )
                except DesktopIPCRemoteError:
                    await self.ipc.start_turn(
                        thread_id,
                        text,
                        client_user_message_id=client_message_id,
                    )
                await self._return_to_latest_turn(chat_id, thread_id)
                return True
            except Exception:
                self._recent_messages.pop(message_key, None)
                logger.exception("无法向 Desktop 线程 %s 发送消息", thread_id)
                return False

    async def _return_to_latest_turn(self, chat_id: str, thread_id: str) -> None:
        """成功发送后让当前聊天回到最新一轮，不影响同线程的其他聊天。"""

        if self._turn_views.pop(chat_id, None) is None:
            return
        state = self._states.get(thread_id)
        if self._bindings.get(chat_id) != thread_id or not isinstance(state, Mapping):
            return
        try:
            await self._publish_card(chat_id, state)
        except Exception:
            # 消息已经提交给 Desktop，卡片刷新失败不应被误报为发送失败。
            logger.exception("发送后无法将 Desktop 卡片切回最新轮")

    async def interrupt(self, chat_id: str, expected_turn_id: Any = None) -> bool:
        thread_id = self._bindings.get(chat_id)
        if not thread_id:
            return False
        current_turn_id = (self._states.get(thread_id) or {}).get("active_turn_id")
        if expected_turn_id is not None and str(expected_turn_id) != str(current_turn_id):
            return False
        try:
            await self.ipc.interrupt(
                thread_id,
                expected_turn_id=current_turn_id,
            )
            return True
        except Exception:
            logger.exception("无法停止 Desktop 线程 %s", thread_id)
            return False

    async def handle_approval(
        self, chat_id: str, kind: str, request_id: Any, decision: Any
    ) -> bool:
        thread_id = self._bindings.get(chat_id)
        if not thread_id or not self._pending_matches(thread_id, kind, request_id, "approval"):
            return False
        pending = self._states[thread_id].get("pending") or {}
        wire_request_id = pending.get("request_id")
        request_key = self._request_key(thread_id, pending, kind)
        if request_key in self._resolved_requests:
            return True
        lock = self._operation_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                if request_key in self._resolved_requests:
                    return True
                if kind == "command_execution":
                    await self.ipc.command_approval(thread_id, wire_request_id, decision)
                elif kind == "file_change":
                    if decision not in ("decline", "deny", "reject", False):
                        return False
                    await self.ipc.file_approval(thread_id, wire_request_id, decision)
                elif kind == "permissions":
                    if decision in ("decline", "deny", "reject", False):
                        response: Any = {"permissions": {}, "scope": "turn"}
                    elif isinstance(decision, Mapping):
                        response = decision
                    else:
                        response = pending.get("permissions_response")
                    if (
                        not isinstance(response, Mapping)
                        or not isinstance(response.get("permissions"), Mapping)
                        or response.get("scope") not in ("turn", "session")
                    ):
                        logger.warning("权限审批缺少安全的原始权限数据，请在 Desktop 中处理")
                        return False
                    await self.ipc.permissions_approval(thread_id, wire_request_id, response)
                else:
                    return False
                self._remember_resolved(request_key)
                return True
            except Exception:
                logger.exception("无法提交 Desktop 审批")
                return False

    async def handle_input(
        self,
        chat_id: str,
        kind: str,
        request_id: Any,
        response: Any,
        question_id: Optional[str] = None,
        _lock_held: bool = False,
    ) -> bool:
        thread_id = self._bindings.get(chat_id)
        if not thread_id or not self._pending_matches(thread_id, kind, request_id, "input"):
            return False
        pending = self._states[thread_id].get("pending") or {}
        wire_request_id = pending.get("request_id")
        if not isinstance(response, Mapping):
            question_id = question_id or pending.get("question_id")
            response = (
                {"answers": {str(question_id): {"answers": [str(response)]}}}
                if question_id
                else {"answer": response}
            )
        request_key = self._request_key(thread_id, pending, kind)
        if request_key in self._resolved_requests:
            return True

        async def submit() -> bool:
            try:
                if request_key in self._resolved_requests:
                    return True
                await self.ipc.submit_user_input(thread_id, wire_request_id, response)
                self._remember_resolved(request_key)
                return True
            except Exception:
                logger.exception("无法提交 Desktop 用户输入")
                return False

        if _lock_held:
            return await submit()
        lock = self._operation_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            return await submit()

    def _request_key(
        self, thread_id: str, pending: Mapping[str, Any], kind: str
    ) -> tuple[str, str, str, str, str]:
        turn_id = str((self._states.get(thread_id) or {}).get("active_turn_id") or "")
        prompt_hash = hashlib.sha256(
            str(pending.get("prompt") or "").encode("utf-8")
        ).hexdigest()[:16]
        return (
            thread_id,
            turn_id,
            str(pending.get("request_id")),
            str(kind),
            prompt_hash,
        )

    def _remember_resolved(self, key: tuple[str, str, str, str, str]) -> None:
        self._resolved_requests[key] = time.monotonic()
        if len(self._resolved_requests) > 2048:
            oldest = sorted(self._resolved_requests, key=self._resolved_requests.get)[:512]
            for item in oldest:
                self._resolved_requests.pop(item, None)

    def _pending_matches(
        self, thread_id: str, kind: str, request_id: str, pending_kind: str
    ) -> bool:
        state = self._states.get(thread_id)
        pending = state.get("pending") if isinstance(state, Mapping) else None
        return bool(
            isinstance(pending, Mapping)
            and pending.get("kind") == pending_kind
            and str(pending.get("request_kind")) == str(kind)
            and str(pending.get("request_id")) == str(request_id)
        )

    # -- IPC 状态和卡片 -----------------------------------------------

    async def _on_state_change(self, params: Dict[str, Any]) -> None:
        thread_id = _clean_thread_id(params.get("conversationId"))
        if not thread_id or thread_id not in self._bindings.values():
            return
        previous = self._states.get(thread_id)
        change = params.get("change") if isinstance(params, Mapping) else None
        if isinstance(change, Mapping) and change.get("type") == "snapshot":
            normalized = normalize_conversation_state(
                change.get("conversationState"), previous, retain_raw=False
            )
            normalized["thread_id"] = thread_id
            normalized["host_id"] = str(params.get("hostId") or DEFAULT_HOST_ID)
            normalized["revision"] = change.get("revision")
            normalized["needs_snapshot"] = not normalized.get("schema_known")
            normalized["patch_only"] = True
        else:
            normalized = normalize_patch_only_update(previous, params)
        self._states[thread_id] = normalized
        if _public_state_key(previous) != _public_state_key(normalized):
            immediate = (
                normalized.get("pending") != (previous or {}).get("pending")
                or (
                    normalized.get("status") != (previous or {}).get("status")
                    and normalized.get("status") in {
                    "waiting_approval", "waiting_input", "completed", "failed", "interrupted"
                    }
                )
            )
            for chat_id, bound_thread in list(self._bindings.items()):
                if bound_thread == thread_id:
                    if self.card_update_interval == 0:
                        await self._publish_card(chat_id, normalized)
                    else:
                        self._schedule_card_publish(chat_id, normalized, immediate=immediate)

    async def _follow_and_request_snapshot(self, thread_id: str, *, quiet: bool) -> bool:
        try:
            await self.ipc.follow(thread_id)
            return True
        except Exception:
            if not quiet:
                logger.exception("无法跟随 Desktop 线程 %s", thread_id)
            else:
                logger.debug("无法刷新 Desktop 线程 %s", thread_id, exc_info=True)
            return False

    async def _publish_card(
        self, chat_id: str, state: Mapping[str, Any], *, replace: bool = False
    ) -> bool:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            content = build_desktop_card(
                state, selected_turn_id=self._turn_views.get(chat_id)
            )
            active = None if replace else self.card_service.get_active_card(chat_id)
            if active is not None:
                active.sequence += 1
                updated = await self.card_service.update_card(
                    active.card_id, active.sequence, content
                )
                if updated:
                    active.last_update = time.time()
                    return True

            card_id = await self.card_service.create_card(content)
            if not card_id:
                return False
            message_id = await self.card_service.send_card(chat_id, card_id)
            if not message_id:
                return False
            self.card_service.set_active_card(
                chat_id, CardState(card_id=card_id, message_id=message_id)
            )
            return True

    def _schedule_card_publish(
        self, chat_id: str, state: Mapping[str, Any], *, immediate: bool = False
    ) -> None:
        self._pending_card_states[chat_id] = state
        existing = self._card_flush_tasks.get(chat_id)
        if existing is not None and not existing.done():
            return

        async def flush() -> None:
            try:
                if not immediate and self.card_update_interval:
                    await asyncio.sleep(self.card_update_interval)
                while chat_id in self._pending_card_states and not self._closed:
                    latest = self._pending_card_states.pop(chat_id)
                    await self._publish_card(chat_id, latest)
                    if chat_id in self._pending_card_states and self.card_update_interval:
                        await asyncio.sleep(self.card_update_interval)
            finally:
                self._card_flush_tasks.pop(chat_id, None)

        task = asyncio.create_task(flush(), name=f"desktop-card-flush-{chat_id[:8]}")
        self._card_flush_tasks[chat_id] = task

    async def _cancel_card_flush(self, chat_id: str) -> None:
        self._pending_card_states.pop(chat_id, None)
        task = self._card_flush_tasks.pop(chat_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _cwd_for_thread(self, thread_id: str) -> str:
        metadata = self._thread_metadata.get(thread_id)
        if metadata is None:
            metadata = self._rollout_metadata({thread_id}).get(thread_id, {})
            if metadata:
                self._thread_metadata[thread_id] = metadata
        cwd = metadata.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else os.getcwd()

    def _seed_state_from_rollout(self, thread_id: str) -> Dict[str, Any]:
        """从 rollout 尾部生成公开基线，兼容超过 IPC 帧上限的长线程。"""
        state = _loading_state(thread_id)
        state.update({"patch_only": True, "needs_snapshot": True})
        title = self._title_from_index(thread_id)
        if title:
            state["title"] = title

        matches = sorted(
            self.sessions_dir.glob(f"**/rollout-*-{thread_id}.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            return state

        messages: List[Dict[str, Any]] = []
        turns: List[Dict[str, Any]] = []
        current_turn: Optional[Dict[str, Any]] = None
        status = "unknown"
        active_turn_id: Any = None
        try:
            with matches[0].open("rb") as source:
                size = source.seek(0, os.SEEK_END)
                source.seek(max(0, size - ROLLOUT_SEED_TAIL_BYTES))
                if source.tell() > 0:
                    source.readline()
                for raw_line in source:
                    try:
                        record = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload") or {}
                    event_type = payload.get("type")
                    if event_type == "task_started":
                        status = "running"
                        active_turn_id = payload.get("turn_id")
                        turn_id = str(active_turn_id or record.get("timestamp") or "")
                        current_turn = {
                            "turn_id": turn_id,
                            "status": "running",
                            "user_messages": [],
                            "agent_messages": [],
                        }
                        turns.append(current_turn)
                    elif event_type == "task_complete":
                        status = "failed" if payload.get("error") is not None else "completed"
                        turn_id = str(payload.get("turn_id") or "")
                        if current_turn is None:
                            current_turn = {
                                "turn_id": turn_id or str(record.get("timestamp") or ""),
                                "status": status,
                                "user_messages": [],
                                "agent_messages": [],
                            }
                            turns.append(current_turn)
                        elif turn_id and current_turn.get("turn_id") != turn_id:
                            current_turn["turn_id"] = turn_id
                            for message in current_turn["agent_messages"]:
                                message["turn_id"] = turn_id
                        current_turn["status"] = status
                        active_turn_id = None
                        current_turn = None
                    elif event_type == "turn_aborted":
                        status = "interrupted"
                        if current_turn is not None:
                            current_turn["status"] = "interrupted"
                        active_turn_id = None
                        current_turn = None
                    elif event_type == "user_message":
                        text = payload.get("message")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        if current_turn is None:
                            current_turn = {
                                "turn_id": str(payload.get("turn_id") or record.get("timestamp") or ""),
                                "status": "unknown",
                                "user_messages": [],
                                "agent_messages": [],
                            }
                            turns.append(current_turn)
                        current_turn["user_messages"].append({
                            "id": str(payload.get("id") or record.get("timestamp") or ""),
                            "kind": (
                                "initial" if not current_turn["user_messages"] else "steering"
                            ),
                            "text": text.strip()[:4000],
                        })
                    elif event_type == "agent_message" and payload.get("phase") in {
                        "commentary", "final_answer"
                    }:
                        text = payload.get("message")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        message = {
                            "id": str(payload.get("id") or record.get("timestamp") or ""),
                            "turn_id": str(
                                payload.get("turn_id")
                                or (current_turn or {}).get("turn_id")
                                or ""
                            ),
                            "phase": payload.get("phase"),
                            "text": text.strip()[:4000],
                        }
                        messages.append(message)
                        if current_turn is None:
                            current_turn = {
                                "turn_id": message["turn_id"] or str(record.get("timestamp") or ""),
                                "status": "unknown",
                                "user_messages": [],
                                "agent_messages": [],
                            }
                            turns.append(current_turn)
                            message["turn_id"] = current_turn["turn_id"]
                        current_turn["agent_messages"].append(dict(message))
        except OSError:
            logger.debug("无法读取 Desktop rollout 基线", exc_info=True)
            return state
        if status == "unknown" and messages:
            status = (
                "running" if messages[-1].get("phase") == "commentary" else "completed"
            )
        if turns and not turns[-1]["user_messages"]:
            recent_users = _recent_rollout_user_messages(matches[0])
            if not recent_users:
                for turn in reversed(turns[:-1]):
                    if turn["user_messages"]:
                        recent_users = [dict(message) for message in turn["user_messages"]]
                        break
            if recent_users:
                # Goal continuation and other internal turns can emit a new
                # task_started without another user_message.  Keep the latest
                # user-authored query visible on that live turn.
                turns[-1]["user_messages"] = recent_users
        state["status"] = status
        state["active_turn_id"] = active_turn_id
        state["turns"] = turns[-20:]
        state["messages"] = messages[-20:]
        state["_patch_turn_ids"] = (
            {'["turns",0]': str(active_turn_id)} if active_turn_id else {}
        )
        return state

    def _title_from_index(self, thread_id: str) -> Optional[str]:
        title: Optional[str] = None
        try:
            with self.session_index_path.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(item, Mapping) and item.get("id") == thread_id:
                        title = _safe_title(item.get("thread_name"))
        except OSError:
            return None
        return title

    # -- 持久化 --------------------------------------------------------

    def _load_bindings(self) -> Dict[str, str]:
        try:
            data = json.loads(self.bindings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, Mapping):
            return {}
        result: Dict[str, str] = {}
        for chat_id, thread_id in data.items():
            cleaned = _clean_thread_id(thread_id)
            if isinstance(chat_id, str) and chat_id and cleaned:
                result[chat_id] = cleaned
        return result

    def _save_bindings(self) -> None:
        try:
            self.bindings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.bindings_path.with_suffix(self.bindings_path.suffix + ".tmp")
            # 只持久化 chat -> thread。_states 含私有 conversationState，禁止序列化。
            temporary.write_text(
                json.dumps(self._bindings, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.bindings_path)
        except OSError:
            logger.exception("无法持久化 Desktop 聊天绑定")


def _clean_thread_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    prefix = "codex://threads/"
    if value.startswith(prefix):
        value = value[len(prefix) :]
    return value if value and "/" not in value and "\x00" not in value else ""


def _safe_title(value: Any) -> str:
    if not isinstance(value, str):
        return "未命名任务"
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32).strip()
    return value[:199] + "…" if len(value) > 200 else (value or "未命名任务")


def _safe_label(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32).strip()
    return value[: limit - 1].rstrip() + "…" if len(value) > limit else value


def _safe_string_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item.strip()
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str) and item.strip()
    }


def _path_value(value: Any) -> Optional[Path]:
    if not isinstance(value, (str, Path)):
        return None
    text = str(value).strip()
    return Path(text) if text and "\x00" not in text else None


def _read_rollout_metadata_file(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as source:
            record = json.loads(source.readline())
    except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return {}
    payload = record.get("payload") if isinstance(record, Mapping) else None
    if not isinstance(payload, Mapping):
        return {}
    git = payload.get("git")
    repository_url = git.get("repository_url") if isinstance(git, Mapping) else None
    return {
        "thread_id": _clean_thread_id(payload.get("id") or payload.get("session_id")),
        "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
        "originator": (
            payload.get("originator") if isinstance(payload.get("originator"), str) else None
        ),
        "_is_child": bool(payload.get("parent_thread_id")),
        "_git_repository_url": (
            repository_url if isinstance(repository_url, str) else None
        ),
    }


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (OSError, OverflowError, ValueError):
            return ""
    return ""


def _resolved_path(value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _repository_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not value:
        return ""
    name = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return _safe_label(name, 120)


def _list_status(value: Any) -> str:
    if value in {"running", "waiting_approval", "waiting_input", "active"}:
        return "running"
    if value in {"failed", "systemError"}:
        return "failed"
    return "idle"


def _rollout_record_status(raw_line: Any) -> Optional[str]:
    try:
        record = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(record, Mapping) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    event_type = payload.get("type")
    if event_type == "task_started":
        return "running"
    if event_type == "task_complete":
        return "failed" if payload.get("error") is not None else "idle"
    if event_type == "turn_aborted":
        return "idle"
    return None


def _loading_state(thread_id: str) -> Dict[str, Any]:
    return {
        "schema_version": NORMALIZED_VERSION,
        "schema_known": False,
        "thread_id": thread_id,
        "host_id": DEFAULT_HOST_ID,
        "revision": None,
        "title": "Codex Desktop",
        "status": "unknown",
        "active_turn_id": None,
        "turns": [],
        "messages": [],
        "pending": None,
    }


def _recent_rollout_user_messages(path: Path) -> List[Dict[str, str]]:
    """在有界字节窗口内反向找最近一个用户轮的公开文本。

    rollout 可能有数百 MiB，且当前 turn 的工具输出可轻易超过初始
    8 MiB 尾窗。这里用 mmap 只在有界窗口内反向定位事件标记，
    并对单行长度另外限制，避免为找 Query 物化整个大文件。
    """

    event_markers = (
        b'"type":"user_message"',
        b'"type": "user_message"',
        b'"type":"task_started"',
        b'"type": "task_started"',
    )
    found: List[Dict[str, str]] = []
    try:
        with path.open("rb") as source:
            size = source.seek(0, os.SEEK_END)
            if size <= 0:
                return []
            scan_start = max(0, size - ROLLOUT_QUERY_SCAN_BYTES)
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                search_end = size
                while search_end > scan_start:
                    offset = max(
                        data.rfind(marker, scan_start, search_end)
                        for marker in event_markers
                    )
                    if offset < 0:
                        break
                    search_end = offset
                    line_floor = max(0, offset - ROLLOUT_QUERY_LINE_BYTES)
                    line_start = data.rfind(b"\n", line_floor, offset) + 1
                    line_end = data.find(
                        b"\n",
                        offset,
                        min(size, offset + ROLLOUT_QUERY_LINE_BYTES),
                    )
                    if line_end < 0:
                        line_end = size
                    if line_end - line_start > ROLLOUT_QUERY_LINE_BYTES:
                        continue
                    try:
                        record = json.loads(data[line_start:line_end])
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(record, Mapping) or record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, Mapping):
                        continue
                    event_type = payload.get("type")
                    if event_type == "task_started":
                        if found:
                            break
                        continue
                    if event_type != "user_message":
                        continue
                    text = payload.get("message")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    found.append({
                        "id": str(payload.get("id") or record.get("timestamp") or ""),
                        "kind": "initial",
                        "text": text.strip()[:4000],
                    })
    except (OSError, ValueError):
        logger.debug("无法反向读取 Desktop rollout Query", exc_info=True)
        return []

    found.reverse()
    for index, message in enumerate(found):
        message["kind"] = "initial" if index == 0 else "steering"
    return found


def _public_state_key(state: Optional[Mapping[str, Any]]) -> Any:
    if not isinstance(state, Mapping):
        return None
    return (
        state.get("title"),
        state.get("status"),
        state.get("active_turn_id"),
        state.get("turns"),
        state.get("messages"),
        state.get("pending"),
    )


__all__ = ["DesktopBridgeManager"]
