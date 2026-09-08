"""卡片时间展示：显式时区转换为北京时间，不修改后台时间值。"""

from datetime import datetime, timedelta
import re
from typing import Any, Optional


_BEIJING_OFFSET = timedelta(hours=8)
_OFFSET_PARTS = re.compile(r"[+-](\d{2}):?(\d{2})(?::?(\d{2})(?:[.,]\d+)?)?$")


def format_beijing_time(value: Any) -> Optional[str]:
    """有明确时区的 ISO 时间转换为 UTC+8；缺失或无效值不猜测时区。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    offset_parts = _OFFSET_PARTS.search(text)
    if offset_parts and (
        int(offset_parts[1]) >= 24 or int(offset_parts[2]) >= 60
        or (offset_parts[3] is not None and int(offset_parts[3]) >= 60)
    ):
        return None
    try:
        instant = datetime.fromisoformat(text)
        offset = instant.utcoffset()
        if instant.tzinfo is None or offset is None:
            return None
        # 源时间带明确偏移；直接换算偏移差，避免极小年份经 UTC 中转溢出。
        local = instant.replace(tzinfo=None) + (_BEIJING_OFFSET - offset)
    except (ValueError, OverflowError):
        return None
    return (
        f"{local.year:04d}-{local.month:02d}-{local.day:02d} "
        f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}（北京时间）"
    )
