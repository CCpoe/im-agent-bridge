import json
import os
from pathlib import Path

import pytest

import lark_client.desktop_subagents as module
from lark_client.desktop_subagents import SubagentStatusReader


def line(record):
    return json.dumps(record).encode() + b"\n"


def metadata(child="child-1", root="root-1", **overrides):
    payload = {
        "id": child,
        "session_id": root,
        "parent_thread_id": root,
        "thread_source": "subagent",
        "subagent_history_start_ordinal": 7,
    }
    payload.update(overrides)
    return {"ordinal": 0, "type": "session_meta", "payload": payload}


def event(kind, ordinal=8, **fields):
    return {
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {"type": kind, "turn_id": "child-turn-1", **fields},
    }


def rollout(tmp_path, records, child="child-1", meta=None, dated=False):
    directory = tmp_path / "2026" / "09" / "07" if dated else tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("rollout-2026-09-07T20-00-00-{}.jsonl".format(child))
    path.write_bytes(line(meta or metadata(child=child)) + b"".join(map(line, records)))
    return path


@pytest.mark.parametrize("kind,fields,expected", [
    ("task_started", {}, "running"),
    ("task_complete", {}, "completed"),
    ("task_complete", {"error": None}, "completed"),
    ("task_complete", {"error": {}}, "failed"),
    ("task_complete", {"error": {"message": "PRIVATE_ERROR"}}, "failed"),
    ("turn_aborted", {}, "interrupted"),
])
def test_reads_explicit_child_lifecycle(tmp_path, kind, fields, expected):
    rollout(tmp_path, [event(kind, **fields)], dated=True)
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") == expected
    assert "PRIVATE_ERROR" not in repr(reader._cache)


def test_ordinal_is_record_field_not_line_number(tmp_path):
    path = rollout(tmp_path, [
        event("task_complete", 50),
        event("task_started", 100),
    ], meta=metadata(subagent_history_start_ordinal=100))
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") is None
    with path.open("ab") as target:
        target.write(line(event("task_started", 101)))
    assert reader.get_status("child-1", "root-1") == "running"


def test_restarted_child_overrides_previous_completed(tmp_path):
    rollout(tmp_path, [
        event("task_complete", 2, turn_id="copied-root-turn"),
        event("task_started", 8),
        event("task_complete", 9),
        event("task_started", 54, turn_id="new-child-turn"),
    ])
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") == "running"


@pytest.mark.parametrize("boundary", [None, -1, True, "7", 7.5])
def test_unknown_history_boundary_is_not_guessed(tmp_path, boundary):
    rollout(tmp_path, [event("task_started")], meta=metadata(subagent_history_start_ordinal=boundary))
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None


@pytest.mark.parametrize("overrides", [
    {"id": "different-child"},
    {"parent_thread_id": "other-root", "session_id": "other-root"},
    {"parent_thread_id": None, "thread_source": "user", "source": "vscode"},
])
def test_rejects_wrong_identity_or_unproven_root(tmp_path, overrides):
    rollout(tmp_path, [event("task_started")], meta=metadata(**overrides))
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None


@pytest.mark.parametrize("source", [
    {"subagent": {"thread_spawn": {"parent_thread_id": "root-1"}}},
    json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root-1"}}}),
    "subAgentThreadSpawn",
])
def test_session_id_requires_explicit_subagent_marker(tmp_path, source):
    rollout(tmp_path, [event("task_started")], meta=metadata(
        parent_thread_id=None, thread_source=None, source=source,
    ))
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") == "running"


@pytest.mark.parametrize("child,root", [
    ("../child-1", "root-1"), ("child-1", "../root-1"),
    ("child-1\x00", "root-1"), (None, "root-1"),
    ("root-1", "root-1"), ("x" * 129, "root-1"),
])
def test_invalid_ids_do_not_search(tmp_path, monkeypatch, child, root):
    reader = SubagentStatusReader(tmp_path)
    monkeypatch.setattr(reader, "_find_path", lambda *args: pytest.fail("不应查找路径"))
    assert reader.get_status(child, root) is None


def test_symlink_rollout_is_not_read(tmp_path):
    external = tmp_path / "external"
    path = rollout(external, [event("task_started")])
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / path.name).symlink_to(path)
    assert SubagentStatusReader(sessions).get_status("child-1", "root-1") is None


def test_symlink_directory_is_not_followed(tmp_path):
    external = tmp_path / "external"
    rollout(external, [event("task_started")])
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "2026").symlink_to(external, target_is_directory=True)
    assert SubagentStatusReader(sessions).get_status("child-1", "root-1") is None


