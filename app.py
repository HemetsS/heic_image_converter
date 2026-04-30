"""Tkinter GUI for the HEIC image converter.

Originals are never touched. Converted images go either next to the original
or into a user-chosen output folder.

Required dependencies (installed via uv):
    * tkinterdnd2  - drag & drop files/folders onto the window
    * ttkbootstrap - modern themed widgets (dark mode, HiDPI)
"""
from __future__ import annotations

import threading
from pathlib import Path
from queue import Queue, Empty
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ttkbootstrap as tb
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


def _make_root() -> tk.Tk:
    """Tk root with the ttkbootstrap dark theme + drag-and-drop wired in."""
    root = tb.Window(themename="darkly")
    # Bolt TkinterDnD onto the bootstrap window so DND_* events fire.
    try:
        TkinterDnD._require(root)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return root


class HeicConverterApp:
    def __init__(self) -> None:
        self.root = _make_root()
        self.root.title("HEIC Image Converter")
        self.root.geometry("1180x720")
        self.root.minsize(960, 600)

        self._items: list[ConversionItem] = []
        self._tree_iid_to_index: dict[str, int] = {}
        self._job: ConversionJob | None = None
        self._job_thread: threading.Thread | None = None
        self._event_queue: Queue = Queue()
        self._preview_imgtk: ImageTk.PhotoImage | None = None  # keep ref alive

        self._build_ui()
        self._enable_dnd()
        self.root.after(100, self._drain_events)

    def mainloop(self) -> None:
        self.root.mainloop()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        # Top: source selection
        top = ttk.LabelFrame(self.root, text="1. Source (or drag & drop here)")
        top.pack(fill="x", **pad)
        self.source_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.source_var).pack(
            side="left", fill="x", expand=True, padx=6, pady=6
        )
        ttk.Button(top, text="Pick folder…", command=self._pick_folder).pack(side="left", padx=3, pady=6)
        ttk.Button(top, text="Pick files…", command=self._pick_files).pack(side="left", padx=3, pady=6)
        ttk.Button(top, text="Scan", command=self._scan).pack(side="left", padx=3, pady=6)

        # Middle: split pane (file list | preview)
        middle = ttk.LabelFrame(self.root, text="2. Select files to convert")
        middle.pack(fill="both", expand=True, **pad)

        paned = ttk.PanedWindow(middle, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # Left side: list + toolbar
        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        toolbar = ttk.Frame(left)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Select all", command=lambda: self._set_all(True)).pack(
            side="left", padx=3, pady=3
        )
        ttk.Button(toolbar, text="Select none", command=lambda: self._set_all(False)).pack(
            side="left", padx=3, pady=3
        )
        self.count_var = tk.StringVar(value="No files scanned")
        ttk.Label(toolbar, textvariable=self.count_var).pack(side="right", padx=6)

        cols = ("sel", "path", "size", "status")
        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("sel", text="✓")
        self.tree.heading("path", text="File")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.column("sel", width=40, anchor="center", stretch=False)
        self.tree.column("path", width=520, anchor="w")
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("status", width=240, anchor="w")
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
        vsb.pack(side="right", fill="y", pady=2)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Right side: preview pane
        right = ttk.LabelFrame(paned, text="Preview")
        paned.add(right, weight=2)
        self.preview_label = ttk.Label(right, anchor="center", text="(select a file)")
        self.preview_label.pack(fill="both", expand=True, padx=4, pady=4)
        self.preview_info = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.preview_info, anchor="w", justify="left").pack(
            fill="x", padx=4, pady=(0, 4)
        )

        # Options
        opts = ttk.LabelFrame(self.root, text="3. Options")
        opts.pack(fill="x", **pad)
        self._build_options(opts)

        # Bottom: progress + actions
        bottom = ttk.LabelFrame(self.root, text="4. Convert")
        bottom.pack(fill="x", **pad)

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", padx=6, pady=4)
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w", padx=6)

        btns = ttk.Frame(bottom)
        btns.pack(fill="x", padx=6, pady=6)
        self.start_btn = ttk.Button(btns, text="Start conversion", command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)

    def _build_options(self, parent: "ttk.LabelFrame") -> None:
        # Row 0: format + quality
        ttk.Label(parent, text="Output format:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.format_var = tk.StringVar(value=list(SUPPORTED_FORMATS.keys())[0])
        format_box = ttk.Combobox(
            parent, textvariable=self.format_var,
            values=list(SUPPORTED_FORMATS.keys()), state="readonly", width=28,
        )
        format_box.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        format_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_quality_state())

        ttk.Label(parent, text="Quality:").grid(row=0, column=2, sticky="e", padx=6, pady=4)
        self.quality_var = tk.IntVar(value=92)
        self.quality_scale = ttk.Scale(
            parent, from_=50, to=100, orient="horizontal",
            variable=self.quality_var, command=lambda v: self.quality_label_var.set(f"{int(float(v))}"),
        )
        self.quality_scale.grid(row=0, column=3, sticky="we", padx=6, pady=4)
        self.quality_label_var = tk.StringVar(value="92")
        ttk.Label(parent, textvariable=self.quality_label_var, width=4).grid(row=0, column=4, padx=2)

        # Row 1: output mode
        ttk.Label(parent, text="Output location:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.output_mode_var = tk.StringVar(value=OutputMode.SAME_AS_ORIGINAL.value)
        mode_frame = ttk.Frame(parent)
        mode_frame.grid(row=1, column=1, columnspan=4, sticky="we")
        ttk.Radiobutton(
            mode_frame, text="Same folder as original",
            value=OutputMode.SAME_AS_ORIGINAL.value, variable=self.output_mode_var,
            command=self._refresh_output_mode_state,
        ).pack(side="left", padx=4)
        ttk.Radiobutton(
            mode_frame, text="Custom folder:",
            value=OutputMode.CUSTOM_FOLDER.value, variable=self.output_mode_var,
            command=self._refresh_output_mode_state,
        ).pack(side="left", padx=4)
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(mode_frame, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.output_browse = ttk.Button(mode_frame, text="Browse…", command=self._pick_output)
        self.output_browse.pack(side="left", padx=4)

        # Row 2: conflict policy + workers
        ttk.Label(parent, text="If output exists:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.conflict_var = tk.StringVar(value=ConflictPolicy.SKIP.value)
        ttk.Combobox(
            parent, textvariable=self.conflict_var, state="readonly", width=24,
            values=[p.value for p in ConflictPolicy],
        ).grid(row=2, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(parent, text="Parallel workers:").grid(row=2, column=2, sticky="e", padx=6, pady=4)
        self.workers_var = tk.IntVar(value=4)
        ttk.Spinbox(parent, from_=1, to=32, textvariable=self.workers_var, width=5).grid(
            row=2, column=3, sticky="w", padx=6, pady=4
        )

        # Row 3+: feature toggles
        toggles = ttk.Frame(parent)
        toggles.grid(row=3, column=0, columnspan=5, sticky="we", padx=4, pady=2)
        self.exif_var = tk.BooleanVar(value=True)
        self.rotate_var = tk.BooleanVar(value=True)
        self.organize_var = tk.BooleanVar(value=False)
        self.live_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=True)
        self.cache_var = tk.BooleanVar(value=True)
        self.dryrun_var = tk.BooleanVar(value=False)
        self.log_var = tk.BooleanVar(value=True)
        for i, (txt, var) in enumerate([
            ("Preserve EXIF", self.exif_var),
            ("Auto-rotate (EXIF orientation)", self.rotate_var),
            ("Organize by capture date (YYYY/YYYY-MM-DD/)", self.organize_var),
            ("Copy Live Photo .MOV companion", self.live_var),
            ("Verify after writing", self.verify_var),
            ("Hash cache (skip already-converted)", self.cache_var),
            ("Dry run (don't write any files)", self.dryrun_var),
            ("Write conversion-log.txt", self.log_var),
        ]):
            ttk.Checkbutton(toggles, text=txt, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=8, pady=2
            )

        parent.columnconfigure(3, weight=1)
        self._refresh_quality_state()
        self._refresh_output_mode_state()

    def _refresh_quality_state(self) -> None:
        _fmt, _ext, supports_quality, _kw = SUPPORTED_FORMATS[self.format_var.get()]
        state = "normal" if supports_quality else "disabled"
        self.quality_scale.configure(state=state)

    def _refresh_output_mode_state(self) -> None:
        custom = self.output_mode_var.get() == OutputMode.CUSTOM_FOLDER.value
        state = "normal" if custom else "disabled"
        self.output_entry.configure(state=state)
        self.output_browse.configure(state=state)

    # ---------------------------------------------------------------- DnD
    def _enable_dnd(self) -> None:
        try:
            self.root.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            self.root.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def _on_drop(self, event) -> None:
        # Tk encodes paths with braces around those that contain spaces.
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

    # ---------------------------------------------------------------- Pickers
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

    # ---------------------------------------------------------------- Scan
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
        # De-duplicate by source path while preserving order.
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
            )
            self._tree_iid_to_index[iid] = idx
        self.count_var.set(f"{len(self._items)} HEIC file(s) found")

    def _set_all(self, value: bool) -> None:
        for it in self._items:
            it.selected = value
        for iid, _ in self._tree_iid_to_index.items():
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
        iid = sel[0]
        idx = self._tree_iid_to_index.get(iid)
        if idx is None:
            return
        self._show_preview(self._items[idx].source)

    # ---------------------------------------------------------------- Preview
    def _show_preview(self, path: Path) -> None:
        try:
            with Image.open(path) as img:
                img.thumbnail((420, 420))
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                self._preview_imgtk = ImageTk.PhotoImage(img.copy())
                size = img.size
            self.preview_label.configure(image=self._preview_imgtk, text="")
            try:
                fsize = human_size(path.stat().st_size)
            except OSError:
                fsize = "?"
            self.preview_info.set(f"{path.name}\n{size[0]}×{size[1]} px • {fsize}")
        except Exception as e:  # noqa: BLE001
            self.preview_label.configure(image="", text=f"Preview failed:\n{e}")
            self._preview_imgtk = None
            self.preview_info.set(str(path))

    # ---------------------------------------------------------------- Run
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
            copy_live_photo=self.live_var.get(),
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

        self.progress.configure(value=0, maximum=len(selected))
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

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
        self.progress.configure(value=p.completed, maximum=p.total)
        for iid, idx in self._tree_iid_to_index.items():
            if self._items[idx].source == item.source:
                msg = f"{item.status}: {item.message}" if item.message else item.status
                self.tree.set(iid, "status", msg)
                break
        self.status_var.set(
            f"{p.completed}/{p.total}  •  ok={p.succeeded}  skip={p.skipped}  err={p.failed}  "
            f"•  elapsed {format_duration(p.elapsed)}  •  ETA {format_duration(p.eta)}"
        )

    def _on_done(self, p: ProgressInfo) -> None:
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status_var.set(
            f"Done. {p.succeeded} converted, {p.skipped} skipped, {p.failed} failed "
            f"in {format_duration(p.elapsed)}."
        )
        self._job = None


def main() -> None:
    HeicConverterApp().mainloop()


if __name__ == "__main__":
    main()
