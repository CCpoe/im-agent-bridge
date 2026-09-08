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

from .card_theme import soft_color_tokens, status_color_tokens, status_tone
from .card_time import format_beijing_time


JSON = Union[None, bool, int, float, str, List["JSON"], Dict[str, "JSON"]]

NORMALIZED_VERSION = 1
MAX_PUBLIC_MESSAGES = 20
MAX_PUBLIC_MESSAGE_CHARS = 4_000
MAX_PUBLIC_TURNS = 20
MAX_CARD_MESSAGES = 6
MAX_CARD_MESSAGE_CHARS = 1_500
MAX_CARD_QUERY_CHARS = 1_500
MAX_PUBLIC_IMAGES = 8
MAX_CARD_IMAGES = 4
MAX_PUBLIC_SUB_AGENTS = 32
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

_STATUS_NOTICES = {
    "idle": ("任务已连接", "等待下一条指令。"),
    "running": ("Codex 正在处理", "公开进度会在当前卡片中持续更新。"),
    "waiting_approval": ("需要你的确认", "处理审批后，当前任务才会继续。"),
    "waiting_input": ("需要你的输入", "请选择一个选项或继续发送指令。"),
    "completed": ("当前轮次已完成", "可以继续发送指令开始下一轮。"),
    "failed": ("当前轮次执行失败", "请查看公开回复后决定是否继续。"),
    "interrupted": ("当前轮次已停止", "可以继续发送指令重新开始。"),
    "unknown": ("正在同步任务状态", "状态确认后会自动刷新当前卡片。"),
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


_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\n]+)\s*\)"
)
_LOCAL_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((?:file://|/|~)[^\n)]*\)", re.IGNORECASE
)
_LOCAL_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _safe_image_alt(value: Any) -> str:
    alt = _clean_text(value, 160) or "Codex 生成的图片"
    lowered = alt.lower()
    if lowered.startswith(("file://", "/", "~/", "./", "../")):
        return "Codex 生成的图片"
    if re.match(r"^[a-zA-Z]:[\\/]", alt):
        return "Codex 生成的图片"
    return alt


def _local_image_source(value: Any) -> Optional[str]:
    """Return a bounded local image reference without exposing it to the card."""
    if not isinstance(value, str):
        return None
    source = value.strip()
    if source.startswith("<") and source.endswith(">"):
        source = source[1:-1].strip()
    if not source or len(source) > 2_048 or "\x00" in source:
        return None
    lowered = source.lower()
    if lowered.startswith(("http://", "https://", "data:")):
        return None
    path_part = lowered[7:] if lowered.startswith("file://") else lowered
    if not path_part.endswith(_LOCAL_IMAGE_SUFFIXES):
        return None
    if lowered.startswith("file://") or source.startswith(("/", "~/", "./", "../")):
        return source
    # Codex commonly emits project-relative output paths such as
    # ``outputs/chart.png``.  Other URI schemes remain fail-closed.
    return source if "://" not in source else None


def _sanitize_image_refs(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, str]] = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source = _local_image_source(item.get("source"))
        if source is None or source in seen:
            continue
        seen.add(source)
        result.append({
            "source": source,
            "alt": _safe_image_alt(item.get("alt")),
        })
        if len(result) >= MAX_PUBLIC_IMAGES:
            break
    return result


def _public_markdown_payload(value: Any) -> Tuple[Any, List[Dict[str, str]]]:
    """Strip Markdown image targets while retaining safe in-memory references."""
    if not isinstance(value, str):
        return value, []
    images: List[Dict[str, str]] = []
    seen = set()

    def replace_image(match: re.Match[str]) -> str:
        alt = _safe_image_alt(match.group(1))
        source = _local_image_source(match.group(2))
        if source is not None and source not in seen and len(images) < MAX_PUBLIC_IMAGES:
            seen.add(source)
            images.append({"source": source, "alt": alt})
        return alt

    text = _MARKDOWN_IMAGE_RE.sub(replace_image, value)
    text = _LOCAL_MARKDOWN_LINK_RE.sub(
        lambda match: match.group(1).strip(),
        text,
    )
    return text, images


def _public_markdown_text(value: Any) -> Any:
    """Remove media targets and local-file links from otherwise public text."""
    return _public_markdown_payload(value)[0]


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
        "turns": [],
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


def _public_text_content(value: Any, limit: int = MAX_PUBLIC_MESSAGE_CHARS) -> Optional[str]:
    """Project text from a Desktop user-input content list.

    Desktop content arrays can also contain images, audio, skills, mentions and
    local paths.  Only exact ``text`` blocks are public here; all other block
    types are deliberately ignored.
    """
    if not isinstance(value, list):
        return None
    parts: List[str] = []
    for block in value:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = _clean_text(_public_markdown_text(block.get("text")), limit)
        if text:
            parts.append(text)
    return _clean_text("\n".join(parts), limit) if parts else None


def _public_user_item(item: Any) -> Optional[Dict[str, str]]:
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    if item_type == "userMessage":
        text = _public_text_content(item.get("content"))
        kind = "initial"
    elif item_type == "steeringUserMessage":
        text = _public_text_content(item.get("input"))
        kind = "steering"
    else:
        return None
    if text is None:
        return None
    return {
        "id": _identifier(item.get("id")) or "",
        "kind": kind,
        "text": text,
    }


