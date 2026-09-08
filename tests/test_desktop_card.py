import copy
import json

import pytest

from lark_client.card_theme import soft_color_tokens
from lark_client.desktop_card import (
    MAX_PUBLIC_SUB_AGENTS,
    PatchApplyError,
    apply_immer_patches,
    build_desktop_card,
    build_desktop_completion_card,
    build_desktop_list_card,
    extract_card_image_sources,
    extract_public_events,
    extract_public_turns,
    normalize_conversation_state,
    normalize_desktop_update,
    normalize_patch_only_update,
    project_subagent_activity,
)


def _walk_card(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_card(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_card(child)


def _callback_values(card, action=None):
    values = []
    for node in _walk_card(card):
        behaviors = node.get("behaviors")
        if not isinstance(behaviors, list):
            continue
        for behavior in behaviors:
            value = behavior.get("value") if isinstance(behavior, dict) else None
            if not isinstance(value, dict):
                continue
            if action is None or value.get("action") == action:
                values.append(value)
    return values


def _buttons(card, label=None):
    result = []
    for node in _walk_card(card):
        if node.get("tag") != "button":
            continue
        text = node.get("text")
        content = text.get("content") if isinstance(text, dict) else None
        if label is None or content == label:
            result.append(node)
    return result


def _status_badge(card):
    badges = [
        node for node in _walk_card(card)
        if node.get("tag") == "interactive_container"
        and node.get("corner_radius") == "999px"
    ]
    assert len(badges) == 1
    return badges[0]


def _assert_status_badge(card, label, tone):
    badge = _status_badge(card)
    assert badge["background_style"] == f"codex_status_{tone}_bg"
    assert badge["elements"][0]["content"] == (
        f"<font color='codex_status_{tone}_text'>{label}</font>"
    )
    assert badge["behaviors"] == []
    assert _callback_values(badge) == []


def _callback_control(card, value):
    controls = [
        node for node in _walk_card(card)
        if node.get("behaviors") == [{"type": "callback", "value": value}]
    ]
    assert len(controls) == 1
    return controls[0]


def _assert_soft_primary(control, label):
    assert control["tag"] == "interactive_container"
    assert control["background_style"] == "codex_button"
    assert control["border_color"] == "codex_button_border"
    assert control["elements"][0]["content"] == (
        f"<font color='codex_button_text'>{label}</font>"
    )


def _highlight(card, label):
    highlights = [
        node for node in _walk_card(card)
        if node.get("tag") == "interactive_container"
        and any(
            child.get("tag") == "markdown"
            and child.get("content", "").endswith(f">{label}</font>")
            for child in node.get("elements", [])
        )
    ]
    assert len(highlights) == 1
    return highlights[0]


def _snapshot():
    return {
        "id": "thread-1",
        "title": "Desktop 会话",
        "threadRuntimeStatus": {"type": "active", "activeFlags": []},
        "turns": [
            {
                "turnId": "turn-1",
                "status": "inProgress",
                "items": [
                    {
                        "id": "reasoning-1",
                        "type": "reasoning",
                        "summary": ["PRIVATE_REASONING"],
                        "content": ["RAW_CHAIN_OF_THOUGHT"],
                    },
                    {
                        "id": "tool-1",
                        "type": "commandExecution",
                        "command": "print-secret",
                        "aggregatedOutput": "PRIVATE_TOOL_OUTPUT",
                    },
                    {
                        "id": "agent-1",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "正在检查项目结构。",
                    },
                ],
            }
        ],
        "requests": [],
    }


def _event(change):
    return {
        "type": "thread-stream-state-changed",
        "params": {
            "conversationId": "thread-1",
            "hostId": "host-1",
            "change": change,
        },
    }


def _sub_agent_item(thread_id="child-1", kind="started", agent_path="/root/review"):
    return {
        "id": "activity-{}-{}".format(thread_id, kind),
        "type": "subAgentActivity",
        "kind": kind,
        "agentThreadId": thread_id,
        "agentPath": agent_path,
        "prompt": "SECRET_CHILD_PROMPT",
        "tool": {"output": "SECRET_CHILD_TOOL_OUTPUT"},
        "reasoning": "SECRET_CHILD_REASONING",
    }


@pytest.mark.parametrize(("kind", "status"), [
    ("started", "running"), ("completed", "completed"), ("interacted", "unknown"),
])
def test_project_subagent_activity_exposes_only_known_metadata(kind, status):
    row = project_subagent_activity(_sub_agent_item(kind=kind))
    assert row == {"thread_id": "child-1", "agent_path": "/root/review", "status": status}


@pytest.mark.parametrize("fields", [
    {"type": "reasoning"}, {"kind": "SECRET_UNKNOWN_KIND"}, {"kind": {}},
    {"agentThreadId": ""}, {"agentThreadId": None}, {"agentThreadId": ["child-1"]},
])
def test_project_subagent_activity_rejects_unknown_or_invalid_metadata(fields):
    assert project_subagent_activity(dict(_sub_agent_item(), **fields)) is None


def test_snapshot_sub_agents_are_bounded_deduplicated_and_do_not_complete_parent():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].extend([
        _sub_agent_item(),
        _sub_agent_item(kind="completed"),
        _sub_agent_item("child-2", "started", "/root/tests"),
        {"type": "subAgentActivity", "agentThreadId": "unknown-child", "kind": "unknown"},
    ])
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    assert normalized["turns"][0]["sub_agents"] == [
        {"thread_id": "child-1", "agent_path": "/root/review", "status": "completed"},
        {"thread_id": "child-2", "agent_path": "/root/tests", "status": "running"},
    ]
    assert normalized["status"] == "running"
    assert normalized["active_turn_id"] == "turn-1"
    rendered = json.dumps(normalized, ensure_ascii=False)
    for secret in ("SECRET_CHILD_PROMPT", "SECRET_CHILD_TOOL_OUTPUT", "SECRET_CHILD_REASONING"):
        assert secret not in rendered
    card = build_desktop_card(normalized)
    assert card["config"]["summary"]["content"].endswith("运行中")
    panel = next(node for node in _walk_card(card) if node.get("tag") == "collapsible_panel")
    assert panel["header"]["title"]["content"] == "子 Agent 状态 · 1 运行中 · 1 已完成"
    assert panel["header"]["icon"] == {
        "tag": "standard_icon", "token": "down_outlined", "color": "grey",
    }
    assert panel["header"]["icon_position"] == "right"
    assert panel["header"]["icon_expanded_angle"] == -180
    assert panel["elements"][0]["text"]["content"] == "review · 已完成\ntests · 运行中"
    assert "/root/" not in json.dumps(panel, ensure_ascii=False)
    assert _callback_values(card, "desktop_interrupt") == [{
        "action": "desktop_interrupt", "thread_id": "thread-1", "turn_id": "turn-1",
    }]

    snapshot["turns"][0]["items"] = [
        _sub_agent_item("child-{}".format(index))
        for index in range(MAX_PUBLIC_SUB_AGENTS + 10)
    ]
    rows = normalize_conversation_state(snapshot, retain_raw=False)["turns"][0]["sub_agents"]
    assert len(rows) == MAX_PUBLIC_SUB_AGENTS
    assert rows[0]["thread_id"] == "child-10"


def test_sub_agents_interacted_awaits_status_sync_instead_of_guessing_restart():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].extend([
        _sub_agent_item(), _sub_agent_item(kind="completed"),
        _sub_agent_item(kind="interacted"),
    ])
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    assert normalized["turns"][0]["sub_agents"][0]["status"] == "unknown"
    assert "review · 状态待同步" in json.dumps(build_desktop_card(normalized), ensure_ascii=False)


