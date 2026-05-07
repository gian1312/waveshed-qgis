# AETHER QGIS Plugin — Implementation Plan

## Context

AETHER is a GPU-accelerated RF propagation engine (Rust + WebGPU). It runs as CLI binaries (`aether_core`, `aether_converter`, `aether_export`) orchestrated by Python scripts. The goal is to create a QGIS plugin that:

1. Provides **Site Analysis** (coverage) and **P2P link analysis** — GUI copied from the MPT SIGMA plugin
2. Works with **any terrain data** the user has loaded in QGIS (local files, WCS, COGs, VRTs)
3. Includes **automatic binary download** from GitHub Releases
4. Gates non-commercial use via an **API key** obtained from a webpage (6-month renewal)

**Key constraint:** Zero changes to `aether_core`. The converter and plugin layer handle all adaptation.

### Existing interfaces (DO NOT modify)

| Interface | Direction | Format | File Reference |
|-----------|-----------|--------|----------------|
| Job config JSON | Input to aether_core | JSON with tx/rx/analysis/output/processing/propagation sections | `rust/aether_core/src/config.rs` |
| Terrain directory | Input to aether_core | Flat directory of .abt tiles (scanned by 44-byte header peek) | `rust/aether_core/src/engines/coverage.rs:416-514` |
| Ingest job JSON | Input to aether_converter | JSON with output_path, ul_lat/lon, resolution_m, size_px, base_tif, swiss_tifs | `rust/aether_converter/src/ingest.rs:18-29` |
| .bit/.tiles + JSON sidecar | Output from aether_core | Binary coverage/loss raster + metadata | `rust/aether_core/src/io/tiled_buffer.rs` |
| GeoTIFF | Output from aether_export | Cloud-Optimized BigTIFF | `rust/export_geotiff/src/main.rs` |
| CLI exit code + stderr | Status from all binaries | Process exit code, RUST_LOG output | All Rust binaries |

---

## 1. Plugin File Structure

```
aether_qgis/
├── __init__.py                  # classFactory() entry point
├── metadata.txt                 # QGIS plugin metadata (name, version, author, qgisMinimumVersion)
├── plugin.py                    # Main plugin class (menu items, toolbar icon, init/unload)
├── provider.py                  # QgsProcessingProvider — registers AETHER algorithms
├── algorithms/
│   ├── coverage.py              # QgsProcessingAlgorithm for Site Analysis (SINGLE task_type)
│   └── p2p.py                   # QgsProcessingAlgorithm for P2P Link Analysis (P2P / BATCH_P2P)
├── gui/
│   ├── coverage_dialog.py       # Site Analysis dialog (PyQt5, copied from MPT SIGMA layout)
│   ├── p2p_dialog.py            # P2P dialog (PyQt5, copied from MPT SIGMA layout)
│   ├── settings_dialog.py       # Binary path, API key, default settings
│   ├── coverage_dialog.ui       # Qt Designer form for coverage
│   ├── p2p_dialog.ui            # Qt Designer form for P2P
│   └── settings_dialog.ui       # Qt Designer form for settings
├── core/
│   ├── binary_manager.py        # Download, discover, verify AETHER binaries
│   ├── terrain_adapter.py       # QGIS raster → temp WGS84 GeoTIFF → .abt conversion
│   ├── job_builder.py           # Build aether_core job config JSON from dialog parameters
│   ├── result_loader.py         # Load output GeoTIFF as styled QGIS raster layer
│   └── api_key.py               # API key validation (Ed25519 offline signature check)
├── resources/
│   ├── icon.png                 # Plugin toolbar icon
│   └── colorramps.xml           # Predefined color ramps for coverage/loss display
└── i18n/                        # Translations (optional, future)
```

**Reference files in this repo to port from:**
- `python/drivers/run_simulation.py:92-121` — job config JSON construction → port to `job_builder.py`
- `rust/aether_core/src/config.rs` — authoritative parameter definitions (all defaults, enums, ranges)
- `python/utils/keygen.py` — Ed25519 license key signing pattern → adapt for API key system

---

## 2. GUI Design

### 2.1 Site Analysis Dialog (`coverage_dialog.py`)

Copied from MPT SIGMA's site analysis dialog. Three tabs. All parameters map directly to `config.rs` fields.

#### Tab 1: Transmitter

