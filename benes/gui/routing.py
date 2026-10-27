"""Routing tab: input/output selection, switch states, permutations and the network diagram."""

import random
import tkinter as tk
from tkinter import messagebox, ttk

from benes.gui.theme import (ACCENT, BG, COLORS, OSA_COLOR, PANEL, POWER_METER_COLOR, TEXT, WDM_COLORS, WIRE,
                             ZERO_VOLT_COLOR)


class RoutingMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_routing_tab(self):
        """Route selection, detector rings, routing sidebar and the network diagram."""
        controls = ttk.Frame(self.route_tab, padding=(24, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="Input").pack(side="left")
        self.input_var = tk.StringVar(value="None")
        self.input_box = ttk.Combobox(controls, textvariable=self.input_var,
                                      values=("None", "1", "2", "3", "4", "5", "6"), width=5,
                                      state="readonly")
        self.input_box.pack(side="left", padx=(8, 20))
        self.input_box.bind("<<ComboboxSelected>>", self.choose_input)
        ttk.Label(controls, text="Output").pack(side="left")
        self.output_var = tk.StringVar(value="None")
        self.output_box = ttk.Combobox(controls, textvariable=self.output_var,
                                       values=("None", "1", "2", "3", "4", "5", "6"), width=5,
                                       state="readonly")
        self.output_box.pack(side="left", padx=8)
        self.output_box.bind("<<ComboboxSelected>>", self.choose_output)
        ttk.Checkbutton(controls, text="Overlay all possible paths", variable=self.all_paths, command=self.draw).pack(side="left", padx=15)
        self.evaluate_button = ttk.Button(controls, text="Evaluate Configuration", style="Accent.TButton",
                                          command=self.start_configuration_evaluation)
        self.evaluate_button.pack(side="left", padx=(0, 12))
        ttk.Button(controls, text="Reset switches", command=self.reset).pack(side="right")
        ttk.Button(controls, text="Random switches", command=self.randomize).pack(side="right", padx=8)

        detector_controls = ttk.Frame(self.route_tab, padding=(24, 0, 24, 8))
        detector_controls.pack(fill="x")
        ttk.Label(detector_controls, text="Power Meter output", foreground=POWER_METER_COLOR).pack(side="left")
        ttk.Combobox(detector_controls, textvariable=self.power_meter_output_var,
                     values=("None", "1", "2", "3", "4", "5", "6"), state="readonly", width=6).pack(side="left", padx=(7, 24))
        ttk.Label(detector_controls, text="OSA output", foreground=OSA_COLOR).pack(side="left")
        ttk.Combobox(detector_controls, textvariable=self.osa_output_var,
                     values=("None", "1", "2", "3", "4", "5", "6"), state="readonly", width=6).pack(side="left", padx=(7, 18))
        ttk.Label(detector_controls, text="Colored rings mark detector ports; the selected route output remains green.").pack(side="left")
        self.power_meter_output_var.trace_add("write", lambda *_: self.draw())
        self.osa_output_var.trace_add("write", lambda *_: self.draw())
        # Choosing an OSA output on either page routes the JDSU switch to it.
        self.osa_output_var.trace_add("write", lambda *_: self.osa_output_chosen(self.osa_output_var))


        # Keep every routing action beside the diagram.  The old stacked layout
        # let the canvas consume the window height and clipped these controls.
        self.status = ttk.Label(self.route_tab, font=("Segoe UI", 11, "bold"), padding=(24, 4))
        self.status.pack(fill="x")
        self.mapping_label = ttk.Label(self.route_tab, padding=(24, 2, 24, 6))
        self.mapping_label.pack(fill="x")
        self.wdm_route_label = ttk.Label(self.route_tab, padding=(24, 0, 24, 6), foreground=OSA_COLOR)
        self.wdm_route_label.pack(fill="x")

        route_body = ttk.Frame(self.route_tab, padding=(12, 0, 12, 10))
        route_body.pack(fill="both", expand=True)
        route_sidebar = ttk.LabelFrame(route_body, text="Routing controls", padding=(12, 8), width=285)
        # Natural size (no pack_propagate(False)) so the tab knows when to scroll.
        route_sidebar.pack(side="right", fill="y", padx=(12, 0))

        self.route_label = ttk.Label(route_sidebar, text="", wraplength=255, justify="left")
        self.route_label.pack(anchor="w", fill="x")
        route_list_frame = ttk.Frame(route_sidebar)
        route_list_frame.pack(fill="both", expand=True, pady=(6, 8))
        self.route_list = tk.Listbox(route_list_frame, height=7, bg=PANEL, fg=TEXT,
                                     selectbackground="#315b78", selectforeground="white",
                                     font=("Consolas", 9), exportselection=False, borderwidth=0,
                                     highlightthickness=0)
        route_scrollbar = ttk.Scrollbar(route_list_frame, orient="vertical", command=self.route_list.yview)
        self.route_list.configure(yscrollcommand=route_scrollbar.set)
        self.route_list.pack(side="left", fill="both", expand=True)
        route_scrollbar.pack(side="right", fill="y")
        self.route_list.bind("<<ListboxSelect>>", self.select_route)
        ttk.Button(route_sidebar, text="Apply path (drive bar/cross)", command=self.apply_route).pack(anchor="w", fill="x")
        ttk.Checkbutton(route_sidebar, text="Keep complete output permutation",
                        variable=self.keep_permutation_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(route_sidebar, text="Off: drive only the selected path's MZIs. On: adjust other MZIs as needed while retaining every current input → output connection.",
                  wraplength=255, foreground="#9baec6").pack(anchor="w", fill="x", pady=(2, 0))

        ttk.Separator(route_sidebar, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(route_sidebar, text="Route complete permutation", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(route_sidebar, text="Outputs for inputs 1, 2, 3, 4, 5, 6", wraplength=255).pack(anchor="w", pady=(3, 2))
        self.permutation = ttk.Entry(route_sidebar)
        self.permutation.insert(0, "1 2 3 4 5 6")
        self.permutation.pack(anchor="w", fill="x", pady=(0, 5))
        buttons = ttk.Frame(route_sidebar)
        buttons.pack(anchor="w", fill="x")
        ttk.Button(buttons, text="Route permutation", command=self.route_permutation).pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Shuffle", command=self.shuffle).pack(side="left", padx=(6, 0))
        ttk.Label(route_sidebar, text="Equivalent full-network routings").pack(anchor="w", pady=(10, 2))
        self.permutation_route_list = tk.Listbox(route_sidebar, height=5, bg=PANEL, fg=TEXT,
                                                 selectbackground="#315b78", selectforeground="white",
                                                 exportselection=False, state="disabled", borderwidth=0,
                                                 highlightthickness=0)
        self.permutation_route_list.pack(anchor="w", fill="x")
        self.permutation_route_list.bind("<<ListboxSelect>>", self.show_permutation_route)

        diagram = ttk.Frame(route_body)
        diagram.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(diagram, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        ttk.Label(diagram, text="Green switches have saved bar and cross voltages. Use the X, =, and 0 controls beside each MZI to set cross, bar, or 0 V; clicking an MZI still cycles the states. Type a voltage and press Enter to send it.", padding=(4, 5), wraplength=680).pack(anchor="w", fill="x")


    def choose_input(self, event=None):
        self.clear_permutation_preview()
        self.source = self.route_port(self.input_var)
        self.refresh()

    def choose_output(self, event=None):
        self.clear_permutation_preview()
        self.destination = self.route_port(self.output_var)
        self.refresh()

    @staticmethod
    def route_port(variable):
        value = variable.get()
        return None if value == "None" else int(value) - 1

    @staticmethod
    def detector_port(variable):
        value = variable.get()
        return None if value == "None" else int(value) - 1

    def click_input(self, row):
        self.input_var.set("None" if self.source == row else str(row + 1))
        self.choose_input()

    def click_output(self, row):
        self.output_var.set("None" if self.destination == row else str(row + 1))
        self.choose_output()

    def toggle(self, name):
        """Cycle a switch: cross -> straight -> 0 V -> cross."""
        if hasattr(self, "clear_permutation_preview"):
            self.clear_permutation_preview()
        to_zero = name not in self.zero_volt_switches and not self.network.states[name]
        crossed = to_zero or name in self.zero_volt_switches
        try:
            if to_zero:
                voltage = self.controller.drive_voltage(name, 0.0)
            else:
                voltage = self.controller.apply_calibrated_state(name, crossed)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
        self.network.states[name] = crossed
        if to_zero:
            self.zero_volt_switches.add(name)
        else:
            self.zero_volt_switches.discard(name)
        if voltage is not None:
            self.voltage_vars[name].set(f"{voltage:.2f}")
        self.refresh(keep_selection=True)
        if voltage is not None:
            state = "0 V (unbiased)" if to_zero else "cross" if crossed else "bar"
            self.update_hardware_status(f"{name} {state}: {voltage:.2f} V")

    def set_switch_state(self, name, state):
        """Set one MZI directly to cross, bar, or its unbiased 0 V state."""
        if hasattr(self, "clear_permutation_preview"):
            self.clear_permutation_preview()
        try:
            if state == "zero":
                crossed, voltage = True, self.controller.drive_voltage(name, 0.0)
            else:
                crossed = state == "cross"
                voltage = self.controller.apply_calibrated_state(name, crossed)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
        self.network.states[name] = crossed
        if state == "zero":
            self.zero_volt_switches.add(name)
        else:
            self.zero_volt_switches.discard(name)
        if voltage is not None:
            self.voltage_vars[name].set(f"{voltage:.2f}")
        self.refresh(keep_selection=True)
        if voltage is not None:
            self.update_hardware_status(f"{name} {'0 V (unbiased)' if state == 'zero' else state}: {voltage:.2f} V")

    def reset(self):
        self.network.states = dict.fromkeys(self.network.switches, True)
        self.apply_calibration()
        self.refresh(keep_selection=True)

    def randomize(self):
        for name in self.network.states:
            self.network.states[name] = bool(random.getrandbits(1))
        self.apply_calibration()
        self.refresh(keep_selection=True)

    def select_route(self, event=None):
        selection = self.route_list.curselection()
        if selection:
            self.selected_route = selection[0]
            self.draw()

    def apply_route(self):
        if not self.routes:
            return
        self.clear_permutation_preview()
        route_states = dict(self.routes[self.selected_route].states)
        if self.keep_permutation_var.get():
            target_states = self.network.states_for_permutation(
                self.network.mapping(), route_states, prefer_states=self.network.states,
            )
            if target_states is None:
                messagebox.showerror(
                    "Equivalent path unavailable",
                    "This alternative path cannot be used while retaining the current complete output permutation.",
                    parent=self,
                )
                return
            names = [name for name in self.network.switches
                     if target_states[name] != self.network.states[name] or name in self.zero_volt_switches]
        else:
            target_states = {**self.network.states, **route_states}
            # Only the MZIs used by this physical route are commanded.
            names = list(route_states)
        if self.drive_route_states(target_states, names):
            self.refresh(keep_selection=True)

    def drive_route_states(self, states, names):
        """Drive only ``names`` to their requested calibrated bar/cross states."""
        try:
            for name in names:
                voltage = self.controller.apply_calibrated_state(name, states[name])
                self.network.states[name] = states[name]
                self.zero_volt_switches.discard(name)
                if voltage is not None:
                    self.voltage_vars[name].set(f"{voltage:.2f}")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Path command failed", str(error), parent=self)
            return False
        mode = "while retaining the complete permutation" if self.keep_permutation_var.get() else "on the selected path only"
        self.update_hardware_status(f"Applied {len(names)} MZI state(s) {mode}")
        return True

    def clear_permutation_preview(self):
        self.permutation_preview_states = None
        if hasattr(self, "permutation_route_list"):
            self.permutation_route_list.selection_clear(0, "end")

    def set_permutation_route_options(self, permutation):
        """List every whole-network routing that produces one output permutation."""
        states = self.network.equivalent_permutation_states(permutation)
        current = self.network.states
        self.permutation_route_states = tuple(sorted(
            states,
            key=lambda candidate: sum(candidate[name] != current[name] for name in self.network.switches),
        ))
        self.permutation_route_list.configure(state="normal")
        self.permutation_route_list.delete(0, "end")
        for index, candidate in enumerate(self.permutation_route_states):
            changes = sum(candidate[name] != current[name] for name in self.network.switches)
            suffix = "current states" if changes == 0 else f"{changes} MZI state changes"
            self.permutation_route_list.insert("end", f"Routing {index + 1} — {suffix}")
        self.selected_permutation_route = 0
        self.permutation_route_list.selection_set(0)
        self.permutation_route_list.see(0)
        self.permutation_preview_states = self.permutation_route_states[0]

    def show_permutation_route(self, event=None):
        selection = self.permutation_route_list.curselection()
        if not selection or selection[0] >= len(self.permutation_route_states):
            return
        self.selected_permutation_route = selection[0]
        self.permutation_preview_states = self.permutation_route_states[self.selected_permutation_route]
        self.draw()

    def route_permutation(self):
        try:
            values = [int(x) - 1 for x in self.permutation.get().replace(",", " ").split()]
            if not self.network.solve(values):
                raise ValueError("No switch configuration found.")
        except ValueError as error:
            messagebox.showerror("Invalid permutation", str(error), parent=self)
            return
        self.apply_calibration()
        self.set_permutation_route_options(values)
        self.refresh(keep_selection=True)

    def shuffle(self):
        values = random.sample(range(1, 7), 6)
        self.permutation.delete(0, "end")
        self.permutation.insert(0, " ".join(map(str, values)))
        self.route_permutation()

    def apply_voltage(self, name):
        try:
            voltage = self.controller.set_voltage(name, self.voltage_vars[name].get())
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Voltage not applied", str(error), parent=self)
            return
        self.zero_volt_switches.discard(name)
        self.draw()
        self.voltage_vars[name].set(f"{voltage:.2f}")
        self.update_hardware_status(f"{name} set to {voltage:.2f} V")

    def refresh(self, keep_selection=False):
        if not keep_selection:
            self.selected_route = 0
        self.routes = ([] if self.source is None or self.destination is None
                       else self.network.routes(self.source, self.destination))
        self.route_list.delete(0, "end")
        for index, route in enumerate(self.routes):
            states = "  ".join(f"{name}={'X' if crossed else '='}" for name, crossed in route.states)
            self.route_list.insert("end", f"Path {index + 1}   {states}")
        if self.routes:
            self.route_list.selection_set(self.selected_route)
            self.route_list.see(self.selected_route)
        if self.source is None or self.destination is None:
            route_text = "No path selected. Choose an input and an output to explore paths."
        else:
            route_text = (f"{len(self.routes)} possible paths · = straight, X cross, 0V unbiased "
                          "(routes as cross; click a switch to cycle) · switches omitted from a path are unconstrained")
        self.route_label.configure(text=route_text)
        mapping = self.network.mapping()
        self.mapping_label.configure(text="CURRENT CONNECTIONS    " + "     ".join(f"{i + 1} → {out + 1}" for i, out in enumerate(mapping)))
        self.update_wdm_routing()
        if self.source is None or self.destination is None:
            self.status.configure(text="No path selected. Click an input and output, or choose None to clear either selection.")
        else:
            current = mapping[self.source] + 1
            self.status.configure(text=f"Input {self.source + 1} currently reaches output {current}   •   Exploring output {self.destination + 1}")
        self.update_hardware_status()
        self.draw()

    def draw(self):
        c = self.canvas
        for entry in self.switch_voltage_entries.values():
            entry.destroy()
        self.switch_voltage_entries = {}
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        # Tk initially reports a 1x1 canvas before its first layout pass.
        if width < 420 or height < 220:
            return
        display_states = self.permutation_preview_states or self.network.states
        previewing_permutation = self.permutation_preview_states is not None
        scale = min(1.0, width / 1180, height / 355)
        font_small = ("Segoe UI", max(7, round(8 * scale)))
        font_normal = ("Segoe UI", max(8, round(9 * scale)))
        font_heading = ("Segoe UI", max(9, round(10 * scale)), "bold")
        self.left_x = max(58, round(78 * scale))
        self.right_x = width - self.left_x
        first_stage = self.left_x + max(65, round(75 * scale))
        last_stage = self.right_x - max(88, round(125 * scale))
        self.xs = [first_stage + i * (last_stage - first_stage) / 4 for i in range(5)]
        top, bottom = max(48, round(58 * scale)), max(47, round(55 * scale))
        self.ys = [top + i * (height - top - bottom) / 5 for i in range(6)]
        # Keep each MZI visually quiet: one compact label and one voltage field.
        half = max(15, round(18 * scale))
        self.switch_half = half
        row_gap = min(self.ys[index + 1] - self.ys[index] for index in range(5))
        show_voltage_entry = row_gap >= 42
        card_pad = max(6, round(7 * scale))
        # Group the three middle stages into two independent 3x3 networks.
        for first, last, label in ((0, 2, "Upper 3×3"), (3, 5, "Lower 3×3")):
            c.create_rectangle(self.xs[1] - 65, self.ys[first] - 38,
                               self.xs[3] + 65, self.ys[last] + 26,
                               fill="#131e30", outline="#33465f", dash=(5, 4))
            c.create_text(self.xs[2], self.ys[last] + 17, text=label,
                          fill="#9baec6", font=font_normal)
        for stage, pairs in enumerate(self.network.pairs):
            x = self.xs[stage]
            c.create_text(x, max(14, round(16 * scale)), text=f"STAGE {stage + 1}", fill="#9baec6", font=font_heading)
            for row, y in enumerate(self.ys):
                if stage == 0:
                    c.create_line(self.left_x + 18, y, x - half, y, fill=WIRE, width=2)
                next_x = self.xs[stage + 1] - half if stage < 4 else self.right_x - 18
                c.create_line(x + half, y, next_x, self.ys[self.network.links[stage][row]], fill=WIRE, width=2)
                if (stage, row) not in self.network.at:
                    c.create_line(x - half, y, x + half, y, fill=WIRE, width=2)
                    c.create_text(x, y + round(13 * scale), text="bypass", fill="#8393a9", font=font_small)
            for index, (a, b) in enumerate(pairs):
                name = f"S{stage + 1}.{index + 1}"
                crossed = display_states[name]
                zero = name in self.zero_volt_switches and not previewing_permutation
                calibration = self.controller.settings["calibration"][name]
                calibrated = calibration["straight"] is not None and calibration["cross"] is not None
                tag = f"switch:{name}"
                outline = ZERO_VOLT_COLOR if zero else ACCENT if calibrated else "#7790ad"
                c.create_rectangle(x - half, self.ys[a] - card_pad, x + half, self.ys[b] + card_pad,
                                   fill="#2b2a1c" if zero else PANEL, outline=outline,
                                   width=2.5 if zero or calibrated else 1.5, tags=tag)
                for start, end in ((a, b), (b, a)) if crossed else ((a, a), (b, b)):
                    c.create_line(x - half, self.ys[start], x + half, self.ys[end],
                                  fill=ZERO_VOLT_COLOR if zero else "#a4bad4", width=2,
                                  dash=(4, 3) if zero else (), tags=tag)
                pin = self.controller.settings["mapping"][name]
                c.create_text(x, self.ys[a] - max(13, round(15 * scale)),
                              text=f"{name}   PIN {pin if pin is not None else '-'}",
                              fill=ZERO_VOLT_COLOR if zero else ACCENT if calibrated else TEXT,
                              font=font_normal, tags=tag)
                button_size = max(10, round(11 * scale))
                button_gap = max(2, round(3 * scale))
                button_x = x + half + max(4, round(5 * scale))
                button_y = (self.ys[a] + self.ys[b]) / 2
                for button_index, (label, state, color) in enumerate(
                        (("X", "cross", ACCENT), ("=", "bar", "#ffbe69"), ("0", "zero", ZERO_VOLT_COLOR))):
                    left = button_x + button_index * (button_size + button_gap)
                    button_tag = f"set:{name}:{state}"
                    c.create_rectangle(left, button_y - button_size / 2,
                                       left + button_size, button_y + button_size / 2,
                                       fill="#263650", outline=color, width=1.25, tags=button_tag)
                    c.create_text(left + button_size / 2, button_y, text=label, fill=color,
                                  font=("Segoe UI", max(7, round(8 * scale)), "bold"), tags=button_tag)
                    c.tag_bind(button_tag, "<Button-1>",
                               lambda event, n=name, s=state: self.set_switch_state(n, s))
                    c.tag_bind(button_tag, "<Enter>", lambda event: c.configure(cursor="hand2"))
                    c.tag_bind(button_tag, "<Leave>", lambda event: c.configure(cursor=""))
                if show_voltage_entry:
                    entry = ttk.Entry(c, textvariable=self.voltage_vars[name], width=4, justify="center")
                    entry.bind("<Return>", lambda event, n=name: self.apply_voltage(n))
                    c.create_window(x, self.ys[b] + max(19, round(20 * scale)), window=entry)
                    self.switch_voltage_entries[name] = entry
                c.tag_bind(tag, "<Button-1>", lambda event, n=name: self.toggle(n))
                c.tag_bind(tag, "<Enter>", lambda event: c.configure(cursor="hand2"))
                c.tag_bind(tag, "<Leave>", lambda event: c.configure(cursor=""))

        current = None if self.source is None else self.network.trace(self.source, display_states)
        wdm = self.wdm_routes()
        wdm_ends = {}
        for row, wavelength, color in wdm:
            wdm_ends[row, "in"] = wdm_ends[self.network.trace(row, display_states).rows[-1], "out"] = (wavelength, color)
        if not previewing_permutation:
            for row, _, color in wdm:
                for points in self.crosstalk_points(self.network.trace(row), self.right_x - 18,
                                                    self.xs, self.ys, self.switch_half):
                    c.create_line(*points, fill=color, width=1.5, dash=(3, 4),
                                  joinstyle="round", capstyle="round", state="disabled")
        if previewing_permutation:
            for row in range(self.network.size):
                self.draw_route(self.network.trace(row, display_states), WDM_COLORS[row], 3)
            c.create_text(width / 2, height - 12,
                          text=(f"Equivalent permutation routing {self.selected_permutation_route + 1} / "
                                f"{len(self.permutation_route_states)} · preview only"),
                          fill=ACCENT, font=font_normal)
        elif current is not None and not self.routes and (self.source, "in") not in wdm_ends:
            for points in self.crosstalk_points(current, self.right_x - 18,
                                                self.xs, self.ys, self.switch_half):
                c.create_line(*points, fill=ACCENT, width=1.5, dash=(3, 4),
                              joinstyle="round", capstyle="round", state="disabled")
        if self.routes and not previewing_permutation:
            for row, _, color in wdm:
                self.draw_route(self.network.trace(row), color, 3)
            if self.all_paths.get():
                for index, route in enumerate(self.routes):
                    self.draw_route(route, COLORS[index % len(COLORS)], 2, dashed=True)
            self.draw_route(self.routes[self.selected_route], ACCENT, 4)
            c.create_text(width / 2, height - 12, text=f"Preview: path {self.selected_route + 1} in green · broad dashes show alternatives · thin dashes show crosstalk", fill=ACCENT, font=("Segoe UI", 10))
        elif not previewing_permutation and current is not None:
            # A wide green halo keeps the selected input visible beneath its comb-line colour.
            self.draw_route(current, ACCENT, 9 if (self.source, "in") in wdm_ends else 4)
            for row, _, color in wdm:
                self.draw_route(self.network.trace(row), color, 3)
            c.create_text(width / 2, height - 12, text="Solid: routed light · thin dashes: possible crosstalk in the same input colour",
                          fill="#9baec6", font=font_normal)
        for row, y in enumerate(self.ys):
            for side, x, selected, callback in (
                ("in", self.left_x, row == self.source, self.click_input),
                ("out", self.right_x, row == self.destination, self.click_output),
            ):
                tag = f"{side}:{row}"
                wavelength, wdm_color = wdm_ends.get((row, side), (None, None))
                outline = ACCENT if selected else wdm_color or WIRE
                c.create_oval(x - 18, y - 18, x + 18, y + 18, fill=ACCENT if selected else PANEL, outline=outline,
                              width=3 if wdm_color and not selected else 2, tags=tag)
                c.create_text(x, y, text=str(row + 1), fill=BG if selected else TEXT, font=("Segoe UI", max(10, round(12 * scale)), "bold"), tags=tag)
                label_x, anchor = (x - 25, "e") if side == "in" else (x + 25, "w")
                label = f"In {row + 1}\nFiber {15 - row}" if side == "in" else f"Out {row + 1}\nFiber {4 + row}"
                label_y = y if wavelength is None else y - round(6 * scale)
                c.create_text(label_x, label_y, text=label, anchor=anchor, fill="#9baec6", font=font_small)
                if wavelength is not None:
                    c.create_text(label_x, y + round(18 * scale), text=f"{wavelength:g} nm", anchor=anchor,
                                  fill=wdm_color, font=font_small)
                if side == "out":
                    detector_colors = []
                    if row == self.detector_port(self.power_meter_output_var):
                        detector_colors.append(POWER_METER_COLOR)
                    if row == self.detector_port(self.osa_output_var):
                        detector_colors.append(OSA_COLOR)
                    for index, color in enumerate(detector_colors):
                        radius = 23 + index * 5
                        c.create_oval(x - radius, y - radius, x + radius, y + radius,
                                      outline=color, width=3, tags=tag)
                c.tag_bind(tag, "<Button-1>", lambda event, r=row, fn=callback: fn(r))
        c.create_text(self.left_x, max(14, round(16 * scale)), text="INPUT", fill=TEXT, font=font_heading)
        c.create_text(self.right_x, max(14, round(16 * scale)), text="OUTPUT", fill=TEXT, font=font_heading)

    def route_points(self, route, start_x, end_x, xs, ys, half):
        points = [start_x, ys[route.rows[0]]]
        states = dict(route.states)
        for stage, row in enumerate(route.rows[:-1]):
            x = xs[stage]
            points.extend((x - half, ys[row]))
            name = self.network.at.get((stage, row))
            exit_row = self.network.switch_output(stage, row, states[name] if name else False)
            points.extend((x + half, ys[exit_row]))
        points.extend((end_x, ys[route.rows[-1]]))
        return points

    def crosstalk_points(self, route, end_x, xs, ys, half):
        """Possible leakage from each used switch's other port, then along current states."""
        states = self.network.states
        for stage, row in enumerate(route.rows[:-1]):
            name = self.network.at.get((stage, row))
            if name is None:
                continue
            a, b = self.network.switches[name][1]
            main_exit = self.network.switch_output(stage, row, states[name])
            other_exit = b if main_exit == a else a
            points = [xs[stage] - half, ys[row], xs[stage] + half, ys[other_exit]]
            next_row = self.network.links[stage][other_exit]
            for next_stage in range(stage + 1, len(self.network.pairs)):
                x = xs[next_stage]
                points.extend((x - half, ys[next_row]))
                next_name = self.network.at.get((next_stage, next_row))
                exit_row = self.network.switch_output(
                    next_stage, next_row, states[next_name] if next_name else False)
                points.extend((x + half, ys[exit_row]))
                next_row = self.network.links[next_stage][exit_row]
            points.extend((end_x, ys[next_row]))
            yield points

    def draw_route(self, route, color, line_width, dashed=False):
        points = self.route_points(route, self.left_x + 18, self.right_x - 18, self.xs, self.ys, self.switch_half)
        self.canvas.create_line(*points, fill=color, width=line_width, dash=(5, 5) if dashed else (), joinstyle="round", capstyle="round", state="disabled")
