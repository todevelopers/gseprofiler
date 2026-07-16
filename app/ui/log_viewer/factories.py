"""Column-view cell factories for the log table.

Free functions (not methods): each cell only needs the ``LogRowItem`` behind the
list item plus the pure formatting helpers from :mod:`common`, so none of this
touches ``LogViewerView`` state. The main view wires them straight onto the
``Gtk.SignalListItemFactory`` ``setup`` / ``bind`` signals — the GTK callback
signature ``(factory, list_item)`` matches these functions directly.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango

from app.ui.log_viewer.common import (
    _BUCKET_ERROR,
    _BUCKET_WARN,
    _LEVEL_PILL_CLASSES,
    _TAG_CSS_CLASSES,
    LogRowItem,
    _bucket_label,
    _bucket_pill_class,
    _tag_color_class,
)


def make_cell_box(content: Gtk.Widget) -> Gtk.Box:
    """Wrap a cell's content widget in a Gtk.Box that fills the cell so
    the severity tint can be applied to the box background."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    box.set_hexpand(True)
    box.add_css_class("log-cell")
    box.append(content)
    return box


def apply_cell_tint(box: Gtk.Box, bucket: str) -> None:
    for cls in ("cell-warn", "cell-error"):
        box.remove_css_class(cls)
    if bucket == _BUCKET_ERROR:
        box.add_css_class("cell-error")
    elif bucket == _BUCKET_WARN:
        box.add_css_class("cell-warn")


def time_setup(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    label = Gtk.Label()
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    label.add_css_class("log-time")
    list_item.set_child(make_cell_box(label))


def time_bind(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    item: LogRowItem = list_item.get_item()
    box: Gtk.Box = list_item.get_child()
    label: Gtk.Label = box.get_first_child()
    label.set_label(item.time_str)
    apply_cell_tint(box, item.bucket)


def level_setup(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    label = Gtk.Label()
    label.set_halign(Gtk.Align.START)
    label.set_valign(Gtk.Align.CENTER)
    label.add_css_class("log-level-pill")
    list_item.set_child(make_cell_box(label))


def level_bind(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    item: LogRowItem = list_item.get_item()
    box: Gtk.Box = list_item.get_child()
    label: Gtk.Label = box.get_first_child()
    for cls in _LEVEL_PILL_CLASSES:
        label.remove_css_class(cls)
    label.set_label(_bucket_label(item.bucket))
    label.add_css_class(_bucket_pill_class(item.bucket))
    apply_cell_tint(box, item.bucket)


def tag_setup(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    label = Gtk.Label()
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    label.add_css_class("log-tag")
    list_item.set_child(make_cell_box(label))


def tag_bind(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    item: LogRowItem = list_item.get_item()
    box: Gtk.Box = list_item.get_child()
    label: Gtk.Label = box.get_first_child()
    for cls in _TAG_CSS_CLASSES:
        label.remove_css_class(cls)
    label.set_label(f"[{item.tag}]")
    label.add_css_class(_tag_color_class(item.tag))
    apply_cell_tint(box, item.bucket)


def msg_setup(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    label = Gtk.Label()
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(False)
    label.add_css_class("log-message")
    list_item.set_child(make_cell_box(label))


def msg_bind(_fac: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
    item: LogRowItem = list_item.get_item()
    box: Gtk.Box = list_item.get_child()
    label: Gtk.Label = box.get_first_child()
    label.set_label(item.body)
    apply_cell_tint(box, item.bucket)
