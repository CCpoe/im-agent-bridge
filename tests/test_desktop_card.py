import copy
import json

import pytest

from lark_client.desktop_card import (
    PatchApplyError,
    apply_immer_patches,
    build_desktop_card,
    build_desktop_list_card,
    extract_public_events,
    normalize_conversation_state,
    normalize_desktop_update,
    normalize_patch_only_update,
)


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
    actions = [
        element["behaviors"][0]["value"]
        for column_set in card["body"]["elements"]
        if column_set.get("tag") == "column_set"
        for column in column_set["columns"]
        for element in column["elements"]
    ]
    assert {action["decision"] for action in actions} == {"accept", "decline"}
    assert all(action["action"] == "desktop_approval" for action in actions)
    assert all(action["thread_id"] == "thread-1" for action in actions)
    assert all(action["request_id"] == 77 for action in actions)


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
    rendered = json.dumps(build_desktop_card(normalized), ensure_ascii=False)

    assert '"name": "desktop_input"' in rendered
    assert '"name": "desktop_command__thread-1"' in rendered
    assert '"action": "desktop_interrupt"' in rendered
    assert '"turn_id": "turn-1"' in rendered
    assert '"action": "desktop_detach"' in rendered


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
    assert "🟢 **测试项目**" in rendered
    assert "Session：**Desktop 会话**" in rendered
    assert "Session ID：`thread-1`" in rendered
    assert "目录：`/workspace`" in rendered
    assert '"action": "desktop_attach"' in rendered
    assert '"thread_id": "thread-1"' in rendered
    assert '"action": "desktop_list_page"' not in rendered


@pytest.mark.parametrize(("status", "icon"), [
    ("running", "🟢"),
    ("waiting_approval", "🟢"),
    ("waiting_input", "🟢"),
    ("failed", "🔴"),
    ("idle", "⚪"),
    ("completed", "⚪"),
    ("unknown", "⚪"),
    (None, "⚪"),
])
def test_desktop_list_card_uses_runtime_status_not_binding(status, icon):
    card = build_desktop_list_card([{
        "thread_id": "thread-1",
        "title": "同名会话",
        "project_name": "项目甲",
        "status": status,
    }], current_thread_id="thread-1")
    details = card["body"]["elements"][0]["columns"][0]["elements"][0]["content"]

    assert details.splitlines()[:2] == [
        f"{icon} **项目甲**",
        "Session：**同名会话**",
    ]
    button = card["body"]["elements"][0]["columns"][1]["elements"][0]
    assert button["behaviors"][0]["value"]["action"] == "desktop_detach"


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
    first_previous = first_card["body"]["elements"][-1]["columns"][1]["elements"][0]
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
    last_next = last_card["body"]["elements"][-1]["columns"][3]["elements"][0]
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