def _public_agent_item(item: Any, turn_id: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
        return None
    phase = item.get("phase")
    public_text, parsed_images = _public_markdown_payload(item.get("text"))
    images = parsed_images + [
        image
        for image in _sanitize_image_refs(item.get("images"))
        if image["source"] not in {existing["source"] for existing in parsed_images}
    ]
    images = images[:MAX_PUBLIC_IMAGES]
    text = _clean_text(public_text, MAX_PUBLIC_MESSAGE_CHARS)
    if text is None and images:
        text = images[0]["alt"]
    if phase not in _PUBLIC_AGENT_PHASES or text is None:
        return None
    result: Dict[str, Any] = {
        "id": _identifier(item.get("id")) or "",
        "turn_id": turn_id,
        "phase": phase or "final_answer",
        "text": text,
    }
    if images:
        result["images"] = images
    return result


def _normalized_turn_status(value: Any) -> str:
    return {
        "active": "running",
        "inProgress": "running",
        "running": "running",
        "idle": "idle",
        "completed": "completed",
        "failed": "failed",
        "errored": "failed",
        "systemError": "failed",
        "interrupted": "interrupted",
    }.get(value, "unknown")


def project_subagent_activity(item: Any) -> Optional[Dict[str, str]]:
    """仅投影已确认的子 Agent 活动元数据，不读取提示词或执行内容。"""
    if not isinstance(item, Mapping) or item.get("type") != "subAgentActivity":
        return None
    thread_id = item.get("agentThreadId")
    kind = item.get("kind")
    if (
        not isinstance(thread_id, str)
        or not thread_id.strip()
        or len(thread_id) > 200
        or not isinstance(kind, str)
        or kind not in {"started", "interacted", "completed"}
    ):
        return None
    return {
        "thread_id": thread_id.strip(),
        "agent_path": _clean_text(item.get("agentPath"), 240) or "",
        "status": {"started": "running", "completed": "completed", "interacted": "unknown"}[kind],
    }


def _merge_sub_agent(rows: List[Dict[str, str]], row: Mapping[str, str]) -> None:
    """按每轮 thread_id 去重，保留最新活动及最近使用的有界任务列表。"""
    existing = next((item for item in rows if item["thread_id"] == row["thread_id"]), None)
    projected = dict(row)
    if not projected.get("agent_path") and existing is not None:
        projected["agent_path"] = existing["agent_path"]
    rows[:] = [item for item in rows if item["thread_id"] != row["thread_id"]]
    rows.append(projected)
    del rows[:-MAX_PUBLIC_SUB_AGENTS]


def _sanitize_sub_agents(value: Any) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        status = item.get("status") if isinstance(item, Mapping) else None
        if not isinstance(status, str) or status not in {"running", "completed", "failed", "interrupted", "unknown"}:
            continue
        projected = project_subagent_activity({
            "type": "subAgentActivity",
            "agentThreadId": item.get("thread_id"),
            "agentPath": item.get("agent_path"),
            "kind": "started",
        })
        if projected is not None:
            projected["status"] = status
            _merge_sub_agent(rows, projected)
    return rows


def _project_public_turn(turn: Any, fallback_id: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(turn, Mapping) or not isinstance(turn.get("items"), list):
        return None
    turn_id = (
        _identifier(turn.get("turnId"))
        or _identifier(turn.get("id"))
        or fallback_id
    )
    if not turn_id:
        return None
    user_messages: List[Dict[str, str]] = []
    agent_messages: List[Dict[str, str]] = []
    sub_agents: List[Dict[str, str]] = []
    for item in turn["items"]:
        user_message = _public_user_item(item)
        if user_message is not None:
            user_messages.append(user_message)
            continue
        agent_message = _public_agent_item(item, turn_id)
        if agent_message is not None:
            agent_messages.append(agent_message)
            continue
        sub_agent = project_subagent_activity(item)
        if sub_agent is not None:
            _merge_sub_agent(sub_agents, sub_agent)
    result = {
        "turn_id": turn_id,
        "status": _normalized_turn_status(turn.get("status")),
        "user_messages": user_messages,
        "agent_messages": agent_messages[-MAX_PUBLIC_MESSAGES:],
    }
    if sub_agents:
        result["sub_agents"] = sub_agents
    return result


def _sanitize_public_turn(turn: Any) -> Optional[Dict[str, Any]]:
    """Revalidate an already-normalized turn before rendering it."""
    if not isinstance(turn, Mapping):
        return None
    turn_id = _identifier(turn.get("turn_id"))
    if turn_id is None:
        return None
    user_messages: List[Dict[str, str]] = []
    raw_users = turn.get("user_messages")
    if isinstance(raw_users, list):
        for message in raw_users:
            if not isinstance(message, Mapping):
                continue
            text = _clean_text(message.get("text"), MAX_PUBLIC_MESSAGE_CHARS)
            kind = message.get("kind")
            if text is None or kind not in {"initial", "steering"}:
                continue
            user_messages.append({
                "id": _identifier(message.get("id")) or "",
                "kind": kind,
                "text": text,
            })
    agent_messages: List[Dict[str, str]] = []
    raw_agents = turn.get("agent_messages")
    if isinstance(raw_agents, list):
        for message in raw_agents:
            projected = _public_agent_item({
                "id": message.get("id") if isinstance(message, Mapping) else None,
                "type": "agentMessage",
                "phase": message.get("phase") if isinstance(message, Mapping) else None,
                "text": message.get("text") if isinstance(message, Mapping) else None,
                "images": message.get("images") if isinstance(message, Mapping) else None,
            }, turn_id)
            if projected is not None:
                agent_messages.append(projected)
    status = turn.get("status")
    if status not in _STATUS_LABELS:
        status = "unknown"
    result = {
        "turn_id": turn_id,
        "status": status,
        "user_messages": user_messages,
        "agent_messages": agent_messages[-MAX_PUBLIC_MESSAGES:],
    }
    sub_agents = _sanitize_sub_agents(turn.get("sub_agents"))
    if sub_agents:
        result["sub_agents"] = sub_agents
    return result


def extract_public_turns(state: Any) -> List[Dict[str, Any]]:
    """Return bounded, ordered turns containing only explicitly public fields."""
    if isinstance(state, Mapping) and state.get("schema_version") == NORMALIZED_VERSION:
        turns = state.get("turns")
        if not isinstance(turns, list):
            return []
        result = [projected for turn in turns if (projected := _sanitize_public_turn(turn))]
        return result[-MAX_PUBLIC_TURNS:]

    raw = _unwrap_state(state)
    if not isinstance(raw, Mapping):
        return []
    turns = _ordered_turns(raw)
    if turns is None:
        return []
    result: List[Dict[str, Any]] = []
    for turn in turns:
        projected = _project_public_turn(turn)
        if projected is None:
            # Unknown turn structure fails closed instead of being recursively
            # scraped for anything that merely looks like text.
            return []
        result.append(projected)
    return result[-MAX_PUBLIC_TURNS:]


def _safe_patch_item(item: Any, turn_id: str) -> Optional[Dict[str, Any]]:
    sub_agent = project_subagent_activity(item)
    if sub_agent is not None:
        return {
            "type": "subAgentActivity",
            "id": _identifier(item.get("id")) or "",
            "turn_id": turn_id,
            "kind": item["kind"],
            "agentThreadId": sub_agent["thread_id"],
            "agentPath": sub_agent["agent_path"],
        }
    agent = _public_agent_item(item, turn_id)
    if agent is not None:
        return dict(agent, type="agentMessage")
    user = _public_user_item(item)
    if user is not None:
        item_type = item.get("type") if isinstance(item, Mapping) else None
        return dict(user, type=item_type, turn_id=turn_id)
    return None


def _safe_patch_items_from_state(state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}

    def add_items(prefix: List[Any], turn: Mapping[str, Any], fallback_id: str = "") -> None:
        items = turn.get("items")
        if not isinstance(items, list):
            return
        turn_id = (
            _identifier(turn.get("turnId"))
            or _identifier(turn.get("id"))
            or fallback_id
        )
        if not turn_id:
            return
        for index, item in enumerate(items):
            projected = _safe_patch_item(item, turn_id)
            if projected is None:
                continue
            key = json.dumps(prefix + ["items", index], ensure_ascii=False, separators=(",", ":"))
            result[key] = projected
            while len(result) > 128:
                result.pop(next(iter(result)))

    turn_history = state.get("turnHistory")
    if isinstance(turn_history, Mapping):
        history = turn_history.get("history")
        entities = history.get("entitiesByKey") if isinstance(history, Mapping) else None
        if isinstance(entities, Mapping):
            for key, turn in entities.items():
                if isinstance(key, str) and isinstance(turn, Mapping):
                    add_items(["turnHistory", "history", "entitiesByKey", key], turn, key)

    # current turns last, so the bounded index always retains streaming item paths.
    turns = state.get("turns")
    if isinstance(turns, list):
        for index, turn in enumerate(turns):
            if isinstance(turn, Mapping):
                add_items(["turns", index], turn)
    return result


def _safe_patch_turn_ids_from_state(state: Mapping[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    turn_history = state.get("turnHistory")
    if isinstance(turn_history, Mapping):
        history = turn_history.get("history")
        entities = history.get("entitiesByKey") if isinstance(history, Mapping) else None
        if isinstance(entities, Mapping):
            for key, turn in entities.items():
                if not isinstance(key, str) or not isinstance(turn, Mapping):
                    continue
                turn_id = _identifier(turn.get("turnId")) or _identifier(turn.get("id")) or key
                result[json.dumps(
                    ["turnHistory", "history", "entitiesByKey", key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )] = turn_id
    turns = state.get("turns")
    if isinstance(turns, list):
        for index, turn in enumerate(turns):
            if not isinstance(turn, Mapping):
                continue
            turn_id = _identifier(turn.get("turnId")) or _identifier(turn.get("id"))
            if turn_id:
                result[json.dumps(["turns", index], separators=(",", ":"))] = turn_id
    return result


def extract_public_events(state: Any) -> List[Dict[str, Any]]:
    """Extract only user-visible assistant messages from a conversation state.

    Accepted items are exactly ``agentMessage`` entries whose phase is
    ``commentary`` or ``final_answer`` (or absent for older Desktop versions).
    No recursive fallback is used; this is intentional so an unknown schema
    cannot accidentally surface reasoning or tool output.
    """
    if isinstance(state, Mapping) and state.get("schema_version") == NORMALIZED_VERSION:
        turns = extract_public_turns(state)
        if turns:
            return [
                message
                for turn in turns
                for message in turn["agent_messages"]
            ][-MAX_PUBLIC_MESSAGES:]
        messages = state.get("messages")
        if not isinstance(messages, list):
            return []
        result: List[Dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            projected = _public_agent_item({
                "id": message.get("id"),
                "type": "agentMessage",
                "phase": message.get("phase"),
                "text": message.get("text"),
                "images": message.get("images"),
            }, _identifier(message.get("turn_id")) or "")
            if projected is not None:
                result.append(projected)
        return result[-MAX_PUBLIC_MESSAGES:]

    return [
        message
        for turn in extract_public_turns(state)
        for message in turn["agent_messages"]
    ][-MAX_PUBLIC_MESSAGES:]


def extract_card_image_sources(
    state: Any, selected_turn_id: Optional[str] = None
) -> List[Dict[str, str]]:
    """Return the bounded local images needed by the currently rendered card.

    Sources stay in memory and are consumed by :class:`DesktopBridgeManager` to
    obtain Lark ``img_key`` values.  They are never copied into the card JSON.
    """
    turns = extract_public_turns(state)
    messages: List[Mapping[str, Any]]
    if turns:
        selected = turns[-1]
        requested = _identifier(selected_turn_id)
        if requested:
            selected = next(
                (turn for turn in turns if turn.get("turn_id") == requested),
                selected,
            )
        messages = selected.get("agent_messages") or []
    else:
        messages = extract_public_events(state)

    refs: List[Dict[str, str]] = []
    seen = set()
    for message in reversed(messages[-MAX_CARD_MESSAGES:]):
        if not isinstance(message, Mapping):
            continue
        images = _sanitize_image_refs(message.get("images"))
        for image in reversed(images):
            source = image["source"]
            if source in seen:
                continue
            seen.add(source)
            refs.append(image)
            if len(refs) >= MAX_CARD_IMAGES:
                return list(reversed(refs))
    return list(reversed(refs))


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

    public_turns = extract_public_turns(raw)
    messages = [
        message
        for turn in public_turns
        for message in turn["agent_messages"]
    ][-MAX_PUBLIC_MESSAGES:]
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
        "turns": public_turns,
        "messages": messages,
        "pending": pending,
        "_patch_items": _safe_patch_items_from_state(raw),
        "_patch_turn_ids": _safe_patch_turn_ids_from_state(raw),
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


def _turn_prefix(path: Sequence[Any]) -> Optional[List[Any]]:
    if len(path) >= 2 and path[0] == "turns" and isinstance(path[1], int):
        return list(path[:2])
    if (
        len(path) >= 4
        and path[:3] == ["turnHistory", "history", "entitiesByKey"]
        and isinstance(path[3], str)
    ):
        return list(path[:4])
    return None


def _turn_id_for_patch_path(
    path: Sequence[Any], turn_ids: Mapping[str, str]
) -> Optional[str]:
    prefix = _turn_prefix(path)
    if prefix is None:
        return None
    key = json.dumps(prefix, ensure_ascii=False, separators=(",", ":"))
    known = _identifier(turn_ids.get(key))
    if known:
        return known
    if len(prefix) == 4:
        return _identifier(prefix[-1])
    return None


def _upsert_public_turn(
    turns: List[Dict[str, Any]], turn: Mapping[str, Any], index: Optional[int] = None
) -> None:
    projected = _sanitize_public_turn(turn)
    if projected is None:
        return
    for old_index, old in enumerate(turns):
        if old.get("turn_id") == projected["turn_id"]:
            turns[old_index] = projected
            return
    if index is not None and 0 <= index <= len(turns):
        turns.insert(index, projected)
    else:
        turns.append(projected)
    del turns[:-MAX_PUBLIC_TURNS]


def _remove_public_item(turns: List[Dict[str, Any]], item: Any, fallback_id: str) -> None:
    if not isinstance(item, Mapping):
        return
    if item.get("type") == "subAgentActivity":
        # 活动 item 与状态行不是一对一；由安全缓存按同轮同 Agent 重新聚合。
        return
    turn_id = _identifier(item.get("turn_id"))
    item_id = _identifier(item.get("id")) or fallback_id
    item_type = item.get("type")
    field = "agent_messages" if item_type == "agentMessage" else "user_messages"
    for turn in turns:
        if turn.get("turn_id") != turn_id:
            continue
        turn[field] = [
            message
            for message in turn.get(field, [])
            if (_identifier(message.get("id")) or fallback_id) != item_id
        ]


def _upsert_public_item(
    turns: List[Dict[str, Any]], item: Any, fallback_id: str
) -> None:
    if not isinstance(item, Mapping):
        return
    turn_id = _identifier(item.get("turn_id"))
    if turn_id is None:
        return
    target = next((turn for turn in turns if turn.get("turn_id") == turn_id), None)
    if target is None:
        target = {
            "turn_id": turn_id,
            "status": "unknown",
            "user_messages": [],
            "agent_messages": [],
        }
        turns.append(target)
        del turns[:-MAX_PUBLIC_TURNS]

    item_id = _identifier(item.get("id")) or fallback_id
    if item.get("type") == "subAgentActivity":
        projected_sub_agent = project_subagent_activity(item)
        if projected_sub_agent is not None:
            _merge_sub_agent(target.setdefault("sub_agents", []), projected_sub_agent)
        return
    elif item.get("type") == "agentMessage":
        projected_agent = _public_agent_item(item, turn_id)
        if projected_agent is None:
            return
        projected = dict(projected_agent)
        projected["id"] = item_id
        field = "agent_messages"
    elif item.get("type") in {"userMessage", "steeringUserMessage"}:
        text = _clean_text(item.get("text"), MAX_PUBLIC_MESSAGE_CHARS)
        kind = item.get("kind")
        if text is None or kind not in {"initial", "steering"}:
            return
        projected = {"id": item_id, "kind": kind, "text": text}
        field = "user_messages"
    else:
        return

    existing = target[field]
    for index, message in enumerate(existing):
        if (_identifier(message.get("id")) or fallback_id) == item_id:
            existing[index] = projected
            break
    else:
        existing.append(projected)
    if field == "agent_messages":
        del existing[:-MAX_PUBLIC_MESSAGES]


def _patch_sub_agent_key(item: Any) -> Optional[Tuple[str, str]]:
    row = project_subagent_activity(item)
    turn_id = _identifier(item.get("turn_id")) if isinstance(item, Mapping) else None
    return (turn_id, row["thread_id"]) if row is not None and turn_id else None


def _shift_patch_item_indices(
    patch_items: Dict[str, Any], list_path: Sequence[Any], index: int, delta: int
) -> None:
    """同步 Immer 数组插入/删除后的安全缓存路径，避免后续状态写到其它 item。"""
    shifted: Dict[str, Any] = {}
    for key, item in patch_items.items():
        try:
            path = json.loads(key)
        except (TypeError, ValueError):
            shifted[key] = item
            continue
        if (
            isinstance(path, list)
            and len(path) == len(list_path) + 1
            and path[:-1] == list(list_path)
            and isinstance(path[-1], int)
            and path[-1] >= index
        ):
            path[-1] += delta
            key = json.dumps(path, ensure_ascii=False, separators=(",", ":"))
        shifted[key] = item
    patch_items.clear()
    patch_items.update(shifted)


def _refresh_patch_sub_agent(
    turns: List[Dict[str, Any]],
    patch_items: Mapping[str, Any],
    key: Tuple[str, str],
) -> None:
    """旧活动更新或删除时，按 wire item 顺序重算该 Agent，不影响其它轮。"""
    turn_id, thread_id = key
    target = next((turn for turn in turns if turn.get("turn_id") == turn_id), None)
    if target is None:
        return
    activities = []
    for path_key, item in patch_items.items():
        if _patch_sub_agent_key(item) != key:
            continue
        try:
            path = json.loads(path_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(path, list) or not path or not isinstance(path[-1], int):
            continue
        # canonical 历史与 active turn 短暂重复时，当前 turn 的投影优先。
        activities.append(((path[0] == "turns", path[-1]), item))
    projected: List[Dict[str, str]] = []
    for _, item in sorted(activities, key=lambda pair: pair[0]):
        row = project_subagent_activity(item)
        if row is not None:
            _merge_sub_agent(projected, row)
    rows = target.get("sub_agents", [])
    previous = next((row for row in rows if row["thread_id"] == thread_id), None)
    rows = [row for row in rows if row["thread_id"] != thread_id]
    if projected:
        row = projected[-1]
        if not row["agent_path"] and previous is not None:
            row["agent_path"] = previous["agent_path"]
        _merge_sub_agent(rows, row)
    if rows:
        target["sub_agents"] = rows
    else:
        target.pop("sub_agents", None)


def normalize_patch_only_update(
    current: Optional[Mapping[str, Any]],
    event: Any,
) -> Dict[str, Any]:
    """Safely project useful v11 patches when a snapshot is unavailable.

    The patch projection recognizes only public user/steering text, public
    assistant messages, sub-agent activity metadata, turn status leaves and top-level requests.  It never
    recursively searches arbitrary payloads for displayable strings.
    """
    params = _event_params(event)
    current = current if isinstance(current, Mapping) else {}
    current_turns = extract_public_turns(current)
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
        "turns": copy.deepcopy(current_turns),
        "messages": list(current.get("messages") or [])[-MAX_PUBLIC_MESSAGES:],
        "pending": current.get("pending") if isinstance(current.get("pending"), Mapping) else None,
        "needs_snapshot": True,
        "patch_only": True,
        "_patch_items": copy.deepcopy(current.get("_patch_items") or {}),
        "_patch_turn_ids": copy.deepcopy(current.get("_patch_turn_ids") or {}),
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

    turns = result["turns"]
    patch_items = result["_patch_items"]
    patch_turn_ids = result["_patch_turn_ids"]
    for patch in patches:
        if not isinstance(patch, Mapping):
            continue
        path = patch.get("path")
        op = patch.get("op")
        value = patch.get("value")
        if not isinstance(path, list) or op not in {"add", "replace", "remove"}:
            continue

        prefix = _turn_prefix(path)
        prefix_key = (
            json.dumps(prefix, ensure_ascii=False, separators=(",", ":"))
            if prefix is not None else None
        )
        is_whole_turn = prefix is not None and len(path) == len(prefix)
        if is_whole_turn:
            old_turn_id = _identifier(patch_turn_ids.get(prefix_key))
            if op == "remove":
                if old_turn_id:
                    represented_elsewhere = any(
                        key != prefix_key and _identifier(candidate) == old_turn_id
                        for key, candidate in patch_turn_ids.items()
                    )
                    if not represented_elsewhere:
                        turns[:] = [
                            turn for turn in turns
                            if turn.get("turn_id") != old_turn_id
                        ]
                    if (
                        prefix
                        and prefix[0] == "turns"
                        and _identifier(result.get("active_turn_id")) == old_turn_id
                    ):
                        result["active_turn_id"] = None
                if prefix_key:
                    patch_turn_ids.pop(prefix_key, None)
                    for key in list(patch_items):
                        if key.startswith(prefix_key[:-1] + ","):
                            patch_items.pop(key, None)
            elif isinstance(value, Mapping):
                fallback_id = (
                    _identifier(prefix[-1]) if len(prefix) == 4 else ""
                ) or ""
                projected_turn = _project_public_turn(value, fallback_id)
                if projected_turn is not None:
                    # ``turns`` in the wire state is the current/active-turn
                    # container, while our public list also contains historical
                    # ``turnHistory`` entries.  Its index therefore cannot be
                    # reused in the merged public list: add turns[0] must appear
                    # after history so the default card renders the new turn.
                    _upsert_public_turn(turns, projected_turn)
                    if prefix_key:
                        patch_turn_ids[prefix_key] = projected_turn["turn_id"]
                        for key in list(patch_items):
                            if key.startswith(prefix_key[:-1] + ","):
                                patch_items.pop(key, None)
                    for item_index, item in enumerate(value.get("items") or []):
                        item_key = json.dumps(
                            prefix + ["items", item_index],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        projected_item = _safe_patch_item(item, projected_turn["turn_id"])
                        if projected_item is not None:
                            if not _identifier(projected_item.get("id")):
                                projected_item["id"] = item_key
                            patch_items[item_key] = projected_item

        item_key: Optional[str] = None
        item_offset: Optional[int] = None
        if "items" in path:
            item_index = path.index("items")
            if len(path) > item_index + 1:
                item_offset = item_index + 2
                item_key = json.dumps(path[:item_offset], ensure_ascii=False, separators=(",", ":"))

        if item_key is not None and item_offset is not None:
            whole_item = len(path) == item_offset and isinstance(path[-1], int)
            if whole_item and op == "add":
                _shift_patch_item_indices(patch_items, path[:-1], path[-1], 1)
            old_partial = patch_items.get(item_key)
            old_sub_agent_key = _patch_sub_agent_key(old_partial)
            _remove_public_item(turns, old_partial, item_key)
            turn_id = _turn_id_for_patch_path(path, patch_turn_ids)
            if turn_id is None and prefix and prefix[0] == "turns":
                turn_id = _identifier(result.get("active_turn_id"))
            if op == "remove" and len(path) == item_offset:
                patch_items.pop(item_key, None)
                if whole_item:
                    _shift_patch_item_indices(patch_items, path[:-1], path[-1] + 1, -1)
            elif op in {"add", "replace"} and len(path) == item_offset and isinstance(value, Mapping):
                projected_item = _safe_patch_item(value, turn_id or "")
                if projected_item is not None:
                    if not _identifier(projected_item.get("id")):
                        projected_item["id"] = item_key
                    patch_items[item_key] = projected_item
                else:
                    patch_items.pop(item_key, None)
            elif item_key in patch_items and len(path) == item_offset + 1:
                field = path[-1]
                partial = patch_items[item_key]
                if partial.get("type") == "subAgentActivity":
                    if field in {"id", "type", "kind", "agentThreadId", "agentPath"}:
                        if op == "remove":
                            partial.pop(field, None)
                        elif field == "kind":
                            if isinstance(value, str) and value in {"started", "interacted", "completed"}:
                                partial[field] = value
                            else:
                                partial.pop(field, None)
                        elif field == "type":
                            if value != "subAgentActivity":
                                patch_items.pop(item_key, None)
                        elif field == "agentPath":
                            partial[field] = _clean_text(value, 240) or ""
                        elif isinstance(value, str) and 0 < len(value.strip()) <= 200:
                            partial[field] = value.strip()
                        else:
                            partial.pop(field, None)
                elif field in {"id", "type", "phase", "text", "images"}:
                    if op == "remove":
                        partial.pop(field, None)
                        if field == "text":
                            partial.pop("images", None)
                    elif field == "images":
                        images = _sanitize_image_refs(value)
                        if images:
                            partial["images"] = images
                        else:
                            partial.pop("images", None)
                    elif field == "text" and partial.get("type") == "agentMessage":
                        public_text, images = _public_markdown_payload(value)
                        text = _clean_text(public_text, MAX_PUBLIC_MESSAGE_CHARS)
                        if text is None:
                            partial.pop("text", None)
                        else:
                            partial["text"] = text
                        if images:
                            partial["images"] = images
                        else:
                            partial.pop("images", None)
                    else:
                        partial[field] = value
                elif field in {"content", "input"}:
                    text = None if op == "remove" else _public_text_content(value)
                    if text is None:
                        partial.pop("text", None)
                    else:
                        partial["text"] = text
                if turn_id:
                    partial["turn_id"] = turn_id

            partial = patch_items.get(item_key)
            _upsert_public_item(turns, partial, item_key)
            for sub_agent_key in {old_sub_agent_key, _patch_sub_agent_key(partial)} - {None}:
                _refresh_patch_sub_agent(turns, patch_items, sub_agent_key)

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
            mapped = _normalized_turn_status(status_value)
            if mapped != "unknown":
                result["status"] = mapped
                turn_id = _turn_id_for_patch_path(path, patch_turn_ids)
                for turn in turns:
                    if turn.get("turn_id") == turn_id:
                        turn["status"] = mapped
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

    result["turns"] = turns[-MAX_PUBLIC_TURNS:]
    represented_turns = {turn["turn_id"] for turn in result["turns"]}
    legacy_messages = []
    for message in result["messages"]:
        if not isinstance(message, Mapping) or message.get("turn_id") in represented_turns:
            continue
        projected = _public_agent_item({
            "id": message.get("id"),
            "type": "agentMessage",
            "phase": message.get("phase"),
            "text": message.get("text"),
            "images": message.get("images"),
        }, _identifier(message.get("turn_id")) or "")
        if projected is not None:
            legacy_messages.append(projected)
    result["messages"] = (
        legacy_messages
        + [message for turn in result["turns"] for message in turn["agent_messages"]]
    )[-MAX_PUBLIC_MESSAGES:]
    if isinstance(result.get("pending"), Mapping):
        result["status"] = (
            "waiting_input" if result["pending"].get("kind") == "input" else "waiting_approval"
        )
    return result


def _summary_content(title: str, status_label: str) -> str:
    summary = "{}：Codex Desktop {}".format(title, status_label)
    return _clean_text(summary, 60) or "Codex Desktop 任务状态已更新"


def _workspace_config(summary: str) -> Dict[str, Any]:
    return {
        "wide_screen_mode": True,
        "update_multi": True,
        "compact_width": False,
        "enable_forward": False,
        "enable_forward_interaction": False,
        "streaming_mode": False,
        "summary": {"content": _clean_text(summary, 60) or "Codex Desktop 任务状态已更新"},
        "style": {"color": soft_color_tokens()},
    }


def _markdown(
    content: str,
    *,
    text_size: str = "normal",
    margin: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": text_size,
    }
    if margin is not None:
        result["margin"] = margin
    return result


def _message_image_elements(
    message: Mapping[str, Any],
    image_keys: Mapping[str, str],
    rendered_sources: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    images: List[Dict[str, str]] = []
    seen = rendered_sources if rendered_sources is not None else set()
    for image in _sanitize_image_refs(message.get("images")):
        source = image["source"]
        if source in seen or len(seen) >= MAX_CARD_IMAGES:
            continue
        img_key = image_keys.get(source)
        if not isinstance(img_key, str) or not img_key.strip():
            continue
        seen.add(source)
        images.append({
            "img_key": img_key.strip(),
        })
    if not images:
        return []
    count = len(images)
    return [{
            "tag": "img_combination",
            "combination_mode": (
                "double" if count <= 2 else "triple" if count == 3 else "bisect"
            ),
            "img_list": images,
            "img_list_length": count,
            "corner_radius": "12px",
            "margin": "12px 0px 0px 0px",
        }]


def _heading(
    title: str,
    subtitle: str,
    tag_label: str,
    *,
    status: str = "unknown",
) -> List[Dict[str, Any]]:
    status_background, status_text = status_color_tokens(status)
    return [
        _markdown(
            "<font color='codex_ink'>**{}**</font>".format(
                _clean_text(title, 60) or "Codex Desktop"
            ),
            text_size="heading-2",
            margin="0px 0px 0px 0px",
        ),
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "horizontal_spacing": "8px",
            "horizontal_align": "left",
            "columns": [{
                "tag": "column",
                "width": "auto",
                "vertical_align": "top",
                "elements": [{
                    "tag": "interactive_container",
                    "behaviors": [],
                    "width": "auto",
                    "height": "auto",
                    "corner_radius": "999px",
                    "has_border": False,
                    "disabled": False,
                    "background_style": status_background,
                    "padding": "3px 8px 3px 8px",
                    "direction": "vertical",
                    "horizontal_spacing": "0px",
                    "vertical_spacing": "0px",
                    "horizontal_align": "center",
                    "vertical_align": "top",
                    "elements": [_markdown(
                        "<font color='{}'>{}</font>".format(status_text, tag_label),
                        text_size="notation",
                        margin="0px 0px 0px 0px",
                    )],
                }],
            }, {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [_markdown(
                    "<font color='codex_muted'>{}</font>".format(subtitle),
                    margin="0px 0px 0px 0px",
                )],
            }],
            "margin": "8px 0px 0px 0px",
        },
    ]


def _surface(
    elements: Iterable[Dict[str, Any]],
    *,
    background: str = "codex_panel",
    margin: str = "12px 0px 0px 0px",
    corner_radius: str = "14px",
    padding: str = "16px 16px 16px 16px",
) -> Dict[str, Any]:
    return {
        "tag": "interactive_container",
        "behaviors": [],
        "width": "fill",
        "height": "auto",
        "corner_radius": corner_radius,
        "has_border": True,
        "border_color": "codex_secondary",
        "disabled": False,
        "background_style": background,
        "padding": padding,
        "direction": "vertical",
        "horizontal_spacing": "0px",
        "vertical_spacing": "0px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "margin": margin,
        "elements": list(elements),
    }


def _message_surface(elements: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "tag": "interactive_container",
        "behaviors": [],
        "width": "fill",
        "height": "auto",
        "corner_radius": "14px",
        "has_border": True,
        "border_color": "codex_secondary",
        "disabled": False,
        "background_style": "codex_body",
        "padding": "12px 14px 12px 14px",
        "direction": "vertical",
        "horizontal_spacing": "0px",
        "vertical_spacing": "0px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "margin": "16px 0px 0px 0px",
        "elements": list(elements),
    }


def _status_grid(status: str, context: str, *, historical: bool = False) -> Dict[str, Any]:
    status_background, status_text = status_color_tokens("unknown" if historical else status)
    if historical:
        title = "历史轮次"
        detail = "审批、输入和停止操作已隐藏。"
    else:
        title, detail = _STATUS_NOTICES[status]
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": status_background,
                "padding": "18px 16px 18px 16px",
                "direction": "vertical",
                "horizontal_spacing": "0px",
                "vertical_spacing": "0px",
                "horizontal_align": "left",
                "vertical_align": "top",
                "elements": [
                    _markdown("<font color='{}'>NOW</font>".format(status_text), text_size="notation"),
                    _markdown(
                        "<font color='{}'>**{}**</font>".format(status_text, title),
                        text_size="heading-2",
                        margin="6px 0px 0px 0px",
                    ),
                    _markdown(
                        "<font color='{}'>{}</font>".format(status_text, detail),
                        text_size="notation",
                        margin="8px 0px 0px 0px",
                    ),
                ],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "codex_accent_2",
                "padding": "18px 16px 18px 16px",
                "direction": "vertical",
                "horizontal_spacing": "0px",
                "vertical_spacing": "0px",
                "horizontal_align": "left",
                "vertical_align": "top",
                "elements": [
                    _markdown("<font color='codex_on_accent'>VIEW</font>", text_size="notation"),
                    _markdown(
                        "<font color='codex_on_accent'>**{}**</font>".format(context),
                        text_size="heading-2",
                        margin="6px 0px 0px 0px",
                    ),
                    _markdown(
                        "<font color='codex_on_accent'>{}</font>".format(
                            "Codex Desktop · 历史只读视图"
                            if historical
                            else "Codex Desktop · 实时同步"
                        ),
                        text_size="notation",
                        margin="8px 0px 0px 0px",
                    ),
                ],
            },
        ],
        "margin": "0px 0px 0px 0px",
    }


def _highlight_grid(
    left_label: str,
    left_title: str,
    left_detail: str,
    right_label: str,
    right_title: str,
    right_detail: str,
    *,
    right_action: Optional[Mapping[str, Any]] = None,
    left_status: str = "unknown",
) -> Dict[str, Any]:
    left_background, left_text = status_color_tokens(left_status)
    if status_tone(left_status) == "success":
        left_background = "codex_accent"
    right_background, right_text = (
        ("codex_accent_2", "codex_button_text")
        if right_action else status_color_tokens("unknown")
    )
    right_surface = _surface([
        _markdown("<font color='{}'>{}</font>".format(right_text, right_label),
                  text_size="notation"),
        _markdown("<font color='{}'>**{}**</font>".format(right_text, right_title),
                  text_size="heading-2", margin="6px 0px 0px 0px"),
        _markdown("<font color='{}'>{}</font>".format(right_text, right_detail),
                  text_size="notation", margin="8px 0px 0px 0px"),
    ], background=right_background, margin="0px 0px 0px 0px")
    if right_action:
        # 整个 NEXT 区域承接连接操作，保留原来的 callback 协议。
        right_surface["behaviors"] = [{"type": "callback", "value": dict(right_action)}]
        right_surface["hover_tips"] = {
            "tag": "plain_text", "content": right_title, "text_align": "left",
        }
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_surface([
                    _markdown("<font color='{}'>{}</font>".format(left_text, left_label),
                              text_size="notation"),
                    _markdown("<font color='{}'>**{}**</font>".format(left_text, left_title),
                              text_size="heading-2", margin="6px 0px 0px 0px"),
                    _markdown("<font color='{}'>{}</font>".format(left_text, left_detail),
                              text_size="notation", margin="8px 0px 0px 0px"),
                ], background=left_background, margin="0px 0px 0px 0px")],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [right_surface],
            },
        ],
        "margin": "0px 0px 0px 0px",
    }


def _footer(content: str, *, margin: str = "20px 0px 0px 0px") -> Dict[str, Any]:
    return _markdown(
        "<font color='codex_muted'>{}</font>".format(content),
        text_size="small",
        margin=margin,
    )


def _collapsible_digest(
    title: str,
    expanded_title: str,
    elements: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "margin": "12px 0px 0px 0px",
        "padding": "0px 12px 12px 12px",
        "background_color": "codex_body",
        "border": {
            "color": "codex_secondary",
            "corner_radius": "12px",
        },
        "header": {
            "title": {"tag": "plain_text", "content": title, "text_align": "left"},
            "expanded_title": {
                "tag": "plain_text",
                "content": expanded_title,
                "text_align": "left",
            },
            "padding": "8px 12px 8px 12px",
            "width": "fill",
            "icon": {"tag": "standard_icon", "token": "down_outlined", "color": "grey"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": list(elements),
    }


def _sub_agents_panel(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    running = sum(row["status"] == "running" for row in rows)
    completed = sum(row["status"] == "completed" for row in rows)
    summary = "{} 运行中 · {} 已完成".format(running, completed)
    for status, label in (("failed", "异常"), ("interrupted", "已停止"), ("unknown", "待同步")):
        count = sum(row["status"] == status for row in rows)
        if count:
            summary += " · {} {}".format(count, label)
    lines = []
    for row in rows:
        name = row["agent_path"].rstrip("/").rsplit("/", 1)[-1] or row["thread_id"]
        name = _clean_text(" ".join(name.split()), 80) or row["thread_id"]
        label = "状态待同步" if row["status"] == "unknown" else _STATUS_LABELS[row["status"]]
        lines.append("{} · {}".format(name, label))
    return _collapsible_digest(
        "子 Agent 状态 · " + summary,
        "收起子 Agent 状态 · " + summary,
        [{
            "tag": "div",
            "text": {"tag": "plain_text", "content": "\n".join(lines), "text_align": "left"},
            "text_size": "normal",
        }],
    )


def _workspace_shell(
    elements: Iterable[Dict[str, Any]],
    *,
    padding: str = "18px 20px 18px 20px",
) -> Dict[str, Any]:
    return {
        "tag": "interactive_container",
        "behaviors": [],
        "width": "fill",
        "height": "auto",
        # 外部圆角与边框交给飞书消息气泡，避免两层轮廓在四角重叠。
        "corner_radius": "0px",
        "has_border": False,
        "disabled": False,
        "background_style": "codex_canvas",
        "padding": padding,
        "direction": "vertical",
        "horizontal_spacing": "12px",
        "vertical_spacing": "12px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "elements": list(elements),
    }


def _paper_sides(content: Dict[str, Any]) -> Dict[str, Any]:
    """用两侧各 2px 的非交互底衬模拟淡阴影，原生 form 必须留在外层。"""
    return {
        "tag": "interactive_container",
        "behaviors": [],
        "width": "fill",
        "height": "auto",
        "corner_radius": "0px",
        "has_border": False,
        "background_style": "codex_paper_edge",
        "padding": "0px 2px 0px 2px",
        "direction": "vertical",
        "horizontal_spacing": "0px",
        "vertical_spacing": "0px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "elements": [content],
    }


def _paper_edge() -> Dict[str, Any]:
    """用合法列内边距呈现轻薄下沿；不是 CSS 模糊阴影，也不增加正文嵌套。"""
    return {
        "tag": "column_set",
        "element_id": "codex_paper_edge",
        "flex_mode": "none",
        "background_style": "default",
        "horizontal_spacing": "0px",
        "margin": "0px 0px 0px 0px",
        "columns": [{
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "background_style": "codex_paper_edge",
            "padding": "0px 0px 3px 0px",
            "vertical_spacing": "0px",
            "elements": [],
        }],
    }


def _workspace_divider(*, bottom_spacing: int = 0) -> Dict[str, Any]:
    """让分隔线留白也使用纸面色，不在顶层 form 两侧留下白色横带。"""
    divider = {
        "tag": "column_set",
        "background_style": "codex_canvas",
        "flex_mode": "none",
        "horizontal_spacing": "0px",
        "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": "codex_canvas",
            "padding": f"0px 20px {bottom_spacing}px 20px",
            "vertical_spacing": "0px",
            "elements": [{"tag": "hr", "margin": "0px 0px 0px 0px"}],
        }],
    }
    # 分栏背景原生带小圆角；用平直同色纸面补齐，避免左右底衬在这里出现弧形缺口。
    return _workspace_shell([divider], padding="0px 0px 0px 0px")


def _theme_control(label: str, value: Dict[str, Any], *, primary: bool = False) -> Dict[str, Any]:
    return {
        "tag": "interactive_container",
        "behaviors": [{"type": "callback", "value": value}],
        "width": "fill",
        "height": "auto",
        "corner_radius": "12px",
        "has_border": True,
        "border_color": "codex_button_border" if primary else "codex_secondary",
        "disabled": False,
        "background_style": "codex_button" if primary else "codex_button_secondary",
        "padding": "8px 12px 8px 12px",
        "direction": "vertical",
        "horizontal_spacing": "0px",
        "vertical_spacing": "0px",
        "horizontal_align": "center",
        "vertical_align": "top",
        "elements": [_markdown(
            "<font color='{}'>{}</font>".format(
                "codex_button_text" if primary else "codex_ink",
                label,
            ),
            margin="0px 0px 0px 0px",
        )],
    }


def _theme_control_row(
    controls: Iterable[Dict[str, Any]],
    *,
    margin: str = "20px 0px 0px 0px",
) -> Dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [control],
            }
            for control in controls
        ],
        "margin": margin,
    }