| Widget | Label | Type | Range | Default | Maps to JSON |
|--------|-------|------|-------|---------|-------------|
| Map click / coordinate entry | TX Location | QgsPointXY / QDoubleSpinBox pair | lat -90..90, lon -180..180 | — | `tx.lat`, `tx.lon` |
| SpinBox | TX Height (m) | QDoubleSpinBox | 0–10000 | 30.0 | `tx.height_m` |
| ComboBox | Height Mode | QComboBox | AGL, AMSL | AGL | `tx.mode` |
| SpinBox | Frequency (MHz) | QDoubleSpinBox | 1–3000 | 433.0 | `tx.freq_mhz` |
| SpinBox | ERP (Watts) | QDoubleSpinBox | 0.001–1000000 | 10.0 | `tx.erp_watts` |
| File picker | AZ Pattern | QgsFileWidget (*.az) | — | None | `tx.az_pattern_file` |
| File picker | EL Pattern | QgsFileWidget (*.el) | — | None | `tx.el_pattern_file` |
| SpinBox | AZ Rotation | QDoubleSpinBox | 0–360 | 0 | `tx.az_rotation` |

#### Tab 2: Analysis

| Widget | Label | Type | Range | Default | Maps to JSON |
|--------|-------|------|-------|---------|-------------|
| Layer dropdown | DEM Layer | QgsMapLayerComboBox (raster only) | — | — | Used by `terrain_adapter.py` |
| ComboBox | Propagation Model | QComboBox | LOS, SIMPLE_LOSS, ITM | LOS | `analysis.propagation_model` |
| ComboBox | Resolution (m) | QComboBox | 2, 5, 10, 30 | 10 | `analysis.resolution_m` |
| SpinBox | Max Range (km) | QSpinBox | 1–500 | 50 | `analysis.max_range_km` |
| SpinBox | RX Height (m) | QDoubleSpinBox | 0–10000 | 1.5 | `rx.height_m` |
| ComboBox | RX Height Mode | QComboBox | AGL, AMSL | AGL | `rx.mode` |
| SpinBox | Azimuth Start | QDoubleSpinBox | 0–360 | 0 | `analysis.azimuth_start_deg` |
| SpinBox | Azimuth End | QDoubleSpinBox | 0–360 | 360 | `analysis.azimuth_end_deg` |
| ComboBox | Compute Backend | QComboBox | AUTO, GPU, CPU | AUTO | `analysis.compute_backend` |

**Resolution constraint:** aether_core only accepts `[2, 5, 10, 30]` m — enforced at `coverage.rs:322`. The plugin ComboBox must restrict to these values.

#### Tab 3: Propagation (ITM Parameters) — collapsed/hidden by default, expand when model = ITM

| Widget | Label | Type | Default | Maps to JSON |
|--------|-------|------|---------|-------------|
| SpinBox | Ground Permittivity (eps) | QDoubleSpinBox | 15.0 | `propagation.eps_dielect` |
| SpinBox | Ground Conductivity (S/m) | QDoubleSpinBox | 0.005 | `propagation.sgm_conductivity` |
| SpinBox | Surface Refractivity (N) | QDoubleSpinBox | 301.0 | `propagation.eno_ns_surfref` |
| ComboBox | Radio Climate | QComboBox (6 ITM climates) | Continental Temperate (5) | `propagation.radio_climate` |
| ComboBox | Polarization | QComboBox | Horizontal (0), Vertical (1) | Horizontal | `propagation.pol` |
| SpinBox | Confidence | QDoubleSpinBox 0.01–0.99 | 0.50 | `propagation.conf` |
| SpinBox | Reliability | QDoubleSpinBox 0.01–0.99 | 0.50 | `propagation.rel` |
| ComboBox | Earth Radius Mode | QComboBox | FOUR_THIRDS, ADVANCED | FOUR_THIRDS | `propagation.earth_radius_mode` |

Radio climate enum values (from ITM spec):
1. Equatorial, 2. Continental Subtropical, 3. Maritime Subtropical, 4. Desert, 5. Continental Temperate, 6. Maritime Temperate

#### Output Section (bottom of dialog)

- Folder picker: Output directory (QgsFileWidget, folder mode)
- Text field: Output filename (QLineEdit, auto-generated: `{tx_name}_{model}_{range}km`)
- Checkbox: "Add result to map" (default: checked)
- Run button → triggers execution flow (§6)

### 2.2 P2P Link Analysis Dialog (`p2p_dialog.py`)

**Mode selector at top:** Radio buttons — "Single Link" / "Batch CSV"

#### Single Link Mode

| Widget | Label | Maps to |
|--------|-------|---------|
| Map click / coords | TX Location (lat, lon) | `tx.lat`, `tx.lon` |
| SpinBox | TX Height (m) | `tx.height_m` |
| ComboBox | TX Height Mode | `tx.mode` |
| Map click / coords | RX Location (lat, lon) | Written to temp batch CSV as "R" line |
| SpinBox | RX Height (m) | `rx.height_m` |
| ComboBox | RX Height Mode | `rx.mode` |

