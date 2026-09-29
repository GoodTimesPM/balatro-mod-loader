"""The window's colours: Discord's greys, with a thin green edge on every field.

Uses `clam`, the only stock ttk theme that can be recoloured; `vista` ignores
background colours. The palette is plain constants so ttk styles and `tk.Text`
options read the same names. The green outline goes on fields only.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# -- Palette ---------------------------------------------------------------
#
# Discord's dark theme.

INK = "#1e1f22"       # the darkest surface: window edge, scrollbar trough
PANEL = "#2b2d31"     # sidebars and bars
BASE = "#313338"      # the main working surface
FIELD = "#383a40"     # anything you type into or pick from
RAISED = "#404249"    # buttons, hovered rows
TEXT = "#dbdee1"      # primary text
MUTED = "#949ba4"     # secondary text, hints, disabled
LINE = "#3f4147"      # separators between things of equal weight

GREEN = "#23a55a"     # Discord's accent: field outlines, "this is fine"
GREEN_BRIGHT = "#3ba55c"
GREEN_DIM = "#1a7943"
RED = "#f23f43"       # Discord's danger red, for problems
AMBER = "#f0b132"

SELECT = "#404a58"    # selected table row: blue-grey, so green stays meaningful

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_HEAD = ("Segoe UI", 10, "bold")


def apply(root: tk.Misc) -> ttk.Style:
    """Recolour every ttk widget class the window uses. Returns the Style."""
    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(background=BASE)
    try:
        root.option_add("*Font", FONT)
    except tk.TclError:  # pragma: no cover -- only if root is not a toplevel
        pass

    # The root style, so unstyled widget classes still use the palette.
    style.configure(
        ".",
        background=BASE, foreground=TEXT, fieldbackground=FIELD,
        bordercolor=LINE, darkcolor=BASE, lightcolor=BASE,
        troughcolor=INK, insertcolor=TEXT, font=FONT,
        focuscolor=GREEN,
    )

    style.configure("TFrame", background=BASE)
    style.configure("Bar.TFrame", background=PANEL)
    style.configure("TLabel", background=BASE, foreground=TEXT)
    style.configure("Muted.TLabel", background=BASE, foreground=MUTED)
    style.configure("Status.TLabel", background=PANEL, foreground=MUTED,
                    relief="flat")
    style.configure("TSeparator", background=LINE)
    style.configure("TPanedwindow", background=BASE)
    style.configure("Sash", sashthickness=6, gripcount=0)

    # -- Fields: the green outline lives here ------------------------------

    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            widget,
            fieldbackground=FIELD, background=FIELD, foreground=TEXT,
            bordercolor=GREEN_DIM, lightcolor=GREEN_DIM, darkcolor=GREEN_DIM,
            insertcolor=TEXT, arrowcolor=MUTED, selectbackground=SELECT,
            selectforeground=TEXT, borderwidth=1, relief="solid", padding=4,
        )
        style.map(
            widget,
            bordercolor=[("focus", GREEN), ("hover", GREEN)],
            lightcolor=[("focus", GREEN), ("hover", GREEN)],
            darkcolor=[("focus", GREEN), ("hover", GREEN)],
            arrowcolor=[("hover", GREEN)],
            fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
            foreground=[("disabled", MUTED), ("readonly", TEXT)],
            # Otherwise a focused readonly Combobox is white on white.
            selectbackground=[("readonly", FIELD)],
            selectforeground=[("readonly", TEXT)],
        )
    # The Combobox drop-down is a Tk listbox, not a ttk widget, and is reached
    # only through the option database.
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", GREEN_DIM)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # -- Controls ----------------------------------------------------------

    style.configure(
        "TButton",
        background=RAISED, foreground=TEXT, bordercolor=LINE,
        lightcolor=RAISED, darkcolor=RAISED,
        borderwidth=1, relief="solid", padding=(10, 4), focusthickness=0,
    )
    style.map(
        "TButton",
        background=[("pressed", GREEN_DIM), ("active", "#4a4d55")],
        bordercolor=[("active", GREEN)],
        lightcolor=[("active", GREEN)], darkcolor=[("active", GREEN)],
        foreground=[("disabled", MUTED)],
    )
    # The one button that writes to the game reads as the primary action.
    style.configure("Accent.TButton", background=GREEN_DIM, bordercolor=GREEN,
                    lightcolor=GREEN, darkcolor=GREEN)
    style.map("Accent.TButton",
              background=[("pressed", GREEN_DIM), ("active", GREEN)],
              foreground=[("disabled", MUTED)])

    style.configure("TCheckbutton", background=BASE, foreground=TEXT,
                    indicatorcolor=FIELD, indicatorbackground=FIELD,
                    bordercolor=GREEN_DIM, focuscolor=BASE, padding=(2, 2))
    style.map("TCheckbutton",
              background=[("active", BASE)],
              indicatorcolor=[("selected", GREEN), ("pressed", GREEN_DIM)],
              bordercolor=[("active", GREEN)])

    # -- The table ---------------------------------------------------------

    style.configure(
        "Treeview",
        background=BASE, fieldbackground=BASE, foreground=TEXT,
        bordercolor=GREEN_DIM, lightcolor=GREEN_DIM, darkcolor=GREEN_DIM,
        borderwidth=1, relief="solid", rowheight=22,
    )
    style.map("Treeview",
              background=[("selected", SELECT)],
              foreground=[("selected", TEXT)])
    style.configure(
        "Treeview.Heading",
        background=PANEL, foreground=MUTED, font=FONT_BOLD,
        bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL,
        relief="flat", padding=(6, 5),
    )
    style.map("Treeview.Heading",
              background=[("active", RAISED)],
              foreground=[("active", TEXT)])

    # -- Tabs --------------------------------------------------------------

    style.configure("TNotebook", background=BASE, bordercolor=GREEN_DIM,
                    lightcolor=BASE, darkcolor=BASE, borderwidth=1,
                    tabmargins=(0, 2, 0, 0))
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                    bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL,
                    padding=(12, 5), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", BASE), ("active", RAISED)],
              foreground=[("selected", TEXT)],
              expand=[("selected", (0, 0, 0, 0))])

    # -- Scrollbars --------------------------------------------------------

    style.configure("Vertical.TScrollbar", background=RAISED, troughcolor=INK,
                    bordercolor=INK, arrowcolor=MUTED, lightcolor=RAISED,
                    darkcolor=RAISED, borderwidth=0, arrowsize=12)
    style.map("Vertical.TScrollbar", background=[("active", GREEN_DIM)])
    style.configure("Horizontal.TScrollbar", background=RAISED, troughcolor=INK,
                    bordercolor=INK, arrowcolor=MUTED, lightcolor=RAISED,
                    darkcolor=RAISED, borderwidth=0, arrowsize=12)
    style.map("Horizontal.TScrollbar", background=[("active", GREEN_DIM)])

    return style


def text_options() -> dict:
    """Constructor options for a `tk.Text`, matched to the ttk fields.

    `highlightthickness` is how a Tk widget gets the same 1px green edge a
    ttk field gets from `bordercolor`; there is no other way to draw one.
    """
    return {
        "background": BASE,
        "foreground": TEXT,
        "insertbackground": TEXT,
        "selectbackground": SELECT,
        "selectforeground": TEXT,
        "relief": "flat",
        "borderwidth": 0,
        "highlightthickness": 1,
        "highlightbackground": GREEN_DIM,
        "highlightcolor": GREEN,
        "font": FONT,
    }


def dialog_options() -> dict:
    """Colours for the plain Tk dialogs (askstring, showerror).

    These are built by Tk itself from the option database, so the only way to
    reach them is to set the defaults before the dialog is created.
    """
    return {
        "*Dialog.msg.background": BASE,
        "*Dialog.msg.foreground": TEXT,
        "*Dialog.background": BASE,
    }
