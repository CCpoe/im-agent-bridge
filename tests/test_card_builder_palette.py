"""CLI 与辅助卡柔和配色回归：颜色调整不得改变回调和原生表单语义。"""

import copy

import pytest

from lark_client.card_builder import (
    _VERSION,
    _build_buttons_v2,
    _build_menu_button_row,
    _soft_primary_control,
    build_dir_card,
    build_help_card,
    build_menu_card,
    build_session_closed_card,
    build_status_card,
    build_stream_card,
)
from lark_client.card_theme import soft_color_tokens


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _buttons(card, label=None):
    return [node for node in _walk(card)
            if node.get("tag") == "button"
            and (label is None or node.get("text", {}).get("content") == label)]


def _callbacks(card, action=None):
    values = []
    for node in _walk(card):
        for behavior in node.get("behaviors", []):
            value = behavior.get("value")
            if isinstance(value, dict) and (action is None or value.get("action") == action):
                values.append(value)
    return values


def _assert_soft_button(card, label):
    buttons = _buttons(card, label)
    assert len(buttons) == 1
    button = buttons[0]
    assert button["type"] == "text"
    assert button["text"]["text_color"] == "codex_button_text"
    assert any(node.get("tag") == "column"
               and node.get("background_style") == "codex_button"
               and button in node.get("elements", []) for node in _walk(card))
    return button


def test_soft_control_preserves_all_native_button_metadata_without_mutating_input():
    button = {
        "tag": "button", "type": "primary", "name": "original_submit",
        "text": {"tag": "plain_text", "content": "发送", "text_align": "center"},
        "action_type": "form_submit", "form_action_type": "submit", "form_name": "original_form",
        "disabled": True, "disabled_tips": {"tag": "plain_text", "content": "暂不可发送"},
        "confirm": {"title": {"tag": "plain_text", "content": "确认"},
                    "text": {"tag": "plain_text", "content": "确认发送此指令？"}},
        "value": {"id": "native-value"},
        "behaviors": [{"type": "callback", "value": {"action": "original_action"}}],
        "element_id": "original-element", "width": "fill", "required": True,
        "custom_action_id": "original-custom", "future_field": {"opaque": "keep"},
    }
    original = copy.deepcopy(button)
    wrapped = _soft_primary_control(button)
    control = _assert_soft_button(wrapped, "发送")
    for key, value in original.items():
        if key not in ("type", "text"):
            assert control[key] == value
    assert {key: value for key, value in control["text"].items() if key != "text_color"} == original["text"]
    assert button == original
    assert _callbacks(wrapped) == [{"action": "original_action"}]
    assert not any(node.get("tag") == "interactive_container" for node in _walk(wrapped))


@pytest.mark.parametrize("width,expected", [(None, "fill"), ("auto", "auto"), ("160px", "160px")])
def test_soft_control_fills_its_visual_surface_without_overwriting_explicit_width(width, expected):
    button = {"tag": "button", "text": {"tag": "plain_text", "content": "打开菜单"},
              "behaviors": [{"type": "callback", "value": {"action": "menu_open"}}]}
    if width is not None:
        button["width"] = width
    wrapped = _soft_primary_control(button)
    assert _buttons(wrapped)[0]["width"] == expected
    assert _callbacks(wrapped) == [{"action": "menu_open"}]


@pytest.mark.parametrize("session_name", [None, "cli-session"])
def test_cli_enter_remains_a_native_submit_button_inside_its_original_form(session_name):
    elements = _build_menu_button_row(session_name=session_name)
    forms = [node for node in _walk(elements) if node.get("tag") == "form"]
    assert len(forms) == 1
    form = forms[0]
    assert form["name"] == "claude_input"
    button = _assert_soft_button(form, "Enter ↵")
    assert button["name"] == "enter_submit"
    assert button["action_type"] == "form_submit"
    inputs = [node for node in _walk(form) if node.get("tag") == "input"]
    assert len(inputs) == 1
    assert inputs[0]["name"] == "command"
    assert _callbacks(elements, "menu_open") == [{"action": "menu_open"}]
    assert _callbacks(elements, "stream_detach") == (
        [{"action": "stream_detach", "session": session_name}] if session_name else [])
    assert _callbacks(elements, "send_key") == [
        {"action": "send_key", "key": "up"},
        {"action": "send_key", "key": "down"},
        {"action": "send_key", "key": "ctrl_o"},
        {"action": "send_key", "key": "shift_tab"},
        {"action": "send_key", "key": "esc"},
        {"action": "send_key", "key": "shift_tab", "times": 3},
    ]


def test_cli_reconnect_and_first_option_keep_their_original_callbacks():
    disconnected = _build_menu_button_row(session_name="cli-session", disconnected=True)
    _assert_soft_button(disconnected, "🔗 重新连接")
    assert _callbacks(disconnected) == [
        {"action": "menu_open"}, {"action": "stream_reconnect", "session": "cli-session"}]
    assert not any(node.get("tag") == "form" for node in _walk(disconnected))
    options = _build_buttons_v2([
        {"label": "输入内容", "value": "input", "needs_input": True},
        {"label": "取消", "value": "cancel"},
    ])
    _assert_soft_button(options, "1. 输入内容")
    assert _buttons(options, "2. 取消")[0]["type"] == "default"
    assert _callbacks(options) == [
        {"action": "select_option", "value": "input", "needs_input": True, "total": "2"},
        {"action": "select_option", "value": "cancel", "needs_input": False, "total": "2"},
    ]


