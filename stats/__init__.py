"""
IM Agent Bridge 本地使用统计模块

全局接口：
  track(category, event, **kwargs)   —— 记录事件
  close()                            —— 关闭前刷新
"""

import os


_ENABLED = True
_MIXPANEL_TOKEN = os.environ.get("IM_AGENT_BRIDGE_MIXPANEL_TOKEN", "").strip()

from .collector import StatsCollector

_collector = StatsCollector(enabled=_ENABLED)
# 远程遥测默认关闭；只有显式提供 token 时才启用。
if _MIXPANEL_TOKEN:
    _collector.set_mixpanel_token(_MIXPANEL_TOKEN)
    _collector.report_install()


def track(category: str, event: str, **kwargs) -> None:
    """记录事件（非阻塞，线程安全，异常不传播）"""
    _collector.track(category, event, **kwargs)


def init_mixpanel(token: str) -> None:
    """显式启用 Mixpanel 上报（保留接口兼容性）。"""
    _collector.set_mixpanel_token(token)
    _collector.report_install()


def report_daily(date: str = None) -> None:
    """手动触发聚合上报（CLI --report 命令使用）"""
    _collector.report_daily(date)


def close() -> None:
    """关闭前刷新队列"""
    _collector.close()
