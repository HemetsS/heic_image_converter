"""Modern Tkinter GUI for the HEIC image converter.

Originals are never touched. Converted images go either next to the original
or into a user-chosen output folder.

Required dependencies (installed via uv):
    * tkinterdnd2  - drag & drop files/folders onto the window
    * ttkbootstrap - modern themed widgets (dark mode, HiDPI)
"""
from __future__ import annotations

import os
import platform
import subprocess
import threading
from pathlib import Path
from queue import Queue, Empty
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

import ttkbootstrap as tb
from ttkbootstrap.constants import (
    DANGER, INFO, OUTLINE, PRIMARY, SECONDARY, SUCCESS, WARNING,
)
from ttkbootstrap.widgets import Floodgauge
from tkinterdnd2 import TkinterDnD, DND_FILES

from PIL import Image, ImageTk

from converter import (
    ConflictPolicy,
    ConversionItem,
    ConversionJob,
    ConverterConfig,
    OutputMode,
    ProgressInfo,
    SUPPORTED_FORMATS,
    find_heic_files,
    format_duration,
    human_size,
)

DARK_THEMES = ("darkly", "cyborg", "superhero", "vapor", "solar")
LIGHT_THEMES = ("flatly", "cosmo", "litera", "minty", "yeti", "lumen")
DEFAULT_THEME = "darkly"

# Status -> bootstrap colour style
STATUS_STYLES: dict[str, str] = {
    "pending": SECONDARY,
    "done": SUCCESS,
    "skipped": WARNING,
    "error": DANGER,
    "dry-run": INFO,
}


def _make_root() -> tk.Tk:
    root = tb.Window(themename=DEFAULT_THEME)
    try:
        TkinterDnD._require(root)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return root


def _card(parent: tk.Misc, title: str, subtitle: str | None = None) -> ttk.Frame:
    """Modern card: a bordered frame with a heading row and a content body."""
    outer = ttk.Frame(parent, padding=0, bootstyle="secondary")  # type: ignore[arg-type]
    inner = ttk.Frame(outer, padding=(16, 12, 16, 14))
    inner.pack(fill="both", expand=True)

    head = ttk.Frame(inner)
    head.pack(fill="x", pady=(0, 10))
    ttk.Label(head, text=title, font=("Segoe UI Semibold", 12)).pack(side="left")
    if subtitle:
        ttk.Label(head, text=subtitle, bootstyle="secondary").pack(side="left", padx=(10, 0))

    body = ttk.Frame(inner)
    body.pack(fill="both", expand=True)
    # Expose body as the card so callers can pack their widgets into it.
    outer.body = body  # type: ignore[attr-defined]
    return outer


