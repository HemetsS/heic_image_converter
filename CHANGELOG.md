# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-04-30
### Added
- Initial public release: cross-platform GUI + CLI for HEIC/HEIF to JPEG/PNG/WebP conversion
- Modern Tkinter/ttkbootstrap GUI with drag & drop, batch, and advanced options
- CLI interface with all core features
- Parallel conversion, EXIF/ICC preservation, auto-rotate, hash cache, and more
- GitHub Actions workflow for building and releasing binaries

### Changed
- Merged hash cache and conflict policy options for clarity

### Fixed
- Overwrite now works correctly with/without hash cache
- Drag & drop and file picker now append to file list
