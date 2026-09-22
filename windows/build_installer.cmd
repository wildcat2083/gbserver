@echo off
setlocal EnableExtensions

rem ============================================================
rem  gbserver installer build
rem
rem  Rebuilds dist\gbserver\ (so the runtime and offline snapshot are
rem  current), then compiles windows\gbserver.iss with Inno Setup 6 into
rem  windows\Output\gbserver-setup.exe
rem
rem  You only need a new installer when the bundled runtime changes
rem  (RUNTIME_VERSION in windows\launcher.py). Code changes reach every
rem  install automatically from GitHub.
rem ============================================================

cd /d "%~dp0.."

call windows\build_exe.cmd
if errorlevel 1 exit /b 1

set ISCC=
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if not defined ISCC if exist "%%~P" set "ISCC=%%~P"
)
if not defined ISCC (
    for /f "delims=" %%P in ('where ISCC.exe 2^>nul') do (
        if not defined ISCC set "ISCC=%%~P"
    )
)
if not defined ISCC (
    echo [ERROR] Inno Setup 6 compiler not found.
    echo Install Inno Setup 6 from https://jrsoftware.org/isdl.php and re-run.
    echo.
    pause
    exit /b 1
)

rem ------------------------------------------------------------------
rem  Where the installer lands. %~dp0 is this script's own folder
rem  (windows\), so the output path is absolute and unambiguous no
rem  matter which directory ISCC happens to use as its cwd.
rem ------------------------------------------------------------------
set OUTDIR=%~dp0Output
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

set RUNTIME=1
for /f "tokens=3" %%V in ('findstr /b /c:"RUNTIME_VERSION = " windows\launcher.py') do set RUNTIME=%%V

echo Using Inno Setup: "%ISCC%"
echo Building installer into: %OUTDIR%
"%ISCC%" /DRuntimeVersion=%RUNTIME% /O"%OUTDIR%" "windows\gbserver.iss"
if errorlevel 1 (
    echo [ERROR] Inno Setup compiler failed - see messages above.
    echo         Looked for output in: %OUTDIR%
    exit /b 1
)

if not exist "%OUTDIR%\gbserver-setup.exe" (
    echo [ERROR] ISCC reported success but gbserver-setup.exe is missing!
    echo         Expected: "%OUTDIR%\gbserver-setup.exe"
    echo         Check Inno Setup output above and the Output folder.
    exit /b 1
)

echo.
echo ============================================================
echo  Done!  "%OUTDIR%\gbserver-setup.exe"
echo  Installs per-user, no admin rights needed. After install it
echo  updates itself from GitHub - no need to rebuild for code changes.
echo ============================================================
endlocal
