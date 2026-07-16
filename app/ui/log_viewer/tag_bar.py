"""Tag chip bar: the width-reactive layout manager and the chip-fitting mixin.

``_TagBarLayout`` reports the bar's real allocated width so the chips can be
re-trimmed on resize. ``TagBarMixin`` carries every method that builds and
maintains the chip row (inline chips, the "+N more" overflow popover, the pinned
extension chip and the tag filter state). It is mixed into ``LogViewerView`` and
relies on that view for the shared filter state (``_tag_counts``,
``_active_tags``, ``_pin_tag``) and for ``_rebuild_view``; the ``TYPE_CHECKING``
block below declares that contract for the type checker without any runtime cost.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from app.ui.log_viewer.common import (
    _TAG_BAR_SPACING,
    _TAG_CSS_CLASSES,
    _tag_color_class,
    _tag_display,
)


class _TagBarLayout(Gtk.BoxLayout):
    """Horizontal box layout that reports its allocated width on every change.

    GTK4 removed the public size-allocate signal, and in this PyGObject version
    vfunc overrides such as ``do_size_allocate`` / ``do_measure`` are silently
    ignored on a ``Gtk.Box`` subclass — only direct ``Gtk.Widget`` subclasses
    honour them. A custom ``Gtk.LayoutManager`` is the reliable hook: its
    ``do_allocate`` fires on every allocation with the real width, while the
    inherited ``Gtk.BoxLayout`` still lays the children out.
    """

    __gtype_name__ = "GseTagBarLayout"

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=_TAG_BAR_SPACING
        )
        self.on_width_changed: "Callable[[int], None] | None" = None
        self._last_w: int = -1

    def do_allocate(
        self, widget: Gtk.Widget, width: int, height: int, baseline: int
    ) -> None:
        Gtk.BoxLayout.do_allocate(self, widget, width, height, baseline)
        if width != self._last_w:
            self._last_w = width
            if self.on_width_changed is not None:
                self.on_width_changed(width)

    def do_measure(
        self, widget: Gtk.Widget, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
        # The bar trims its own chips to whatever width it is allocated, so it
        # must never report the full (un-trimmed) chip run as its horizontal
        # request. Reporting the real minimum (the sum of all non-shrinkable
        # chips) would force the toplevel window wider than the screen, push
        # chips past the window edge, and — since the bar is then allocated
        # that ballooned width — defeat the fit logic entirely. The bar is
        # FILL inside a vertical box, so it still receives the real window
        # width via do_allocate regardless of what it requests here.
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 0, 0, -1, -1
        return cast(
            "tuple[int, int, int, int]",
            Gtk.BoxLayout.do_measure(self, widget, orientation, for_size),
        )


class TagBarMixin:
    """Builds and maintains the tag chip bar for ``LogViewerView``."""

    if TYPE_CHECKING:
        # State owned by LogViewerView.
        _tag_counts: dict[str, int]
        _active_tags: set[str]
        _pin_tag: str | None
        _block_tag_signals: bool
        _chips_rebuild_pending: bool
        _inline_chip_widgets: dict[str, Gtk.ToggleButton]
        _popover_row_widgets: dict[str, tuple[Gtk.CheckButton, Gtk.Label]]
        # Widgets created in _build_tag_bar.
        _tag_bar: Gtk.Box
        _tag_bar_layout: _TagBarLayout
        _pin_chip: Gtk.ToggleButton
        _pin_sep: Gtk.Separator
        _inline_chips_box: Gtk.Box
        _more_label: Gtk.Label
        _more_btn: Gtk.MenuButton
        _popover_search: Gtk.SearchEntry
        _popover_list: Gtk.ListBox
        _clear_tags_btn: Gtk.Button
        # Provided by the main view.
        _rebuild_view: Callable[[], None]

    def _build_tag_bar(self) -> Gtk.Box:
        bar = Gtk.Box()
        self._tag_bar_layout = _TagBarLayout()
        bar.set_layout_manager(self._tag_bar_layout)
        self._tag_bar_layout.on_width_changed = self._on_tag_bar_width_changed
        bar.add_css_class("log-tagbar")
        # Clip any chips added transiently before the next idle re-trim (the
        # fit logic runs all chips through once when the bar is not yet
        # allocated) so they never paint beyond the bar's allocated width.
        bar.set_overflow(Gtk.Overflow.HIDDEN)
        self._tag_bar = bar

        # Pin chip — always first; updated when selected extension changes
        self._pin_chip = Gtk.ToggleButton()
        self._pin_chip.add_css_class("tag-chip")
        self._pin_chip.add_css_class("tag-chip-pin")
        self._pin_chip.add_css_class("flat")
        self._pin_chip.set_valign(Gtk.Align.CENTER)
        self._pin_chip.set_visible(False)
        self._pin_chip.set_tooltip_text("Show only logs for the selected extension")
        self._pin_chip.connect("toggled", self._on_pin_chip_toggled)
        bar.append(self._pin_chip)

        self._pin_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._pin_sep.set_margin_start(2)
        self._pin_sep.set_margin_end(2)
        self._pin_sep.set_visible(False)
        bar.append(self._pin_sep)

        # Dynamic inline chip slots
        self._inline_chips_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.append(self._inline_chips_box)

        # "+N more" overflow button with popover
        self._more_label = Gtk.Label(label="+0 more")
        more_icon = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        more_icon.set_pixel_size(12)
        more_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        more_content.append(self._more_label)
        more_content.append(more_icon)

        self._more_btn = Gtk.MenuButton()
        self._more_btn.set_child(more_content)
        self._more_btn.add_css_class("flat")
        self._more_btn.add_css_class("tag-chip")
        self._more_btn.set_valign(Gtk.Align.CENTER)
        self._more_btn.set_visible(False)
        bar.append(self._more_btn)

        # Popover: search entry + scrollable list of all tags
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        popover_box.set_margin_top(8)
        popover_box.set_margin_bottom(8)
        popover_box.set_margin_start(8)
        popover_box.set_margin_end(8)

        self._popover_search = Gtk.SearchEntry()
        self._popover_search.set_placeholder_text("Filter tags…")
        self._popover_search.set_size_request(210, -1)
        self._popover_search.connect("search-changed", self._on_popover_search_changed)
        popover_box.append(self._popover_search)

        pop_scroll = Gtk.ScrolledWindow()
        pop_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Grow to fit the tag list (so short lists show every row) and only
        # start scrolling past max. Without propagate-natural-height the window
        # stays pinned at min_content_height and shows ~3 rows regardless.
        pop_scroll.set_propagate_natural_height(True)
        pop_scroll.set_min_content_height(0)
        pop_scroll.set_max_content_height(400)

        self._popover_list = Gtk.ListBox()
        self._popover_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._popover_list.add_css_class("tag-popover-list")
        self._popover_list.set_filter_func(self._popover_row_filter)
        pop_scroll.set_child(self._popover_list)
        popover_box.append(pop_scroll)

        popover = Gtk.Popover()
        popover.set_child(popover_box)
        popover.connect("show", self._on_popover_show)
        self._more_btn.set_popover(popover)

        # Spacer pushes Clear to the right edge
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self._clear_tags_btn = Gtk.Button(label="Clear")
        self._clear_tags_btn.add_css_class("flat")
        self._clear_tags_btn.add_css_class("tag-clear")
        self._clear_tags_btn.set_valign(Gtk.Align.CENTER)
        self._clear_tags_btn.set_tooltip_text("Clear tag filter")
        self._clear_tags_btn.set_visible(False)
        self._clear_tags_btn.connect("clicked", self._on_clear_tags)
        bar.append(self._clear_tags_btn)

        return bar

    # ── Tag bar — builders / refreshers ───────────────────────────────────

    def _update_pin_chip(self) -> None:
        if self._pin_tag is None:
            self._pin_chip.set_visible(False)
            self._pin_sep.set_visible(False)
            return

        for cls in _TAG_CSS_CLASSES:
            self._pin_chip.remove_css_class(cls)
        self._pin_chip.add_css_class(_tag_color_class(self._pin_tag))
        self._pin_chip.set_label(f"{self._pin_tag} {self._tag_counts.get(self._pin_tag, 0)}")
        self._pin_chip.set_visible(True)
        self._pin_sep.set_visible(bool(self._tag_counts))

        self._block_tag_signals = True
        self._pin_chip.set_active(self._pin_tag in self._active_tags)
        self._block_tag_signals = False

    @staticmethod
    def _natural_width(widget: Gtk.Widget) -> int:
        """Real, CSS-aware natural horizontal size of *widget* in pixels.

        Returns 0 for an invisible widget (GTK measures hidden widgets as 0),
        so callers that need a hidden widget's size must make it visible first.
        """
        return int(widget.measure(Gtk.Orientation.HORIZONTAL, -1)[1])

    def _make_tag_chip(self, tag: str, count: int) -> Gtk.ToggleButton:
        btn = Gtk.ToggleButton(label=f"{_tag_display(tag)} {count}")
        btn.add_css_class("tag-chip")
        btn.add_css_class("flat")
        btn.add_css_class(_tag_color_class(tag))
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(f"Show only {_tag_display(tag)} entries")
        btn.set_active(tag in self._active_tags)
        btn.connect("toggled", self._on_inline_chip_toggled, tag)
        return btn

    def _schedule_chips_rebuild(self) -> None:
        """Coalesce a burst of rebuild triggers (resize-drag allocations, a
        flood of entries growing chip counts) into one rebuild on the next
        idle tick."""
        if not self._chips_rebuild_pending:
            self._chips_rebuild_pending = True
            GLib.idle_add(self._chips_rebuild_idle)

    def _on_tag_bar_width_changed(self, _width: int) -> None:
        self._schedule_chips_rebuild()

    def _chips_rebuild_idle(self) -> bool:
        self._chips_rebuild_pending = False
        self._rebuild_chips()
        return False

    def _compute_available_chips_width(self) -> int:
        """Pixel width available for the inline chips + the "+N more" button:
        the bar's allocated width minus the pin chip, separator and Clear button
        (each with the gap that surrounds it)."""
        bar_w = int(self._tag_bar.get_width())
        if bar_w <= 0:
            return 0
        taken = 0
        for widget in (self._pin_chip, self._pin_sep, self._clear_tags_btn):
            if widget.get_visible():
                taken += self._natural_width(widget) + _TAG_BAR_SPACING
        return max(0, bar_w - taken)

    def _rebuild_chips(self) -> None:
        """Rebuild the inline tag chips so they fill the full bar width, only
        spilling into the "+N more" popover when chips genuinely do not fit."""
        while (child := self._inline_chips_box.get_first_child()):
            self._inline_chips_box.remove(child)
        self._inline_chip_widgets.clear()

        sorted_tags = sorted(self._tag_counts.items(), key=lambda x: -x[1])
        # The pinned extension already has its own dedicated chip.
        filtered = [(t, c) for t, c in sorted_tags if t != self._pin_tag]

        available = self._compute_available_chips_width()

        if available <= 0:
            # Not allocated yet — show everything. The layout manager fires a
            # width change once the bar is allocated and this runs again to trim.
            for tag, count in filtered:
                btn = self._make_tag_chip(tag, count)
                self._inline_chips_box.append(btn)
                self._inline_chip_widgets[tag] = btn
            overflow = 0
        else:
            # Reserve room for the "+N more" button, measured while visible
            # (hidden widgets measure to 0). Size its label for the worst case
            # since the real overflow count is not known until the loop ends.
            self._more_btn.set_visible(True)
            self._more_label.set_label(f"+{max(len(filtered) - 1, 1)} more")
            more_w = self._natural_width(self._more_btn)

            used = 0
            shown = 0
            for i, (tag, count) in enumerate(filtered):
                btn = self._make_tag_chip(tag, count)
                self._inline_chips_box.append(btn)  # append first so CSS applies
                chip_w = self._natural_width(btn)
                gap = _TAG_BAR_SPACING if shown else 0
                # Reserve the overflow button only while chips still remain.
                has_remaining = i < len(filtered) - 1
                reserve = (_TAG_BAR_SPACING + more_w) if has_remaining else 0
                if used + gap + chip_w + reserve <= available:
                    used += gap + chip_w
                    shown += 1
                    self._inline_chip_widgets[tag] = btn
                else:
                    self._inline_chips_box.remove(btn)
                    break
            overflow = len(filtered) - shown

        if overflow > 0:
            self._more_label.set_label(f"+{overflow} more")
            self._more_btn.set_visible(True)
        else:
            self._more_btn.set_visible(False)

        self._pin_sep.set_visible(
            self._pin_tag is not None and bool(self._tag_counts)
        )

    def _rebuild_popover(self) -> None:
        """Rebuild the popover list from the current tag counts (called on open)."""
        while True:
            row = self._popover_list.get_row_at_index(0)
            if row is None:
                break
            self._popover_list.remove(row)
        self._popover_row_widgets.clear()

        for tag, count in sorted(self._tag_counts.items(), key=lambda x: -x[1]):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(3)
            row_box.set_margin_bottom(3)
            row_box.set_margin_start(6)
            row_box.set_margin_end(8)

            check = Gtk.CheckButton()
            check.set_active(tag in self._active_tags)
            check.connect("toggled", self._on_popover_check_toggled, tag)

            tag_lbl = Gtk.Label(label=_tag_display(tag))
            tag_lbl.set_hexpand(True)
            tag_lbl.set_halign(Gtk.Align.START)
            tag_lbl.add_css_class("log-tag")
            tag_lbl.add_css_class(_tag_color_class(tag))

            count_lbl = Gtk.Label(label=str(count))
            count_lbl.add_css_class("dim-label")

            row_box.append(check)
            row_box.append(tag_lbl)
            row_box.append(count_lbl)

            list_row = Gtk.ListBoxRow()
            list_row.set_child(row_box)
            list_row._tag_value = tag  # type: ignore[attr-defined]
            self._popover_list.append(list_row)
            self._popover_row_widgets[tag] = (check, count_lbl)

    def _sync_tag_filter_state(self) -> None:
        """Sync all chip toggle states and the Clear button without firing handlers."""
        self._block_tag_signals = True
        for tag, btn in self._inline_chip_widgets.items():
            btn.set_active(tag in self._active_tags)
        if self._pin_tag is not None:
            self._pin_chip.set_active(self._pin_tag in self._active_tags)
        for tag, (check, _) in self._popover_row_widgets.items():
            check.set_active(tag in self._active_tags)
        self._block_tag_signals = False
        self._clear_tags_btn.set_visible(bool(self._active_tags))

    def _refresh_chip_counts(self) -> None:
        """Update inline + pin chip labels in place when tag counts change
        without a full rebuild (a new tag triggers _rebuild_chips instead)."""
        for tag, btn in self._inline_chip_widgets.items():
            btn.set_label(f"{_tag_display(tag)} {self._tag_counts.get(tag, 0)}")
        if self._pin_tag is not None:
            self._pin_chip.set_label(
                f"{self._pin_tag} {self._tag_counts.get(self._pin_tag, 0)}"
            )
        # Growing counts widen the labels, so the shown chips may no longer
        # fit the bar. Re-fit on the next idle (coalesced) instead of letting
        # the in-place labels drift past the available width.
        self._schedule_chips_rebuild()

    # ── Tag bar — signal handlers ─────────────────────────────────────────

    def _on_pin_chip_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._block_tag_signals or self._pin_tag is None:
            return
        if btn.get_active():
            self._active_tags.add(self._pin_tag)
        else:
            self._active_tags.discard(self._pin_tag)
        self._sync_tag_filter_state()
        self._rebuild_view()

    def _on_inline_chip_toggled(self, btn: Gtk.ToggleButton, tag: str) -> None:
        if self._block_tag_signals:
            return
        if btn.get_active():
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        self._sync_tag_filter_state()
        self._rebuild_view()

    def _on_popover_check_toggled(self, check: Gtk.CheckButton, tag: str) -> None:
        if self._block_tag_signals:
            return
        if check.get_active():
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        self._sync_tag_filter_state()
        self._rebuild_view()

    def _on_popover_show(self, _popover: Gtk.Popover) -> None:
        self._rebuild_popover()
        self._popover_search.set_text("")

    def _on_popover_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._popover_list.invalidate_filter()

    def _popover_row_filter(self, row: Gtk.ListBoxRow) -> bool:
        text = self._popover_search.get_text().lower()
        if not text:
            return True
        return text in getattr(row, "_tag_value", "").lower()

    def _on_clear_tags(self, _btn: Gtk.Button) -> None:
        self._active_tags.clear()
        self._sync_tag_filter_state()
        self._rebuild_view()
