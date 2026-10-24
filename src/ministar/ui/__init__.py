"""Desktop UI.

Not implemented. Toolkit decision in ``docs/research/09-python-gui-windows.md``:
PySide6 / Qt Widgets, LGPL-3, shipped as a PyInstaller *onedir* tree.

Measured on this hardware: 627 ms cold start, 131.8 MB dist, and ~1.4 ms of GUI
thread per frame for a 24 MP preview (downscale with OpenCV in the worker, hand
over an owning ``QImage``, then ``QPixmap.fromImage`` + ``setPixmap``).

Two traps worth remembering: ``QImage.scaled`` is 74.6 ms on 24 MP (12x slower
than OpenCV) so never call it on the GUI thread, and a ``QImage`` that views a
garbage-collected numpy buffer returns silent nonsense rather than raising.
"""
