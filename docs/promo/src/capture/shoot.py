"""Capture every UI state the promo needs from the real application."""
import json
import random
import traceback
from datetime import datetime, timedelta

import harness as H  # must come first: it sets XDG and fontconfig env before GLib loads

from gi.repository import GLib, Gtk  # noqa: E402

P = "_indicator."
rnd = random.Random(7)
EVENTS: list[dict] = []


def ev(fn: str, start: float, dur_ms: float, depth: int) -> float:
    EVENTS.append({"type": "profile_event", "extensionUuid": H.RSS, "function": P + fn,
                   "start": start, "end": start + dur_ms / 1000.0, "depth": depth})
    return start + dur_ms / 1000.0


def poll(t: float) -> None:
    ev("_pollFeeds", t, 0.92, 0)
    ev("_getSettings", t + 0.00005, 0.31, 1)
    for i in range(4):
        ev("_httpGetRequestAsync", t + 0.0004 + i * 0.00013, 0.11 + rnd.random() * 0.03, 1)


def download(t: float) -> None:
    d = 5.2 + rnd.random() * 2.6
    ev("_onDownload", t, d, 0)
    pd = d - 0.9
    ev("_parseFeed", t + 0.0002, pd, 1)
    n = 6
    for i in range(n):
        ev("_sanitizeTitle", t + 0.0004 + i * (pd / 1000.0 - 0.0004) / n, 0.0006, 2)
    ev("_markMinimalDirty", t + (d - 0.55) / 1000.0, 0.035, 1)
    ev("_isMinimalLayout", t + (d - 0.54) / 1000.0, 0.0007, 2)
    ev("_updateUnreadCountLabel", t + (d - 0.4) / 1000.0, 0.046, 1)


def rebuild(t: float, items: int) -> None:
    per = 2.9
    total = 1.1 + items * (per + 0.18)
    ev("_flushMinimalRebuild", t, total + 0.3, 0)
    ev("_rebuildMinimalSection", t + 0.00012, total, 1)
    ev("_computeMinimalList", t + 0.0002, 0.28, 2)
    ev("_isMinimalLayout", t + 0.00022, 0.0008, 3)
    x = t + 0.0008
    for _ in range(items):
        d = per * (0.75 + rnd.random() * 0.5)
        ev("_createMenuItem", x, d, 2)
        ev("_formatDate", x + d * 0.0003, 0.021, 3)
        ev("_sanitizeTitle", x + d * 0.0006, 0.0006, 3)
        x += (d + 0.18) / 1000.0


def menu_open(t: float) -> None:
    ev("_onMenuOpenStateChanged", t, 1.24, 0)
    ev("_isMinimalLayout", t + 0.0001, 0.0007, 1)
    ev("_updateHeaderSubtitle", t + 0.0003, 0.22, 1)
    ev("_reorderClassicSection", t + 0.0007, 0.12, 1)


for base, items in ((0.0, 14), (2.4, 9), (6.1, 12)):
    poll(base)
    for i in range(4):
        download(base + 0.055 + i * 0.021 + rnd.random() * 0.004)
    rebuild(base + 0.16, items)
    menu_open(base + 0.31)
EVENTS.sort(key=lambda e: e["start"])

INSPECT = [
    ("_menuCapturedId", "number", "37236"), ("_pMaxMenuHeight", "number", "500"),
    ("_buttonMenu", "object", "[PopupBaseMenuItem]"), ("_headerSubtitle", "object", "[St_Label]"),
    ("_unreadBadgeText", "object", "[St_Label]"), ("_unreadBadge", "object", "[RssBadgeButton]"),
    ("_feedsSection", "object", "[RssPopupMenuSection]"), ("_minimalSection", "object", "[RssPopupMenuSection]"),
    ("_layoutMode", "string", "minimal"), ("_lcid", "number", "37677"), ("_updateInterval", "number", "1"),
    ("_itemsVisible", "number", "1024"),
    ("_rssFeedsSources", "array", "Array(4)", [
        ("0", "string", "https://www.reddit.com/r/gnome.rss"), ("1", "string", "https://blog.google/products/android/rss"),
        ("2", "string", "https://store.steampowered.com/feeds/news.xml"), ("3", "string", "https://planet.gnome.org/rss20.xml")]),
    ("_enableNotifications", "boolean", "true"), ("_maxMenuHeight", "number", "500"),
    ("_notifOnLockScreen", "boolean", "false"), ("_markInitialAsNew", "boolean", "false"),
    ("_timeout", "number", "10805"), ("_minimalDirty", "boolean", "true"), ("_menuIsOpen", "boolean", "false"),
    ("setSensitive", "function", "function setSensitive() { … }"), ("setMenu", "function", "function setMenu() { … }"),
]

LOGS = [
    (6, "", "Loading extension rss-feed@gnome-shell-extension"),
    (7, "Gjs", "settings schema loaded"),
    (6, "rss-feed", "Polling 4 feed sources"),
    (4, "libmutter", "Frame clock dispatch took longer than expected"),
    (4, "rss-feed", "HTTP GET https://www.reddit.com/r/gnome.rss: Gio.IOErrorEnum: Socket I/O timed out"),
    (6, "rss-feed", "Fetched 24 items from planet.gnome.org"),
    (3, "", "JS ERROR: TypeError: can't access property \"actor\", this._menu is null"),
    (6, "rss-feed", "Unread count updated: 7"),
    (4, "GLib", "Source ID 4127 was not found when attempting to remove it"),
    (6, "rss-feed", "Rebuilt minimal section in 55.2 ms"),
    (6, "", "Extension state changed: ENABLED"),
    (4, "rss-feed", "HTTP GET https://www.reddit.com/r/gnome.rss: Gio.IOErrorEnum: Socket I/O timed out"),
    (6, "rss-feed", "Fetched 18 items from blog.google"),
    (6, "rss-feed", "Unread count updated: 11"),
]
PRIO = {3: "err", 4: "warning", 6: "info", 7: "debug"}

