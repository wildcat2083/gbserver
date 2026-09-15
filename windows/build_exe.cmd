@echo off
setlocal EnableExtensions

rem ============================================================
rem  gbserver Windows build script
rem
rem  Builds the server using PyInstaller. Requires the 64-bit Python
rem  used to create this build (3.11/3.12/3.13) installed and on PATH.
rem  Run from the project root:
rem      windows\build_exe.cmd                  (one-folder build)
rem      windows\build_exe.cmd onefile          (single-file exe)
rem
rem  - one-folder (default): dist\gbserver\gbserver.exe + _internal\,
rem    with roms\ and saves\ copied next to it. Recommended.
rem  - onefile:              dist\gbserver.exe, roms\ and saves\ copied
rem    next to it (created on first run regardless). Slower to start.
rem ============================================================

cd /d "%~dp0.."

set BUILD_VENV=build_venv
set PY=

rem Prefer the "py" launcher, falling back to "python". Verify the
rem interpreter actually works first - the Microsoft Store "python"
rem alias passes a `where python` check but cannot really run.
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=py
if defined PY goto :found_interpreter

python -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=python
if defined PY goto :found_interpreter

echo [ERROR] No working 64-bit Python found.
echo Install Python 3.11+ from https://www.python.org/downloads/
echo Enable the py launcher and "Add Python to PATH" during setup, then re-run.
exit /b 1

:found_interpreter

if exist "%BUILD_VENV%\Scripts\python.exe" goto :venv_ok
echo Creating build venv...
%PY% -m venv "%BUILD_VENV%"
if errorlevel 1 exit /b 1

:venv_ok

set PYEXE=%BUILD_VENV%\Scripts\python.exe

rem ------------------------------------------------------------
rem  Make sure the venv actually has pip. The Microsoft Store
rem  "python" alias can create a venv with no pip at all, which
rem  later breaks every source build - bootstrap it if missing.
rem ------------------------------------------------------------
"%PYEXE%" -m pip --version >nul 2>nul
if not errorlevel 1 goto :pip_ok

echo pip missing in the build venv - bootstrapping it...
"%PYEXE%" -m ensurepip --upgrade >nul 2>nul
if not errorlevel 1 goto :pip_ok

echo Downloading get-pip.py...
powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing https://bootstrap.pypa.io/get-pip.py -OutFile _get-pip.py"
if errorlevel 1 exit /b 1
"%PYEXE%" _get-pip.py
if errorlevel 1 exit /b 1
del _get-pip.py

"%PYEXE%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pip is still not available in the build venv.
    echo This usually means the Microsoft Store Python is being used.
    echo Install the official 64-bit Python, delete the build_venv folder, and re-run.
    exit /b 1
)

:pip_ok

echo Upgrading pip...
"%PYEXE%" -m pip install --quiet --upgrade pip
if errorlevel 1 exit /b 1

echo Installing dependencies...
"%PYEXE%" -m pip install --quiet -r windows\requirements-win.txt
if errorlevel 1 exit /b 1

if /i "%~1"=="onefile" goto :build_onefile

echo Building gbserver.exe (one-folder) - this can take a few minutes...
"%PYEXE%" -m PyInstaller --noconfirm --clean windows\gbserver.spec
if errorlevel 1 exit /b 1

echo.
echo Copying ROM library and saves next to the exe...
if exist roms robocopy roms dist\gbserver\roms /E /NFL /NDL /NJH /NJS /NC /NS >nul
if exist saves robocopy saves dist\gbserver\saves /E /NFL /NDL /NJH /NJS /NC /NS >nul
goto :done

:build_onefile

echo Building gbserver.exe (single-file) - this can take longer...
"%PYEXE%" -m PyInstaller --noconfirm --clean windows\gbserver_onefile.spec
if errorlevel 1 exit /b 1

echo.
echo Copying ROM library and saves next to the one-file exe...
if exist roms robocopy roms dist\roms /E /NFL /NDL /NJH /NJS /NC /NS >nul
if exist saves robocopy saves dist\saves /E /NFL /NDL /NJH /NJS /NC /NS >nul
goto :done

:done

echo.
echo ============================================================
echo  Done!
if /i "%~1"=="onefile" goto :done_onefile
echo  Run the server with:  dist\gbserver\gbserver.exe
goto :done_common

:done_onefile
echo  Run the server with:  dist\gbserver.exe

:done_common
echo  Then open:            http://127.0.0.1:8080/
echo  Dashboard:            http://127.0.0.1:8080/dashboard
echo  Windows Firewall may prompt to allow the port - allow it.
echo.
echo  To build a proper Windows installer once Inno Setup 6 is
echo  installed, run:  windows\build_installer.cmd
echo ============================================================
endlocal