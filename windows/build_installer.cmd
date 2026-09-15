@echo off
setlocal EnableExtensions

rem ============================================================
rem  gbserver installer build
rem
rem  Builds the one-folder app first if needed, then compiles
rem  windows\gbserver.iss with Inno Setup 6 into
rem  windows\Output\gbserver-setup.exe
rem
rem  Requires Inno Setup 6: https://jrsoftware.org/isdl.php
rem  Usage:  windows\build_installer.cmd
rem ============================================================

cd /d "%~dp0.."

if not exist "dist\gbserver\gbserver.exe" (
    echo The app has not been built yet - building it now...
    call windows\build_exe.cmd
    if errorlevel 1 exit /b 1
)

set ISCC=

rem Check Inno Setup's usual install locations first:
rem   C:\Program Files (x86)\Inno Setup 6 - per-machine x64
rem   C:\Program Files\Inno Setup 6       - per-machine
rem   %LOCALAPPDATA%\Programs\Inno Setup 6 - per-user "for me only"
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if not defined ISCC if exist "%%~P" set "ISCC=%%~P"
)

rem winget/chocolatey/scoop installs usually aren't under Program Files -
rem find it on PATH instead (choco/scoop place an iscc shim there).
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

echo Using Inno Setup: "%ISCC%"
echo Compiling installer...
"%ISCC%" "windows\gbserver.iss"
if errorlevel 1 exit /b 1

echo.
echo ============================================================
echo  Done!  windows\Output\gbserver-setup.exe  is ready to
echo  distribute - double-click it on the target machine to install.
echo  Installs per-user with Start Menu and Desktop shortcuts plus
echo  an uninstaller. No admin rights needed.
echo ============================================================
endlocal