@pytest.mark.parametrize("status", ["failed", "interrupted", "unknown"])
def test_normalized_sub_agent_lifecycle_status_and_extra_fields_are_sanitized(status):
    normalized = normalize_conversation_state(_snapshot(), retain_raw=False)
    normalized["turns"][0]["sub_agents"] = [{
        "thread_id": "child-1", "agent_path": "/root/review", "status": status,
        "prompt": "SECRET_NORMALIZED_PROMPT", "reasoning": "SECRET_NORMALIZED_REASONING",
    }]
    rows = extract_public_turns(normalized)[0]["sub_agents"]
    assert rows == [{"thread_id": "child-1", "agent_path": "/root/review", "status": status}]
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "SECRET_NORMALIZED" not in rendered
    assert {"failed": "review · 异常", "interrupted": "review · 已停止", "unknown": "review · 状态待同步"}[status] in rendered


@pytest.mark.parametrize("child_status", ["running", "completed", "failed", "interrupted", "unknown"])
def test_sub_agent_status_never_changes_parent_badge_or_stop_callback(child_status):
    normalized = normalize_conversation_state(_snapshot(), retain_raw=False)
    normalized["turns"][0]["sub_agents"] = [{
        "thread_id": "child-1", "agent_path": "/root/review", "status": child_status,
    }]
    before = copy.deepcopy(normalized)
    card = build_desktop_card(normalized)

    _assert_status_badge(card, "运行中", "running")
    assert normalized == before
    assert normalized["status"] == "running"
    assert normalized["pending"] is None
    assert card["config"]["summary"]["content"].endswith("运行中")
    assert _callback_values(card, "desktop_interrupt") == [{
        "action": "desktop_interrupt", "thread_id": "thread-1", "turn_id": "turn-1",
    }]


@pytest.mark.parametrize("patch_only", [False, True])
def test_sub_agent_patch_add_kind_update_and_remove_preserve_parent_state(patch_only):
    current = normalize_conversation_state(_snapshot(), retain_raw=not patch_only)
    current["revision"] = 0
    update = normalize_patch_only_update if patch_only else normalize_desktop_update
    added = update(current, _event({"type": "patches", "baseRevision": 0, "revision": 1, "patches": [{
        "op": "add", "path": ["turns", 0, "items", 3], "value": _sub_agent_item(),
    }]}))
    assert added["turns"][0]["sub_agents"][0]["status"] == "running"
    completed = update(added, _event({"type": "patches", "baseRevision": 1, "revision": 2, "patches": [
        {"op": "replace", "path": ["turns", 0, "items", 3, "kind"], "value": "completed"},
        {"op": "add", "path": ["turns", 0, "items", 3, "prompt"], "value": "SECRET_PATCH_PROMPT"},
    ]}))
    assert completed["turns"][0]["sub_agents"][0]["status"] == "completed"
    assert completed["status"] == "running"
    assert completed["active_turn_id"] == "turn-1"
    assert completed["turns"][0]["status"] == "running"
    assert "SECRET_PATCH_PROMPT" not in json.dumps(build_desktop_card(completed), ensure_ascii=False)
    if patch_only:
        assert "SECRET_PATCH_PROMPT" not in json.dumps(completed, ensure_ascii=False)
    removed = update(completed, _event({"type": "patches", "baseRevision": 2, "revision": 3, "patches": [{
        "op": "remove", "path": ["turns", 0, "items", 3],
    }]}))
    assert "sub_agents" not in removed["turns"][0]
    assert "子 Agent 状态" not in json.dumps(build_desktop_card(removed), ensure_ascii=False)


def test_sub_agent_patch_updates_use_item_order_and_preserve_other_agents():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].extend([
        _sub_agent_item(), _sub_agent_item(kind="completed"),
        _sub_agent_item("child-2", "started", "/root/tests"),
    ])
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    changed = normalize_patch_only_update(normalized, _event({"type": "patches", "patches": [{
        "op": "replace", "path": ["turns", 0, "items", 3, "kind"], "value": "interacted",
    }]}))
    rows = {row["thread_id"]: row for row in changed["turns"][0]["sub_agents"]}
    assert len(rows) == 2
    assert rows["child-1"]["status"] == "completed"
    assert rows["child-2"]["status"] == "running"
    removed = normalize_patch_only_update(changed, _event({"type": "patches", "patches": [{
        "op": "remove", "path": ["turns", 0, "items", 4],
    }]}))
    rows = {row["thread_id"]: row for row in removed["turns"][0]["sub_agents"]}
    assert rows["child-1"]["status"] == "unknown"
    assert rows["child-2"]["status"] == "running"


def test_sub_agent_patch_only_array_indices_follow_insertions_and_removals():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].extend([
        _sub_agent_item(), _sub_agent_item("child-2", "started", "/root/tests"),
    ])
    current = normalize_conversation_state(snapshot, retain_raw=False)
    changed = normalize_patch_only_update(current, _event({"type": "patches", "patches": [
        {"op": "add", "path": ["turns", 0, "items", 3], "value": _sub_agent_item("child-3")},
        {"op": "replace", "path": ["turns", 0, "items", 5, "kind"], "value": "completed"},
        {"op": "remove", "path": ["turns", 0, "items", 4]},
        {"op": "replace", "path": ["turns", 0, "items", 4, "kind"], "value": "interacted"},
    ]}))
    rows = {row["thread_id"]: row for row in changed["turns"][0]["sub_agents"]}
    assert set(rows) == {"child-2", "child-3"}
    assert rows["child-2"]["status"] == "unknown"
    assert rows["child-3"]["status"] == "running"
    assert changed["status"] == "running"


def test_canonical_sub_agent_history_and_live_turns_remain_isolated():
    snapshot = _snapshot()
    snapshot["turnHistory"] = {"kind": "canonical", "history": {
        "islands": [{"entries": [{"value": "turn-history"}]}],
        "entitiesByKey": {"turn-history": {
            "turnId": "turn-history", "status": "completed",
            "items": [_sub_agent_item("history-child", "completed", "/root/history_review")],
        }},
    }}
    snapshot["turns"][0]["items"].append(_sub_agent_item("live-child", "started", "/root/live_tests"))
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    assert [turn["turn_id"] for turn in normalized["turns"]] == ["turn-history", "turn-1"]
    historical = json.dumps(build_desktop_card(normalized, selected_turn_id="turn-history"), ensure_ascii=False)
    live = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "history_review · 已完成" in historical and "live_tests" not in historical
    assert "live_tests · 运行中" in live and "history_review" not in live
    changed = normalize_patch_only_update(normalized, _event({"type": "patches", "patches": [{
        "op": "replace",
        "path": ["turnHistory", "history", "entitiesByKey", "turn-history", "items", 0, "kind"],
        "value": "interacted",
    }]}))
    assert changed["turns"][0]["sub_agents"][0]["status"] == "unknown"
    assert changed["turns"][1]["sub_agents"][0]["status"] == "running"
    assert changed["status"] == "running"
    shell = build_desktop_card(changed)["body"]["elements"][0]["elements"]
    panel_index = next(index for index, node in enumerate(shell) if node.get("tag") == "collapsible_panel")
    navigation_index = next(index for index, node in enumerate(shell) if _callback_values(node, "desktop_turn_page"))
    assert panel_index < navigation_index


def test_sub_agent_patch_only_whole_turn_replacement_drops_stale_activity_cache():
    current = normalize_conversation_state(_snapshot(), retain_raw=False)
    changed = normalize_patch_only_update(current, _event({"type": "patches", "patches": [{
        "op": "replace", "path": ["turns", 0], "value": {
            "turnId": "turn-1", "status": "inProgress", "items": [_sub_agent_item()],
        },
    }]}))
    assert changed["turns"][0]["sub_agents"][0]["status"] == "running"
    replaced = normalize_patch_only_update(changed, _event({"type": "patches", "patches": [{
        "op": "replace", "path": ["turns", 0], "value": {
            "turnId": "turn-1", "status": "inProgress", "items": [],
        },
    }]}))
    assert "sub_agents" not in replaced["turns"][0]
    assert replaced["_patch_items"] == {}


