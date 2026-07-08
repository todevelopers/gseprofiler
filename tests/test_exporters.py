"""Unit tests for the speedscope / trace-event exporters (pure Python, no gi)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.exporters import to_speedscope, to_trace_event


def _ev(fn: str, start: float, end: float, depth: int = 0) -> dict:
    return {
        "type": "profile_event",
        "extensionUuid": "demo@example",
        "function": fn,
        "start": start,
        "end": end,
        "depth": depth,
    }


# ── speedscope ────────────────────────────────────────────────────────────


def test_speedscope_empty() -> None:
    out = to_speedscope([])
    assert out["shared"]["frames"] == []
    profile = out["profiles"][0]
    assert profile["events"] == []
    assert profile["startValue"] == 0.0
    assert profile["endValue"] == 0.0


def test_speedscope_schema_fields() -> None:
    out = to_speedscope([_ev("foo", 1.0, 2.0)], name="my-ext@example")
    assert out["$schema"] == "https://www.speedscope.app/file-format-schema.json"
    assert out["exporter"] == "gse-profiler"
    profile = out["profiles"][0]
    assert profile["type"] == "evented"
    assert profile["unit"] == "seconds"
    assert profile["name"] == "my-ext@example"


def test_speedscope_single_event() -> None:
    out = to_speedscope([_ev("foo", 5.0, 5.25)])
    assert out["shared"]["frames"] == [{"name": "foo"}]
    profile = out["profiles"][0]
    # Times are normalized to t0.
    assert profile["events"] == [
        {"type": "O", "frame": 0, "at": 0.0},
        {"type": "C", "frame": 0, "at": 0.25},
    ]
    assert profile["endValue"] == 0.25


def test_speedscope_nested_events() -> None:
    events = [
        _ev("child", 1.1, 1.4, depth=1),
        _ev("parent", 1.0, 2.0, depth=0),
    ]
    out = to_speedscope(events)
    profile = out["profiles"][0]
    names = {i: f["name"] for i, f in enumerate(out["shared"]["frames"])}
    seq = [(e["type"], names[e["frame"]], e["at"]) for e in profile["events"]]
    assert seq == [
        ("O", "parent", 0.0),
        ("O", "child", pytest.approx(0.1)),
        ("C", "child", pytest.approx(0.4)),
        ("C", "parent", 1.0),
    ]


def test_speedscope_sibling_events() -> None:
    events = [_ev("a", 0.0, 1.0), _ev("b", 2.0, 3.0)]
    out = to_speedscope(events)
    profile = out["profiles"][0]
    seq = [(e["type"], e["at"]) for e in profile["events"]]
    assert seq == [("O", 0.0), ("C", 1.0), ("O", 2.0), ("C", 3.0)]


def test_speedscope_open_close_balanced_and_monotonic() -> None:
    events = [
        _ev("root", 0.0, 10.0, depth=0),
        _ev("a", 1.0, 4.0, depth=1),
        _ev("leaf", 2.0, 3.0, depth=2),
        _ev("b", 5.0, 9.0, depth=1),
        _ev("root", 12.0, 15.0, depth=0),
    ]
    out = to_speedscope(events)
    stream = out["profiles"][0]["events"]
    opens = sum(1 for e in stream if e["type"] == "O")
    closes = sum(1 for e in stream if e["type"] == "C")
    assert opens == closes == len(events)
    ats = [e["at"] for e in stream]
    assert ats == sorted(ats)


def test_speedscope_clamps_float_fuzz_child_end() -> None:
    # Child overshoots parent's end by float fuzz — must be clamped so the
    # emitted stream stays well-nested.
    events = [
        _ev("parent", 0.0, 1.0, depth=0),
        _ev("child", 0.5, 1.0000001, depth=1),
    ]
    out = to_speedscope(events)
    stream = out["profiles"][0]["events"]
    names = {i: f["name"] for i, f in enumerate(out["shared"]["frames"])}
    seq = [(e["type"], names[e["frame"]]) for e in stream]
    assert seq == [("O", "parent"), ("O", "child"), ("C", "child"), ("C", "parent")]
    ats = [e["at"] for e in stream]
    assert ats == sorted(ats)


def test_speedscope_dedupes_frames() -> None:
    events = [_ev("foo", 0.0, 1.0), _ev("foo", 2.0, 3.0), _ev("bar", 4.0, 5.0)]
    out = to_speedscope(events)
    assert out["shared"]["frames"] == [{"name": "foo"}, {"name": "bar"}]


def test_speedscope_skips_invalid_events() -> None:
    events = [
        _ev("ok", 0.0, 1.0),
        {"function": "no-times"},
        _ev("negative-span", 5.0, 4.0),
    ]
    out = to_speedscope(events)
    assert out["shared"]["frames"] == [{"name": "ok"}]
    assert len(out["profiles"][0]["events"]) == 2


# ── trace event ───────────────────────────────────────────────────────────


def test_trace_event_empty() -> None:
    out = to_trace_event([])
    assert out["traceEvents"] == []


def test_trace_event_fields_and_units() -> None:
    out = to_trace_event([_ev("foo", 2.0, 2.5)])
    (te,) = out["traceEvents"]
    assert te["name"] == "foo"
    assert te["ph"] == "X"
    assert te["ts"] == 0.0  # normalized to t0
    assert te["dur"] == 500_000.0  # 0.5 s in µs
    assert te["pid"] == 1
    assert te["tid"] == 1
    assert te["cat"] == "demo@example"


def test_trace_event_sorted_and_normalized() -> None:
    events = [_ev("b", 3.0, 4.0), _ev("a", 1.0, 5.0)]
    out = to_trace_event(events)
    tes = out["traceEvents"]
    assert [te["name"] for te in tes] == ["a", "b"]
    assert tes[0]["ts"] == 0.0
    assert tes[1]["ts"] == 2_000_000.0


def test_trace_event_skips_invalid_events() -> None:
    out = to_trace_event([{"function": "no-times"}, _ev("ok", 0.0, 1.0)])
    assert [te["name"] for te in out["traceEvents"]] == ["ok"]