Plus the same Frequency, ERP, Propagation tabs as Site Analysis.

#### Batch Mode

| Widget | Label | Notes |
|--------|-------|-------|
| File picker | Batch CSV | QgsFileWidget (*.csv) |
| Preview table | CSV Contents | Read-only QTableWidget showing parsed S/R entries |

**Batch CSV format** (per `rust/aether_core/src/engines/p2p.rs:40-66`):
```
S,TX1,47.0563,8.4846,40.0,AGL
R,RX1,47.5000,8.9000,10.0,AGL
S,TX2,46.9500,7.4400,25.0,AGL
R,RX2,47.2000,7.8000,5.0,AMSL
```

Fields: `type(S|R), ID, lat, lon, alt_m, mode(AGL|AMSL)`

#### P2P Output

- **Single link:** Results displayed in dialog (path loss dB, signal dBm, LOS/NLOS) + terrain profile plot
- **Batch:** CSV file written with columns: `Source_ID, Target_ID, Signal_dBm, Path_Loss_dB`
- Option to add result CSV to QGIS as a table layer

### 2.3 Settings Dialog (`settings_dialog.py`)

| Section | Widgets |
|---------|---------|
| **Binaries** | Path to AETHER binaries (QgsFileWidget, folder). Auto-detect button. "Download Binaries" button. Version display. |
| **API Key** | Key input field (QLineEdit, masked). Status label ("Valid until 2026-11-15" / "Expired" / "Not set"). "Get API Key" button → opens browser to registration page. |
| **Defaults** | Default terrain cache dir (~/.aether/cache/). Max VRAM budget (GB). Max RAM budget (GB). |

---

## 3. Terrain Data Pipeline (`terrain_adapter.py`)

### 3.1 The Problem

aether_core expects .abt tiles in a specific format (WGS84, uniform resolution, R16SINT or BC6H encoded, 256-byte row alignment). Users have terrain data in arbitrary formats and CRS.

### 3.2 The Solution

The plugin uses QGIS's bundled GDAL to extract, reproject, and save a temporary WGS84 GeoTIFF, then calls `aether_converter` (unchanged) to produce .abt tiles.

### 3.3 Verified: converter handles generic WGS84 GeoTIFF

The `base_tif` field in `ingest.rs:26` accepts any WGS84 GeoTIFF. The rasterization loop at `ingest.rs:216-232` samples `base_tif` using raw WGS84 longitude/latitude. With `swiss_tifs: []` (empty array), the converter uses only the base DEM. **No converter changes needed.**

The `load_tiff_to_ram()` function (`ingest.rs:69-106`) reads GeoTIFF coordinates from standard TIFF tags (`ModelTransformationTag`, `ModelTiepointTag`, `ModelPixelScaleTag`). Any GDAL-produced WGS84 GeoTIFF will have these tags set correctly.

### 3.4 Pipeline Steps

```python
def prepare_terrain(dem_layer, tx_lat, tx_lon, max_range_km, resolution_m):
    """
    Convert any QGIS raster layer to .abt tiles for aether_core.
    
    Returns: path to directory containing .abt tiles
    """
    
    # Step 1: Reject WMS/WMTS layers (rendered images, not elevation data)
    if dem_layer.providerType() in ('wms', 'wmts'):
        raise ValueError(
            "WMS/WMTS layers provide rendered images, not elevation data. "
            "Please select a DEM raster layer (GeoTIFF, WCS, etc.)."
        )
    
    # Step 2: Calculate bounding box (TX location ± max_range)
    range_deg = max_range_km / 111.0  # approximate degrees
    bbox = {
        'north': tx_lat + range_deg,
        'south': tx_lat - range_deg,
        'east': tx_lon + range_deg / math.cos(math.radians(tx_lat)),
        'west': tx_lon - range_deg / math.cos(math.radians(tx_lat)),
    }
    
    # Step 3: Check cache — skip conversion if .abt already exists for this bbox+resolution
    cache_key = hashlib.md5(
        f"{dem_layer.source()}|{bbox}|{resolution_m}".encode()
    ).hexdigest()
    cache_dir = os.path.join(settings.cache_dir, cache_key)
    if os.path.exists(cache_dir) and any(f.endswith('.abt') for f in os.listdir(cache_dir)):
        return cache_dir
    
    # Step 4: Extract + reproject to WGS84 using GDAL
    from osgeo import gdal
    temp_tif = os.path.join(tempfile.gettempdir(), f"aether_extract_{cache_key}.tif")
    gdal.Warp(
        temp_tif,
        dem_layer.source(),
        dstSRS="EPSG:4326",
        outputBounds=[bbox['west'], bbox['south'], bbox['east'], bbox['north']],
        format="GTiff",
        outputType=gdal.GDT_Float32,
        resampleAlg=gdal.GRA_Bilinear,
    )
    
    # Step 5: Calculate tile parameters
    deg_per_m = 1.0 / 111111.0
    pixel_deg = resolution_m * deg_per_m
    width_px = int(math.ceil((bbox['east'] - bbox['west']) / pixel_deg))
    height_px = int(math.ceil((bbox['north'] - bbox['south']) / pixel_deg))
    size_px = max(width_px, height_px)
    size_px = ((size_px + 3) // 4) * 4  # round up to multiple of 4 for BC6H
    
    # Step 6: Build converter job JSON
    os.makedirs(cache_dir, exist_ok=True)
    job = {
        "output_path": os.path.join(cache_dir, f"tile_{cache_key}.abt"),
        "format": "r16sint",
        "ul_lat": bbox['north'],
        "ul_lon": bbox['west'],
        "resolution_m": resolution_m,
        "size_px": size_px,
        "base_tif": os.path.abspath(temp_tif),
        "swiss_tifs": []
    }
    job_file = os.path.join(cache_dir, "convert_job.json")
    with open(job_file, 'w') as f:
        json.dump(job, f)
    
    # Step 7: Run aether_converter
    converter_exe = binary_manager.find_binary('aether_converter')
    result = subprocess.run(
        [converter_exe, "ingest", "--job-file", job_file],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Terrain conversion failed: {result.stderr}")
    
    # Step 8: Clean up temp GeoTIFF
    os.remove(temp_tif)
    
    return cache_dir
```

