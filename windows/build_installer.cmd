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
    exit /b 1
)

set RUNTIME=1
for /f "tokens=3" %%V in ('findstr /b /c:"RUNTIME_VERSION = " windows\launcher.py') do set RUNTIME=%%V

echo Using Inno Setup: "%ISCC%"
"%ISCC%" /DRuntimeVersion=%RUNTIME% "windows\gbserver.iss"
if errorlevel 1 exit /b 1

echo.
echo ============================================================
echo  Done!  windows\Output\gbserver-setup.exe
echo  Installs per-user, no admin rights needed. After install it
echo  updates itself from GitHub - no need to rebuild for code changes.
echo ============================================================
endlocal