def test_snapshot_normalization_and_card_only_expose_public_agent_messages():
    normalized = normalize_desktop_update(
        None,
        _event({"type": "snapshot", "revision": 45, "conversationState": _snapshot()}),
    )

    assert normalized["schema_known"] is True
    assert normalized["thread_id"] == "thread-1"
    assert normalized["revision"] == 45
    assert normalized["status"] == "running"
    assert normalized["messages"] == [
        {
            "id": "agent-1",
            "turn_id": "turn-1",
            "phase": "commentary",
            "text": "正在检查项目结构。",
        }
    ]

    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "Desktop 会话" in rendered
    assert "正在检查项目结构" in rendered
    assert "PRIVATE_REASONING" not in rendered
    assert "RAW_CHAIN_OF_THOUGHT" not in rendered
    assert "PRIVATE_TOOL_OUTPUT" not in rendered
    assert "print-secret" not in rendered
    assert "_conversation_state" not in rendered


def test_public_markdown_strips_image_targets_and_local_file_links():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].insert(0, {
        "id": "user-local-link",
        "type": "userMessage",
        "content": [{
            "type": "text",
            "text": "查看 [配置文件](/Users/private/config.md)",
        }],
    })
    snapshot["turns"][0]["items"].append({
        "id": "agent-local-image",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": (
            "预览如下：\n"
            "![卡片预览](/Users/private/card-preview.png)\n"
            "[打开本地文件](file:///Users/private/report.md)"
        ),
    })

    card = build_desktop_card(normalize_conversation_state(snapshot, retain_raw=False))
    rendered = json.dumps(card, ensure_ascii=False)

    assert "卡片预览" in rendered
    assert "配置文件" in rendered
    assert "打开本地文件" in rendered
    assert "/Users/private" not in rendered
    assert "file://" not in rendered
    assert "![" not in rendered


def test_markdown_image_is_retained_for_upload_and_rendered_only_with_img_key():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"][-1] = {
        "id": "agent-image",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "结果如下：\n![趋势图](</tmp/codex output/chart.png>)",
    }

    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    refs = extract_card_image_sources(normalized)
    assert refs == [{
        "source": "/tmp/codex output/chart.png",
        "alt": "趋势图",
    }]

    without_upload = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "/tmp/codex output/chart.png" not in without_upload
    assert '"tag": "img_combination"' not in without_upload

    with_upload = build_desktop_card(
        normalized,
        image_keys={"/tmp/codex output/chart.png": "img_v3_test"},
    )
    images = [
        node for node in _walk_card(with_upload)
        if node.get("tag") == "img_combination"
    ]
    assert images == [{
        "tag": "img_combination",
        "combination_mode": "double",
        "img_list": [{"img_key": "img_v3_test"}],
        "img_list_length": 1,
        "corner_radius": "12px",
        "margin": "12px 0px 0px 0px",
    }]
    assert "/tmp/codex output/chart.png" not in json.dumps(with_upload, ensure_ascii=False)


def test_remote_markdown_image_is_not_exposed_or_uploaded():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"][-1]["text"] = (
        "远程图：![外部图片](https://example.invalid/private.png)"
    )
    normalized = normalize_conversation_state(snapshot, retain_raw=False)

    assert extract_card_image_sources(normalized) == []
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "https://example.invalid" not in rendered
    assert "外部图片" in rendered


def test_markdown_image_path_in_alt_is_redacted():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"][-1]["text"] = (
        "![/Users/private/photo.png](/Users/private/photo.png)"
    )
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)

    assert "/Users/private" not in rendered
    assert "Codex 生成的图片" in rendered


def test_snapshot_envelope_metadata_is_normalized():
    normalized = normalize_conversation_state(
        _event({"type": "snapshot", "revision": 45, "conversationState": _snapshot()})
    )
    assert normalized["thread_id"] == "thread-1"
    assert normalized["host_id"] == "host-1"
    assert normalized["revision"] == 45


def test_immer_add_replace_remove_are_immutable_and_ordered():
    original = {"turns": [{"items": [{"text": "first"}]}], "requests": ["old"]}
    before = copy.deepcopy(original)

    result = apply_immer_patches(original, [
        {"op": "add", "path": ["turns", 0, "items", 1], "value": {"text": "second"}},
        {"op": "replace", "path": ["turns", 0, "items", 0, "text"], "value": "changed"},
        {"op": "remove", "path": ["requests", 0]},
    ])

    assert original == before
    assert result == {
        "turns": [{"items": [{"text": "changed"}, {"text": "second"}]}],
        "requests": [],
    }


def test_patch_update_adds_public_message_and_remove_hides_it():
    current = normalize_desktop_update(
        None,
        _event({"type": "snapshot", "revision": 45, "conversationState": _snapshot()}),
    )
    added = normalize_desktop_update(current, _event({
        "type": "patches",
        "baseRevision": 45,
        "revision": 46,
        "patches": [{
            "op": "add",
            "path": ["turns", 0, "items", 3],
            "value": {
                "id": "agent-2",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "处理完成。",
            },
        }],
    }))
    assert [event["text"] for event in extract_public_events(added)] == [
        "正在检查项目结构。",
        "处理完成。",
    ]

    removed = normalize_desktop_update(added, _event({
        "type": "patches",
        "baseRevision": 46,
        "revision": 47,
        "patches": [{"op": "remove", "path": ["turns", 0, "items", 3]}],
    }))
    assert [event["text"] for event in extract_public_events(removed)] == ["正在检查项目结构。"]


def test_approval_request_sets_waiting_state_and_card_actions():
    snapshot = _snapshot()
    snapshot["requests"] = [{
        "id": 77,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "turnId": "turn-1",
            "reason": "需要运行测试",
            "command": "SECRET_COMMAND_MUST_NOT_BE_RENDERED",
        },
    }]
    normalized = normalize_conversation_state(snapshot)

    assert normalized["status"] == "waiting_approval"
    assert normalized["pending"] == {
        "kind": "approval",
        "request_kind": "command_execution",
        "request_id": 77,
        "method": "item/commandExecution/requestApproval",
        "title": "命令执行审批",
        "prompt": "需要运行测试",
        "options": [],
        "details": "命令：`SECRET_[REDACTED]`",
    }

    card = build_desktop_card(normalized)
    rendered = json.dumps(card, ensure_ascii=False)
    assert "等待审批" in rendered
    assert "命令：" in rendered
    assert "SECRET_COMMAND_MUST_NOT_BE_RENDERED" not in rendered
    actions = _callback_values(card, "desktop_approval")
    assert {action["decision"] for action in actions} == {"accept", "decline"}
    assert all(action["action"] == "desktop_approval" for action in actions)
    assert all(action["thread_id"] == "thread-1" for action in actions)
    assert all(action["request_id"] == 77 for action in actions)
    assert actions == [{
        "action": "desktop_approval", "thread_id": "thread-1", "request_id": 77,
        "kind": "command_execution", "decision": decision,
    } for decision in ("accept", "decline")]
    _assert_status_badge(card, "等待审批", "waiting")
    _assert_soft_primary(_callback_control(card, actions[0]), "允许")
    decline = _callback_control(card, actions[1])
    assert decline["background_style"] == "codex_button_secondary"
    assert decline["border_color"] == "codex_secondary"


def test_permissions_request_keeps_grant_payload_internal_only():
    snapshot = _snapshot()
    snapshot["requests"] = [{
        "id": "permissions-1",
        "method": "item/permissions/requestApproval",
        "params": {
            "reason": "需要网络权限",
            "permissions": {"network": {"enabled": True}},
        },
    }]
    normalized = normalize_conversation_state(snapshot)
    assert normalized["pending"]["permissions_response"] == {
        "permissions": {"network": {"enabled": True}},
        "scope": "turn",
    }
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "permissions_response" not in rendered
    assert "请求权限" in rendered


def test_file_change_approval_only_allows_remote_decline():
    snapshot = _snapshot()
    snapshot["requests"] = [{
        "id": "file-1",
        "method": "item/fileChange/requestApproval",
        "params": {"reason": "需要修改文件", "grantRoot": "/workspace"},
    }]
    normalized = normalize_conversation_state(snapshot)
    card = build_desktop_card(normalized)
    rendered = json.dumps(card, ensure_ascii=False)
    assert "写入范围" in rendered
    assert "飞书仅支持拒绝" in rendered
    assert '"decision": "accept"' not in rendered
    assert '"decision": "decline"' in rendered
    assert _callback_values(card, "desktop_approval") == [{
        "action": "desktop_approval", "thread_id": "thread-1", "request_id": "file-1",
        "kind": "file_change", "decision": "decline",
    }]