### 3.5 Supported terrain source types

| Source Type | Example | How Plugin Accesses It |
|-------------|---------|----------------------|
| Local GeoTIFF | SwissAlti3D .tif, SRTM .hgt | `layer.source()` → file path → GDAL reads directly |
| WCS (Web Coverage Service) | Copernicus WCS, national DEM services | GDAL reads via `/vsicurl/`. Plugin passes `layer.source()` which includes WCS params. |
| Cloud-hosted COG | Copernicus S3 COGs, 3DEP on AWS | GDAL reads via `/vsicurl/` with HTTP range requests. Only fetches needed bytes. |
| Virtual Raster (VRT) | Multi-file mosaic | GDAL reads VRT transparently. Plugin treats as single raster. |
| PostGIS Raster | Database-hosted DEM | GDAL reads via `PG:` connection string from `layer.source()`. |
| **WMS / WMTS** | **Any map tile service** | **REJECTED** — returns rendered images, not elevation values. Plugin shows error. |
| **XYZ Tiles** | **Terrain RGB tiles** | **REJECTED for v1** — would need RGB→elevation decode. Possible future enhancement. |

### 3.6 Performance considerations

- **GDAL extraction is fast:** Extracting a 50 km bbox from a local GeoTIFF takes < 1 second.
- **Remote sources (WCS, COG):** 1–5 seconds depending on network + file size. GDAL caches internally.
- **aether_converter:** Conversion to .abt takes 1–10 seconds for typical tile sizes.
- **Caching eliminates repeat costs:** The cache key includes source path + bbox + resolution. Subsequent simulations in the same area are instant (cache hit).
- **Large area at high resolution:** A 200 km radius at 2 m = ~200,000 × 200,000 pixels. This would produce a multi-GB temp GeoTIFF and take minutes to convert. The plugin should warn the user before proceeding with large high-res jobs.

---

## 4. Binary Management (`binary_manager.py`)

### 4.1 Binary Discovery

Check in order (first match wins):
1. User-configured path in plugin settings (`QgsSettings("aether/binary_dir")`)
2. `AETHER_BIN_DIR` environment variable
3. `PATH` environment variable (search for `aether_core` / `aether_core.exe`)
4. Default location: `~/.aether/bin/`
5. Plugin install directory: `<plugin_dir>/bin/`

Required binaries (3 total):
- `aether_core` / `aether_core.exe` — main propagation engine
- `aether_converter` / `aether_converter.exe` — terrain format converter
- `aether_export` / `aether_export.exe` — GeoTIFF export (actually named `aether_export`)

### 4.2 Automatic Download

Triggered by "Download Binaries" button in settings, or on first run when binaries are not found.

