@echo off
setlocal
rem ---------------------------------------------------------------------------
rem make_torture.bat - (re)generate the torture project with QGIS's Python
rem
rem Usage:  make_torture.bat [generator args]
rem         make_torture.bat                         all stages (fetch,fabricate,vectors,project)
rem         make_torture.bat --stages project        rewrite manifest + .qgs only
rem         make_torture.bat --list                  print the catalogue, build nothing
rem
rem Finds python-qgis.bat in this order:
rem   1. TORTURE_PYQGIS environment variable (full path to python-qgis.bat)
rem   2. next to the qgis_exe configured in torture.local.ini
rem   3. C:\OSGeo4W\bin\python-qgis.bat
rem   4. newest "C:\Program Files\QGIS *\bin\python-qgis.bat"
rem
rem Do NOT exec() the generator from the QGIS console: its dataclasses need a
rem real module namespace. The fabricate stage needs GDAL + numpy, which is why
rem this goes through python-qgis.bat. Retires the old results pair as STALE.
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

echo [make_torture] Could not find python-qgis.bat.
echo   Set TORTURE_PYQGIS to its full path, or set qgis_exe in torture.local.ini.
exit /b 2

:found
echo [make_torture] using "%PYQGIS%"
pushd "%~dp0"
call "%PYQGIS%" "%~dp0tools\make_torture_project.py" %*
set "STATUS=%ERRORLEVEL%"
popd
exit /b %STATUS%
