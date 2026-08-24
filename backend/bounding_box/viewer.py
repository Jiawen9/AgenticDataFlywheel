from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageTk


PROJECT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = PROJECT_DIR / "annotated" / "manifest.json"


class ActionBoxViewer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Trajectory Action Box Viewer")
        self.geometry("1480x900")
        self.minsize(1050, 650)
        self.records: list[dict[str, Any]] = []
        self.record_by_item: dict[str, int] = {}
        self.current_index = 0
        self.original_image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.show_annotated = tk.BooleanVar(value=True)
        self.show_raw_action = tk.BooleanVar(value=True)
        self.fit_to_window = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Loading…")
        self._build_ui()
        self._load_manifest()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 7))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="◀ Previous", command=lambda: self._move(-1)).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Next ▶", command=lambda: self._move(1)).pack(side=tk.LEFT, padx=(6, 14))
        ttk.Checkbutton(
            toolbar, text="Show annotated image", variable=self.show_annotated, command=self._show_current
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            toolbar,
            text="Show raw click/swipe",
            variable=self.show_raw_action,
            command=self._render,
        ).pack(side=tk.LEFT, padx=(14, 0))
        ttk.Checkbutton(
            toolbar, text="Fit to window", variable=self.fit_to_window, command=self._render
        ).pack(side=tk.LEFT, padx=14)
        ttk.Button(toolbar, text="Rebuild annotations", command=self._rebuild_hint).pack(side=tk.RIGHT)

        main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main, padding=6)
        center = ttk.Frame(main, padding=4)
        right = ttk.Frame(main, padding=6)
        main.add(left, weight=1)
        main.add(center, weight=5)
        main.add(right, weight=2)

        ttk.Label(left, text="Trajectories / Steps", font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor=tk.W, pady=(0, 6)
        )
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        tree_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)

        self.canvas = tk.Canvas(center, background="#181a1f", highlightthickness=0)
        hbar = ttk.Scrollbar(center, orient=tk.HORIZONTAL, command=self.canvas.xview)
        vbar = ttk.Scrollbar(center, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        center.rowconfigure(0, weight=1)
        center.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda _event: self._render() if self.fit_to_window.get() else None)

        ttk.Label(right, text="Box details", font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor=tk.W, pady=(0, 6)
        )
        self.details = tk.Text(
            right,
            wrap=tk.WORD,
            width=38,
            font=("Consolas", 10),
            background="#f7f7f8",
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        detail_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set)
        self.details.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        detail_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(self, textvariable=self.status, anchor=tk.W, padding=(8, 5)).pack(fill=tk.X)
        self.bind("<Left>", lambda _event: self._move(-1))
        self.bind("<Right>", lambda _event: self._move(1))

    def _load_manifest(self) -> None:
        if not MANIFEST_PATH.exists():
            messagebox.showerror("Missing manifest", f"Run build_annotations.py first:\n{MANIFEST_PATH}")
            return
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.records = data.get("records", [])
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, record in enumerate(self.records):
            groups.setdefault(record["trajectory"], []).append((index, record))
        for trajectory, items in groups.items():
            parent = self.tree.insert("", tk.END, text=f"{trajectory}  ({len(items)})", open=True)
            for index, record in items:
                kind = record.get("action", {}).get("action", "unknown")
                action = record.get("action", {})
                if kind in {"click", "long_press"}:
                    coordinate = action.get("coordinate", [])
                    action_text = f"{kind} ({coordinate[0]},{coordinate[1]})" if len(coordinate) >= 2 else kind
                elif kind == "swipe":
                    start = action.get("start_coordinate", [])
                    end = action.get("end_coordinate", [])
                    action_text = (
                        f"swipe ({start[0]},{start[1]})→({end[0]},{end[1]})"
                        if len(start) >= 2 and len(end) >= 2
                        else kind
                    )
                else:
                    action_text = kind
                confidence = record.get("box", {}).get("confidence", 0)
                item = self.tree.insert(
                    parent,
                    tk.END,
                    text=f"step{record['step']:03d}  {action_text}  [{confidence:.2f}]",
                )
                self.record_by_item[item] = index
        if self.records:
            self._select_index(0)
        self.status.set(
            f"{len(self.records)} annotated images · "
            f"{data.get('skipped_count', 0)} skipped actions · "
            f"{data.get('missing_count', 0)} missing source images"
        )

    def _tree_selected(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if selection and selection[0] in self.record_by_item:
            self.current_index = self.record_by_item[selection[0]]
            self._show_current()

    def _select_index(self, index: int) -> None:
        if not self.records:
            return
        self.current_index = max(0, min(len(self.records) - 1, index))
        for item, value in self.record_by_item.items():
            if value == self.current_index:
                self.tree.selection_set(item)
                self.tree.focus(item)
                self.tree.see(item)
                break
        self._show_current()

    def _move(self, delta: int) -> None:
        self._select_index(self.current_index + delta)

    def _show_current(self) -> None:
        if not self.records:
            return
        record = self.records[self.current_index]
        if self.show_annotated.get():
            path = PROJECT_DIR / "annotated" / record["annotated"]
        else:
            path = Path(record["original"])
        try:
            with Image.open(path) as opened:
                self.original_image = opened.convert("RGB")
        except OSError as exc:
            messagebox.showerror("Unable to open image", f"{path}\n\n{exc}")
            return
        self._render()
        payload = {
            "trajectory": record["trajectory"],
            "step": record["step"],
            "action": record["action"],
            "action_summary": record.get("action_summary", ""),
            "bbox": record["box"]["bbox"],
            "source": record["box"]["source"],
            "confidence": record["box"]["confidence"],
            "reason": record["box"]["reason"],
            "target": record["box"]["target"],
            "xml_source": record["xml_source"],
            "rule_box": record.get("rule_box"),
            "qwen": record.get("qwen"),
            "original": record["original"],
            "annotated": str(PROJECT_DIR / "annotated" / record["annotated"]),
        }
        self.details.configure(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        self.details.configure(state=tk.DISABLED)
        self.status.set(
            f"{self.current_index + 1}/{len(self.records)} · {record['trajectory']} · step{record['step']:03d}"
        )

    def _render(self) -> None:
        if self.original_image is None:
            return
        image = self.original_image
        if self.fit_to_window.get():
            available_w = max(100, self.canvas.winfo_width() - 20)
            available_h = max(100, self.canvas.winfo_height() - 20)
            scale = min(available_w / image.width, available_h / image.height)
        else:
            scale = 1.0
        size = max(1, round(image.width * scale)), max(1, round(image.height * scale))
        shown = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.delete("all")
        self.canvas.create_image(10, 10, image=self.photo, anchor=tk.NW)
        if self.show_raw_action.get() and self.records:
            self._draw_raw_action(self.records[self.current_index], size)
        self.canvas.configure(scrollregion=(0, 0, size[0] + 20, size[1] + 20))

    def _draw_raw_action(self, record: dict[str, Any], shown_size: tuple[int, int]) -> None:
        """Overlay the recorded click point or swipe path without changing saved images."""
        action = record.get("action", {})
        kind = str(action.get("action", "unknown"))
        original_size = record.get("image_size", [shown_size[0], shown_size[1]])
        sx = shown_size[0] / max(1, float(original_size[0]))
        sy = shown_size[1] / max(1, float(original_size[1]))

        def point(value: Any) -> tuple[float, float] | None:
            if not isinstance(value, list) or len(value) < 2:
                return None
            try:
                return 10 + float(value[0]) * sx, 10 + float(value[1]) * sy
            except (TypeError, ValueError):
                return None

        if kind in {"click", "long_press"}:
            location = point(action.get("coordinate"))
            if location is None:
                return
            x, y = location
            radius = max(8, min(16, round(min(shown_size) / 55)))
            color = "#ffe600" if kind == "click" else "#ff9f0a"
            self.canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                outline=color,
                width=4,
                tags=("raw_action",),
            )
            self.canvas.create_line(
                x - radius - 7, y, x + radius + 7, y, fill=color, width=3, tags=("raw_action",)
            )
            self.canvas.create_line(
                x, y - radius - 7, x, y + radius + 7, fill=color, width=3, tags=("raw_action",)
            )
            coordinate = action.get("coordinate", [])
            self.canvas.create_text(
                x + radius + 8,
                y - radius - 8,
                text=f"RAW {kind} ({coordinate[0]}, {coordinate[1]})",
                anchor=tk.SW,
                fill=color,
                font=("Microsoft YaHei UI", 10, "bold"),
                tags=("raw_action",),
            )
            return

        if kind == "swipe":
            start = point(action.get("start_coordinate"))
            end = point(action.get("end_coordinate"))
            if start is None or end is None:
                return
            x1, y1 = start
            x2, y2 = end
            color = "#ffe600"
            self.canvas.create_line(
                x1,
                y1,
                x2,
                y2,
                fill=color,
                width=5,
                arrow=tk.LAST,
                arrowshape=(18, 22, 8),
                tags=("raw_action",),
            )
            for x, y, label in ((x1, y1, "S"), (x2, y2, "E")):
                self.canvas.create_oval(
                    x - 9, y - 9, x + 9, y + 9, fill="#181a1f", outline=color, width=3,
                    tags=("raw_action",),
                )
                self.canvas.create_text(
                    x, y, text=label, fill=color, font=("Arial", 8, "bold"), tags=("raw_action",)
                )
            start_raw = action.get("start_coordinate", [])
            end_raw = action.get("end_coordinate", [])
            self.canvas.create_text(
                x1 + 12,
                y1 - 12,
                text=f"RAW swipe ({start_raw[0]}, {start_raw[1]}) → ({end_raw[0]}, {end_raw[1]})",
                anchor=tk.SW,
                fill=color,
                font=("Microsoft YaHei UI", 10, "bold"),
                tags=("raw_action",),
            )

    def _rebuild_hint(self) -> None:
        messagebox.showinfo(
            "Rebuild annotations",
            "Run run_build.cmd, then restart this viewer to reload the manifest.",
        )


if __name__ == "__main__":
    ActionBoxViewer().mainloop()