```python
import platform, zipfile, urllib.request, stat

GITHUB_RELEASE_URL = "https://github.com/<org>/aether/releases/latest/download"
# or a fixed URL: "https://aether-rf.com/downloads/latest"

PLATFORM_MAP = {
    ("Windows", "AMD64"):  "aether-windows-x64.zip",
    ("Linux",   "x86_64"): "aether-linux-x64.zip",
    ("Darwin",  "arm64"):  "aether-macos-arm64.zip",
    ("Darwin",  "x86_64"): "aether-macos-x64.zip",
}

def download_binaries(target_dir="~/.aether/bin"):
    target_dir = os.path.expanduser(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    
    # 1. Detect platform
    system = platform.system()
    machine = platform.machine()
    zip_name = PLATFORM_MAP.get((system, machine))
    if not zip_name:
        raise RuntimeError(f"Unsupported platform: {system} {machine}")
    
    # 2. Download ZIP
    url = f"{GITHUB_RELEASE_URL}/{zip_name}"
    zip_path = os.path.join(target_dir, zip_name)
    urllib.request.urlretrieve(url, zip_path, reporthook=progress_callback)
    
    # 3. Extract
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(target_dir)
    os.remove(zip_path)
    
    # 4. Set executable permissions (Linux/macOS)
    if system != "Windows":
        for binary in ['aether_core', 'aether_converter', 'aether_export']:
            path = os.path.join(target_dir, binary)
            if os.path.exists(path):
                os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    
    # 5. Save path in settings
    QgsSettings().setValue("aether/binary_dir", target_dir)
    
    # 6. Verify
    verify_binaries(target_dir)
    
    return target_dir
```

### 4.3 Prerequisites Check

```python
def check_prerequisites(binary_dir):
    """Check runtime dependencies. Returns list of issues (empty = OK)."""
    issues = []
    
    # Check all 3 binaries exist
    for name in ['aether_core', 'aether_converter', 'aether_export']:
        exe = name + ('.exe' if platform.system() == 'Windows' else '')
        path = os.path.join(binary_dir, exe)
        if not os.path.exists(path):
            issues.append(f"Binary not found: {path}")
    
    # Windows: check dxcompiler.dll alongside aether_core
    if platform.system() == 'Windows':
        dxc = os.path.join(binary_dir, 'dxcompiler.dll')
        if not os.path.exists(dxc):
            issues.append("dxcompiler.dll not found (required for GPU on Windows)")
    
    # Linux: check Vulkan availability
    if platform.system() == 'Linux':
        try:
            subprocess.run(['vulkaninfo', '--summary'], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            issues.append("Vulkan drivers not detected. GPU mode may not work. "
                         "Install: apt install libvulkan1 mesa-vulkan-drivers")
    
    return issues
```

### 4.4 Build Notes for Binary Packaging

| Platform | Build Target | Key Flags | Bundled Files |
|----------|-------------|-----------|---------------|
| Windows x64 | `x86_64-pc-windows-msvc` | `RUSTFLAGS="-C target-feature=+crt-static"` (eliminates VC++ Redistributable) | `aether_core.exe`, `aether_converter.exe`, `aether_export.exe`, `dxcompiler.dll` |
| Linux x64 | `x86_64-unknown-linux-musl` | Static binary, zero glibc dependency | `aether_core`, `aether_converter`, `aether_export` |
| macOS ARM | `aarch64-apple-darwin` | Must be code-signed + notarized ($99/yr Apple Developer) | `aether_core`, `aether_converter`, `aether_export` |
| macOS x64 | `x86_64-apple-darwin` | Same signing requirement | Same |

**Important:** These are **non-proprietary** builds (no `-Proprietary` flag, no shader encryption). The API key gates the plugin, not the binary.

---

## 5. API Key System

### 5.1 Key Format

The API key is a signed token validated **offline** (no server round-trip). It uses Ed25519 signatures with a public key embedded in the plugin source.

```
Key structure:
  payload = JSON: {"email_hash": "sha256...", "issued": "2026-05-07", "expiry": "2026-11-07", "tier": "non-commercial"}
  signature = Ed25519.sign(payload_bytes, server_private_key)  [64 bytes]
  api_key = base64url(payload_bytes + signature)
```

### 5.2 Server-Side Key Generation (webpage backend)

Minimal Flask app (or serverless function):

```python
# api_server.py (runs on aether-rf.com)
from flask import Flask, request, jsonify
import nacl.signing, json, base64, datetime, hashlib

app = Flask(__name__)
SIGNING_KEY = nacl.signing.SigningKey(open("api_private_key.bin", "rb").read())

@app.route("/api/register", methods=["POST"])
def register():
    email = request.form["email"]
    # Validate email, check non-commercial agreement, etc.
    
    payload = json.dumps({
        "email_hash": hashlib.sha256(email.encode()).hexdigest()[:16],
        "issued": datetime.date.today().isoformat(),
        "expiry": (datetime.date.today() + datetime.timedelta(days=183)).isoformat(),
        "tier": "non-commercial"
    }).encode()
    
    signed = SIGNING_KEY.sign(payload)
    api_key = base64.urlsafe_b64encode(signed.message + signed.signature).decode()
    
    # Send key via email + display on page
    return jsonify({"api_key": api_key, "expires": "2026-11-07"})
```

