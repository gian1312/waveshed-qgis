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

Every binary answers `--version` with `<name> <semver>` (toolkit binaries since
converter 0.2.5 / export 1.0.1). `binary_manager.check_engine_for_job` runs
`aether_core --version` at the start of every run, logs the engine path and
version, and refuses 90 m / 250 m on engines older than
`COARSE_RESOLUTION_MIN_ENGINE` (0.4.3) before any terrain is fetched; an engine
that does not know the flag counts as the oldest known build.

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
  acquisition route. Rendered servers (the per-tile QGIS export) state the
  same contract through `rendered_export_gap`: `writeRaster` reports success
  for an answer that arrived with holes, so the export is measured against
  the LAYER's own published extent — empty ground outside it is the coverage
  edge (already warned by bounds), empty ground inside it is a partial
  answer and gets its own warning. Without it a dropped block reached the
  user as silent sea level, visible only as a cross-tab terrain
  disagreement (row 4.3, 2026-09-20: up to 402 m).
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

These are covered by `.gitignore`. The plugin embeds no licence key at all —
licence signatures, node-lock and revocation are verified by the engine. The
only key it carries is the **public** release-manifest key in
`waveshed/core/release_keys.py` (see Engine distribution); the matching private
key lives in the AETHER vendor keys / CI secrets, never here.

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
  `nacl`; the one signature the plugin checks itself (the release manifest)
  goes through the pure-Python verifier `core/ed25519.py`. Keep it that way: a
  pip dependency is a support burden in a QGIS plugin, which cannot install one.

## Testing

```bash
pytest tests/ -v --tb=short
```

Unit tests mock QGIS APIs. Integration tests require QGIS environment.

## Engine distribution (binary_manager)

The engine is never bundled; Settings → Download Binaries fetches it on demand
after EULA consent (asked every time). Rules, all in `core/binary_manager.py`:

- **Signed manifest.** `fetch_manifest` is the only way any code gets a
  manifest (Download, the startup update check, Check for updates). It checks
  `https://releases.waveshed.io/manifests/v<version>/latest.json.sig`
  (JSON `{alg: ed25519, context: waveshed-manifest-v1, public_key, signature}`)
  over `b"waveshed-manifest-v1\0" + the exact manifest bytes` — the
  waveshed.io relay serves them verbatim. Valid + trusted key → ok; bad /
  untrusted / malformed → refuse; HTTP 404 → allowed only for versions
  `<= LAST_UNSIGNED_ENGINE` (0.4.7, published before signing; never raise it);
  any other fetch error → refuse. The version must be X.Y.Z before it is put
  in the URL.
- **Trusted keys** live in `core/release_keys.py` (`MANIFEST_PUBLIC_KEYS`,
  64-hex strings). That file is rewritten by the AETHER release GUI — keep the
  documented tuple format. Empty tuple = every signed manifest refused;
  `package.py --release` refuses to build then. Rotation = ship the new key
  next to the old one first.
- **Host pin.** Asset URLs must start with `https://releases.waveshed.io/`.
- **Staged, all-or-nothing install.** The archive is SHA-256-checked
  (fail-closed), extracted into `<target>/.waveshed-staging-*`, must contain
  all three binaries, gets `chmod +x` + quarantine clearing + `revocation.bin`,
  and `aether_core --version` must report the manifest version. Only then
  `_install_staged` swaps files in (backup-rename, move, roll back on any
  error; Windows refuses up front when a binary is locked by a running job).
  Files the archive does not contain (`license.key`) are never touched;
  leftovers are removed by the next install.
- **macOS: no notarization, by decision.** The ad-hoc-signed engine runs once
  the download quarantine is cleared. Like MPT_SIGMA, `prepare_engine_dir`
  (`chmod +x` + `xattr -cr`) runs at plugin load, after Browse/Auto-detect,
  and before every `--version` probe; a Gatekeeper-looking failure gets the
  `xattr -cr "<dir>"` hint (`macos_launch_hint`).
- **Notices** (`gui/engine_notices.py`): first start without an engine →
  message bar with "Open Settings"; once per session (switch
  `waveshed/engine_update_check`, default on; silent offline) a newer release
  → one notice per version (`waveshed/engine_update_notified_version`) with
  "Update" → Settings download flow. Run errors that say "Settings →
  Download binaries" get an "Update engine" / "Open Settings" button
  (`show_run_error`). Nothing ever downloads without the consent dialog.

## Build / Package / Release

Plugin is distributed as a ZIP (build with `python3 package.py` →
`dist/waveshed.<version>.zip`; `--release` additionally requires public
metadata URLs and a trusted manifest key).

Release = push tag `v<metadata version>`. `.github/workflows/release.yml` runs
the full test suite (distro Python + python3-gdal, so nothing is skipped for
want of GDAL), `package.py --release`, `tools/make_plugin_repo.py` (writes
`plugins.xml` + `latest.json` from the metadata *inside* the ZIP), uploads to
R2 `waveshed-releases` — `qgis/waveshed.<V>.zip` (immutable, never
overwritten), then `qgis/latest.json`, then `qgis/plugins.xml` — and waits
until `https://waveshed.io/qgis/latest.json` / `plugins.xml` (AETHER_Web
relays) serve the new version, then re-downloads the ZIP and checks its
SHA-256. Secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_S3_ENDPOINT`.
`workflow_dispatch` is a dry run (no upload). QGIS users add
`https://waveshed.io/qgis/plugins.xml` as a plugin repository; plugins.qgis.org
is the later, separate channel.
