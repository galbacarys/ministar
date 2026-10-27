"""Shared look and feel for every screen.

Centralised because dark adaptation is a *hard requirement* (brief 14 §6), not a
style preference: a white flash at a dark site costs minutes of night vision. One
palette, defined once, is the only way that survives contact with new screens.

The rules behind the colours:

* red on black only -- no white text, no blue links, no bright icons;
* large touch targets, because this is operated on a phone at arm's length;
* a monospace readout column, so numbers do not jitter as they change.
"""

from __future__ import annotations

from nicegui import ui

#: Deep red, e.g. for headings. Brighter than this starts costing dark adaptation.
INK = "#ff5a4d"
#: Body text and numbers.
BODY = "#e04a3c"
#: Warnings. Amber rather than red so it reads as "look at me" rather than "error".
WARN = "#ff9d33"
#: Panel/background.
BLACK = "#000000"

CSS = """
body, .nicegui-content { background: #000000 !important; }
.mn-title { color: #ff5a4d; font-family: Consolas, monospace; font-size: 15px;
            letter-spacing: 1px; }
.mn-heading { color: #ff7a6a; font-family: Consolas, monospace; font-size: 22px;
              font-weight: 700; letter-spacing: 1px; }
.mn-body { color: #e04a3c; font-family: Consolas, monospace; font-size: 15px;
           line-height: 1.5; }
.mn-metrics { color: #e04a3c; font-family: Consolas, monospace; font-size: 17px;
              line-height: 1.55; white-space: pre; }
.mn-warn { color: #ff9d33; font-family: Consolas, monospace; font-size: 14px; }
.mn-dim { color: #a33226; font-family: Consolas, monospace; font-size: 13px; }
.mn-preview { border: 1px solid #5a1510; background: #000000; width: 100%;
              max-width: 880px; }
.mn-drive { background: #24080a !important; color: #ff7a6a !important;
            border: 1px solid #6b1f1a !important; font-family: Consolas, monospace;
            font-size: 19px !important; font-weight: 700;
            min-width: 108px; min-height: 68px; }
.mn-drive:hover { background: #3a0d10 !important; }
.mn-action { background: #2a0a0c !important; color: #ff8a7a !important;
             border: 1px solid #7a241c !important; font-family: Consolas, monospace;
             font-size: 17px !important; font-weight: 700;
             min-width: 130px; min-height: 58px; }
.mn-action:hover { background: #401216 !important; }
.mn-step { color: #7a2a22; font-family: Consolas, monospace; font-size: 13px;
           letter-spacing: 1px; }
.mn-step-now { color: #ff5a4d !important; font-weight: 700;
               border-bottom: 2px solid #ff5a4d; }
.mn-step-done { color: #b8534a !important; }
.mn-card { background: #140405; border: 1px solid #4a1512; border-radius: 6px; }
"""


def install() -> None:
    """Apply the palette to the current page. Safe to call more than once."""
    ui.dark_mode(True)
    ui.add_css(CSS)