### 5.3 Plugin-Side Validation (`api_key.py`)

```python
import json, base64, datetime

# Embedded in plugin — public key only, safe to distribute
AETHER_API_PUBLIC_KEY = bytes.fromhex("<ed25519_public_key_hex_here>")

def validate_api_key(key_string):
    """
    Validate API key offline using embedded Ed25519 public key.
    Returns: (is_valid: bool, message: str, expiry_date: date or None)
    """
    try:
        raw = base64.urlsafe_b64decode(key_string)
        payload_bytes = raw[:-64]
        signature = raw[-64:]
        
        # Verify Ed25519 signature
        from nacl.signing import VerifyKey
        vk = VerifyKey(AETHER_API_PUBLIC_KEY)
        vk.verify(payload_bytes, signature)  # raises BadSignatureError if invalid
        
        data = json.loads(payload_bytes)
        expiry = datetime.date.fromisoformat(data["expiry"])
        
        if datetime.date.today() > expiry:
            return False, f"Key expired on {expiry}", expiry
        
        return True, f"Valid until {expiry}", expiry
        
    except Exception as e:
        return False, f"Invalid key: {e}", None
```

### 5.4 Where the Key Is Checked

- **Plugin startup:** Validate stored key, show status in settings bar / status indicator
- **Before each simulation:** Call `validate_api_key()`. If expired or missing:
  - Show dialog: "Your API key is expired/missing. [Enter Key] [Get New Key (opens browser)]"
  - Block simulation until valid key is provided
- **Key does NOT gate the binaries** — it gates the plugin's Python orchestration layer

### 5.5 Key Storage

```python
from qgis.core import QgsSettings

# Store
QgsSettings().setValue("aether/api_key", key_string)

# Retrieve
key = QgsSettings().value("aether/api_key", "")
```

### 5.6 Webpage (aether-rf.com/register)

Simple registration page:
- Fields: Email, Name, Organization, "I confirm non-commercial use" checkbox
- Submit → server generates key → displays on page + sends via email
- Renewal: same form, re-issues key with new 6-month expiry
- Minimal tech stack: static HTML + serverless backend (AWS Lambda / Cloudflare Worker) or small Flask app
- The registration page should explain:
  - What "non-commercial" means
  - That the key expires after 6 months and must be renewed
  - Link to commercial licensing for commercial use

---

## 6. Job Execution Flow

### 6.1 Site Analysis (Coverage)

