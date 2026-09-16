@echo off
setlocal EnableExtensions

rem ============================================================
rem  gbserver auto-updating build
rem
rem  Builds dist\gbserver\gbserver.exe: Python + every library gbserver
rem  needs, plus the launcher that downloads gbserver's code from GitHub
rem  and keeps it up to date. Also bundles a snapshot of the current
rem  commit (dist\gbserver\seed\) so a fresh install can start offline.
rem
rem  Requires 64-bit Python 3.11+ on PATH (or the py launcher), and git
rem  for the offline snapshot. Run from anywhere:
rem      windows\build_exe.cmd
rem ============================================================

cd /d "%~dp0.."

set BUILD_VENV=build_venv
set PY=

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=py -3
if defined PY goto :found_interpreter
python -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=python
if defined PY goto :found_interpreter
echo [ERROR] No working 64-bit Python found.
echo Install Python 3.11+ from https://www.python.org/downloads/ and re-run.
exit /b 1

:found_interpreter
if exist "%BUILD_VENV%\Scripts\python.exe" goto :venv_ok
echo Creating build venv...
%PY% -m venv "%BUILD_VENV%"
if errorlevel 1 exit /b 1

:venv_ok
set PYEXE=%BUILD_VENV%\Scripts\python.exe

"%PYEXE%" -m pip --version >nul 2>nul
if not errorlevel 1 goto :pip_ok
echo pip missing in the build venv - bootstrapping it...
"%PYEXE%" -m ensurepip --upgrade >nul 2>nul
"%PYEXE%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pip is not available in the build venv. Install the official
    echo 64-bit Python ^(not the Microsoft Store one^), delete build_venv, re-run.
    exit /b 1
)

:pip_ok
echo Installing build dependencies...
"%PYEXE%" -m pip install --quiet --upgrade pip
if errorlevel 1 exit /b 1
"%PYEXE%" -m pip install --quiet --upgrade -r windows\requirements-win.txt
if errorlevel 1 exit /b 1

echo Building gbserver.exe - this takes a few minutes...
"%PYEXE%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build windows\gbserver.spec
if errorlevel 1 exit /b 1

rem ------------------------------------------------------------
rem  Offline snapshot of the committed code (not uncommitted edits),
rem  so the very first launch works without internet. The launcher
rem  replaces it with the latest GitHub version as soon as it can.
rem ------------------------------------------------------------
if exist "dist\gbserver\seed" rmdir /s /q "dist\gbserver\seed"
where git >nul 2>nul
if errorlevel 1 (
    echo [warn] git not found - no offline snapshot; first launch will need internet.
    goto :done
)
for /f %%C in ('git rev-parse HEAD') do set SEED_COMMIT=%%C
if not defined SEED_COMMIT (
    echo [warn] not a git checkout - no offline snapshot; first launch will need internet.
    goto :done
)
mkdir "dist\gbserver\seed\source"
git archive --format=zip -o "build\seed.zip" HEAD
if errorlevel 1 exit /b 1
powershell -NoProfile -Command "Expand-Archive -Force 'build\seed.zip' 'dist\gbserver\seed\source'"
if errorlevel 1 exit /b 1
if exist "dist\gbserver\seed\source\roms" rmdir /s /q "dist\gbserver\seed\source\roms"
if exist "dist\gbserver\seed\source\saves" rmdir /s /q "dist\gbserver\seed\source\saves"
> "dist\gbserver\seed\seed.json" echo {"commit": "%SEED_COMMIT%"}
echo Bundled offline snapshot of commit %SEED_COMMIT%

:done
echo.
echo ============================================================
echo  Done!  dist\gbserver\gbserver.exe
echo  Build the installer with:  windows\build_installer.cmd
echo ============================================================
endlocal
