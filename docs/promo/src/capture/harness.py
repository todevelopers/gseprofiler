"""Drive the real GSE Profiler window with synthetic bridge data and capture it to PNG."""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path("/mnt/c/GitHubRepos/gse-profiler")
XDG = HERE / "xdg"
(XDG / "config" / "gse-profiler").mkdir(parents=True, exist_ok=True)
os.environ["XDG_CONFIG_HOME"] = str(XDG / "config")
os.environ["XDG_DATA_HOME"] = str(XDG / "data")
os.environ["XDG_CACHE_HOME"] = str(XDG / "cache")
fc = HERE / "fonts.conf"
fc.write_text(
    '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
    '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
    f"<dir>{HERE / 'fonts'}</dir>"
    "<alias binding=\"strong\"><family>monospace</family><prefer><family>Adwaita Mono</family></prefer></alias>"
    "</fontconfig>"
)
os.environ["FONTCONFIG_FILE"] = str(fc)
os.environ.setdefault("GSK_RENDERER", "cairo")

RSS = "rss-feed@gnome-shell-extension"
(XDG / "config" / "gse-profiler" / "favorites.json").write_text(json.dumps([RSS]))

sys.path.insert(0, str(REPO))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from app import main as app_main  # noqa: E402
from app.core.dbus_client import DBusClient  # noqa: E402
from app.core.socket_server import SocketServer  # noqa: E402

SocketServer.is_client_connected = property(lambda self: True)
SocketServer.send = lambda self, message: None
DBusClient._init_proxy = lambda self: None

EXTENSIONS = {
    RSS: {"name": "RSS Feed", "description": "RSS feed reader for the top panel", "state": 1, "type": 2,
          "path": f"/home/dev/.local/share/gnome-shell/extensions/{RSS}", "version": 12, "hasPrefs": True,
          "url": "https://github.com/todevelopers/gnome-shell-extension-rss-feed"},
    "gse-profiler-bridge@todevelopers": {"name": "GSE Profiler Bridge", "state": 1, "type": 2, "version": 1,
                                         "description": "Bridge for GSE Profiler"},
}


def pump(ms: int = 150) -> None:
    """Run the main loop for a while so layout, timers and frame clocks settle."""
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def capture(win: Gtk.Window, name: str, scale: float = 2.0) -> None:
    node = None
    for _ in range(20):
        win.unset_state_flags(Gtk.StateFlags.BACKDROP)
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot()
        snap.scale(scale, scale)
        Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            break
        win.present()
        pump(250)
    if node is None:
        raise RuntimeError(f"window produced no render node for {name}")
    rect = Graphene.Rect().init(0, 0, w * scale, h * scale)
    tex = win.get_renderer().render_texture(node, rect)
    out = HERE / "shots" / f"{name}.png"
    out.parent.mkdir(exist_ok=True)
    tex.save_to_png(str(out))


def build(scenario) -> None:
    app = Adw.Application(application_id="io.github.todevelopers.GseProfiler.Capture")

    def on_activate(_app):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-font-name", "Adwaita Sans 11")
        settings.set_property("gtk-enable-animations", False)
        settings.set_property("gtk-icon-theme-name", "Adwaita")
        real = app_main.Application.__new__(app_main.Application)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(REPO / "app" / "data" / "icons"))
        provider = Gtk.CssProvider()
        provider.load_from_path(str(REPO / "app" / "data" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        del real

        from app.core.bridge_manager import BridgeManager
        from app.core.ego_client import EgoClient
        from app.core.ego_installer import EgoInstaller
        from app.core.github_installer import GitHubInstaller
        from app.core.keybindings import KeybindingManager
        from app.core.source_registry import SourceRegistry

        dbus = DBusClient()
        sock = SocketServer()
        registry = SourceRegistry()
        win = app_main.MainWindow(
            application=app, dbus_client=dbus, socket_server=sock,
            bridge=BridgeManager(REPO, dbus), installer=GitHubInstaller(registry),
            ego_installer=EgoInstaller(registry, EgoClient()), keybindings=KeybindingManager(),
        )
        win.set_default_size(1180, 780)
        win.present()
        dbus._extensions = {u: app_main_parse(u, i) for u, i in EXTENSIONS.items()}
        dbus.emit("extensions-changed", dbus.get_extensions())
        sock.emit("client-connected")
        pump(400)
        try:
            scenario(win, dbus, sock)
        finally:
            app.quit()

    app.connect("activate", on_activate)
    app.run([])


def app_main_parse(uuid, info):
    from app.core.dbus_client import _parse_info
    return _parse_info(uuid, info)
