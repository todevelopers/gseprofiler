"""Live journalctl log viewer.

Split from the former single-file ``app/ui/log_viewer.py`` into a package that
mirrors ``app/ui/profiler``: :mod:`common` holds shared constants/helpers and the
row model, :mod:`factories` the column cell factories, :mod:`tag_bar` the chip
bar, :mod:`capture_panel` the capture-source controls, and :mod:`view` the main
``LogViewerView`` that composes them. ``LogViewerView`` is re-exported here so
``from app.ui.log_viewer import LogViewerView`` keeps working.
"""

from app.ui.log_viewer.view import LogViewerView

__all__ = ["LogViewerView"]
