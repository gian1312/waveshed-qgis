# Waveshed QGIS Plugin - Claude Code Guide

Waveshed is a QGIS plugin providing GUI access to the AETHER RF propagation engine. Pure Python wrapper — all computation is done by external Rust binaries (aether_core, aether_converter, aether_export). The plugin (product) is "Waveshed"; the engine keeps its "Aether" name.

## Architecture

```
waveshed/             Plugin package (installed into QGIS plugins dir)
  __init__.py          classFactory() entry point
  metadata.txt         QGIS plugin registry metadata
  plugin.py            Menu/toolbar integration, init/unload lifecycle
  provider.py          QgsProcessingProvider registration
  algorithms/          QgsProcessingAlgorithm implementations
  gui/                 Qt dialogs, hand-built in Python (no .ui files)
  core/                Business logic (no GUI imports)
  resources/           Icons, color ramps
tests/                 pytest unit + integration tests
```

## Key Constraint: Zero Changes to AETHER Binaries

All adaptation happens in this plugin. Communication with binaries is exclusively through:
- JSON config files (written by plugin, read by binaries)
- Subprocess invocation (stdin/stdout/stderr)
- File outputs (.bit/.tiles, .json sidecar, .tif)

## Binary Interfaces

| Binary | CLI | Input | Output |
|--------|-----|-------|--------|
| `aether_core` | `--config <job.json>` | Job config JSON, .abt terrain tiles | .bit/.tiles + .json sidecar |
| `aether_converter` | `ingest --job-file <job.json>` | Ingest job JSON, GeoTIFF | .abt terrain tiles |
| `aether_export` | `-i <.bit/.tiles> -j <.json> -o <.tif>` | .bit/.tiles + .json sidecar | Cloud-Optimized BigTIFF |

## Job Config JSON Structure (aether_core)

Defined in `rust/aether_core/src/config.rs`. Sections: tx, rx, analysis, output, processing, propagation.

- `analysis.resolution_m` is an f32; the plugin offers [2, 5, 10, 30, 90, 250] (see `job_builder.VALID_RESOLUTIONS`). The engine itself accepts any value ≥ 0.1 m — this list is a plugin-side convenience, kept in sync with `terrain_adapter.ABT_EXTENT_DEG`, which maps each resolution to its `.abt` tile extent in degrees and is the single source of truth for both terrain paths (Site Analysis and Map Converter). Tile extent is an engine constraint, not just a file-size one: every tile a wedge touches goes into one contiguous terrain-atlas allocation capped at ~3.86 GB, so oversized tiles get the job rejected.
- `analysis.task_type`: "SINGLE" (coverage), "P2P", "BATCH_P2P"
- `analysis.propagation_model`: "LOS", "SIMPLE_LOSS", "ITM"
- `analysis.compute_backend`: "AUTO", "GPU", "CPU"
- `tx.mode` / `rx.mode`: "AGL" or "AMSL"
- `propagation.radio_climate`: integer 1-6 (ITM spec)
- `propagation.earth_radius_mode`: "FOUR_THIRDS" or "ADVANCED"

## Converter Ingest Job JSON Structure

Defined in `aether_converter/src/ingest.rs` (contract v2.0):
```json
{
  "output_path": "path/to/output.abt",
  "format": "r16sint",
  "ul_lat": 47.5, "ul_lon": 8.0,
  "resolution_m": 10,
  "size_px": 4096,
  "sources": [
    {"path": "overlay.tif", "crs": "EPSG:32632"},
    {"path": "base.tif", "crs": "EPSG:4326"}
  ],
  "void_fill_m": 0.0
}
```

- `sources[]`: priority = array order (first valid sample wins per pixel).
  `crs` is optional (`"EPSG:nnnn"` or a proj string); when absent the
  converter reads the GeoTIFF's GeoKeys and hard-errors if they are
  user-defined/absent. `nodata` is optional per source (else `GDAL_NODATA`
  tag). The converter reprojects while sampling — the plugin never warps.
- A source `path` may also be a pool **`.abt` tile** (detected by its `AETH`
  magic): self-describing — geometry from its own header, values already
  half-metres. Setting `crs` or `nodata` on a `.abt` source is a converter
  hard error. The Map Converter uses this for XYZ terrain: the shared
  downloader (`terrain_adapter.ensure_pool_tiles`) fills the Site-Analysis
  pool, and the pool tile becomes an ingest source.
- Both tabs acquire terrain through ONE router: XYZ → shared Rust
  `download`; local files/folders → direct `sources[]`; only true rendered
  servers (WMS/WMTS/ArcGIS) are exported through QGIS, per tile. The single
  engine-subprocess entry point is `terrain_adapter.run_converter_streaming`.
