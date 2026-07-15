from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from app.core.dbus_client import DBusClient, ExtensionState
from app.core.socket_server import SocketServer

_INVALID_POS = GLib.MAXUINT

# Order matches the design's "Type pill palette".
_TYPE_PILL_CLASSES: tuple[str, ...] = (
    "t-string",
    "t-number",
    "t-boolean",
    "t-object",
    "t-array",
    "t-function",
    "t-null",
    "t-undefined",
    "t-error",
    "t-getter",
    "t-info",
)


class PropertyItem(GObject.Object):
    """One row in the Inspector property table."""

    __gtype_name__ = "InspectorPropertyItem"

    def __init__(
        self,
        name: str,
        type_str: str,
        value_str: str,
        depth: int = 0,
        parent_name: str = "",
    ) -> None:
        super().__init__()
        self.name = name
        self.type_str = type_str
        self.value_str = value_str
        self.depth = depth
        self.parent_name = parent_name
        self.has_children = False
        self.expanded = False
        self.children_data: list[dict[str, Any]] = []


class InspectorView(Gtk.Stack):
    """Live extension stateObj inspector — Phase 5."""

    def __init__(self, dbus_client: DBusClient, socket_server: SocketServer) -> None:
        super().__init__()
        self._dbus = dbus_client
        self._socket = socket_server
        self._current_uuid: str | None = None
        self._store = Gio.ListStore(item_type=PropertyItem)
        self._current_path: list[str] = []
        # handler IDs for expand/drill buttons, keyed by id(button widget)
        self._expand_handlers: dict[int, int] = {}
        self._drill_handlers: dict[int, int] = {}

        self._item_positions: dict[int, int] = {}
        self._build_ui()

        self._signal_ids: list[tuple[GObject.Object, int]] = [
            (socket_server, socket_server.connect("message-received", self._on_message)),
            (socket_server, socket_server.connect("client-connected", self._on_client_connected)),
            (socket_server, socket_server.connect("client-disconnected", self._on_disconnected)),
            (dbus_client, dbus_client.connect("extensions-changed", self._on_extensions_changed)),
        ]
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for obj, sig_id in self._signal_ids:
            obj.disconnect(sig_id)
        self._signal_ids.clear()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        no_selection = Adw.StatusPage()
        no_selection.set_icon_name("edit-find-symbolic")
        no_selection.set_title("No Extension Selected")
        no_selection.set_description("Select an enabled extension from the list to inspect its state object.")
        self.add_named(no_selection, "no-selection")

        disabled = Adw.StatusPage()
        disabled.set_icon_name("edit-find-symbolic")
        disabled.set_title("Extension Disabled")
        disabled.set_description("Enable the extension to inspect its state object.")
        self.add_named(disabled, "disabled")

        bridge_offline = Adw.StatusPage()
        bridge_offline.set_icon_name("network-offline-symbolic")
        bridge_offline.set_title("Bridge Extension Offline")
        bridge_offline.set_description(
            "The bridge extension is not running. Enable it in the Extensions tab"
            " and wait for it to connect."
        )
        self.add_named(bridge_offline, "bridge-offline")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add_named(content, "content")
        self.set_visible_child_name("no-selection")

        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.add_css_class("inspector-toolbar")

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh properties")
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", self._on_refresh)

        self._copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self._copy_btn.set_tooltip_text("Copy selected value to clipboard")
        self._copy_btn.add_css_class("flat")
        self._copy_btn.set_sensitive(False)
        self._copy_btn.connect("clicked", self._on_copy)

        self._status_lbl = Gtk.Label()
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_halign(Gtk.Align.END)
        self._status_lbl.add_css_class("inspector-status")

        toolbar.append(refresh_btn)
        toolbar.append(self._copy_btn)
        toolbar.append(self._status_lbl)

        # ── Column view ─────────────────────────────────────────────────────
        self._selection = Gtk.SingleSelection.new(self._store)
        self._selection.connect("selection-changed", self._on_selection_changed)

        col_view = Gtk.ColumnView(model=self._selection)
        col_view.set_vexpand(True)
        col_view.set_show_row_separators(False)
        col_view.set_show_column_separators(False)
        col_view.connect("activate", self._on_row_activate)
        self._col_view = col_view

        # Name column
        name_fac = Gtk.SignalListItemFactory()
        name_fac.connect("setup", self._name_setup)
        name_fac.connect("bind", self._name_bind)
        name_fac.connect("unbind", self._name_unbind)
        name_col = Gtk.ColumnViewColumn(title="PROPERTY", factory=name_fac)
        name_col.set_fixed_width(260)
        name_col.set_resizable(True)
        col_view.append_column(name_col)

        # Type column
        type_fac = Gtk.SignalListItemFactory()
        type_fac.connect("setup", self._type_setup)
        type_fac.connect("bind", self._type_bind)
        type_col = Gtk.ColumnViewColumn(title="TYPE", factory=type_fac)
        type_col.set_fixed_width(90)
        type_col.set_resizable(True)
        col_view.append_column(type_col)

        # Value column
        value_fac = Gtk.SignalListItemFactory()
        value_fac.connect("setup", self._value_setup)
        value_fac.connect("bind", self._value_bind)
        value_col = Gtk.ColumnViewColumn(title="VALUE", factory=value_fac)
        value_col.set_expand(True)
        value_col.set_resizable(True)
        col_view.append_column(value_col)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_size_request(0, -1)
        scrolled.set_child(col_view)

        # ── Placeholder ─────────────────────────────────────────────────────
        self._placeholder = Adw.StatusPage()
        self._placeholder.set_icon_name("edit-find-symbolic")
        self._placeholder.set_title("No Properties")
        self._placeholder.set_description(
            "Select an extension and click Refresh to inspect its state object."
        )
        self._placeholder.set_vexpand(True)

        # ── Main stack ──────────────────────────────────────────────────────
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.add_named(self._placeholder, "placeholder")
        self._stack.add_named(scrolled, "table")

        # ── Breadcrumb bar ───────────────────────────────────────────────────
        self._breadcrumb_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._breadcrumb_box.add_css_class("inspector-breadcrumb")

        self._breadcrumb_revealer = Gtk.Revealer()
        self._breadcrumb_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._breadcrumb_revealer.set_reveal_child(False)
        self._breadcrumb_revealer.set_child(self._breadcrumb_box)

        content.append(toolbar)
        content.append(self._breadcrumb_revealer)
        content.append(self._stack)

        # Tab-scoped shortcut. MANAGED scope + Gtk.Stack unmapping the hidden
        # pages means this is only live while the Inspector tab is selected.
        shortcut_ctrl = Gtk.ShortcutController()
        shortcut_ctrl.set_scope(Gtk.ShortcutScope.MANAGED)
        shortcut_ctrl.add_shortcut(Gtk.Shortcut.new(
            Gtk.KeyvalTrigger.new(Gdk.KEY_r, Gdk.ModifierType.CONTROL_MASK),
            Gtk.CallbackAction.new(self._on_refresh_shortcut),
        ))
        self.add_controller(shortcut_ctrl)

    # ── Name column factory ────────────────────────────────────────────────

    def _name_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        expand_btn = Gtk.Button()
        expand_btn.add_css_class("flat")
        expand_btn.set_can_focus(False)
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        label.add_css_class("monospace")
        drill_btn = Gtk.Button(icon_name="go-next-symbolic")
        drill_btn.add_css_class("flat")
        drill_btn.add_css_class("inspector-drill-slot")
        drill_btn.set_can_focus(False)
        drill_btn.set_tooltip_text("Inspect subtree")
        box.append(expand_btn)
        box.append(label)
        box.append(drill_btn)
        list_item.set_child(box)

    def _name_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: PropertyItem = list_item.get_item()
        box = list_item.get_child()
        expand_btn = box.get_first_child()
        label = expand_btn.get_next_sibling()
        drill_btn = label.get_next_sibling()

        box.set_margin_start(item.depth * 24)

        # Expand toggle — always occupy the same slot so rows align; only the
        # icon is visible when the row is actually expandable.
        has_expand = item.has_children and item.depth == 0
        if has_expand:
            expand_btn.set_icon_name(
                "pan-down-symbolic" if item.expanded else "pan-end-symbolic"
            )
            expand_btn.set_opacity(1.0)
            expand_btn.set_can_target(True)
            expand_btn.set_sensitive(True)
        else:
            expand_btn.set_icon_name("pan-end-symbolic")  # placeholder for sizing
            expand_btn.set_opacity(0)
            expand_btn.set_can_target(False)
            expand_btn.set_sensitive(False)

        label.set_label(item.name)

        # Drill button — depth-0 object/array. Slot is always present; the
        # 'inspector-drill-active' class lets CSS reveal it on row hover.
        drillable = item.depth == 0 and item.type_str in ("object", "array")
        if drillable:
            drill_btn.add_css_class("inspector-drill-active")
            drill_btn.set_can_target(True)
            drill_btn.set_sensitive(True)
        else:
            drill_btn.remove_css_class("inspector-drill-active")
            drill_btn.set_can_target(False)
            drill_btn.set_sensitive(False)

        # Rebind expand handler
        btn_id = id(expand_btn)
        if btn_id in self._expand_handlers:
            expand_btn.disconnect(self._expand_handlers[btn_id])
        self._expand_handlers[btn_id] = expand_btn.connect(
            "clicked", self._on_expand_clicked, item
        )

        # Rebind drill handler
        d_id = id(drill_btn)
        if d_id in self._drill_handlers:
            drill_btn.disconnect(self._drill_handlers[d_id])
        if drillable:
            self._drill_handlers[d_id] = drill_btn.connect(
                "clicked", self._on_drill_in, item
            )

    def _name_unbind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        box = list_item.get_child()
        if not box:
            return
        expand_btn = box.get_first_child()
        label = expand_btn.get_next_sibling()
        drill_btn = label.get_next_sibling()

        for widget, store in ((expand_btn, self._expand_handlers), (drill_btn, self._drill_handlers)):
            wid = id(widget)
            if wid in store:
                try:
                    widget.disconnect(store.pop(wid))
                except Exception:
                    pass

    # ── Type column factory ────────────────────────────────────────────────

    def _type_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.add_css_class("inspector-type-pill")
        list_item.set_child(label)

    def _type_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: PropertyItem = list_item.get_item()
        label: Gtk.Label = list_item.get_child()
        for cls in _TYPE_PILL_CLASSES:
            label.remove_css_class(cls)
        label.set_label(item.type_str)
        safe = "".join(c for c in item.type_str.lower() if c.isalpha())
        if safe:
            label.add_css_class(f"t-{safe}")

    # ── Value column factory ───────────────────────────────────────────────

    def _value_setup(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        label.add_css_class("monospace")
        label.add_css_class("inspector-value")
        list_item.set_child(label)

    def _value_bind(self, _fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item: PropertyItem = list_item.get_item()
        label: Gtk.Label = list_item.get_child()
        label.set_label(item.value_str)
        if item.type_str in ("null", "undefined"):
            label.add_css_class("dim")
        else:
            label.remove_css_class("dim")

    # ── Expand / collapse ──────────────────────────────────────────────────

    def _on_expand_clicked(self, btn: Gtk.Button, item: PropertyItem) -> None:
        item.expanded = not item.expanded
        btn.set_icon_name(
            "pan-down-symbolic" if item.expanded else "pan-end-symbolic"
        )
        if item.expanded:
            self._insert_children(item)
        else:
            self._remove_children(item)

    def _insert_children(self, parent: PropertyItem) -> None:
        parent_pos = self._find_item_pos(parent)
        if parent_pos is None:
            return
        children = [
            PropertyItem(
                name=c.get("name", ""),
                type_str=c.get("type", "unknown"),
                value_str=str(c.get("value", "")),
                depth=1,
                parent_name=parent.name,
            )
            for c in parent.children_data
        ]
        if children:
            self._store.splice(parent_pos + 1, 0, children)
            self._rebuild_item_positions()

    def _remove_children(self, parent: PropertyItem) -> None:
        parent_pos = self._find_item_pos(parent)
        if parent_pos is None:
            return
        n = self._store.get_n_items()
        count = 0
        for i in range(parent_pos + 1, n):
            child = self._store.get_item(i)
            if child.depth == 1 and child.parent_name == parent.name:
                count += 1
            else:
                break
        if count > 0:
            self._store.splice(parent_pos + 1, count, [])
            self._rebuild_item_positions()

    def _rebuild_item_positions(self) -> None:
        n = self._store.get_n_items()
        self._item_positions = {id(self._store.get_item(i)): i for i in range(n)}

    def _find_item_pos(self, target: PropertyItem) -> int | None:
        return self._item_positions.get(id(target))

    # ── Drill-in / breadcrumb navigation ──────────────────────────────────

    def _on_drill_in(self, _btn: Gtk.Button, item: PropertyItem) -> None:
        self._navigate_to(self._current_path + [item.name])

    def _navigate_to(self, path: list[str]) -> None:
        self._current_path = path
        self._update_breadcrumb()
        if self._current_uuid:
            self._socket.send({"type": "inspect", "uuid": self._current_uuid, "path": path})
            self._status_lbl.set_label("Loading…")

    def _on_back(self, _btn: Gtk.Button) -> None:
        self._navigate_to(self._current_path[:-1])

    def _update_breadcrumb(self) -> None:
        # Clear existing breadcrumb widgets
        child = self._breadcrumb_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._breadcrumb_box.remove(child)
            child = nxt

        if not self._current_path:
            self._breadcrumb_revealer.set_reveal_child(False)
            return

        self._breadcrumb_revealer.set_reveal_child(True)

        # Back button
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.add_css_class("flat")
        back_btn.set_tooltip_text("Go up one level")
        back_btn.connect("clicked", self._on_back)
        self._breadcrumb_box.append(back_btn)

        # Segments: "stateObj › _fetcher › _client" — first is always literal stateObj.
        segments = ["stateObj"] + self._current_path
        for i, seg in enumerate(segments):
            if i > 0:
                sep = Gtk.Label(label="›")
                sep.add_css_class("inspector-bc-sep")
                self._breadcrumb_box.append(sep)
            if i < len(segments) - 1:
                target = self._current_path[:i]
                btn = Gtk.Button(label=seg)
                btn.add_css_class("flat")
                btn.connect("clicked", lambda _b, p=target: self._navigate_to(p))
                self._breadcrumb_box.append(btn)
            else:
                lbl = Gtk.Label(label=seg)
                lbl.add_css_class("inspector-bc-current")
                self._breadcrumb_box.append(lbl)

    # ── Public API ─────────────────────────────────────────────────────────

    def set_target_extension(self, uuid: str | None) -> None:
        """Set the extension to inspect. Resets path and auto-loads if connected."""
        changed = uuid != self._current_uuid
        if changed:
            self._current_uuid = uuid
            self._current_path = []
            self._update_breadcrumb()
            self._store.splice(0, self._store.get_n_items(), [])
            self._item_positions.clear()
            self._stack.set_visible_child_name("placeholder")
            self._status_lbl.set_label("")
        self._update_visible_child(force=changed)

    def _update_visible_child(self, force: bool = False) -> None:
        uuid = self._current_uuid
        prev = self.get_visible_child_name()
        if uuid is None:
            self.set_visible_child_name("no-selection")
            return
        if self._dbus.get_extension_state(uuid) != ExtensionState.ENABLED:
            self.set_visible_child_name("disabled")
            return
        if not self._socket.is_client_connected:
            self.set_visible_child_name("bridge-offline")
            return
        self.set_visible_child_name("content")
        if prev != "content" or force:
            self._socket.send({"type": "inspect", "uuid": uuid, "path": self._current_path})
            self._status_lbl.set_label("Loading…")

    def _on_extensions_changed(
        self, _dbus: DBusClient, _extensions: dict[str, Any]
    ) -> None:
        if self._current_uuid is not None:
            self._update_visible_child()

    # ── Toolbar actions ────────────────────────────────────────────────────

    def _on_refresh(self, _btn: object) -> None:
        uuid = self._current_uuid
        if not uuid:
            return
        self._socket.send({"type": "inspect", "uuid": uuid, "path": self._current_path})
        self._status_lbl.set_label("Refreshing…")

    def _on_refresh_shortcut(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self.get_visible_child_name() == "content":
            self._on_refresh(None)
        return True

    def _on_copy(self, _btn: Gtk.Button) -> None:
        pos = self._selection.get_selected()
        if pos == _INVALID_POS:
            return
        item: PropertyItem | None = self._store.get_item(pos)
        if not item:
            return
        text = f"{item.name}\t{item.type_str}\t{item.value_str}"
        self.get_clipboard().set(text)

    def _on_selection_changed(self, _sel: Gtk.SingleSelection, _pos: int, _n: int) -> None:
        self._copy_btn.set_sensitive(self._selection.get_selected() != _INVALID_POS)

    # ── Row activation ─────────────────────────────────────────────────────

    def _on_row_activate(self, _col_view: Gtk.ColumnView, pos: int) -> None:
        item: PropertyItem | None = self._store.get_item(pos)
        if not item:
            return
        # Double-click a row with children → drill in.
        if item.depth == 0 and item.type_str in ("object", "array"):
            self._navigate_to(self._current_path + [item.name])

    # ── Socket message handling ────────────────────────────────────────────

    def _on_message(self, _server: SocketServer, msg: dict[str, Any]) -> None:
        if msg.get("type") == "inspect_result":
            self._on_inspect_result(msg)

    def _on_inspect_result(self, msg: dict[str, Any]) -> None:
        if msg.get("extensionUuid") != self._current_uuid:
            return
        if msg.get("path", []) != self._current_path:
            return  # stale response from a previous navigation
        properties: list[dict[str, Any]] = msg.get("properties", [])

        items: list[PropertyItem] = []
        for prop in properties:
            pi = PropertyItem(
                name=prop.get("name", ""),
                type_str=prop.get("type", "unknown"),
                value_str=str(prop.get("value", "")),
            )
            children = prop.get("children")
            if children:
                pi.has_children = True
                pi.children_data = children
            items.append(pi)

        self._store.splice(0, self._store.get_n_items(), items)
        self._rebuild_item_positions()

        count = len(items)
        word = "property" if count == 1 else "properties"
        self._status_lbl.set_label(f"{count} {word}")

        if count > 0:
            self._stack.set_visible_child_name("table")
        else:
            self._placeholder.set_description(
                f"No inspectable properties found for {self._current_uuid}."
            )
            self._stack.set_visible_child_name("placeholder")

    def _on_client_connected(self, _server: SocketServer) -> None:
        if self._current_uuid:
            self._update_visible_child(force=True)

    def _on_disconnected(self, _server: SocketServer) -> None:
        self._status_lbl.set_label("Disconnected")
        self._update_visible_child()
