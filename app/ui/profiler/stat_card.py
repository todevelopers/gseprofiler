"""Profiler stat cards — header / value / sub-label tiles with a content-aware
horizontal layout.

The four cards above the timeline used to sit in a homogeneous `Gtk.Box`, so
every tile got the same fixed width regardless of what it held. Number cards
("33", "773.81 ms") ended up far wider than their content while the function-name
cards (Hottest function, Max call) truncated their identifiers.

`StatCardStrip` replaces that with a custom `Gtk.LayoutManager` that measures each
card's minimum and natural width and distributes the row width by *demand*: a card
whose content fits stays at its minimum, and the slack is shared among the cards
that are being truncated, in proportion to how much each still wants. The row
always fills its full width.
"""

import gi

gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Graphene, Gsk, Gtk, Pango


def _iter_children(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


class StatCard(Gtk.Box):
    """A single stat tile: an uppercase header, a large value, and a sub-label.

    ``mono_value``     renders the value in a monospace font (for code
                       identifiers) while keeping the same size as the others.
    ``value_growable`` / ``sub_growable`` mark the value / sub label as an
                       ellipsizing function name: it reports a small minimum so
                       the card can shrink, and a full natural width so the
                       layout knows how much it wants to grow.
    """

    __gtype_name__ = "ProfStatCard"

    def __init__(
        self,
        title: str,
        *,
        mono_value: bool = False,
        value_growable: bool = False,
        sub_growable: bool = False,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("prof-stat-card")
        self.set_hexpand(True)

        self._header = Gtk.Label(label=title, xalign=0.0)
        self._header.add_css_class("prof-stat-label")

        self._value = Gtk.Label(xalign=0.0)
        self._value.add_css_class("prof-stat-value")
        if mono_value:
            self._value.add_css_class("mono")
        if value_growable:
            self._value.set_ellipsize(Pango.EllipsizeMode.END)

        self._sub = Gtk.Label(xalign=0.0)
        self._sub.add_css_class("prof-stat-delta")
        if sub_growable:
            self._sub.set_ellipsize(Pango.EllipsizeMode.END)

        self.append(self._header)
        self.append(self._value)
        self.append(self._sub)

    def set_value(self, text: str, *, tooltip: str | None = None) -> None:
        self._value.set_text(text)
        self._value.set_tooltip_text(tooltip)

    def set_sub(self, text: str, *, tooltip: str | None = None) -> None:
        self._sub.set_text(text)
        self._sub.set_tooltip_text(tooltip)


class _StatCardLayout(Gtk.LayoutManager):
    """Distributes the row width across cards by growth demand (see module doc)."""

    __gtype_name__ = "ProfStatCardLayout"

    def __init__(self, spacing: float = 10.0) -> None:
        super().__init__()
        self._spacing = float(spacing)

    def do_measure(self, widget, orientation, for_size):  # noqa: ARG002
        children = [c for c in _iter_children(widget) if c.get_visible()]
        if orientation == Gtk.Orientation.HORIZONTAL:
            min_w = nat_w = 0.0
            for c in children:
                cm, cn, _, _ = c.measure(orientation, -1)
                min_w += cm
                nat_w += cn
            gap = self._spacing * max(0, len(children) - 1)
            return (int(min_w + gap), int(nat_w + gap), -1, -1)

        min_h = nat_h = 0
        for c in children:
            cm, cn, _, _ = c.measure(orientation, -1)
            min_h = max(min_h, cm)
            nat_h = max(nat_h, cn)
        return (min_h, nat_h, -1, -1)

    def do_allocate(self, widget, width, height, baseline):
        children = [c for c in _iter_children(widget) if c.get_visible()]
        n = len(children)
        if n == 0:
            return

        mins: list[float] = []
        nats: list[float] = []
        for c in children:
            cm, cn, _, _ = c.measure(Gtk.Orientation.HORIZONTAL, -1)
            mins.append(float(cm))
            nats.append(float(cn))

        avail = max(0.0, float(width) - self._spacing * (n - 1))
        widths = self._distribute(mins, nats, avail)

        # Snap to integer edges so the cards tile exactly with no sub-pixel seams.
        x = 0.0
        for c, w in zip(children, widths):
            left = int(round(x))
            right = int(round(x + w))
            point = Graphene.Point()
            point.init(left, 0)
            c.allocate(max(0, right - left), height, baseline, Gsk.Transform().translate(point))
            x += w + self._spacing

    @staticmethod
    def _distribute(mins: list[float], nats: list[float], avail: float) -> list[float]:
        n = len(mins)
        sum_min = sum(mins)
        sum_nat = sum(nats)

        if avail >= sum_nat:
            # Room to spare: spread the surplus proportional to natural width so
            # the strip fills fully and stays visually balanced.
            if sum_nat <= 0:
                return [avail / n] * n
            return [nat * avail / sum_nat for nat in nats]

        if avail <= sum_min:
            # Too cramped to honour every minimum: shrink proportionally.
            if sum_min <= 0:
                return [avail / n] * n
            return [m * avail / sum_min for m in mins]

        # The common case: every card keeps its minimum and the slack is handed
        # out in proportion to each card's growth demand (natural − minimum), so
        # truncated function-name cards grow while content-fit number cards stay put.
        slack = avail - sum_min
        demand = [nats[i] - mins[i] for i in range(n)]
        sum_demand = sum(demand) or 1.0
        return [mins[i] + slack * demand[i] / sum_demand for i in range(n)]


class StatCardStrip(Gtk.Box):
    """Horizontal row of `StatCard`s laid out by `_StatCardLayout`.

    Subclasses `Gtk.Box` purely for its child-lifecycle handling (children are
    unparented on dispose) — the default `GtkBoxLayout` is swapped out for the
    demand-weighted `_StatCardLayout`, so the box's own spacing / homogeneous
    properties are unused.
    """

    __gtype_name__ = "ProfStatCardStrip"

    def __init__(self, spacing: float = 10.0) -> None:
        super().__init__()
        self.set_layout_manager(_StatCardLayout(spacing=spacing))

    def add_card(self, card: StatCard) -> None:
        self.append(card)