def test_menu_session_attach_desktop_and_destructive_controls_keep_their_meaning():
    card = build_menu_card(
        [{"name": "current"}, {"name": "other"}], current_session="current",
        session_groups={"other": "existing-group"}, desktop_available=True, desktop_connected=True,
        notify_enabled=False,
    )
    _assert_soft_button(card, "进入会话")
    _assert_soft_button(card, "🖥️ Desktop 会话（已连接）")
    assert _callbacks(card, "list_attach") == [{"action": "list_attach", "session": "other"}]
    assert _callbacks(card, "list_detach") == [{"action": "list_detach", "session": "current"}]
    assert _callbacks(card, "desktop_list") == [{"action": "desktop_list"}]
    assert _buttons(card, "断开连接")[0]["type"] == "danger"
    for button in _buttons(card, "🗑️ 关闭"):
        assert button["type"] == "danger"
        session = button["behaviors"][0]["value"]["session"]
        assert button["confirm"] == {
            "title": {"tag": "plain_text", "content": "确认关闭会话"},
            "text": {"tag": "plain_text", "content": f"确定要关闭「{session}」吗？此操作不可撤销。"},
        }
    urgent = _buttons(card, "🔇 加急通知: 关")[0]
    assert urgent["disabled"] is True
    assert "behaviors" not in urgent
    group = _buttons(card, "进入群聊")[0]
    for key in ("default_url", "android_url", "ios_url", "pc_url"):
        assert group["behaviors"][0][key] == "https://applink.feishu.cn/client/chat/open?openChatId=existing-group"


def test_directory_claude_group_and_closed_session_controls_keep_callbacks():
    card = build_dir_card("/workspace", [
        {"name": "project", "is_dir": True, "full_path": "/workspace/project"},
    ], [])
    _assert_soft_button(card, "Claude群聊")
    assert _callbacks(card, "dir_new_group") == [
        {"action": "dir_new_group", "path": "/workspace/project", "session_name": "project", "cli_type": "claude"},
        {"action": "dir_new_group", "path": "/workspace/project", "session_name": "project", "cli_type": "codex"},
    ]
    assert _buttons(card, "Codex群聊")[0]["type"] == "default"
    closed = build_session_closed_card("cli-session")
    _assert_soft_button(closed, "📋 查看会话")
    assert _callbacks(closed) == [{"action": "menu_list"}, {"action": "menu_open"}]


@pytest.mark.parametrize("kwargs,tone", [
    ({}, "success"),
    ({"status_line": {"action": "处理中"}}, "running"),
    ({"option_block": {"sub_type": "option"}}, "waiting"),
    ({"option_block": {"sub_type": "permission"}}, "waiting"),
    ({"is_frozen": True}, "neutral"),
    ({"disconnected": True}, "neutral"),
])
def test_stream_header_preserves_status_semantics_without_saturated_native_fill(kwargs, tone):
    card = build_stream_card([], **kwargs)
    assert "header" not in card
    header = card["body"]["elements"][0]
    assert header["element_id"] == "cli_card_header"
    column = header["columns"][0]
    assert column["background_style"] == f"codex_status_{tone}_bg"
    assert column["elements"][0]["text"]["text_color"] == f"codex_status_{tone}_text"
    if _VERSION:
        assert column["elements"][1]["text"]["content"] == _VERSION


@pytest.mark.parametrize("card", [
    build_stream_card([{"_type": "PlanBlock", "title": "计划", "content": "检查"}], session_name="cli-session"),
    build_stream_card([], disconnected=True),
    build_status_card(True, "cli-session"),
    build_status_card(False),
    build_help_card(),
    build_dir_card("/workspace", [{"name": "project", "is_dir": True, "full_path": "/workspace/project"}], []),
    build_session_closed_card("cli-session"),
    build_menu_card([{"name": "cli-session"}], desktop_available=True, desktop_connected=True),
])
def test_all_legacy_card_builders_register_soft_tokens_and_remove_saturated_primary(card):
    assert "header" not in card
    assert card["body"]["elements"][0]["element_id"] == "cli_card_header"
    assert card["config"]["compact_width"] is False
    assert 8 <= len(card["config"]["summary"]["content"]) <= 60
    assert card["config"]["style"]["color"] == soft_color_tokens()
    assert all(button.get("type") not in {"primary", "primary_filled", "primary_text"}
               for button in _buttons(card))
    for node in _walk(card):
        if node.get("tag") == "column" and node.get("width") == "weighted":
            assert node["weight"] == 1
        if node.get("tag") == "collapsible_panel":
            assert node["header"]["icon"] == {"tag": "standard_icon", "token": "down_outlined", "color": "grey"}
            assert node["header"]["icon_position"] == "right"
            assert node["header"]["icon_expanded_angle"] == -180
        for key in ("background_style", "text_color"):
            value = node.get(key, "")
            if value.startswith("codex_"):
                assert value in card["config"]["style"]["color"]
