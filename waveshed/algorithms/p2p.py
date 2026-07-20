"""QgsProcessingAlgorithm for P2P Link Analysis (P2P / BATCH_P2P).

Supports both single-link (TX/RX coordinate parameters) and batch
(user-provided CSV) modes.  When no batch file is given, a temporary
2-line CSV is generated from the TX/RX parameters.
"""

from __future__ import annotations

import csv
import math
import os
import platform
import subprocess
import tempfile

_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsRasterLayer,
)

from ..core.binary_manager import find_binary
from ..core.job_builder import P2PParams, build_p2p_job, write_job_file
from ..core.terrain_adapter import prepare_terrain
from ..core import api_key
from ..core import binary_manager as bm

# Enum value lists (index-based for QgsProcessingParameterEnum).
_MODELS = ["LOS", "SIMPLE_LOSS", "ITM"]
_RESOLUTIONS = ["2", "5", "10", "30"]
_BACKENDS = ["AUTO", "GPU", "CPU"]


class P2PAlgorithm(QgsProcessingAlgorithm):
    """Point-to-Point Link Analysis algorithm for the Processing toolbox."""

    # -- Parameter names (constants) ----------------------------------------

    INPUT_DEM = "INPUT_DEM"
    BATCH_FILE = "BATCH_FILE"
    TX_LAT = "TX_LAT"
    TX_LON = "TX_LON"
    TX_HEIGHT = "TX_HEIGHT"
    RX_LAT = "RX_LAT"
    RX_LON = "RX_LON"
    RX_HEIGHT = "RX_HEIGHT"
    MODEL = "MODEL"
    RESOLUTION = "RESOLUTION"
    BACKEND = "BACKEND"
    OUTPUT_DIR = "OUTPUT_DIR"

    # -- Metadata -----------------------------------------------------------

    def name(self) -> str:
        return "p2p"

    def displayName(self) -> str:
        return "Point-to-Point Link Analysis"

    def group(self) -> str:
        return "Analysis"

    def groupId(self) -> str:
        return "analysis"

    def shortHelpString(self) -> str:
        return (
            "Performs point-to-point RF link analysis using the AETHER engine.\n\n"
            "Supports two modes:\n"
            "  - Single link: Provide TX/RX coordinates directly.\n"
            "  - Batch: Provide a CSV file with multiple S/R entries.\n\n"
            "When a batch CSV is provided, the TX/RX coordinate parameters are "
            "ignored and the CSV drives the analysis.\n\n"
            "Batch CSV format:\n"
            "  S,TX1,47.0563,8.4846,40.0,AGL\n"
            "  R,RX1,47.5000,8.9000,10.0,AGL\n\n"
            "Fields: type(S|R), ID, lat, lon, alt_m, mode(AGL|AMSL)"
        )

    def createInstance(self):
        return P2PAlgorithm()

    # -- Parameter definitions -----------------------------------------------

    def initAlgorithm(self, config: Optional[Dict[str, Any]] = None) -> None:
        # DEM layer
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_DEM,
                "DEM Layer",
            )
        )

        # Batch file (optional)
        self.addParameter(
            QgsProcessingParameterFile(
                self.BATCH_FILE,
                "Batch CSV File",
                behavior=QgsProcessingParameterFile.File,
                fileFilter="CSV Files (*.csv)",
                optional=True,
            )
        )

        # TX coordinates
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TX_LAT,
                "TX Latitude",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=-90.0,
                maxValue=90.0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TX_LON,
                "TX Longitude",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=-180.0,
                maxValue=180.0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TX_HEIGHT,
                "TX Height (m)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=30.0,
                minValue=0.0,
                maxValue=10000.0,
                optional=True,
            )
        )

        # RX coordinates
        self.addParameter(
            QgsProcessingParameterNumber(
                self.RX_LAT,
                "RX Latitude",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=-90.0,
                maxValue=90.0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.RX_LON,
                "RX Longitude",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=-180.0,
                maxValue=180.0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.RX_HEIGHT,
                "RX Height (m)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.5,
                minValue=0.0,
                maxValue=10000.0,
                optional=True,
            )
        )

        # Analysis options
        self.addParameter(
            QgsProcessingParameterEnum(
                self.MODEL,
                "Propagation Model",
                options=_MODELS,
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESOLUTION,
                "Resolution (m)",
                options=_RESOLUTIONS,
                defaultValue=2,  # index 2 -> "10"
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.BACKEND,
                "Compute Backend",
                options=_BACKENDS,
                defaultValue=0,
            )
        )

        # Output
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_DIR,
                "Output Directory",
            )
        )

    # -- Processing ----------------------------------------------------------

    def processAlgorithm(
        self,
        parameters: Dict[str, Any],
        context: Any,
        feedback: Any,
    ) -> Dict[str, Any]:
        """Execute the P2P analysis pipeline.

        Steps:
            1. Validate API key
            2. Determine batch file (user-provided or generated from TX/RX)
            3. Compute terrain bounding box from CSV entries
            4. Prepare terrain via terrain_adapter
            5. Build P2P job via job_builder
            6. Run aether_core
            7. Export results via aether_export
            8. Return output path
        """

        # ---- Resolve parameters ----
        dem_layer: QgsRasterLayer = self.parameterAsRasterLayer(
            parameters, self.INPUT_DEM, context,
        )
        batch_file: Optional[str] = self.parameterAsFile(
            parameters, self.BATCH_FILE, context,
        )
        tx_lat: float = self.parameterAsDouble(parameters, self.TX_LAT, context)
        tx_lon: float = self.parameterAsDouble(parameters, self.TX_LON, context)
        tx_height: float = self.parameterAsDouble(parameters, self.TX_HEIGHT, context)
        rx_lat: float = self.parameterAsDouble(parameters, self.RX_LAT, context)
        rx_lon: float = self.parameterAsDouble(parameters, self.RX_LON, context)
        rx_height: float = self.parameterAsDouble(parameters, self.RX_HEIGHT, context)

        model_idx: int = self.parameterAsEnum(parameters, self.MODEL, context)
        resolution_idx: int = self.parameterAsEnum(parameters, self.RESOLUTION, context)
        backend_idx: int = self.parameterAsEnum(parameters, self.BACKEND, context)

        output_dir: str = self.parameterAsString(parameters, self.OUTPUT_DIR, context)

        model = _MODELS[model_idx]
        resolution_m = int(_RESOLUTIONS[resolution_idx])
        backend = _BACKENDS[backend_idx]

        os.makedirs(output_dir, exist_ok=True)

        # ---- 1. Determine batch file ----
        is_batch = bool(batch_file and batch_file.strip())

        if not is_batch:
            # Generate temp 2-line CSV from TX/RX params.
            feedback.setProgressText("Generating single-link batch CSV...")
            batch_file = self._write_temp_csv(
                tx_lat, tx_lon, tx_height,
                rx_lat, rx_lon, rx_height,
            )

        # ---- 3. Compute terrain bbox from CSV entries ----
        feedback.setProgressText("Computing terrain extents...")
        feedback.setProgress(5)

        entries = self._parse_csv(batch_file)
        centre_lat, centre_lon, max_range_km = self._compute_terrain_extent(entries)

        if feedback.isCanceled():
            return {}

        # ---- 4. Prepare terrain ----
        feedback.setProgressText("Preparing terrain tiles...")
        feedback.setProgress(10)

        abt_dir = prepare_terrain(
            dem_layer=dem_layer,
            tx_lat=centre_lat,
            tx_lon=centre_lon,
            max_range_km=max_range_km,
            resolution_m=resolution_m,
            binary_manager=bm,
            feedback=feedback,
        )

        if feedback.isCanceled():
            return {}

        # ---- 5. Build job config ----
        feedback.setProgressText("Building job configuration...")
        feedback.setProgress(20)

        output_name = f"p2p_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        params = P2PParams(
            tx_lat=tx_lat,
            tx_lon=tx_lon,
            tx_height=tx_height,
            tx_mode="AGL",
            freq_mhz=433.0,
            erp_watts=10.0,
            rx_height=rx_height,
            rx_mode="AGL",
            model=model,
            resolution_m=resolution_m,
            max_range_km=int(math.ceil(max_range_km)),
            backend=backend,
            output_name=output_name,
        )

        job_config = build_p2p_job(params, abt_dir, output_dir, batch_file=batch_file)
        job_file = write_job_file(job_config, output_dir, output_name)

        if feedback.isCanceled():
            return {}

        # ---- 6. Run aether_core ----
        feedback.setProgressText("Running aether_core...")
        feedback.setProgress(25)

        core_exe = find_binary("aether_core")

        # aether_core is licensed — validate the API key and inject it as
        # AETHER_LICENSE. (aether_converter / aether_export are unlicensed.)
        core_env = os.environ.copy()
        try:
            api_key.apply_license_env(core_env)
        except api_key.ApiKeyError as exc:
            raise QgsProcessingException(str(exc)) from exc

        proc = subprocess.Popen(
            [core_exe, "--config", job_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # aether_core logs everything to stdout
            text=True,
            env=core_env,
            creationflags=_SUBPROCESS_FLAGS,
        )

        # aether_core writes all its diagnostics to stdout — surface every
        # line and keep a tail for the error message. (Previously only stderr
        # was read, so none of it was visible and stdout could deadlock.)
        output_lines: list[str] = []
        for line in iter(proc.stdout.readline, ""):
            if feedback.isCanceled():
                proc.terminate()
                return {}
            line = line.rstrip()
            if not line:
                continue
            output_lines.append(line)
            feedback.pushInfo(line)

        proc.wait()
        if proc.returncode != 0:
            tail = "\n".join(output_lines[-20:])
            raise QgsProcessingException(
                f"aether_core exited with code {proc.returncode}:\n{tail}"
            )

        if feedback.isCanceled():
            return {}

        # ---- 7. Export results ----
        feedback.setProgressText("Exporting results...")
        feedback.setProgress(80)

        export_exe = find_binary("aether_export")

        bit_file = os.path.join(output_dir, output_name + ".bit")
        tiles_file = os.path.join(output_dir, output_name + ".tiles")
        input_file = bit_file if os.path.isfile(bit_file) else tiles_file

        json_sidecar = os.path.join(output_dir, output_name + ".json")
        output_path = os.path.join(output_dir, output_name + ".csv")

        result = subprocess.run(
            [export_exe, "-i", input_file, "-j", json_sidecar, "-o", output_path],
            capture_output=True,
            text=True,
            creationflags=_SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            raise QgsProcessingException(
                f"aether_export failed (exit {result.returncode}):\n{result.stderr}"
            )

        feedback.setProgress(100)
        feedback.setProgressText("Complete.")

        return {self.OUTPUT_DIR: output_path}

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _write_temp_csv(
        tx_lat: float,
        tx_lon: float,
        tx_height: float,
        rx_lat: float,
        rx_lon: float,
        rx_height: float,
    ) -> str:
        """Write a temporary 2-line batch CSV for single-link analysis."""
        path = os.path.join(
            tempfile.gettempdir(),
            f"aether_p2p_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "S", "TX",
                f"{tx_lat:.6f}", f"{tx_lon:.6f}",
                f"{tx_height:.1f}", "AGL",
            ])
            writer.writerow([
                "R", "RX",
                f"{rx_lat:.6f}", f"{rx_lon:.6f}",
                f"{rx_height:.1f}", "AGL",
            ])
        return path

    @staticmethod
    def _parse_csv(
        path: str,
    ) -> List[Tuple[str, str, float, float, float, str]]:
        """Parse an AETHER batch CSV.

        Returns a list of (type, id, lat, lon, alt_m, mode) tuples.
        """
        entries: List[Tuple[str, str, float, float, float, str]] = []
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            for line_no, row in enumerate(reader, start=1):
                if not row or row[0].startswith("#"):
                    continue
                if len(row) < 6:
                    raise QgsProcessingException(
                        f"Batch CSV line {line_no}: expected 6 fields, got {len(row)}"
                    )
                row_type = row[0].strip().upper()
                if row_type not in ("S", "R"):
                    raise QgsProcessingException(
                        f"Batch CSV line {line_no}: type must be 'S' or 'R', "
                        f"got '{row[0].strip()}'"
                    )
                entries.append((
                    row_type,
                    row[1].strip(),
                    float(row[2].strip()),
                    float(row[3].strip()),
                    float(row[4].strip()),
                    row[5].strip().upper(),
                ))
        return entries

    @staticmethod
    def _compute_terrain_extent(
        entries: List[Tuple[str, str, float, float, float, str]],
    ) -> Tuple[float, float, float]:
        """Compute (centre_lat, centre_lon, max_range_km) from CSV entries.

        Returns the centroid of all points and a radius large enough to
        enclose them all (with a 5 km safety margin, minimum 10 km).
        """
        lats = [e[2] for e in entries]
        lons = [e[3] for e in entries]

        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        centre_lat = (min_lat + max_lat) / 2.0
        centre_lon = (min_lon + max_lon) / 2.0

        lat_span_km = (max_lat - min_lat) * 111.0
        cos_lat = math.cos(math.radians(centre_lat))
        if cos_lat < 1e-6:
            cos_lat = 1e-6
        lon_span_km = (max_lon - min_lon) * 111.0 * cos_lat
        half_diag_km = math.sqrt(lat_span_km**2 + lon_span_km**2) / 2.0

        max_range_km = max(half_diag_km + 5.0, 10.0)

        return centre_lat, centre_lon, max_range_km
