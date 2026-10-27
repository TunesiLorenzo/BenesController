"""Containers that stretch to fill their window and scroll when the window is too small."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from benes.gui.theme import BG

# Widgets that scroll themselves under the mouse wheel.
_NATIVE_SCROLLERS = ("Listbox", "Text", "Treeview")


class ScrollableFrame(ttk.Frame):
    """Put content in ``.inner``.

    When the window is larger than the content's requested size, the content is
    stretched to fill it (so ``fill``/``expand`` layouts still grow). When it is
    smaller, a vertical and/or horizontal scrollbar appears instead of clipping.
    """

    _instances: dict[str, "ScrollableFrame"] = {}
    _POLL_MS = 200

    def __init__(self, master, background: str | None = None, **inner_options):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        if background is not None:
            self.canvas.configure(background=background)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = ttk.Frame(self.canvas, **inner_options)
        self._item = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._last = None
        self.canvas.bind("<Configure>", lambda event: self.sync())
        ScrollableFrame._instances[str(self.canvas)] = self
        self.bind("<Destroy>", self._forget, add="+")
        _install_wheel(self)
        self.after(self._POLL_MS, self._poll)

    def _forget(self, event):
        if event.widget is self:
            ScrollableFrame._instances.pop(str(self.canvas), None)

    def _poll(self):
        # Tk raises no event when the content's requested size changes, so check it.
        if not self.winfo_exists():
            return
        self.sync()
        self.after(self._POLL_MS, self._poll)

    def sync(self):
        """Size the content to max(requested, visible) and show only the needed scrollbars."""
        want_w, want_h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        view_w, view_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        state = (want_w, want_h, view_w, view_h)
        if state == self._last:
            return
        self._last = state
        # Report the content size upward so a new window opens just large enough.
        self.canvas.configure(width=want_w, height=want_h)
        if view_w <= 1 or view_h <= 1:
            return  # Not laid out yet.
        width, height = max(want_w, view_w), max(want_h, view_h)
        self.canvas.itemconfigure(self._item, width=width, height=height)
        self.canvas.configure(scrollregion=(0, 0, width, height))
        self._show(self.vbar, want_h > view_h, row=0, column=1, sticky="ns")
        self._show(self.hbar, want_w > view_w, row=1, column=0, sticky="ew")
        if want_h <= view_h:
            self.canvas.yview_moveto(0)
        if want_w <= view_w:
            self.canvas.xview_moveto(0)

    @staticmethod
    def _show(bar, needed, **grid):
        if needed and not bar.winfo_ismapped():
            bar.grid(**grid)
        elif not needed and bar.winfo_ismapped():
            bar.grid_remove()

    def can_scroll(self, horizontal: bool) -> bool:
        bar = self.hbar if horizontal else self.vbar
        return bar.winfo_ismapped()

    def scroll(self, units: int, horizontal: bool):
        (self.canvas.xview_scroll if horizontal else self.canvas.yview_scroll)(units, "units")


def _install_wheel(widget):
    root = widget._root()
    if getattr(root, "_scrollable_wheel_installed", False):
        return
    root._scrollable_wheel_installed = True

    def on_wheel(event, horizontal=False):
        target = event.widget
        if isinstance(target, str):
            try:
                target = root.nametowidget(target)
            except (KeyError, tk.TclError):
                return
        if target.winfo_class() in _NATIVE_SCROLLERS:
            return
        # Scroll the innermost scrollable container under the pointer that can move.
        while target is not None:
            frame = ScrollableFrame._instances.get(str(target))
            if frame is not None and frame.can_scroll(horizontal):
                frame.scroll(-1 if event.delta > 0 else 1, horizontal)
                return
            target = target.master

    root.bind_all("<MouseWheel>", on_wheel, add="+")
    root.bind_all("<Shift-MouseWheel>", lambda event: on_wheel(event, horizontal=True), add="+")


def fit_to_screen(window, margin_x: int = 80, margin_y: int = 120):
    """Open a window at its natural size, capped to the screen so scrollbars take over."""
    window.update_idletasks()
    for frame in list(ScrollableFrame._instances.values()):
        if frame.winfo_toplevel() is window:
            frame.sync()
    window.update_idletasks()
    max_width = window.winfo_screenwidth() - margin_x
    max_height = window.winfo_screenheight() - margin_y
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    # A scrollbar needed in one direction takes room in the other; allow for it
    # so a capped height does not also force a horizontal scrollbar.
    bar = 20
    if height > max_height:
        width += bar
    if width > max_width:
        height += bar
    window.geometry(f"{min(width, max_width)}x{min(height, max_height)}")


class VerticalScrolledFrame(ttk.Frame):
    """A compact scroll container for dialogs whose content exceeds the display."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, borderwidth=0)
        vertical = ttk.Scrollbar(self, orient="vertical", style="Accent.Vertical.TScrollbar",
                                 command=self.canvas.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", style="Accent.Horizontal.TScrollbar",
                                   command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        vertical.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas)
        self.content_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_region)
        self.canvas.bind("<MouseWheel>", self._scroll)

    def _update_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _scroll(self, event):
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")