def test_cache_reuses_stat_and_invalidates_for_append_truncate_replace(tmp_path, monkeypatch):
    path = rollout(tmp_path, [event("task_started")])
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") == "running"
    original = module._read_status
    monkeypatch.setattr(module, "_read_status", lambda *args: pytest.fail("应复用 stat 缓存"))
    assert reader.get_status("child-1", "root-1") == "running"
    monkeypatch.setattr(module, "_read_status", original)
    with path.open("ab") as target:
        target.write(line(event("task_complete", 9)))
    assert reader.get_status("child-1", "root-1") == "completed"
    path.write_bytes(line(metadata()) + line(event("turn_aborted", 10)))
    assert reader.get_status("child-1", "root-1") == "interrupted"
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(line(metadata()) + line(event("task_complete", 11, error={})))
    os.replace(replacement, path)
    assert reader.get_status("child-1", "root-1") == "failed"


def test_cache_is_root_specific_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_CACHE_ENTRIES", 3)
    for number in range(4):
        rollout(tmp_path, [event("task_started")], child="child-{}".format(number))
    reader = SubagentStatusReader(tmp_path)
    for number in range(4):
        assert reader.get_status("child-{}".format(number), "root-1") == "running"
    assert len(reader._cache) == 3
    assert ("child-0", "root-1") not in reader._cache
    assert reader.get_status("child-3", "other-root") is None


def test_missing_path_lookup_is_throttled_and_retried(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") is None
    rollout(tmp_path, [event("task_started")])
    assert reader.get_status("child-1", "root-1") is None
    clock[0] += module.MISSING_PATH_RETRY_SECONDS
    assert reader.get_status("child-1", "root-1") == "running"


def test_lookup_has_entry_budget(tmp_path, monkeypatch):
    rollout(tmp_path, [event("task_started")], dated=True)
    monkeypatch.setattr(module, "MAX_LOOKUP_ENTRIES", 2)
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None


def test_huge_completion_returns_unknown_not_stale_running(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_LINE_BYTES", 256)
    rollout(tmp_path, [
        event("task_started"),
        event("task_complete", 9, last_agent_message="PRIVATE_OUTPUT" * 500),
    ])
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") is None
    assert "PRIVATE_OUTPUT" not in repr(reader._cache)


def test_completion_larger_than_scan_window_returns_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_SCAN_BYTES", 512)
    rollout(tmp_path, [
        event("task_started"),
        event("task_complete", 9, last_agent_message="PRIVATE_OUTPUT" * 500),
    ])
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None


def test_tail_window_discards_partial_first_line_but_keeps_later_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_SCAN_BYTES", 512)
    rollout(tmp_path, [
        {"ordinal": 8, "type": "response_item", "payload": {"text": "PRIVATE" * 500}},
        event("task_started", 9),
    ])
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") == "running"


def test_tail_window_keeps_record_exactly_at_line_boundary(tmp_path, monkeypatch):
    latest = event("task_started", 9)
    monkeypatch.setattr(module, "MAX_SCAN_BYTES", len(line(latest)))
    rollout(tmp_path, [event("task_complete", 8), latest])
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") == "running"


def test_partial_completion_never_preserves_old_running(tmp_path):
    path = rollout(tmp_path, [event("task_started")])
    completion = line(event("task_complete", 9))
    with path.open("ab") as target:
        target.write(completion[:-1])
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") is None
    with path.open("ab") as target:
        target.write(b"\n")
    assert reader.get_status("child-1", "root-1") == "completed"


@pytest.mark.parametrize("record", [
    event("task_unknown", 9),
    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "child-turn-1"}},
    event("task_complete", True),
    event("task_complete", 9, turn_id=None),
])
def test_unknown_lifecycle_does_not_preserve_previous_status(tmp_path, record):
    rollout(tmp_path, [event("task_started"), record])
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None


def test_non_lifecycle_records_do_not_change_status_or_leak_text(tmp_path):
    rollout(tmp_path, [
        event("task_started"),
        {"ordinal": 9, "type": "response_item", "payload": {"text": "PRIVATE_OUTPUT"}},
        {"ordinal": 10, "type": "event_msg", "payload": {"type": "item_completed", "item": {"text": "PRIVATE_OUTPUT"}}},
    ])
    reader = SubagentStatusReader(tmp_path)
    assert reader.get_status("child-1", "root-1") == "running"
    assert "PRIVATE_OUTPUT" not in repr(reader._cache)


def test_incomplete_or_oversized_metadata_returns_unknown(tmp_path, monkeypatch):
    path = rollout(tmp_path, [event("task_started")])
    monkeypatch.setattr(module, "MAX_METADATA_BYTES", 10)
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None
    monkeypatch.setattr(module, "MAX_METADATA_BYTES", 1024)
    path.write_bytes(line(metadata())[:-1])
    assert SubagentStatusReader(tmp_path).get_status("child-1", "root-1") is None