def test_multiple_input_questions_fail_closed_to_desktop():
    snapshot = _snapshot()
    snapshot["requests"] = [{
        "id": "multi-input",
        "method": "item/tool/requestUserInput",
        "params": {
            "questions": [
                {"id": "one", "question": "问题一"},
                {"id": "two", "question": "问题二"},
            ],
        },
    }]
    normalized = normalize_conversation_state(snapshot)
    assert normalized["pending"]["unsupported"] is True
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "请在 Codex Desktop 中处理" in rendered


def test_user_input_request_only_exposes_question_and_options():
    snapshot = _snapshot()
    snapshot["requests"] = [{
        "id": "request-1",
        "method": "item/tool/requestUserInput",
        "params": {
            "turnId": "turn-1",
            "questions": [{
                "id": "environment",
                "header": "环境",
                "question": "选择部署环境",
                "options": [
                    {"label": "测试", "description": "使用测试环境"},
                    {"label": "生产", "description": "使用生产环境"},
                ],
            }],
            "private": "DO_NOT_SHOW_THIS_FIELD",
        },
    }]
    normalized = normalize_conversation_state(snapshot)
    card = build_desktop_card(normalized)
    rendered = json.dumps(card, ensure_ascii=False)

    assert normalized["status"] == "waiting_input"
    assert "选择部署环境" in rendered
    assert "DO_NOT_SHOW_THIS_FIELD" not in rendered
    assert '"action": "desktop_input"' in rendered
    assert _callback_values(card, "desktop_input") == [{
        "action": "desktop_input", "thread_id": "thread-1", "request_id": "request-1",
        "kind": "user_input", "question_id": "environment", "answer": answer,
    } for answer in ("测试", "生产")]
    _assert_status_badge(card, "等待输入", "waiting")


def test_unknown_schema_fails_closed_without_recursive_text_scraping():
    unknown = {
        "id": "thread-unknown",
        "futureTimeline": [{
            "type": "agentMessage",
            "text": "SHOULD_NOT_ESCAPE_UNKNOWN_SCHEMA",
        }],
        "debug": {
            "reasoning": "PRIVATE_REASONING",
            "toolOutput": "PRIVATE_TOOL_OUTPUT",
        },
    }
    normalized = normalize_conversation_state(unknown)
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)

    assert normalized["schema_known"] is False
    assert normalized["messages"] == []
    assert normalized["pending"] is None
    assert "SHOULD_NOT_ESCAPE_UNKNOWN_SCHEMA" not in rendered
    assert "PRIVATE_REASONING" not in rendered
    assert "PRIVATE_TOOL_OUTPUT" not in rendered


def test_desktop_card_has_send_stop_and_detach_controls():
    normalized = normalize_conversation_state(_snapshot())
    card = build_desktop_card(normalized)
    rendered = json.dumps(card, ensure_ascii=False)

    assert '"name": "desktop_input"' in rendered
    assert '"name": "desktop_command__thread-1"' in rendered
    assert '"action": "desktop_interrupt"' in rendered
    assert '"turn_id": "turn-1"' in rendered
    assert '"action": "desktop_detach"' in rendered
    assert '"action": "menu_open"' in rendered


@pytest.mark.parametrize("status", [
    "idle",
    "running",
    "waiting_approval",
    "waiting_input",
    "completed",
    "failed",
    "interrupted",
    "unknown",
])
def test_desktop_card_v2_contract_for_all_states(status):
    normalized = normalize_conversation_state(_snapshot(), retain_raw=False)
    normalized["status"] = status
    normalized["active_turn_id"] = "turn-1" if status in {
        "running", "waiting_approval", "waiting_input"
    } else None
    card = build_desktop_card(normalized)
    rendered = json.dumps(card, ensure_ascii=False)

    assert card["schema"] == "2.0"
    assert "header" not in card
    assert card["config"]["update_multi"] is True
    assert card["config"]["compact_width"] is False
    summary = card["config"]["summary"]["content"]
    assert 8 <= len(summary) <= 60
    assert _STATUS_LABEL_FOR_TEST[status] in rendered
    assert "PRIVATE_REASONING" not in rendered
    assert "PRIVATE_TOOL_OUTPUT" not in rendered
    _assert_status_badge(card, _STATUS_LABEL_FOR_TEST[status], _STATUS_TONE_FOR_TEST[status])
    menu = _callback_control(card, {"action": "menu_open"})
    _assert_soft_primary(menu, "打开菜单")
    assert menu["background_style"] != _status_badge(card)["background_style"]


_STATUS_LABEL_FOR_TEST = {
    "idle": "空闲",
    "running": "运行中",
    "waiting_approval": "等待审批",
    "waiting_input": "等待输入",
    "completed": "已完成",
    "failed": "异常",
    "interrupted": "已停止",
    "unknown": "状态未知",
}


_STATUS_TONE_FOR_TEST = {
    "idle": "neutral",
    "running": "running",
    "waiting_approval": "waiting",
    "waiting_input": "waiting",
    "completed": "success",
    "failed": "failure",
    "interrupted": "neutral",
    "unknown": "neutral",
}


def test_desktop_form_preserves_dispatch_contract_and_controls_stay_outside():
    card = build_desktop_card(normalize_conversation_state(_snapshot(), retain_raw=False))
    assert card["body"]["elements"][0]["tag"] == "interactive_container"
    assert card["body"]["elements"][0]["background_style"] == "codex_canvas"
    assert card["body"]["elements"][0]["corner_radius"] == "12px"
    assert card["body"]["elements"][0]["border_color"] == "codex_secondary"
    assert [element["tag"] for element in card["body"]["elements"]] == [
        "interactive_container",
        "hr",
        "form",
        "hr",
        "interactive_container",
    ]
    forms = [node for node in _walk_card(card) if node.get("tag") == "form"]
    assert len(forms) == 1
    form = forms[0]
    assert form["name"] == "desktop_input"

    inputs = [node for node in _walk_card(form) if node.get("tag") == "input"]
    assert [node["name"] for node in inputs] == ["desktop_command__thread-1"]
    assert inputs[0]["max_length"] == 1000

    submit = _buttons(form, "发送指令")
    assert len(submit) == 1
    assert submit[0]["name"] == "desktop_send"
    assert submit[0]["action_type"] == "form_submit"
    assert submit[0]["form_action_type"] == "submit"
    assert submit[0]["form_name"] == "desktop_input"
    assert _callback_values(form) == []
    assert not any(
        node.get("tag") == "form"
        for node in _walk_card(card["body"]["elements"][0])
    )

    assert _callback_values(card, "menu_open") == [{"action": "menu_open"}]
    assert _callback_values(card, "desktop_interrupt") == [{
        "action": "desktop_interrupt",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
    }]
    assert _callback_values(card, "desktop_detach") == [{
        "action": "desktop_detach",
        "thread_id": "thread-1",
    }]


@pytest.mark.parametrize(("status", "has_stop"), [
    ("idle", False),
    ("running", True),
    ("waiting_approval", True),
    ("waiting_input", True),
    ("completed", False),
    ("failed", False),
    ("interrupted", False),
    ("unknown", False),
])
def test_live_status_matrix_controls(status, has_stop):
    normalized = normalize_conversation_state(_snapshot(), retain_raw=False)
    normalized["status"] = status
    normalized["active_turn_id"] = "turn-1" if has_stop else None
    card = build_desktop_card(normalized)

    assert bool(_callback_values(card, "desktop_interrupt")) is has_stop
    assert _callback_values(card, "desktop_detach")


