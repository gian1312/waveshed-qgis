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


class _FakeMessageLog:
    @staticmethod
    def logMessage(*a, **kw):
        pass


# ---------------------------------------------------------------------------
# 5. Fake geometry types
# ---------------------------------------------------------------------------

class _FakeWkbTypes:
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
_core.QgsMapLayerProxyModel = type("Proxy", (), {"RasterLayer": 1, "VectorLayer": 2})
_core.QgsPointXY = type("PT", (), {"__init__": lambda self, *a: None, "x": lambda self: 0, "y": lambda self: 0})
_core.QgsProject = type("Proj", (), {"instance": staticmethod(lambda: mock.MagicMock())})
_core.QgsRasterLayer = type("RL", (), {"__init__": lambda self, *a, **kw: None})
_core.QgsRectangle = type("Rect", (), {"__init__": lambda self, *a: None})
_core.QgsVectorLayer = type("VL", (), {"__init__": lambda self, *a, **kw: None})
_core.QgsWkbTypes = _FakeWkbTypes
_core.QgsGeometry = mock.MagicMock()

# GUI classes
_gui.QgsMapLayerComboBox = type("MLCB", (), {"__init__": lambda self, *a, **kw: None})
_gui.QgsRubberBand = type("RB", (), {"__init__": lambda self, *a, **kw: None, "reset": lambda self, *a: None, "addPoint": lambda self, *a: None, "setColor": lambda self, *a: None, "setWidth": lambda self, *a: None})
_gui.QgsMapToolEmitPoint = type("MTEP", (), {"__init__": lambda self, *a: None})

# Qt Core
_qtcore.Qt = type("Qt", (), {
    "ItemIsEditable": 2, "CrossCursor": 24, "LeftButton": 1,
    "Key_Escape": 0x01000000, "WA_DeleteOnClose": 55, "Window": 1,
    "Widget": 0,
})
_qtcore.QThread = _MockQThread
_qtcore.pyqtSignal = _MockSignalDescriptor
_qtcore.QTimer = mock.MagicMock()
_qtcore.QUrl = type("QUrl", (), {"__init__": lambda self, *a, **kw: None})

# Qt Widgets — create lightweight stubs for all used widgets
_widget_names = [
    "QAbstractItemView", "QButtonGroup", "QCheckBox", "QComboBox",
    "QDialog", "QDoubleSpinBox", "QFileDialog", "QFormLayout",
    "QGroupBox", "QHBoxLayout", "QHeaderView", "QLabel", "QLineEdit",
    "QMessageBox", "QPlainTextEdit", "QProgressBar", "QPushButton",
    "QRadioButton", "QSizePolicy", "QSpinBox", "QSplitter",
    "QTableWidget", "QTableWidgetItem", "QTabWidget", "QToolButton",
    "QVBoxLayout", "QWidget", "QInputDialog", "QDialogButtonBox",
]
for name in _widget_names:
    if name in ("QWidget",):
        setattr(_qtwidgets, name, _MockQWidget)
    elif name in ("QDialog",):
        setattr(_qtwidgets, name, _MockQDialog)
    elif name == "QMessageBox":
        mb = type("QMessageBox", (), {
            "__init__": lambda self, *a, **kw: None,
            "Yes": 0x4000, "No": 0x10000, "Ok": 0x400,
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
_qtwidgets.QLineEdit.Normal = 0
_qtwidgets.QLineEdit.Password = 2

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
_gdal.Warp = mock.MagicMock(return_value=None)
_osgeo.gdal = _gdal
sys.modules.setdefault("osgeo", _osgeo)
sys.modules.setdefault("osgeo.gdal", _gdal)

# Ensure plugin root is on sys.path
_plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _plugin_root not in sys.path:
    sys.path.insert(0, _plugin_root)
