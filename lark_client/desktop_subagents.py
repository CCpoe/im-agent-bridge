"""有界读取已知子 Agent 的执行状态，不缓存或输出会话正文。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional, Tuple


MAX_CACHE_ENTRIES = 128
MAX_METADATA_BYTES = 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_LOOKUP_ENTRIES = 4096
MAX_LOOKUP_DIRECTORIES = 128
MAX_LOOKUP_DEPTH = 3
MISSING_PATH_RETRY_SECONDS = 2.0

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_IGNORED = object()
_StatKey = Tuple[int, int, int, int, int]


@dataclass
class _CacheEntry:
    path: Optional[Path]
    signature: Optional[_StatKey]
    status: Optional[str]
    checked_at: float


class SubagentStatusReader:
    """按明确的子线程 ID 读取最新生命周期，未知时返回 None。

    ``ordinal`` 是 JSON 记录的顶层序号，不是文件行号。只接受严格晚于
    ``subagent_history_start_ordinal`` 的事件，以排除 fork 复制的父历史。
    超长/不完整记录、窗口内没有可靠生命周期或身份不符均保守返回 None，
    不把旧的 running/completed 状态冒充最新状态。
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)
        self._cache: OrderedDict[Tuple[str, str], _CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

    def get_status(self, thread_id: str, root_thread_id: str) -> Optional[str]:
        if (
            not _valid_identifier(thread_id)
            or not _valid_identifier(root_thread_id)
            or thread_id == root_thread_id
        ):
            return None
        with self._lock:
            return self._get_status(thread_id, root_thread_id)

    def _get_status(self, thread_id: str, root_thread_id: str) -> Optional[str]:
        key = (thread_id, root_thread_id)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            if cached.path is None and now - cached.checked_at < MISSING_PATH_RETRY_SECONDS:
                return None
        try:
            root = self.sessions_dir.resolve(strict=True)
            if not root.is_dir():
                raise OSError("会话目录不可用")
            path = cached.path if cached is not None else None
            if path is None or not _safe_regular_path(path, root):
                path = self._find_path(root, thread_id)
            if path is None:
                self._remember(key, _CacheEntry(None, None, None, now))
                return None
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                before = _stat_key(os.fstat(source.fileno()))
                if cached is not None and cached.path == path and cached.signature == before:
                    return cached.status
                status = _read_status(source, thread_id, root_thread_id, before[2])
                if _stat_key(os.fstat(source.fileno())) != before:
                    status = None
            self._remember(key, _CacheEntry(path, before, status, now))
            return status
        except (OSError, ValueError, RuntimeError):
            self._remember(key, _CacheEntry(None, None, None, now))
            return None

    def _remember(self, key: Tuple[str, str], entry: _CacheEntry) -> None:
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    def _find_path(self, root: Path, thread_id: str) -> Optional[Path]:
        # 只遍历 sessions/YYYY/MM/DD，按日期从新到旧查找；条目、目录和深度
        # 都有硬上限。不能用无界递归 glob 扫描全部历史会话。
        pending = [(root, 0)]
        entry_count = 0
        directory_count = 0
        suffix = "-{}.jsonl".format(thread_id)
        while pending and directory_count < MAX_LOOKUP_DIRECTORIES:
            directory, depth = pending.pop()
            directory_count += 1
            entries = []
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        entry_count += 1
                        if entry_count > MAX_LOOKUP_ENTRIES:
                            return None
                        entries.append(entry)
            except OSError:
                continue
            directories = []
            for entry in sorted(entries, key=lambda item: item.name, reverse=True):
                try:
                    if (
                        entry.name.startswith("rollout-")
                        and entry.name.endswith(suffix)
                        and entry.is_file(follow_symlinks=False)
                    ):
                        candidate = Path(entry.path)
                        if _safe_regular_path(candidate, root):
                            return candidate
                    if (
                        depth < MAX_LOOKUP_DEPTH
                        and entry.name.isdigit()
                        and entry.is_dir(follow_symlinks=False)
                    ):
                        directories.append((Path(entry.path), depth + 1))
                except OSError:
                    continue
            pending.extend(reversed(directories))
        return None


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _safe_regular_path(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        return resolved == path and path.is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def _stat_key(stat: os.stat_result) -> _StatKey:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _has_subagent_marker(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("thread_source") == "subagent":
        return True
    source = metadata.get("source")
    if isinstance(source, str):
        if source.lower() in {"subagent", "subagentthreadspawn"}:
            return True
        try:
            source = json.loads(source)
        except (json.JSONDecodeError, TypeError):
            return False
    return isinstance(source, Mapping) and isinstance(source.get("subagent"), Mapping)


def _read_status(
    source: BinaryIO, thread_id: str, root_thread_id: str, file_size: int
) -> Optional[str]:
    raw_metadata = source.readline(MAX_METADATA_BYTES + 1)
    if len(raw_metadata) > MAX_METADATA_BYTES or not raw_metadata.endswith(b"\n"):
        return None
    try:
        record = json.loads(raw_metadata)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(record, Mapping) or record.get("type") != "session_meta":
        return None
    metadata = record.get("payload")
    if not isinstance(metadata, Mapping) or metadata.get("id") != thread_id:
        return None
    if not (
        metadata.get("parent_thread_id") == root_thread_id
        or (
            metadata.get("session_id") == root_thread_id
            and _has_subagent_marker(metadata)
        )
    ):
        return None
    history_boundary = metadata.get("subagent_history_start_ordinal")
    if not _nonnegative_integer(history_boundary):
        return None
    metadata_end = source.tell()
    start = max(metadata_end, file_size - MAX_SCAN_BYTES)
    source.seek(start)
    if start > metadata_end:
        # 尾窗可能从任意大记录中间开始；丢弃首段，不把文本中伪造的 JSON
        # 当作一条独立事件。窗口外的旧状态不用于推断当前状态。
        source.seek(start - 1)
        at_line_boundary = source.read(1) == b"\n"
        if not at_line_boundary and not _skip_line(source, file_size):
            return None
    status: Optional[str] = None
    while source.tell() < file_size:
        remaining = file_size - source.tell()
        raw = source.readline(min(MAX_LINE_BYTES + 1, remaining))
        if not raw:
            return None
        if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
            # 完成回复可以很长。这里选择有界降级 None，不能跳过后沿用
            # 较早的 task_started，把实际已完成的任务显示为运行中。
            status = None
            if not raw.endswith(b"\n") and not _skip_line(source, file_size):
                return None
            continue
        projected = _record_status(raw, history_boundary)
        if projected is not _IGNORED:
            status = projected
    return status


def _skip_line(source: BinaryIO, end: int) -> bool:
    while source.tell() < end:
        chunk = source.readline(min(MAX_LINE_BYTES, end - source.tell()))
        if chunk.endswith(b"\n"):
            return True
        if not chunk:
            break
    return False


def _record_status(raw: bytes, history_boundary: int) -> Any:
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(record, Mapping):
        return None
    if record.get("type") != "event_msg":
        return _IGNORED
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type not in {"task_started", "task_complete", "turn_aborted"}:
        return None if event_type.startswith("task_") else _IGNORED
    ordinal = record.get("ordinal")
    if not _nonnegative_integer(ordinal):
        return None
    if ordinal <= history_boundary:
        return _IGNORED
    if not _valid_identifier(payload.get("turn_id")):
        return None
    if event_type == "task_started":
        return "running"
    if event_type == "turn_aborted":
        return "interrupted"
    return "failed" if payload.get("error") is not None else "completed"


__all__ = ["SubagentStatusReader"]