def test_conversation_card_uses_single_column_copy_and_standard_collapsible_icon():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].insert(0, {
        "id": "user-1",
        "type": "userMessage",
        "content": [{"type": "text", "text": "请检查项目结构"}],
    })
    snapshot["turns"][0]["items"].append({
        "id": "agent-2",
        "type": "agentMessage",
        "phase": "commentary",
        "text": "继续处理。",
    })
    card = build_desktop_card(normalize_conversation_state(snapshot, retain_raw=False))

    for node in _walk_card(card):
        if node.get("tag") != "column_set":
            continue
        for column in node["columns"]:
            if column.get("width") == "weighted":
                assert column.get("weight") == 1

    panels = [node for node in _walk_card(card) if node.get("tag") == "collapsible_panel"]
    assert len(panels) == 1
    header = panels[0]["header"]
    assert header["icon"] == {
        "tag": "standard_icon",
        "token": "down_outlined",
        "color": "grey",
    }
    assert header["icon_position"] == "right"
    assert header["icon_expanded_angle"] == -180

    rendered = json.dumps(card, ensure_ascii=False)
    assert "Workspace" not in rendered
    assert "ASSISTANT / LIVE OUTPUT" not in rendered
    assert "YOU / TASK CONTEXT" not in rendered
    shell = card["body"]["elements"][0]["elements"]
    user_index = next(
        index for index, node in enumerate(shell)
        if node.get("tag") == "interactive_container"
        and "你" in json.dumps(node, ensure_ascii=False)
    )
    codex_index = next(
        index for index, node in enumerate(shell)
        if node.get("tag") == "markdown"
        and "Codex</font>" in node.get("content", "")
    )
    assert user_index < codex_index
    assert [node["tag"] for node in card["body"]["elements"]] == [
        "interactive_container",
        "hr",
        "form",
        "hr",
        "interactive_container",
    ]
    assert card["body"]["elements"][-1]["corner_radius"] == "12px"
    assert card["body"]["elements"][-1]["border_color"] == "codex_secondary"
    bordered_surfaces = [
        node for node in _walk_card(card)
        if node.get("tag") == "interactive_container"
        and node.get("background_style") in {"codex_body", "codex_button_secondary"}
    ]
    assert bordered_surfaces
    assert all(
        node.get("has_border") is True
        and node.get("border_color") == "codex_secondary"
        for node in bordered_surfaces
    )
    assert panels[0]["border"] == {
        "color": "codex_secondary",
        "corner_radius": "12px",
    }


def test_all_desktop_builders_use_workspace_foundation_and_final_palette():
    cards = [
        build_desktop_card(normalize_conversation_state(_snapshot(), retain_raw=False)),
        *(build_desktop_list_card([{
            "thread_id": "thread-1",
            "title": "Desktop 会话",
            "project_name": "测试项目",
            "status": "running",
        }], archived=archived) for archived in (False, True)),
        *(build_desktop_completion_card({
            "thread_id": "thread-1",
            "title": "Desktop 会话",
            "project_name": "测试项目",
            "outcome": outcome,
        }) for outcome in ("completed", "failed")),
    ]

    for card in cards:
        assert card["schema"] == "2.0"
        assert "header" not in card
        assert card["config"]["compact_width"] is False
        assert card["config"]["update_multi"] is True
        assert 8 <= len(card["config"]["summary"]["content"]) <= 60
        assert card["body"]["padding"] == "0px 0px 0px 0px"
        assert card["body"]["elements"][0]["background_style"] == "codex_canvas"
        colors = card["config"]["style"]["color"]
        assert colors == soft_color_tokens()
        assert colors["codex_canvas"]["light_mode"] == "rgba(250,251,252,1)"
        assert colors["codex_accent"]["light_mode"] == "rgba(237,243,239,1)"
        assert colors["codex_accent_2"]["light_mode"] == "rgba(234,240,245,1)"
        assert colors["codex_button"] == {
            "light_mode": "rgba(229,237,243,1)",
            "dark_mode": "rgba(59,75,88,1)",
        }
        assert all(token["light_mode"] != token["dark_mode"] for token in colors.values())
        rendered = json.dumps(card).lower()
        for removed_color in (
            "#3941ff", "#5a61ff", "rgba(57,65,255,1)", "rgba(90,97,255,1)",
            "rgba(203,197,255,1)", "rgba(198,214,255,1)",
        ):
            assert removed_color not in rendered
        assert not any(
            node.get("tag") == "button" and node.get("type", "").startswith("primary")
            for node in _walk_card(card)
        )


def test_pending_interrupt_precedes_conversation_and_history_is_read_only():
    snapshot = _snapshot()
    snapshot["turns"].insert(0, {
        "turnId": "turn-history",
        "status": "completed",
        "items": [
            {"id": "u-old", "type": "userMessage",
             "content": [{"type": "text", "text": "历史问题"}]},
            {"id": "a-old", "type": "agentMessage", "phase": "final_answer",
             "text": "历史回答"},
        ],
    })
    snapshot["requests"] = [{
        "id": "approval-live",
        "method": "item/commandExecution/requestApproval",
        "params": {"turnId": "turn-1", "reason": "需要确认命令"},
    }]
    normalized = normalize_conversation_state(snapshot, retain_raw=False)

    live = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert live.index("命令执行审批") < live.index("正在检查项目结构")

    historical_card = build_desktop_card(normalized, selected_turn_id="turn-history")
    _assert_status_badge(historical_card, "已完成", "success")
    historical = json.dumps(historical_card, ensure_ascii=False)
    assert "历史第 1/2 轮" in historical
    assert "实时同步" not in historical
    assert '"action": "desktop_approval"' not in historical
    assert '"action": "desktop_interrupt"' not in historical


def test_historical_turn_hides_live_pending_and_interrupt_controls():
    snapshot = _snapshot()
    snapshot["turns"].insert(0, {
        "turnId": "turn-history",
        "status": "completed",
        "items": [
            {
                "id": "old-user",
                "type": "userMessage",
                "content": [{"type": "text", "text": "历史问题"}],
            },
            {
                "id": "old-agent",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "历史回答",
            },
        ],
    })
    snapshot["requests"] = [{
        "id": "approval-live",
        "method": "item/commandExecution/requestApproval",
        "params": {"turnId": "turn-1", "reason": "当前轮待审批"},
    }]
    normalized = normalize_conversation_state(snapshot)

    historical = json.dumps(
        build_desktop_card(normalized, selected_turn_id="turn-history"),
        ensure_ascii=False,
    )
    assert "历史问题" in historical
    assert "等待审批" not in historical
    assert "已完成" in historical
    assert "当前轮待审批" not in historical
    assert '"action": "desktop_approval"' not in historical
    assert '"action": "desktop_interrupt"' not in historical
    assert '"action": "desktop_detach"' in historical

    latest_card = build_desktop_card(normalized)
    _assert_status_badge(latest_card, "等待审批", "waiting")
    latest = json.dumps(latest_card, ensure_ascii=False)
    assert "当前轮待审批" in latest
    assert '"action": "desktop_approval"' in latest
    assert '"action": "desktop_interrupt"' in latest


def test_desktop_list_card_uses_thread_ids_for_attach():
    card = build_desktop_list_card([
        {
            "thread_id": "thread-1",
            "title": "Desktop 会话",
            "project_name": "测试项目",
            "status": "running",
            "cwd": "/workspace",
            "updated_at": "2026-08-24T16:00:00Z",
        }
    ])
    rendered = json.dumps(card, ensure_ascii=False)

    assert "Desktop 会话" in rendered
    assert "**测试项目**" in rendered
    assert "<font color='codex_muted'>运行中" in rendered
    assert "Session：**Desktop 会话**" in rendered
    assert "Session ID：`thread-1`" in rendered
    assert "目录：`/workspace`" in rendered
    assert '"action": "desktop_attach"' in rendered
    assert '"thread_id": "thread-1"' in rendered
    assert '"action": "desktop_list_page"' not in rendered