def _workspace_pair(
    output_elements: Iterable[Dict[str, Any]],
    context_elements: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_surface(
                    list(output_elements),
                    background="codex_panel",
                    margin="0px 0px 0px 0px",
                    padding="18px 16px 18px 16px",
                )],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_surface(
                    list(context_elements),
                    background="codex_body",
                    margin="0px 0px 0px 0px",
                    padding="18px 16px 18px 16px",
                )],
            },
        ],
        "margin": "0px 0px 0px 0px",
    }


def _button_icon(value: Mapping[str, Any]) -> Optional[str]:
    action = value.get("action")
    if action == "menu_open":
        return "menu_outlined"
    if action == "desktop_interrupt":
        return "stop_outlined"
    if action in {"desktop_detach", "list_detach", "stream_detach"}:
        return "logout_outlined"
    if action in {"desktop_attach", "stream_reconnect"}:
        return "arrow_outlined"
    if action == "desktop_unarchive":
        return "archive_outlined"
    if action == "desktop_approval":
        return "check_outlined" if value.get("decision") == "accept" else "close_outlined"
    if action == "desktop_turn_page":
        return "history_outlined"
    return None


def _button(label: str, button_type: str, value: Dict[str, Any]) -> Dict[str, Any]:
    if button_type == "primary":
        return _theme_control(label, value, primary=True)
    visual_type = {
        "danger": "danger_filled",
    }.get(button_type, button_type)
    result: Dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label, "text_align": "left"},
        "type": visual_type,
        "size": "medium",
        "width": "fill",
        "behaviors": [{"type": "callback", "value": value}],
    }
    icon = _button_icon(value)
    if icon:
        result["icon"] = {"tag": "standard_icon", "token": icon}
    return result


