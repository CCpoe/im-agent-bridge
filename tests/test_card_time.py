import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lark_client.card_time import format_beijing_time
from lark_client.desktop_card import (
    build_desktop_completion_card,
    build_desktop_list_card,
)


@pytest.mark.parametrize(("value", "expected"), [
    ("2026-09-07T14:20:10.566Z", "2026-09-07 22:20:10（北京时间）"),
    ("2026-09-07T16:20:10Z", "2026-09-08 00:20:10（北京时间）"),
    ("2026-12-31T23:59:59.999999+00:00", "2027-01-01 07:59:59（北京时间）"),
    ("2026-09-07T22:20:10.566+08:00", "2026-09-07 22:20:10（北京时间）"),
    ("2026-09-07T10:20:10-04:00", "2026-09-07 22:20:10（北京时间）"),
    ("2026-09-07T19:50:10+05:30", "2026-09-07 22:20:10（北京时间）"),
    ("2026-01-01T00:00:00+14:00", "2025-12-31 18:00:00（北京时间）"),
    ("2024-02-29T16:00:00Z", "2024-03-01 00:00:00（北京时间）"),
    ("2026-01-07T14:20:10Z", "2026-01-07 22:20:10（北京时间）"),
    ("2026-07-07T14:20:10Z", "2026-07-07 22:20:10（北京时间）"),
    ("0001-01-01T00:00:00+08:00", "0001-01-01 00:00:00（北京时间）"),
    ("0001-01-01T01:00:00+09:00", "0001-01-01 00:00:00（北京时间）"),
])
def test_format_beijing_time_uses_fixed_utc_plus_eight(value, expected):
    assert format_beijing_time(value) == expected


@pytest.mark.parametrize("value", [
    None,
    "",
    "   ",
    True,
    0,
    1788780810.566,
    {},
    {"timestamp": "2026-09-07T14:20:10Z"},
    [],
    ["2026-09-07T14:20:10Z"],
    b"2026-09-07T14:20:10Z",
    "2026-09-07",
    "2026-09-07T14:20:10.566",
    "2026-09-07 14:20:10",
    "09-07 14:20",
    "2026-02-29T14:20:10Z",
    "2026-13-07T14:20:10Z",
    "2026-09-31T14:20:10Z",
    "2026-09-07T25:20:10Z",
    "2026-09-07T14:20:60Z",
    "2026-09-07T14:20:10+25:00",
    "2026-09-07T14:20:10+00:60",
    "2026-09-07T14:20:10+0060",
    "2026-09-07T14:20:10+00:00:60",
    "2026-09-07T14:20:10Z<font color='red'>无效</font>",
    "not-a-date",
    "9999-12-31T16:00:00Z",
    "0001-01-01T00:00:00+14:00",
])
def test_format_beijing_time_rejects_invalid_or_ambiguous_values(value):
    assert format_beijing_time(value) is None


@pytest.mark.parametrize("process_timezone", ["UTC0", "EST5", "JST-9"])
def test_format_beijing_time_is_independent_of_host_timezone(process_timezone):
    # 仅对子进程设置 TZ，不触碰系统设置或当前测试进程的时区。
    environment = dict(os.environ, TZ=process_timezone)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import time; "
            "time.tzset() if hasattr(time, 'tzset') else None; "
            "from lark_client.card_time import format_beijing_time; "
            "print(format_beijing_time('2026-09-07T14:20:10.566Z'))",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "2026-09-07 22:20:10（北京时间）"


@pytest.mark.parametrize(("outcome", "status_label"), [
    ("completed", "执行完成"),
    ("failed", "执行失败"),
])
def test_completion_card_renders_beijing_time_without_mutating_event(outcome, status_label):
    event = {
        "thread_id": "thread-time-test",
        "title": "北京时间展示测试",
        "outcome": outcome,
        "completed_at": "2026-09-07T14:20:10.566Z",
        "metadata": {"original_timestamp": "2026-09-07T14:20:10.566Z"},
    }
    original = copy.deepcopy(event)

    card = build_desktop_completion_card(event)
    rendered = json.dumps(card, ensure_ascii=False)

    assert "时间：2026-09-07 22:20:10（北京时间）" in rendered
    assert rendered.count("北京时间）") == 1
    assert "2026-09-07T14:20:10.566Z" not in rendered
    assert ".566" not in rendered
    assert status_label in rendered
    assert event == original


@pytest.mark.parametrize("archived", [False, True])
def test_list_card_renders_beijing_time_without_mutating_or_reordering_threads(archived):
    threads = [
        {
            "thread_id": "thread-earlier",
            "title": "已带北京时间偏移",
            "status": "completed",
            "updated_at": "2026-09-07T22:20:10.566+08:00",
        },
        {
            "thread_id": "thread-later",
            "title": "UTC 跨日",
            "status": "running",
            "updated_at": "2026-09-07T16:00:00Z",
        },
    ]
    original = copy.deepcopy(threads)

    card = build_desktop_list_card(threads, archived=archived)
    rendered = json.dumps(card, ensure_ascii=False)

    assert "更新：2026-09-07 22:20:10（北京时间）" in rendered
    assert "更新：2026-09-08 00:00:00（北京时间）" in rendered
    assert rendered.count("北京时间）") == 2
    assert "2026-09-07T22:20:10.566+08:00" not in rendered
    assert "2026-09-07T16:00:00Z" not in rendered
    assert rendered.index("已带北京时间偏移") < rendered.index("UTC 跨日")
    assert ("恢复并进入" if archived else "进入任务") in rendered
    assert threads == original


@pytest.mark.parametrize("value", [
    None,
    "",
    "2026-09-07T14:20:10.566",
    "invalid-timestamp",
    {"private": "PRIVATE_TIME_PAYLOAD"},
    "9999-12-31T16:00:00Z",
])
@pytest.mark.parametrize("kind", ["completed", "failed", "recent", "archived"])
def test_cards_omit_invalid_time_instead_of_showing_raw_value(kind, value):
    source = {"thread_id": "thread-invalid-time", "title": "时间异常测试"}
    if kind in ("completed", "failed"):
        source.update(outcome=kind, completed_at=value)
        original = copy.deepcopy(source)
        card = build_desktop_completion_card(source)
        time_label = "时间："
    else:
        source["updated_at"] = value
        original = copy.deepcopy(source)
        card = build_desktop_list_card([source], archived=kind == "archived")
        time_label = "更新："
    rendered = json.dumps(card, ensure_ascii=False)

    assert time_label not in rendered
    assert "北京时间）" not in rendered
    assert "PRIVATE_TIME_PAYLOAD" not in rendered
    if isinstance(value, str) and value:
        assert value not in rendered
    assert source == original
