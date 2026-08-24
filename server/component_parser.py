"""向后兼容 shim — 实现已迁移到 server/parsers/claude_parser.py

保留此文件以免破坏已有的 `from component_parser import ScreenParser` 导入。
新代码请直接从 parsers 包导入：

    from parsers import ClaudeParser, CodexParser, BaseParser
"""

from parsers.claude_parser import (  # noqa: F401
    ClaudeParser,
    ScreenParser,
    _parse_bottom_bar_agents,
    components_content_key,
    STAR_CHARS,
    DOT_CHARS,
    DIVIDER_CHARS,
    BOX_CORNER_TOP,
    BOX_CORNER_BOTTOM,
    BOX_VERTICAL,
    _get_row_text,
    _get_col0,
    _get_col0_blink,
    _get_row_ansi_text,
    _get_col0_ansi,
    _is_divider_row,
    _has_numbered_options,
    _find_contiguous_options,
    _char_style_parts,
    _fg_sgr,
    _bg_sgr,
    _strip_inline_boxes_pair,
)

__all__ = [
    'ScreenParser',
    'ClaudeParser',
    '_parse_bottom_bar_agents',
    'components_content_key',
]