```python
def run_coverage(params, dem_layer, output_dir, feedback):
    """
    Full execution pipeline for site analysis.
    Called from coverage algorithm's processAlgorithm().
    """
    # 1. Validate API key
    key = QgsSettings().value("aether/api_key", "")
    valid, msg, _ = api_key.validate_api_key(key)
    if not valid:
        raise AuthError(msg)
    
    # 2. Extract + convert terrain
    feedback.setProgressText("Preparing terrain data...")
    abt_dir = terrain_adapter.prepare_terrain(
        dem_layer, params.tx_lat, params.tx_lon,
        params.max_range_km, params.resolution_m
    )
    
    # 3. Build job config JSON
    # (Port structure from python/drivers/run_simulation.py:92-121)
    job_config = {
        "tx": {
            "lat": params.tx_lat,
            "lon": params.tx_lon,
            "height_m": params.tx_height,
            "mode": params.tx_mode,
            "freq_mhz": params.freq_mhz,
            "erp_watts": params.erp_watts,
            # Optional antenna patterns
            **({"az_pattern_file": params.az_pattern} if params.az_pattern else {}),
            **({"el_pattern_file": params.el_pattern} if params.el_pattern else {}),
            **({"az_rotation": params.az_rotation} if params.az_rotation else {}),
        },
        "rx": {
            "height_m": params.rx_height,
            "mode": params.rx_mode,
        },
        "analysis": {
            "task_type": "SINGLE",
            "propagation_model": params.model,  # "LOS", "SIMPLE_LOSS", "ITM"
            "max_range_km": params.max_range_km,
            "resolution_m": params.resolution_m,  # Must be 2, 5, 10, or 30
            "azimuth_start_deg": params.az_start,
            "azimuth_end_deg": params.az_end,
            "compute_backend": params.backend,  # "AUTO", "GPU", "CPU"
        },
        "output": {
            "directory": output_dir,
            "filename": params.output_name,
        },
        "processing": {
            "terrain_dir": abt_dir,
            "max_ram_usage_gb": params.max_ram_gb or 16,
            "max_vram_usage_gb": params.max_vram_gb or 8,
        },
        "propagation": {
            "eps_dielect": params.eps,
            "sgm_conductivity": params.sgm,
            "eno_ns_surfref": params.ens,
            "radio_climate": params.climate,
            "pol": params.pol,
            "conf": params.conf,
            "rel": params.rel,
            "earth_radius_mode": params.earth_radius,
        }
    }
    job_file = os.path.join(output_dir, f"{params.output_name}_job.json")
    with open(job_file, 'w') as f:
        json.dump(job_config, f, indent=2)
    
    # 4. Run aether_core
    feedback.setProgressText("Running simulation...")
    aether_core = binary_manager.find_binary('aether_core')
    proc = subprocess.Popen(
        [aether_core, "--config", job_file],
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "RUST_LOG": "info"}
    )
    
    # Parse stderr for progress (look for "Wedge X/Y" pattern)
    for line in proc.stderr:
        if feedback.isCanceled():
            proc.kill()
            return None
        # Parse progress from log lines
        match = re.search(r'Wedge (\d+)/(\d+)', line)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            feedback.setProgress(int(50 + 40 * current / total))
    
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("aether_core failed. Check QGIS log for details.")
    
    # 5. Export to GeoTIFF
    feedback.setProgressText("Exporting GeoTIFF...")
    bit_path = os.path.join(output_dir, f"{params.output_name}.bit")
    json_path = os.path.join(output_dir, f"{params.output_name}.json")
    tif_path = os.path.join(output_dir, f"{params.output_name}.tif")
    
    # Check for .tiles format (newer) vs .bit (legacy)
    tiles_path = os.path.join(output_dir, f"{params.output_name}.tiles")
    input_path = tiles_path if os.path.exists(tiles_path) else bit_path
    
    aether_export = binary_manager.find_binary('aether_export')
    subprocess.run(
        [aether_export, input_path, json_path, tif_path],
        check=True
    )
    
    # 6. Load result into QGIS
    if params.add_to_map:
        result_layer = result_loader.load_coverage_result(tif_path, params)
        QgsProject.instance().addMapLayer(result_layer)
    
    return tif_path
```

### 6.2 P2P Link Analysis

Similar flow, key differences:
- `task_type` = `"P2P"` (single link) or `"BATCH_P2P"` (batch CSV)
- Single link: plugin generates a temporary 2-line batch CSV
- Terrain extraction covers the bounding box of all TX-RX pairs (envelope)
- Output parsing: read the result CSV for signal/loss values
- Single link also generates terrain profile files (`.gp` format)

### 6.3 Result Loading (`result_loader.py`)

```python
def load_coverage_result(tif_path, params):
    """Load GeoTIFF result as a styled QGIS raster layer."""
    layer = QgsRasterLayer(tif_path, f"AETHER {params.model} {params.output_name}")
    
    if params.model == "LOS":
        # Binary visibility: green (visible) / transparent (not visible)
        apply_single_band_pseudocolor(layer, {0: "transparent", 1: "#00FF00"})
    elif params.model in ("SIMPLE_LOSS", "ITM"):
        # Signal strength: red-yellow-green color ramp
        apply_continuous_color_ramp(layer, min_val=-140, max_val=0,
            colors=["#FF0000", "#FFFF00", "#00FF00"])
    
    return layer
```

---

## 7. Changes to Existing Codebase

### aether_core: **NONE**
### aether_converter: **NONE**
### aether_export: **NONE**

All new code lives in the `aether_qgis/` plugin directory. Communication with AETHER binaries is exclusively through:
- JSON config files (written by plugin, read by binaries)
- Subprocess invocation
- File outputs (.bit/.tiles, .json metadata, .tif)

### Possible future enhancement (NOT in this plan)

Adding structured JSON progress output to aether_core (`{"wedge": 5, "total": 72, "pct": 7}` on stderr) would enable a smoother progress bar. Current approach: regex-parse log lines. Acceptable for v1.

---

## 8. Distribution

### QGIS Plugin Repository (official)
- Plugin code only: pure Python, < 1 MB
- Plugin license: GPLv2+ (required by QGIS repo; covers only the thin Python wrapper)
- On first run: prompts user to download binaries + enter API key
- metadata.txt includes: `homepage=`, `repository=`, `tracker=` (GitHub links)

### Binary Distribution (separate)
- GitHub Releases: platform-specific ZIPs per tag
- ZIP naming: `aether-{version}-{platform}.zip` (e.g., `aether-0.1.0-windows-x64.zip`)
- Non-proprietary builds (no shader encryption, no vendor license.key required)
- Download URL pattern embedded in `binary_manager.py` (configurable)

