"""Core HEIC conversion logic.

Originals are NEVER modified, moved, or deleted. They are only read.
Converted files are written either next to the original (mirroring source
structure) or into a user-chosen output folder.

Features:
    * Output formats: JPEG / PNG / WebP, with quality slider (JPEG/WebP).
    * Auto-rotate via EXIF orientation; orientation tag stripped from output.
    * EXIF + ICC profile preserved.
    * Optional EXIF-date based foldering ("YYYY/YYYY-MM-DD/IMG_xxxx.jpg").
    * Conflict policy: skip / overwrite / rename-with-suffix.
    * Idempotent re-runs via SHA-256 cache stored next to the output root.
    * Live Photo (.MOV) companion files copied alongside if present.
    * Verify pass: re-opens each output to confirm decodability + dimensions.
    * Dry-run mode: no files written.
    * Per-output log file written into the output root.
    * Parallel workers; cancellable; progress + ETA callbacks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Optional

from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()

HEIC_EXTENSIONS = {".heic", ".heif"}
LIVE_PHOTO_EXTENSIONS = {".mov", ".MOV"}

# label -> (PIL format, default extension, supports_quality, default save kwargs)
SUPPORTED_FORMATS: dict[str, tuple[str, str, bool, dict]] = {
    "JPEG (recommended)": ("JPEG", ".jpg", True, {"optimize": True, "progressive": True}),
    "PNG (lossless, larger)": ("PNG", ".png", False, {"optimize": True}),
    "WebP (modern, small)": ("WEBP", ".webp", True, {"method": 6}),
}

CACHE_FILENAME = ".heic-converter-cache.json"
LOG_FILENAME = "conversion-log.txt"


class OutputMode(str, Enum):
    SAME_AS_ORIGINAL = "same_as_original"
    CUSTOM_FOLDER = "custom_folder"


class ConflictPolicy(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    RENAME = "rename"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ConversionItem:
    source: Path
    root: Path  # the user-selected root path the file was discovered under
    selected: bool = True
    status: str = "pending"  # pending | done | skipped | error | dry-run
    message: str = ""
    output: Optional[Path] = None


@dataclass
class ProgressInfo:
    total: int
    completed: int
    current_file: str
    elapsed: float
    eta: float
    succeeded: int
    failed: int
    skipped: int


@dataclass
class ConverterConfig:
    output_format_label: str = "JPEG (recommended)"
    output_mode: OutputMode = OutputMode.SAME_AS_ORIGINAL
    output_dir: Optional[Path] = None  # required if output_mode == CUSTOM_FOLDER

    quality: int = 92
    keep_exif: bool = True
    auto_rotate: bool = True
    organize_by_date: bool = False  # YYYY/YYYY-MM-DD/ subfolders
    copy_live_photo: bool = True
    verify_after: bool = True
    use_hash_cache: bool = True

    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    dry_run: bool = False
    write_log: bool = True

    workers: int = 4


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def find_heic_files(root: Path) -> list[Path]:
    """Recursively find HEIC/HEIF files under `root` (case-insensitive)."""
    if root.is_file():
        return [root] if root.suffix.lower() in HEIC_EXTENSIONS else []
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in HEIC_EXTENSIONS:
            out.append(p)
    return out


def find_live_photo_companion(heic: Path) -> Optional[Path]:
    """Apple Live Photos store IMG_1234.HEIC + IMG_1234.MOV side by side."""
    for ext in LIVE_PHOTO_EXTENSIONS:
        candidate = heic.with_suffix(ext)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_relative(path: Path, root: Path) -> Path:
    base = root if root.is_dir() else root.parent
    try:
        return path.relative_to(base)
    except ValueError:
        return Path(path.name)


def _exif_capture_date(img: Image.Image) -> Optional[datetime]:
    try:
        exif = img.getexif()
        if not exif:
            return None
        for tag in (36867, 36868, 306):  # DateTimeOriginal, DateTimeDigitized, DateTime
            value = None
            try:
                ifd = exif.get_ifd(0x8769)  # ExifIFD
                value = ifd.get(tag)
            except Exception:  # noqa: BLE001
                value = None
            value = value or exif.get(tag)
            if value:
                try:
                    return datetime.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _resolve_output_root(item: ConversionItem, cfg: ConverterConfig) -> Path:
    """The base folder under which the relative file path will be placed."""
    if cfg.output_mode == OutputMode.SAME_AS_ORIGINAL:
        # Output mirrors the source tree in place — root is the source root itself.
        return item.root if item.root.is_dir() else item.root.parent
    assert cfg.output_dir is not None, "output_dir required for CUSTOM_FOLDER mode"
    # Namespace the custom folder by the source root name to avoid collisions
    # when scanning multiple roots.
    return cfg.output_dir / (item.root.name if item.root.is_dir() else item.root.parent.name)


def _build_output_path(
    item: ConversionItem, cfg: ConverterConfig, ext: str, capture_date: Optional[datetime]
) -> Path:
    out_root = _resolve_output_root(item, cfg)
    rel = _safe_relative(item.source, item.root)
    if cfg.organize_by_date and capture_date is not None:
        sub = Path(capture_date.strftime("%Y")) / capture_date.strftime("%Y-%m-%d")
        return out_root / sub / rel.with_suffix(ext).name
    return out_root / rel.with_suffix(ext)


def _resolve_conflict(path: Path, policy: ConflictPolicy) -> Optional[Path]:
    """Return the path to write to, or None if the item should be skipped."""
    if not path.exists():
        return path
    if policy == ConflictPolicy.SKIP:
        return None
    if policy == ConflictPolicy.OVERWRITE:
        return path
    # RENAME: file.jpg -> file (1).jpg, file (2).jpg, ...
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _hash_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Hash cache
# ---------------------------------------------------------------------------


class HashCache:
    """Per-output-root JSON cache: source-hash -> output-path string.

    Used to skip re-conversions when the user re-runs on the same library.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / CACHE_FILENAME
        self._lock = Lock()
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._data = {}

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _convert_one(
    item: ConversionItem,
    cfg: ConverterConfig,
    cache: Optional[HashCache],
    logger: Optional[logging.Logger],
) -> ConversionItem:
    fmt, default_ext, supports_quality, base_save_kwargs = SUPPORTED_FORMATS[cfg.output_format_label]

    src_hash: Optional[str] = None
    if cache is not None:
        try:
            src_hash = _hash_file(item.source)
            cached_out = cache.get(src_hash)
            if cached_out and Path(cached_out).exists():
                item.status = "skipped"
                item.message = f"Already converted (cache): {Path(cached_out).name}"
                item.output = Path(cached_out)
                if logger:
                    logger.info("SKIP cache %s -> %s", item.source, cached_out)
                return item
        except OSError as e:
            item.status = "error"
            item.message = f"Read failed: {e}"
            return item

    try:
        with Image.open(item.source) as img:
            capture_date = _exif_capture_date(img) if cfg.organize_by_date else None
            out_path = _build_output_path(item, cfg, default_ext, capture_date)
            resolved = _resolve_conflict(out_path, cfg.conflict_policy)
            if resolved is None:
                item.status = "skipped"
                item.message = f"Exists: {out_path.name}"
                item.output = out_path
                if logger:
                    logger.info("SKIP exists %s -> %s", item.source, out_path)
                return item
            out_path = resolved

            if cfg.dry_run:
                item.status = "dry-run"
                item.message = f"Would write: {out_path}"
                item.output = out_path
                if logger:
                    logger.info("DRY %s -> %s", item.source, out_path)
                return item

            out_path.parent.mkdir(parents=True, exist_ok=True)

            # Capture metadata before any transformation.
            exif_bytes = img.info.get("exif") if cfg.keep_exif else None
            icc = img.info.get("icc_profile")

            working = img
            if cfg.auto_rotate:
                working = ImageOps.exif_transpose(img)
                # exif_transpose visually applies rotation; clear orientation tag.
                if exif_bytes:
                    try:
                        new_exif = working.getexif()
                        if 0x0112 in new_exif:
                            del new_exif[0x0112]
                        exif_bytes = new_exif.tobytes()
                    except Exception:  # noqa: BLE001
                        pass

            save_kwargs = dict(base_save_kwargs)
            if supports_quality:
                save_kwargs["quality"] = int(cfg.quality)
            if fmt == "JPEG" and working.mode in ("RGBA", "P", "LA"):
                working = working.convert("RGB")
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes
            if icc:
                save_kwargs["icc_profile"] = icc

            working.save(out_path, fmt, **save_kwargs)
            expected_size = working.size

        # Verify pass: re-open and check.
        if cfg.verify_after:
            try:
                with Image.open(out_path) as v:
                    v.load()
                    if v.size != expected_size:
                        raise ValueError(
                            f"size mismatch (got {v.size}, expected {expected_size})"
                        )
            except Exception as e:  # noqa: BLE001
                item.status = "error"
                item.message = f"Verify failed: {e}"
                if logger:
                    logger.error("VERIFY-FAIL %s: %s", out_path, e)
                return item

        # Live Photo companion .MOV -> copy alongside (never moved/deleted).
        if cfg.copy_live_photo:
            mov = find_live_photo_companion(item.source)
            if mov is not None:
                mov_dest = out_path.with_suffix(mov.suffix)
                if not mov_dest.exists() or cfg.conflict_policy == ConflictPolicy.OVERWRITE:
                    try:
                        shutil.copy2(mov, mov_dest)
                    except OSError as e:
                        if logger:
                            logger.warning("Live Photo copy failed for %s: %s", mov, e)

        item.status = "done"
        item.message = str(out_path)
        item.output = out_path

        if cache is not None and src_hash is not None:
            cache.put(src_hash, str(out_path))

        if logger:
            logger.info("OK %s -> %s", item.source, out_path)

    except Exception as e:  # noqa: BLE001
        item.status = "error"
        item.message = str(e)
        if logger:
            logger.error("FAIL %s: %s", item.source, e)
    return item


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------