META: dict = {"live": []}


def bounds(widget, win) -> list[float]:
    ok, r = widget.compute_bounds(win)
    return [r.get_x(), r.get_y(), r.get_width(), r.get_height()]


def scenario(win, dbus, sock) -> None:
    try:
        run(win)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def run(win) -> None:
    pv = win._profiler_view
    win._ext_list.set_active_uuid(H.RSS)
    win._on_extension_activated(None, H.RSS)
    win._view_stack.set_visible_child_name("profiler")
    pv._mode_btns["flamegraph"].set_active(True)
    win._keybindings.set_global_available(True)
    win.set_focus(None)
    H.pump(300)
    H.capture(win, "ready")
    META["start_btn"] = bounds(pv._start_stop_btn, win)

    # ── start recording ──
    pv._start_profiling()
    gen = pv._active_start_generation
    pv._on_profiling_started({"type": "profiling_started", "uuid": H.RSS, "ok": True, "sessionId": gen,
                              "patchedFunctions": 139, "visitedObjects": 412, "skippedFunctions": 0,
                              "truncated": False, "overheadUs": 0.42})
    win.set_focus(None)
    H.pump(300)
    H.capture(win, "armed")

    steps = 44
    per = len(EVENTS) / steps
    done = 0
    for i in range(steps):
        upto = len(EVENTS) if i == steps - 1 else int(round((i + 1) * per))
        for e in EVENTS[done:upto]:
            pv._ingest_event(dict(e, sessionId=gen), schedule_refresh=False)
        done = upto
        elapsed = 2 + i * 0.4
        pv._rec_start_ts = GLib.get_monotonic_time() / 1e6 - elapsed
        pv._flush_refresh()
        H.pump(90)
        if i == 0:
            pv._paned.set_position(268)
            H.pump(150)
        H.capture(win, f"live_{i:03d}")
        META["live"].append(done)
    print("events", len(EVENTS), "functions", len(pv._stats))

    META["tl"] = bounds(pv._tl_stack, win)
    META["tl_panel"] = bounds(pv._tl_stack.get_parent().get_parent(), win)
    META["cards"] = bounds(pv._paned.get_parent().get_first_child(), win)
    META["table"] = bounds(pv._paned.get_end_child(), win)
    META["toolbar_save"] = bounds(pv._save_btn, win)

    hot = P + "_rebuildMinimalSection"
    pv._apply_selected_fn(hot, sync_table=True)
    H.pump(200)
    H.capture(win, "flame_sel")
    pv._apply_selected_fn(None, sync_table=True)

    for mode in ("swimlane", "histogram"):
        pv._mode_btns[mode].set_active(True)
        H.pump(350)
        H.capture(win, mode)
    pv._apply_selected_fn(P + "_createMenuItem", sync_table=True)
    H.pump(200)
    H.capture(win, "histogram_sel")
    pv._apply_selected_fn(None, sync_table=True)

    # ── bottom of the table: rows below the measurement floor are dimmed ──
    adj = pv._col_view.get_parent().get_vadjustment()
    adj.set_value(adj.get_upper())
    H.pump(300)
    H.capture(win, "table_noise")
    adj.set_value(0)

    # ── stopped, flamegraph again ──
    pv._mode_btns["flamegraph"].set_active(True)
    pv._stop_profiling()
    win.set_focus(None)
    H.pump(350)
    H.capture(win, "stopped")

    # ── shortcuts dialog ──
    try:
        win._on_show_shortcuts(None, None)
        H.pump(700)
        win.set_focus(None)
        H.pump(100)
        H.capture(win, "shortcuts")
        dlg = win.get_visible_dialog() if hasattr(win, "get_visible_dialog") else None
        if dlg is not None:
            dlg.force_close()
        H.pump(400)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    # ── inspector ──
    try:
        iv = win._inspector_view
        win._view_stack.set_visible_child_name("inspector")
        H.pump(200)
        iv._navigate_to(["_indicator"])
        props = []
        for row in INSPECT:
            p = {"name": row[0], "type": row[1], "value": row[2]}
            if len(row) > 3:
                p["children"] = [{"name": c[0], "type": c[1], "value": c[2]} for c in row[3]]
            props.append(p)
        iv._on_inspect_result({"type": "inspect_result", "extensionUuid": H.RSS,
                               "path": ["_indicator"], "properties": props})
        H.pump(300)
        for i in range(iv._store.get_n_items()):
            item = iv._store.get_item(i)
            if item.name == "_rssFeedsSources":
                iv._on_expand_clicked(Gtk.Button(), item)
                break
        win.set_focus(None)
        H.pump(300)
        H.capture(win, "inspector")
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    # ── logs ──
    try:
        from app.core.journal_reader import LogEntry
        lv = win._logs_view
        win._view_stack.set_visible_child_name("logs")
        lv._is_running = True
        lv._set_start_stop_state(running=True)
        lv._set_state_pill(running=True)
        lv._update_list_stack()
        t = datetime(2026, 11, 13, 15, 14, 27, 102000)
        for prio, dom, msg in LOGS:
            t += timedelta(milliseconds=180 + rnd.randint(0, 600))
            lv._on_log_entry(None, LogEntry(timestamp=t, priority=prio, priority_name=PRIO[prio],
                                            identifier="gnome-shell", glib_domain=dom, message=msg, raw={}))
        win.set_focus(None)
        H.pump(500)
        H.capture(win, "logs")
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    (H.HERE / "shots" / "meta.json").write_text(json.dumps(META, indent=1))


H.build(scenario)
