#!/usr/bin/env python3
"""测试 Codex Parser 新增的亮色判断和输入区域检测函数"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pyte
from pyte.screens import Char

from server.parsers.codex_parser import (
    _is_bright_color,
    _is_lit_prompt_row,
    _get_row_dominant_bg,
    _has_row_bg,
    _has_full_row_bg,
    CODEX_PROMPT_CHARS,
)


def create_test_screen():
    """创建一个测试用的 pyte Screen"""
    screen = pyte.Screen(220, 50)
    return screen


def fill_row_bg(screen, row, bg_color, cols=220):
    """填充整行的背景色"""
    for col in range(cols):
        set_cell_color(screen, row, col, ' ', bg=bg_color)


def set_cell_color(screen, row, col, char, fg=None, bg=None):
    """设置指定位置的字符颜色（需要模拟 pyte Char 结构）"""
    # pyte 的 Char 结构包含 data, fg, bg 等属性
    # 使用 pyte.screens.Char 创建新字符并写入 screen.buffer
    try:
        new_char = Char(data=char, fg=fg, bg=bg)
        screen.buffer[row][col] = new_char
    except (KeyError, IndexError):
        pass


def test_is_bright_color():
    """测试 _is_bright_color 函数"""
    print("=== 测试 _is_bright_color ===\n")

    # 测试 'default'
    assert not _is_bright_color('default'), "default 应该是非亮色"
    assert not _is_bright_color(''), "空字符串应该是非亮色"
    print("✓ 'default' 和空字符串测试通过")

    # 测试 bright colors
    for color in ['brightred', 'brightgreen', 'brightblue', 'brightyellow',
                  'brightmagenta', 'brightcyan', 'brightwhite', 'brightblack']:
        assert _is_bright_color(color), f"{color} 应该是亮色"
    print("✓ bright colors 测试通过")

    # 测试标准 colors（暗色）
    for color in ['red', 'green', 'blue', 'yellow',
                  'magenta', 'cyan', 'white', 'black', 'brown']:
        assert not _is_bright_color(color), f"{color} 应该是暗色"
    print("✓ 标准 colors 测试通过")

    # 测试 hex 颜色
    # 暗色（亮度 < 128）
    assert not _is_bright_color('404040'), "暗灰色应该是非亮色"
    assert not _is_bright_color('2a2a2a'), "深灰色应该是非亮色"
    print("✓ 暗色 hex 颜色测试通过")

    # 亮色（亮度 > 128）
    assert _is_bright_color('ffffff'), "白色应该是亮色"
    assert _is_bright_color('00ff00'), "绿色应该是亮色（亮度约182）"
    assert _is_bright_color('ffff00'), "黄色应该是亮色（亮度约226）"
    assert _is_bright_color('00ffff'), "青色应该是亮色（亮度约200）"
    # 红色、蓝色、品红亮度较低（不含绿色分量）
    assert not _is_bright_color('ff0000'), "红色应该是暗色（亮度约54）"
    assert not _is_bright_color('0000ff'), "蓝色应该是暗色（亮度约18）"
    assert not _is_bright_color('ff00ff'), "品红应该是暗色（亮度约73）"
    print("✓ 亮色/暗色 hex 颜色测试通过")

    # 中等亮度
    assert not _is_bright_color('808080'), "中等灰色应该是暗色（刚好 128，不大于）"
    assert _is_bright_color('818181'), "稍亮于中等的灰色应该是亮色（亮度约129）"
    print("✓ 中等亮度颜色测试通过")

    print("\n✅ 所有 _is_bright_color 测试通过\n")


def test_is_lit_prompt_row():
    """测试 _is_lit_prompt_row 函数"""
    print("=== 测试 _is_lit_prompt_row ===\n")

    # 测试正常情况
    print("  测试正常情况...")
    screen = create_test_screen()
    # 写入上分割线
    fill_row_bg(screen, 10, '2a2a2a')
    # 写入当前行（亮色 › + 文字）
    set_cell_color(screen, 11, 0, '›', fg='brightgreen', bg='2a2a2a')
    text = "current input text"
    for col, char in enumerate(text, start=1):
        set_cell_color(screen, 11, col, char, bg='2a2a2a')
    for col in range(len(text) + 1, 220):
        set_cell_color(screen, 11, col, ' ', bg='2a2a2a')
    # 写入下分割线
    fill_row_bg(screen, 12, '2a2a2a')

    assert _is_lit_prompt_row(screen, 11), "正常的亮色 › 行应该被识别"
    assert not _is_lit_prompt_row(screen, 10), "非 › 行不应该被识别"
    assert not _is_lit_prompt_row(screen, 12), "非 › 行不应该被识别"
    print("✓ 正常亮色 › 行识别通过\n")

    # 测试暗色前景（历史 InputBlock）
    print("  测试暗色前景（历史 InputBlock）...")
    screen = create_test_screen()
    fill_row_bg(screen, 10, '2a2a2a')
    set_cell_color(screen, 11, 0, '›', fg='green', bg='2a2a2a')  # 暗色
    for col in range(1, 220):
        set_cell_color(screen, 11, col, ' ', bg='2a2a2a')
    fill_row_bg(screen, 12, '2a2a2a')
    assert not _is_lit_prompt_row(screen, 11), "暗色 › 行不应该被识别"
    print("✓ 暗色 › 行（历史 InputBlock）测试通过\n")

    # 测试缺少上分割线
    print("  测试缺少上分割线...")
    screen = create_test_screen()
    set_cell_color(screen, 11, 0, '›', fg='brightgreen', bg='2a2a2a')
    for col in range(1, 220):
        set_cell_color(screen, 11, col, ' ', bg='2a2a2a')
    fill_row_bg(screen, 12, '2a2a2a')
    assert not _is_lit_prompt_row(screen, 11), "缺少上分割线时不应该被识别"
    print("✓ 缺少上分割线测试通过\n")

    # 测试缺少下分割线
    print("  测试缺少下分割线...")
    screen = create_test_screen()
    fill_row_bg(screen, 10, '2a2a2a')
    set_cell_color(screen, 11, 0, '›', fg='brightgreen', bg='2a2a2a')
    for col in range(1, 220):
        set_cell_color(screen, 11, col, ' ', bg='2a2a2a')
    assert not _is_lit_prompt_row(screen, 11), "缺少下分割线时不应该被识别"
    print("✓ 缺少下分割线测试通过\n")

    # 测试行本身无背景色
    print("  测试行本身无背景色...")
    screen = create_test_screen()
    fill_row_bg(screen, 10, '2a2a2a')
    set_cell_color(screen, 11, 0, '›', fg='brightgreen')  # 无背景色
    for col in range(1, 220):
        set_cell_color(screen, 11, col, ' ')
    fill_row_bg(screen, 12, '2a2a2a')
    assert not _is_lit_prompt_row(screen, 11), "行本身无背景色时不应该被识别"
    print("✓ 行本身无背景色测试通过\n")

    # 测试边界情况：屏幕顶部
    print("  测试边界情况...")
    screen = create_test_screen()
    set_cell_color(screen, 0, 0, '›', fg='brightgreen', bg='2a2a2a')
    for col in range(1, 220):
        set_cell_color(screen, 0, col, ' ', bg='2a2a2a')
    fill_row_bg(screen, 1, '2a2a2a')
    assert not _is_lit_prompt_row(screen, 0), "屏幕顶部不应该被识别"
    print("✓ 屏幕顶部测试通过")

    # 测试边界情况：屏幕底部
    screen = create_test_screen()
    fill_row_bg(screen, 48, '2a2a2a')
    set_cell_color(screen, 49, 0, '›', fg='brightgreen', bg='2a2a2a')
    for col in range(1, 220):
        set_cell_color(screen, 49, col, ' ', bg='2a2a2a')
    assert not _is_lit_prompt_row(screen, 49), "屏幕底部不应该被识别"
    print("✓ 屏幕底部测试通过\n")

    print("✅ 所有 _is_lit_prompt_row 测试通过\n")


def test_edge_cases():
    """测试边界情况"""
    print("=== 测试边界情况 ===\n")

    screen = create_test_screen()

    # 测试空屏幕
    assert not _is_lit_prompt_row(screen, 25), "空屏幕不应该识别"
    print("✓ 空屏幕测试通过")

    # 测试异常颜色值
    assert not _is_bright_color(None), "None 应该是非亮色"
    # 无效字符串但非 default，默认判定为亮色
    assert _is_bright_color('invalid'), "无效颜色字符串默认判定为亮色"
    # 两位 hex 默认判定为亮色
    assert _is_bright_color('00'), "两位 hex 应该是亮色（默认）"
    print("✓ 异常颜色值测试通过")

    # 测试部分背景色行（不是整行都有背景色）
    set_cell_color(screen, 10, 0, ' ', bg='2a2a2a')
    set_cell_color(screen, 11, 0, '›', fg='brightgreen', bg='2a2a2a')
    set_cell_color(screen, 12, 0, ' ', bg='2a2a2a')
    assert not _is_lit_prompt_row(screen, 11), "部分背景色行不应该被识别"
    print("✓ 部分背景色行测试通过")

    print("\n✅ 所有边界情况测试通过\n")


if __name__ == '__main__':
    test_is_bright_color()
    test_is_lit_prompt_row()
    test_edge_cases()

    print("=" * 50)
    print("🎉 所有测试通过！")
    print("=" * 50)
