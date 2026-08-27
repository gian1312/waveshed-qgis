@echo off
setlocal
rem ---------------------------------------------------------------------------
rem run_torture.bat - run the torture suite STANDALONE (the only way tier C runs)
rem
rem Usage:  run_torture.bat [runner args]
rem         run_torture.bat                        full suite (tiers abc)
rem         run_torture.bat --rows 4.1 --tier c    one row, pipeline tier only
rem
rem Finds python-qgis.bat in this order:
rem   1. TORTURE_PYQGIS environment variable (full path to python-qgis.bat)
rem   2. next to the qgis_exe configured in torture.local.ini
rem   3. C:\OSGeo4W\bin\python-qgis.bat
rem   4. newest "C:\Program Files\QGIS *\bin\python-qgis.bat"
rem
rem Remember: plugin code changed -> run  python deploy.py  first. Tier C tests
rem THIS repo's plugin (the pairing check enforces it); the engine binaries and
rem QGIS settings are still the installed ones.
rem ---------------------------------------------------------------------------

set "PYQGIS="

if defined TORTURE_PYQGIS if exist "%TORTURE_PYQGIS%" set "PYQGIS=%TORTURE_PYQGIS%"
if defined PYQGIS goto found

rem -- torture.local.ini: qgis_exe = <...>\qgis-bin.exe ----------------------
if not exist "%~dp0torture.local.ini" goto try_osgeo4w
set "QGIS_EXE="
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /r /c:"qgis_exe *=" "%~dp0torture.local.ini"`) do set "QGIS_EXE=%%B"
if not defined QGIS_EXE goto try_osgeo4w
for /f "tokens=* delims= " %%T in ("%QGIS_EXE%") do set "QGIS_EXE=%%T"
if not defined QGIS_EXE goto try_osgeo4w
for %%I in ("%QGIS_EXE%") do set "CANDIDATE=%%~dpIpython-qgis.bat"
if exist "%CANDIDATE%" set "PYQGIS=%CANDIDATE%"
if defined PYQGIS goto found

:try_osgeo4w
if exist "C:\OSGeo4W\bin\python-qgis.bat" set "PYQGIS=C:\OSGeo4W\bin\python-qgis.bat"
if defined PYQGIS goto found

rem -- standalone installers: alphabetically last match = newest version -----
for /d %%D in ("C:\Program Files\QGIS *") do if exist "%%D\bin\python-qgis.bat" set "PYQGIS=%%D\bin\python-qgis.bat"
if defined PYQGIS goto found

echo [run_torture] Could not find python-qgis.bat.
echo   Set TORTURE_PYQGIS to its full path, or set qgis_exe in torture.local.ini.
exit /b 2

:found
echo [run_torture] using "%PYQGIS%"
pushd "%~dp0"
call "%PYQGIS%" "%~dp0tools\torture_runner.py" %*
set "STATUS=%ERRORLEVEL%"
popd
exit /b %STATUS%
