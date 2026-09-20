"""Static guard keeping the plugin importable on both Qt5 and Qt6.

QGIS 4 ships PyQt6, where every enum member is *scoped*: ``Qt.Checked`` is
gone and only ``Qt.CheckState.Checked`` resolves, ``exec_()`` is ``exec()``,
``QRegExp``/``QVariant``/``QDesktopWidget`` no longer exist, and ``QAction``
moved from ``QtWidgets`` to ``QtGui``.  All of the replacements also work on
PyQt5 >= 5.11 (QGIS 3.28 ships far newer), so the plugin can speak the Qt6
dialect everywhere and stay one codebase.

The trap is that Qt5 still accepts the old spelling, so an unscoped enum that
slips back in is invisible until a QGIS 4 user hits that exact line.  This is a
pure text scan over every source file — no Qt import, no widget construction —
so it covers the lines no test happens to execute.

Keeping it green
----------------
Adding a Qt or QGIS enum?  Write it scoped.  If a *new* unscoped member shows
up that this file does not know about yet, add it to the table below: the list
is a denylist, so an unlisted member is silently allowed and the guard is only
as good as the table.  ``tests/conftest.py`` is the runtime twin — its stubs
expose the scoped spelling only, so an unscoped member also breaks any test
that executes it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Sequence, Tuple

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
#: ``tools/`` is in scope because the torture runner and the project builder
#: execute inside QGIS too, so they meet PyQt6 exactly as the plugin does.
SCAN_DIRS = ("waveshed", "tests", "tools")

#: This file necessarily spells the forbidden names out, so it cannot scan
#: itself.
SELF_NAME = Path(__file__).name


# ---------------------------------------------------------------------------
# What a Qt6-ready file may not contain
# ---------------------------------------------------------------------------

#: Unscoped members of the ``Qt`` namespace, by the scope they belong to.
#: Only the member names matter to the scan; the scope is there so the failure
#: message can name the replacement.
QT_MEMBERS: Dict[str, Sequence[str]] = {
    "AlignmentFlag": (
        "AlignLeft", "AlignRight", "AlignHCenter", "AlignJustify", "AlignTop",
        "AlignBottom", "AlignVCenter", "AlignCenter", "AlignAbsolute",
    ),
    "CheckState": ("Unchecked", "PartiallyChecked", "Checked"),
    "ConnectionType": (
        "AutoConnection", "DirectConnection", "QueuedConnection",
        "BlockingQueuedConnection", "UniqueConnection",
    ),
    "ContextMenuPolicy": (
        "NoContextMenu", "DefaultContextMenu", "ActionsContextMenu",
        "CustomContextMenu", "PreventContextMenu",
    ),
    "CursorShape": (
        "ArrowCursor", "BusyCursor", "CrossCursor", "PointingHandCursor",
        "WaitCursor", "IBeamCursor", "SizeAllCursor", "BlankCursor",
    ),
    "DateFormat": ("ISODate", "ISODateWithMs", "TextDate", "RFC2822Date"),
    "DockWidgetArea": (
        "LeftDockWidgetArea", "RightDockWidgetArea", "TopDockWidgetArea",
        "BottomDockWidgetArea", "AllDockWidgetAreas", "NoDockWidgetArea",
    ),
    "FocusPolicy": ("NoFocus", "TabFocus", "ClickFocus", "StrongFocus", "WheelFocus"),
    "GlobalColor": (
        "white", "black", "red", "darkRed", "green", "darkGreen", "blue",
        "darkBlue", "cyan", "darkCyan", "magenta", "darkMagenta", "yellow",
        "darkYellow", "gray", "darkGray", "lightGray", "transparent",
    ),
    "ItemDataRole": (
        "DisplayRole", "DecorationRole", "EditRole", "ToolTipRole",
        "StatusTipRole", "WhatsThisRole", "UserRole", "CheckStateRole",
        "TextAlignmentRole", "ForegroundRole", "BackgroundRole",
    ),
    "ItemFlag": (
        "NoItemFlags", "ItemIsSelectable", "ItemIsEditable", "ItemIsDragEnabled",
        "ItemIsDropEnabled", "ItemIsUserCheckable", "ItemIsEnabled",
        "ItemIsAutoTristate", "ItemNeverHasChildren",
    ),
    "Key": (
        "Key_Escape", "Key_Tab", "Key_Backspace", "Key_Return", "Key_Enter",
        "Key_Delete", "Key_Space", "Key_Left", "Key_Right", "Key_Up", "Key_Down",
    ),
    "KeyboardModifier": (
        "NoModifier", "ShiftModifier", "ControlModifier", "AltModifier",
        "MetaModifier", "KeypadModifier",
    ),
    "MatchFlag": (
        "MatchExactly", "MatchContains", "MatchStartsWith", "MatchEndsWith",
        "MatchRegularExpression", "MatchWildcard", "MatchFixedString",
        "MatchCaseSensitive", "MatchWrap", "MatchRecursive",
    ),
    "MouseButton": (
        "NoButton", "LeftButton", "RightButton", "MiddleButton", "MidButton",
    ),
    "Orientation": ("Horizontal", "Vertical"),
    "PenStyle": (
        "NoPen", "SolidLine", "DashLine", "DotLine", "DashDotLine",
        "DashDotDotLine", "CustomDashLine",
    ),
    "ScrollBarPolicy": (
        "ScrollBarAsNeeded", "ScrollBarAlwaysOff", "ScrollBarAlwaysOn",
    ),
    "SortOrder": ("AscendingOrder", "DescendingOrder"),
    "TextElideMode": ("ElideLeft", "ElideRight", "ElideMiddle", "ElideNone"),
    "TextFormat": ("PlainText", "RichText", "AutoText", "MarkdownText"),
    "TextInteractionFlag": (
        "NoTextInteraction", "TextSelectableByMouse", "TextSelectableByKeyboard",
        "LinksAccessibleByMouse", "LinksAccessibleByKeyboard",
        "TextBrowserInteraction", "TextEditorInteraction",
    ),
    "ToolButtonStyle": (
        "ToolButtonIconOnly", "ToolButtonTextOnly", "ToolButtonTextBesideIcon",
        "ToolButtonTextUnderIcon", "ToolButtonFollowStyle",
    ),
    "TransformationMode": ("FastTransformation", "SmoothTransformation"),
    "AspectRatioMode": (
        "IgnoreAspectRatio", "KeepAspectRatio", "KeepAspectRatioByExpanding",
    ),
    "CaseSensitivity": ("CaseInsensitive", "CaseSensitive"),
    "WidgetAttribute": (
        "WA_DeleteOnClose", "WA_TranslucentBackground", "WA_StyledBackground",
        "WA_TransparentForMouseEvents", "WA_OpaquePaintEvent",
    ),
    "WindowModality": ("NonModal", "WindowModal", "ApplicationModal"),
    "WindowState": (
        "WindowNoState", "WindowMinimized", "WindowMaximized",
        "WindowFullScreen", "WindowActive",
    ),
    "WindowType": (
        "Widget", "Window", "Dialog", "Sheet", "Drawer", "Popup", "Tool",
        "ToolTip", "SplashScreen", "SubWindow", "FramelessWindowHint",
        "CustomizeWindowHint", "WindowTitleHint", "WindowSystemMenuHint",
        "WindowMinimizeButtonHint", "WindowMaximizeButtonHint",
        "WindowCloseButtonHint", "WindowStaysOnTopHint",
    ),
}

#: Unscoped members of widget/GUI/QGIS classes, as {class: {scope: members}}.
CLASS_MEMBERS: Dict[str, Dict[str, Sequence[str]]] = {
    "QMessageBox": {
        "StandardButton": (
            "Ok", "Open", "Save", "Cancel", "Close", "Discard", "Apply",
            "Reset", "Yes", "YesToAll", "No", "NoToAll", "Abort", "Retry",
            "Ignore", "NoButton",
        ),
        "ButtonRole": (
            "InvalidRole", "AcceptRole", "RejectRole", "DestructiveRole",
            "ActionRole", "HelpRole", "YesRole", "NoRole", "ResetRole",
            "ApplyRole",
        ),
        "Icon": ("NoIcon", "Information", "Question"),
    },
    "QDialogButtonBox": {
        "StandardButton": (
            "Ok", "Open", "Save", "Cancel", "Close", "Discard", "Apply",
            "Reset", "Yes", "YesToAll", "No", "NoToAll", "Abort", "Retry",
            "Ignore", "NoButton", "RestoreDefaults", "Help",
        ),
        "ButtonRole": (
            "InvalidRole", "AcceptRole", "RejectRole", "DestructiveRole",
            "ActionRole", "HelpRole", "YesRole", "NoRole", "ResetRole",
            "ApplyRole",
        ),
    },
    "QDialog": {"DialogCode": ("Accepted", "Rejected")},
    "QFileDialog": {
        "Option": (
            "ShowDirsOnly", "DontResolveSymlinks", "DontConfirmOverwrite",
            "DontUseNativeDialog", "ReadOnly", "HideNameFilterDetails",
        ),
        "FileMode": ("AnyFile", "ExistingFile", "Directory", "ExistingFiles"),
        "AcceptMode": ("AcceptOpen", "AcceptSave"),
    },
    "QHeaderView": {
        "ResizeMode": (
            "Interactive", "Fixed", "Stretch", "ResizeToContents", "Custom",
        ),
    },
    "QAbstractItemView": {
        "SelectionBehavior": ("SelectItems", "SelectRows", "SelectColumns"),
        "SelectionMode": (
            "NoSelection", "SingleSelection", "MultiSelection",
            "ExtendedSelection", "ContiguousSelection",
        ),
        "EditTrigger": (
            "NoEditTriggers", "CurrentChanged", "DoubleClicked",
            "SelectedClicked", "EditKeyPressed", "AnyKeyPressed", "AllEditTriggers",
        ),
        "ScrollHint": (
            "EnsureVisible", "PositionAtTop", "PositionAtBottom", "PositionAtCenter",
        ),
    },
    "QTableWidget": {
        "SelectionBehavior": ("SelectItems", "SelectRows", "SelectColumns"),
        "SelectionMode": (
            "NoSelection", "SingleSelection", "MultiSelection",
            "ExtendedSelection", "ContiguousSelection",
        ),
    },
    "QTableView": {
        "SelectionBehavior": ("SelectItems", "SelectRows", "SelectColumns"),
    },
    "QListWidget": {
        "Flow": ("LeftToRight", "TopToBottom"),
        "ViewMode": ("ListMode", "IconMode"),
        "SelectionMode": (
            "NoSelection", "SingleSelection", "MultiSelection",
            "ExtendedSelection", "ContiguousSelection",
        ),
    },
    "QListView": {
        "Flow": ("LeftToRight", "TopToBottom"),
        "ViewMode": ("ListMode", "IconMode"),
    },
    "QScrollArea": {
        "Shape": ("NoFrame", "Box", "Panel", "WinPanel", "HLine", "VLine", "StyledPanel"),
    },
    "QFrame": {
        "Shape": ("NoFrame", "Box", "Panel", "WinPanel", "HLine", "VLine", "StyledPanel"),
        "Shadow": ("Plain", "Raised", "Sunken"),
    },
    "QLineEdit": {"EchoMode": ("Normal", "NoEcho", "Password", "PasswordEchoOnEdit")},
    "QSizePolicy": {
        "Policy": (
            "Fixed", "Minimum", "Maximum", "Preferred", "Expanding",
            "MinimumExpanding", "Ignored",
        ),
    },
    "QComboBox": {
        "InsertPolicy": ("NoInsert", "InsertAtTop", "InsertAtBottom"),
        "SizeAdjustPolicy": ("AdjustToContents", "AdjustToContentsOnFirstShow"),
    },
    "QLayout": {"SizeConstraint": ("SetDefaultConstraint", "SetFixedSize", "SetMinimumSize")},
    "QFont": {"Weight": ("Thin", "Light", "Normal", "Medium", "DemiBold", "Bold", "Black")},
    "QPainter": {
        "RenderHint": ("Antialiasing", "TextAntialiasing", "SmoothPixmapTransform"),
    },
    "QImage": {
        "Format": (
            "Format_RGB32", "Format_ARGB32", "Format_ARGB32_Premultiplied",
            "Format_Grayscale8", "Format_Invalid",
        ),
    },
    "QPalette": {
        "ColorRole": (
            "Window", "WindowText", "Base", "AlternateBase", "Text", "Button",
            "ButtonText", "Highlight", "HighlightedText",
        ),
    },
    "QKeySequence": {"StandardKey": ("Copy", "Paste", "Cut", "Delete", "Save", "Close")},
    "QSettings": {
        "Format": ("NativeFormat", "IniFormat", "InvalidFormat"),
        "Scope": ("UserScope", "SystemScope"),
    },
    "QgsSettings": {
        "Section": (
            "NoSection", "Core", "Gui", "Server", "Plugins", "Auth", "App",
            "Providers", "Expressions", "Misc", "Gps",
        ),
    },
    "QgsContrastEnhancement": {
        "ContrastEnhancementAlgorithm": (
            "NoEnhancement", "StretchToMinimumMaximum",
            "StretchAndClipToMinimumMaximum", "ClipToMinimumMaximum",
            "UserDefinedEnhancement",
        ),
    },
    # --- QGIS classes.  Their unscoped spellings survive in QGIS 4 as
    # monkey-patched aliases, but they are deprecated; the scoped form works
    # from 3.22 on, so there is no reason to keep the old one.
    "Qgis": {
        "MessageLevel": ("Info", "Warning", "Critical", "Success", "NoLevel"),
    },
    "QgsWkbTypes": {
        "GeometryType": (
            "PointGeometry", "LineGeometry", "PolygonGeometry",
            "UnknownGeometry", "NullGeometry",
        ),
        "Type": ("Point", "LineString", "Polygon", "MultiPoint", "NoGeometry"),
    },
    "QgsMapLayerProxyModel": {
        "Filter": (
            "RasterLayer", "VectorLayer", "PointLayer", "LineLayer",
            "PolygonLayer", "NoGeometry", "HasGeometry", "PluginLayer", "All",
        ),
    },
    "QgsProcessing": {
        "SourceType": (
            "TypeMapLayer", "TypeVectorAnyGeometry", "TypeVectorPoint",
            "TypeVectorLine", "TypeVectorPolygon", "TypeRaster", "TypeFile",
            "TypeVector", "TypeMesh",
        ),
    },
    "QgsProcessingParameterFile": {"Behavior": ("File", "Folder")},
    "QgsProcessingParameterNumber": {"Type": ("Integer", "Double")},
    "QgsColorRampShader": {"Type": ("Interpolated", "Discrete", "Exact")},
    "QgsRasterBandStats": {
        "Stats": ("Min", "Max", "Range", "Sum", "Mean", "StdDev", "SumOfSquares", "All"),
    },
    "QgsRasterFileWriter": {
        "WriterError": (
            "NoError", "SourceProviderError", "DestProviderError",
            "CreateDatasourceError", "WriteError", "NoDataConflict", "WriteCanceled",
        ),
    },
    "QgsBlockingNetworkRequest": {
        "ErrorCode": ("NoError", "NetworkError", "TimeoutError", "ServerExceptionError"),
    },
    "QgsFeatureRequest": {
        "Flag": (
            "NoFlags", "NoGeometry", "SubsetOfAttributes", "ExactIntersect",
            "IgnoreStaticNodesDuringExpressionCompilation",
        ),
    },
    "QgsUnitTypes": {
        "DistanceUnit": (
            "DistanceMeters", "DistanceKilometers", "DistanceFeet",
            "DistanceDegrees", "DistanceMiles", "DistanceUnknownUnit",
        ),
    },
}

#: Names Qt6 removed outright, plus the Qt5-only spellings of calls.
FORBIDDEN_TOKENS: Sequence[Tuple[str, str]] = (
    (r"\.exec_\s*\(", "exec_() is Qt5-only; use .exec() (PyQt5 >= 5.11 has it too)"),
    (r"\bQRegExp\b", "QRegExp is removed in Qt6; use QRegularExpression"),
    (r"\bQVariant\b", "QVariant is not usable in Qt6; use QMetaType.Type / plain Python values"),
    (r"\bQDesktopWidget\b", "QDesktopWidget is removed in Qt6; use QScreen / QGuiApplication"),
    (r"\bQApplication\.desktop\s*\(", "QApplication.desktop() is removed in Qt6"),
    (r"\.setResizeMode\s*\(", "QHeaderView.setResizeMode() is removed; use setSectionResizeMode()"),
    (r"^\s*(?:from|import)\s+PyQt5\b", "import PyQt5 directly is forbidden; use qgis.PyQt"),
    (r"^\s*(?:from|import)\s+PyQt6\b", "import PyQt6 directly is forbidden; use qgis.PyQt"),
    (r"\bfrom\s+PyQt5\b", "import PyQt5 directly is forbidden; use qgis.PyQt"),
    (r"\bfrom\s+PyQt6\b", "import PyQt6 directly is forbidden; use qgis.PyQt"),
)


def _build_enum_patterns() -> List[Tuple[re.Pattern, str]]:
    """One compiled pattern per (class, scope), with its replacement hint."""
    patterns: List[Tuple[re.Pattern, str]] = []
    for scope, members in QT_MEMBERS.items():
        alternatives = "|".join(sorted(members, key=len, reverse=True))
        patterns.append((
            re.compile(rf"\bQt\.({alternatives})\b"),
            f"Qt.{scope}.\\1",
        ))
    for cls, scopes in CLASS_MEMBERS.items():
        for scope, members in scopes.items():
            alternatives = "|".join(sorted(members, key=len, reverse=True))
            patterns.append((
                re.compile(rf"\b{cls}\.({alternatives})\b"),
                f"{cls}.{scope}.\\1",
            ))
    return patterns


ENUM_PATTERNS = _build_enum_patterns()
TOKEN_PATTERNS = [(re.compile(rx, re.MULTILINE), msg) for rx, msg in FORBIDDEN_TOKENS]


def iter_source_files() -> Iterator[Path]:
    """Every plugin/test source file the guard applies to."""
    for rel in SCAN_DIRS:
        for path in sorted((PLUGIN_ROOT / rel).rglob("*.py")):
            if "__pycache__" in path.parts or path.name == SELF_NAME:
                continue
            yield path


def scan_file(path: Path) -> List[str]:
    """Return one human-readable complaint per offending line in *path*."""
    problems: List[str] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rel = path.relative_to(PLUGIN_ROOT).as_posix()

    for lineno, line in enumerate(lines, start=1):
        for pattern, replacement in ENUM_PATTERNS:
            for match in pattern.finditer(line):
                problems.append(
                    f"{rel}:{lineno}: unscoped enum {match.group(0)!r} — "
                    f"PyQt6 only accepts {match.expand(replacement)!r}"
                )
        for pattern, message in TOKEN_PATTERNS:
            match = pattern.search(line)
            if match:
                problems.append(f"{rel}:{lineno}: {match.group(0)!r} — {message}")
    return problems


class TestQt6Compatibility:
    """Every source file must already speak the Qt6 dialect."""

    def test_sources_are_qt6_ready(self):
        files = list(iter_source_files())
        assert files, "found no source files to scan — the paths must be wrong"

        problems: List[str] = []
        for path in files:
            problems.extend(scan_file(path))

        assert not problems, (
            "Qt6/QGIS 4 incompatibilities found "
            f"({len(problems)} in {len(files)} scanned files):\n  "
            + "\n  ".join(problems)
            + "\n\nEvery replacement also works on the PyQt5 that QGIS 3.28+ "
              "ships, so scoping is never a compatibility trade-off."
        )

    def test_qaction_import_is_binding_agnostic(self):
        """QAction lives in QtGui on Qt6 and QtWidgets on Qt5.

        Importing it from QtWidgets is fine as the *fallback* — what must not
        happen is a file reaching for QtWidgets without ever trying QtGui.
        """
        moved_classes = ("QAction", "QActionGroup", "QShortcut")
        problems: List[str] = []
        for path in iter_source_files():
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(PLUGIN_ROOT).as_posix()
            gui_imports = set()
            for line in text.splitlines():
                match = re.search(
                    r"^\s*from\s+qgis\.PyQt\.QtGui\s+import\s+(.*)$", line,
                )
                if match:
                    gui_imports |= {n.strip() for n in match.group(1).split(",")}
            for lineno, line in enumerate(text.splitlines(), start=1):
                match = re.search(
                    r"^\s*from\s+qgis\.PyQt\.QtWidgets\s+import\s+(.*)$", line,
                )
                if not match:
                    continue
                imported = {n.strip() for n in match.group(1).split(",")}
                clash = (imported & set(moved_classes)) - gui_imports
                if clash:
                    problems.append(
                        f"{rel}:{lineno}: {sorted(clash)} moved to QtGui in Qt6 — "
                        "import it with a try/except ImportError over both modules"
                    )
        assert not problems, "\n  ".join([""] + problems)

    def test_guard_actually_matches_a_violation(self):
        """The scan must fail on known-bad input, not just on nothing."""
        bad = [
            "x = Qt.AlignLeft",
            "if answer == QMessageBox.Yes:",
            "dlg.exec_()",
            "from PyQt5.QtWidgets import QWidget",
            "rb.reset(QgsWkbTypes.PointGeometry)",
        ]
        for line in bad:
            hits = [
                p.search(line) for p, _ in ENUM_PATTERNS + TOKEN_PATTERNS
            ]
            assert any(hits), f"guard failed to flag {line!r}"

    def test_guard_accepts_the_scoped_spelling(self):
        """Scoped forms must not be flagged — otherwise the fix is unreachable."""
        good = [
            "x = Qt.AlignmentFlag.AlignLeft",
            "if answer == QMessageBox.StandardButton.Yes:",
            "dlg.exec()",
            "from qgis.PyQt.QtWidgets import QWidget",
            "rb.reset(QgsWkbTypes.GeometryType.PointGeometry)",
            "item.setCheckState(Qt.CheckState.Checked)",
            "combo.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)",
        ]
        for line in good:
            for pattern, _ in ENUM_PATTERNS + TOKEN_PATTERNS:
                assert not pattern.search(line), (
                    f"guard wrongly flagged the scoped form {line!r} "
                    f"via {pattern.pattern!r}"
                )