def _button_row(
    buttons: Iterable[Dict[str, Any]],
    *,
    margin: str = "12px 20px 0px 20px",
) -> Dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [button],
            }
            for button in buttons
        ],
        "margin": margin,
    }


def _disabled_button(label: str) -> Dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label, "text_align": "left"},
        "type": "default",
        "size": "medium",
        "width": "fill",
        "disabled": True,
    }


def _desktop_input_form(thread_id: str, *, historical: bool) -> Dict[str, Any]:
    heading = "继续当前任务" if historical else "继续对话"
    intro = (
        "**{}**\n<font color='codex_muted'>输入内容会发送到当前实时任务，而不是改写正在查看的历史轮次。</font>".format(
            heading
        )
        if historical
        else "**{}**".format(heading)
    )
    form = {
        "tag": "form",
        "name": "desktop_input",
        "direction": "vertical",
        "horizontal_spacing": "8px",
        "vertical_spacing": "8px",
        "horizontal_align": "left",
        "vertical_align": "top",
        "padding": "16px 20px 18px 20px",
        "margin": "0px 0px 0px 0px",
        "elements": [
            _markdown(intro),
            {
                "tag": "input",
                "name": "desktop_command__{}".format(thread_id),
                "input_type": "multiline_text",
                "rows": 2,
                "max_rows": 4,
                "auto_resize": True,
                "label": {
                    "tag": "plain_text",
                    "content": "给 Codex 发消息",
                    "text_align": "left",
                },
                "label_position": "top",
                "placeholder": {
                    "tag": "plain_text",
                    "content": "输入下一条指令",
                    "text_align": "left",
                },
                "default_value": "",
                "max_length": 1000,
                "width": "fill",
                "required": False,
                "margin": "6px 0px 0px 0px",
            },
            {
                "tag": "button",
                "name": "desktop_send",
                "text": {
                    "tag": "plain_text",
                    "content": "发送指令",
                    "text_align": "left",
                },
                "icon": {"tag": "standard_icon", "token": "send_outlined"},
                "type": "default",
                "size": "medium",
                "width": "fill",
                "action_type": "form_submit",
                "form_action_type": "submit",
                "form_name": "desktop_input",
            },
        ],
    }
    # Form 不支持背景色且必须保持顶层；只给内部 Composer 内容铺纸面。
    canvas = _workspace_shell(form["elements"], padding=form["padding"])
    canvas["horizontal_spacing"] = form["horizontal_spacing"]
    canvas["vertical_spacing"] = form["vertical_spacing"]
    form["elements"] = [_paper_sides(canvas)]
    form["padding"] = "0px 0px 0px 0px"
    return form


