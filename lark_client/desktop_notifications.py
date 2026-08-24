"""Completion notifications for Codex Desktop turns.

The monitor tails only root Codex Desktop rollout files.  It stores byte
offsets and a small outbox so a daemon restart neither replays old completions
nor silently drops a completion that had not yet reached Lark.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .desktop_card import build_desktop_completion_card


logger = logging.getLogger("DesktopNotifications")

_STATE_VERSION = 1
_MAX_LINE_BYTES = 1024 * 1024
_MAX_DELIVERED = 4096


class DesktopCompletionMonitor:
    """Tail Desktop rollouts and notify registered Lark users on completion."""

    def __init__(
        self,
        card_service: Any,
        source_provider: Callable[[], Iterable[Mapping[str, Any]]],
        *,
        state_path: Optional[Path] = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.card_service = card_service
        self.source_provider = source_provider
        self.state_path = Path(
            state_path
            or Path.home() / ".remote-claude" / "desktop_notifications.json"
        )
        self.poll_interval = max(0.2, float(poll_interval))
        stored = self._load_state()
        self._targets: Dict[str, float] = stored["targets"]
        self._cursors: Dict[str, Dict[str, Any]] = stored["cursors"]
        self._pending: Dict[str, Dict[str, Any]] = stored["pending"]
        self._delivered: Dict[str, float] = stored["delivered"]
        self._registered_at = stored["registered_at"]
        self._task: Optional[asyncio.Task] = None
        self._lock: Optional[asyncio.Lock] = None
        self._closed = False

    @property
    def has_targets(self) -> bool:
        return bool(self._targets)

    async def register_target(self, user_id: str) -> bool:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            is_first_target = not self._targets
            if user_id not in self._targets:
                baseline: Dict[str, Dict[str, Any]] = {}
                registration_cutoff = time.time()
                if is_first_target and not self._cursors:
                    # Establish the byte boundary before the subscription becomes
                    # active.  The cutoff is captured first so a completion that
                    # lands while the snapshot is being built is still eligible.
                    baseline = await asyncio.to_thread(
                        self._snapshot_cursors, registration_cutoff
                    )
                self._targets[user_id] = registration_cutoff
                if not self._registered_at:
                    self._registered_at = registration_cutoff
                if baseline:
                    self._cursors.update(baseline)
                self._save_state()
        return True

    def start(self) -> None:
        self._closed = False
        if not self._targets:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="desktop-completion-monitor"
            )

    async def close(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def poll_once(self) -> int:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            changed = await asyncio.to_thread(self._scan_new_events)
            if changed:
                self._save_state()
            return await self._deliver_pending()

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("扫描 Desktop 完成事件失败")
            await asyncio.sleep(self.poll_interval)

    def _snapshot_cursors(self, cutoff: float) -> Dict[str, Dict[str, Any]]:
        cursors: Dict[str, Dict[str, Any]] = {}
        for thread in self._safe_sources():
            thread_id = _identifier(thread.get("thread_id"))
            path = _path(thread.get("_rollout_path"))
            if not thread_id or path is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            # If this file changed after registration began, scan it from the
            # beginning and let the per-target timestamp filter discard older
            # completions.  This closes the boundary where a completion lands
            # just before this file's stat() call.
            changed_after_cutoff = stat.st_mtime_ns >= int(cutoff * 1_000_000_000)
            cursors[thread_id] = {
                "path": str(path),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "offset": (
                    0
                    if changed_after_cutoff
                    else _complete_file_offset(path, stat.st_size)
                ),
            }
        return cursors

    def _scan_new_events(self) -> bool:
        changed = False
        now = time.time()
        for thread in self._safe_sources():
            thread_id = _identifier(thread.get("thread_id"))
            path = _path(thread.get("_rollout_path"))
            if not thread_id or path is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            cursor = self._cursors.get(thread_id)
            had_cursor = isinstance(cursor, Mapping)
            same_identity = bool(
                had_cursor
                and cursor.get("device") == stat.st_dev
                and cursor.get("inode") == stat.st_ino
            )
            same_file = bool(
                same_identity
                and isinstance(cursor.get("offset"), int)
                and 0 <= cursor["offset"] <= stat.st_size
            )
            if not same_file:
                created_at = getattr(stat, "st_birthtime", stat.st_ctime)
                offset = (
                    0
                    if had_cursor or (
                        self._registered_at and created_at >= self._registered_at
                    )
                    else _complete_file_offset(path, stat.st_size)
                )
                self._cursors[thread_id] = {
                    "path": str(path),
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "offset": offset,
                }
                cursor = self._cursors[thread_id]
                changed = True

            offset = int(cursor["offset"])
            if stat.st_size <= offset:
                continue
            events, new_offset = _read_completion_events(path, offset)
            if new_offset != offset:
                cursor["offset"] = new_offset
                changed = True
            for event in events:
                event_key = _event_key(thread_id, event)
                if event_key in self._delivered or event_key in self._pending:
                    continue
                completed_at = _timestamp_seconds(event.get("timestamp")) or now
                targets = [
                    target for target, registered_at in self._targets.items()
                    if completed_at >= registered_at
                ]
                if not targets:
                    continue
                self._pending[event_key] = {
                    "thread_id": thread_id,
                    "turn_id": _identifier(event.get("turn_id")) or "",
                    "outcome": event.get("outcome"),
                    "completed_at": event.get("timestamp") or "",
                    "title": _text(thread.get("title"), 200) or "未命名任务",
                    "project_name": _text(thread.get("project_name"), 120) or "未知项目",
                    "targets": targets,
                    "attempts": 0,
                    "next_attempt_at": 0.0,
                }
                changed = True
        return changed

    async def _deliver_pending(self) -> int:
        delivered_count = 0
        now = time.time()
        for event_key, event in list(self._pending.items()):
            if float(event.get("next_attempt_at") or 0) > now:
                continue
            targets = [target for target in event.get("targets") or [] if target in self._targets]
            remaining: List[str] = []
            card = build_desktop_completion_card(event)
            for user_id in targets:
                message_uuid = str(uuid.uuid5(
                    uuid.NAMESPACE_URL, f"im-agent-bridge:{event_key}:{user_id}"
                ))
                try:
                    sent = await self.card_service.create_and_send_card_to_user(
                        user_id, card, message_uuid=message_uuid
                    )
                except Exception:
                    logger.exception("发送 Desktop 完成通知失败: thread=%s", event.get("thread_id"))
                    sent = None
                if sent:
                    delivered_count += 1
                else:
                    remaining.append(user_id)
            if remaining:
                attempts = int(event.get("attempts") or 0) + 1
                event["attempts"] = attempts
                event["targets"] = remaining
                event["next_attempt_at"] = now + min(300.0, 2.0 ** min(attempts, 8))
            else:
                self._pending.pop(event_key, None)
                self._delivered[event_key] = now
                if len(self._delivered) > _MAX_DELIVERED:
                    oldest = sorted(self._delivered, key=self._delivered.get)[:1024]
                    for old_key in oldest:
                        self._delivered.pop(old_key, None)
            self._save_state()
        return delivered_count

    def _safe_sources(self) -> List[Mapping[str, Any]]:
        try:
            return [item for item in self.source_provider() if isinstance(item, Mapping)]
        except Exception:
            logger.exception("读取 Desktop rollout 列表失败")
            return []

    def _load_state(self) -> Dict[str, Any]:
        empty = {
            "targets": {},
            "cursors": {},
            "pending": {},
            "delivered": {},
            "registered_at": 0.0,
        }
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return empty
        if not isinstance(data, Mapping) or data.get("version") != _STATE_VERSION:
            return empty
        targets = data.get("targets") if isinstance(data.get("targets"), Mapping) else {}
        cursors = data.get("cursors") if isinstance(data.get("cursors"), Mapping) else {}
        pending = data.get("pending") if isinstance(data.get("pending"), Mapping) else {}
        delivered = data.get("delivered") if isinstance(data.get("delivered"), Mapping) else {}
        return {
            "targets": {
                str(key): float(value) for key, value in targets.items()
                if isinstance(key, str) and isinstance(value, (int, float))
            },
            "cursors": {
                str(key): dict(value) for key, value in cursors.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            },
            "pending": {
                str(key): dict(value) for key, value in pending.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            },
            "delivered": {
                str(key): float(value) for key, value in delivered.items()
                if isinstance(key, str) and isinstance(value, (int, float))
            },
            "registered_at": float(data.get("registered_at") or 0),
        }

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _STATE_VERSION,
                "registered_at": self._registered_at,
                "targets": self._targets,
                "cursors": self._cursors,
                "pending": self._pending,
                "delivered": self._delivered,
            }
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(payload, target, ensure_ascii=False, sort_keys=True)
            os.replace(temporary, self.state_path)
        except OSError:
            logger.exception("无法持久化 Desktop 完成通知状态")


def _read_completion_events(path: Path, offset: int) -> tuple[List[Dict[str, Any]], int]:
    events: List[Dict[str, Any]] = []
    committed_offset = offset
    try:
        with path.open("rb") as source:
            source.seek(offset)
            while True:
                line_start = source.tell()
                raw_line = source.readline(_MAX_LINE_BYTES + 1)
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    if len(raw_line) <= _MAX_LINE_BYTES:
                        break
                    scanner = _CompletionMetadataScanner()
                    scanner.feed(raw_line)
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = source.readline(_MAX_LINE_BYTES + 1)
                        scanner.feed(raw_line)
                    if not raw_line:
                        break
                    committed_offset = source.tell()
                    event = scanner.completion_event()
                    if event is not None:
                        events.append(event)
                    continue
                committed_offset = source.tell()
                event = _completion_event(raw_line)
                if event is not None:
                    events.append(event)
                if committed_offset <= line_start:
                    break
    except (OSError, ValueError):
        return [], offset
    return events, committed_offset


class _CompletionMetadataScanner:
    """Extract bounded task metadata from one arbitrarily large JSON record.

    Rollout records can contain a very large ``last_agent_message``.  This
    scanner tracks JSON strings and object depth without buffering unrelated
    values, so text inside that message cannot be mistaken for structural
    ``type`` or ``error`` fields.
    """

    _TEXT_LIMIT = 512

    def __init__(self) -> None:
        self._depth = 0
        self._payload_depth: Optional[int] = None
        self._pending_keys: Dict[int, str] = {}
        self._value_keys: Dict[int, str] = {}
        self._in_string = False
        self._escaped = False
        self._string_role = ""
        self._string_key = ""
        self._string_bytes = bytearray()
        self._capture_string = False
        self._root_type: Optional[str] = None
        self._payload_type: Optional[str] = None
        self._turn_id: Optional[str] = None
        self._timestamp: Optional[str] = None
        self._error_seen = False
        self._error_non_null = False

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._in_string:
                if self._capture_string and len(self._string_bytes) < self._TEXT_LIMIT:
                    self._string_bytes.append(byte)
                if self._escaped:
                    self._escaped = False
                elif byte == 0x5C:  # backslash
                    self._escaped = True
                elif byte == 0x22:  # quote
                    if self._capture_string:
                        # Drop the closing quote retained above.
                        del self._string_bytes[-1:]
                    self._finish_string()
                continue

            if byte in b" \t\r\n":
                continue
            if byte == 0x22:  # quote
                value_key = self._value_keys.get(self._depth)
                self._string_role = "value" if value_key is not None else "key"
                self._string_key = value_key or ""
                self._capture_string = self._string_role == "key" or self._wanted_value(
                    self._depth, self._string_key
                )
                self._string_bytes.clear()
                self._in_string = True
                self._escaped = False
                continue
            if byte == 0x3A:  # colon
                key = self._pending_keys.pop(self._depth, None)
                if key is not None:
                    self._value_keys[self._depth] = key
                continue
            if byte == 0x7B:  # opening brace
                key = self._value_keys.pop(self._depth, None)
                if self._depth == 1 and key == "payload":
                    self._payload_depth = 2
                if self._depth == self._payload_depth and key == "error":
                    self._error_seen = True
                    self._error_non_null = True
                self._depth += 1
                continue
            if byte == 0x7D:  # closing brace
                self._pending_keys.pop(self._depth, None)
                self._value_keys.pop(self._depth, None)
                if self._depth == self._payload_depth:
                    self._payload_depth = None
                self._depth = max(0, self._depth - 1)
                continue
            if byte == 0x2C:  # comma
                self._pending_keys.pop(self._depth, None)
                self._value_keys.pop(self._depth, None)
                continue

            key = self._value_keys.get(self._depth)
            if self._depth == self._payload_depth and key == "error":
                self._error_seen = True
                self._error_non_null = byte != 0x6E  # ``n`` begins JSON null
                self._value_keys.pop(self._depth, None)

    def completion_event(self) -> Optional[Dict[str, Any]]:
        if self._root_type != "event_msg" or self._payload_type != "task_complete":
            return None
        return {
            "turn_id": self._turn_id or "",
            "outcome": (
                "failed" if self._error_seen and self._error_non_null else "completed"
            ),
            "timestamp": self._timestamp or "",
        }

    def _wanted_value(self, depth: int, key: str) -> bool:
        return (
            (depth == 1 and key in {"timestamp", "type"})
            or (
                depth == self._payload_depth
                and key in {"type", "turn_id", "error"}
            )
        )

    def _finish_string(self) -> None:
        raw = bytes(self._string_bytes)
        self._in_string = False
        value = _decode_json_string(raw) if self._capture_string else None
        if self._string_role == "key":
            if value is not None:
                self._pending_keys[self._depth] = value
            return

        key = self._string_key
        self._value_keys.pop(self._depth, None)
        if self._depth == 1:
            if key == "timestamp":
                self._timestamp = value
            elif key == "type":
                self._root_type = value
        elif self._depth == self._payload_depth:
            if key == "type":
                self._payload_type = value
            elif key == "turn_id":
                self._turn_id = value
            elif key == "error":
                self._error_seen = True
                self._error_non_null = True


def _decode_json_string(raw: bytes) -> Optional[str]:
    try:
        value = json.loads(b'"' + raw + b'"')
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, str) else None


def _complete_file_offset(path: Path, size: int) -> int:
    if size <= 0:
        return 0
    try:
        with path.open("rb") as source:
            # Inspect the exact stat snapshot boundary.  The file may grow
            # between stat() and open(); checking the new EOF could otherwise
            # return an offset in the middle of a just-completed JSON record.
            source.seek(size - 1, os.SEEK_SET)
            return size if source.read(1) == b"\n" else _last_newline_offset(source, size)
    except (OSError, ValueError):
        return 0


def _last_newline_offset(source: Any, size: int) -> int:
    position = size
    while position > 0:
        read_size = min(64 * 1024, position)
        position -= read_size
        source.seek(position)
        block = source.read(read_size)
        newline = block.rfind(b"\n")
        if newline >= 0:
            return position + newline + 1
    return 0


def _completion_event(raw_line: bytes) -> Optional[Dict[str, Any]]:
    if b"task_complete" not in raw_line:
        return None
    try:
        record = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(record, Mapping) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "task_complete":
        return None
    return {
        "turn_id": _identifier(payload.get("turn_id")) or "",
        "outcome": "failed" if payload.get("error") is not None else "completed",
        "timestamp": _text(record.get("timestamp"), 80) or "",
    }


def _event_key(thread_id: str, event: Mapping[str, Any]) -> str:
    raw = "\0".join((
        thread_id,
        _identifier(event.get("turn_id")) or _text(event.get("timestamp"), 80) or "unknown",
        str(event.get("outcome") or "unknown"),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _timestamp_seconds(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def _identifier(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ord(ch) >= 32).strip()
    if not value:
        return None
    return value[:limit]


def _path(value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    return Path(value)


__all__ = ["DesktopCompletionMonitor"]