- `void_fill_m` is optional: pixels no source covers are written as this
  elevation instead of the VOID sentinel. Site Analysis passes `0.0`
  (0 m ground outside the DEM); the Map Converter tab omits it (VOID kept).
- **No-data contract (2026-08-31, CONTRACT items 17/18): ground a source has
  no data for degrades to sea, loudly — never an error.** Download: HTTP 404
  is "no tile here" (own error class + `[Stats] NO-DATA (HTTP 404):` line),
  excluded from the majority-fatal rule — a run over or past a bounded
  source's edge exits 0 with voids there; only real failures
  (timeout/connect/403/429/5xx/decode) can majority-abort. Ingest: a batch
  with zero covered pixels exits 0 with `[Warn] NO-DATA batch:` instead of
  bailing. Plugin side: an all-404 miss set is a COMPLETE download (no
  rebuild flags), `_warn_no_data_tiles`/`_warn_if_bbox_exceeds_bounds` (and
  the rendered-layer extent check) tell the user "assumed 0 m (sea level)",
  and `_sync_view` fills pool-tile voids to 0 m in the Site Analysis VIEW
  only — the pool keeps VOID for the Map Converter, whose output keeps VOID
  by contract. The torture suite's `overrun:` checks pin all of this per
  acquisition route.
- `base_tif` / `swiss_tifs` are DEPRECATED aliases still accepted by the
  converter for older callers; the plugin no longer emits them, and emitting
  them alongside `sources` is an error.
- The converter also has a `plan` subcommand
  (`plan --south S --north N --west W --east E --resolutions 30,90`) that
  prints the tile grid as JSON (`aether-plan/1`); the Map Converter worker
  cross-checks it against the plugin's own enumeration before every run and
  aborts on mismatch (or on an engine too old to have `plan`).

## Sensitive Files (DO NOT commit)

- `api_private_key.bin` / `api_private_key.pem` - Ed25519 signing key (server-side only)
- `vendor_keys.json` - Master seed + keypair (AETHER proprietary builds)
- `api_key.txt` - User API keys
- Any `*.key` files

These are covered by `.gitignore`. The plugin only embeds the **public** verification key.

## Code Conventions

- snake_case everywhere (Python, JSON keys, file names)
- Qt for all GUI, imported through `qgis.PyQt` only — never `PyQt5`/`PyQt6`
  directly. The plugin runs on QGIS 3.28+ (Qt5) **and** QGIS 4 (Qt6), so every
  Qt/QGIS enum is spelled with its scope (`Qt.CheckState.Checked`,
  `QMessageBox.StandardButton.Yes`, `QgsWkbTypes.GeometryType.PointGeometry`),
  `.exec()` not `.exec_()`, and `QAction`/`QActionGroup`/`QShortcut` are
  imported with a `try: from qgis.PyQt.QtGui` / `except ImportError:
  from qgis.PyQt.QtWidgets` pair (Qt6 moved them). Scoped names work on the
  PyQt5 QGIS 3.28 ships, so this costs nothing on Qt5.
  `tests/test_qt6_compat.py` enforces all of it; `tests/conftest.py` mirrors it
  by stubbing scoped enums only.
- GDAL access via `osgeo` (QGIS bundles it) — never add GDAL as a pip dependency
- Use `QgsSettings("waveshed/...")` for persistent plugin settings (the engine env var `AETHER_BIN_DIR` keeps its `AETHER` name)
- Use `QgsMessageLog` for debug logging, `QgsMessageBar` for user-facing messages
- Type hints on all public functions
- No external pip dependencies beyond what QGIS provides (PyQt5, osgeo/GDAL, numpy).
  There are no exceptions — `core/api_key.py` validates keys structurally
  (Base58 charset + payload length) with the standard library alone; the
  Ed25519 signature is verified by the engine binary, so nothing imports
  `nacl`. Keep it that way: a
  pip dependency is a support burden in a QGIS plugin, which cannot install one.

## Testing

```bash
pytest tests/ -v --tb=short
```

Unit tests mock QGIS APIs. Integration tests require QGIS environment.

## Build / Package

Plugin is distributed as a ZIP for QGIS Plugin Manager (build with `python3 package.py` → `dist/waveshed.<version>.zip`). The Aether engine binaries are distributed separately via the waveshed.io release manifest (`https://waveshed.io/releases/latest.json`) and downloaded on demand by the plugin's Settings dialog.
