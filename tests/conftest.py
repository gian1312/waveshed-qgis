"""Pytest conftest — bootstrap QGIS/PyQt stubs so plugin code can be imported
without a running QGIS instance.

Follows the MPT_SIGMA pattern: install mock modules into sys.modules before
any test file imports plugin code.  This file is loaded automatically by
pytest before any test collection happens.
"""

import os
import sys
from types import ModuleType
from unittest import mock

# ---------------------------------------------------------------------------
# 1. Create the mock module tree
# ---------------------------------------------------------------------------

_qgis = ModuleType("qgis")
_core = ModuleType("qgis.core")
_gui = ModuleType("qgis.gui")
_pyqt = ModuleType("qgis.PyQt")
_qtcore = ModuleType("qgis.PyQt.QtCore")
_qtwidgets = ModuleType("qgis.PyQt.QtWidgets")
_qtgui = ModuleType("qgis.PyQt.QtGui")

_qgis.core = _core
_qgis.gui = _gui
_qgis.PyQt = _pyqt
_pyqt.QtCore = _qtcore
_pyqt.QtWidgets = _qtwidgets
_pyqt.QtGui = _qtgui


# ---------------------------------------------------------------------------
# 2. Fake pyqtSignal that works without Qt runtime
# ---------------------------------------------------------------------------

class _BoundSignal:
    """Instance-level signal that stores slots and calls them on emit."""

    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def disconnect(self, fn=None):
        if fn is None:
            self._slots.clear()
        else:
            self._slots = [s for s in self._slots if s is not fn]

    def emit(self, *args):
        for fn in self._slots:
            fn(*args)


class _MockSignalDescriptor:
    """Descriptor replacing pyqtSignal — each instance gets its own _BoundSignal."""

    _counter = 0

    def __init__(self, *args):
        _MockSignalDescriptor._counter += 1
        self._name = f"_sig_{_MockSignalDescriptor._counter}"

    def __set_name__(self, owner, name):
        self._name = f"_sig_{name}"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if not hasattr(obj, self._name):
            setattr(obj, self._name, _BoundSignal())
        return getattr(obj, self._name)


# ---------------------------------------------------------------------------
# 2b. Scoped enum stubs
# ---------------------------------------------------------------------------
#
# Qt6 / PyQt6 (QGIS 4) removed the unscoped spelling of every enum member: the
# bare ``Checked`` on ``Qt`` is gone, only ``Qt.CheckState.Checked`` resolves.
# (Spelling the dead form out here would trip the static guard.)  The stubs
# below therefore expose the **scoped** name only.  That is deliberate: an
# unscoped member left anywhere in the plugin raises AttributeError here and
# fails the test that touches it, instead of passing under Qt5 and breaking on
# a user's QGIS 4.  ``tests/test_qt6_compat.py`` is the static twin of this
# rule and catches the lines no test happens to execute.

def _enum_scope(name: str, **members: int) -> type:
    """Build a stand-in for a scoped Qt/QGIS enum (``Qt.CheckState`` …)."""
    return type(name, (), dict(members))


# ---------------------------------------------------------------------------
# 3. Lightweight Qt widget stubs
# ---------------------------------------------------------------------------

class _MockQWidget:
    def __init__(self, *args, **kwargs):
        pass

    def setToolTip(self, *a): pass
    def setVisible(self, *a): pass
    def setEnabled(self, *a): pass
    def setMinimumWidth(self, *a): pass
    def setMinimumHeight(self, *a): pass
    def setMinimumSize(self, *a): pass
    def setMaximumWidth(self, *a): pass
    def setWindowTitle(self, *a): pass
    def setWindowFlags(self, *a): pass
    def windowFlags(self): return 0
    def setAttribute(self, *a): pass
    def show(self): pass
    def hide(self): pass
    def close(self): pass
    def raise_(self): pass
    def activateWindow(self): pass
    def showMinimized(self): pass
    def showNormal(self): pass
    def isVisible(self): return False


class _MockQDialog(_MockQWidget):
    class DialogCode:
        Accepted = 1
        Rejected = 0


class _MockQThread:
    def __init__(self, *args, **kwargs):
        pass
    def start(self): pass
    def wait(self, *a): return True
    def isRunning(self): return False


class _MockQSettings:
    _store: dict = {}
    def __init__(self, *args, **kwargs):
        pass
    def value(self, key, default=None, type=None):
        return self._store.get(key, default)
    def setValue(self, key, value):
        self._store[key] = value


