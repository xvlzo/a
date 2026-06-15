@echo off
setlocal

:: ── Settings — edit these ─────────────────────────────────────────────────────
set TARGET_KPH=160
set HUMANIZATION=0.7
set SMOOTHNESS=0.6
set SAFETY=1.2
set CAR=0
set SPLINE=data\fast_lane.aipp

:: ── Sanity checks ─────────────────────────────────────────────────────────────
set EXE=build\Release\nohesi_bot.exe

if not exist "%EXE%" (
    echo [RUN] %EXE% not found — run build.bat first.
    pause & exit /b 1
)

if not exist "%SPLINE%" (
    echo [RUN] %SPLINE% not found.
    echo       Copy fast_lane.aip from:
    echo       assettocorsa\content\tracks\shuto_revival_project_beta\ai\fast_lane.aip
    pause & exit /b 1
)

:: ── Launch ────────────────────────────────────────────────────────────────────
echo ================================================================
echo   No Hesi Bot
echo   Speed: %TARGET_KPH% kph   Safety: %SAFETY% m   Human: %HUMANIZATION%
echo   Ctrl+C to stop. Press F5 in-game to enable.
echo ================================================================
echo.

"%EXE%" ^
    --fast-lane  "%SPLINE%"      ^
    --target-kph %TARGET_KPH%    ^
    --humanization %HUMANIZATION% ^
    --smoothness %SMOOTHNESS%    ^
    --safety     %SAFETY%        ^
    --car        %CAR%
