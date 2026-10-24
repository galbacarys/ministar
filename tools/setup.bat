@echo off
setlocal
rem Create the Windows-side venv and install ministar in editable mode.
rem Run this once after the first sync: tools\setup.bat
set "HERE=%~dp0"
cd /d "%HERE%.."

where uv >nul 2>&1 || (
    echo error: uv not found on PATH 1>&2
    exit /b 2
)

if not exist ".venv\Scripts\python.exe" (
    echo creating venv...
    uv venv .venv --python 3.13 || exit /b 1
)

echo installing...
uv pip install --python ".venv\Scripts\python.exe" -e ".[dev]" || exit /b 1

echo.
echo done. run: tools\ministar.bat preflight
exit /b 0
