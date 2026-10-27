"""Colours, shared GUI constants and the ttk style of the Beneš GUI."""

from tkinter import ttk

# Completed OSA sweeps to wait for after moving the output switch: the first may
# have begun before the move, the second lies wholly after it.
POST_SWITCH_SWEEPS = 2
# Smallest plot area before the tab scrolls instead of squeezing the figure further.
PLOT_MIN_SIZE = {"width": 480, "height": 320}


BG = "#101827"
PANEL = "#192437"
WIRE = "#52647c"
TEXT = "#e6edf7"
ACCENT = "#53dfbb"
COLORS = ("#53dfbb", "#ffbe69", "#78b9ff", "#ed91cf", "#b6a0ff", "#fff080")
POWER_METER_COLOR = "#ffbe69"
OSA_COLOR = "#78b9ff"
ZERO_VOLT_COLOR = "#ffd84d"
# One colour per input for the paths and spectrum markers of assigned comb lines.
WDM_COLORS = ("#ff6b6b", "#ffa94d", "#4dabf7", "#f06595", "#b197fc", "#c0eb75")
# Tags comb-line marker lines so spectrum export skips them.
WDM_MARKER_GID = "wdm-comb-line"
# MZI calibration status: (diagram/table colour, description).
CAL_STATUS = {
    "pending": ("#7790ad", "not calibrated"),
    "saved": ("#2f9e80", "saved voltages"),
    "active": (ZERO_VOLT_COLOR, "sweeping"),
    "calibrated": (ACCENT, "calibrated"),
    "ok": (ACCENT, "state OK"),
    "partial": ("#ffa94d", "low extinction"),
    "inverted": ("#ff6b6b", "wrong state"),
    "unknown": ("#9baec6", "no line"),
    "failed": ("#ff6b6b", "sweep failed"),
}
# Worst first, to combine the verdicts of several state checks.
VERDICT_ORDER = ("inverted", "partial", "unknown", "ok")
CHECK_SHORT = {"Alignment (all 0 V)": "0 V", "All cross (calibrated)": "all X",
               "All bar (calibrated)": "all =", "Routed states": "routed"}


def apply_theme(root):
    """Configure the dark ttk theme used by every window."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("TButton", background="#263650", foreground=TEXT, font=("Segoe UI", 10), padding=(10, 7))
    style.map("TButton", background=[("active", "#355171"), ("disabled", "#1b2638")],
              foreground=[("disabled", "#71829a")])
    style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                    font=("Segoe UI", 10, "bold"), padding=(10, 7))
    style.map("Accent.TButton", background=[("active", "#7bf0cf"), ("disabled", "#285749")],
              foreground=[("disabled", "#9ab9af")])
    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, bordercolor="#7790ad")
    style.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT, arrowcolor=TEXT, bordercolor="#7790ad")
    style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
    style.configure("TLabelframe", background=BG, foreground=TEXT)
    style.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=("Segoe UI", 10, "bold"))
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background="#172338", foreground="#9baec6", padding=(15, 8),
                    font=("Segoe UI", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", PANEL), ("active", "#21324b")],
              foreground=[("selected", ACCENT), ("active", TEXT)])
    # High-contrast scrollbars everywhere so it is obvious when a window has more content.
    for scrollbar_style in ("Accent.Vertical.TScrollbar", "Accent.Horizontal.TScrollbar",
                            "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(scrollbar_style, background=ACCENT, troughcolor="#172338",
                        bordercolor=BG, arrowcolor=BG, gripcount=0)
        style.map(scrollbar_style, background=[("active", "#7bf0cf"), ("pressed", "#3ac69f")])
