# Container for tools/release_check.py — the pre-upload check (tests + the
# plugins.qgis.org scans) without installing anything on the host.
#
# Debian bookworm's PyQGIS 3.22 / GDAL 3.6 / numpy 1.24 is the stack the suite
# is verified on in the dev container. numpy, GDAL and PyQGIS come from apt
# ONLY: pip's numpy 2.x breaks the distro osgeo.gdal_array (_ARRAY_API not
# found), so the venv sees them via --system-site-packages and pip adds just
# pytest and the scan analysers.
#
# Built and run by the Waveshed pipeline GUI (Build tab → QGIS plugin →
# Run checks), or by hand from the repo root:
#
#   docker build -t waveshed-plugin-check tools/check_container
#   docker run --rm -v "$PWD:/src" -v "<AETHER>/bin/linux:/aether-bin:ro" \
#       waveshed-plugin-check --bin-dir /aether-bin
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3-qgis python3-gdal python3-numpy python3-matplotlib python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /opt/check \
 && /opt/check/bin/pip install --no-cache-dir \
        pytest==9.1.1 bandit==1.9.4 detect-secrets==1.5.0 flake8==7.4.1

# The repo is bind-mounted from the host: no bytecode or caches written into it.
ENV QT_QPA_PLATFORM=offscreen \
    PYTHONDONTWRITEBYTECODE=1 \
    WAVESHED_SCAN_TOOLS=/opt/check/bin

WORKDIR /src
ENTRYPOINT ["/opt/check/bin/python", "tools/release_check.py"]
