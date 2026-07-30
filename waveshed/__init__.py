"""Waveshed QGIS Plugin — GPU-accelerated RF propagation analysis."""


def classFactory(iface):
    """QGIS plugin entry point. Called by QGIS on plugin load."""
    from .plugin import AetherPlugin
    return AetherPlugin(iface)