class HeicConverterApp:
    def __init__(self) -> None:
        self.root = _make_root()
        self.root.title("HEIC Image Converter")
        self.root.geometry("1280x820")
        # A reasonable lower bound; refined after the UI has been laid out
        # so it always matches the actual required size of all widgets.
        self.root.minsize(1060, 680)

        # Slightly bigger default font for a modern feel.
        try:
            tkfont.nametofont("TkDefaultFont").configure(family="Segoe UI", size=10)
            tkfont.nametofont("TkTextFont").configure(family="Segoe UI", size=10)
            tkfont.nametofont("TkHeadingFont").configure(family="Segoe UI Semibold", size=10)
        except tk.TclError:
            pass

        self._items: list[ConversionItem] = []
        self._tree_iid_to_index: dict[str, int] = {}
        self._job: ConversionJob | None = None
        self._job_thread: threading.Thread | None = None
        self._event_queue: Queue = Queue()
        self._preview_imgtk: ImageTk.PhotoImage | None = None

        self._configure_treeview_style()
        self._build_ui()
        self._enable_dnd()
        # After the UI is laid out, force-fit the window so every component
        # is visible. We use the natural required size as the minimum and
        # clamp the initial size to the available screen area.
        self.root.after(0, self._fit_window)
        self.root.after(100, self._drain_events)

    def _fit_window(self) -> None:
        """Set minsize to the required size and clamp the window to the screen."""
        self.root.update_idletasks()
        req_w = max(self.root.winfo_reqwidth(), 900)
        req_h = max(self.root.winfo_reqheight(), 600)

        # Leave some breathing room for the OS taskbar / window chrome.
        screen_w = self.root.winfo_screenwidth() - 80
        screen_h = self.root.winfo_screenheight() - 120

        min_w = min(req_w, screen_w)
        min_h = min(req_h, screen_h)
        self.root.minsize(min_w, min_h)

        # If the current geometry is smaller than the required size, grow it.
        cur_w = max(self.root.winfo_width(), min_w)
        cur_h = max(self.root.winfo_height(), min_h)
        cur_w = min(cur_w, screen_w)
        cur_h = min(cur_h, screen_h)
        self.root.geometry(f"{cur_w}x{cur_h}")

    def mainloop(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------ Style
    def _configure_treeview_style(self) -> None:
        style = tb.Style()
        # Slightly taller rows for breathing room.
        style.configure("Treeview", rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10), padding=6)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # --- Header bar ----------------------------------------------------
        header = ttk.Frame(self.root, padding=(20, 14, 20, 6))
        header.pack(fill="x")

        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        ttk.Label(
            title_box, text="HEIC Image Converter",
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w")
        ttk.Label(
            title_box,
            text="Convert Apple HEIC photos. Originals are never modified.",
            bootstyle="secondary",
        ).pack(anchor="w")

        right_box = ttk.Frame(header)
        right_box.pack(side="right")
        ttk.Label(right_box, text="Theme:", bootstyle="secondary").pack(side="left", padx=(0, 6))
        self.theme_var = tk.StringVar(value=DEFAULT_THEME)
        theme_box = ttk.Combobox(
            right_box, textvariable=self.theme_var, state="readonly",
            values=list(DARK_THEMES + LIGHT_THEMES), width=14,
        )
        theme_box.pack(side="left")
        theme_box.bind("<<ComboboxSelected>>", lambda _e: tb.Style().theme_use(self.theme_var.get()))

        ttk.Separator(self.root).pack(fill="x", padx=20, pady=(6, 8))

        # --- Bottom action bar (packed FIRST so it's always reserved) -----
        # Without this, a long file list would push the start/cancel buttons
        # off the bottom of the window.
        action_bar_container = ttk.Frame(self.root)
        action_bar_container.pack(side="bottom", fill="x")
        ttk.Separator(action_bar_container).pack(fill="x", padx=20, pady=(0, 0))
        self._build_action_bar(action_bar_container).pack(
            fill="x", padx=20, pady=(8, 14)
        )

        # --- Main 2-column layout (fills the remaining space) -------------
        main = ttk.Frame(self.root, padding=(20, 4, 20, 0))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(0, weight=1)

        # Left column = source + file list
        left_col = ttk.Frame(main)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_col.rowconfigure(1, weight=1)
        left_col.columnconfigure(0, weight=1)

        self._build_source_card(left_col).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._build_files_card(left_col).grid(row=1, column=0, sticky="nsew")

        # Right column = preview + options
        right_col = ttk.Frame(main)
        right_col.grid(row=0, column=1, sticky="nsew")
        right_col.rowconfigure(1, weight=1)
        right_col.columnconfigure(0, weight=1)

        self._build_preview_card(right_col).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._build_options_card(right_col).grid(row=1, column=0, sticky="nsew")

    # ------------------------------------------------------------------ Cards
    def _build_source_card(self, parent: tk.Misc) -> ttk.Frame:
        card = _card(parent, "1 · Source", "drag & drop files or folders here")
        body: ttk.Frame = card.body  # type: ignore[attr-defined]

        self.source_var = tk.StringVar()
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.source_var).pack(
            side="left", fill="x", expand=True, ipady=3
        )
        ttk.Button(
            row, text="📁  Folder…", command=self._pick_folder, bootstyle=(SECONDARY, OUTLINE),
        ).pack(side="left", padx=(8, 4))
        ttk.Button(
            row, text="🖼  Files…", command=self._pick_files, bootstyle=(SECONDARY, OUTLINE),
        ).pack(side="left", padx=4)
        ttk.Button(
            row, text="🔍  Scan", command=self._scan, bootstyle=PRIMARY,
        ).pack(side="left", padx=(8, 0))
        return card

    def _build_files_card(self, parent: tk.Misc) -> ttk.Frame:
        card = _card(parent, "2 · Files to convert")
        body: ttk.Frame = card.body  # type: ignore[attr-defined]

        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(
            toolbar, text="Select all", command=lambda: self._set_all(True),
            bootstyle=(SECONDARY, OUTLINE),
        ).pack(side="left")
        ttk.Button(
            toolbar, text="Select none", command=lambda: self._set_all(False),
            bootstyle=(SECONDARY, OUTLINE),
        ).pack(side="left", padx=(6, 0))
        self.count_var = tk.StringVar(value="No files scanned yet")
        ttk.Label(toolbar, textvariable=self.count_var, bootstyle="secondary").pack(
            side="right"
        )

        list_frame = ttk.Frame(body)
        list_frame.pack(fill="both", expand=True)

        cols = ("sel", "path", "size", "status")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("sel", text="✓")
        self.tree.heading("path", text="File")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.column("sel", width=44, anchor="center", stretch=False)
        self.tree.column("path", width=520, anchor="w")
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("status", width=240, anchor="w")

        # Color tags for status rows.
        self.tree.tag_configure("done", foreground="#7ddc7d")
        self.tree.tag_configure("error", foreground="#ff6b6b")
        self.tree.tag_configure("skipped", foreground="#f0ad4e")
        self.tree.tag_configure("dry-run", foreground="#5bc0de")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        return card

    def _build_preview_card(self, parent: tk.Misc) -> ttk.Frame:
        card = _card(parent, "Preview")
        body: ttk.Frame = card.body  # type: ignore[attr-defined]

        # Fixed-size container so the image never resizes the surrounding
        # layout. pack_propagate(False) keeps the frame at the requested
        # width/height regardless of the label's contents.
        self._preview_box_size = (320, 220)
        box = ttk.Frame(body, width=self._preview_box_size[0], height=self._preview_box_size[1])
        box.pack(anchor="center")
        box.pack_propagate(False)

        self.preview_label = ttk.Label(
            box, anchor="center", text="Select a file from the list\n(double-click to open)",
            bootstyle="secondary", cursor="hand2",
        )
        self.preview_label.pack(fill="both", expand=True)
        self.preview_label.bind("<Double-Button-1>", self._open_preview_file)

        self.preview_info = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.preview_info, anchor="w", justify="left",
                  bootstyle="secondary").pack(fill="x", pady=(8, 0))
        return card

    def _build_options_card(self, parent: tk.Misc) -> ttk.Frame:
        card = _card(parent, "3 · Options")
        body: ttk.Frame = card.body  # type: ignore[attr-defined]

        nb = ttk.Notebook(body, bootstyle=SECONDARY)
        nb.pack(fill="both", expand=True)

        # --- Tab: Output --------------------------------------------------
        tab_out = ttk.Frame(nb, padding=12)
        nb.add(tab_out, text="  Output  ")

        ttk.Label(tab_out, text="Format").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.format_var = tk.StringVar(value=list(SUPPORTED_FORMATS.keys())[0])
        format_box = ttk.Combobox(
            tab_out, textvariable=self.format_var,
            values=list(SUPPORTED_FORMATS.keys()), state="readonly",
        )
        format_box.grid(row=1, column=0, columnspan=3, sticky="we", pady=(0, 12))
        format_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_quality_state())

        ttk.Label(tab_out, text="Quality").grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.quality_var = tk.IntVar(value=92)
        self.quality_label_var = tk.StringVar(value="92")
        self.quality_scale = ttk.Scale(
            tab_out, from_=50, to=100, orient="horizontal",
            variable=self.quality_var, bootstyle=PRIMARY,
            command=lambda v: self.quality_label_var.set(f"{int(float(v))}"),
        )
        self.quality_scale.grid(row=3, column=0, columnspan=2, sticky="we", pady=(0, 12))
        ttk.Label(tab_out, textvariable=self.quality_label_var, width=4).grid(
            row=3, column=2, padx=(8, 0)
        )

        ttk.Label(tab_out, text="Output location").grid(row=4, column=0, sticky="w", pady=(0, 4))
        self.output_mode_var = tk.StringVar(value=OutputMode.SAME_AS_ORIGINAL.value)
        mode_frame = ttk.Frame(tab_out)
        mode_frame.grid(row=5, column=0, columnspan=3, sticky="we", pady=(0, 6))
        ttk.Radiobutton(
            mode_frame, text="Same folder as original",
            value=OutputMode.SAME_AS_ORIGINAL.value, variable=self.output_mode_var,
            command=self._refresh_output_mode_state, bootstyle="primary-toolbutton",
        ).pack(side="left", padx=(0, 6), ipadx=6, ipady=2)
        ttk.Radiobutton(
            mode_frame, text="Custom folder",
            value=OutputMode.CUSTOM_FOLDER.value, variable=self.output_mode_var,
            command=self._refresh_output_mode_state, bootstyle="primary-toolbutton",
        ).pack(side="left", ipadx=6, ipady=2)

        out_row = ttk.Frame(tab_out)
        out_row.grid(row=6, column=0, columnspan=3, sticky="we", pady=(4, 12))
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(out_row, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.output_browse = ttk.Button(
            out_row, text="Browse…", command=self._pick_output, bootstyle=(SECONDARY, OUTLINE),
        )
        self.output_browse.pack(side="left", padx=(8, 0))

        tab_out.columnconfigure(0, weight=1)
        tab_out.columnconfigure(1, weight=1)

        # --- Tab: Advanced ------------------------------------------------
        tab_feat = ttk.Frame(nb, padding=12)
        nb.add(tab_feat, text="  Advanced  ")

        # Row 0/1: conflict policy + parallel workers
        ttk.Label(tab_feat, text="If output exists").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.conflict_var = tk.StringVar(value=ConflictPolicy.SKIP.value)
        ttk.Combobox(
            tab_feat, textvariable=self.conflict_var, state="readonly",
            values=[p.value for p in ConflictPolicy],
        ).grid(row=1, column=0, sticky="we", pady=(0, 10), padx=(0, 8))

        ttk.Label(tab_feat, text="Parallel workers").grid(row=0, column=1, sticky="w", pady=(0, 4))
        self.workers_var = tk.IntVar(value=4)
        ttk.Spinbox(tab_feat, from_=1, to=32, textvariable=self.workers_var).grid(
            row=1, column=1, sticky="we", pady=(0, 10)
        )

        ttk.Separator(tab_feat).grid(row=2, column=0, columnspan=2, sticky="we", pady=(2, 8))

        self.exif_var = tk.BooleanVar(value=True)
        self.rotate_var = tk.BooleanVar(value=True)
        self.organize_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=True)
        self.cache_var = tk.BooleanVar(value=True)
        self.dryrun_var = tk.BooleanVar(value=False)
        self.log_var = tk.BooleanVar(value=True)

        toggles = [
            ("Preserve EXIF metadata", self.exif_var),
            ("Auto-rotate (EXIF orientation)", self.rotate_var),
            ("Organize by capture date (YYYY/YYYY-MM-DD/)", self.organize_var),
            ("Verify after writing", self.verify_var),
            ("Hash cache (skip already-converted)", self.cache_var),
            ("Dry run (don't write any files)", self.dryrun_var),
            ("Write conversion-log.txt", self.log_var),
        ]
        for i, (txt, var) in enumerate(toggles):
            ttk.Checkbutton(
                tab_feat, text=txt, variable=var, bootstyle="round-toggle",
            ).grid(row=3 + i, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        tab_feat.columnconfigure(0, weight=1)
        tab_feat.columnconfigure(1, weight=1)

        self._refresh_quality_state()
        self._refresh_output_mode_state()
        return card

    def _build_action_bar(self, parent: tk.Misc) -> ttk.Frame:
        bar = ttk.Frame(parent)

        # Floodgauge: a modern progress bar that shows the % inside.
        # NOTE: maximum is fixed at 100 and `value` is the percentage; that way
        # the bar fill matches the percentage shown in its text label.
        self.gauge = Floodgauge(
            bar, length=300, mode="determinate", maximum=100, value=0,
            bootstyle=INFO, font=("Segoe UI Semibold", 11), text="Idle",
        )
        self.gauge.pack(side="left", fill="x", expand=True, padx=(0, 16))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, bootstyle="secondary").pack(
            side="left", padx=(0, 16)
        )

        self.cancel_btn = ttk.Button(
            bar, text="Cancel", command=self._cancel,
            state="disabled", bootstyle=(DANGER, OUTLINE), width=10,
        )
        self.cancel_btn.pack(side="right")
        self.start_btn = ttk.Button(
            bar, text="▶  Start conversion", command=self._start,
            bootstyle=SUCCESS, width=22,
        )
        self.start_btn.pack(side="right", padx=(0, 8))
        self.open_output_btn = ttk.Button(
            bar, text="📂  Open output", command=self._open_output_folder,
            bootstyle=(INFO, OUTLINE), width=16, state="disabled",
        )
        self.open_output_btn.pack(side="right", padx=(0, 8))
        self._last_output_root: Path | None = None
        return bar

    # ------------------------------------------------------------------ State
    def _refresh_quality_state(self) -> None:
        _fmt, _ext, supports_quality, _kw = SUPPORTED_FORMATS[self.format_var.get()]
        self.quality_scale.configure(state="normal" if supports_quality else "disabled")

    def _refresh_output_mode_state(self) -> None:
        custom = self.output_mode_var.get() == OutputMode.CUSTOM_FOLDER.value
        state = "normal" if custom else "disabled"
        self.output_entry.configure(state=state)
        self.output_browse.configure(state=state)

    # ------------------------------------------------------------------ DnD
    def _enable_dnd(self) -> None:
        try:
            self.root.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            self.root.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def _on_drop(self, event) -> None:
        raw = event.data
        paths: list[str] = []
        buf = ""
        in_brace = False
        for ch in raw:
            if ch == "{":
                in_brace = True
                buf = ""
            elif ch == "}":
                in_brace = False
                paths.append(buf)
                buf = ""
            elif ch == " " and not in_brace:
                if buf:
                    paths.append(buf)
                    buf = ""
            else:
                buf += ch
        if buf:
            paths.append(buf)
        if paths:
            self.source_var.set(";".join(paths))
            self._scan()

    # ------------------------------------------------------------------ Pickers
    def _pick_folder(self) -> None:
        path = filedialog.askdirectory(title="Pick folder to scan")
        if path:
            self.source_var.set(path)

    def _pick_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="Pick HEIC files",
            filetypes=[("HEIC images", "*.heic *.HEIC *.heif *.HEIF"), ("All files", "*.*")],
        )
        if files:
            self.source_var.set(";".join(files))

    def _pick_output(self) -> None:
        p = filedialog.askdirectory(title="Pick output folder")
        if p:
            self.output_var.set(p)

    # ------------------------------------------------------------------ Scan
    def _scan(self) -> None:
        raw = self.source_var.get().strip()
        if not raw:
            messagebox.showwarning("No source", "Pick a folder or files first.")
            return
        roots = [Path(p) for p in raw.split(";") if p]
        items: list[ConversionItem] = []
        for r in roots:
            if not r.exists():
                continue
            for f in find_heic_files(r):
                items.append(ConversionItem(source=f, root=r))
        seen: set[Path] = set()
        unique: list[ConversionItem] = []
        for it in items:
            if it.source not in seen:
                unique.append(it)
                seen.add(it.source)
        self._items = unique
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._tree_iid_to_index.clear()
        for idx, it in enumerate(self._items):
            try:
                size_str = human_size(it.source.stat().st_size)
            except OSError:
                size_str = "?"
            iid = self.tree.insert(
                "", "end",
                values=("☑" if it.selected else "☐", str(it.source), size_str, it.status),
                tags=(it.status,),
            )
            self._tree_iid_to_index[iid] = idx
        self.count_var.set(f"{len(self._items)} HEIC file(s) found")

    def _set_all(self, value: bool) -> None:
        for it in self._items:
            it.selected = value
        for iid in self._tree_iid_to_index:
            self.tree.set(iid, "sel", "☑" if value else "☐")

    def _toggle(self, iid: str) -> None:
        idx = self._tree_iid_to_index.get(iid)
        if idx is None:
            return
        it = self._items[idx]
        it.selected = not it.selected
        self.tree.set(iid, "sel", "☑" if it.selected else "☐")

    def _on_tree_click(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle(iid)

    def _on_tree_space(self, event) -> str:
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

    def _on_tree_select(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self._tree_iid_to_index.get(sel[0])
        if idx is not None:
            self._show_preview(self._items[idx].source)

    # ------------------------------------------------------------------ Preview
    def _show_preview(self, path: Path) -> None:
        self._preview_path = path
        try:
            with Image.open(path) as img:
                img.thumbnail(self._preview_box_size)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                self._preview_imgtk = ImageTk.PhotoImage(img.copy())
                size = img.size
            self.preview_label.configure(image=self._preview_imgtk, text="")
            try:
                fsize = human_size(path.stat().st_size)
            except OSError:
                fsize = "?"
            self.preview_info.set(
                f"{path.name}\n{size[0]}×{size[1]} px • {fsize}  ·  double-click to open"
            )
        except Exception as e:  # noqa: BLE001
            self.preview_label.configure(image="", text=f"Preview failed:\n{e}")
            self._preview_imgtk = None
            self.preview_info.set(str(path))

    def _open_preview_file(self, _event=None) -> None:
        path = getattr(self, "_preview_path", None)
        if path is not None:
            self._open_path(path)

    def _open_output_folder(self) -> None:
        if self._last_output_root is not None and self._last_output_root.exists():
            self._open_path(self._last_output_root)

    @staticmethod
    def _open_path(path: Path) -> None:
        """Open a file or folder with the OS default handler."""
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Open failed", str(e))

    @staticmethod
    def _compute_output_root(cfg: ConverterConfig, items: list[ConversionItem]) -> Path | None:
        """Return the folder we'd 'open' after a successful run."""
        if cfg.output_mode == OutputMode.CUSTOM_FOLDER and cfg.output_dir is not None:
            return cfg.output_dir
        if items:
            r = items[0].root
            return r if r.is_dir() else r.parent
        return None

    # ------------------------------------------------------------------ Run
    def _start(self) -> None:
        selected = [i for i in self._items if i.selected]
        if not selected:
            messagebox.showwarning("Nothing to do", "No files selected.")
            return

        mode = OutputMode(self.output_mode_var.get())
        output_path: Path | None = None
        if mode == OutputMode.CUSTOM_FOLDER:
            txt = self.output_var.get().strip()
            if not txt:
                messagebox.showwarning("Output folder required", "Pick a custom output folder.")
                return
            output_path = Path(txt)
            try:
                output_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Output folder error", str(e))
                return

        cfg = ConverterConfig(
            output_format_label=self.format_var.get(),
            output_mode=mode,
            output_dir=output_path,
            quality=int(self.quality_var.get()),
            keep_exif=self.exif_var.get(),
            auto_rotate=self.rotate_var.get(),
            organize_by_date=self.organize_var.get(),
            verify_after=self.verify_var.get(),
            use_hash_cache=self.cache_var.get(),
            conflict_policy=ConflictPolicy(self.conflict_var.get()),
            dry_run=self.dryrun_var.get(),
            write_log=self.log_var.get(),
            workers=int(self.workers_var.get()),
        )

        for it in selected:
            it.status = "pending"
            it.message = ""
        self._refresh_tree()

        self.gauge.configure(value=0, maximum=100, bootstyle=INFO)
        self.gauge["text"] = f"0 / {len(selected)}"
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_output_btn.configure(state="disabled")
        self._last_output_root = self._compute_output_root(cfg, selected)

        self._job = ConversionJob(
            self._items, cfg,
            on_progress=lambda p, it: self._event_queue.put(("progress", p, it)),
            on_done=lambda p: self._event_queue.put(("done", p, None)),
        )
        self._job_thread = threading.Thread(target=self._job.run, daemon=True)
        self._job_thread.start()

    def _cancel(self) -> None:
        if self._job:
            self._job.cancel()
            self.status_var.set("Cancelling…")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, info, item = self._event_queue.get_nowait()
                if kind == "progress":
                    self._on_progress(info, item)
                elif kind == "done":
                    self._on_done(info)
        except Empty:
            pass
        self.root.after(100, self._drain_events)

    def _on_progress(self, p: ProgressInfo, item: ConversionItem) -> None:
        pct = (p.completed / p.total * 100.0) if p.total else 0.0
        self.gauge.configure(value=pct)
        self.gauge["text"] = f"{p.completed} / {p.total}  ({pct:.0f}%)"

        for iid, idx in self._tree_iid_to_index.items():
            if self._items[idx].source == item.source:
                msg = f"{item.status}: {item.message}" if item.message else item.status
                self.tree.set(iid, "status", msg)
                self.tree.item(iid, tags=(item.status,))
                break

        self.status_var.set(
            f"ok {p.succeeded} · skip {p.skipped} · err {p.failed}  "
            f"·  elapsed {format_duration(p.elapsed)}  ·  ETA {format_duration(p.eta)}"
        )

    def _on_done(self, p: ProgressInfo) -> None:
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        # Make sure the bar is exactly full when finished.
        self.gauge.configure(value=100)
        style = INFO if p.failed == 0 else (WARNING if p.succeeded else DANGER)
        self.gauge.configure(bootstyle=style)
        self.gauge["text"] = (
            f"Done  ·  {p.succeeded} ok · {p.skipped} skipped · {p.failed} failed"
        )
        self.status_var.set(f"Finished in {format_duration(p.elapsed)}.")
        if self._last_output_root is not None and self._last_output_root.exists():
            self.open_output_btn.configure(state="normal")
        self._job = None


def main() -> None:
    HeicConverterApp().mainloop()


if __name__ == "__main__":
    main()