### API Key Webpage
- Hosted at aether-rf.com/register (or equivalent)
- Linked from plugin settings dialog ("Get API Key" button opens browser)

---

## 9. Testing & Verification

### 9.1 Unit Tests (pytest)

| Test Module | What It Tests |
|-------------|---------------|
| `test_api_key.py` | Key generation, validation, expiry detection, tampered key rejection |
| `test_job_builder.py` | JSON config generation, all parameter combinations, defaults |
| `test_terrain_adapter.py` | Bbox calculation, tile size rounding, cache key generation |
| `test_binary_manager.py` | Platform detection, path discovery, prerequisites check |

### 9.2 Integration Test

Full pipeline with bundled test data:
1. Load a sample GeoTIFF DEM into QGIS (include small test DEM in `tests/fixtures/`)
2. Run Site Analysis with LOS model, 10 km range, 30 m resolution
3. Verify: .abt tile created, aether_core completes, GeoTIFF output exists and loads as QGIS layer
4. Run P2P single link between two points
5. Verify: CSV output with valid signal/loss values

### 9.3 Manual Test Matrix

| Platform | QGIS Version | Terrain Source | Model | Status |
|----------|-------------|----------------|-------|--------|
| Windows 10 | 3.34 LTR | Local GeoTIFF | LOS | |
| Windows 10 | 3.34 LTR | Local GeoTIFF | ITM | |
| Ubuntu 22.04 | 3.34 LTR | Local GeoTIFF | LOS | |
| Ubuntu 22.04 | 3.34 LTR | WCS layer | SIMPLE_LOSS | |
| macOS 14 (ARM) | 3.34 LTR | Local GeoTIFF | LOS | |
| Any | 3.34 LTR | VRT mosaic | ITM | |
| Any | 3.34 LTR | WMS layer | Any | Should reject with clear error |

### 9.4 Edge Cases to Verify

- Missing GPU → CPU fallback works
- Expired API key → clear error message with renewal link
- No internet during binary download → clear error
- WMS layer selected as DEM → rejected with explanation
- Very large area at high resolution → warning before proceeding
- Non-WGS84 DEM (e.g., UTM) → auto-reprojected correctly
- Cancellation during simulation → process killed cleanly

---

## 10. Implementation Phases

### Phase 1: Core Plugin Skeleton (Week 1)
- [ ] `__init__.py`, `plugin.py`, `provider.py`, `metadata.txt`
- [ ] Settings dialog: binary path configuration (manual only)
- [ ] Binary discovery (search PATH + default locations)
- [ ] API key stub (accept any non-empty string for development)
- [ ] Basic menu/toolbar integration in QGIS

### Phase 2: Site Analysis — End to End (Weeks 2–3)
- [ ] `coverage_dialog.py` + `.ui` — full parameter dialog with all 3 tabs
- [ ] `terrain_adapter.py` — GDAL extract + reproject + aether_converter
- [ ] `job_builder.py` — construct aether_core JSON config from dialog parameters
- [ ] `result_loader.py` — load GeoTIFF, apply model-appropriate color ramp
- [ ] Coverage algorithm: click map → run simulation → see result on map
- [ ] Progress feedback (parse stderr logs)
- [ ] .abt tile caching

### Phase 3: P2P Analysis (Week 4)
- [ ] `p2p_dialog.py` + `.ui` — single link + batch CSV modes
- [ ] Single link: temp batch CSV generation, result display in dialog
- [ ] Batch: CSV file parsing, preview table, result CSV loading
- [ ] Terrain profile visualization (matplotlib embedded in dialog, or QGIS plot)

### Phase 4: Binary Download + API Key (Week 5)
- [ ] `binary_manager.py` — GitHub Release download, platform detection, extraction, verification
- [ ] `api_key.py` — Ed25519 offline validation with embedded public key
- [ ] API key webpage backend (Flask/serverless: registration → key generation → email)
- [ ] Settings dialog integration: download button, key input + status, prerequisite warnings
- [ ] Generate Ed25519 keypair for API key system (separate from vendor_keys.json)

### Phase 5: Polish & Distribution (Week 6)
- [ ] Error handling: user-friendly messages for all failure modes
- [ ] Progress bar improvements
- [ ] Color ramp presets (LOS binary, signal strength gradient, path loss gradient)
- [ ] Package plugin ZIP for QGIS Plugin Repository submission
- [ ] Write user documentation (README with screenshots)
- [ ] Platform testing (Windows, Linux, macOS)
- [ ] Publish initial GitHub Release with platform binaries

**Total estimated effort: 5–6 weeks**
