"""Command-line interface for the HEIC converter.

Reuses the same engine as the GUI (`converter.py`).

Examples:
    python cli.py ./Photos --output ./Converted --format jpeg --quality 90
    python cli.py ./Photos --in-place --organize-by-date --dry-run
    python cli.py img1.HEIC img2.HEIC -o out --workers 8 --on-conflict rename
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
)

FORMAT_ALIASES = {
    "jpeg": "JPEG (recommended)",
    "jpg": "JPEG (recommended)",
    "png": "PNG (lossless, larger)",
    "webp": "WebP (modern, small)",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="heic-converter",
        description="Convert Apple HEIC/HEIF images. Originals are never modified.",
    )
    p.add_argument("paths", nargs="+", type=Path, help="Files or folders to scan.")

    out = p.add_mutually_exclusive_group(required=True)
    out.add_argument("-o", "--output", type=Path, help="Custom output folder.")
    out.add_argument(
        "--in-place", action="store_true",
        help="Write converted files next to the originals (originals are NOT touched).",
    )

    p.add_argument(
        "-f", "--format", default="jpeg", choices=sorted(FORMAT_ALIASES.keys()),
        help="Output format (default: jpeg).",
    )
    p.add_argument("-q", "--quality", type=int, default=92, help="Quality 50-100 (JPEG/WebP).")
    p.add_argument("-w", "--workers", type=int, default=4, help="Parallel workers (default: 4).")
    p.add_argument(
        "--on-conflict", choices=[c.value for c in ConflictPolicy], default=ConflictPolicy.SKIP.value,
        help="What to do if the output file already exists.",
    )

    p.add_argument("--organize-by-date", action="store_true",
                   help="Place outputs in YYYY/YYYY-MM-DD/ subfolders based on EXIF capture date.")
    p.add_argument("--no-exif", action="store_true", help="Strip EXIF metadata.")
    p.add_argument("--no-rotate", action="store_true", help="Disable EXIF auto-rotate.")
    p.add_argument("--no-live", action="store_true", help="Don't copy Live Photo .MOV companions.")
    p.add_argument("--no-verify", action="store_true", help="Skip post-write verify pass.")
    p.add_argument("--no-cache", action="store_true", help="Disable hash-based skip cache.")
    p.add_argument("--no-log", action="store_true", help="Don't write a conversion-log.txt.")
    p.add_argument("--dry-run", action="store_true", help="List actions without writing files.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    items: list[ConversionItem] = []
    for raw in args.paths:
        if not raw.exists():
            print(f"warning: not found: {raw}", file=sys.stderr)
            continue
        for f in find_heic_files(raw):
            items.append(ConversionItem(source=f, root=raw))
    if not items:
        print("No HEIC/HEIF files found.", file=sys.stderr)
        return 1

    cfg = ConverterConfig(
        output_format_label=FORMAT_ALIASES[args.format],
        output_mode=OutputMode.SAME_AS_ORIGINAL if args.in_place else OutputMode.CUSTOM_FOLDER,
        output_dir=args.output,
        quality=args.quality,
        keep_exif=not args.no_exif,
        auto_rotate=not args.no_rotate,
        organize_by_date=args.organize_by_date,
        copy_live_photo=not args.no_live,
        verify_after=not args.no_verify,
        use_hash_cache=not args.no_cache,
        conflict_policy=ConflictPolicy(args.on_conflict),
        dry_run=args.dry_run,
        write_log=not args.no_log,
        workers=args.workers,
    )
    if cfg.output_format_label not in SUPPORTED_FORMATS:  # safety
        print(f"Unknown format: {args.format}", file=sys.stderr)
        return 2

    print(f"Found {len(items)} file(s). Starting...")
    final: ProgressInfo | None = None

    def on_progress(p: ProgressInfo, item: ConversionItem) -> None:
        marker = {"done": "✔", "skipped": "→", "error": "✘", "dry-run": "·"}.get(item.status, "?")
        print(f"  [{p.completed:>4}/{p.total}] {marker} {item.source.name}  ({item.status}) "
              f"ETA {format_duration(p.eta)}")

    def on_done(p: ProgressInfo) -> None:
        nonlocal final
        final = p

    ConversionJob(items, cfg, on_progress, on_done).run()
    assert final is not None
    print(
        f"\nDone in {format_duration(final.elapsed)}: "
        f"{final.succeeded} ok, {final.skipped} skipped, {final.failed} failed."
    )
    return 0 if final.failed == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
