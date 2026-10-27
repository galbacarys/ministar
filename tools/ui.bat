@echo off
setlocal

rem Launch the ministar focus screen on Windows.
rem Must keep CRLF line endings (00 section 4).
rem Pass --fake for synthetic stars with no camera attached.

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo error: no venv at "%PY%" 1>^&2
    echo        run tools\setup.bat once to create it 1>^&2
    exit /b 2
)

set "PYTHONPATH=%ROOT%\src"
"%PY%" -m ministar.ui.app %*
exit /b %errorlevel%