@pytest.mark.parametrize("archived", [False, True])
def test_desktop_list_highlights_stay_passive_and_rows_keep_attach_actions(archived):
    card = build_desktop_list_card([
        {"thread_id": "thread-1", "title": "任务一"},
        {"thread_id": "thread-2", "title": "任务二"},
    ], archived=archived)

    assert _callback_values(card, "desktop_attach") == [
        {"action": "desktop_attach", "thread_id": "thread-1"},
        {"action": "desktop_attach", "thread_id": "thread-2"},
    ]
    _assert_status_badge(card, "已归档" if archived else "任务列表", "neutral")
    tasks = _highlight(card, "TASKS")
    assert tasks["background_style"] == "codex_status_neutral_bg"
    for thread_id in ("thread-1", "thread-2"):
        control = _callback_control(card, {"action": "desktop_attach", "thread_id": thread_id})
        _assert_soft_primary(control, "恢复并进入" if archived else "进入任务")
    for label in ("TASKS", "PAGE"):
        highlight = _highlight(card, label)
        assert highlight["background_style"] == "codex_status_neutral_bg"
        assert _callback_values(highlight) == []


@pytest.mark.parametrize(("status", "label"), [
    ("running", "运行中"),
    ("waiting_approval", "等待审批"),
    ("waiting_input", "等待输入"),
    ("failed", "异常"),
    ("idle", "空闲"),
    ("completed", "已完成"),
    ("unknown", "状态未知"),
    (None, "状态未知"),
])
def test_desktop_list_card_uses_runtime_status_not_binding(status, label):
    card = build_desktop_list_card([{
        "thread_id": "thread-1",
        "title": "同名会话",
        "project_name": "项目甲",
        "status": status,
    }], current_thread_id="thread-1")
    rendered = json.dumps(card, ensure_ascii=False)

    assert "**项目甲**" in rendered
    assert "Session：**同名会话**" in rendered
    assert f"<font color='codex_muted'>{label} · 当前任务</font>" in rendered
    assert _callback_values(card, "desktop_detach") == [{
        "action": "desktop_detach",
        "thread_id": "thread-1",
    }]


def test_desktop_list_card_paginates_five_threads_and_clamps_page():
    threads = [
        {
            "thread_id": f"thread-{index}",
            "title": f"任务 {index}",
            "project_name": f"项目 {index}",
            "cwd": f"/workspace/project-{index}",
        }
        for index in range(12)
    ]

    first_card = build_desktop_list_card(threads)
    first = json.dumps(first_card, ensure_ascii=False)
    assert "第 1/3 页 · 共 12 个" in first
    assert "Session ID：`thread-0`" in first
    assert "Session ID：`thread-4`" in first
    assert "Session ID：`thread-5`" not in first
    assert '"action": "desktop_list_page", "page": 1' in first
    first_previous = _buttons(first_card, "上一页")[0]
    assert first_previous["disabled"] is True
    assert "behaviors" not in first_previous

    middle = json.dumps(build_desktop_list_card(threads, page=1), ensure_ascii=False)
    assert "第 2/3 页 · 共 12 个" in middle
    assert "Session ID：`thread-5`" in middle
    assert "Session ID：`thread-9`" in middle
    assert "Session ID：`thread-10`" not in middle
    assert '"action": "desktop_list_page", "page": 0' in middle
    assert '"action": "desktop_list_page", "page": 2' in middle

    last_card = build_desktop_list_card(threads, page=999)
    last = json.dumps(last_card, ensure_ascii=False)
    assert "第 3/3 页 · 共 12 个" in last
    assert "Session ID：`thread-10`" in last
    assert "Session ID：`thread-11`" in last
    assert "Session ID：`thread-9`" not in last
    last_next = _buttons(last_card, "下一页")[0]
    assert last_next["disabled"] is True
    assert "behaviors" not in last_next

    invalid_page = json.dumps(build_desktop_list_card(threads, page="bad"), ensure_ascii=False)
    assert "第 1/3 页 · 共 12 个" in invalid_page


def test_desktop_list_card_filters_invalid_rows_before_pagination():
    threads = [{"title": "无 ID"}] + [
        {"thread_id": f"valid-{index}", "title": f"任务 {index}"}
        for index in range(6)
    ]
    rendered = json.dumps(build_desktop_list_card(threads), ensure_ascii=False)

    assert "Session ID：`valid-0`" in rendered
    assert "Session ID：`valid-4`" in rendered
    assert "Session ID：`valid-5`" not in rendered
    assert "第 1/2 页 · 共 6 个" in rendered


def test_archived_desktop_list_has_restore_and_attach_actions():
    card = build_desktop_list_card([{
        "thread_id": "archived-1",
        "title": "已归档任务",
        "project_name": "项目甲",
        "status": "failed",
    }], current_thread_id="archived-1", archived=True)
    rendered = json.dumps(card, ensure_ascii=False)

    assert "Codex Desktop 已归档" in rendered
    assert "**项目甲**" in rendered
    assert "<font color='codex_muted'>异常" in rendered
    assert "Session：**已归档任务**" in rendered
    assert '"action": "desktop_unarchive"' in rendered
    assert '"action": "desktop_attach"' in rendered
    assert "恢复并进入" in rendered
    assert "移出归档" in rendered
    assert '"action": "desktop_detach"' not in rendered


@pytest.mark.parametrize(("outcome", "label"), [
    ("completed", "执行完成"),
    ("failed", "执行失败"),
])
def test_desktop_completion_card_can_reconnect(outcome, label):
    card = build_desktop_completion_card({
        "thread_id": "thread-1",
        "title": "会话名称",
        "project_name": "项目名称",
        "outcome": outcome,
        "completed_at": "2026-08-24T12:00:00Z",
    })
    rendered = json.dumps(card, ensure_ascii=False)

    assert "header" not in card
    assert card["config"]["compact_width"] is False
    assert card["config"]["summary"]["content"]
    assert label in rendered
    tone = "failure" if outcome == "failed" else "success"
    _assert_status_badge(card, label, tone)
    result = _highlight(card, "RESULT")
    assert result["background_style"] == (
        "codex_status_failure_bg" if outcome == "failed" else "codex_accent"
    )
    assert f"<font color='codex_status_{tone}_text'>**{label}**</font>" in rendered
    assert _callback_values(result) == []
    assert "**项目名称**" in rendered
    assert "Session：**会话名称**" in rendered
    expected_action = {"action": "desktop_attach", "thread_id": "thread-1"}
    assert _callback_values(card, "desktop_attach") == [expected_action]
    reconnect_controls = [
        node for node in _walk_card(card)
        if node.get("behaviors") == [{"type": "callback", "value": expected_action}]
    ]
    assert len(reconnect_controls) == 1
    reconnect_control = reconnect_controls[0]
    assert reconnect_control["tag"] == "interactive_container"
    assert reconnect_control["background_style"] == "codex_accent_2"
    reconnect_content = json.dumps(reconnect_control, ensure_ascii=False)
    assert "<font color='codex_button_text'>NEXT</font>" in reconnect_content
    assert f"codex_status_{tone}_text" not in reconnect_content
    assert "NEXT" in reconnect_content
    assert "重新连接" in reconnect_content
    assert "连接此 Session" not in rendered
    assert "连接到此 Session" not in rendered


@pytest.mark.parametrize("thread_fields", [
    {},
    {"thread_id": None},
    {"thread_id": ""},
    {"thread_id": " \t\n "},
    {"thread_id": 123},
    {"thread_id": True},
    {"thread_id": ["thread-1"]},
    {"thread_id": {"id": "thread-1"}},
])
def test_desktop_completion_without_valid_thread_id_has_no_reconnect(thread_fields):
    card = build_desktop_completion_card({
        "title": "会话名称",
        "outcome": "completed",
        **thread_fields,
    })
    rendered = json.dumps(card, ensure_ascii=False)

    assert _callback_values(card, "desktop_attach") == []
    assert "无法重新连接" in rendered
    assert "缺少" in rendered
    assert "Session ID" in rendered
    assert "连接此 Session" not in rendered
    assert "连接到此 Session" not in rendered


def test_bad_patch_and_revision_mismatch_fail_closed():
    with pytest.raises(PatchApplyError):
        apply_immer_patches({"items": []}, [{"op": "move", "path": ["items", 0]}])

    current = normalize_desktop_update(
        None,
        _event({"type": "snapshot", "revision": 45, "conversationState": _snapshot()}),
    )
    result = normalize_desktop_update(current, _event({
        "type": "patches",
        "baseRevision": 44,
        "revision": 46,
        "patches": [],
    }))
    assert result["schema_known"] is False
    assert result["needs_snapshot"] is True
    assert result["messages"] == []
    assert "_conversation_state" not in result