# ---------------------------------------------------------------------------
# 4. Fake Qgis enums
# ---------------------------------------------------------------------------

class _FakeQgis:
    class MessageLevel:
        Info = 0
        Warning = 1
        Critical = 2
        Success = 3

    class DataType:
        Byte = 0
        UInt16 = 1
        Int16 = 2
        Float32 = 6
        # What every rendered-image provider (WMS/WMTS/XYZ) reports. Values
        # match QGIS so a test reading them is reading the real thing.
        ARGB32 = 12
        ARGB32_Premultiplied = 13


class _FakeMessageLog:
    @staticmethod
    def logMessage(*a, **kw):
        pass


# ---------------------------------------------------------------------------
# 5. Fake geometry types
# ---------------------------------------------------------------------------

class _FakeWkbTypes:
    class GeometryType:
        PointGeometry = 0
        LineGeometry = 1
        PolygonGeometry = 2

    @staticmethod
    def hasZ(wkb_type):
        return False


class _FakeCRS:
    def __init__(self, *a):
        self._id = a[0] if a else ""
    def isValid(self):
        return bool(self._id)
    def isGeographic(self):
        return "4326" in str(self._id)
    def authid(self):
        return self._id
    def __eq__(self, other):
        return isinstance(other, _FakeCRS) and self._id == other._id
    def __ne__(self, other):
        return not self.__eq__(other)


# ---------------------------------------------------------------------------
# 6. Install everything into sys.modules
# ---------------------------------------------------------------------------

# Core classes
_core.Qgis = _FakeQgis
_core.QgsMessageLog = _FakeMessageLog
_core.QgsSettings = _MockQSettings
_core.QgsCoordinateReferenceSystem = _FakeCRS
_core.QgsCoordinateTransform = mock.MagicMock()
_core.QgsMapLayerProxyModel = type("Proxy", (), {
    "Filter": _enum_scope("Filter", RasterLayer=1, VectorLayer=2),
})
_core.QgsPointXY = type("PT", (), {"__init__": lambda self, *a: None, "x": lambda self: 0, "y": lambda self: 0})
_core.QgsProject = type("Proj", (), {"instance": staticmethod(lambda: mock.MagicMock())})
_core.QgsRasterLayer = type("RL", (), {"__init__": lambda self, *a, **kw: None})
_core.QgsRectangle = type("Rect", (), {"__init__": lambda self, *a: None})
_core.QgsVectorLayer = type("VL", (), {"__init__": lambda self, *a, **kw: None})
_core.QgsWkbTypes = _FakeWkbTypes
_core.QgsGeometry = mock.MagicMock()

# Raster styling classes — enough for core.result_loader to import.
_core.QgsColorRampShader = type("QgsColorRampShader", (), {
    "__init__": lambda self, *a, **kw: None,
    "Type": _enum_scope("Type", Interpolated=0, Discrete=1, Exact=2),
    "ColorRampItem": type("ColorRampItem", (), {"__init__": lambda self, *a: None}),
})
_core.QgsRasterShader = type("QgsRasterShader", (), {"__init__": lambda self, *a, **kw: None})
# Stats is an IntFlag in QGIS; result_loader ORs Min with Max.
_core.QgsRasterBandStats = type("QgsRasterBandStats", (), {
    "Stats": _enum_scope("Stats", Min=1, Max=2),
})
_core.QgsSingleBandPseudoColorRenderer = type(
    "QgsSingleBandPseudoColorRenderer", (), {"__init__": lambda self, *a, **kw: None},
)
_core.QgsPalettedRasterRenderer = type("QgsPalettedRasterRenderer", (), {
    "__init__": lambda self, *a, **kw: None,
    "Class": type("Class", (), {"__init__": lambda self, *a: None}),
})

