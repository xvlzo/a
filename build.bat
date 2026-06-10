@echo off
setlocal

:: ── Configure ────────────────────────────────────────────────────────────────
:: Change the generator if you have VS 2019: "Visual Studio 16 2019"
cmake -B build -G "Visual Studio 17 2022" -A x64
if errorlevel 1 (
    echo.
    echo [BUILD] CMake configure failed.
    echo         Make sure CMake and Visual Studio 2022 C++ workload are installed.
    pause & exit /b 1
)

:: ── Build ─────────────────────────────────────────────────────────────────────
cmake --build build --config Release
if errorlevel 1 (
    echo.
    echo [BUILD] Compile failed — see errors above.
    pause & exit /b 1
)

echo.
echo [BUILD] OK  ^>  build\Release\nohesi_bot.exe
