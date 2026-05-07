"""QgsProcessingAlgorithm for Site Analysis (Coverage).

Runs the full coverage pipeline from the QGIS Processing toolbox:
  terrain prep -> aether_core -> aether_export -> load result.

Parameters mirror the GUI dialog but are exposed as Processing parameters
for scripting, batch execution, and model-builder integration.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
)

from ..core.api_key import ApiKeyError, check_key_or_raise
from ..core.binary_manager import find_binary
from ..core.job_builder import CoverageParams, build_coverage_job, write_job_file
from ..core.result_loader import add_layer_to_project, load_coverage_result
from ..core.terrain_adapter import prepare_terrain
from ..core import binary_manager as bm


class CoverageAlgorithm(QgsProcessingAlgorithm):
    """Site Analysis (Coverage) — Processing toolbox algorithm."""

    # Parameter keys
    INPUT_DEM = "INPUT_DEM"
    TX_LAT = "TX_LAT"
    TX_LON = "TX_LON"
    TX_HEIGHT = "TX_HEIGHT"
    FREQ_MHZ = "FREQ_MHZ"
    ERP_WATTS = "ERP_WATTS"
    MODEL = "MODEL"
    RESOLUTION = "RESOLUTION"
    MAX_RANGE = "MAX_RANGE"
    BACKEND = "BACKEND"
    OUTPUT_DIR = "OUTPUT_DIR"

    # Enum value lists (order matters — index is the value)
    _MODELS = ["LOS", "SIMPLE_LOSS", "ITM"]
    _RESOLUTIONS = ["2", "5", "10", "30"]
    _BACKENDS = ["AUTO", "GPU", "CPU"]

    # -- Metadata ----------------------------------------------------------

    def name(self) -> str:
        return "coverage"

    def displayName(self) -> str:
        return "Site Analysis (Coverage)"

    def group(self) -> str:
        return "Analysis"

    def groupId(self) -> str:
        return "analysis"

    def shortHelpString(self) -> str:
        return (
            "Run an AETHER coverage (site analysis) computation.\n\n"
            "Prepares terrain from the selected DEM, runs the propagation "
            "model, and exports the result as a Cloud-Optimized GeoTIFF."
        )

    def createInstance(self) -> "CoverageAlgorithm":
        return CoverageAlgorithm()

    # -- Parameters --------------------------------------------------------

    def initAlgorithm(self, config: Dict[str, Any] = None) -> None:
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_DEM,
                "DEM Layer",
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.TX_LAT,
                "TX Latitude",
                type=QgsProcessingParameterNumber.Double,
                minValue=-90.0,
                maxValue=90.0,
                defaultValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.TX_LON,
                "TX Longitude",
                type=QgsProcessingParameterNumber.Double,
                minValue=-180.0,
                maxValue=180.0,
                defaultValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.TX_HEIGHT,
                "TX Height (m)",
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                maxValue=10000.0,
                defaultValue=30.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.FREQ_MHZ,
                "Frequency (MHz)",
                type=QgsProcessingParameterNumber.Double,
                minValue=1.0,
                maxValue=3000.0,
                defaultValue=433.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.ERP_WATTS,
                "ERP (Watts)",
                type=QgsProcessingParameterNumber.Double,
                minValue=0.001,
                maxValue=1000000.0,
                defaultValue=10.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.MODEL,
                "Propagation Model",
                options=self._MODELS,
                defaultValue=0,  # LOS
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESOLUTION,
                "Resolution (m)",
                options=self._RESOLUTIONS,
                defaultValue=2,  # "10"
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_RANGE,
                "Max Range (km)",
                type=QgsProcessingParameterNumber.Integer,
                minValue=1,
                maxValue=500,
                defaultValue=50,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.BACKEND,
                "Compute Backend",
                options=self._BACKENDS,
                defaultValue=0,  # AUTO
            )
        )

        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_DIR,
                "Output Directory",
            )
        )

    # -- Execution ---------------------------------------------------------

    def processAlgorithm(
        self,
        parameters: Dict[str, Any],
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> Dict[str, Any]:
        # ---- Read parameters ----
        dem_layer = self.parameterAsRasterLayer(parameters, self.INPUT_DEM, context)
        tx_lat = self.parameterAsDouble(parameters, self.TX_LAT, context)
        tx_lon = self.parameterAsDouble(parameters, self.TX_LON, context)
        tx_height = self.parameterAsDouble(parameters, self.TX_HEIGHT, context)
        freq_mhz = self.parameterAsDouble(parameters, self.FREQ_MHZ, context)
        erp_watts = self.parameterAsDouble(parameters, self.ERP_WATTS, context)
        model_idx = self.parameterAsEnum(parameters, self.MODEL, context)
        resolution_idx = self.parameterAsEnum(parameters, self.RESOLUTION, context)
        max_range = self.parameterAsInt(parameters, self.MAX_RANGE, context)
        backend_idx = self.parameterAsEnum(parameters, self.BACKEND, context)
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)

        model = self._MODELS[model_idx]
        resolution_m = int(self._RESOLUTIONS[resolution_idx])
        backend = self._BACKENDS[backend_idx]

        # ---- 1. Validate API key ----
        feedback.setProgressText("Validating API key...")
        feedback.setProgress(0)

        try:
            check_key_or_raise()
        except ApiKeyError as exc:
            raise QgsProcessingException(
                f"API Key Error: {exc}"
            ) from exc

        if feedback.isCanceled():
            return {}

        # ---- 2. Prepare terrain ----
        feedback.setProgressText("Preparing terrain tiles...")
        feedback.setProgress(5)

        try:
            abt_dir = prepare_terrain(
                dem_layer=dem_layer,
                tx_lat=tx_lat,
                tx_lon=tx_lon,
                max_range_km=max_range,
                resolution_m=resolution_m,
                binary_manager=bm,
                feedback=feedback,
            )
        except Exception as exc:
            raise QgsProcessingException(
                f"Terrain preparation failed: {exc}"
            ) from exc

        if feedback.isCanceled():
            return {}

        # ---- 3. Build job config ----
        feedback.setProgressText("Building job configuration...")
        feedback.setProgress(15)

        output_name = "coverage"
        os.makedirs(output_dir, exist_ok=True)

        params = CoverageParams(
            tx_lat=tx_lat,
            tx_lon=tx_lon,
            tx_height=tx_height,
            freq_mhz=freq_mhz,
            erp_watts=erp_watts,
            model=model,
            resolution_m=resolution_m,
            max_range_km=max_range,
            backend=backend,
            output_name=output_name,
        )

        job_config = build_coverage_job(params, abt_dir, output_dir)
        job_file = write_job_file(job_config, output_dir, output_name)

        if feedback.isCanceled():
            return {}

        # ---- 4. Run aether_core ----
        feedback.setProgressText("Running aether_core...")
        feedback.setProgress(20)

        try:
            core_exe = find_binary("aether_core")
        except RuntimeError as exc:
            raise QgsProcessingException(str(exc)) from exc

        proc = subprocess.Popen(
            [core_exe, "--config", job_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Parse stderr for progress (lines like "Wedge 120/360")
        for line in iter(proc.stderr.readline, ""):
            if feedback.isCanceled():
                proc.terminate()
                return {}
            m = re.search(r"Wedge\s+(\d+)/(\d+)", line)
            if m:
                current, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    # Map wedge progress into 20-80% range
                    pct = 20 + int(current * 60 / total)
                    feedback.setProgress(pct)

        proc.wait()

        if proc.returncode != 0:
            stderr_output = proc.stderr.read() if proc.stderr else ""
            raise QgsProcessingException(
                f"aether_core failed (exit {proc.returncode}):\n{stderr_output}"
            )

        if feedback.isCanceled():
            return {}

        # ---- 5. Run aether_export ----
        feedback.setProgressText("Exporting to GeoTIFF...")
        feedback.setProgress(85)

        try:
            export_exe = find_binary("aether_export")
        except RuntimeError as exc:
            raise QgsProcessingException(str(exc)) from exc

        # Locate the .bit/.tiles output and .json sidecar
        bit_file = os.path.join(output_dir, output_name + ".bit")
        tiles_file = os.path.join(output_dir, output_name + ".tiles")
        input_file = bit_file if os.path.isfile(bit_file) else tiles_file

        json_sidecar = os.path.join(output_dir, output_name + ".json")
        tif_path = os.path.join(output_dir, output_name + ".tif")

        # IMPORTANT: use -i/-j/-o flags, NOT positional args
        result = subprocess.run(
            [export_exe, "-i", input_file, "-j", json_sidecar, "-o", tif_path],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise QgsProcessingException(
                f"aether_export failed (exit {result.returncode}):\n{result.stderr}"
            )

        # ---- 6. Load result ----
        feedback.setProgressText("Loading result...")
        feedback.setProgress(95)

        try:
            layer = load_coverage_result(tif_path, model)
            add_layer_to_project(layer)
        except Exception as exc:
            # Non-fatal — the output file still exists
            feedback.reportError(f"Could not add result to map: {exc}")

        feedback.setProgress(100)

        return {self.OUTPUT_DIR: output_dir}