# Processing framework — enough for the algorithms/ modules to import and for
# their pure-Python helpers (CSV parsing, extent maths) to be unit-tested. The
# parameter classes are inert: they record nothing and enforce no range, so a
# test must never assert on ``minValue`` behaviour through them — assert on the
# core-layer rejection instead.
_core.QgsProcessing = type("QgsProcessing", (), {
    "TypeRaster": 3, "TypeVectorAnyGeometry": -1,
})
_core.QgsProcessingException = type("QgsProcessingException", (Exception,), {})
_core.QgsProcessingAlgorithm = type("QgsProcessingAlgorithm", (), {
    "__init__": lambda self, *a, **kw: None,
    "addParameter": lambda self, *a, **kw: None,
})
_core.QgsProcessingContext = type("QgsProcessingContext", (), {
    "__init__": lambda self, *a, **kw: None,
})
_core.QgsProcessingFeedback = type("QgsProcessingFeedback", (), {
    "__init__": lambda self, *a, **kw: None,
})
_core.QgsProcessingProvider = type("QgsProcessingProvider", (), {
    "__init__": lambda self, *a, **kw: None,
})
for _param_name in (
    "QgsProcessingParameterEnum",
    "QgsProcessingParameterFile",
    "QgsProcessingParameterFolderDestination",
    "QgsProcessingParameterNumber",
    "QgsProcessingParameterRasterLayer",
):
    setattr(_core, _param_name, type(_param_name, (), {
        "__init__": lambda self, *a, **kw: None,
        "Type": _enum_scope("Type", Integer=0, Double=1),
        "Behavior": _enum_scope("Behavior", File=0, Folder=1),
    }))

# GUI classes
_gui.QgsMapLayerComboBox = type("MLCB", (), {"__init__": lambda self, *a, **kw: None})
_gui.QgsRubberBand = type("RB", (), {"__init__": lambda self, *a, **kw: None, "reset": lambda self, *a: None, "addPoint": lambda self, *a: None, "setColor": lambda self, *a: None, "setWidth": lambda self, *a: None})
_gui.QgsMapToolEmitPoint = type("MTEP", (), {"__init__": lambda self, *a: None})

# Qt Core — scoped exactly as PyQt6 spells them (see section 2b).
_qtcore.Qt = type("Qt", (), {
    "CheckState": _enum_scope(
        "CheckState", Unchecked=0, PartiallyChecked=1, Checked=2,
    ),
    "CursorShape": _enum_scope("CursorShape", CrossCursor=2, WaitCursor=3),
    "DockWidgetArea": _enum_scope(
        "DockWidgetArea", LeftDockWidgetArea=1, RightDockWidgetArea=2,
    ),
    "ItemDataRole": _enum_scope("ItemDataRole", DisplayRole=0, UserRole=0x0100),
    "ItemFlag": _enum_scope(
        "ItemFlag", ItemIsEditable=2, ItemIsEnabled=32, ItemIsUserCheckable=16,
    ),
    "Key": _enum_scope("Key", Key_Escape=0x01000000),
    "MouseButton": _enum_scope("MouseButton", LeftButton=1, RightButton=2),
    "Orientation": _enum_scope("Orientation", Horizontal=1, Vertical=2),
    "PenStyle": _enum_scope("PenStyle", SolidLine=1, DashLine=2),
    "ScrollBarPolicy": _enum_scope(
        "ScrollBarPolicy",
        ScrollBarAsNeeded=0, ScrollBarAlwaysOff=1, ScrollBarAlwaysOn=2,
    ),
    "TextFormat": _enum_scope("TextFormat", PlainText=0, RichText=1),
    "WidgetAttribute": _enum_scope("WidgetAttribute", WA_DeleteOnClose=55),
    "WindowModality": _enum_scope(
        "WindowModality", NonModal=0, WindowModal=1, ApplicationModal=2,
    ),
    "WindowType": _enum_scope("WindowType", Widget=0, Window=1, Dialog=3),
})
_qtcore.QThread = _MockQThread
_qtcore.pyqtSignal = _MockSignalDescriptor
_qtcore.QTimer = mock.MagicMock()
_qtcore.QUrl = type("QUrl", (), {"__init__": lambda self, *a, **kw: None})
_qtcore.QByteArray = type("QByteArray", (), {"__init__": lambda self, *a, **kw: None})

