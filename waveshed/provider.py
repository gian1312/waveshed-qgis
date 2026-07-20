"""QgsProcessingProvider — registers AETHER algorithms in the Processing toolbox."""

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
import os


class AetherProvider(QgsProcessingProvider):

    def id(self):
        return "waveshed"

    def name(self):
        return "Waveshed"

    def longName(self):
        return "Waveshed RF Propagation"

    def icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), "resources", "icon.png")
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return super().icon()

    def loadAlgorithms(self):
        from .algorithms.coverage import CoverageAlgorithm
        from .algorithms.p2p import P2PAlgorithm
        self.addAlgorithm(CoverageAlgorithm())
        self.addAlgorithm(P2PAlgorithm())
