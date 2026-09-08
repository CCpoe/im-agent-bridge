"""固定用户批准的柔和配色，防止状态与操作重新共用亮蓝。"""

import re

import pytest

from lark_client.card_theme import soft_color_tokens, status_color_tokens, status_tone


# 与确认预览一致；不从实现常量反向生成期望值。
_APPROVED_HEX_PAIRS = {
    "codex_canvas": ("FAFBFC", "262B31"),
    "codex_body": ("FFFFFF", "2C3239"),
    "codex_panel": ("FFFFFF", "2C3239"),
    "codex_secondary": ("DDE4EA", "454F59"),
    "codex_ink": ("37434F", "D3D9DF"),
    "codex_muted": ("687582", "A5AFB9"),
    "codex_accent": ("EDF3EF", "303D35"),
    "codex_accent_2": ("EAF0F5", "34424E"),
    "codex_button": ("E5EDF3", "3B4B58"),
    "codex_button_text": ("486175", "CBD9E5"),
    "codex_button_border": ("CDD9E3", "506271"),
    "codex_button_secondary": ("EEF1F3", "383F47"),
    "codex_on_accent": ("37434F", "D3D9DF"),
    "codex_status_running_bg": ("E7EFF5", "344552"),
    "codex_status_running_text": ("48667D", "C1D2E0"),
    "codex_status_success_bg": ("E8F0EB", "35483C"),
    "codex_status_success_text": ("4D6C58", "C7D8C9"),
    "codex_status_waiting_bg": ("F5EFDF", "4B4436"),
    "codex_status_waiting_text": ("796642", "DACDAE"),
    "codex_status_failure_bg": ("F3E8E5", "4A3836"),
    "codex_status_failure_text": ("865D57", "DEC5C1"),
    "codex_status_neutral_bg": ("EEF1F3", "383F47"),
    "codex_status_neutral_text": ("626F7A", "C5CED6"),
}


def _rgba(hex_color):
    channels = [int(hex_color[index:index + 2], 16) for index in (0, 2, 4)]
    return "rgba({},{},{},1)".format(*channels)


def _luminance(rgba):
    match = re.fullmatch(r"rgba\((\d+),(\d+),(\d+),1\)", rgba)
    assert match is not None
    channels = [int(value) / 255 for value in match.groups()]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), linear))


def test_soft_palette_matches_approved_light_and_dark_values():
    expected = {
        name: {"light_mode": _rgba(light), "dark_mode": _rgba(dark)}
        for name, (light, dark) in _APPROVED_HEX_PAIRS.items()
    }
    colors = soft_color_tokens()

    assert colors == expected
    assert all(value["light_mode"] != value["dark_mode"] for value in colors.values())
    removed_colors = {"rgba(57,65,255,1)", "rgba(90,97,255,1)"}
    assert not removed_colors.intersection(
        rgba for modes in colors.values() for rgba in modes.values()
    )


def test_soft_palette_returns_independent_nested_tokens():
    first = soft_color_tokens()
    second = soft_color_tokens()
    assert first is not second
    assert all(first[name] is not second[name] for name in first)

    first["codex_button"]["light_mode"] = "rgba(0,0,0,1)"
    first["codex_body"]["dark_mode"] = "rgba(0,0,0,1)"
    first["unexpected"] = {}

    assert second == soft_color_tokens()
    assert "unexpected" not in second
    assert second["codex_button"]["light_mode"] == "rgba(229,237,243,1)"
    assert first["codex_panel"]["dark_mode"] == "rgba(44,50,57,1)"


@pytest.mark.parametrize(("status", "tone"), [
    ("running", "running"),
    ("active", "running"),
    ("inProgress", "running"),
    ("completed", "success"),
    ("waiting_approval", "waiting"),
    ("waiting_input", "waiting"),
    ("failed", "failure"),
    ("systemError", "failure"),
    ("errored", "failure"),
    ("idle", "neutral"),
    ("interrupted", "neutral"),
    ("unknown", "neutral"),
    ("archived", "neutral"),
    ("list", "neutral"),
    ("future_status", "neutral"),
    ("", "neutral"),
    (None, "neutral"),
    (True, "neutral"),
    (123, "neutral"),
    ({"type": "running"}, "neutral"),
    (["running"], "neutral"),
])
def test_status_tokens_preserve_semantics_and_unknown_values_fail_closed(status, tone):
    assert status_tone(status) == tone
    assert status_color_tokens(status) == (
        f"codex_status_{tone}_bg", f"codex_status_{tone}_text",
    )


@pytest.mark.parametrize("mode", ["light_mode", "dark_mode"])
@pytest.mark.parametrize(("background", "foreground"), [
    ("codex_button", "codex_button_text"),
    ("codex_status_running_bg", "codex_status_running_text"),
    ("codex_status_success_bg", "codex_status_success_text"),
    ("codex_status_waiting_bg", "codex_status_waiting_text"),
    ("codex_status_failure_bg", "codex_status_failure_text"),
    ("codex_status_neutral_bg", "codex_status_neutral_text"),
    ("codex_accent", "codex_status_success_text"),
    ("codex_accent_2", "codex_button_text"),
    ("codex_canvas", "codex_ink"),
])
def test_soft_backgrounds_keep_small_text_readable(mode, background, foreground):
    colors = soft_color_tokens()
    luminances = sorted((_luminance(colors[background][mode]), _luminance(colors[foreground][mode])))
    contrast = (luminances[1] + 0.05) / (luminances[0] + 0.05)
    assert contrast >= 4.5


def test_action_colors_are_distinct_from_every_status_pair():
    colors = soft_color_tokens()
    for tone in ("running", "success", "waiting", "failure", "neutral"):
        assert colors["codex_button"] != colors[f"codex_status_{tone}_bg"]
        assert colors["codex_button_text"] != colors[f"codex_status_{tone}_text"]