def build_desktop_card(
    state: Any,
    selected_turn_id: Optional[str] = None,
    image_keys: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build one updateable CardKit 2.0 card from normalized public state."""
    if not isinstance(state, Mapping) or state.get("schema_version") != NORMALIZED_VERSION:
        state = _empty_state()

    status = state.get("status") if state.get("status") in _STATUS_LABELS else "unknown"
    title = _clean_text(state.get("title"), 120) or "Codex Desktop"
    thread_id = _identifier(state.get("thread_id")) or ""
    image_keys = image_keys if isinstance(image_keys, Mapping) else {}
    rendered_image_sources: set[str] = set()
    public_turns = extract_public_turns(state)
    selected_index: Optional[int] = None
    selected_turn: Optional[Dict[str, Any]] = None
    if public_turns:
        selected_index = len(public_turns) - 1
        requested_turn_id = _identifier(selected_turn_id)
        if requested_turn_id:
            for index, turn in enumerate(public_turns):
                if turn["turn_id"] == requested_turn_id:
                    selected_index = index
                    break
        selected_turn = public_turns[selected_index]
    viewing_latest_turn = (
        selected_index is None or selected_index == len(public_turns) - 1
    )
    display_status = status
    if not viewing_latest_turn and selected_turn is not None:
        historical_status = selected_turn.get("status")
        if historical_status in _STATUS_LABELS:
            display_status = historical_status
    historical = not viewing_latest_turn
    subtitle = "Codex Desktop · {}".format(
        "历史第 {}/{} 轮".format(selected_index + 1, len(public_turns))
        if historical and selected_index is not None
        else "实时同步"
    )
    elements: List[Dict[str, Any]] = _heading(
        title,
        subtitle,
        _STATUS_LABELS[display_status],
        status=display_status,
    )
    turn_label = (
        "第 {}/{} 轮".format(selected_index + 1, len(public_turns))
        if selected_index is not None
        else ""
    )
    pending_insert_index = len(elements)

    if selected_turn is not None:
        user_messages = selected_turn["user_messages"]
        if user_messages:
            query_parts: List[str] = []
            for message in user_messages:
                prefix = "补充指令：" if message["kind"] == "steering" else ""
                query_parts.append(prefix + message["text"])
            query = _clean_text("\n\n".join(query_parts), MAX_CARD_QUERY_CHARS) or ""
            elements.append(_message_surface([
                _markdown("<font color='codex_muted'>你</font>", text_size="notation"),
                _markdown("**{}**".format(query), margin="6px 0px 0px 0px"),
            ]))

        elements.append({"tag": "hr", "margin": "18px 0px 0px 0px"})

        messages = selected_turn["agent_messages"]
        if messages:
            message = messages[-1]
            text = _clean_text(message["text"], MAX_CARD_MESSAGE_CHARS) or ""
            elements.extend([
                _markdown("<font color='codex_muted'>Codex</font>", text_size="notation",
                          margin="16px 0px 0px 0px"),
                _markdown(text, margin="8px 0px 0px 0px"),
            ])
            elements.extend(_message_image_elements(
                message, image_keys, rendered_image_sources
            ))
        else:
            elements.extend([
                _markdown("<font color='codex_muted'>Codex</font>", text_size="notation",
                          margin="16px 0px 0px 0px"),
                _markdown("**Codex 正在处理**\n\n等待该轮公开进度…",
                          margin="8px 0px 0px 0px"),
            ])

        older_messages = messages[-MAX_CARD_MESSAGES:-1]
        if older_messages:
            older_elements: List[Dict[str, Any]] = []
            for message in older_messages:
                label = "Codex 回复" if message["phase"] == "final_answer" else "执行进度"
                text = _clean_text(message["text"], MAX_CARD_MESSAGE_CHARS) or ""
                older_elements.append(_markdown("**{}**\n{}".format(label, text)))
                older_elements.extend(_message_image_elements(
                    message, image_keys, rendered_image_sources
                ))
            elements.append(_collapsible_digest(
                "较早进度（{}项）".format(len(older_messages)),
                "收起较早进度（{}项）".format(len(older_messages)),
                older_elements,
            ))

        if selected_turn.get("sub_agents"):
            elements.append(_sub_agents_panel(selected_turn["sub_agents"]))

        if len(public_turns) > 1 and selected_index is not None:
            previous_button: Dict[str, Any]
            next_button: Dict[str, Any]
            if selected_index == 0:
                previous_button = _disabled_button("前一轮")
            else:
                previous_button = _button("前一轮", "default", {
                    "action": "desktop_turn_page",
                    "thread_id": thread_id,
                    "target_turn_id": public_turns[selected_index - 1]["turn_id"],
                })
            if selected_index >= len(public_turns) - 1:
                next_button = _disabled_button("后一轮")
            else:
                next_button = _button("后一轮", "default", {
                    "action": "desktop_turn_page",
                    "thread_id": thread_id,
                    "target_turn_id": public_turns[selected_index + 1]["turn_id"],
                })
            elements.append({
                "tag": "column_set",
                "flex_mode": "stretch",
                "background_style": "default",
                "horizontal_spacing": "8px",
                "horizontal_align": "left",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "vertical_align": "top", "elements": [previous_button]},
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "vertical_align": "center", "elements": [
                         {
                             "tag": "markdown",
                             "content": turn_label,
                             "text_align": "center",
                             "text_size": "notation",
                         }
                     ]},
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "vertical_align": "top", "elements": [next_button]},
                ],
                "margin": "12px 0px 0px 0px",
            })
    else:
        # Compatibility for rollout-derived or older normalized state that only
        # has the original flat ``messages`` field.
        messages = extract_public_events(state)
        legacy_older_digest: Optional[Dict[str, Any]] = None
        if messages:
            message = messages[-1]
            text = _clean_text(message["text"], MAX_CARD_MESSAGE_CHARS) or ""
            elements.extend([
                _markdown("<font color='codex_muted'>Codex</font>", text_size="notation",
                          margin="16px 0px 0px 0px"),
                _markdown(text, margin="8px 0px 0px 0px"),
            ])
            elements.extend(_message_image_elements(
                message, image_keys, rendered_image_sources
            ))
            older_messages = messages[-MAX_CARD_MESSAGES:-1]
            if older_messages:
                older_elements: List[Dict[str, Any]] = []
                for message in older_messages:
                    label = "Codex 回复" if message["phase"] == "final_answer" else "执行进度"
                    text = _clean_text(message["text"], MAX_CARD_MESSAGE_CHARS) or ""
                    older_elements.append(_markdown("**{}**\n{}".format(label, text)))
                    older_elements.extend(_message_image_elements(
                        message, image_keys, rendered_image_sources
                    ))
                legacy_older_digest = _collapsible_digest(
                    "较早进度（{}项）".format(len(older_messages)),
                    "收起较早进度（{}项）".format(len(older_messages)),
                    older_elements,
                )
        else:
            elements.extend([
                _markdown("<font color='codex_muted'>Codex</font>", text_size="notation",
                          margin="16px 0px 0px 0px"),
                _markdown("**Codex 正在处理**\n\n等待公开进度…",
                          margin="8px 0px 0px 0px"),
            ])
        if legacy_older_digest is not None:
            elements.append(legacy_older_digest)

    pending = state.get("pending")
    if viewing_latest_turn and isinstance(pending, Mapping):
        request_id = _wire_identifier(pending.get("request_id"))
        kind = _identifier(pending.get("request_kind")) or "unknown"
        prompt = _clean_text(pending.get("prompt"), 600) or "需要处理"
        pending_title = _clean_text(pending.get("title"), 120) or "待处理"
        pending_elements: List[Dict[str, Any]] = [
            _markdown("<font color='codex_ink'>**{}**</font>\n{}".format(pending_title, prompt)),
        ]
        details = _clean_text(pending.get("details"), 1000)
        if details:
            pending_elements.append(_markdown(details))
        if request_id is not None and pending.get("kind") == "approval":
            base = {
                "action": "desktop_approval",
                "thread_id": thread_id,
                "request_id": request_id,
                "kind": kind,
            }
            buttons = []
            if pending.get("allow_remote") is not False:
                buttons.append(_theme_control(
                    "允许",
                    dict(base, decision="accept"),
                    primary=True,
                ))
            buttons.append(_theme_control("拒绝", dict(base, decision="decline")))
            pending_elements.append(_theme_control_row(buttons))
            if pending.get("allow_remote") is False:
                pending_elements.append(_markdown(
                    "<font color='codex_muted'>文件变更详情请在 Codex Desktop 中确认；飞书仅支持拒绝。</font>"
                ))
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
                    buttons.append(_theme_control(label, {
                        "action": "desktop_input",
                        "thread_id": thread_id,
                        "request_id": request_id,
                        "kind": kind,
                        "question_id": _identifier(pending.get("question_id")) or "",
                        "answer": label,
                    }))
                if buttons:
                    pending_elements.append(_theme_control_row(buttons))
        elements.insert(
            pending_insert_index,
            _surface(pending_elements, background="codex_body"),
        )

    input_form: Optional[Dict[str, Any]] = None
    secondary_elements: List[Dict[str, Any]] = []
    if thread_id:
        input_form = _desktop_input_form(thread_id, historical=historical)
        controls = [
            _theme_control("打开菜单", {"action": "menu_open"}, primary=True),
            _theme_control("断开任务", {
                "action": "desktop_detach",
                "thread_id": thread_id,
            }),
        ]
        if (
            viewing_latest_turn
            and status in {"running", "waiting_approval", "waiting_input"}
            and state.get("active_turn_id") is not None
        ):
            controls.insert(1, _theme_control("停止任务", {
                "action": "desktop_interrupt",
                "thread_id": thread_id,
                "turn_id": state.get("active_turn_id"),
            }))
        secondary_elements.extend([
            _theme_control_row(controls, margin="0px 0px 0px 0px"),
            _footer("IM Agent Bridge · Codex Desktop", margin="6px 0px 0px 0px"),
        ])
    else:
        elements.append(_footer("IM Agent Bridge · Codex Desktop"))

    # Construct the result field-by-field.  Never merge the normalized state
    # into it: the private `_conversation_state` may contain sensitive data.
    return {
        "schema": "2.0",
        "config": _workspace_config(_summary_content(title, _STATUS_LABELS[display_status])),
        "body": {
            "direction": "vertical",
            "horizontal_spacing": "0px",
            "vertical_spacing": "0px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "padding": "0px 0px 0px 0px",
            "elements": (
                [_paper_sides(_workspace_shell(elements))]
                + ([
                    _paper_sides(_workspace_divider()),
                    input_form,
                ] if input_form else [])
                + ([
                    _paper_sides(_workspace_divider(bottom_spacing=12)),
                    _paper_sides(_workspace_shell(
                        secondary_elements,
                        padding="0px 20px 16px 20px",
                    ))
                ] if secondary_elements else [])
                + [_paper_edge()]
            ),
        },
    }


def build_desktop_list_card(
    threads: Any,
    current_thread_id: Optional[str] = None,
    page: int = 0,
    archived: bool = False,
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

    heading_title = "Codex Desktop 已归档" if archived else "Codex Desktop"
    heading_tag = "已归档" if archived else "任务列表"
    elements.extend(_heading(
        heading_title,
        "{} · 第 {}/{} 页 · 共 {} 个".format(
            "选择已归档任务" if archived else "选择最近任务",
            page + 1,
            total_pages,
            total,
        ),
        heading_tag,
    ))
    elements.append(_highlight_grid(
        "TASKS",
        "{} 个".format(total),
        "已归档任务" if archived else "最近 Desktop 任务",
        "PAGE",
        "{}/{}".format(page + 1, total_pages),
        "选择任务后进入 Workspace",
    ))

    for thread, thread_id in valid_threads[start:start + DESKTOP_LIST_PAGE_SIZE]:
        title = _clean_text(thread.get("title"), 160) or "Codex Desktop 任务"
        cwd = _clean_text(thread.get("cwd"), 240) or ""
        project_name = _clean_text(thread.get("project_name"), 120)
        if not project_name and cwd:
            project_name = cwd.rstrip("/").rsplit("/", 1)[-1]
        updated_at = format_beijing_time(thread.get("updated_at"))
        is_current = thread_id == current_thread_id
        thread_status = thread.get("status")
        if thread_status not in _STATUS_LABELS:
            thread_status = "unknown"
        status_label = _STATUS_LABELS[thread_status]
        title_line = "**{}**".format(project_name or title)
        status_line = status_label
        if is_current:
            status_line += " · 当前任务"
        details = [
            "<font color='codex_muted'>{}</font>".format(status_line),
            title_line,
        ]
        if project_name:
            details.append(f"Session：**{title}**")
        details.append(f"Session ID：`{thread_id}`")
        if cwd:
            details.append(f"目录：`{cwd}`")
        if updated_at:
            details.append(f"<font color='grey'>更新：{updated_at}</font>")
        if archived:
            buttons = [
                _theme_control("恢复并进入", {
                    "action": "desktop_attach",
                    "thread_id": thread_id,
                }, primary=True),
                _theme_control("移出归档", {
                    "action": "desktop_unarchive",
                    "thread_id": thread_id,
                    "page": page,
                }),
            ]
        else:
            action = "desktop_detach" if is_current else "desktop_attach"
            label = "断开任务" if is_current else "进入任务"
            buttons = [_theme_control(label, {
                "action": action,
                "thread_id": thread_id,
            }, primary=not is_current)]
        elements.append(_surface([
            _markdown("\n".join(details)),
            _theme_control_row(buttons),
        ], margin="8px 0px 0px 0px"))
    if not valid_threads:
        elements.append(_surface([
            _markdown(
                "**没有找到{}任务**\n<font color='grey'>{}</font>".format(
                    "已归档" if archived else "本机 Codex Desktop",
                    "归档任务会在这里显示。"
                    if archived
                    else "请确认 Codex Desktop 已启动并至少创建过一个任务。",
                )
            )
        ]))
    elif total_pages > 1:
        page_action = "desktop_archived_list_page" if archived else "desktop_list_page"
        if page == 0:
            previous_button = _disabled_button("上一页")
        else:
            previous_button = _button("上一页", "default", {
                "action": page_action,
                "page": page - 1,
            })
        if page >= total_pages - 1:
            next_button = _disabled_button("下一页")
        else:
            next_button = _button("下一页", "default", {
                "action": page_action,
                "page": page + 1,
            })
        elements.append({
            "tag": "column_set",
            "flex_mode": "stretch",
            "background_style": "default",
            "horizontal_spacing": "8px",
            "horizontal_align": "left",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "vertical_align": "top", "elements": [previous_button]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "vertical_align": "center", "elements": [
                     _markdown(f"第 {page + 1}/{total_pages} 页 · 共 {total} 个")
                 ]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "vertical_align": "top", "elements": [next_button]},
            ],
            "margin": "12px 0px 0px 0px",
        })
    elements.append(_footer("IM Agent Bridge · Codex Desktop {}".format(
        "归档任务" if archived else "任务列表"
    )))
    return {
        "schema": "2.0",
        "config": _workspace_config(
            "Codex Desktop：{}，共 {} 个任务".format(
                "已归档任务" if archived else "最近任务",
                total,
            )
        ),
        "body": {
            "direction": "vertical",
            "horizontal_spacing": "0px",
            "vertical_spacing": "0px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "padding": "0px 0px 0px 0px",
            "elements": [_paper_sides(_workspace_shell(elements)), _paper_edge()],
        },
    }


def build_desktop_completion_card(event: Any) -> Dict[str, Any]:
    """Build a standalone completion notification with a reconnect action."""

    event = event if isinstance(event, Mapping) else {}
    raw_thread_id = event.get("thread_id")
    thread_id = raw_thread_id.strip() if isinstance(raw_thread_id, str) else ""
    title = _clean_text(event.get("title"), 160) or "Codex Desktop 任务"
    project_name = _clean_text(event.get("project_name"), 120)
    outcome = "failed" if event.get("outcome") == "failed" else "completed"
    failed = outcome == "failed"
    status_text = "执行失败" if failed else "执行完成"
    elements: List[Dict[str, Any]] = _heading(
        "Codex Desktop",
        "任务完成提醒",
        status_text,
        status=outcome,
    )
    elements.append(_highlight_grid(
        "RESULT",
        status_text,
        "查看公开回复后决定是否继续。" if failed else "当前轮次已经完成。",
        "NEXT",
        "重新连接" if thread_id else "无法重新连接",
        "点击此处进入原任务并继续发送指令。" if thread_id else "缺少 Session ID，无法定位原任务。",
        right_action={
            "action": "desktop_attach",
            "thread_id": thread_id,
        } if thread_id else None,
        left_status=outcome,
    ))
    details = []
    if project_name:
        details.append(f"**{project_name}**")
    details.append(f"Session：**{title}**")
    if thread_id:
        details.append(f"Session ID：`{thread_id}`")
    completed_at = format_beijing_time(event.get("completed_at"))
    if completed_at:
        details.append(f"<font color='grey'>时间：{completed_at}</font>")
    elements.append(_surface([_markdown("\n".join(details))]))
    elements.append(_footer("IM Agent Bridge · Codex Desktop 完成提醒"))
    return {
        "schema": "2.0",
        "config": _workspace_config("Codex Desktop：{}，{}".format(title, status_text)),
        "body": {
            "direction": "vertical",
            "horizontal_spacing": "0px",
            "vertical_spacing": "0px",
            "horizontal_align": "left",
            "vertical_align": "top",
            "padding": "0px 0px 0px 0px",
            "elements": [_paper_sides(_workspace_shell(elements)), _paper_edge()],
        },
    }


__all__ = [
    "MAX_PUBLIC_SUB_AGENTS",
    "PatchApplyError",
    "apply_immer_patches",
    "build_desktop_card",
    "build_desktop_completion_card",
    "build_desktop_list_card",
    "extract_card_image_sources",
    "extract_public_events",
    "extract_public_turns",
    "normalize_conversation_state",
    "normalize_desktop_update",
    "normalize_patch_only_update",
    "project_subagent_activity",
]