def test_patch_only_mode_accepts_only_whitelisted_public_values():
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 10,
        "title": "Desktop task",
        "status": "running",
        "messages": [],
        "pending": None,
    }
    result = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 10,
        "revision": 11,
        "patches": [
            {
                "op": "add",
                "path": ["turnHistory", "history", "entitiesByKey", "turn-1", "items", 5],
                "value": {
                    "id": "agent-2",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "公开进度",
                },
            },
            {
                "op": "add",
                "path": ["turnHistory", "history", "entitiesByKey", "turn-1", "items", 6],
                "value": {
                    "id": "reasoning-2",
                    "type": "reasoning",
                    "summary": ["PRIVATE_REASONING"],
                },
            },
        ],
    }))

    rendered = json.dumps(build_desktop_card(result), ensure_ascii=False)
    assert result["patch_only"] is True
    assert result["revision"] == 11
    assert "公开进度" in rendered
    assert "PRIVATE_REASONING" not in rendered


def test_patch_only_command_status_does_not_change_thread_status():
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 11,
        "title": "Desktop task",
        "status": "running",
        "messages": [],
        "pending": None,
    }
    result = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 11,
        "revision": 12,
        "patches": [{
            "op": "replace",
            "path": ["turnHistory", "history", "entitiesByKey", "turn-1", "items", 9, "status"],
            "value": "completed",
        }],
    }))
    assert result["status"] == "running"


def test_patch_only_thread_runtime_status_updates_public_status():
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 12,
        "title": "Desktop task",
        "status": "completed",
        "messages": [],
        "pending": None,
    }
    result = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 12,
        "revision": 13,
        "patches": [{
            "op": "replace",
            "path": ["threadRuntimeStatus", "type"],
            "value": "active",
        }],
    }))
    assert result["status"] == "running"


def test_patch_only_item_remove_removes_projected_message():
    current = normalize_patch_only_update({
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 1,
        "title": "Desktop task",
        "status": "running",
        "messages": [],
        "pending": None,
    }, _event({
        "type": "patches",
        "baseRevision": 1,
        "revision": 2,
        "patches": [{
            "op": "add",
            "path": ["turnHistory", "history", "entitiesByKey", "turn-1", "items", 0],
            "value": {"id": "m1", "type": "agentMessage", "phase": "commentary", "text": "hello"},
        }],
    }))
    assert [message["id"] for message in current["messages"]] == ["m1"]

    removed = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 2,
        "revision": 3,
        "patches": [{
            "op": "remove",
            "path": ["turnHistory", "history", "entitiesByKey", "turn-1", "items", 0],
        }],
    }))
    assert removed["messages"] == []


def test_snapshot_projection_indexes_existing_agent_for_later_text_delta():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"][-1]["text"] = "半截"
    current = normalize_conversation_state(snapshot, retain_raw=False)
    current["thread_id"] = "thread-1"
    current["host_id"] = "local"
    current["revision"] = 8

    updated = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 8,
        "revision": 9,
        "patches": [{
            "op": "replace",
            "path": ["turns", 0, "items", 2, "text"],
            "value": "半截内容继续增长",
        }],
    }))
    assert updated["messages"][-1]["text"] == "半截内容继续增长"
    assert "_conversation_state" not in updated


def test_patch_replacing_markdown_image_drops_stale_image_reference():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"][-1]["text"] = (
        "旧图：![预览](/tmp/old-preview.png)"
    )
    current = normalize_conversation_state(snapshot, retain_raw=False)
    current["thread_id"] = "thread-1"
    current["host_id"] = "local"
    current["revision"] = 8
    assert extract_card_image_sources(current)

    updated = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 8,
        "revision": 9,
        "patches": [{
            "op": "replace",
            "path": ["turns", 0, "items", 2, "text"],
            "value": "图片已移除",
        }],
    }))

    assert extract_card_image_sources(updated) == []
    assert "/tmp/old-preview.png" not in json.dumps(
        build_desktop_card(updated), ensure_ascii=False
    )


def test_patch_removing_structured_images_drops_stale_reference():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"][-1]["images"] = [{
        "source": "/tmp/structured-preview.png",
        "alt": "结构化图片",
    }]
    current = normalize_conversation_state(snapshot, retain_raw=False)
    current["thread_id"] = "thread-1"
    current["host_id"] = "local"
    current["revision"] = 8
    assert extract_card_image_sources(current)

    updated = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 8,
        "revision": 9,
        "patches": [{
            "op": "remove",
            "path": ["turns", 0, "items", 2, "images"],
        }],
    }))

    assert extract_card_image_sources(updated) == []


def test_repeated_image_source_renders_once_per_card():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"] = [
        {
            "id": "agent-{}".format(index),
            "type": "agentMessage",
            "phase": "commentary",
            "text": "进度 {}\n![预览](/tmp/shared-preview.png)".format(index),
        }
        for index in range(6)
    ]
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    card = build_desktop_card(
        normalized,
        image_keys={"/tmp/shared-preview.png": "img_v3_shared"},
    )

    combinations = [
        node for node in _walk_card(card)
        if node.get("tag") == "img_combination"
    ]
    assert len(combinations) == 1
    assert combinations[0]["img_list"] == [{"img_key": "img_v3_shared"}]


@pytest.mark.parametrize(("count", "mode"), [
    (1, "double"),
    (2, "double"),
    (3, "triple"),
    (4, "bisect"),
])
def test_image_gallery_uses_compact_layout_by_count(count, mode):
    snapshot = _snapshot()
    sources = ["/tmp/preview-{}.png".format(index) for index in range(count)]
    snapshot["turns"][0]["items"][-1]["text"] = "\n".join(
        "![预览 {}]({})".format(index, source)
        for index, source in enumerate(sources)
    )
    card = build_desktop_card(
        normalize_conversation_state(snapshot, retain_raw=False),
        image_keys={source: "img_v3_{}".format(index) for index, source in enumerate(sources)},
    )
    combinations = [
        node for node in _walk_card(card)
        if node.get("tag") == "img_combination"
    ]

    assert len(combinations) == 1
    assert combinations[0]["combination_mode"] == mode
    assert combinations[0]["img_list_length"] == count


def test_card_groups_public_content_by_turn_and_pages_with_stable_turn_ids():
    snapshot = _snapshot()
    snapshot["turns"] = [
        {
            "turnId": "turn-1",
            "status": "completed",
            "items": [
                {
                    "id": "user-1",
                    "type": "userMessage",
                    "content": [
                        {"type": "text", "text": "第一轮问题"},
                        {"type": "localImage", "path": "PRIVATE_IMAGE_PATH"},
                    ],
                },
                {"id": "reason-1", "type": "reasoning", "text": "PRIVATE_REASONING"},
                {
                    "id": "agent-old",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "第一轮回答",
                },
            ],
        },
        {
            "turnId": "turn-2",
            "status": "inProgress",
            "items": [
                {
                    "id": "user-2",
                    "type": "userMessage",
                    "content": [
                        {"type": "text", "text": "第二轮问题"},
                        {"type": "mention", "path": "PRIVATE_MENTION_PATH"},
                    ],
                },
                {
                    "id": "steer-2",
                    "type": "steeringUserMessage",
                    "input": [
                        {"type": "text", "text": "再补充一个条件"},
                        {"type": "audio", "url": "PRIVATE_AUDIO_URL"},
                    ],
                },
                {
                    "id": "tool-2",
                    "type": "commandExecution",
                    "aggregatedOutput": "PRIVATE_TOOL_OUTPUT",
                },
                {
                    "id": "agent-new",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "第二轮处理中",
                },
            ],
        },
    ]

    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    assert [turn["turn_id"] for turn in extract_public_turns(normalized)] == [
        "turn-1",
        "turn-2",
    ]
    assert [message["text"] for message in normalized["turns"][1]["user_messages"]] == [
        "第二轮问题",
        "再补充一个条件",
    ]
    # Keep the original flat field for existing bridge consumers.
    assert [message["text"] for message in normalized["messages"]] == [
        "第一轮回答",
        "第二轮处理中",
    ]

    latest = json.dumps(build_desktop_card(normalized), ensure_ascii=False)
    assert "第二轮问题" in latest
    assert "补充指令：再补充一个条件" in latest
    assert "第二轮处理中" in latest
    assert "第一轮问题" not in latest
    assert "第一轮回答" not in latest
    assert '"action": "desktop_turn_page"' in latest
    assert '"target_turn_id": "turn-1"' in latest
    assert "第 2/2 轮" in latest

    older = json.dumps(
        build_desktop_card(normalized, selected_turn_id="turn-1"),
        ensure_ascii=False,
    )
    assert "第一轮问题" in older
    assert "第一轮回答" in older
    assert "第二轮问题" not in older
    assert "第二轮处理中" not in older
    assert '"target_turn_id": "turn-2"' in older
    assert "第 1/2 轮" in older

    for rendered in (latest, older):
        assert "PRIVATE_IMAGE_PATH" not in rendered
        assert "PRIVATE_MENTION_PATH" not in rendered
        assert "PRIVATE_AUDIO_URL" not in rendered
        assert "PRIVATE_REASONING" not in rendered
        assert "PRIVATE_TOOL_OUTPUT" not in rendered


