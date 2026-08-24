"""Pure helpers for projecting Codex Desktop state into a safe Lark card.

The Desktop IPC protocol sends a full ``conversationState`` followed by Immer
patches.  That object is a private, versioned implementation detail, so this
module deliberately treats it as opaque storage and only projects a very small
allowlist of fields.  In particular, reasoning and tool items are never read by
the public-event extractor or the card builder.

``_conversation_state`` in a normalized result is intended for in-memory patch
application only.  Callers must not log or persist it.  ``build_desktop_card``
never serializes that field.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


JSON = Union[None, bool, int, float, str, List["JSON"], Dict[str, "JSON"]]

NORMALIZED_VERSION = 1
MAX_PUBLIC_MESSAGES = 20
MAX_PUBLIC_MESSAGE_CHARS = 4_000
MAX_CARD_MESSAGES = 6
MAX_CARD_MESSAGE_CHARS = 1_500
DESKTOP_LIST_PAGE_SIZE = 5

_PUBLIC_AGENT_PHASES = {None, "commentary", "final_answer"}

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": ("command_execution", "命令执行审批"),
    "item/fileChange/requestApproval": ("file_change", "文件修改审批"),
    "item/permissions/requestApproval": ("permissions", "权限审批"),
}

_INPUT_METHODS = {
    "item/tool/requestUserInput": ("user_input", "等待输入"),
}

_STATUS_LABELS = {
    "idle": "空闲",
    "running": "运行中",
    "waiting_approval": "等待审批",
    "waiting_input": "等待输入",
    "completed": "已完成",
    "failed": "异常",
    "interrupted": "已停止",
    "unknown": "状态未知",
}

_STATUS_TEMPLATES = {
    "idle": "grey",
    "running": "blue",
    "waiting_approval": "orange",
    "waiting_input": "orange",
    "completed": "green",
    "failed": "red",
    "interrupted": "grey",
    "unknown": "grey",
}

_LIST_STATUS_ICONS = {
    "running": "🟢",
    "waiting_approval": "🟢",
    "waiting_input": "🟢",
    "failed": "🔴",
}


class PatchApplyError(ValueError):
    """Raised when an Immer patch cannot be applied safely."""


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    # Preserve newlines, but remove control characters that are unsafe/useless
    # in a card.  Do not stringify arbitrary objects: they can contain private
    # reasoning or tool payloads.
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32).strip()
    if not value:
        return None
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def _identifier(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _wire_identifier(value: Any) -> Optional[Union[str, int]]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _approval_details(request_kind: str, params: Mapping[str, Any]) -> str:
    if request_kind == "command_execution":
        command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        text = _clean_text(command, 900) if isinstance(command, str) else None
        if text:
            text = re.sub(
                r"(?i)(authorization|password|secret|token|api[_-]?key)(\s*[:=]\s*|[_-])\S+",
                r"\1\2[REDACTED]",
                text,
            )
            return "命令：`{}`".format(text.replace("`", "\\`"))
    if request_kind == "permissions" and isinstance(params.get("permissions"), Mapping):
        text = _clean_text(
            json.dumps(params["permissions"], ensure_ascii=False, separators=(",", ":")),
            900,
        )
        return "请求权限：`{}`".format((text or "").replace("`", "\\`"))
    if request_kind == "file_change":
        grant_root = _clean_text(params.get("grantRoot"), 600)
        if grant_root:
            return "写入范围：`{}`".format(grant_root.replace("`", "\\`"))
    return ""


def _empty_state(
    *,
    thread_id: str = "",
    host_id: str = "",
    revision: Optional[int] = None,
    raw_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "schema_version": NORMALIZED_VERSION,
        "schema_known": False,
        "thread_id": thread_id,
        "host_id": host_id,
        "revision": revision,
        "title": "Codex Desktop",
        "status": "unknown",
        "active_turn_id": None,
        "messages": [],
        "pending": None,
    }
    if raw_state is not None:
        result["_conversation_state"] = copy.deepcopy(raw_state)
    return result


def _unwrap_state(payload: Any) -> Any:
    """Accept a raw state as well as the common snapshot/envelope wrappers."""
    if not isinstance(payload, Mapping):
        return payload
    if "conversationState" in payload:
        return payload.get("conversationState")
    change = payload.get("change")
    if isinstance(change, Mapping) and change.get("type") == "snapshot":
        return change.get("conversationState")
    params = payload.get("params")
    if isinstance(params, Mapping):
        change = params.get("change")
        if isinstance(change, Mapping) and change.get("type") == "snapshot":
            return change.get("conversationState")
    return payload


def _snapshot_metadata(payload: Any) -> Tuple[str, str, Optional[int]]:
    if not isinstance(payload, Mapping):
        return "", "", None
    params: Mapping[str, Any] = payload
    nested_params = payload.get("params")
    if isinstance(nested_params, Mapping):
        params = nested_params
    change = params.get("change")
    revision = change.get("revision") if isinstance(change, Mapping) else payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = None
    return (
        _identifier(params.get("conversationId")) or "",
        _identifier(params.get("hostId")) or "",
        revision,
    )


def _ordered_turns(state: Mapping[str, Any]) -> Optional[List[Mapping[str, Any]]]:
    """Return known conversation turns without guessing unknown schemas."""
    turns = state.get("turns")
    if not isinstance(turns, list):
        return None

    ordered: List[Mapping[str, Any]] = []
    seen: set = set()

    turn_history = state.get("turnHistory")
    if turn_history is not None:
        if not isinstance(turn_history, Mapping) or turn_history.get("kind") != "canonical":
            return None
        history = turn_history.get("history")
        if not isinstance(history, Mapping):
            return None
        islands = history.get("islands")
        entities = history.get("entitiesByKey")
        if not isinstance(islands, list) or not isinstance(entities, Mapping):
            return None
        for island in islands:
            if not isinstance(island, Mapping) or not isinstance(island.get("entries"), list):
                return None
            for entry in island["entries"]:
                if not isinstance(entry, Mapping):
                    return None
                key = entry.get("value")
                turn = entities.get(key) if isinstance(key, str) else None
                if not isinstance(turn, Mapping):
                    return None
                marker = _identifier(turn.get("turnId")) or _identifier(turn.get("id")) or str(key)
                if marker not in seen:
                    ordered.append(turn)
                    seen.add(marker)

    for turn in turns:
        if not isinstance(turn, Mapping):
            return None
        marker = _identifier(turn.get("turnId")) or _identifier(turn.get("id"))
        if marker is None or marker not in seen:
            ordered.append(turn)
            if marker is not None:
                seen.add(marker)
    return ordered


def _safe_patch_items_from_state(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}

    def add_items(prefix: List[Any], items: Any) -> None:
        if not isinstance(items, list):
            return
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
                continue
            key = json.dumps(prefix + ["items", index], ensure_ascii=False, separators=(",", ":"))
            result[key] = {
                "id": item.get("id"),
                "type": item.get("type"),
                "phase": item.get("phase"),
                "text": _clean_text(item.get("text"), MAX_PUBLIC_MESSAGE_CHARS),
            }
            while len(result) > 32:
                result.pop(next(iter(result)))

    turn_history = state.get("turnHistory")
    if isinstance(turn_history, Mapping):
        history = turn_history.get("history")
        entities = history.get("entitiesByKey") if isinstance(history, Mapping) else None
        if isinstance(entities, Mapping):
            for key, turn in entities.items():
                if isinstance(key, str) and isinstance(turn, Mapping):
                    add_items(["turnHistory", "history", "entitiesByKey", key], turn.get("items"))

    # current turns last, so the bounded index always retains streaming item paths.
    turns = state.get("turns")
    if isinstance(turns, list):
        for index, turn in enumerate(turns):
            if isinstance(turn, Mapping):
                add_items(["turns", index], turn.get("items"))
    return result


def extract_public_events(state: Any) -> List[Dict[str, Any]]:
    """Extract only user-visible assistant messages from a conversation state.

    Accepted items are exactly ``agentMessage`` entries whose phase is
    ``commentary`` or ``final_answer`` (or absent for older Desktop versions).
    No recursive fallback is used; this is intentional so an unknown schema
    cannot accidentally surface reasoning or tool output.
    """
    if isinstance(state, Mapping) and state.get("schema_version") == NORMALIZED_VERSION:
        messages = state.get("messages")
        if not isinstance(messages, list):
            return []
        result: List[Dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            text = _clean_text(message.get("text"), MAX_PUBLIC_MESSAGE_CHARS)
            phase = message.get("phase")
            if text is None or phase not in _PUBLIC_AGENT_PHASES:
                continue
            result.append({
                "id": _identifier(message.get("id")) or "",
                "turn_id": _identifier(message.get("turn_id")) or "",
                "phase": phase or "final_answer",
                "text": text,
            })
        return result[-MAX_PUBLIC_MESSAGES:]

    raw = _unwrap_state(state)
    if not isinstance(raw, Mapping):
        return []
    turns = _ordered_turns(raw)
    if turns is None:
        return []

    events: List[Dict[str, Any]] = []
    seen: set = set()
    for turn in turns:
        items = turn.get("items")
        if not isinstance(items, list):
            return []
        turn_id = _identifier(turn.get("turnId")) or _identifier(turn.get("id")) or ""
        for item in items:
            if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
                continue
            phase = item.get("phase")
            if phase not in _PUBLIC_AGENT_PHASES:
                continue
            text = _clean_text(item.get("text"), MAX_PUBLIC_MESSAGE_CHARS)
            if text is None:
                continue
            item_id = _identifier(item.get("id")) or ""
            marker = (turn_id, item_id, phase, text)
            if marker in seen:
                continue
            seen.add(marker)
            events.append({
                "id": item_id,
                "turn_id": turn_id,
                "phase": phase or "final_answer",
                "text": text,
            })
    return events[-MAX_PUBLIC_MESSAGES:]


def _safe_options(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, str]] = []
    for option in value[:8]:
        if not isinstance(option, Mapping):
            continue
        label = _clean_text(option.get("label"), 120)
        if label is None:
            continue
        description = _clean_text(option.get("description"), 240) or ""
        result.append({"label": label, "description": description})
    return result


def _pending_request(requests: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(requests, list):
        return None
    for request in reversed(requests):
        if not isinstance(request, Mapping):
            continue
        request_id = _wire_identifier(request.get("id"))
        method = request.get("method")
        params = request.get("params")
        if request_id is None or not isinstance(method, str) or not isinstance(params, Mapping):
            continue

        approval = _APPROVAL_METHODS.get(method)
        if approval is not None:
            request_kind, title = approval
            pending = {
                "kind": "approval",
                "request_kind": request_kind,
                "request_id": request_id,
                "method": method,
                "title": title,
                # The reason is user-facing metadata.  Commands, diffs,
                # arguments and execution output are deliberately ignored.
                "prompt": _clean_text(params.get("reason"), 600) or "需要在 Codex Desktop 中确认",
                "options": [],
            }
            if request_kind == "permissions" and isinstance(params.get("permissions"), Mapping):
                # 仅保存在进程内用于原样授予请求的子集；卡片构建器不会渲染该字段。
                pending["permissions_response"] = {
                    "permissions": copy.deepcopy(params["permissions"]),
                    "scope": "turn",
                }
            if request_kind == "file_change":
                pending["allow_remote"] = False
            details = _approval_details(request_kind, params)
            if details:
                pending["details"] = details
            return pending

        input_kind = _INPUT_METHODS.get(method)
        if input_kind is not None:
            request_kind, title = input_kind
            questions = params.get("questions")
            if isinstance(questions, list) and len(questions) != 1:
                return {
                    "kind": "input",
                    "request_kind": request_kind,
                    "request_id": request_id,
                    "method": method,
                    "title": title,
                    "prompt": "该请求包含多个问题，请在 Codex Desktop 中处理",
                    "question_id": None,
                    "options": [],
                    "unsupported": True,
                }
            question: Optional[Mapping[str, Any]] = None
            if isinstance(questions, list) and questions and isinstance(questions[0], Mapping):
                question = questions[0]
            prompt = _clean_text(question.get("question"), 600) if question is not None else None
            if prompt is None:
                prompt = _clean_text(params.get("question"), 600) or "请提供输入"
            options = _safe_options(question.get("options")) if question is not None else []
            if not options:
                options = _safe_options(params.get("options"))
            return {
                "kind": "input",
                "request_kind": request_kind,
                "request_id": request_id,
                "method": method,
                "title": title,
                "prompt": prompt,
                "question_id": _identifier(question.get("id")) if question is not None else None,
                "options": options,
            }
    return None


def _status_from_state(state: Mapping[str, Any], turns: Sequence[Mapping[str, Any]], pending: Any) -> str:
    if isinstance(pending, Mapping):
        return "waiting_input" if pending.get("kind") == "input" else "waiting_approval"

    runtime = state.get("threadRuntimeStatus")
    if isinstance(runtime, Mapping):
        runtime_type = runtime.get("type")
        flags = runtime.get("activeFlags")
        if runtime_type == "active":
            if isinstance(flags, list) and "waitingOnApproval" in flags:
                return "waiting_approval"
            return "running"
        if runtime_type == "idle":
            return "idle"
        if runtime_type == "systemError":
            return "failed"
        if runtime_type == "notLoaded":
            return "unknown"

    direct = state.get("status")
    if isinstance(direct, Mapping):
        direct = direct.get("type")
    direct_map = {
        "active": "running",
        "inProgress": "running",
        "running": "running",
        "idle": "idle",
        "completed": "completed",
        "failed": "failed",
        "errored": "failed",
        "systemError": "failed",
        "interrupted": "interrupted",
    }
    if isinstance(direct, str) and direct in direct_map:
        return direct_map[direct]

    if turns:
        turn_status = turns[-1].get("status")
        if isinstance(turn_status, str) and turn_status in direct_map:
            return direct_map[turn_status]
    return "unknown"


def normalize_conversation_state(
    payload: Any,
    previous: Optional[Mapping[str, Any]] = None,
    *,
    retain_raw: bool = True,
) -> Dict[str, Any]:
    """Project a Desktop conversation snapshot into the stable safe model."""
    raw = _unwrap_state(payload)
    payload_thread_id, payload_host_id, payload_revision = _snapshot_metadata(payload)
    previous = previous if isinstance(previous, Mapping) else {}
    previous_thread_id = payload_thread_id or _identifier(previous.get("thread_id")) or ""
    previous_host_id = payload_host_id or _identifier(previous.get("host_id")) or ""
    previous_revision = payload_revision if payload_revision is not None else previous.get("revision")

    if not isinstance(raw, dict):
        return _empty_state(
            thread_id=previous_thread_id,
            host_id=previous_host_id,
            revision=previous_revision if isinstance(previous_revision, int) else None,
        )

    thread_id = _identifier(raw.get("id")) or _identifier(raw.get("conversationId")) or previous_thread_id
    turns = _ordered_turns(raw)
    requests = raw.get("requests")
    # Both are stable fields in the observed v11 state.  If they disappear or
    # change type, do not guess by recursively scraping the payload.
    if turns is None or not isinstance(requests, list):
        return _empty_state(
            thread_id=thread_id,
            host_id=previous_host_id,
            revision=previous_revision if isinstance(previous_revision, int) else None,
            raw_state=raw if retain_raw else None,
        )

    messages = extract_public_events(raw)
    pending = _pending_request(requests)
    title = _clean_text(raw.get("title"), 200) or _clean_text(raw.get("name"), 200)
    if title is None:
        title = "未命名任务"
    result: Dict[str, Any] = {
        "schema_version": NORMALIZED_VERSION,
        "schema_known": True,
        "thread_id": thread_id,
        "host_id": previous_host_id,
        "revision": previous_revision if isinstance(previous_revision, int) else None,
        "title": title,
        "status": _status_from_state(raw, turns, pending),
        "active_turn_id": None,
        "messages": messages,
        "pending": pending,
        "_patch_items": _safe_patch_items_from_state(raw),
    }
    if retain_raw:
        result["_conversation_state"] = copy.deepcopy(raw)
    if turns:
        last_turn = turns[-1]
        if last_turn.get("status") in {"inProgress", "running"}:
            result["active_turn_id"] = (
                _wire_identifier(last_turn.get("turnId"))
                or _wire_identifier(last_turn.get("id"))
            )
    return result


def _path_index(segment: Any, length: int, *, allow_end: bool = False) -> int:
    if isinstance(segment, bool):
        raise PatchApplyError("boolean is not a valid list index")
    if isinstance(segment, int):
        index = segment
    elif isinstance(segment, str) and segment.isdigit():
        index = int(segment)
    elif segment == "-" and allow_end:
        return length
    else:
        raise PatchApplyError("invalid list index")
    upper = length if allow_end else length - 1
    if index < 0 or index > upper:
        raise PatchApplyError("list index out of range")
    return index


def _patch_parent(document: Any, path: Sequence[Any]) -> Tuple[Any, Any]:
    if not path:
        raise PatchApplyError("root patch has no parent")
    parent = document
    for segment in path[:-1]:
        if isinstance(parent, dict):
            if not isinstance(segment, str) or segment not in parent:
                raise PatchApplyError("patch path does not exist")
            parent = parent[segment]
        elif isinstance(parent, list):
            parent = parent[_path_index(segment, len(parent))]
        else:
            raise PatchApplyError("patch path traverses a scalar")
    return parent, path[-1]


def apply_immer_patches(state: Any, patches: Any) -> Any:
    """Apply Immer ``add``/``replace``/``remove`` patches without mutation.

    The patch batch is transactional: malformed input raises
    :class:`PatchApplyError` and the caller's state remains untouched.
    """
    if not isinstance(patches, list):
        raise PatchApplyError("patches must be a list")
    document = copy.deepcopy(state)
    for patch in patches:
        if not isinstance(patch, Mapping):
            raise PatchApplyError("patch must be an object")
        operation = patch.get("op")
        path = patch.get("path")
        if operation not in {"add", "replace", "remove"} or not isinstance(path, list):
            raise PatchApplyError("unsupported or malformed patch")
        if any(not isinstance(part, (str, int)) or isinstance(part, bool) for part in path):
            raise PatchApplyError("patch path contains an invalid segment")

        if not path:
            if operation == "remove":
                document = None
            elif "value" not in patch:
                raise PatchApplyError("patch value is required")
            else:
                document = copy.deepcopy(patch["value"])
            continue

        parent, key = _patch_parent(document, path)
        if isinstance(parent, dict):
            if not isinstance(key, str):
                raise PatchApplyError("object key must be a string")
            if operation == "remove":
                if key not in parent:
                    raise PatchApplyError("remove target does not exist")
                del parent[key]
            else:
                if "value" not in patch:
                    raise PatchApplyError("patch value is required")
                if operation == "replace" and key not in parent:
                    raise PatchApplyError("replace target does not exist")
                parent[key] = copy.deepcopy(patch["value"])
        elif isinstance(parent, list):
            if operation == "add":
                if "value" not in patch:
                    raise PatchApplyError("patch value is required")
                index = _path_index(key, len(parent), allow_end=True)
                parent.insert(index, copy.deepcopy(patch["value"]))
            else:
                index = _path_index(key, len(parent))
                if operation == "remove":
                    del parent[index]
                else:
                    if "value" not in patch:
                        raise PatchApplyError("patch value is required")
                    parent[index] = copy.deepcopy(patch["value"])
        else:
            raise PatchApplyError("patch target parent is a scalar")
    return document


def _event_params(event: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(event, Mapping):
        return None
    event_name = event.get("type") or event.get("method")
    if event_name is not None and event_name != "thread-stream-state-changed":
        return None
    params = event.get("params")
    if isinstance(params, Mapping):
        return params
    # Useful for a caller that has already unwrapped the notification params.
    return event if isinstance(event.get("change"), Mapping) else None


def normalize_desktop_update(
    current: Optional[Mapping[str, Any]],
    event: Any,
) -> Dict[str, Any]:
    """Consume one ``thread-stream-state-changed`` v11 snapshot or patch."""
    params = _event_params(event)
    current = current if isinstance(current, Mapping) else {}
    thread_id = _identifier(params.get("conversationId")) if params is not None else None
    host_id = _identifier(params.get("hostId")) if params is not None else None
    thread_id = thread_id or _identifier(current.get("thread_id")) or ""
    host_id = host_id or _identifier(current.get("host_id")) or ""
    if params is None or not isinstance(params.get("change"), Mapping):
        result = _empty_state(thread_id=thread_id, host_id=host_id)
        result["needs_snapshot"] = True
        return result

    change = params["change"]
    change_type = change.get("type")
    revision = change.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        result = _empty_state(thread_id=thread_id, host_id=host_id)
        result["needs_snapshot"] = True
        return result

    if change_type == "snapshot":
        raw = change.get("conversationState")
    elif change_type == "patches":
        base_revision = change.get("baseRevision")
        current_revision = current.get("revision")
        raw_current = current.get("_conversation_state")
        if (
            not isinstance(base_revision, int)
            or isinstance(base_revision, bool)
            or current_revision != base_revision
            or not isinstance(raw_current, dict)
        ):
            result = _empty_state(thread_id=thread_id, host_id=host_id, revision=revision)
            result["needs_snapshot"] = True
            return result
        try:
            raw = apply_immer_patches(raw_current, change.get("patches"))
        except PatchApplyError:
            result = _empty_state(thread_id=thread_id, host_id=host_id, revision=revision)
            result["needs_snapshot"] = True
            return result
    else:
        result = _empty_state(thread_id=thread_id, host_id=host_id, revision=revision)
        result["needs_snapshot"] = True
        return result

    if not isinstance(raw, dict):
        result = _empty_state(thread_id=thread_id, host_id=host_id, revision=revision)
        result["needs_snapshot"] = True
        return result
    result = normalize_conversation_state(raw, current)
    result["thread_id"] = thread_id or result["thread_id"]
    result["host_id"] = host_id
    result["revision"] = revision
    result["needs_snapshot"] = not result["schema_known"]
    return result


def normalize_patch_only_update(
    current: Optional[Mapping[str, Any]],
    event: Any,
) -> Dict[str, Any]:
    """Safely project useful v11 patches when an oversized snapshot is unavailable.

    Long Desktop conversations can exceed the IPC frame limit.  This fallback
    never recursively scrapes values: it only accepts whole ``agentMessage``
    items, known status leaves, and request objects under the top-level
    ``requests`` collection.
    """
    params = _event_params(event)
    current = current if isinstance(current, Mapping) else {}
    result: Dict[str, Any] = {
        "schema_version": NORMALIZED_VERSION,
        "schema_known": bool(current.get("schema_known")),
        "thread_id": (
            _identifier(params.get("conversationId")) if params is not None else None
        ) or _identifier(current.get("thread_id")) or "",
        "host_id": (
            _identifier(params.get("hostId")) if params is not None else None
        ) or _identifier(current.get("host_id")) or "",
        "revision": current.get("revision"),
        "title": _clean_text(current.get("title"), 200) or "Codex Desktop",
        "status": current.get("status") if current.get("status") in _STATUS_LABELS else "unknown",
        "active_turn_id": current.get("active_turn_id"),
        "messages": list(current.get("messages") or [])[-MAX_PUBLIC_MESSAGES:],
        "pending": current.get("pending") if isinstance(current.get("pending"), Mapping) else None,
        "needs_snapshot": True,
        "patch_only": True,
        "_patch_items": copy.deepcopy(current.get("_patch_items") or {}),
    }
    if params is None or not isinstance(params.get("change"), Mapping):
        return result
    change = params["change"]
    revision = change.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        result["revision"] = revision
    patches = change.get("patches")
    if not isinstance(patches, list):
        return result

    messages = list(result["messages"])
    patch_items = result["_patch_items"]
    for patch in patches:
        if not isinstance(patch, Mapping):
            continue
        path = patch.get("path")
        op = patch.get("op")
        value = patch.get("value")
        if not isinstance(path, list) or op not in {"add", "replace", "remove"}:
            continue

        item_key: Optional[str] = None
        item_offset: Optional[int] = None
        if "items" in path:
            item_index = path.index("items")
            if len(path) > item_index + 1:
                item_offset = item_index + 2
                item_key = json.dumps(path[:item_offset], ensure_ascii=False, separators=(",", ":"))

        if item_key is not None:
            old_partial = patch_items.get(item_key)
            old_message_id = (
                _identifier(old_partial.get("id")) or item_key
                if isinstance(old_partial, Mapping)
                else None
            )
            if op == "remove" and len(path) == item_offset:
                patch_items.pop(item_key, None)
            elif op in {"add", "replace"} and len(path) == item_offset and isinstance(value, Mapping):
                if value.get("type") == "agentMessage":
                    patch_items[item_key] = {
                        key: value.get(key)
                        for key in ("id", "type", "phase", "text")
                    }
                else:
                    patch_items.pop(item_key, None)
            elif item_key in patch_items and len(path) == item_offset + 1:
                field = path[-1]
                if field in {"id", "type", "phase", "text"}:
                    if op == "remove":
                        patch_items[item_key].pop(field, None)
                    else:
                        patch_items[item_key][field] = value

            if item_key not in patch_items or patch_items[item_key].get("type") != "agentMessage":
                if old_message_id is not None:
                    messages = [m for m in messages if m.get("id") != old_message_id]

            partial = patch_items.get(item_key)
            if isinstance(partial, Mapping) and partial.get("type") == "agentMessage":
                phase = partial.get("phase")
                text = _clean_text(partial.get("text"), MAX_PUBLIC_MESSAGE_CHARS)
                if phase in _PUBLIC_AGENT_PHASES and text:
                    message_id = _identifier(partial.get("id")) or item_key
                    message = {
                        "id": message_id,
                        "turn_id": "",
                        "phase": phase or "final_answer",
                        "text": text,
                    }
                    messages = [m for m in messages if m.get("id") != message_id]
                    messages.append(message)

        if op in {"add", "replace"} and isinstance(value, Mapping):
            if path and path[0] == "requests":
                result["pending"] = _pending_request([value])

        if path and path[0] == "requests" and op == "remove":
            result["pending"] = None

        status_value = value
        if path == ["threadRuntimeStatus"] and isinstance(value, Mapping):
            status_value = value.get("type")
        is_thread_status_path = path in (["status"], ["threadRuntimeStatus", "type"]) or (
            bool(path)
            and path[-1] == "status"
            and "items" not in path
            and "hookRuns" not in path
            and ("turns" in path or "turnHistory" in path or "threadRuntimeStatus" in path)
        )
        if is_thread_status_path and isinstance(status_value, str):
            mapped = {
                "active": "running",
                "inProgress": "running",
                "running": "running",
                "idle": "idle",
                "completed": "completed",
                "failed": "failed",
                "errored": "failed",
                "interrupted": "interrupted",
            }.get(status_value)
            if mapped:
                result["status"] = mapped
                if mapped != "running":
                    result["active_turn_id"] = None

        if (
            op in {"add", "replace"}
            and isinstance(value, Mapping)
            and "items" not in path
            and value.get("status") in {"inProgress", "running"}
            and ("turns" in path or "entitiesByKey" in path)
        ):
            result["status"] = "running"
            result["active_turn_id"] = (
                _wire_identifier(value.get("turnId"))
                or _wire_identifier(value.get("id"))
                or result.get("active_turn_id")
            )

    result["messages"] = messages[-MAX_PUBLIC_MESSAGES:]
    if isinstance(result.get("pending"), Mapping):
        result["status"] = (
            "waiting_input" if result["pending"].get("kind") == "input" else "waiting_approval"
        )
    return result


def _button(label: str, button_type: str, value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "behaviors": [{"type": "callback", "value": value}],
    }


def _button_row(buttons: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "auto", "elements": [button]}
            for button in buttons
        ],
    }


def build_desktop_card(state: Any) -> Dict[str, Any]:
    """Build one updateable CardKit 2.0 card from normalized public state."""
    if not isinstance(state, Mapping) or state.get("schema_version") != NORMALIZED_VERSION:
        state = _empty_state()

    status = state.get("status") if state.get("status") in _STATUS_LABELS else "unknown"
    title = _clean_text(state.get("title"), 120) or "Codex Desktop"
    thread_id = _identifier(state.get("thread_id")) or ""
    elements: List[Dict[str, Any]] = [
        {"tag": "markdown", "content": "**{}**\n<font color='grey'>{}</font>".format(title, _STATUS_LABELS[status])}
    ]

    messages = extract_public_events(state)
    if messages:
        elements.append({"tag": "hr"})
        for message in messages[-MAX_CARD_MESSAGES:]:
            label = "完成回复" if message["phase"] == "final_answer" else "进度"
            text = _clean_text(message["text"], MAX_CARD_MESSAGE_CHARS) or ""
            elements.append({"tag": "markdown", "content": "**{}**\n{}".format(label, text)})
    else:
        elements.append({"tag": "markdown", "content": "等待公开进度…"})

    pending = state.get("pending")
    if isinstance(pending, Mapping):
        request_id = _wire_identifier(pending.get("request_id"))
        kind = _identifier(pending.get("request_kind")) or "unknown"
        prompt = _clean_text(pending.get("prompt"), 600) or "需要处理"
        pending_title = _clean_text(pending.get("title"), 120) or "待处理"
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": "**{}**\n{}".format(pending_title, prompt)},
        ])
        details = _clean_text(pending.get("details"), 1000)
        if details:
            elements.append({"tag": "markdown", "content": details})
        if request_id is not None and pending.get("kind") == "approval":
            base = {
                "action": "desktop_approval",
                "thread_id": thread_id,
                "request_id": request_id,
                "kind": kind,
            }
            buttons = []
            if pending.get("allow_remote") is not False:
                buttons.append(_button("允许", "primary", dict(base, decision="accept")))
            buttons.append(_button("拒绝", "danger", dict(base, decision="decline")))
            elements.append(_button_row(buttons))
            if pending.get("allow_remote") is False:
                elements.append({
                    "tag": "markdown",
                    "content": "<font color='grey'>文件变更详情请在 Codex Desktop 中确认；飞书仅支持拒绝。</font>",
                })
        elif request_id is not None and pending.get("kind") == "input":
            options = pending.get("options")
            if isinstance(options, list) and options:
                buttons: List[Dict[str, Any]] = []
                for option in options[:4]:
                    if not isinstance(option, Mapping):
                        continue
                    label = _clean_text(option.get("label"), 80)
                    if label is None:
                        continue
                    buttons.append(_button(label, "default", {
                        "action": "desktop_input",
                        "thread_id": thread_id,
                        "request_id": request_id,
                        "kind": kind,
                        "question_id": _identifier(pending.get("question_id")) or "",
                        "answer": label,
                    }))
                if buttons:
                    elements.append(_button_row(buttons))

    if thread_id:
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": "<font color='grey'>codex://threads/{}</font>".format(thread_id)},
        ])

        controls = [
            _button("断开", "default", {
                "action": "desktop_detach",
                "thread_id": thread_id,
            }),
        ]
        if (
            status in {"running", "waiting_approval", "waiting_input"}
            and state.get("active_turn_id") is not None
        ):
            controls.insert(0, _button("停止", "danger", {
                "action": "desktop_interrupt",
                "thread_id": thread_id,
                "turn_id": state.get("active_turn_id"),
            }))
        input_form = {
            "tag": "form",
            "name": "desktop_input",
            "elements": [
                _button_row(controls + [{
                    "tag": "button",
                    "name": "desktop_send",
                    "text": {"tag": "plain_text", "content": "发送 ↵"},
                    "type": "primary",
                    "action_type": "form_submit",
                }]),
                {
                    "tag": "input",
                    "name": "desktop_command__{}".format(thread_id),
                    "placeholder": {"tag": "plain_text", "content": "向 Desktop 会话发送消息…"},
                    "width": "fill",
                },
            ],
        }
        elements.append(input_form)

    # Construct the result field-by-field.  Never merge the normalized state
    # into it: the private `_conversation_state` may contain sensitive data.
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex Desktop · {}".format(_STATUS_LABELS[status])},
            "subtitle": {"tag": "plain_text", "content": "实时同步"},
            "template": _STATUS_TEMPLATES[status],
        },
        "body": {"elements": elements},
    }


def build_desktop_list_card(
    threads: Any,
    current_thread_id: Optional[str] = None,
    page: int = 0,
) -> Dict[str, Any]:
    """Build a five-item, paginated picker for recent Desktop threads."""
    elements: List[Dict[str, Any]] = []
    valid_threads: List[Tuple[Mapping[str, Any], str]] = []
    if isinstance(threads, list):
        for thread in threads:
            if not isinstance(thread, Mapping):
                continue
            thread_id = _identifier(thread.get("thread_id")) or _identifier(thread.get("id"))
            if thread_id is None:
                continue
            valid_threads.append((thread, thread_id))

    total = len(valid_threads)
    total_pages = max(1, (total + DESKTOP_LIST_PAGE_SIZE - 1) // DESKTOP_LIST_PAGE_SIZE)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, total_pages - 1))
    start = page * DESKTOP_LIST_PAGE_SIZE

    for thread, thread_id in valid_threads[start:start + DESKTOP_LIST_PAGE_SIZE]:
        title = _clean_text(thread.get("title"), 160) or "未命名任务"
        cwd = _clean_text(thread.get("cwd"), 240) or ""
        project_name = _clean_text(thread.get("project_name"), 120)
        if not project_name and cwd:
            project_name = cwd.rstrip("/").rsplit("/", 1)[-1]
        project_name = project_name or "未知项目"
        updated_at = _clean_text(thread.get("updated_at"), 80) or ""
        is_current = thread_id == current_thread_id
        status_icon = _LIST_STATUS_ICONS.get(thread.get("status"), "⚪")
        details = [
            f"{status_icon} **{project_name}**",
            f"Session：**{title}**",
            f"Session ID：`{thread_id}`",
        ]
        if cwd:
            details.append(f"目录：`{cwd}`")
        if updated_at:
            details.append(f"<font color='grey'>更新：{updated_at}</font>")
        action = "desktop_detach" if is_current else "desktop_attach"
        label = "断开" if is_current else "进入"
        button_type = "danger" if is_current else "primary"
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 4,
                    "elements": [{"tag": "markdown", "content": "\n".join(details)}],
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [_button(label, button_type, {
                        "action": action,
                        "thread_id": thread_id,
                    })],
                },
            ],
        })
        elements.append({"tag": "hr"})
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()
    if not elements:
        elements.append({"tag": "markdown", "content": "没有找到本机 Codex Desktop 会话。"})
    elif total_pages > 1:
        previous_button = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "⬅ 上一页"},
            "type": "default",
        }
        next_button = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "下一页 ➡"},
            "type": "default",
        }
        if page == 0:
            previous_button["disabled"] = True
        else:
            previous_button["behaviors"] = [{
                "type": "callback",
                "value": {"action": "desktop_list_page", "page": page - 1},
            }]
        if page >= total_pages - 1:
            next_button["disabled"] = True
        else:
            next_button["behaviors"] = [{
                "type": "callback",
                "value": {"action": "desktop_list_page", "page": page + 1},
            }]
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "small",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "elements": [{"tag": "markdown", "content": " "}]},
                    {"tag": "column", "width": "auto", "elements": [previous_button]},
                    {"tag": "column", "width": "auto", "vertical_align": "center",
                     "elements": [{"tag": "markdown", "content":
                                   f"第 {page + 1}/{total_pages} 页 · 共 {total} 个"}]},
                    {"tag": "column", "width": "auto", "elements": [next_button]},
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "elements": [{"tag": "markdown", "content": " "}]},
                ],
            },
        ])
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex Desktop 会话"},
            "subtitle": {"tag": "plain_text", "content":
                         f"选择任务 · 第 {page + 1}/{total_pages} 页 · 共 {total} 个"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


__all__ = [
    "PatchApplyError",
    "apply_immer_patches",
    "build_desktop_card",
    "build_desktop_list_card",
    "extract_public_events",
    "normalize_conversation_state",
    "normalize_desktop_update",
    "normalize_patch_only_update",
]
