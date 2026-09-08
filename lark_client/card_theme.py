"""Desktop 与 CLI 卡片共用的低饱和状态色和操作色。"""

from __future__ import annotations

from typing import Any, Dict, Tuple


# 与用户确认的 soft-palette-card.json 一致；每个 token 独立定义浅/深色。
_PALETTE = {
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


def _rgba(value: str) -> str:
    return "rgba({},{},{},1)".format(*(int(value[index:index + 2], 16) for index in (0, 2, 4)))


def soft_color_tokens() -> Dict[str, Dict[str, str]]:
    """每次返回独立字典，调用方修改某张卡时不会污染其他卡片。"""

    return {
        name: {"light_mode": _rgba(light), "dark_mode": _rgba(dark)}
        for name, (light, dark) in _PALETTE.items()
    }


def status_tone(status: Any) -> str:
    """未知/中性状态保持灰色，不从标题或用户文本猜测状态。"""

    if not isinstance(status, str):
        return "neutral"
    return {
        "running": "running", "active": "running", "inProgress": "running",
        "completed": "success",
        "waiting_approval": "waiting", "waiting_input": "waiting",
        "failed": "failure", "errored": "failure", "systemError": "failure",
    }.get(status, "neutral")


def status_color_tokens(status: Any) -> Tuple[str, str]:
    tone = status_tone(status)
    return "codex_status_{}_bg".format(tone), "codex_status_{}_text".format(tone)


__all__ = ["soft_color_tokens", "status_tone", "status_color_tokens"]