def test_unknown_selected_turn_falls_back_to_latest_and_unknown_text_does_not_leak():
    snapshot = _snapshot()
    snapshot["turns"][0]["items"].insert(0, {
        "id": "user-1",
        "type": "userMessage",
        "content": [
            {"type": "unknown", "text": "PRIVATE_UNKNOWN_TEXT"},
            {"type": "image", "url": "PRIVATE_IMAGE_URL"},
        ],
    })
    normalized = normalize_conversation_state(snapshot, retain_raw=False)
    rendered = json.dumps(
        build_desktop_card(normalized, selected_turn_id="missing-turn"),
        ensure_ascii=False,
    )
    assert "正在检查项目结构" in rendered
    assert "该轮没有可显示的文本输入" not in rendered
    assert "PRIVATE_UNKNOWN_TEXT" not in rendered
    assert "PRIVATE_IMAGE_URL" not in rendered


def test_patch_only_whole_turn_and_text_delta_keep_query_and_agent_in_same_turn():
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 1,
        "title": "Desktop task",
        "status": "idle",
        "active_turn_id": None,
        "turns": [],
        "messages": [],
        "pending": None,
    }
    added = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 1,
        "revision": 2,
        "patches": [{
            "op": "add",
            "path": ["turnHistory", "history", "entitiesByKey", "turn-2"],
            "value": {
                "turnId": "turn-2",
                "status": "inProgress",
                "items": [
                    {
                        "id": "user-2",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "只处理这一轮"}],
                    },
                    {
                        "id": "agent-2",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "初始进度",
                    },
                    {"id": "secret", "type": "reasoning", "text": "PRIVATE_REASONING"},
                ],
            },
        }],
    }))
    assert added["turns"][0]["turn_id"] == "turn-2"
    assert added["turns"][0]["user_messages"][0]["text"] == "只处理这一轮"
    assert added["messages"][0]["turn_id"] == "turn-2"

    updated = normalize_patch_only_update(added, _event({
        "type": "patches",
        "baseRevision": 2,
        "revision": 3,
        "patches": [{
            "op": "replace",
            "path": [
                "turnHistory", "history", "entitiesByKey", "turn-2",
                "items", 1, "text",
            ],
            "value": "实时增长后的进度",
        }],
    }))
    assert updated["turns"][0]["agent_messages"][0]["text"] == "实时增长后的进度"
    assert updated["messages"][0]["text"] == "实时增长后的进度"
    rendered = json.dumps(build_desktop_card(updated), ensure_ascii=False)
    assert "只处理这一轮" in rendered
    assert "实时增长后的进度" in rendered
    assert "PRIVATE_REASONING" not in rendered


def test_patch_only_item_uses_active_turn_when_turn_index_has_no_mapping():
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 7,
        "title": "Desktop task",
        "status": "running",
        "active_turn_id": "turn-active",
        "turns": [{
            "turn_id": "turn-old",
            "status": "completed",
            "user_messages": [],
            "agent_messages": [],
        }],
        "messages": [],
        "pending": None,
        "_patch_turn_ids": {},
    }

    updated = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 7,
        "revision": 8,
        "patches": [{
            "op": "add",
            "path": ["turns", 0, "items", 0],
            "value": {
                "id": "agent-active",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "当前轮进度",
            },
        }],
    }))

    turns = {turn["turn_id"]: turn for turn in updated["turns"]}
    assert turns["turn-old"]["agent_messages"] == []
    assert turns["turn-active"]["agent_messages"][0]["text"] == "当前轮进度"
    assert updated["messages"][0]["turn_id"] == "turn-active"


def test_patch_only_add_turn_zero_appends_after_seeded_history_and_renders_latest():
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 10,
        "title": "Desktop task",
        "status": "completed",
        "active_turn_id": None,
        "turns": [{
            "turn_id": "turn-history",
            "status": "completed",
            "user_messages": [{"id": "old-user", "kind": "initial", "text": "历史问题"}],
            "agent_messages": [{
                "id": "old-agent",
                "turn_id": "turn-history",
                "phase": "final_answer",
                "text": "历史回答",
            }],
        }],
        "messages": [],
        "pending": None,
        "_patch_turn_ids": {},
    }

    updated = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 10,
        "revision": 11,
        "patches": [{
            "op": "add",
            "path": ["turns", 0],
            "value": {
                "turnId": "turn-active",
                "status": "inProgress",
                "items": [
                    {
                        "id": "new-user",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "新一轮问题"}],
                    },
                    {
                        "id": "new-agent",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "新一轮处理中",
                    },
                ],
            },
        }],
    }))

    assert [turn["turn_id"] for turn in updated["turns"]] == [
        "turn-history", "turn-active",
    ]
    assert updated["active_turn_id"] == "turn-active"
    rendered = json.dumps(build_desktop_card(updated), ensure_ascii=False)
    assert "新一轮问题" in rendered
    assert "新一轮处理中" in rendered
    assert "历史问题" not in rendered
    assert "历史回答" not in rendered


@pytest.mark.parametrize("history_first", [True, False])
def test_patch_only_history_upsert_and_active_remove_keep_completed_turn(history_first):
    current = {
        "schema_version": 1,
        "schema_known": False,
        "thread_id": "thread-1",
        "host_id": "local",
        "revision": 20,
        "title": "Desktop task",
        "status": "running",
        "active_turn_id": "turn-1",
        "turns": [{
            "turn_id": "turn-1",
            "status": "running",
            "user_messages": [{"id": "user-1", "kind": "initial", "text": "问题"}],
            "agent_messages": [],
        }],
        "messages": [],
        "pending": None,
        "_patch_turn_ids": {'["turns",0]': "turn-1"},
    }
    history_patch = {
        "op": "add",
        "path": ["turnHistory", "history", "entitiesByKey", "turn-1"],
        "value": {
            "turnId": "turn-1",
            "status": "completed",
            "items": [
                {
                    "id": "user-1",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "问题"}],
                },
                {
                    "id": "final-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "最终回答",
                },
            ],
        },
    }
    remove_active_patch = {"op": "remove", "path": ["turns", 0]}
    patches = (
        [history_patch, remove_active_patch]
        if history_first else [remove_active_patch, history_patch]
    )

    updated = normalize_patch_only_update(current, _event({
        "type": "patches",
        "baseRevision": 20,
        "revision": 21,
        "patches": patches,
    }))

    assert len(updated["turns"]) == 1
    assert updated["turns"][0]["turn_id"] == "turn-1"
    assert updated["turns"][0]["status"] == "completed"
    assert updated["turns"][0]["agent_messages"][0]["text"] == "最终回答"
    assert updated["active_turn_id"] is None
    assert updated["messages"][0]["text"] == "最终回答"
