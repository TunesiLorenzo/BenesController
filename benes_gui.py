"""Run with: python benes_gui.py"""

import random
import tkinter as tk
from tkinter import messagebox, ttk

from network import BenesNetwork


BG = "#101827"
PANEL = "#192437"
WIRE = "#52647c"
TEXT = "#e6edf7"
ACCENT = "#53dfbb"
COLORS = ("#53dfbb", "#ffbe69", "#78b9ff", "#ed91cf", "#b6a0ff", "#fff080")


class BenesApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Beneš • 6 × 6 route explorer")
        self.geometry("1320x850")
        self.minsize(1120, 760)
        self.configure(bg=BG)
        self.network = BenesNetwork()
        self.source = 0
        self.destination = None
        self.routes = []
        self.selected_route = 0
        self.all_paths = tk.BooleanVar(value=True)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=7)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)

        header = ttk.Frame(self, padding=(24, 18, 24, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Beneš route explorer", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        ttk.Label(header, text="6 inputs · 6 outputs · 5 stages · 12 switches · generalized topology with bypass wires").pack(anchor="w", pady=(5, 0))

        controls = ttk.Frame(self, padding=(24, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="Input").pack(side="left")
        self.input_box = ttk.Combobox(controls, values=list(range(1, 7)), width=5, state="readonly")
        self.input_box.current(0)
        self.input_box.pack(side="left", padx=(8, 20))
        self.input_box.bind("<<ComboboxSelected>>", self.choose_input)
        ttk.Label(controls, text="Destination").pack(side="left")
        self.output_box = ttk.Combobox(controls, values=["Trace current"] + list(range(1, 7)), width=14, state="readonly")
        self.output_box.current(0)
        self.output_box.pack(side="left", padx=8)
        self.output_box.bind("<<ComboboxSelected>>", self.choose_output)
        ttk.Checkbutton(controls, text="Overlay all possible paths", variable=self.all_paths, command=self.draw).pack(side="left", padx=15)
        ttk.Button(controls, text="Reset switches", command=self.reset).pack(side="right")
        ttk.Button(controls, text="Random switches", command=self.randomize).pack(side="right", padx=8)

        self.status = ttk.Label(self, font=("Segoe UI", 12, "bold"), padding=(24, 8))
        self.status.pack(fill="x")
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, height=355)
        self.canvas.pack(fill="both", expand=True, padx=12)
        self.canvas.bind("<Configure>", lambda event: self.draw())

        ttk.Label(self, text="Click an input to trace it. Click an output to explore destinations. Click a switch to toggle straight / cross.", padding=(24, 6)).pack(anchor="w")
        bottom = ttk.Frame(self, padding=(24, 4, 24, 12))
        bottom.pack(fill="x")
        left = ttk.Frame(bottom)
        left.pack(side="left", fill="both", expand=True)
        self.route_label = ttk.Label(left, text="")
        self.route_label.pack(anchor="w")
        self.route_list = tk.Listbox(left, height=4, bg=PANEL, fg=TEXT, selectbackground="#315b78", selectforeground="white", font=("Consolas", 10), exportselection=False, borderwidth=0, highlightthickness=0)
        self.route_list.pack(fill="x", pady=6)
        self.route_list.bind("<<ListboxSelect>>", self.select_route)
        ttk.Button(left, text="Apply selected path to switches", command=self.apply_route).pack(anchor="w")
        right = ttk.Frame(bottom, padding=(24, 0, 0, 0))
        right.pack(side="right", fill="y")
        ttk.Label(right, text="Route a complete permutation", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(right, text="Desired outputs for inputs 1, 2, 3, 4, 5, 6").pack(anchor="w", pady=4)
        self.permutation = ttk.Entry(right, width=32)
        self.permutation.insert(0, "1 2 3 4 5 6")
        self.permutation.pack(anchor="w", pady=4)
        buttons = ttk.Frame(right)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Route permutation", command=self.route_permutation).pack(side="left")
        ttk.Button(buttons, text="Shuffle", command=self.shuffle).pack(side="left", padx=6)
        self.mapping_label = ttk.Label(self, padding=(24, 6, 24, 14))
        self.mapping_label.pack(fill="x")
        self.refresh()

    def choose_input(self, event=None):
        self.source = self.input_box.current()
        self.refresh()

    def choose_output(self, event=None):
        self.destination = self.output_box.current() - 1
        if self.destination < 0:
            self.destination = None
        self.refresh()

    def click_input(self, row):
        self.input_box.current(row)
        self.choose_input()

    def click_output(self, row):
        self.output_box.current(row + 1)
        self.choose_output()

    def toggle(self, name):
        self.network.states[name] = not self.network.states[name]
        self.refresh(keep_selection=True)

    def reset(self):
        self.network.states = dict.fromkeys(self.network.switches, False)
        self.refresh(keep_selection=True)

    def randomize(self):
        for name in self.network.states:
            self.network.states[name] = bool(random.getrandbits(1))
        self.refresh(keep_selection=True)

    def select_route(self, event=None):
        selection = self.route_list.curselection()
        if selection:
            self.selected_route = selection[0]
            self.draw()

    def apply_route(self):
        if self.routes:
            self.network.states.update(dict(self.routes[self.selected_route].states))
            self.refresh(keep_selection=True)

    def route_permutation(self):
        try:
            values = [int(x) - 1 for x in self.permutation.get().replace(",", " ").split()]
            if not self.network.solve(values):
                raise ValueError("No switch configuration found.")
        except ValueError as error:
            messagebox.showerror("Invalid permutation", str(error), parent=self)
            return
        self.refresh(keep_selection=True)

    def shuffle(self):
        values = random.sample(range(1, 7), 6)
        self.permutation.delete(0, "end")
        self.permutation.insert(0, " ".join(map(str, values)))
        self.route_permutation()

    def refresh(self, keep_selection=False):
        if not keep_selection:
            self.selected_route = 0
        self.routes = [] if self.destination is None else self.network.routes(self.source, self.destination)
        self.route_list.delete(0, "end")
        for index, route in enumerate(self.routes):
            states = "  ".join(f"{name}={'X' if crossed else '='}" for name, crossed in route.states)
            self.route_list.insert("end", f"Path {index + 1}   {states}")
        if self.routes:
            self.route_list.selection_set(self.selected_route)
            self.route_list.see(self.selected_route)
        self.route_label.configure(text=f"{len(self.routes)} possible paths · = straight, X cross · switches omitted from a path are unconstrained" if self.routes else "Current route · select a destination to list every possible path and its required switch states")
        mapping = self.network.mapping()
        self.mapping_label.configure(text="CURRENT CONNECTIONS    " + "     ".join(f"{i + 1} → {out + 1}" for i, out in enumerate(mapping)))
        current = mapping[self.source] + 1
        self.status.configure(text=f"Input {self.source + 1} currently reaches output {current}" + (f"   •   Exploring output {self.destination + 1}" if self.destination is not None else ""))
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        width, height = max(c.winfo_width(), 1080), max(c.winfo_height(), 300)
        self.ys = [58 + i * (height - 100) / 5 for i in range(6)]
        self.xs = [140 + i * (width - 330) / 4 for i in range(5)]
        self.left_x, self.right_x = 65, width - 65
        half = 24
        # Group the three middle stages into two independent 3x3 networks.
        for first, last, label in ((0, 2, "Upper 3×3"), (3, 5, "Lower 3×3")):
            c.create_rectangle(self.xs[1] - 65, self.ys[first] - 38,
                               self.xs[3] + 65, self.ys[last] + 26,
                               fill="#131e30", outline="#33465f", dash=(5, 4))
            c.create_text(self.xs[2], self.ys[last] + 17, text=label,
                          fill="#9baec6", font=("Segoe UI", 9))
        for stage, pairs in enumerate(self.network.pairs):
            x = self.xs[stage]
            c.create_text(x, 16, text=f"STAGE {stage + 1}", fill="#9baec6", font=("Segoe UI", 10, "bold"))
            for row, y in enumerate(self.ys):
                if stage == 0:
                    c.create_line(self.left_x + 18, y, x - half, y, fill=WIRE, width=2)
                next_x = self.xs[stage + 1] - half if stage < 4 else self.right_x - 18
                c.create_line(x + half, y, next_x, self.ys[self.network.links[stage][row]], fill=WIRE, width=2)
                if (stage, row) not in self.network.at:
                    c.create_line(x - half, y, x + half, y, fill=WIRE, width=2)
                    c.create_text(x, y + 13, text="bypass", fill="#8393a9", font=("Segoe UI", 8))
            for index, (a, b) in enumerate(pairs):
                name = f"S{stage + 1}.{index + 1}"
                crossed = self.network.states[name]
                tag = f"switch:{name}"
                c.create_rectangle(x - half, self.ys[a] - 12, x + half, self.ys[b] + 12, fill=PANEL, outline="#7790ad", width=1.5, tags=tag)
                for start, end in ((a, b), (b, a)) if crossed else ((a, a), (b, b)):
                    c.create_line(x - half, self.ys[start], x + half, self.ys[end], fill="#a4bad4", width=2, tags=tag)
                c.create_text(x, self.ys[a] - 23, text=f"{name} {'X' if crossed else '='}", fill=TEXT, font=("Segoe UI", 9), tags=tag)
                c.tag_bind(tag, "<Button-1>", lambda event, n=name: self.toggle(n))
                c.tag_bind(tag, "<Enter>", lambda event: c.configure(cursor="hand2"))
                c.tag_bind(tag, "<Leave>", lambda event: c.configure(cursor=""))

        current = self.network.trace(self.source)
        if self.routes:
            if self.all_paths.get():
                for index, route in enumerate(self.routes):
                    self.draw_route(route, COLORS[index % len(COLORS)], 2, dashed=True)
            self.draw_route(self.routes[self.selected_route], ACCENT, 4)
            c.create_text(width / 2, height - 12, text=f"Preview: path {self.selected_route + 1} in green · dashed lines show alternatives · Apply sets the required switches", fill=ACCENT, font=("Segoe UI", 10))
        else:
            self.draw_route(current, ACCENT, 4)
        for row, y in enumerate(self.ys):
            for side, x, selected, callback in (
                ("in", self.left_x, row == self.source, self.click_input),
                ("out", self.right_x, row == (self.destination if self.destination is not None else current.rows[-1]), self.click_output),
            ):
                tag = f"{side}:{row}"
                c.create_oval(x - 18, y - 18, x + 18, y + 18, fill=ACCENT if selected else PANEL, outline=ACCENT if selected else WIRE, width=2, tags=tag)
                c.create_text(x, y, text=str(row + 1), fill=BG if selected else TEXT, font=("Segoe UI", 12, "bold"), tags=tag)
                c.tag_bind(tag, "<Button-1>", lambda event, r=row, fn=callback: fn(r))
        c.create_text(self.left_x, 16, text="INPUT", fill=TEXT, font=("Segoe UI", 10, "bold"))
        c.create_text(self.right_x, 16, text="OUTPUT", fill=TEXT, font=("Segoe UI", 10, "bold"))

    def draw_route(self, route, color, line_width, dashed=False):
        points = [self.left_x + 18, self.ys[route.rows[0]]]
        states = dict(route.states)
        for stage, row in enumerate(route.rows[:-1]):
            x = self.xs[stage]
            points.extend((x - 24, self.ys[row]))
            name = self.network.at.get((stage, row))
            exit_row = self.network.switch_output(stage, row, states[name] if name else False)
            points.extend((x + 24, self.ys[exit_row]))
        points.extend((self.right_x - 18, self.ys[route.rows[-1]]))
        self.canvas.create_line(*points, fill=color, width=line_width, dash=(5, 5) if dashed else (), joinstyle="round", capstyle="round", state="disabled")


if __name__ == "__main__":
    BenesApp().mainloop()