class ConversionJob:
    """Runs a batch conversion in a worker pool with progress + cancel support."""

    def __init__(
        self,
        items: list[ConversionItem],
        cfg: ConverterConfig,
        on_progress: Callable[[ProgressInfo, ConversionItem], None],
        on_done: Callable[[ProgressInfo], None],
    ) -> None:
        self.items = [i for i in items if i.selected]
        self.cfg = cfg
        self.on_progress = on_progress
        self.on_done = on_done
        self._cancel = Event()
        self._lock = Lock()
        self._completed = 0
        self._succeeded = 0
        self._failed = 0
        self._skipped = 0
        self._start: float = 0.0
        self._executor: Optional[ThreadPoolExecutor] = None

    def cancel(self) -> None:
        self._cancel.set()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def run(self) -> None:
        total = len(self.items)
        self._start = time.monotonic()
        if total == 0:
            self.on_done(ProgressInfo(0, 0, "", 0.0, 0.0, 0, 0, 0))
            return

        cache_root = self._cache_root()
        cache: Optional[HashCache] = None
        if self.cfg.use_hash_cache and cache_root is not None and not self.cfg.dry_run:
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
                cache = HashCache(cache_root)
            except OSError:
                cache = None

        logger: Optional[logging.Logger] = None
        log_handler: Optional[logging.Handler] = None
        if self.cfg.write_log and cache_root is not None and not self.cfg.dry_run:
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
                logger = logging.getLogger(f"heic_converter.{id(self)}")
                logger.setLevel(logging.INFO)
                logger.propagate = False
                log_handler = logging.FileHandler(
                    cache_root / LOG_FILENAME, encoding="utf-8"
                )
                log_handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                logger.addHandler(log_handler)
                logger.info("--- Conversion run started (%d files) ---", total)
            except OSError:
                logger = None

        try:
            self._executor = ThreadPoolExecutor(max_workers=max(1, self.cfg.workers))
            futures: list[tuple[Future, ConversionItem]] = []
            for it in self.items:
                if self._cancel.is_set():
                    break
                fut = self._executor.submit(_convert_one, it, self.cfg, cache, logger)
                futures.append((fut, it))

            for fut, src_item in futures:
                if self._cancel.is_set():
                    break
                try:
                    done_item = fut.result()
                except Exception as e:  # noqa: BLE001
                    done_item = src_item
                    done_item.status = "error"
                    done_item.message = str(e)

                with self._lock:
                    self._completed += 1
                    if done_item.status in ("done", "dry-run"):
                        self._succeeded += 1
                    elif done_item.status == "skipped":
                        self._skipped += 1
                    else:
                        self._failed += 1
                    elapsed = time.monotonic() - self._start
                    rate = self._completed / elapsed if elapsed > 0 else 0
                    remaining = total - self._completed
                    eta = remaining / rate if rate > 0 else 0.0
                    progress = ProgressInfo(
                        total=total,
                        completed=self._completed,
                        current_file=str(done_item.source),
                        elapsed=elapsed,
                        eta=eta,
                        succeeded=self._succeeded,
                        failed=self._failed,
                        skipped=self._skipped,
                    )
                self.on_progress(progress, done_item)

            self._executor.shutdown(wait=True)
        finally:
            if cache is not None:
                cache.save()
            if logger is not None and log_handler is not None:
                logger.info(
                    "--- Run finished: ok=%d skip=%d err=%d ---",
                    self._succeeded, self._skipped, self._failed,
                )
                logger.removeHandler(log_handler)
                log_handler.close()

        elapsed = time.monotonic() - self._start
        self.on_done(
            ProgressInfo(
                total=total,
                completed=self._completed,
                current_file="",
                elapsed=elapsed,
                eta=0.0,
                succeeded=self._succeeded,
                failed=self._failed,
                skipped=self._skipped,
            )
        )

    def _cache_root(self) -> Optional[Path]:
        """Pick a stable folder to put the cache + log under."""
        if self.cfg.output_mode == OutputMode.CUSTOM_FOLDER and self.cfg.output_dir is not None:
            return self.cfg.output_dir
        if self.items:
            r = self.items[0].root
            return r if r.is_dir() else r.parent
        return None


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN guard
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
