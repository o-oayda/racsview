# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [v2.0.0] - 2026-03-25

### Changed

- Source catalogue loading now uses a local Python server instead of direct browser-side catalogue queries.
- The datastore location is now configurable via `RACSVIEW_DATASTORE` or the UI datastore picker.
- `deploy.sh` now runs the local server and supports browser launching on Linux and macOS.

### Added

- A local Python backend for source catalogue queries and HEALPix grid generation.
- Dropdown-based image and source selection UI.
- Circle overlay and HEALPix grid overlay controls.
- A UI datastore picker using the host system's native directory chooser.
- Python project metadata and dependency management via `pyproject.toml` and `uv.lock`.

### Fixed

- Startup and deployment issues related to local server launch and datastore configuration flow.

[Unreleased]: https://github.com/o-oayda/racsview/compare/v2.0.0...HEAD
[v2.0.0]: https://github.com/o-oayda/racsview/releases/tag/v2.0.0
