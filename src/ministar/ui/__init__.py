"""Operator console.

**NiceGUI**, per brief 14 §2. One codebase serves both deployments: a normal
browser tab, or a desktop window via ``ui.run(native=True)``, which wraps
``pywebview`` and the OS webview -- *not* Electron, so no browser engine is
bundled.

This supersedes the PySide6 decision in brief 09, which assumed a Windows-only
desktop app. The measurements from that brief still govern the image path -- in
particular "never scale a 24 MP frame on the GUI thread", which survives as "never
do camera or heavy image work on the UI thread" in :mod:`ministar.ui.focus`.

Screens live one-per-module:

* :mod:`ministar.ui.focus` -- live view, HFD, lens drive. The first screen, and
  the only one whose behaviour is verified on hardware (brief 16).
"""
