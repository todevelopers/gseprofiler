"""Serializers converting recorded profile events to external viewer formats.

Both exporters consume the raw event list held by the profiler view
(schema: ``{type, extensionUuid, function, start, end, depth}`` with
``start``/``end`` in seconds) and are pure functions — no GTK or I/O.
"""

import logging
from typing import Any

_log = logging.getLogger(__name__)

_SPEEDSCOPE_SCHEMA = "https://www.speedscope.app/file-format-schema.json"


def _valid_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop events without a usable name or a non-negative time span."""
    out = []
    for e in events:
        start = e.get("start")
        end = e.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if end < start:
            continue
        out.append(e)
    return out


def to_speedscope(
    events: list[dict[str, Any]], *, name: str = "gse-profiler profile"
) -> dict[str, Any]:
    """Convert raw profile events to the speedscope evented file format.

    Complete spans are rebuilt into open/close ("O"/"C") transitions using
    a stack: events sorted by (start, -end) open in caller-before-callee
    order, and a child's end is clamped to its parent's so float fuzz in
    the recorded timestamps can never produce ill-nested output.
    """
    valid = _valid_events(events)
    if not valid:
        return {
            "$schema": _SPEEDSCOPE_SCHEMA,
            "shared": {"frames": []},
            "profiles": [
                {
                    "type": "evented",
                    "name": name,
                    "unit": "seconds",
                    "startValue": 0.0,
                    "endValue": 0.0,
                    "events": [],
                }
            ],
            "name": name,
            "exporter": "gse-profiler",
        }

    ordered = sorted(valid, key=lambda e: (e["start"], -e["end"]))
    t0 = ordered[0]["start"]

    frames: list[dict[str, str]] = []
    frame_index: dict[str, int] = {}
    for e in ordered:
        fn = str(e.get("function", "?"))
        if fn not in frame_index:
            frame_index[fn] = len(frames)
            frames.append({"name": fn})

    out_events: list[dict[str, Any]] = []
    # Stack of (end, frame_index) for currently open spans.
    stack: list[tuple[float, int]] = []
    for e in ordered:
        start = e["start"] - t0
        end = e["end"] - t0
        while stack and stack[-1][0] <= start:
            closed_end, closed_frame = stack.pop()
            out_events.append({"type": "C", "frame": closed_frame, "at": closed_end})
        if stack and end > stack[-1][0]:
            end = stack[-1][0]
        idx = frame_index[str(e.get("function", "?"))]
        out_events.append({"type": "O", "frame": idx, "at": start})
        stack.append((end, idx))
    while stack:
        closed_end, closed_frame = stack.pop()
        out_events.append({"type": "C", "frame": closed_frame, "at": closed_end})

    end_value = max(e["end"] for e in ordered) - t0
    return {
        "$schema": _SPEEDSCOPE_SCHEMA,
        "shared": {"frames": frames},
        "profiles": [
            {
                "type": "evented",
                "name": name,
                "unit": "seconds",
                "startValue": 0.0,
                "endValue": end_value,
                "events": out_events,
            }
        ],
        "name": name,
        "exporter": "gse-profiler",
    }


def to_trace_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert raw profile events to the Chrome Trace Event format.

    Emits complete ("X") events with microsecond timestamps on a single
    pid/tid; viewers (Firefox Profiler, Perfetto, speedscope) rebuild the
    call nesting from ts/dur containment.
    """
    valid = _valid_events(events)
    t0 = min((e["start"] for e in valid), default=0.0)
    trace_events = [
        {
            "name": str(e.get("function", "?")),
            "cat": str(e.get("extensionUuid", "extension")),
            "ph": "X",
            "ts": (e["start"] - t0) * 1e6,
            "dur": (e["end"] - e["start"]) * 1e6,
            "pid": 1,
            "tid": 1,
        }
        for e in sorted(valid, key=lambda e: (e["start"], -e["end"]))
    ]
    return {"traceEvents": trace_events, "displayTimeUnit": "ms"}
