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
  gui/                 PyQt5 dialogs (.py + .ui files)
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

- `analysis.resolution_m` must be one of: [2, 5, 10, 30]
- `analysis.task_type`: "SINGLE" (coverage), "P2P", "BATCH_P2P"
- `analysis.propagation_model`: "LOS", "SIMPLE_LOSS", "ITM"
- `analysis.compute_backend`: "AUTO", "GPU", "CPU"
- `tx.mode` / `rx.mode`: "AGL" or "AMSL"
- `propagation.radio_climate`: integer 1-6 (ITM spec)
- `propagation.earth_radius_mode`: "FOUR_THIRDS" or "ADVANCED"

## Converter Ingest Job JSON Structure

Defined in `rust/aether_converter/src/ingest.rs:18-29`:
```json
{
  "output_path": "path/to/output.abt",
  "format": "r16sint",
  "ul_lat": 47.5, "ul_lon": 8.0,
  "resolution_m": 10,
  "size_px": 4096,
  "base_tif": "path/to/wgs84.tif",
  "swiss_tifs": []
}
```

## Sensitive Files (DO NOT commit)

- `api_private_key.bin` / `api_private_key.pem` - Ed25519 signing key (server-side only)
- `vendor_keys.json` - Master seed + keypair (AETHER proprietary builds)
- `api_key.txt` - User API keys
- Any `*.key` files

These are covered by `.gitignore`. The plugin only embeds the **public** verification key.

## Code Conventions

- snake_case everywhere (Python, JSON keys, file names)
- PyQt5 for all GUI (QGIS bundles it)
- GDAL access via `osgeo` (QGIS bundles it) — never add GDAL as a pip dependency
- Use `QgsSettings("waveshed/...")` for persistent plugin settings (the engine env var `AETHER_BIN_DIR` keeps its `AETHER` name)
- Use `QgsMessageLog` for debug logging, `QgsMessageBar` for user-facing messages
- Type hints on all public functions
- No external pip dependencies beyond what QGIS provides (PyQt5, osgeo/GDAL, numpy)
  - Exception: `pynacl` for Ed25519 API key validation (bundled with plugin)

## Testing

```bash
pytest tests/ -v --tb=short
```

Unit tests mock QGIS APIs. Integration tests require QGIS environment.

## Build / Package

Plugin is distributed as a ZIP for QGIS Plugin Manager (build with `python3 package.py` → `dist/waveshed.<version>.zip`). The Aether engine binaries are distributed separately via the waveshed.io release manifest (`https://waveshed.io/releases/latest.json`) and downloaded on demand by the plugin's Settings dialog.
