@echo off
REM ============================================================
REM  ProTube Saver — fresh demo toggle
REM ============================================================
REM  Drop this file NEXT TO ProTubeSaver.exe.
REM  Double-click to toggle between real data and a clean demo state.
REM
REM  Two states this script flips between:
REM    REAL state    : data\          (your library / settings)
REM    DEMO state    : data\ (empty)  + data.bak\ (your real data, parked)
REM
REM  Run once  -> backs up data\ to data.bak\, creates empty data\.
REM               Launch the app from a fresh slate. Record your demo.
REM  Run again -> deletes demo data\, restores data.bak\ as data\.
REM               You're back exactly where you were before.
REM
REM  Refuses to run while ProTubeSaver.exe is running so it never
REM  fights an open settings.json handle.
REM ============================================================

setlocal enabledelayedexpansion

REM Always operate on the folder this .bat lives in, regardless of
REM what folder cmd was launched from.
cd /d "%~dp0"

echo.
echo  ProTube Saver - fresh demo toggle
echo  ---------------------------------
echo  Working in: %CD%
echo.

REM ------------------------------------------------------------
REM  Refuse if the app is running. We check for *any* .exe in the
REM  same folder being in tasklist (most likely ProTubeSaver.exe,
REM  but works even if you renamed the build).
REM ------------------------------------------------------------
set "APP_RUNNING="
for %%F in (*.exe) do (
    tasklist /FI "IMAGENAME eq %%~nxF" 2>NUL | find /I "%%~nxF" >NUL
    if not errorlevel 1 set "APP_RUNNING=%%~nxF"
)
if defined APP_RUNNING (
    echo  ERROR: %APP_RUNNING% is currently running.
    echo  Close the app and run this script again.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM  Decide which way to flip based on what's on disk.
REM ------------------------------------------------------------
if exist "data.bak\" (
    echo  Currently in DEMO mode  ^(real data parked at data.bak\^)
    echo.
    echo  This will:
    echo    1. delete the demo data\ folder
    echo    2. restore data.bak\ as data\
    echo.
    choice /C YN /N /M "  Restore real data? (Y/N) "
    if errorlevel 2 (
        echo  Cancelled. No changes made.
        echo.
        pause
        exit /b 0
    )
    if exist "data\" (
        rmdir /S /Q "data"
        if errorlevel 1 (
            echo  ERROR: failed to remove data\. Close anything that has files
            echo  in that folder open and try again.
            pause
            exit /b 1
        )
    )
    ren "data.bak" "data"
    if errorlevel 1 (
        echo  ERROR: failed to rename data.bak\ back to data\.
        pause
        exit /b 1
    )
    echo.
    echo  DONE. Real data is restored.
    echo  Launching the app will load your library/settings as before.
    echo.
    pause
    exit /b 0
)

if exist "data\" (
    echo  Currently using REAL data.
    echo.
    echo  This will:
    echo    1. rename data\ to data.bak\  ^(your real data, untouched^)
    echo    2. create an empty data\ folder for the demo
    echo.
    choice /C YN /N /M "  Enter demo mode? (Y/N) "
    if errorlevel 2 (
        echo  Cancelled. No changes made.
        echo.
        pause
        exit /b 0
    )
    ren "data" "data.bak"
    if errorlevel 1 (
        echo  ERROR: failed to rename data\ to data.bak\.
        pause
        exit /b 1
    )
    mkdir "data"
    if errorlevel 1 (
        echo  ERROR: failed to create empty data\ folder.
        echo  Trying to roll back...
        ren "data.bak" "data"
        pause
        exit /b 1
    )
    REM Drop a .migrated_from_legacy marker so app_paths.migrate_legacy() skips
    REM repopulating the empty data\ folder from ~/Downloads/ProTube Saver/.
    REM Without this, the next app launch silently restores your real settings
    REM into the demo folder and the demo state vanishes.
    echo demo-mode-skip-migration> "data\.migrated_from_legacy"
    echo.
    echo  DONE. Demo mode active.
    echo  Launching the app will look like a fresh first install.
    echo  Run this script again when you finish recording to restore real data.
    echo.
    pause
    exit /b 0
)

echo  No data\ folder found in this directory.
echo.
echo  This means either:
echo    a^) ProTubeSaver.exe has not been launched here yet, OR
echo    b^) this .bat is in the wrong folder.
echo.
echo  Drop this .bat next to ProTubeSaver.exe and launch the app once.
echo  The app creates data\ on first launch. Then re-run this script.
echo.
pause
exit /b 0