# Qt Widgets — create lightweight stubs for all used widgets
_widget_names = [
    "QAbstractItemView", "QButtonGroup", "QCheckBox", "QComboBox",
    "QDialog", "QDoubleSpinBox", "QFileDialog", "QFormLayout",
    "QGroupBox", "QHBoxLayout", "QHeaderView", "QLabel", "QLineEdit",
    "QMessageBox", "QPlainTextEdit", "QProgressBar", "QPushButton",
    "QRadioButton", "QSizePolicy", "QSpinBox", "QSplitter",
    "QTableWidget", "QTableWidgetItem", "QTabWidget", "QToolButton",
    "QVBoxLayout", "QWidget", "QInputDialog", "QDialogButtonBox",
    "QTextEdit", "QProgressDialog", "QApplication", "QListWidget",
    "QListWidgetItem", "QScrollArea", "QStackedWidget", "QSlider",
]
for name in _widget_names:
    if name in ("QWidget",):
        setattr(_qtwidgets, name, _MockQWidget)
    elif name in ("QDialog",):
        setattr(_qtwidgets, name, _MockQDialog)
    elif name == "QMessageBox":
        mb = type("QMessageBox", (), {
            "__init__": lambda self, *a, **kw: None,
            "StandardButton": _enum_scope(
                "StandardButton",
                Ok=0x400, Save=0x800, Discard=0x800000,
                Cancel=0x400000, Yes=0x4000, No=0x10000,
            ),
            "ButtonRole": _enum_scope(
                "ButtonRole", AcceptRole=0, RejectRole=1, ActionRole=3,
            ),
            "Icon": _enum_scope(
                "Icon", NoIcon=0, Information=1, Warning=2, Critical=3, Question=4,
            ),
            "warning": staticmethod(lambda *a, **kw: 0x4000),
            "critical": staticmethod(lambda *a, **kw: 0x400),
            "information": staticmethod(lambda *a, **kw: 0x400),
            "question": staticmethod(lambda *a, **kw: 0x4000),
        })
        setattr(_qtwidgets, name, mb)
    else:
        setattr(_qtwidgets, name, type(name, (), {"__init__": lambda self, *a, **kw: None}))

# Extra widget attributes some dialogs reference at call time.
_qtwidgets.QInputDialog.getText = staticmethod(lambda *a, **kw: ("", False))
_qtwidgets.QLineEdit.EchoMode = _enum_scope("EchoMode", Normal=0, Password=2)
_qtwidgets.QAbstractItemView.SelectionBehavior = _enum_scope(
    "SelectionBehavior", SelectItems=0, SelectRows=1, SelectColumns=2,
)
_qtwidgets.QAbstractItemView.SelectionMode = _enum_scope(
    "SelectionMode", NoSelection=0, SingleSelection=1, ExtendedSelection=3,
)
_qtwidgets.QTableWidget.SelectionBehavior = _qtwidgets.QAbstractItemView.SelectionBehavior
_qtwidgets.QTableWidget.SelectionMode = _qtwidgets.QAbstractItemView.SelectionMode
_qtwidgets.QListWidget.SelectionMode = _qtwidgets.QAbstractItemView.SelectionMode
_qtwidgets.QListWidget.Flow = _enum_scope("Flow", LeftToRight=0, TopToBottom=1)
_qtwidgets.QHeaderView.ResizeMode = _enum_scope(
    "ResizeMode", Interactive=0, Stretch=1, Fixed=2, ResizeToContents=3,
)
_qtwidgets.QScrollArea.Shape = _enum_scope("Shape", NoFrame=0, Box=1, Panel=2)
_qtwidgets.QDialogButtonBox.StandardButton = _enum_scope(
    "StandardButton", Ok=0x400, Cancel=0x400000, Close=0x200000,
)
_qtwidgets.QDialogButtonBox.ButtonRole = _enum_scope(
    "ButtonRole", AcceptRole=0, RejectRole=1, ActionRole=3,
)
_qtwidgets.QFileDialog.Option = _enum_scope("Option", ShowDirsOnly=1)

# Qt GUI
_qtgui.QColor = type("QColor", (), {"__init__": lambda self, *a: None})
_qtgui.QIcon = type("QIcon", (), {"__init__": lambda self, *a: None})
_qtgui.QDesktopServices = type(
    "QDesktopServices", (), {"openUrl": staticmethod(lambda *a, **kw: True)}
)

# Install module tree
for mod_name, mod_obj in [
    ("qgis", _qgis),
    ("qgis.core", _core),
    ("qgis.gui", _gui),
    ("qgis.PyQt", _pyqt),
    ("qgis.PyQt.QtCore", _qtcore),
    ("qgis.PyQt.QtWidgets", _qtwidgets),
    ("qgis.PyQt.QtGui", _qtgui),
]:
    sys.modules.setdefault(mod_name, mod_obj)

# Stub osgeo.gdal
_osgeo = ModuleType("osgeo")
_gdal = ModuleType("osgeo.gdal")
_gdal.GRA_Bilinear = 1
_gdal.GRA_Average = 5
_gdal.Warp = mock.MagicMock(return_value=None)
_osgeo.gdal = _gdal
sys.modules.setdefault("osgeo", _osgeo)
sys.modules.setdefault("osgeo.gdal", _gdal)

# Ensure plugin root is on sys.path
_plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _plugin_root not in sys.path:
    sys.path.insert(0, _plugin_root)
