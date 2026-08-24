"""终端屏幕解析器包

提供可插拔的解析器架构，支持 Claude CLI 和 Codex CLI（及未来其他 CLI 工具）。

使用方法：
    from parsers import ClaudeParser, CodexParser, BaseParser
"""

from .base_parser import BaseParser
from .claude_parser import ClaudeParser, ScreenParser  # ScreenParser 为向后兼容别名
from .codex_parser import CodexParser

__all__ = ['BaseParser', 'ClaudeParser', 'CodexParser', 'ScreenParser']
